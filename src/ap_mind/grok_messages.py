"""Durable handoff to Grok Desktop's own loopback queue, never a second CLI."""
import hashlib
import json
import threading
from urllib.parse import urlsplit, quote
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler
from urllib.error import HTTPError

from .contracts import ContractError, utc_now
from .grok_sessions import desktop_home, load
from .organization_runner import _write_json, _read_json
from .product import redact_portable


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def connection(home=None):
    value = load((home or desktop_home())/'session-api.json', {}) or {}
    url, token = value.get('url'), value.get('token')
    parsed = urlsplit(url or '')
    if parsed.scheme!='http' or parsed.hostname!='127.0.0.1' or not parsed.port or parsed.username or parsed.password or parsed.path not in {'','/'} or parsed.query or parsed.fragment:
        raise ContractError('grok_desktop_not_connected')
    if not isinstance(token,str) or not token or '\n' in token or '\r' in token:
        raise ContractError('grok_desktop_not_connected')
    return url.rstrip('/'), token


def send_native(session, message, request_id):
    url, token = connection()
    body=json.dumps({'prompt':message,'idempotencyKey':request_id},ensure_ascii=False).encode('utf8')
    request=Request(url+'/v1/sessions/'+quote(session,safe='')+'/turns',data=body,
        headers={'Content-Type':'application/json','Authorization':'Bearer '+token})
    opener=build_opener(NoRedirect,ProxyHandler({}))
    try:
        with opener.open(request,timeout=12) as response:
            result=json.loads(response.read(65537))
    except HTTPError as exc:
        try:result=json.loads(exc.read(65537))
        except (ValueError,OSError):raise ContractError('grok_desktop_response_unknown') from None
    if not isinstance(result,dict) or result.get('sessionId')!=session or result.get('idempotencyKey')!=request_id:
        raise ContractError('grok_desktop_response_unknown')
    # Explicit allowlist: no native token, arbitrary error text or tool payload.
    return {k:result[k] for k in ('ok','status','sessionId','idempotencyKey','queueItemId') if k in result}


class GrokMessages:
    def __init__(self,service):
        self.service=service
        self.folder=service.data_dir/'grok-messages';self.folder.mkdir(parents=True,exist_ok=True)
        self.lock=threading.RLock()
        self.stop=threading.Event()
        self.worker=None
        queued=False
        for path in self.folder.glob('*.json'):
            value=_read_json(path)
            if value and value.get('status')=='dispatching':
                value.update(status='uncertain',detail='发送期间服务中断；请回看Grok原会话。原请求保留，不会自动重发。')
                _write_json(path,value)
            queued=queued or bool(value and value.get('status')=='queued')
        if queued:self._ensure_worker()

    def shutdown(self):
        self.stop.set()
        if self.worker:self.worker.join(timeout=15)

    def _ensure_worker(self):
        with self.lock:
            if not self.worker or not self.worker.is_alive():
                self.worker=threading.Thread(target=self._work,daemon=True)
                self.worker.start()

    def _work(self):
        while not self.stop.wait(2):
            try:
                with self.lock:
                    if not any((v:=_read_json(p)) and v.get('status')=='queued' for p in self.folder.glob('*.json')):
                        self.worker=None
                        return
                    self._drain_once()
            except Exception:continue

    def busy(self,session):
        source=self.source(session)
        observation=self.service.session_directory.observation(source['source_id'])
        return observation.get('state') in {'running','starting','cancelling'}

    def _drain_once(self):
        with self.lock:
            rows=sorted((v for p in self.folder.glob('*.json') if (v:=_read_json(p)) and v.get('status')=='queued'),
                        key=lambda v:v['created_at'])
            sent=set()
            for value in rows:
                if self.stop.is_set():return
                session=value['session_id']
                if session in sent:continue
                try:
                    connection()
                    if self.busy(session):continue
                    self._dispatch(value)
                    sent.add(session)
                except (OSError,ValueError,AttributeError,ContractError):continue

    def _dispatch(self,value):
        value.update(status='dispatching',dispatch_started_at=utc_now())
        path=self._path(value['request_id']);_write_json(path,value)
        try:
            receipt=send_native(value['session_id'],value['message'],value['request_id'])
            status=receipt.get('status')
            accepted=receipt.get('ok') is True and status in {'turn_started','queued'}
            rejected=status in {'busy','not_found','app_not_running','retry_later'} and receipt.get('ok') is False
            value.update(status='submitted' if accepted else 'failed' if rejected else 'uncertain',native_receipt=receipt,
                detail='Grok原会话已接收；回复显示在公开会话中。' if accepted else 'Grok未接收这条消息。' if rejected else '接收结果待核对，请回看Grok原会话。')
        except Exception:
            value.update(status='uncertain',detail='本地传输中断，接收结果未知；先查看Grok原会话，不自动重发。')
        value['updated_at']=utc_now();_write_json(path,value)

    def cancel(self,raw):
        if not isinstance(raw.get('session_id'),str) or not isinstance(raw.get('request_id'),str):
            raise ContractError('message_request_id_required')
        with self.lock:
            value=self.read_delivery(raw.get('session_id'),raw.get('request_id',''))
            if not value:raise ContractError('message_not_found')
            if value['status'] not in {'queued','cancelled'}:raise ContractError('message_already_dispatched')
            value.update(status='cancelled',detail='已撤回本地待发消息，尚未交给Grok。',updated_at=utc_now())
            _write_json(self._path(value['request_id']),value)
            return {'ok':True,'delivery':value}

    def _path(self,request_id):
        return self.folder/(hashlib.sha256(request_id.encode()).hexdigest()+'.json')

    def source(self,session):
        for s in self.service.external_sessions.discover()['sources']:
            if s['harness']=='grok' and s['session_id']==session and s.get('native_surface')=='desktop':
                return s
        raise ContractError('grok_desktop_session_not_found')

    def read_delivery(self,session,request_id):
        value=_read_json(self._path(request_id))
        return value if value and value.get('session_id')==session else None

    def list(self,session):
        self.source(session)
        values=[v for p in self.folder.glob('*.json') if (v:=_read_json(p)) and v.get('session_id')==session]
        try:connection();transport='grok_desktop_queue'
        except (OSError,ValueError,ContractError):transport='grok_desktop_offline'
        return {'ok':True,'transport':transport,'deliveries':sorted(values,key=lambda v:v['created_at'])[-40:]}

    def enqueue(self,raw):
        session,request_id,message=raw.get('session_id'),raw.get('request_id'),raw.get('message')
        if not isinstance(session,str) or not 1<=len(session)<=256:raise ContractError('message_session_id_invalid')
        if not isinstance(request_id,str) or not 1<=len(request_id)<=200:raise ContractError('message_request_id_required')
        if not isinstance(message,str) or not message.strip() or len(message)>12000:raise ContractError('message_text_invalid')
        message=redact_portable(message.strip(),preserve_local_paths=True)
        with self.lock:
            path=self._path(request_id);prior=_read_json(path)
            if prior:
                if prior['session_id']!=session or prior['message']!=message:raise ContractError('request_id_conflict')
                return {'ok':True,'replayed':True,'delivery':prior}
            waiting=self.busy(session)
            connection()  # Connection absence before dispatch is known to be safe to retry later.
            value={'request_id':request_id,'session_id':session,'message':message,'status':'queued','created_at':utc_now(),
                'detail':'Grok当前正在工作；消息已保存在本地，当前轮结束后发送。'}
            _write_json(path,value)
            if waiting:self._ensure_worker()
            else:self._dispatch(value)
            return {'ok':True,'delivery':value}
