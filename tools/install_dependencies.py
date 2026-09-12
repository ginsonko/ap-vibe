"""Repair optional native-session readers in the configured Python runtime.

This installs a library, never third-party agent applications. An offline
installation leaves the core workbench usable and reports a repair command.
"""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys


def ensure(*, check=False):
    requirements={'zstandard':'zstandard>=0.22,<1','yaml':'PyYAML>=6,<7'}
    if os.name != 'nt':
        requirements['cryptography'] = 'cryptography>=43,<47'
    missing=[module for module in requirements if importlib.util.find_spec(module) is None]
    if not missing:
        return {'ok': True, 'status': 'available', 'capabilities': ['dsh_compressed_sessions','hermes_config_merge']}
    command = [sys.executable, '-m', 'pip', '--disable-pip-version-check',
               'install', '--no-input', '--timeout', '20', '--retries', '1', *[requirements[m] for m in missing]]
    if not check:
        try:
            result = subprocess.run(command, capture_output=True, timeout=120)
            if result.returncode == 0 and all(importlib.util.find_spec(m) is not None for m in missing):
                return {'ok': True, 'status': 'installed', 'modules': missing}
        except (OSError, subprocess.TimeoutExpired):
            pass
    # Do not echo pip output: local index URLs may carry credentials.
    return {'ok': False, 'status': 'optional_dependency_missing',
            'missing_modules': missing, 'core_available': True,
            'message': '部分可选接入依赖尚未安装；已有会话与工作台仍可使用。联网后可重跑 AP-Vibe 安装器。',
            'repair_command': command}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--config', type=Path)  # Common installer interface; no secrets are read.
    args = parser.parse_args()
    outcome = ensure(check=args.check)
    print(json.dumps(outcome, ensure_ascii=False))
    raise SystemExit(0 if outcome['ok'] else 1)
