import {useEffect,useRef,useState} from 'react';
import {AppearancePicker} from './AgentAppearance';
import {useDialogFocus} from './useDialogFocus';

export function StudioManagerPanel({agents,openRun}){
  const [data,setData]=useState(null),[form,setForm]=useState(null),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const [connectionError,setConnectionError]=useState('');
  const pending=useRef(null),saving=useRef(false);
  const managerProfile=agents.find(a=>a.agent_id===data?.settings.agent_id);
  const dialog=useDialogFocus(()=>setForm(null),busy,!!form);
  useEffect(()=>{let alive=true,timer;async function poll(){try{const r=await fetch('/v1/ap-vibe/studio/manager');const v=await r.json();if(!r.ok||!v.settings)throw new Error('管理办公室正在连接，请稍后刷新。');if(alive){setData(v);setConnectionError('');}}catch(e){if(alive)setConnectionError(e.message);}if(alive)timer=setTimeout(poll,5000);}poll();return()=>{alive=false;clearTimeout(timer);};},[]);
  async function save(e){
    e.preventDefault();if(saving.current)return;
    const identity=JSON.stringify(form);
    if(pending.current?.identity!==identity)pending.current={identity,body:{...form,expected_revision:data.settings.revision,request_id:crypto.randomUUID(),agent_id:form.agent_id||null}};
    saving.current=true;setBusy(true);setError('');
    const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),20000);
    try{
      const r=await fetch('/v1/ap-vibe/studio/manager',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(pending.current.body),signal:controller.signal});
      const v=await r.json();
      if(!r.ok||!v.ok){
        if(v.error?.code&&r.status>=400&&r.status<500&&r.status!==408){pending.current=null;setError(v.error?.message||'设置未保存，请按提示重试。');return;}
        throw new Error('unconfirmed');
      }
      pending.current=null;setData(old=>({...old,settings:v.settings}));setForm(null);
    }catch{setError('保存结果暂未确认，你的设置已保留。再次保存会核对同一次请求。');}
    finally{clearTimeout(timeout);saving.current=false;setBusy(false);}
  }
  return <div className="manager-panel"><div className="agent-output-head"><div><span className="eyebrow">管理办公室</span><h3>{data?.settings.name||'芙芙'}在这里协调工作</h3></div>{data&&<button onClick={()=>setForm({...data.settings})}>配置{data.settings.name||'管理员'}</button>}</div>
    <p>管理员按工作计划协调分工与依赖，出现明确故障时安排接续。空闲时只在本地值守。配置名字、外观、人设与执行模型，让她按你的习惯协调。</p>
    {data&&!data.settings.agent_id&&<p>尚未选择管理模型，当前沿用本地任务调度。选择一位独立的管理伙伴，避免普通工程占用管理身份。</p>}
    {managerProfile?.activated===false&&<p role="status">管理伙伴尚未激活。请在伙伴配置中补齐服务 URL、模型和 Key；已选人设与形象会保留。</p>}
    {data?.incidents.map(i=><article className="manager-incident" key={i.incident_id}><small>{new Date(i.created_at).toLocaleString('zh-CN')} · {{registered:'事故已登记',deliberating:'管理伙伴判断中',proposed:'已有协调建议',applied:'已登记后续安排',fallback:'本地调度兜底',superseded:'已由新的任务安排替代',queued:'独立接续已排队',running:'伙伴正在接续',completed:'接续成果已验收',waiting_review:'接续成果待验收',needs_help:'接续需要帮助',held:'管理建议暂缓',waiting_candidates:'等待可用伙伴',unclassified:'等待项目归类'}[i.state]||i.state}</small><p>{i.decision?.message||i.decision?.reason||i.reason||'正在检查故障原因和接续范围。'}</p><small>{i.decision?.action==='fallback'?'决策来源：本地调度（管理未交付有效决定）':i.decision_source?'决策来源：管理模型':i.manager_run_id?'管理正在判断':'决策来源：本地调度'}{i.decision?.agent_id?' · 安排给：'+(agents.find(a=>a.agent_id===i.decision.agent_id)?.name||i.decision.agent_id):''}{i.decision?.action==='retry'?' · 恢复原伙伴':''}</small>{i.manager_run_id&&<button onClick={()=>openRun(i.manager_run_id)}>查看管理过程</button>}</article>)}
    {data&&!data.incidents.length&&<p className="muted">当前没有需要协调的事故，不消耗管理模型额度。</p>}{(error||connectionError)&&<p role="alert">{error||connectionError}</p>}
    {form&&<div className="agent-dialog-backdrop"><form ref={dialog} className="agent-dialog" role="dialog" aria-modal="true" aria-label="管理伙伴设置" onSubmit={save}><fieldset disabled={busy} style={{border:0,padding:0,margin:0,minWidth:0,display:"grid",gap:15}}><h2>管理伙伴设置</h2><label>名字<input value={form.name} onChange={e=>setForm({...form,name:e.target.value})}/></label><label>管理模型<select aria-label="管理模型" value={form.agent_id||''} onChange={e=>setForm({...form,agent_id:e.target.value})}><option value="">仅本地调度</option>{agents.filter(a=>!a.archived&&a.executor_kind==='claude').map(a=><option key={a.agent_id} value={a.agent_id}>{a.name} · {a.model}{a.activated===false?' · 未激活':''}</option>)}</select></label><div><h3>管理员的像素形象</h3><button className="secondary-button" type="button" onClick={()=>setForm({...form,appearance_id:'fufu-v1'})}>使用芙芙默认形象</button><AppearancePicker value={form.appearance_id} onChange={appearance_id=>setForm({...form,appearance_id})}/></div><label>可选人设<textarea rows={4} value={form.persona} onChange={e=>setForm({...form,persona:e.target.value})}/></label><label>协调模型<select aria-label="协调模型" value={String(form.enabled)} onChange={e=>setForm({...form,enabled:e.target.value==='true'})}><option value="true">按事件启用</option><option value="false">暂停管理模型，沿用本地调度</option></select></label><label>判断时限（秒）<input type="number" min="10" value={form.decision_timeout_seconds} onChange={e=>setForm({...form,decision_timeout_seconds:Number(e.target.value)})}/></label><small>读取简短事故包，可能产生API费用。达到时限后不等待迟到提案，按原候选继续。</small><div className="agent-actions"><button type="button" disabled={busy} onClick={()=>setForm(null)}>返回</button><button className="primary-button" disabled={busy}>{busy?'正在保存…':'保存设置'}</button></div>{(error||connectionError)&&<p role="alert">{error||connectionError}</p>}</fieldset></form></div>}
  </div>;
}
