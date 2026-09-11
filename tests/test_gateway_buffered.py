"""Exercise real HTTP boundaries when upstream tool streaming is unreliable."""
import json
import threading
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from ap_mind.claude_gateway import ClaudeGateway


@pytest.mark.parametrize('protocol', ['openai', 'anthropic'])
@pytest.mark.parametrize('invalid', [False, True])
def test_buffered_client_stream_uses_one_complete_upstream_request(protocol, invalid):
    calls = []
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            if invalid:
                self.wfile.write(b'{"error":{"message":"provider result incomplete"}}')
                return
            if protocol == 'openai':
                result = {'id':'actual-response', 'choices':[{'finish_reason':'tool_calls','message':{
                    'content':'Inspect before changing.', 'tool_calls':[
                        {'id':'read-file-1','function':{'name':'Read','arguments':'{"path":"notes.md"}'}},
                        {'id':'read-file-2','function':{'name':'Read','arguments':'{"path":"design.md"}'}}]}}],
                    'usage':{'prompt_tokens':20,'completion_tokens':9}}
            else:
                result = {'id':'actual-response','type':'message','role':'assistant','model':'chosen',
                    'content':[{'type':'text','text':'Inspect before changing.'},
                        {'type':'tool_use','id':'read-file-1','name':'Read','input':{'path':'notes.md'}},
                        {'type':'tool_use','id':'read-file-2','name':'Read','input':{'path':'design.md'}}],
                    'stop_reason':'tool_use','stop_sequence':None,'usage':{'input_tokens':20,'output_tokens':9}}
            self.wfile.write(json.dumps(result).encode())
    server=ThreadingHTTPServer(('127.0.0.1',0),Upstream)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    observed=[]
    gateway=ClaudeGateway(f'http://127.0.0.1:{server.server_port}/v1','fixture','chosen',
                          observed.append,protocol=protocol,upstream_mode='buffered').start()
    try:
        request=urllib.request.Request(gateway.url+'/v1/messages',data=json.dumps({
            'model':'alias','stream':True,'messages':[{'role':'user','content':'Inspect both files'}],
            'max_tokens':100}).encode(),headers={'x-api-key':gateway.token})
        with urllib.request.urlopen(request,timeout=5) as response:
            assert response.headers['Content-Type']=='text/event-stream'
            lines=response.read().decode().splitlines()
        events=[json.loads(line[6:]) for line in lines if line.startswith('data: ')]
        assert len(calls)==1 and calls[0]['stream'] is False and 'stream_options' not in calls[0]
        assert calls[0]['model']=='chosen'
        if invalid:
            assert any(e['type']=='error' for e in events)
            assert not any(e['type']=='message_stop' for e in events)
            with pytest.raises(urllib.error.HTTPError): urllib.request.urlopen(request,timeout=5)
            assert len(calls)==1
            assert not any(e['phase']=='completed' for e in observed)
        else:
            blocks=[e['content_block'] for e in events if e['type']=='content_block_start']
            assert [b.get('id') for b in blocks if b['type']=='tool_use']==['read-file-1','read-file-2']
            inputs=[json.loads(e['delta']['partial_json']) for e in events
                    if e.get('delta',{}).get('type')=='input_json_delta']
            assert inputs==[{'path':'notes.md'},{'path':'design.md'}]
            assert events[-1]['type']=='message_stop'
            assert sum(e['type']=='content_block_stop' for e in events)==3
            assert events[-2]['delta']['stop_reason']=='tool_use'
            assert events[-2]['usage']['output_tokens']==9
            assert len([e for e in observed if e['phase']=='completed'])==1
    finally:
        gateway.close();server.shutdown();server.server_close();thread.join(timeout=2)
