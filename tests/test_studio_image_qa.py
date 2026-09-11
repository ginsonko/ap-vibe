import base64
import json
from pathlib import Path
import pytest

from test_agent_studio import studio,profile
from ap_mind.studio_image_qa import StudioImageQA
from ap_mind.studio_vision import parse_results
from ap_mind.contracts import ContractError

PNG=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aD2sAAAAASUVORK5CYII=')

def batch(studio,tmp_path,count=4,**kw):
    path=tmp_path/'source.png';path.write_bytes(PNG)
    a=profile(studio)['agent_id']
    raw={'request_id':'create','title':'产品图片','project_id':'test-project','agent_id':a,
         'sample_percent':0,'batch_size':4,'items':[{'id':str(i),'product_id':'p','path':str(path),'requirements':'完整且清晰'} for i in range(count)],**kw}
    value=studio.image_qa.create(raw)
    studio.image_qa.action({'request_id':'start','batch_id':value['batch_id'],'expected_revision':1,'action':'start'})
    return value,raw,path

def verdicts(work,verdict='passed'):
    return [{'id':i['id'],'verdict':verdict,'reason':'实际像素测试执行器给出的模拟结论'} for i in work['items']]


def test_4000_items_checkpoint_restart_no_replay_and_paged_complete(studio,tmp_path):
    created,raw,_=batch(studio,tmp_path,4000,batch_size=100)
    q=studio.image_qa;identity=created['batch_id']
    assert q.create(raw)['replayed']
    first=q.reserve(identity);q.settle(first,verdicts(first),{})
    q.settle(first,verdicts(first),{})
    orphan=q.reserve(identity)
    with studio.registry.transaction():
        studio.registry._connect().execute("UPDATE studio_image_requests SET state='submitted' WHERE request_id=?",(orphan['request_id'],))
    reopened=StudioImageQA(studio)
    assert reopened.list(identity)['batches'][0]['counts']=={'passed':100,'pending':3800,'uncertain':100}
    seen=set(orphan['items'][i]['id'] for i in range(100))
    while work:=reopened.reserve(identity):
        assert not seen.intersection(v['id'] for v in work['items'])
        seen.update(v['id'] for v in work['items'])
        reopened.settle(work,verdicts(work),{})
    state=reopened.list(identity)['batches'][0]
    assert state['counts']=={'passed':3900,'uncertain':100} and state['state']=='needs_attention'
    items=[];offset=0
    while offset is not None:
        page=reopened.list(identity,offset,200);items.extend(page['items']);offset=page['next_offset']
    assert len(items)==4000 and len(items[0]['history'])==1
    assert reopened.reserve(identity) is None


def test_missing_duplicate_or_invented_results_never_pass():
    for values in ([],[{'id':'a','verdict':'passed','reason':'ok'}]*2,[{'id':'z','verdict':'passed','reason':'ok'}]):
        with pytest.raises(ContractError):parse_results(json.dumps({'results':values}),['a','b'])
    with pytest.raises(ContractError):parse_results('{"results":[{"id":"a","verdict":"passed","reason":""}]}',['a'])


def test_uncertain_review_and_disagreement_keep_both_versions(studio,tmp_path):
    reviewer=profile(studio,name='reviewer')['agent_id']
    created,_,_=batch(studio,tmp_path,2,reviewer_agent_id=reviewer,sample_percent=100)
    q=studio.image_qa;work=q.reserve(created['batch_id'])
    q.settle(work,verdicts(work),{})
    review=q.reserve(created['batch_id']);assert review['phase']=='review'
    q.settle(review,verdicts(review,'rejected'),{})
    values=q.list(created['batch_id'])['items']
    assert all(v['state']=='needs_review' and len(v['history'])==2 for v in values)


def test_changed_file_never_sent_and_cancel_does_not_revive(studio,tmp_path):
    created,_,path=batch(studio,tmp_path)
    q=studio.image_qa;work=q.reserve(created['batch_id']);path.write_bytes(PNG+b'change')
    calls=[];q.executor=lambda *a:calls.append(a)
    q._run(work)
    assert not calls and q.list(created['batch_id'])['batches'][0]['counts']=={'error':4}
    q.action({'request_id':'cancel','batch_id':created['batch_id'],'expected_revision':2,'action':'cancel'})
    assert q.reserve(created['batch_id']) is None
    q.settle(work,verdicts(work),{})
    assert q.list(created['batch_id'])['batches'][0]['state']=='cancelled'


def test_missing_picture_is_excluded_without_failing_other_items(studio,tmp_path):
    a=profile(studio)['agent_id'];paths=[tmp_path/(str(i)+'.png') for i in range(2)]
    for p in paths:p.write_bytes(PNG)
    v=studio.image_qa.create({'request_id':'mixed','title':'mixed','project_id':'test-project','agent_id':a,
        'items':[{'id':str(i),'product_id':'p','path':str(p),'requirements':'clear'} for i,p in enumerate(paths)]})
    studio.image_qa.action({'request_id':'start-mixed','batch_id':v['batch_id'],'expected_revision':1,'action':'start'})
    work=studio.image_qa.reserve(v['batch_id']);paths[0].unlink()
    calls=[]
    def executor(profile,key,parts,identity):
        calls.append(parts)
        return {'text':json.dumps({'results':[{'id':'1','verdict':'passed','reason':'visible'}]}),'usage':{'prompt_tokens':10,'completion_tokens':5}}
    studio.image_qa.executor=executor;studio.image_qa._run(work)
    result=studio.image_qa.list(v['batch_id'])
    assert [i['state'] for i in result['items']]==['error','passed']
    assert len(calls)==1 and sum(p['type']=='image_url' for p in calls[0])==1
    assert result['requests'][0]['item_ids']==['1']
    assert result['requests'][0]['skipped_before_submission']==['0']


