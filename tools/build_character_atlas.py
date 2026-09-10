"""Normalize a reviewed, equal-cell chroma sheet without inventing missing actions."""
import argparse
import hashlib
import json
from pathlib import Path
from PIL import Image, ImageDraw


def keyed(pixel, spec):
    r, g, b, a = pixel
    if a < spec.get('alpha_threshold', 1):
        return True
    key = spec.get('chroma_key')
    if key is not None:
        distance = sum((value - target) ** 2 for value, target in zip((r, g, b), key))
        if distance <= spec.get('chroma_tolerance', 90) ** 2:
            return True
        dominance = spec.get('chroma_dominance')
        channel = primary_channel(key)
        if dominance is not None and channel is not None:
            rgb = (r, g, b)
            return rgb[channel] - max(v for i, v in enumerate(rgb) if i != channel) > dominance
        return False
    return r > 110 and b > 110 and g < 150 and r > g * 1.55 and b > g * 1.55


def primary_channel(key):
    if key is None:
        return None
    channel = max(range(3), key=lambda i: key[i])
    others = [v for i, v in enumerate(key) if i != channel]
    return channel if key[channel] - max(others) >= 128 else None


def foreground(pixel, spec):
    rgb = list(pixel[:3])
    channel = primary_channel(spec.get('chroma_key'))
    if spec.get('suppress_chroma_spill') and channel is not None:
        rgb[channel] = min(rgb[channel], max(v for i, v in enumerate(rgb) if i != channel))
    return (*rgb, 255 if spec.get('opaque_pixels') else pixel[3])


