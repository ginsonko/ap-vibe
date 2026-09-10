"""Released pixel assets obey the same import contract as user assets."""
import base64
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from ap_mind.studio_appearances import normalize


def test_builtin_atlases_use_valid_frames_and_content_hashes():
    import hashlib
    root=Path(__file__).resolve().parents[1]/'assets/characters'
    seen=set()
    for path in root.glob('*/manifest.json'):
        for character in json.loads(path.read_text(encoding='utf-8'))['characters']:
            assert character['appearance_id'] not in seen
            seen.add(character['appearance_id'])
            png=(path.parent/character['atlas']).read_bytes()
            assert hashlib.sha256(png).hexdigest()==character['sha256']
            result,_=normalize({'display_name':character['display_name'],
                                'png_base64':base64.b64encode(png).decode(),
                                'manifest':{k:character[k] for k in ['frame_width','frame_height','anchor','animations']}})
            assert result['anchor']==character['anchor']
            assert set(result['animations'])==set(character['animations'])
    assert 'deepseek-v3' in seen and 'deepseek-v1' in seen
