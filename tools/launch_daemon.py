"""Detach an AP-Vibe daemon without inheriting the caller's terminal pipes."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import os
import subprocess


def launch(executable, arguments, cwd, stdout_path, stderr_path):
    flags = 0
    if os.name == 'nt':
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    with Path(stdout_path).open('ab', buffering=0) as out, Path(stderr_path).open('ab', buffering=0) as err:
        options = dict(cwd=cwd, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                       close_fds=True, start_new_session=os.name != 'nt')
        breakaway = os.name == 'nt'
        try:
            process = subprocess.Popen([executable, *arguments], **options,
                creationflags=flags | (getattr(subprocess,'CREATE_BREAKAWAY_FROM_JOB',0) if breakaway else 0))
        except PermissionError as exc:
            if not breakaway or getattr(exc,'winerror',None) != 5:
                raise
            breakaway = False
            process = subprocess.Popen([executable, *arguments], **options, creationflags=flags)
    return process.pid, breakaway


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--executable', required=True)
    parser.add_argument('--cwd', required=True)
    parser.add_argument('--stdout', required=True)
    parser.add_argument('--stderr', required=True)
    parser.add_argument('arguments', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    arguments = args.arguments[1:] if args.arguments[:1] == ['--'] else args.arguments
    pid, breakaway = launch(args.executable, arguments, args.cwd, args.stdout, args.stderr)
    print(json.dumps({'pid':pid, 'launch_mode':'breakaway' if breakaway else 'detached'}))
