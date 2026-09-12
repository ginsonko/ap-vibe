"""Shareable partner profiles plus immutable, validated appearance atlases.

Export reads public_json, never the secret column, and never decrypts keys.
Import creates new agent IDs with empty keys and does not start models.
"""
from __future__ import annotations

from contextlib import closing
from pathlib import Path
import base64
import hashlib
import json
import os
import re
from urllib.parse import urlsplit

from ap_mind.contracts import ContractError, utc_now
from ap_mind.studio_appearances import clean_png, normalize
from ap_mind.studio_budget import policy as budget_policy
from ap_mind.studio_routing import normalize_routing


SCHEMA = 'ap-vibe.agents.v1'
MAX_BUNDLE_BYTES = 32 * 1024 * 1024
HIDDEN = '[凭据已隐藏]'
PROFILE_KEYS = (
    'name', 'base_url', 'model', 'protocol', 'auth_mode', 'role', 'persona',
    'avatar', 'appearance_id', 'connection_label', 'capability_notes',
    'max_request_retries', 'request_timeout_seconds', 'upstream_mode',
    'routing_profile', 'executor_kind',
)
BUDGET_KEYS = ('token_limit', 'amount_limit', 'prices', 'price_basis', 'price_source', 'currency')
DROP_KEYS = {
    'api_key', 'secret', 'secrets', 'password', 'token', 'access_token',
    'refresh_token', 'authorization', 'private_key', 'client_secret', 'cookie', 'cookies',
}
SK_RE = re.compile(r'\bsk-[A-Za-z0-9_-]{16,}')
AUTH_RE = re.compile(r'(?i)(authorization\s*[:=]\s*)(?:bearer\s+)?\S+')
BEARER_RE = re.compile(r'(?i)\bbearer\s+[A-Za-z0-9._\-+=/]{8,}')
MANIFEST_KEYS = ('schema_version', 'frame_width', 'frame_height', 'anchor', 'animations')


