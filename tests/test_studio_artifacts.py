"""Actual files and bounded previews, with no provider calls."""
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ap_mind.contracts import ContractError
from ap_mind.studio_server import StudioEpisodeService
from ap_mind import studio_artifacts


@pytest.fixture
def files(tmp_path):
    root = tmp_path / 'project'; root.mkdir()
    workspace = tmp_path / 'outputs'; workspace.mkdir()
    service = StudioEpisodeService(tmp_path / 'data', project_root=root, codex_project_id='test-project')
    with service.product_registry.transaction():
        service.product_registry._connect().execute('INSERT INTO studio_runs VALUES (?,?,?,?,?,?)',
            ('fixture-run', 'fixture-request', 'fixture-fingerprint', 'fixture-agent', 'awaiting_review',
             json.dumps({'workspace': str(workspace)})))
    yield service.agent_studio.artifacts, workspace
    service.close()


def test_real_report_preview_hash_and_no_state_write(files):
    artifacts, root = files
    content = '# 核验\n\n|项目|结论|\n|---|---|\n|费用|需要说明|\n'
    blob = content.encode()
    (root / 'acceptance.md').write_bytes(blob)
    (root / 'another.txt').write_text('another')
    listing = artifacts.list('fixture-run')
    assert listing['files'][0]['name'] == 'acceptance.md'
    value = artifacts.read('fixture-run', 'acceptance.md')
    assert value['text'] == content and value['kind'] == 'markdown'
    assert value['sha256'] == hashlib.sha256(blob).hexdigest()
    assert not value['truncated'] and value['hash_scope'] == 'complete_file'
    assert (root / 'acceptance.md').read_bytes() == blob
    with artifacts.studio.registry._connect() as c:
        assert c.execute('SELECT state FROM studio_runs').fetchone()[0] == 'awaiting_review'


@pytest.mark.parametrize('name', ['../outside.txt', '..\\outside.txt', '/etc/passwd', 'C:\\Windows\\win.ini', 'file:stream', '.env', '.env.local', '.ssh/id_rsa', 'private.pem'])
def test_private_or_escaping_paths_cannot_be_read(files, name):
    artifacts, _ = files
    with pytest.raises(ContractError, match='artifact_(path_invalid|private_file)'):
        artifacts.read('fixture-run', name)


def test_missing_and_unknown_run_are_distinct(files):
    artifacts, root = files
    with pytest.raises(ContractError, match='artifact_not_found'):
        artifacts.read('fixture-run', 'removed.md')
    with pytest.raises(ContractError, match='run_not_found'):
        artifacts.list('unknown-run')
    root.rmdir()
    assert artifacts.list('fixture-run')['workspace_exists'] is False


def test_bounded_utf8_empty_binary_and_redaction(files, monkeypatch):
    artifacts, root = files
    (root / 'empty.txt').write_bytes(b'')
    assert artifacts.read('fixture-run', 'empty.txt')['text'] == ''
    (root / 'binary.png').write_bytes(b'\x89PNG\x00\xff')
    assert artifacts.read('fixture-run', 'binary.png')['kind'] == 'binary'
    monkeypatch.setattr(studio_artifacts, 'MAX_PREVIEW_BYTES', 7)
    (root / 'large.txt').write_text('文本很长', encoding='utf-8')
    truncated = artifacts.read('fixture-run', 'large.txt')
    assert truncated['text'] == '文本' and truncated['truncated']
    assert truncated['sha256'] is None and truncated['hash_scope'] == 'unavailable'
    monkeypatch.setattr(studio_artifacts, 'MAX_PREVIEW_BYTES', 128 * 1024)
    secret = 'fixture-sensitive-value-123'
    (root / 'config-example.json').write_text(json.dumps({'api_key': secret}))
    preview = artifacts.read('fixture-run', 'config-example.json')
    assert secret not in preview['text'] and preview['redacted']
    assert secret in (root / 'config-example.json').read_text()
    (root / '.env').write_text('secret')
    assert '.env' not in [f['name'] for f in artifacts.list('fixture-run')['files']]


def test_symlink_escape_is_not_followed(files):
    artifacts, root = files
    outside = root.parent / 'outside.txt'; outside.write_text('outside')
    try:
        (root / 'link.txt').symlink_to(outside)
    except OSError:
        pytest.skip('This Windows account cannot create symlinks')
    assert artifacts.list('fixture-run')['files'] == []
    with pytest.raises(ContractError, match='path_invalid'):
        artifacts.read('fixture-run', 'link.txt')


def test_listing_stops_at_budget_and_marks_partial(files, monkeypatch):
    artifacts, root = files
    for index in range(4): (root / f'{index}.txt').write_text('file')
    monkeypatch.setattr(studio_artifacts, 'MAX_ENTRIES', 2)
    result = artifacts.list('fixture-run')
    assert result['truncated'] and len(result['files']) == 2
