import gzip
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import sys
import threading
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from ap_mind.http_encoding import json_transfer
from ap_mind.studio_server import StudioRequestHandler


def test_encoding_negotiation_and_byte_preservation():
    raw = json.dumps({'活动': ['公开事实与完整回读'] * 1500}, ensure_ascii=False).encode()
    for header in ('gzip', 'br, gzip', '*;q=0.5', 'GZip;q=0.4, identity;q=0.2'):
        encoded, used = json_transfer(raw, header)
        assert used and gzip.decompress(encoded) == raw
    for header in ('', 'br', 'gzip;q=0', '*;q=1, gzip;q=0', 'gzip;q=nan', 'gzip;q=0.5, identity;q=1'):
        assert json_transfer(raw, header) == (raw, False)
    assert json_transfer(b'{"ok":true}', 'gzip') == (b'{"ok":true}', False)


def test_real_http_content_length_status_and_identity():
    payload = {'state': {'events': [{'text': '中文证据', 'score': None}] * 2000}}

    class Handler(StudioRequestHandler):
        def do_GET(self):
            self._write_json(503 if self.path == '/error' else 200, payload)

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        base = f'http://127.0.0.1:{server.server_port}'
        def read(encoding):
            with urlopen(Request(base, headers={'Accept-Encoding': encoding})) as response:
                value = response.read()
                assert len(value) == int(response.headers['Content-Length'])
                assert response.headers['Vary'] == 'Accept-Encoding'
                assert response.headers['Cache-Control'] == 'no-store'
                return value, response.headers.get('Content-Encoding')
        identity, label = read('identity'); assert label is None
        compressed, label = read('gzip'); assert label == 'gzip'
        assert gzip.decompress(compressed) == identity
        assert len(compressed) < len(identity) / 10
        from urllib.error import HTTPError
        try:
            urlopen(Request(base+'/error', headers={'Accept-Encoding':'gzip'}))
        except HTTPError as exc:
            assert exc.code == 503
            assert json.loads(gzip.decompress(exc.read())) == payload
        else:
            raise AssertionError('Error status was changed')
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=2)