def encoded(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (ValueError, TypeError, RecursionError) as exc:
        raise ContractError('agent_share_bundle_invalid') from exc


def default_characters_root():
    env = os.environ.get('AP_VIBE_CHARACTERS_ROOT')
    if env:
        return Path(env)
    import ap_mind
    here = Path(ap_mind.__file__).resolve()
    for base in (here.parents[2], here.parents[1], here.parent):
        candidate = base / 'assets' / 'characters'
        if candidate.is_dir():
            return candidate
    return here.parents[2] / 'assets' / 'characters'


def _text_id(value, name, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or '\x00' in value:
        raise ContractError('agent_share_' + name + '_invalid')
    return value.strip()


def _size_of(value):
    return len(encoded(value).encode('utf-8'))


def _redact_text(text):
    text = AUTH_RE.sub(r'\1' + HIDDEN, text)
    text = BEARER_RE.sub('Bearer ' + HIDDEN, text)
    return SK_RE.sub(HIDDEN, text)


def sanitize(value, path, warnings):
    if isinstance(value, dict):
        out = {}
        for key, child in value.items():
            name = key if isinstance(key, str) else str(key)
            loc = f'{path}.{name}' if path else name
            lowered = name.lower()
            drop = lowered in DROP_KEYS or lowered.endswith('_secret') or (
                lowered.endswith('_key') and lowered != 'appearance_id'
            )
            if drop:
                warnings.append('removed ' + loc)
                continue
            out[key] = sanitize(child, loc, warnings)
        return out
    if isinstance(value, list):
        return [sanitize(child, f'{path}[{index}]', warnings) for index, child in enumerate(value)]
    if isinstance(value, str):
        redacted = _redact_text(value)
        if redacted != value:
            warnings.append('redacted ' + path)
        return redacted
    return value


def _contained_file(root, relative, appearance_id):
    if not isinstance(relative, str) or not relative.strip() or '\x00' in relative:
        raise ContractError('agent_share_appearance_path_invalid:' + appearance_id)
    if '\\' in relative or relative.startswith('/') or ':' in relative:
        raise ContractError('agent_share_appearance_path_invalid:' + appearance_id)
    parts = Path(relative).parts
    if not parts or any(part in {'.', '..'} for part in parts) or Path(relative).is_absolute():
        raise ContractError('agent_share_appearance_path_invalid:' + appearance_id)
    root = root.resolve()
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ContractError('agent_share_appearance_path_invalid:' + appearance_id) from exc
    if not candidate.is_file():
        raise ContractError('agent_share_appearance_missing:' + appearance_id)
    return candidate


def _manifest_slice(raw):
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ContractError('appearance_manifest_invalid')
    return {key: raw[key] for key in MANIFEST_KEYS if key in raw}


def _budget_slice(raw):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ContractError('agent_share_budget_invalid')
    cleaned = {key: raw[key] for key in BUDGET_KEYS if key in raw}
    if cleaned.get('prices') is not None and not isinstance(cleaned['prices'], dict):
        raise ContractError('agent_share_budget_invalid')
    cleaned['prices'] = dict(cleaned.get('prices') or {})
    return budget_policy(cleaned)


class StudioAgentSharing:
    def __init__(self, studio, characters_root=None):
        self.studio = studio
        self.characters_root = Path(characters_root) if characters_root is not None else default_characters_root()
        self._builtin = None

    def export(self, raw):
        agent_ids = self._agent_ids(raw)
        warnings = []
        agents, appearance_map = [], {}
        with closing(self.studio.registry._connect()) as connection:
            for agent_id in agent_ids:
                public = self._public(connection, agent_id)
                profile, extra = self._profile_share(public)
                warnings.extend(extra)
                budget, extra = self._budget_export(connection, agent_id)
                warnings.extend(extra)
                appearance_id = profile.get('appearance_id') or ''
                if appearance_id:
                    self._export_appearance(connection, agent_id, appearance_id, appearance_map)
                agents.append({'source_id': agent_id, 'profile': profile, 'budget': budget})
        bundle = {'schema': SCHEMA, 'agents': agents, 'appearances': list(appearance_map.values())}
        self._enforce_size(bundle)
        return {'ok': True, 'bundle': bundle, 'warnings': self._unique(warnings)}

    def preview(self, raw):
        bundle, warnings = self._validated_bundle((raw or {}).get('bundle'))
        existing = self._existing_fingerprints()
        agents = []
        for item in bundle['agents']:
            profile = item['profile']
            digest = self._fingerprint(item, bundle)
            agents.append({
                'source_id': item['source_id'],
                'name': profile.get('name', ''),
                'model': profile.get('model', ''),
                'base_url': profile.get('base_url', ''),
                'executor_kind': profile.get('executor_kind', ''),
                'appearance_id': profile.get('appearance_id', ''),
                'already_imported': digest in existing,
            })
        appearances = [{**normalize(self._appearance_input(a))[0], 'source_id': a['source_id']} for a in bundle['appearances']]
        return {'ok': True, 'agents': agents, 'appearances': appearances, 'warnings': warnings}

    def import_bundle(self, raw):
        raw = raw or {}
        request_id = _text_id(raw.get('request_id'), 'request_id')
        bundle, warnings = self._validated_bundle(raw.get('bundle'))
        selected = raw.get('selected_ids')
        if selected is None:
            selected_ids = [item['source_id'] for item in bundle['agents']]
        else:
            if not isinstance(selected, list) or any(not isinstance(item, str) for item in selected):
                raise ContractError('agent_share_selected_ids_invalid')
            known = {item['source_id'] for item in bundle['agents']}
            selected_ids = list(dict.fromkeys(selected))
            missing = [item for item in selected_ids if item not in known]
            if missing:
                raise ContractError('agent_share_selected_id_unknown:' + missing[0])
        chosen = [item for item in bundle['agents'] if item['source_id'] in selected_ids]
        needed = {item['profile'].get('appearance_id') for item in chosen if item['profile'].get('appearance_id')}
        appearances = {item['source_id']: item for item in bundle['appearances'] if item['source_id'] in needed}
        prepared = []
        for item in chosen:
            appearance_id = item['profile'].get('appearance_id') or ''
            if appearance_id and appearance_id not in appearances:
                raise ContractError('agent_share_appearance_missing:' + appearance_id + ':agent:' + item['source_id'])
            if appearance_id:
                normalize(self._appearance_input(appearances[appearance_id]))
            prepared.append(item)
        with self.studio._lock, self.studio.registry.transaction():
            connection = self.studio.registry._connect()
            request_id, fingerprint, old = self.studio.agent_setup._request(connection, raw, 'share_import')
            if old:
                return old
            existing = self._existing_fingerprints(connection)
            remap, imported, skipped = {}, [], []
            for source_id, appearance in appearances.items():
                remap[source_id] = self._import_appearance(appearance)
            for item in prepared:
                digest = self._fingerprint(item, bundle)
                if digest in existing:
                    skipped.append({
                        'source_id': item['source_id'],
                        'reason': 'duplicate_content',
                        'agent_id': existing[digest],
                    })
                    continue
                dest_appearance = remap.get(item['profile'].get('appearance_id') or '', '')
                saved = self._save_agent(item['profile'], dest_appearance)
                self._save_budget(request_id, item['source_id'], saved['agent_id'], item['budget'])
                public = self._write_provenance(connection, saved, item, digest, dest_appearance, request_id)
                existing[digest] = public['agent_id']
                imported.append({
                    'source_id': item['source_id'],
                    'agent_id': public['agent_id'],
                    'appearance_id': public.get('appearance_id', ''),
                    'name': public['name'],
                })
            return self.studio.agent_setup._receipt(connection, request_id, fingerprint, {
                'agents': imported, 'skipped': skipped, 'warnings': warnings,
            })

    def _agent_ids(self, raw):
        ids = (raw or {}).get('agent_ids')
        if not isinstance(ids, list) or not ids or any(not isinstance(item, str) for item in ids):
            raise ContractError('agent_share_agent_ids_invalid')
        return list(dict.fromkeys(_text_id(item, 'agent_id', 80) for item in ids))

    def _public(self, connection, agent_id):
        row = connection.execute(
            'SELECT public_json FROM studio_agents WHERE agent_id=?', (agent_id,)
        ).fetchone()
        if row is None:
            raise ContractError('agent_share_agent_not_found:' + agent_id)
        payload = row['public_json'] if hasattr(row, 'keys') else row[0]
        return json.loads(payload)

    def _profile_share(self, public):
        if not isinstance(public, dict):
            raise ContractError('agent_share_profile_invalid')
        warnings = []
        raw = {key: public[key] for key in PROFILE_KEYS if key in public}
        cleaned = sanitize(raw, 'profile', warnings)
        defaults = {'executor_kind':'claude','auth_mode':'api_key','model':'','base_url':'','protocol':'anthropic',
                    'role':'','persona':'','avatar':'fish','appearance_id':'','connection_label':'',
                    'capability_notes':'','max_request_retries':5,'request_timeout_seconds':300,'upstream_mode':'stream'}
        cleaned = {**defaults, **cleaned}
        from .studio_routing import default_routing
        cleaned['routing_profile'] = normalize_routing(cleaned.get('routing_profile', default_routing(cleaned)))
        if cleaned.get('auth_mode') == 'local_login':
            cleaned['base_url'] = ''
            cleaned['protocol'] = 'responses'
        for key, limit in [('name',100),('model',200),('base_url',2048),('role',2000),('persona',4000),
                           ('connection_label',120),('capability_notes',2000),('appearance_id',120)]:
            value = cleaned.get(key)
            if not isinstance(value,str) or len(value)>limit or '\x00' in value or (key=='name' and not value.strip()):
                raise ContractError('agent_share_profile_invalid')
        for key in ('name','model','base_url'):cleaned[key]=cleaned[key].strip()
        cleaned['base_url']=cleaned['base_url'].rstrip('/')
        if cleaned['base_url']:
            try:
                url = urlsplit(cleaned['base_url'])
                valid = url.scheme in ('http','https') and url.hostname and not (url.username or url.password or url.query or url.fragment)
                if url.scheme=='http' and url.hostname not in ('localhost','127.0.0.1','::1'): valid=False
            except ValueError:valid=False
            if not valid:raise ContractError('agent_url_invalid')
        return cleaned, warnings

    def _budget_export(self, connection, agent_id):
        warnings = []
        settings = self.studio.budget._policy(connection, agent_id)
        sliced = _budget_slice(settings)
        cleaned = sanitize(sliced, 'budget', warnings)
        return cleaned, warnings

    def _export_appearance(self, connection, agent_id, appearance_id, collected):
        if appearance_id in collected:
            return
        builtin = self.builtin_catalog().get(appearance_id)
        if builtin is not None:
            png = _contained_file(builtin['folder'], builtin['atlas'], appearance_id).read_bytes()
            encoded_png, _, _ = clean_png(base64.b64encode(png).decode('ascii'))
            collected[appearance_id] = {
                'source_id': appearance_id,
                'display_name': builtin['display_name'],
                'png_base64': base64.b64encode(encoded_png).decode('ascii'),
                'manifest': builtin['manifest'],
                'attribution': builtin['attribution'],
            }
            return
        row = connection.execute(
            'SELECT manifest_json, png FROM studio_appearances WHERE appearance_id=?',
            (appearance_id,),
        ).fetchone()
        if row is None:
            raise ContractError('agent_share_appearance_missing:' + appearance_id + ':agent:' + agent_id)
        manifest = json.loads(row['manifest_json'])
        encoded_png, _, _ = clean_png(base64.b64encode(bytes(row['png'])).decode('ascii'))
        collected[appearance_id] = {
            'source_id': appearance_id,
            'display_name': manifest.get('display_name', appearance_id),
            'png_base64': base64.b64encode(encoded_png).decode('ascii'),
            'manifest': _manifest_slice(manifest),
            'attribution': manifest.get('attribution', ''),
        }

    def _validated_bundle(self, bundle):
        if not isinstance(bundle, dict):
            raise ContractError('agent_share_bundle_invalid')
        self._enforce_size(bundle)
        if bundle.get('schema') != SCHEMA:
            raise ContractError('agent_share_schema_invalid')
        agents, appearances = bundle.get('agents'), bundle.get('appearances')
        if not isinstance(agents, list) or not agents or len(agents) > 100 or not isinstance(appearances, list) or len(appearances) > 100:
            raise ContractError('agent_share_bundle_invalid')
        warnings, cleaned_agents, seen = [], [], set()
        for item in agents:
            if not isinstance(item, dict):
                raise ContractError('agent_share_bundle_invalid')
            source_id = _text_id(item.get('source_id'), 'source_id', 80)
            if source_id in seen:
                raise ContractError('agent_share_source_id_duplicated')
            seen.add(source_id)
            profile, extra = self._profile_share(item.get('profile') or {})
            warnings.extend(extra)
            if isinstance(item.get('profile'), dict) and any(
                key in item['profile'] for key in ('api_key', 'secret', 'agent_id')
            ):
                warnings.append('ignored untrusted credential or identity on ' + source_id)
            budget_warnings = []
            budget = sanitize(_budget_slice(item.get('budget')), 'budget', budget_warnings)
            warnings.extend(budget_warnings)
            if 'name' not in profile:
                raise ContractError('agent_share_profile_invalid')
            cleaned_agents.append({'source_id': source_id, 'profile': profile, 'budget': budget})
        cleaned_appearances, seen_app = [], set()
        for item in appearances:
            if not isinstance(item, dict):
                raise ContractError('agent_share_bundle_invalid')
            source_id = _text_id(item.get('source_id'), 'appearance_id', 120)
            if source_id in seen_app:
                raise ContractError('agent_share_appearance_duplicated')
            seen_app.add(source_id)
            png = item.get('png_base64')
            if not isinstance(png, str) or png.startswith('http:') or png.startswith('https:') or png.startswith('file:'):
                raise ContractError('agent_share_appearance_image_invalid:' + source_id)
            name = item.get('display_name')
            attribution = item.get('attribution', '')
            if not isinstance(attribution, str):
                raise ContractError('appearance_manifest_invalid')
            cleaned_appearances.append({
                'source_id': source_id,
                'display_name': name,
                'png_base64': png,
                'manifest': _manifest_slice(item.get('manifest')),
                'attribution': attribution,
            })
            normalized, png_bytes = normalize(self._appearance_input(cleaned_appearances[-1]))
            cleaned_appearances[-1].update(
                manifest=_manifest_slice(normalized),
                png_base64=base64.b64encode(png_bytes).decode('ascii'))
        available = {item['source_id'] for item in cleaned_appearances}
        for item in cleaned_agents:
            appearance_id = item['profile'].get('appearance_id')
            if appearance_id and appearance_id not in available:
                raise ContractError('agent_share_appearance_missing')
        cleaned = {'schema': SCHEMA, 'agents': cleaned_agents, 'appearances': cleaned_appearances}
        return cleaned, self._unique(warnings)

    def _appearance_input(self, item):
        return {
            'display_name': item.get('display_name'),
            'png_base64': item.get('png_base64'),
            'manifest': item.get('manifest') or {},
            'attribution': item.get('attribution', ''),
        }

    def _import_appearance(self, item):
        source_id = item['source_id']
        if source_id in self.builtin_catalog() and self._appearance_marker(source_id, item) == self._appearance_marker(source_id, None):
            return source_id
        saved = self.studio.appearances.save(self._appearance_input(item))['appearances'][0]
        return saved['appearance_id']

    def _save_agent(self, profile, appearance_id):
        payload = {
            'name': profile['name'],
            'executor_kind': profile.get('executor_kind', 'claude'),
            'auth_mode': profile.get('auth_mode', 'api_key'),
            'model': profile.get('model', ''),
            'base_url': profile.get('base_url', ''),
            'protocol': profile.get('protocol', 'anthropic'),
            'role': profile.get('role', ''),
            'persona': profile.get('persona', ''),
            'avatar': profile.get('avatar', 'fish'),
            'appearance_id': appearance_id,
            'connection_label': profile.get('connection_label', ''),
            'capability_notes': profile.get('capability_notes', ''),
            'max_request_retries': profile.get('max_request_retries', 5),
            'request_timeout_seconds': profile.get('request_timeout_seconds', 300),
            'upstream_mode': profile.get('upstream_mode', 'stream'),
            # Imported profiles deliberately start without credentials.  The
            # placeholder must never be persisted as a usable key.
            'api_key': '',
            'expected_revision': 0,
        }
        if 'routing_profile' in profile:
            payload['routing_profile'] = profile['routing_profile']
        return self.studio.save(payload)['agent']

    def _save_budget(self, request_id, source_id, agent_id, budget):
        payload = {
            'request_id': request_id + ':budget:' + source_id,
            'agent_id': agent_id,
            'expected_revision': 0,
            **budget,
        }
        self.studio.budget.save(payload)

    def _write_provenance(self, connection, saved, item, digest, appearance_id, request_id):
        public = dict(saved)
        public['import_provenance'] = {
            'request_id': request_id,
            'source_id': item['source_id'],
            'content_fingerprint': digest,
            'appearance_source_id': item['profile'].get('appearance_id') or '',
            'appearance_id': appearance_id,
            'imported_at': utc_now(),
        }
        connection.execute(
            'UPDATE studio_agents SET public_json=? WHERE agent_id=?',
            (encoded(public), public['agent_id']),
        )
        return public

    def _fingerprint(self, item, bundle):
        appearance_id = item['profile'].get('appearance_id') or ''
        appearance = next((entry for entry in bundle['appearances'] if entry['source_id'] == appearance_id), None)
        profile = {key: item['profile'].get(key) for key in PROFILE_KEYS if key != 'appearance_id'}
        marker = self._appearance_marker(appearance_id, appearance)
        return hashlib.sha256(encoded({
            'profile': profile,
            'budget': item['budget'],
            'appearance': marker,
        }).encode('utf-8')).hexdigest()

    def _appearance_marker(self, appearance_id, appearance):
        if not appearance_id:
            return None
        if appearance is None and appearance_id in self.builtin_catalog():
            builtin = self.builtin_catalog()[appearance_id]
            appearance = {**builtin, 'png_base64': base64.b64encode(
                _contained_file(builtin['folder'], builtin['atlas'], appearance_id).read_bytes()).decode('ascii')}
        if appearance is None:
            return {'kind': 'id', 'id': appearance_id}
        manifest, png = normalize(self._appearance_input(appearance))
        return {'content': manifest['appearance_id']}

    def _existing_fingerprints(self, connection=None):
        if connection is None:
            with closing(self.studio.registry._connect()) as handle:
                return self._existing_fingerprints(handle)
        mapping = {}
        rows = connection.execute('SELECT public_json FROM studio_agents').fetchall()
        catalog = self.builtin_catalog()
        for row in rows:
            public = json.loads(row['public_json'])
            if public.get('archived'):
                continue
            profile, _ = self._profile_share(public)
            budget, _ = self._budget_export(connection, public['agent_id'])
            appearance_id = profile.get('appearance_id') or ''
            appearance = None
            if appearance_id and appearance_id not in catalog:
                stored = connection.execute(
                    'SELECT manifest_json, png FROM studio_appearances WHERE appearance_id=?',
                    (appearance_id,),
                ).fetchone()
                if stored:
                    appearance = {
                        'display_name': json.loads(stored['manifest_json']).get('display_name'),
                        'attribution': json.loads(stored['manifest_json']).get('attribution', ''),
                        'source_id': appearance_id,
                        'png_base64': base64.b64encode(bytes(stored['png'])).decode('ascii'),
                        'manifest': json.loads(stored['manifest_json']),
                    }
            digest = hashlib.sha256(encoded({
                'profile': {key: profile.get(key) for key in PROFILE_KEYS if key != 'appearance_id'},
                'budget': budget,
                'appearance': self._appearance_marker(appearance_id, appearance),
            }).encode('utf-8')).hexdigest()
            mapping.setdefault(digest, public['agent_id'])
        return mapping

    def builtin_catalog(self):
        if self._builtin is not None:
            return self._builtin
        catalog = {}
        root = self.characters_root
        if root.is_dir():
            for manifest_path in sorted(root.glob('*/manifest.json')):
                folder = manifest_path.parent
                try:
                    payload = json.loads(manifest_path.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    continue
                characters = payload.get('characters') if isinstance(payload, dict) else None
                if not isinstance(characters, list):
                    continue
                for character in characters:
                    if not isinstance(character, dict):
                        continue
                    appearance_id = character.get('appearance_id')
                    if not isinstance(appearance_id, str) or not appearance_id or appearance_id in catalog:
                        continue
                    if any(ord(ch) < 32 for ch in appearance_id) or '/' in appearance_id or '\\' in appearance_id:
                        continue
                    catalog[appearance_id] = {
                        'folder': folder,
                        'atlas': character.get('atlas', ''),
                        'display_name': character.get('display_name', appearance_id),
                        'attribution': character.get('attribution') or character.get('credit') or '',
                        'manifest': _manifest_slice(character),
                    }
                    credit = catalog[appearance_id]['attribution']
                    if isinstance(credit, dict):
                        catalog[appearance_id]['attribution'] = encoded(sanitize(credit, 'attribution', []))
        self._builtin = catalog
        return catalog

    def _enforce_size(self, value):
        if _size_of(value) > MAX_BUNDLE_BYTES:
            raise ContractError('agent_share_too_large')

    @staticmethod
    def _unique(items):
        return list(dict.fromkeys(items))
