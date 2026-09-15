"""Public release discovery, conditional caching and GitHub rate-limit recovery."""
from email.utils import parsedate_to_datetime
import json
import re
import time
from urllib import error, request


class DiscoveryError(RuntimeError):
    def __init__(self, message, details):
        super().__init__(message)
        self.details = details


def retry_time(headers, now):
    """Honor both primary quota reset and secondary Retry-After responses."""
    times = [now + 60]
    try:
        times.append(float(headers.get('X-RateLimit-Reset', 0)))
    except (TypeError, ValueError):
        pass
    value = headers.get('Retry-After')
    if value:
        try:
            times.append(now + float(value))
        except ValueError:
            try:
                times.append(parsedate_to_datetime(value).timestamp())
            except (TypeError, ValueError, OverflowError):
                pass
    return max(times)


def cached_json(url, cache, *, limit=2*1024*1024):
    if not isinstance(cache.get('entries'), dict):
        cache['entries'] = {}
    entries = cache['entries']
    entry = entries.get(url, {})
    if not isinstance(entry, dict):
        entry = {}
    headers = {'User-Agent': 'AP-Vibe-Updater', 'Accept': 'application/json'}
    if entry.get('etag') and 'body' in entry:
        headers['If-None-Match'] = entry['etag']
    try:
        with request.urlopen(request.Request(url, headers=headers), timeout=20) as response:
            data = response.read(limit + 1)
            if len(data) > limit:
                raise ValueError('release_discovery_too_large')
            value = json.loads(data)
            entries[url] = {'body': value, 'etag': response.headers.get('ETag')}
            return value
    except error.HTTPError as exc:
        if exc.code == 304 and 'body' in entry:
            return entry['body']
        raise


def discover(repo, channel, cache):
    """Fallback hints are never installation proof; callers must verify assets."""
    now = time.time()
    details = {}
    api_url = 'https://api.github.com/repos/' + repo + '/releases?per_page=10'
    try:
        retry_at = float(cache.get('api_retry_at', 0))
    except (TypeError, ValueError):
        retry_at = 0
    if retry_at > now:
        details.update(discovery_error=cache.get('api_error', 'GitHub API 限流'), api_retry_at=retry_at)
    else:
        try:
            releases = cached_json(api_url, cache)
            if not isinstance(releases, list) or any(not isinstance(r, dict) for r in releases):
                raise ValueError('release_catalog_invalid')
            cache.pop('api_retry_at', None)
            cache.pop('api_error', None)
            release = next((r for r in releases if not r.get('draft') and
                            (channel == 'beta' or not r.get('prerelease'))), None)
            return release, {'discovery_source': 'github_api'}
        except (error.URLError, OSError, ValueError) as exc:
            details['discovery_error'] = str(exc)[:250]
            if isinstance(exc, error.HTTPError) and exc.code in (403, 429):
                retry_at = retry_time(exc.headers, now)
                cache.update(api_retry_at=retry_at, api_error=details['discovery_error'])
                details['api_retry_at'] = retry_at
    try:
        marker = cached_json('https://raw.githubusercontent.com/' + repo + '/main/ap-vibe-version.json', cache, limit=65536)
        version = marker.get('version', '') if isinstance(marker, dict) else ''
        if marker.get('product') != 'AP-Vibe' or not re.fullmatch(r'v\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?', version):
            raise ValueError('release_version_marker_invalid')
        if channel != 'beta' and '-' in version:
            raise ValueError('备用入口目前指向内测版，保留仅正式版偏好，稍后重试正式版查询')
        prefix = 'https://github.com/' + repo + '/releases/download/' + version + '/'
        release = {'tag_name': version, 'draft': False, 'prerelease': '-' in version,
                   'assets': [{'name': name, 'browser_download_url': prefix + name}
                              for name in ('ap-vibe-manifest.json', 'ap-vibe-app.zip')]}
        details.update(discovery_source='github_raw',
                       message='已通过 GitHub 公开版本文件查询；安装前仍校验同版本的完整发布包。')
        return release, details
    except (error.URLError, OSError, ValueError, TypeError, AttributeError) as exc:
        details['fallback_error'] = str(exc)[:250]
        raise DiscoveryError('GitHub 版本查询未完成：' + details['discovery_error'] +
                             '；备用入口：' + details['fallback_error'], details) from exc
