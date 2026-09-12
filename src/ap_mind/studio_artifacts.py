"""Bounded, read-only previews of a managed run's actual output files."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re

from .contracts import ContractError, utc_now

MAX_PREVIEW_BYTES = 128 * 1024
MAX_ENTRIES = 1000
MAX_FILES = 200
_PRIVATE_DIRS = {'.git', '.claude', '.codex', '.ssh', 'node_modules', '__pycache__', '.venv'}
_PRIVATE_FILES = {'auth.json', 'credentials.json', 'credentials', 'id_rsa', 'id_ed25519'}


def public_path(path):
    return not any(part.lower() in _PRIVATE_DIRS or part.lower() in _PRIVATE_FILES or
                   part.lower() == '.env' or part.lower().startswith('.env.') or
                   Path(part).suffix.lower() in {'.pem', '.key', '.p12', '.pfx'}
                   for part in path.parts)


def preview_text(text):
    # Preview formatting never executes an artifact, including HTML or scripts.
    text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|$)',
                  '[私钥内容已隐藏]', text)
    text = re.sub(r'\bsk-[A-Za-z0-9_-]{10,}', '[凭据已隐藏]', text)
    def hide_value(match):
        value = match.group(0)[len(match.group(1)):]
        # An explicit empty string contains no credential. Replacing it with
        # a nonempty placeholder changes the meaning of installation examples.
        if value in ("''", '""'):
            return match.group(0)
        return match.group(1) + '"[凭据已隐藏]"'
    return re.sub(r'''(?im)(["']?(?:api[_ -]?key|authorization|password|secret|access[_ -]?token)["']?\s*[:=]\s*)(?:["'][^"'\r\n]*["']|[^\r\n,;]+)''',
                  hide_value, text)


class StudioArtifacts:
    def __init__(self, studio):
        self.studio = studio

    def _workspace(self, run_id):
        if not isinstance(run_id, str) or not run_id:
            raise ContractError('agent_artifact_run_required')
        with closing(self.studio.registry._connect()) as c:
            row = c.execute('SELECT payload_json FROM studio_runs WHERE run_id=?', (run_id,)).fetchone()
        if row is None:
            raise ContractError('agent_run_not_found')
        return Path(json.loads(row[0])['workspace']).resolve()

    def _resolve(self, root, name):
        if not isinstance(name, str) or not name or len(name) > 2048 or '\x00' in name:
            raise ContractError('agent_artifact_path_invalid')
        relative = PurePosixPath(name.replace('\\', '/'))
        if relative.is_absolute() or PureWindowsPath(name).drive or '..' in relative.parts or ':' in name:
            raise ContractError('agent_artifact_path_invalid')
        if not public_path(relative):
            raise ContractError('agent_artifact_private_file')
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or path == root:
            raise ContractError('agent_artifact_path_invalid')
        if not public_path(path.relative_to(root)):
            raise ContractError('agent_artifact_private_file')
        return path, relative.as_posix()

    def list(self, run_id):
        root = self._workspace(run_id)
        result = {'ok': True, 'run_id': run_id, 'files': [], 'truncated': False,
                  'observed_at': utc_now(), 'workspace_exists': root.is_dir()}
        if not result['workspace_exists']:
            return result
        pending, visited, inspected = [root], set(), 0
        while pending:
            directory = pending.pop()
            resolved = directory.resolve()
            if resolved in visited or not resolved.is_relative_to(root):
                continue
            visited.add(resolved)
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        inspected += 1
                        if inspected > MAX_ENTRIES or len(result['files']) >= MAX_FILES:
                            result['truncated'] = True
                            return self._ordered(result)
                        relative = Path(entry.path).relative_to(root)
                        if not public_path(relative) or entry.is_symlink():
                            continue
                        try:
                            path, name = self._resolve(root, relative.as_posix())
                            if entry.is_dir(follow_symlinks=False):
                                pending.append(path)
                            elif entry.is_file(follow_symlinks=False):
                                stat = path.stat()
                                result['files'].append({'name': name, 'size': stat.st_size, 'modified_at': stat.st_mtime})
                        except (OSError, ContractError):
                            result['truncated'] = True
            except OSError:
                result['truncated'] = True
        return self._ordered(result)

    @staticmethod
    def _ordered(result):
        result['files'].sort(key=lambda f: (Path(f['name']).name.lower() != 'acceptance.md', f['name'].lower()))
        return result

    def read(self, run_id, name):
        root = self._workspace(run_id)
        path, relative = self._resolve(root, name)
        try:
            if not path.is_file():
                raise ContractError('agent_artifact_not_found')
            with path.open('rb') as source:
                before = os.fstat(source.fileno())
                blob = source.read(MAX_PREVIEW_BYTES + 1)
                after = os.fstat(source.fileno())
        except OSError:
            raise ContractError('agent_artifact_unreadable') from None
        truncated = len(blob) > MAX_PREVIEW_BYTES
        stable = (before.st_size, before.st_mtime_ns) == (after.st_size, after.st_mtime_ns)
        full = not truncated and stable and len(blob) == after.st_size
        result = {'ok': True, 'run_id': run_id, 'name': relative, 'local_path': str(path), 'size': after.st_size,
                  'observed_at': utc_now(), 'truncated': truncated, 'changed_during_read': not stable,
                  'sha256': hashlib.sha256(blob).hexdigest() if full else None,
                  'hash_scope': 'complete_file' if full else 'unavailable'}
        body = blob[:MAX_PREVIEW_BYTES]
        try:
            # A UTF-8 character may straddle a preview boundary.
            import codecs
            text = codecs.getincrementaldecoder('utf-8-sig')().decode(body, final=not truncated)
            if '\x00' in text:
                raise UnicodeError('binary')
        except UnicodeError:
            return {**result, 'kind': 'binary', 'text': None, 'redacted': False}
        rendered = preview_text(text)
        return {**result, 'kind': 'markdown' if path.suffix.lower() in {'.md', '.markdown'} else 'text',
                'text': rendered, 'redacted': rendered != text}