def test_actual_executor_receipt_usage_and_timeout_unknown(studio,tmp_path):
    created,raw,_=batch(studio,tmp_path,8)
    q=studio.image_qa;work=q.reserve(created['batch_id']);calls=[]
    def execute(profile,key,parts,identity):
        calls.append(identity)
        assert sum(p['type']=='image_url' for p in parts)==4
        return {'text':json.dumps({'results':verdicts(work)}),'usage':{'prompt_tokens':100,'completion_tokens':50},'protocol':'openai','elapsed_seconds':1.2,'provider_request_id':'provider-test'}
    q.executor=execute;q._run(work)
    assert studio.budget.status(raw['agent_id'])['tokens_used']==150
    second=q.reserve(created['batch_id']);q.executor=lambda *a:(_ for _ in ()).throw(TimeoutError('response unknown'))
    q._run(second)
    status=q.list(created['batch_id'])['batches'][0]
    assert status['counts']=={'passed':4,'uncertain':4}
    assert len(calls)==1 and q.reserve(created['batch_id']) is None


def test_pause_and_restart_release_only_unsubmitted_images(studio,tmp_path):
    created,_,_=batch(studio,tmp_path,8)
    q=studio.image_qa;identity=created['batch_id'];work=q.reserve(identity)
    q.action({'request_id':'pause','batch_id':identity,'expected_revision':2,'action':'pause'})
    calls=[];q.executor=lambda *args:calls.append(args);q._run(work)
    assert not calls and q.list(identity)['batches'][0]['counts']=={'pending':8}
    assert q.list(identity)['requests'][0]['state']=='not_submitted'
    q.action({'request_id':'resume','batch_id':identity,'expected_revision':3,'action':'resume'})
    first=q.reserve(identity);second=q.reserve(identity)
    with studio.registry.transaction():
        studio.registry._connect().execute("UPDATE studio_image_requests SET state='submitted' WHERE request_id=?",(second['request_id'],))
    restarted=StudioImageQA(studio)
    assert restarted.list(identity)['batches'][0]['counts']=={'pending':4,'uncertain':4}
    assert restarted.reserve(identity)['items'][0]['id']==first['items'][0]['id']


def test_shared_reference_once_and_manual_adjudication_preserves_model(studio,tmp_path):
    from ap_mind.studio_vision import content
    created,_,path=batch(studio,tmp_path,2)
    q=studio.image_qa;identity=created['batch_id'];work=q.reserve(identity)
    for item in work['items']:item['reference']=item['image']
    parts=content(work['items'])
    assert len([p for p in parts if p['type']=='image_url'])==3
    q.settle(work,verdicts(work,'uncertain'),{})
    item=q.list(identity)['items'][0]
    raw={'request_id':'human','batch_id':identity,'item_id':item['id'],'expected_version':item['version'],
        'verdict':'passed','reason':'人工打开原图，核对完整瓶盖与500mL文字。'}
    assert q.resolve(raw)['paid_request'] is False and q.resolve(raw)['replayed']
    updated=q.list(identity)['items'][0]
    assert updated['state']=='passed' and len(updated['history'])==2
    assert updated['history'][0]['verdict']=='uncertain' and updated['history'][1]['phase']=='manual'
    with pytest.raises(ContractError,match='changed_refresh'):
        q.resolve({**raw,'request_id':'stale'})


def test_page_types_and_nonexistent_file_are_recoverable(studio,tmp_path):
    created,_,_=batch(studio,tmp_path)
    for offset,limit in [('0',50),(0,None),(False,50)]:
        with pytest.raises(ContractError,match='image_page_invalid'):studio.image_qa.list(created['batch_id'],offset,limit)


def test_manual_last_item_closes_batch_and_preview_binds_hash(studio,tmp_path):
    created,_,path=batch(studio,tmp_path,1,project_id=None)
    q=studio.image_qa;identity=created['batch_id'];work=q.reserve(identity)
    q.fail(work,'uncertain','渠道没有返回结果')
    assert q.reserve(identity) is None
    item=q.list(identity)['items'][0]
    assert item['history'][-1]['model']==work['profile']['model']
    data,mime=q.image(identity,item['id']);assert data==PNG and mime=='image/png'
    q.resolve({'request_id':'last-human','batch_id':identity,'item_id':item['id'],'expected_version':item['version'],
        'verdict':'passed','reason':'人工核对已登记的原图和参考要求'})
    assert q.list(identity)['batches'][0]['state']=='completed'
    path.write_bytes(PNG+b'changed')
    with pytest.raises(ContractError,match='changed_after_registration'):q.image(identity,item['id'])


def test_responses_vision_uses_image_blocks_and_reports_real_usage(monkeypatch):
    from ap_mind import studio_vision
    from io import BytesIO
    calls=[]
    def endpoint(req,**kwargs):
        calls.append(req)
        response=BytesIO(json.dumps({'id':'resp-fixture','output':[{'type':'message','content':[{'type':'output_text','text':'test visible response'}]}],
            'usage':{'input_tokens':20,'output_tokens':3}}).encode())
        response.headers={}
        return response
    monkeypatch.setattr(studio_vision.request,'urlopen',endpoint)
    result=studio_vision.execute({'protocol':'responses','base_url':'https://example.invalid/v1','model':'test'},'fixture-only',
        [{'type':'text','text':'check image'},{'type':'image_url','image_url':{'url':'data:image/png;base64,AA=='}}],'r1')
    assert calls[0].full_url.endswith('/v1/responses')
    assert json.loads(calls[0].data)['input'][0]['content'][1]['type']=='input_image'
    assert result['text']=='test visible response' and result['usage']['input_tokens']==20
