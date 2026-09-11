"""Local harness identities and capabilities, independent of model families.

Add an adapter here only after checking its public storage/CLI contract. Session
reading does not imply an application can be remotely resumed. User paths are
configured in harness-monitor.json; missing optional clients are ordinary state.
"""
import os
from pathlib import Path
import re
import shutil


HARNESS = {
    'codex': {'name': 'Codex', 'storage': 'codex', 'executor': 'codex', 'resume': True},
    'claude': {'name': 'Claude Code', 'storage': 'claude', 'executor': 'claude', 'resume': True},
    'opencode': {'name': 'OpenCode', 'storage': 'opencode', 'executor': 'opencode', 'resume': True},
    'openclaw': {'name': 'OpenClaw', 'storage': 'pi', 'executor': 'openclaw', 'resume': True},
    'pi': {'name': 'PI CLI', 'storage': 'pi', 'executor': None, 'resume': False},
    'pi-desktop': {'name': 'PI Desktop', 'storage': 'pi-desktop', 'executor': None, 'resume': False},
    'zcode': {'name': 'ZCode', 'storage': 'opencode', 'executor': None, 'resume': False},
    'mimocode': {'name': 'MiMo Code', 'storage': 'opencode', 'executor': 'mimocode', 'resume': True},
    'ga-admin': {'name': 'GenericAgent Admin', 'storage': 'ga-json', 'executor': None, 'resume': False},
    'hermes': {'name': 'Hermes Desktop / CLI', 'storage': 'hermes', 'executor': 'hermes', 'resume': True},
    'dsh': {'name': 'DSH Desktop', 'storage': 'dsh', 'executor': None, 'resume': False},
}


def valid_kind(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', value))


def default_roots():
    home = Path.home()
    return {
        'opencode': [str(Path(os.environ.get('XDG_DATA_HOME') or home / '.local/share') / 'opencode')],
        'openclaw': [str(Path(os.environ.get('OPENCLAW_STATE_DIR') or home / '.openclaw') / 'agents')],
        'pi': [str(Path(os.environ.get('PI_CODING_AGENT_DIR') or home / '.pi/agent') / 'sessions')],
        'pi-desktop': [str(Path(os.environ.get('PI_DESKTOP_DATA_DIR') or home / '.pi-desktop') / 'sessions')],
        'zcode': [str(home / '.zcode/cli/db')],
        'mimocode': [str(Path(os.environ['MIMOCODE_HOME']) / 'data') if os.environ.get('MIMOCODE_HOME') else
                     str(Path(os.environ.get('XDG_DATA_HOME') or home / '.local/share') / 'mimocode')],
        # Portable Admin stores sessions beside its executable; custom portable
        # locations are configured as roots without scanning the user's disk.
        'ga-admin': [str(Path(os.environ['GA_ADMIN_CHAT_DATA_DIR']) / 'chat_sessions')] if os.environ.get('GA_ADMIN_CHAT_DATA_DIR') else [],
        'hermes': [str(Path(os.environ.get('HERMES_HOME') or
                    (Path(os.environ.get('LOCALAPPDATA') or home/'AppData/Local')/'hermes' if os.name=='nt' else home/'.hermes')))],
        'dsh': [str(Path(os.environ.get('DSH_HOME') or home/'.dsh')/'sessions')],
    }


def catalog():
    return [{'harness': key, 'name': value['name'], 'session_discovery': True,
             'configured_executor': value['executor'], 'resume_command': value['resume'],
             'ordinary_terminal_wake': key == 'codex'} for key, value in HARNESS.items()]


def executable(kind):
    """Return argv, never a shell string; npm shims resolve to their JS entry."""
    override = os.environ.get('AP_VIBE_' + kind.upper().replace('-', '_') + '_EXE')
    if override and Path(override).is_file() and Path(override).suffix.lower() not in {'.cmd', '.bat', '.ps1'}:
        return [str(Path(override).resolve())]
    if kind=='hermes':
        home=Path(os.environ.get('LOCALAPPDATA') or Path.home()/'.local/share')/'hermes'
        python=home/'hermes-agent/venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        if python.is_file():return [str(python),'-X','utf8','-m','hermes_cli.main']
    binary = 'mimo' if kind == 'mimocode' else kind
    found = shutil.which(binary + ('.exe' if os.name == 'nt' else ''))
    if found:
        return [found]
    local = Path(os.environ.get('LOCALAPPDATA') or Path.home() / '.local/share') / 'AP-Vibe/runtimes' / kind / (binary + ('.exe' if os.name == 'nt' else ''))
    if local.is_file():
        return [str(local)]
    if kind == 'openclaw':
        npm = Path(os.environ.get('APPDATA') or Path.home()) / 'npm/node_modules/openclaw/openclaw.mjs'
        node = shutil.which('node')
        if node and npm.is_file():
            return [node, str(npm)]
    return None