def source_edges(length, count, configured=None):
    assert type(count) is int and count > 0, 'Invalid source grid count'
    if configured is None:
        assert length % count == 0, 'Unequal source cells require explicit edges'
        return [i * (length // count) for i in range(count + 1)]
    assert isinstance(configured, list) and len(configured) == count + 1, 'Invalid source edges'
    assert all(type(value) is int for value in configured), 'Source edges must be integers'
    assert configured[0] == 0 and configured[-1] == length, 'Source edges must span the image'
    assert all(a < b for a, b in zip(configured, configured[1:])), 'Source edges must increase'
    return configured


def build(source, spec, target):
    if spec.get('chroma_key') is not None:
        assert len(spec['chroma_key']) == 3 and all(type(v) is int and 0 <= v <= 255 for v in spec['chroma_key']), 'Invalid chroma color'
        assert type(spec.get('chroma_tolerance', 90)) in (int, float) and 0 <= spec.get('chroma_tolerance', 90) <= 255, 'Invalid chroma tolerance'
        if spec.get('chroma_dominance') is not None:
            assert type(spec['chroma_dominance']) in (int, float) and 0 <= spec['chroma_dominance'] <= 255, 'Invalid chroma dominance'
    image = Image.open(source).convert('RGBA')
    cols, rows = spec['columns'], spec['rows']
    xs = source_edges(image.width, cols, spec.get('source_column_edges'))
    ys = source_edges(image.height, rows, spec.get('source_row_edges'))
    cells, measures = [], []
    for index in range(cols * rows):
        col, row = index % cols, index // cols
        rect = (xs[col], ys[row], xs[col + 1], ys[row + 1])
        cell = image.crop(rect)
        cw, ch = cell.size
        pixels = cell.load()
        for y in range(ch):
            for x in range(cw):
                r, g, b, a = pixels[x, y]
                if keyed((r, g, b, a), spec):
                    pixels[x, y] = (0, 0, 0, 0)
                else:
                    pixels[x, y] = foreground((r, g, b, a), spec)
        box = cell.getbbox()
        assert box and box[0] > 0 and box[1] > 0 and box[2] < cw and box[3] < ch, f'Cell {index} touches boundary'
        feet = [(x, y) for y in range(max(box[1], box[3] - 5), box[3])
                for x in range(box[0], box[2]) if pixels[x, y][3]]
        foot_x = sum(x for x, _ in feet) / len(feet)
        cells.append(cell)
        measures.append({'index': index, 'source_bbox': box, 'source_foot_x': foot_x, 'source_cell': rect})
    scale = min(84 / max(m['source_bbox'][3] - m['source_bbox'][1] for m in measures),
                88 / max(m['source_bbox'][2] - m['source_bbox'][0] for m in measures))
    frames = []
    for cell, m in zip(cells, measures):
        box = m['source_bbox']
        cropped = cell.crop(box)
        resized = cropped.resize((round(cropped.width * scale), round(cropped.height * scale)), Image.Resampling.NEAREST)
        x = round(48 - (m['source_foot_x'] - box[0]) * scale)
        y = 94 - resized.height
        assert x >= 0 and x + resized.width <= 96, f'Foot alignment clips frame {m["index"]}'
        frame = Image.new('RGBA', (96, 96))
        frame.alpha_composite(resized, (x, y))
        frames.append(frame)
        m.update(output_bbox=frame.getbbox(), sha256=hashlib.sha256(frame.tobytes()).hexdigest())
    atlas = Image.new('RGBA', (cols * 96, rows * 96))
    for i, frame in enumerate(frames):
        atlas.alpha_composite(frame, (i % cols * 96, i // cols * 96))
    target.mkdir(parents=True, exist_ok=True)
    atlas_path = target / spec['atlas']
    atlas.save(atlas_path)
    actions = spec['animations']
    for name, action in actions.items():
        assert all(0 <= index < len(frames) for index in action['frames'])
        if len(action['frames']) > 1:
            assert len({measures[i]['sha256'] for i in action['frames']}) > 1, name + ' has no pixel motion'
        rendered = []
        for index in action['frames']:
            preview = Image.new('RGBA', (192, 192), '#e3eee7')
            preview.alpha_composite(frames[index].resize((192, 192), Image.Resampling.NEAREST))
            rendered.append(preview.convert('RGB'))
        rendered[0].save(target / (name + '.gif'), save_all=True, append_images=rendered[1:],
                         duration=round(1000 / max(action.get('fps', 0), 1)), loop=0)
    character = {**{k: v for k, v in spec.items() if k not in ('columns', 'rows')},
                 'frame_width': 96, 'frame_height': 96, 'atlas_width': atlas.width, 'atlas_height': atlas.height,
                 'anchor': {'x': 48, 'y': 94}, 'sha256': hashlib.sha256(atlas_path.read_bytes()).hexdigest(),
                 'source_sha256': hashlib.sha256(Path(source).read_bytes()).hexdigest(),
                 'missing_animations': [name for name in ['idle', 'walk', 'work', 'read', 'talk', 'wait', 'rest'] if name not in actions]}
    (target / 'manifest.json').write_text(json.dumps({'schema_version': 1, 'characters': [character]}, ensure_ascii=False, indent=2), encoding='utf-8')
    (target / 'extraction.json').write_text(json.dumps({'scale': scale, 'source_size': image.size, 'frames': measures}, indent=2), encoding='utf-8')
    overview = Image.new('RGB', (cols * 192, rows * 208), '#e3eee7')
    draw = ImageDraw.Draw(overview)
    for i, frame in enumerate(frames):
        x, y = i % cols * 192, i // cols * 208
        overview.paste(frame.resize((192, 192), Image.Resampling.NEAREST), (x, y), frame.resize((192, 192), Image.Resampling.NEAREST))
        draw.text((x + 8, y + 191), str(i), fill='#2b4c43')
    overview.save(target / 'contact-sheet.png')
    print(json.dumps({'appearance_id': character['appearance_id'], 'frames': len(frames), 'missing': character['missing_animations'], 'sha256': character['sha256']}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('spec', type=Path)
    parser.add_argument('target', type=Path)
    args = parser.parse_args()
    build(args.source, json.loads(args.spec.read_text(encoding='utf-8')), args.target)
