"""Shared local installation identity and safely quoted lifecycle commands."""
from pathlib import Path
import os


def default_config_path() -> Path:
    return Path(os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local'))) / 'AP-Vibe/config.json'


def custom_config_path(config_path: Path | None) -> Path | None:
    if config_path is None:
        return None
    resolved = config_path.expanduser().resolve()
    return None if resolved == default_config_path().resolve() else resolved


def hook_commands(product_root: Path, python: str, config_path: Path | None = None) -> dict:
    values = [python.replace('\\', '/'), (product_root / 'tools/task_client.py').as_posix()]
    custom = custom_config_path(config_path)
    if custom:
        values.append(custom.as_posix())
    if any(char in ''.join(values) for char in ('"', '\n', '\r', '`', '$', '%')):
        raise ValueError('Client or config path cannot be safely represented in a hook command')
    exe, script = values[:2]
    command = f'"{exe}" "{script}" hook'
    windows = "& '" + exe.replace("'", "''") + "' '" + script.replace("'", "''") + "' hook"
    if custom:
        command += ' --config "' + values[2] + '"'
        windows += " --config '" + values[2].replace("'", "''") + "'"
    return {'command': command, 'commandWindows': windows}
