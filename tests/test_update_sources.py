import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from tools import update_client, update_sources
from test_update_client import package

REPO = 'ginsonko/ap-vibe'
API = 'https://api.github.com/repos/' + REPO + '/releases?per_page=10'
RAW = 'https://raw.githubusercontent.com/' + REPO + '/main/ap-vibe-version.json'


class Response(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(json.dumps(data).encode())
        self.headers = headers or {}


def limited(req, timeout):
    if req.full_url == API:
        raise HTTPError(API, 403, 'rate limit exceeded', {'X-RateLimit-Reset':'2000'}, None)
    assert req.full_url == RAW
    return Response({'product':'AP-Vibe', 'version':'v0.2.0-beta.1'})


def config(tmp_path, **extra):
    cfg = tmp_path/'config.json'
    cfg.write_text(json.dumps({'product_root':str(tmp_path), 'installed_version':'v0.1.0-beta.1',
                               'updates':{'auto_install':False}, **extra}))
    return cfg


@pytest.mark.parametrize('status', [403,429])
def test_api_limit_uses_raw_and_stages_verified_release_without_changing_config(tmp_path, monkeypatch, status):
    archive, manifest = package(tmp_path)
    def transport(req, timeout):
        if req.full_url == API:
            raise HTTPError(API,status,'rate limit exceeded',{'X-RateLimit-Reset':'2000'},None)
        return limited(req, timeout)
    monkeypatch.setattr(update_sources.request, 'urlopen', transport)
    monkeypatch.setattr(update_sources.time, 'time', lambda:1000)
    payload = archive.read_bytes()
    cfg = config(tmp_path)
    before = cfg.read_bytes()
    calls = []
    def fetch(url, limit):
        calls.append(url)
        assert url.startswith('https://github.com/ginsonko/ap-vibe/releases/download/v0.2.0-beta.1/')
        return json.dumps(manifest).encode() if url.endswith('.json') else payload
    monkeypatch.setattr(update_client, 'fetch', fetch)
    result = update_client.check(cfg)
    assert result['state'] == 'ready' and result['discovery_source'] == 'github_raw'
    assert result['api_retry_at'] == 2000 and str(status) in result['discovery_error']
    assert update_client.verify(result['candidate_root'], manifest)
    assert cfg.read_bytes() == before
    assert update_client.check(cfg, force=True)['state'] == 'ready'
    assert len([u for u in calls if u.endswith('.zip')]) == 1


def test_unpublished_marker_cannot_report_current(tmp_path, monkeypatch):
    monkeypatch.setattr(update_sources.request, 'urlopen', limited)
    def unavailable(url, limit):
        raise HTTPError(url,404,'unpublished',{},None)
    monkeypatch.setattr(update_client,'fetch',unavailable)
    result = update_client.check(config(tmp_path,installed_version='v0.2.0-beta.1'))
    assert result['state'] == 'check_failed' and result['failed_phase'] == 'manifest'


def test_manifest_archive_hash_cannot_redirect_cached_stage(tmp_path):
    _, manifest = package(tmp_path)
    manifest['sha256'] = '../unexpected'
    with pytest.raises(ValueError, match='archive_digest_invalid'):
        update_client.validate_manifest(manifest)


def test_manual_check_during_reset_skips_api_and_retains_failure_reason(tmp_path, monkeypatch):
    calls = []
    def offline(req, timeout):
        calls.append(req.full_url)
        if req.full_url == API:
            return limited(req, timeout)
        raise URLError('raw offline')
    monkeypatch.setattr(update_sources.request, 'urlopen', offline)
    monkeypatch.setattr(update_sources.time, 'time', lambda:1000)
    cfg = config(tmp_path)
    for _ in range(2):
        result = update_client.check(cfg, force=True)
        assert result['state'] == 'check_failed' and result['next_check_at'] == 2000
        assert '403' in result['discovery_error'] and 'raw offline' in result['fallback_error']
    assert calls.count(API) == 1 and calls.count(RAW) == 2


def test_conditional_304_reuses_server_confirmed_catalog(monkeypatch):
    bodies = [{'tag_name':'v1.0.0', 'assets':[]}]
    seen = []
    def transport(req, timeout):
        seen.append(req)
        if len(seen) == 1:
            return Response(bodies, {'ETag':'"catalog-v1"'})
        assert req.get_header('If-none-match') == '"catalog-v1"'
        raise HTTPError(API, 304, 'not modified', {}, None)
    monkeypatch.setattr(update_sources.request, 'urlopen', transport)
    cache = {}
    for _ in range(2):
        release, info = update_sources.discover(REPO, 'beta', cache)
        assert release == bodies[0] and info['discovery_source'] == 'github_api'


def test_stable_preference_never_falls_back_to_beta(monkeypatch):
    monkeypatch.setattr(update_sources.request, 'urlopen', limited)
    with pytest.raises(update_sources.DiscoveryError, match='保留仅正式版偏好'):
        update_sources.discover(REPO, 'stable', {})


@pytest.mark.parametrize('failure', ['missing', 'tag', 'digest'])
def test_raw_marker_never_bypasses_published_manifest_or_archive(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(update_sources.request, 'urlopen', limited)
    archive, manifest = package(tmp_path)
    if failure == 'tag':
        manifest['version'] = 'v9.0.0'
    def fetch(url, limit):
        if failure == 'missing':
            raise HTTPError(url, 404, 'unpublished', {}, None)
        return json.dumps(manifest).encode() if url.endswith('.json') else b'corrupt archive'
    monkeypatch.setattr(update_client, 'fetch', fetch)
    cfg = config(tmp_path)
    original = cfg.read_bytes()
    result = update_client.check(cfg)
    assert not result['ok'] and result['state'] == 'check_failed'
    assert result['failed_phase'] == ('verify' if failure == 'digest' else 'manifest')
    assert cfg.read_bytes() == original and 'candidate_root' not in result


def test_raw_old_release_never_downgrades(tmp_path, monkeypatch):
    monkeypatch.setattr(update_sources.request, 'urlopen', limited)
    _, manifest = package(tmp_path)
    calls = []
    def fetch(url, limit):
        calls.append(url)
        return json.dumps(manifest).encode()
    monkeypatch.setattr(update_client, 'fetch', fetch)
    result = update_client.check(config(tmp_path, installed_version='v0.4.0-beta.1'))
    assert result['state'] == 'current' and len(calls) == 1 and calls[0].endswith('.json')


@pytest.mark.parametrize('headers,expected', [({'Retry-After':'120'},1120),
    ({'Retry-After':'Thu, 01 Jan 1970 00:30:00 GMT'},1800),
    ({'X-RateLimit-Reset':'3000','Retry-After':'20'},3000), ({},1060)])
def test_server_retry_headers(headers, expected):
    assert update_sources.retry_time(headers,1000) == expected


@pytest.mark.parametrize('mode', ['automatic','check','off'])
def test_failed_check_retries_after_deadline_without_new_prompt(monkeypatch, mode):
    from ap_mind.studio_updates import StudioUpdates
    updates = StudioUpdates(SimpleNamespace())
    status = {'mode':mode, 'state':'check_failed', 'next_check_at':2000}
    monkeypatch.setattr(updates, 'status', lambda:status)
    calls = []
    monkeypatch.setattr(updates, 'action', lambda raw:calls.append(raw))
    monkeypatch.setattr('ap_mind.studio_updates.time.time', lambda:1000)
    updates.tick()
    assert not calls
    updates.next_poll = 0
    monkeypatch.setattr('ap_mind.studio_updates.time.time', lambda:2001)
    updates.tick()
    assert bool(calls) == (mode != 'off')
