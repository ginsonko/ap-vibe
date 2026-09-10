"""Bounded local App Server requests, without resuming or creating tasks."""
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from ap_mind.codex_cli import codex_command
from ap_mind.organization_runner import _terminate_process_tree


class CodexRpc:
    def __enter__(self):
        self.events=queue.Queue()
        self.counter=0
        self.process=subprocess.Popen(codex_command()+['app-server','--listen','stdio://'],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),close_fds=True)
        def read():
            for line in self.process.stdout:
                try:self.events.put(json.loads(line))
                except ValueError:pass
        threading.Thread(target=read,daemon=True).start()
        try:
            self.request('initialize',{'clientInfo':{'name':'ap_vibe_local','version':'0.1.0'},'capabilities':{'experimentalApi':True}})
            self.send({'method':'initialized'})
            return self
        except Exception:
            _terminate_process_tree(self.process)
            raise

    def send(self,value):
        self.process.stdin.write((json.dumps(value)+'\n').encode('utf8'))
        self.process.stdin.flush()

    def request(self,method,params,timeout=15):
        self.counter+=1
        identity=self.counter
        self.send({'id':identity,'method':method,'params':params})
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            try:event=self.events.get(timeout=min(.5,max(.01,deadline-time.monotonic())))
            except queue.Empty:
                if self.process.poll() is not None:raise RuntimeError('Codex 本地接口已退出')
                continue
            if event.get('id')!=identity:continue
            if 'error' in event:raise RuntimeError('Codex 本地接口返回错误：'+str(event['error'].get('code')))
            return event.get('result',{})
        raise TimeoutError('Codex 本地接口等待超时')

    def __exit__(self,*_):
        _terminate_process_tree(self.process)
