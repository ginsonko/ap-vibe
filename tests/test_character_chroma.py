from tools.build_character_atlas import foreground, keyed
from tools.build_character_atlas import build, source_edges
from PIL import Image, ImageDraw
import json
import pytest


def test_optional_chroma_dominance_preserves_character_colors():
    spec = {'chroma_key':[0,255,0], 'chroma_tolerance':90, 'chroma_dominance':35, 'suppress_chroma_spill':True}
    assert keyed((15,90,20,255),spec)
    assert not keyed((15,90,20,255),{'chroma_key':[0,255,0]})
    for color in [(110,80,180,255),(22,22,22,255),(230,230,230,255),(70,90,210,255)]:
        assert not keyed(color,spec)
        assert foreground(color,spec)==color
    assert foreground((30,45,32,255),spec)==(30,32,32,255)


def test_legacy_and_nonprimary_chroma_remain_unchanged():
    assert keyed((250,0,250,255),{})
    assert not keyed((20,40,80,255),{})
    assert foreground((40,55,42,80),{})==(40,55,42,80)
    assert foreground((40,55,42,80),{'chroma_key':[255,0,255],'suppress_chroma_spill':True})==(40,55,42,80)
    assert foreground((40,55,42,80),{'opaque_pixels':True})==(40,55,42,255)


def test_explicit_source_grid_preserves_complete_feet(tmp_path):
    image = Image.new('RGBA', (20, 20))
    draw = ImageDraw.Draw(image)
    for x in [3, 13]:
        draw.rectangle((x, 7, x + 2, 11), fill=(70, 80, 140, 255))
        draw.rectangle((x, 14, x + 2, 16), fill=(90, 30, 140, 255))
    source = tmp_path / 'source.png'
    image.save(source)
    spec = {'columns':2, 'rows':2, 'atlas':'test-atlas.png', 'appearance_id':'test',
            'animations':{'idle':{'frames':[0], 'fps':0}}}
    with pytest.raises(AssertionError, match='touches boundary'):
        build(source, spec, tmp_path / 'equal')
    build(source, {**spec, 'source_row_edges':[0, 13, 20]}, tmp_path / 'explicit')
    evidence = json.loads((tmp_path / 'explicit/extraction.json').read_text())
    assert evidence['frames'][0]['source_bbox'] == [3, 7, 6, 12]
    assert evidence['frames'][2]['source_bbox'] == [3, 1, 6, 4]
    assert Image.open(tmp_path / 'explicit/test-atlas.png').size == (192, 192)


def test_grid_validation_keeps_default_and_rejects_ambiguous_layout():
    assert source_edges(1024, 4) == [0, 256, 512, 768, 1024]
    for invalid in [[0, 10, 20], [0, True, 8, 12, 20], [0, 5, 5, 15, 20],
                    [1, 5, 10, 15, 20], [0, 5, 10, 15, 19]]:
        with pytest.raises(AssertionError):
            source_edges(20, 4, invalid)
