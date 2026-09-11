from test_agent_studio import studio
from ap_mind.studio_replay import StudioReplay


def scene(room='planning',state='running'):
    return {'runs':[{'run_id':'r1','agent_id':'a1','name':'伙伴甲','room':room,'state':state,
        'active':state=='running','animation':'work','activity_label':'正在工作','prompt':'写一个具体文件'}]}


def test_phase_changes_and_message_have_no_replay_side_effect(studio):
    journal=studio.replay
    journal.capture(scene());journal.capture(scene());journal.capture(scene('library'))
    raw={'sender':'a1','recipient':'a1','body':'请核对这份资料。','task_id':'r1'}
    journal.message(raw,'m1');journal.message(raw,'m1')
    first=journal.list(limit=2)
    assert [e['kind'] for e in first['events']]==['state','move'] and first['has_more']
    second=journal.list(after=first['next_cursor'])
    assert len(second['events'])==1 and second['events'][0]['body']==raw['body']
    assert second['events'][0]['sender_actor']['name']=='伙伴甲'
    assert studio.service.collaboration.list()['messages']==[]
    restarted=StudioReplay(studio);restarted.capture(scene('library'))
    assert len(restarted.list()['events'])==3


def test_replay_preserves_event_time_name_and_unknown_position(studio):
    studio.replay.capture(scene())
    updated=scene('review');updated['runs'][0]['name']='新名字'
    studio.replay.capture(updated)
    entries=studio.replay.list()['events']
    assert entries[0]['actor']['name']=='伙伴甲' and entries[0]['previous'] is None
    assert entries[1]['previous']['name']=='伙伴甲' and entries[1]['actor']['name']=='新名字'
    assert all(e['timing_basis']=='observed_transition' for e in entries)


def test_message_scope_cannot_replace_sender_with_recipient(studio):
    journal=studio.replay
    initial=scene()
    initial['runs'].extend([{**initial['runs'][0],'run_id':'r2','agent_id':'a2','name':'伙伴乙'},
        {**initial['runs'][0],'run_id':'r3','agent_id':'a2','name':'伙伴乙的另一个任务'}])
    journal.capture(initial)
    journal.message({'sender':'a1','recipient':'a2','task_id':'r2','body':'只向任务二交接'},'m-scope')
    events=journal.list(interactions_only=True)['events']
    assert len(events)==1
    assert events[0]['sender_actor']['run_id']=='r1'
    assert events[0]['recipient_actor']['run_id']=='r2'
