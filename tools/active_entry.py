"""Resolve old absolute integration paths to the configured active release."""
import json
import os
from pathlib import Path
import runpy
import sys


def forward(current_file):
    current = Path(current_file).resolve()
    config_file = os.environ.get('AP_VIBE_CONFIG_PATH')
    for i, arg in enumerate(sys.argv[1:], 1):
        if arg == '--config' and i + 1 < len(sys.argv):
            config_file = sys.argv[i + 1]
        elif arg.startswith('--config='):
            config_file = arg.split('=', 1)[1]
    if config_file:
        config_path=Path(config_file)
    else:
        # An old absolute entry can forward with only this file and an explicit
        # config. Load installation dependencies only for default discovery.
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
        from tools.installation import default_config_path
        config_path=default_config_path()
    try:
        config = json.loads(config_path.read_text(encoding='utf-8-sig'))
        if config.get('product') != 'AP-Vibe':
            return
        target = (Path(config['product_root'])/'tools'/current.name).resolve()
        if target == current or not target.is_file():
            return
    except (OSError, ValueError, KeyError, TypeError):
        return
    sys.path.insert(0, str(target.parent))
    sys.path.insert(0, str(target.parent.parent))
    sys.argv[0] = str(target)
    runpy.run_path(str(target), run_name='__main__')
    raise SystemExit(0)
