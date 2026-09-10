"""Prepare a uniform 3x3 character sheet for transparent static-preview assets.

Requires Pillow only for this optional asset-production command, not the daemon.
The output declares one idle frame; no generated animation is implied.
"""
import argparse
from collections import Counter, deque
import hashlib
import json
from pathlib import Path
import shutil

from PIL import Image, ImageDraw, ImageFont


FAMILIES = ['DeepSeek', 'Kimi', 'GLM', 'Qwen', 'Gemini', 'Claude', 'GPT', 'Grok', 'OpenCode']


def remove_background(image):
    image = image.convert('RGBA')
    width, height = image.size
    pixels = image.load()
    border = [pixels[x, y][:3] for x in range(width) for y in (0, height - 1)]
    border += [pixels[x, y][:3] for y in range(height) for x in (0, width - 1)]
    background = Counter(border).most_common(1)[0][0]
    visited = set()
    queue = deque([(x, y) for x in range(width) for y in (0, height - 1)] +
                  [(x, y) for y in range(height) for x in (0, width - 1)])
    while queue:
        x, y = queue.popleft()
        if (x, y) in visited or not (0 <= x < width and 0 <= y < height):
            continue
        visited.add((x, y))
        if max(abs(pixels[x, y][c] - background[c]) for c in range(3)) > 18:
            continue
        pixels[x, y] = (0, 0, 0, 0)
        queue.extend(((x - 1, y), (x + 1, y), (x, y - 1), (x, y + 1)))
    return image, background


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--font', type=Path, help='Optional local TrueType font for the preview labels')
    args = parser.parse_args()
    original = Image.open(args.source)
    if original.width != original.height or original.width % 3:
        raise ValueError('Expected a square sheet of nine equal cells')
    args.output.mkdir(parents=True, exist_ok=True)
    shutil.copy2(args.source, args.output / 'source-sheet.png')
    cell = original.width // 3
    manifest = {'schema_version': 1, 'status': 'static_preview', 'source_sha256': digest(args.source),
                'production': {'method': 'gpt-image-2 via Yinzi media workflow, then local extraction',
                               'source_generation_id': 41, 'source_task_id': '536b51ea-3fa5-4e3a-b405-e8322265887b',
                               'original_reference_images_distributed': False},
                'characters': []}
    preview = Image.new('RGB', (960, 1056), '#eff6f7')
    draw = ImageDraw.Draw(preview)
    fonts = [args.font, Path('C:/Windows/Fonts/arial.ttf'),
             Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')]
    font_path = next((str(file) for file in fonts if file and file.is_file()), None)
    font = ImageFont.truetype(font_path, 24) if font_path else ImageFont.load_default()
    for index, family in enumerate(FAMILIES):
        col, row = index % 3, index // 3
        crop = original.crop((col * cell, row * cell, (col + 1) * cell, (row + 1) * cell))
        clean, background = remove_background(crop)
        bounds = clean.getbbox()
        if not bounds or bounds[0] < 2 or bounds[1] < 2 or bounds[2] >= cell - 1 or bounds[3] >= cell - 1:
            raise ValueError(f'{family}: extraction touches cell boundary')
        body = clean.crop(bounds)
        scale = min(88 / body.width, 90 / body.height)
        size = (max(1, round(body.width * scale)), max(1, round(body.height * scale)))
        body = body.resize(size, Image.Resampling.NEAREST)
        alpha = body.getchannel('A')
        body = body.convert('RGB').quantize(colors=64).convert('RGBA')
        body.putalpha(alpha)
        frame = Image.new('RGBA', (96, 96))
        frame.alpha_composite(body, ((96 - size[0]) // 2, 94 - size[1]))
        file = args.output / f'{family.lower()}-idle.png'
        frame.save(file)
        counts = Counter(frame.getchannel('A').getdata())
        assert counts[0] > 96 * 96 * .2 and counts[255] > 500
        manifest['characters'].append({'appearance_id': family.lower() + '-v1', 'version': 1,
            'display_name': family, 'family_hint': None if family == 'OpenCode' else family,
            'executor_hint': 'opencode' if family == 'OpenCode' else None,
            'atlas': file.name, 'sha256': digest(file), 'frame_width': 96, 'frame_height': 96,
            'anchor': {'x': 48, 'y': 94}, 'source_cell': index, 'background_rgb': background,
            'animations': {'idle': {'frames': [0], 'fps': 0, 'direction': 'front', 'loop': False}},
            'missing_animations': ['walk', 'work', 'read', 'talk', 'wait', 'rest'],
            'alpha': {'transparent_pixels': counts[0], 'opaque_pixels': counts[255]},
            'attribution': {'creator': 'AP-Vibe / generated original redesign',
                            'reference_role': 'user-provided fan-character style directions',
                            'license': 'AP-Vibe Personal Noncommercial 1.0; no upstream brand endorsement'}})
        x, y = col * 320, row * 352
        draw.rectangle((x + 8, y + 8, x + 312, y + 304), fill='#dcebed' if index % 2 == 0 else '#263538')
        enlarged = frame.resize((288, 288), Image.Resampling.NEAREST)
        preview.paste(enlarged, (x + 16, y + 12), enlarged)
        draw.text((x + 160, y + 327), family, font=font, fill='#142e31', anchor='mm')
    preview.save(args.output / 'static-preview.png')
    (args.output / 'manifest.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'characters': len(manifest['characters']), 'status': manifest['status'],
                      'source_sha256': manifest['source_sha256'], 'output': str(args.output)}, ensure_ascii=False))


if __name__ == '__main__':
    main()
