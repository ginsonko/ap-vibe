import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parents[1] / 'src'))
from ap_mind.collaboration import CollaborationStore
import pytest

def test_collaboration_message_claim_handoff_dependency_persists(tmp_path):
 s=CollaborationStore(tmp_path/'c.sqlite')
 a=s.send({'request_id':'m1','sender':'planner','recipient':'worker','body':'开始检查'})
 assert a['ok'] and s.send({'request_id':'m1','sender':'planner','recipient':'worker','body':'开始检查'})['replayed']
 assert s.claim({'task_id':'t1','agent_id':'worker'})['status']=='claimed'
 with pytest.raises(Exception): s.claim({'task_id':'t1','agent_id':'reviewer'})
 h=s.handoff({'task_id':'t1','from_agent':'worker','to_agent':'reviewer','note':'请接手验收'})
 assert h['to_agent']=='reviewer'
 d=s.dependency({'task_id':'t2','depends_on':'t1','status':'completed'})
 assert d['woken'] and len(s.state()['messages'])==1
 s2=CollaborationStore(tmp_path/'c.sqlite'); state=s2.state(); assert state['claims'][0]['agent_id']=='reviewer' and state['dependencies'][0]['status']=='completed'

def test_ready_dependency_is_recoverable(tmp_path):
 s=CollaborationStore(tmp_path/'c.sqlite')
 s.dependency({'task_id':'waiter','depends_on':'worker-task','status':'waiting'})
 assert s.ready()['ready_tasks']==[]
 s.dependency({'task_id':'worker-task','depends_on':'worker-task','status':'completed'})
 assert s.ready()['ready_tasks']==[{'task_id':'waiter','depends_on':'worker-task','woken':True}]

def test_messages_are_scoped_to_task_and_agent(tmp_path):
 s=CollaborationStore(tmp_path/'c.sqlite')
 s.send({'request_id':'task-msg','sender':'planner','recipient':'agent-a','task_id':'run-a','body':'先检查接口契约'})
 s.send({'request_id':'other-msg','sender':'planner','recipient':'agent-b','task_id':'run-b','body':'无关消息'})
 relevant=s.for_task('run-a','agent-a')
 assert [m['body'] for m in relevant] == ['先检查接口契约']
 assert relevant[0]['task_id']=='run-a'
 s.send({'request_id':'neighbor','sender':'planner','recipient':'agent-a','task_id':'run-other','body':'同一Agent其它任务'})
 assert len(s.for_task('run-a','agent-a'))==1
 with pytest.raises(Exception,match='request_conflict'):
  s.send({'request_id':'task-msg','sender':'planner','recipient':'agent-a','task_id':'run-a','body':'变更内容'})

def test_broadcast_is_atomic_and_idempotent(tmp_path):
 s=CollaborationStore(tmp_path/'broadcast.sqlite')
 payload={'request_id':'broadcast-1','sender':'planner','recipients':['a','b','a'],'task_id':'run-x','body':'请报告进度'}
 first=s.broadcast(payload)
 assert first['ok'] and first['recipients']==['a','b'] and len(first['messages'])==2
 replay=s.broadcast(payload)
 assert replay['replayed'] and replay['broadcast_id']==first['broadcast_id']
 assert len(s.state()['messages'])==2
 with pytest.raises(Exception,match='request_conflict'):
  s.broadcast({**payload,'body':'改发另一条'})

def test_parallel_dependencies_wait_for_every_parent(tmp_path):
 s=CollaborationStore(tmp_path/'c.sqlite')
 s.dependency({'task_id':'review','depends_on':['first','second'],'status':'waiting'})
 s.dependency({'task_id':'first','depends_on':'first','status':'completed'})
 assert not s.ready()['ready_tasks']
 s.dependency({'task_id':'second','depends_on':'second','status':'failed'})
 assert not s.ready()['ready_tasks']
 s.dependency({'task_id':'second','depends_on':'second','status':'completed'})
 assert set(s.ready()['ready_tasks'][0]['depends_on'])=={'first','second'}

def test_new_work_does_not_inherit_unrelated_unscoped_broadcasts(tmp_path):
 s=CollaborationStore(tmp_path/'messages.sqlite')
 for req,recipient,task,body in [
     ('old','agent-a',None,'Unrelated old sales rules'),
     ('current','agent-a','logical-image','Image task instruction'),
     ('other','agent-b','logical-image','Other recipient instruction'),
     ('direct','run-image',None,'Directly addressed to this run'),
     ('previous','agent-a','run-before','Prior handoff context')]:
  s.send({'request_id':req,'sender':'planner','recipient':recipient,'task_id':task,'body':body})
 messages=s.for_task('run-image','agent-a',['logical-image','run-before'])
 assert [m['request_id'] for m in messages]==['current','direct','previous']
 assert len(s.list()['messages'])==5
 assert s.for_task('run-unrelated','agent-a')==[]
