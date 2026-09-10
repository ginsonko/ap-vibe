"""Optional JSON transfer compression; no changes to the represented data."""
import gzip


def json_transfer(raw: bytes, accept_encoding: str) -> tuple[bytes, bool]:
    if len(raw) < 4096:
        return raw, False
    qualities = {}
    for entry in accept_encoding.lower().split(','):
        parts = [part.strip() for part in entry.split(';')]
        if not parts[0]:
            continue
        quality = 1.0
        for parameter in parts[1:]:
            key, sep, value = parameter.partition('=')
            if sep and key.strip() == 'q':
                try:
                    quality = float(value)
                except ValueError:
                    quality = 0.0
        qualities[parts[0]] = quality if 0 <= quality <= 1 else 0.0
    weight = qualities.get('gzip', qualities.get('*', 0))
    if weight <= 0 or qualities.get('identity', 0) > weight:
        return raw, False
    compressed = gzip.compress(raw, compresslevel=1, mtime=0)
    return (compressed, True) if len(compressed) < len(raw) else (raw, False)
