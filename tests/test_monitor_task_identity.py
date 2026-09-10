from copy import deepcopy
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from ap_mind.monitor_projection import group_task_sources


def source(project, key, session='same-task', count=100, classification='unclassified'):
    messages=[{'role':'assistant','text':str(i),'timestamp':f'2026-09-10T00:{i//60:02}:{i%60:02}Z','message_id':key+str(i)} for i in range(count)]
    return {'project_id':project,'project_name':project,'source_key':key,'session_id':session,
            'title':'Identical title','classification':classification,'messages':messages,'active':True,
            'message_count':count,'last_activity_at':messages[-1]['timestamp'],
            'last_activity_role':'assistant','last_activity_text':messages[-1]['text'],'last_error':None}


def test_same_task_across_roots_keeps_history_and_confirmed_identity():
    first=source('confirmed-project','a',classification='confirmed')
    second=source('temporary-workspace','b',count=150)
    second['last_error']={'code':'old-file-missing'}
    inputs=[second,first];before=deepcopy(inputs)
    tasks=group_task_sources(inputs,80)
    assert len(tasks)==1
    task=tasks[0]
    assert task['project_id']=='confirmed-project'
    assert task['message_count']==150 and len(task['messages'])==80
    assert task['last_activity_text']=='149'
    assert task['project_ids']==['confirmed-project','temporary-workspace']
    assert len(task['source_projects'])==2 and task['source_errors'][0]['error']['code']=='old-file-missing'
    task['messages'][0]['text']='caller modification'
    assert inputs==before


def test_same_title_different_ids_or_unknown_ids_are_separate():
    inputs=[source('p','a','task-a'),source('p','b','task-b'),source('p','c',None),source('q','d',None)]
    assert len(group_task_sources(inputs,80))==4
    # A project-filtered caller passes only its sources; no cross-project read.
    assert group_task_sources([inputs[0]],80)[0]['project_ids']==['p']


def test_repeated_text_at_different_times_survives_and_timezone_sort_is_chronological():
    a=source('p','a',count=1);b=source('p','b',count=1)
    a['messages'][0]['timestamp']='2026-09-10T09:00:00+08:00'
    b['messages'][0]['timestamp']='2026-09-10T02:00:00Z'
    for item in [a,b]:item['last_activity_at']=item['messages'][0]['timestamp']
    result=group_task_sources([a,b],80)[0]
    assert result['message_count']==2
    assert result['messages'][-1]['message_id']=='b0'
