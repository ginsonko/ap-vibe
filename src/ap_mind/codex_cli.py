"""Discover installed Codex capabilities instead of assuming PATH matches Desktop."""
from functools import lru_cache
import os
from pathlib import Path
import shutil
import subprocess

from .contracts import ContractError


def codex_command():
    explicit = os.environ.get('AP_VIBE_CODEX_EXECUTABLE')
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ContractError('configured_codex_executable_not_found')
        return [str(path)]
    if os.name == 'nt':
        desktop = Path(os.environ.get('LOCALAPPDATA', str(Path.home()/'AppData/Local'))) / 'OpenAI/Codex/bin'
        candidates = sorted(desktop.glob('*/codex.exe'), key=lambda p:p.stat().st_mtime, reverse=True)
        for path in candidates:
            if supports_queue((str(path),)):
                return [str(path)]
    native = shutil.which('codex.exe') if os.name == 'nt' else shutil.which('codex')
    if native:
        return [native]
    wrapper = shutil.which('codex.cmd')
    script = Path(wrapper).parent/'node_modules/@openai/codex/bin/codex.js' if wrapper else None
    node = shutil.which('node.exe') or shutil.which('node')
    if script and script.is_file() and node:
        return [node, str(script)]
    raise ContractError('organization_codex_cli_missing')


@lru_cache(maxsize=16)
def supports_queue(command):
    try:
        result = subprocess.run([*command, 'queue', '--help'], capture_output=True, timeout=4,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        return result.returncode == 0 and b'--thread' in result.stdout and b'--message' in result.stdout
    except (OSError, subprocess.SubprocessError):
        return False
