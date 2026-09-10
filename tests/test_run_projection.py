import json
from pathlib import Path
import sys
import threading
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ap_mind.studio_server import create_server
from ap_mind import agent_studio


def test_http_run_directory_is_small_and_exact_details_remain(tmp_path, monkeypatch):
    root = tmp_path/'project'; root.mkdir()
    server = create_server(port=0, data_dir=tmp_path/'data', project_root=root, codex_project_id='test')
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    studio = server.service.agent_studio
    monkeypatch.setattr(studio, '_execute', lambda *_: None)
    monkeypatch.setattr(studio, 'tick', lambda: None)
    monkeypatch.setattr(agent_studio, 'claude_executable', lambda: '/fixture/claude')
    try:
        agent = studio.save({'name':'worker','model':'fixture','api_key':'fixture-secret','base_url':'https://example.invalid/v1'})['agent']
        run = studio.start({'request_id':'projection-test','agent_id':agent['agent_id'],'project_id':'test','prompt':'Original target'})
        studio._state(run['run_id'], 'awaiting_review', result={'full_text':'large output ' * 100000, 'total_cost_usd':None})
        base = f'http://127.0.0.1:{server.server_port}/v1/ap-vibe/agents/runs'
        def read(query=''):
            with urlopen(base+query, timeout=10) as response:return response.read()
        small, complete, detail = read(), read('?compact=false'), read('?run_id='+run['run_id'])
        row = json.loads(small)['runs'][0]
        assert row['run_id'] == run['run_id'] and row['state'] == 'awaiting_review'
        assert row['prompt'] == 'Original target' and row['projection'] == 'summary'
        assert len(small) < len(complete)/100 and 'result' not in row
        assert json.loads(complete)['runs'] == json.loads(detail)['runs']
        assert studio.runs()['runs'][0]['result']['total_cost_usd'] is None
        assert json.loads(detail)['runs'][0]['result']['full_text'] == 'large output ' * 100000
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
