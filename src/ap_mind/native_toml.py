"""Small TOML writer for generated CLI configs (not a user-config rewriter)."""
import json
import math


def atom(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if type(value) in {int, float} and math.isfinite(value):
        return str(value)
    if isinstance(value, list):
        return '[' + ', '.join(atom(v) for v in value) + ']'
    raise ValueError('Unsupported generated TOML value')


def dumps(value, prefix=()):
    lines = ['[' + '.'.join(atom(k) for k in prefix) + ']'] if prefix else []
    for key, item in value.items():
        if not isinstance(item, dict):
            lines.append(atom(key) + ' = ' + atom(item))
    for key, item in value.items():
        if isinstance(item, dict):
            lines.extend(['', dumps(item, (*prefix, key)).rstrip()])
    return '\n'.join(lines) + '\n'
