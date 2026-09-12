"""One set of per-user paths for clients, services and portable installers."""
import os
from pathlib import Path
import sys


def config_dir():
    home = Path.home()
    if sys.platform == 'win32':
        return Path(os.environ.get('LOCALAPPDATA') or home / 'AppData/Local') / 'AP-Vibe'
    modern = (home / 'Library/Application Support/AP-Vibe' if sys.platform == 'darwin'
              else Path(os.environ.get('XDG_CONFIG_HOME') or home / '.config') / 'ap-vibe')
    # A previously installed POSIX client may have used the old Windows-like
    # default. Keep that concrete installation unless the modern one exists.
    legacy = home / 'AppData/Local/AP-Vibe'
    if sys.platform != 'darwin' and os.environ.get('XDG_CONFIG_HOME'):
        return modern
    return legacy if (legacy / 'config.json').is_file() and not (modern / 'config.json').is_file() else modern


def data_dir():
    if sys.platform in {'win32', 'darwin'}:
        return config_dir() / 'data'
    return Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local/share') / 'ap-vibe'


def private_dir():
    configured = os.environ.get('AP_VIBE_CONFIG_PATH')
    return Path(configured).expanduser().resolve().parent if configured else config_dir()
