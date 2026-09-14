"""OpenAI wire compatibility for Grok's strict SSE decoder.

Some compatible providers emit choices:null heartbeat/usage chunks. Grok 1.0.30
requires an array and aborts otherwise. Normalize that field only, preserving
model output and failures. No model call is retried by this transport.
"""
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler
from urllib.request import Request, build_opener, ProxyHandler
from urllib.error import HTTPError
import uuid

from .grok_messages import NoRedirect
from .local_http import LocalThreadingHTTPServer


def normalize_chunk(value):
    if isinstance(value,dict) and value.get('choices','missing') is None:
        return {**value,'choices':[]}
    return value


class GrokTransport:
    def __init__(self, profile, key, observe, before_request=lambda:None):
        self.profile,self.key,self.observe,self.before_request=profile,key,observe,before_request
        self.token=secrets.token_urlsafe(32)
        self.exhausted=False
        self.normalized_chunks=0
        outer=self
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.0'
            def log_message(self,*args):pass
            def result(self,code,value):
                body=json.dumps(value).encode();self.send_response(code)
                self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body)
            def do_GET(self):
                if self.headers.get('Authorization')!='Bearer '+outer.token:
                    self.result(401,{'error':{'message':'Local connection token required'}});return
                if self.path!='/v1/models':self.result(404,{});return
                self.result(200,{'object':'list','data':[{'id':profile['model'],'object':'model','owned_by':'configured-provider'}]})
            def do_POST(self):
                if self.headers.get('Authorization')!='Bearer '+outer.token:
                    self.result(401,{'error':{'message':'Local connection token required'}});return
                if self.path!='/v1/chat/completions':self.result(404,{});return
                try:size=int(self.headers.get('Content-Length','0'))
                except ValueError:size=0
                if not 0<size<=32*1024*1024:self.result(413,{'error':{'message':'Request too large'}});return
                raw=self.rfile.read(size)
                try:
                    data=json.loads(raw)
                    if data.get('model')!=profile['model']:raise ValueError('Unconfigured model')
                except (ValueError,AttributeError):self.result(400,{'error':{'message':'Invalid native model request'}});return
                if outer.before_request():
                    outer.exhausted=True;self.result(402,{'error':{'message':'AP-Vibe configured budget exhausted'}});return
                identity='grok-upstream-'+uuid.uuid4().hex
                observed=False
                started=False
                def usage(value):
                    nonlocal observed
                    if isinstance(value,dict) and isinstance(value.get('usage'),dict):
                        outer.observe(identity,value['usage']);observed=True
                request=Request(profile['base_url'].rstrip('/')+'/chat/completions',data=raw,
                    headers={'Authorization':'Bearer '+key,'Content-Type':'application/json','Accept':'text/event-stream'})
                try:
                    upstream=build_opener(NoRedirect,ProxyHandler({})).open(request,timeout=profile.get('request_timeout_seconds',300))
                    with upstream:
                        self.send_response(upstream.status)
                        content=upstream.headers.get('Content-Type','application/json')
                        self.send_header('Content-Type',content);self.end_headers();started=True
                        if 'text/event-stream' in content:
                            frame=[];frame_bytes=0
                            def forward():
                                if not frame:return
                                payload=b'\n'.join(line[5:].lstrip(b' ') for line in frame if line.startswith(b'data:'))
                                if payload and payload.strip()!=b'[DONE]':
                                    try:
                                        value=json.loads(payload);usage(value);updated=normalize_chunk(value)
                                        if updated is not value:
                                            outer.normalized_chunks+=1
                                            preserved=[line for line in frame if not line.startswith(b'data:')]
                                            self.wfile.write(b'\n'.join(preserved+[b'data: '+json.dumps(updated,separators=(',',':')).encode()])+b'\n\n');self.wfile.flush();return
                                    except (ValueError,UnicodeError):pass
                                self.wfile.write(b'\n'.join(frame)+b'\n\n');self.wfile.flush()
                            while True:
                                line=upstream.readline(4*1024*1024+1)
                                if not line:
                                    forward();break
                                frame_bytes+=len(line)
                                if frame_bytes>8*1024*1024:raise ValueError('Oversize upstream SSE frame')
                                line=line.rstrip(b'\r\n')
                                if not line:
                                    forward();frame=[];frame_bytes=0
                                else:frame.append(line)
                        else:
                            body=upstream.read(32*1024*1024+1)
                            if len(body)>32*1024*1024:raise ValueError('Oversize upstream body')
                            try:usage(json.loads(body))
                            except ValueError:pass
                            self.wfile.write(body)
                except HTTPError as exc:
                    self.result(exc.code,{'error':{'message':'Configured upstream returned HTTP '+str(exc.code)}})
                except Exception:
                    if not started:self.result(502,{'error':{'message':'Configured upstream transport interrupted; result unknown'}})
                finally:
                    if not observed:outer.observe(identity,None)
        self.server=LocalThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.url=f'http://127.0.0.1:{self.server.server_port}/v1'

    def start(self):
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start();return self

    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join(timeout=2)
