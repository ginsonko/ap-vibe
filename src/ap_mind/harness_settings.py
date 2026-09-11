"""Configure native discovery paths without touching the applications' data."""
import json
import hashlib
import os
from pathlib import Path
import uuid

from .contracts import ContractError
from .harness_registry import HARNESS


def read(external):
    with external._lock:
        raw = json.loads(external.config_path.read_text('utf-8-sig')) if external.config_path.exists() else {}
        roots, warnings = external._settings()
        coverage = external.discover()['coverage']
        return {'ok': True, 'revision': raw.get('revision', 0), 'warnings': warnings,
            'sources': [{'harness': k, 'name': v['name'],
                'enabled': raw.get('sources', {}).get(k, {}).get('enabled', True),
                'custom_roots': raw.get('sources', {}).get(k, {}).get('roots'),
                'roots': [str(p) for p in roots.get(k, [])],
                'discovered': coverage.get(k, {}).get('total_discovered', 0),
                'executor': v['executor'], 'available_root_count': coverage.get(k, {}).get('available_root_count', 0)}
                for k, v in HARNESS.items() if k not in {'codex', 'claude'}]}


def save(external, request):
    with external._lock:
        path = external.config_path
        original = path.read_bytes() if path.exists() else None
        raw = json.loads(original.decode('utf-8-sig')) if original else {}
        updates = request.get('sources')
        if not isinstance(updates, dict) or not updates:
            raise ContractError('harness_sources_invalid')
        fingerprint=hashlib.sha256(json.dumps({'sources':updates,'expected_revision':request.get('expected_revision')},sort_keys=True).encode()).hexdigest()
        if request.get('request_id') and raw.get('last_request_id') == request['request_id']:
            if raw.get('last_request_fingerprint') not in (None,fingerprint):
                raise ContractError('harness_settings_request_conflict')
            return read(external)
        if request.get('expected_revision') != raw.get('revision', 0):
            raise ContractError('harness_settings_revision_conflict')
        entries = raw.setdefault('sources', {})
        for kind, item in updates.items():
            if kind not in HARNESS or kind in {'codex', 'claude'} or not isinstance(item, dict):
                raise ContractError('harness_sources_invalid')
            current = dict(entries.get(kind, {}))
            if 'enabled' in item:
                if type(item['enabled']) is not bool:
                    raise ContractError('harness_sources_invalid')
                current['enabled'] = item['enabled']
            if 'roots' in item:
                roots = item['roots']
                if roots is None:
                    current.pop('roots', None)
                else:
                    if not isinstance(roots, list) or not all(isinstance(p, str) and p.strip() and Path(p).expanduser().is_absolute() for p in roots):
                        raise ContractError('harness_absolute_roots_required')
                    current['roots'] = list(dict.fromkeys(str(Path(p).expanduser().resolve()) for p in roots))
            entries[kind] = current
        raw.update(revision=raw.get('revision', 0) + 1, last_request_id=request.get('request_id'),last_request_fingerprint=fingerprint)
        path.parent.mkdir(parents=True, exist_ok=True)
        pending = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            pending.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding='utf-8')
            if (path.read_bytes() if path.exists() else None) != original:
                raise ContractError('harness_settings_revision_conflict')
            if original:
                backup = path.parent/'harness-monitor-backups'
                backup.mkdir(exist_ok=True)
                (backup/(uuid.uuid4().hex+'.json')).write_bytes(original)
            os.replace(pending, path)
        finally:
            pending.unlink(missing_ok=True)
        external._snapshot = None
        return read(external)
