"""Immutable local PNG appearances, independent of model configuration."""
import base64
import binascii
from contextlib import closing
import hashlib
import json
import re
import struct
import zlib

from .contracts import ContractError, utc_now

MAX_PNG_BYTES = 2 * 1024 * 1024
ACTIONS = ('idle', 'walk', 'work', 'read', 'talk', 'wait', 'rest')


def integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ContractError('appearance_frame_invalid')
    return value


def clean_png(encoded):
    if not isinstance(encoded, str) or len(encoded) > (MAX_PNG_BYTES * 4 // 3 + 8):
        raise ContractError('appearance_image_invalid')
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ContractError('appearance_image_invalid') from exc
    if len(data) > MAX_PNG_BYTES or data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ContractError('appearance_image_invalid')
    pos, parts, compressed = 8, [], bytearray()
    width = height = channels = 0
    ended = False
    while pos + 12 <= len(data):
        length = struct.unpack('>I', data[pos:pos + 4])[0]
        end = pos + 12 + length
        if end > len(data):
            raise ContractError('appearance_image_invalid')
        kind = data[pos + 4:pos + 8]
        payload = data[pos + 8:pos + 8 + length]
        crc = struct.unpack('>I', data[end - 4:end])[0]
        if zlib.crc32(kind + payload) & 0xffffffff != crc:
            raise ContractError('appearance_image_invalid')
        if kind == b'IHDR':
            if parts or length != 13:
                raise ContractError('appearance_image_invalid')
            width, height, depth, color, compression, filtering, interlace = struct.unpack('>IIBBBBB', payload)
            if not (1 <= width <= 2048 and 1 <= height <= 2048 and depth == 8
                    and color in (2, 6) and compression == filtering == interlace == 0):
                raise ContractError('appearance_image_invalid')
            channels = 4 if color == 6 else 3
        elif not width:
            raise ContractError('appearance_image_invalid')
        if kind == b'IDAT':
            compressed.extend(payload)
        if kind in (b'IHDR', b'IDAT', b'IEND'):
            parts.append(data[pos:end])
        elif not kind[0] & 32:
            raise ContractError('appearance_image_invalid')
        pos = end
        if kind == b'IEND':
            if length or pos != len(data):
                raise ContractError('appearance_image_invalid')
            ended = True
            break
    if not ended or not compressed:
        raise ContractError('appearance_image_invalid')
    expected = height * (1 + width * channels)
    try:
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(compressed, expected + 1)
        if len(pixels) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise ValueError('PNG scanline length')
        if any(pixels[i] > 4 for i in range(0, expected, 1 + width * channels)):
            raise ValueError('PNG filter')
    except (ValueError, zlib.error) as exc:
        raise ContractError('appearance_image_invalid') from exc
    return b'\x89PNG\r\n\x1a\n' + b''.join(parts), width, height


def normalize(raw):
    png, width, height = clean_png(raw.get('png_base64'))
    manifest = raw.get('manifest')
    if manifest is None:
        manifest = {}
    if not isinstance(manifest, dict) or manifest.get('schema_version', 1) != 1:
        raise ContractError('appearance_manifest_invalid')
    name = raw.get('display_name') or manifest.get('display_name')
    if not isinstance(name, str) or not name.strip() or len(name) > 80 or any(ord(c) < 32 for c in name):
        raise ContractError('appearance_name_invalid')
    fw = integer(manifest.get('frame_width', width), 1, width)
    fh = integer(manifest.get('frame_height', height), 1, height)
    if width % fw or height % fh:
        raise ContractError('appearance_frame_invalid')
    count = (width // fw) * (height // fh)
    anchor = manifest.get('anchor', {'x': fw // 2, 'y': fh})
    if not isinstance(anchor, dict):
        raise ContractError('appearance_frame_invalid')
    anchor = {'x': integer(anchor.get('x'), 0, fw), 'y': integer(anchor.get('y'), 0, fh)}
    source = manifest.get('animations', {'idle': {'frames': [0], 'fps': 0, 'loop': False}})
    if not isinstance(source, dict) or 'idle' not in source or len(source) > 40:
        raise ContractError('appearance_manifest_invalid')
    animations = {}
    for action, spec in source.items():
        if not isinstance(action, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,39}', action) or not isinstance(spec, dict):
            raise ContractError('appearance_manifest_invalid')
        frames = spec.get('frames')
        if not isinstance(frames, list) or not 1 <= len(frames) <= 256:
            raise ContractError('appearance_frame_invalid')
        frames = [integer(frame, 0, count - 1) for frame in frames]
        fps = spec.get('fps', 6 if len(frames) > 1 else 0)
        if type(fps) not in (int, float) or not 0 <= fps <= 30 or (len(frames) > 1 and fps == 0):
            raise ContractError('appearance_frame_invalid')
        animations[action] = {'frames': frames, 'fps': fps, 'loop': bool(spec.get('loop', True)),
                              'direction': str(spec.get('direction', 'front'))[:20]}
    attribution = raw.get('attribution', manifest.get('attribution', ''))
    if not isinstance(attribution, str) or len(attribution) > 1000:
        raise ContractError('appearance_manifest_invalid')
    result = {'schema_version': 1, 'display_name': name.strip(), 'frame_width': fw, 'frame_height': fh,
              'atlas_width': width, 'atlas_height': height, 'anchor': anchor, 'animations': animations,
              'missing_animations': [key for key in ACTIONS if key not in animations],
              'attribution': attribution, 'sha256': hashlib.sha256(png).hexdigest()}
    digest = hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    result.update(appearance_id='custom-' + digest, version=1, custom=True)
    return result, png


class StudioAppearances:
    def __init__(self, registry):
        self.registry = registry
        with closing(registry._connect()) as c:
            c.execute('''CREATE TABLE IF NOT EXISTS studio_appearances(
                appearance_id TEXT PRIMARY KEY, manifest_json TEXT NOT NULL,
                png BLOB NOT NULL, archived INTEGER NOT NULL DEFAULT 0)''')
            c.commit()

    @staticmethod
    def public(row):
        item = json.loads(row['manifest_json'])
        return {**item, 'archived': bool(row['archived']),
                'url': '/v1/ap-vibe/agents/appearance-image?id=' + item['appearance_id']}

    def list(self, appearance_id=None):
        with closing(self.registry._connect()) as c:
            rows = c.execute('SELECT appearance_id,manifest_json,archived FROM studio_appearances WHERE ' +
                             ('appearance_id=?' if appearance_id else 'archived=0') + ' ORDER BY rowid DESC',
                             (appearance_id,) if appearance_id else ()).fetchall()
        return {'ok': True, 'appearances': [self.public(row) for row in rows]}

    def save(self, raw):
        item, png = normalize(raw)
        item['created_at'] = utc_now()
        with self.registry.transaction():
            c = self.registry._connect()
            c.execute('INSERT OR IGNORE INTO studio_appearances(appearance_id,manifest_json,png) VALUES (?,?,?)',
                      (item['appearance_id'], json.dumps(item, ensure_ascii=False), png))
            c.execute('UPDATE studio_appearances SET archived=0 WHERE appearance_id=?', (item['appearance_id'],))
        return self.list(item['appearance_id'])

    def image(self, appearance_id):
        with closing(self.registry._connect()) as c:
            row = c.execute('SELECT png FROM studio_appearances WHERE appearance_id=?', (appearance_id,)).fetchone()
        if not row:
            raise ContractError('appearance_not_found')
        return bytes(row['png'])

    def archive(self, raw):
        with self.registry.transaction():
            self.registry._connect().execute('UPDATE studio_appearances SET archived=1 WHERE appearance_id=?',
                                            (raw.get('appearance_id'),))
        return {'ok': True, 'history_retained': True}
