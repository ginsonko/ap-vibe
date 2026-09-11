import {useEffect,useRef,useState} from 'react';

async function request(path, body, signal){
  const controller=new AbortController();
  const cancel=()=>controller.abort();
  signal?.addEventListener('abort',cancel,{once:true});
  if(signal?.aborted)controller.abort();
  const timeout=setTimeout(cancel,20000);
  try{
    const response=await fetch('/v1/ap-vibe/agents/setup'+path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined,signal:controller.signal});
    const value=await response.json().catch(()=>null);
    if(!response.ok||value?.ok!==true){
      const error=new Error(value?.error?.message||'工作台暂时没有返回完整结果，请重试。');
      error.code=value?.error?.code;
      error.status=response.status;
      // Only a structured rejection establishes that this write was refused.
      error.rejected=!!error.code&&response.status>=400&&response.status<500&&response.status!==408;
      throw error;
    }
    return value;
  }finally{clearTimeout(timeout);signal?.removeEventListener('abort',cancel);}
}

export function AgentOnboarding({agents,onSaved}){
  const [catalog,setCatalog]=useState(null),[selected,setSelected]=useState([]),[busy,setBusy]=useState(false),[message,setMessage]=useState('');
  const [catalogState,setCatalogState]=useState('loading'),[catalogAttempt,setCatalogAttempt]=useState(0),[refreshNeeded,setRefreshNeeded]=useState(false),[refreshing,setRefreshing]=useState(false);
  const [key,setKey]=useState(''),[url,setUrl]=useState('https://api.yinziapi.top/v1'),[mode,setMode]=useState('key'),[targets,setTargets]=useState([]);
  const pending=useRef(null),saving=useRef(false),selectionInitialized=useRef(false);
  const apiAgents=agents.filter(a=>!a.archived&&a.auth_mode!=='local_login');
  useEffect(()=>{
    const controller=new AbortController();setCatalogState('loading');
    request('',undefined,controller.signal).then(v=>{
      if(controller.signal.aborted)return;
      setCatalog(v);setSelected(v.templates.filter(t=>t.recommended).map(t=>t.template_id));setCatalogState('ready');
    }).catch(e=>{if(!controller.signal.aborted)setCatalogState(e.status===404?'unavailable':'error');});
    return()=>controller.abort();
  },[catalogAttempt]);
  useEffect(()=>{
    if(!selectionInitialized.current&&apiAgents.length){selectionInitialized.current=true;setTargets(apiAgents.map(a=>a.agent_id));}
    else setTargets(old=>old.filter(id=>apiAgents.some(a=>a.agent_id===id)));
  },[apiAgents.map(a=>a.agent_id).join(',')]);
  async function refresh(){
    setRefreshing(true);
    try{await onSaved();setRefreshNeeded(false);}
    catch{setRefreshNeeded(true);}
    finally{setRefreshing(false);}
  }
  async function save(path,payload){
    if(saving.current)return;
    // Polling may already see a committed write after its response was lost.
    // Keep the original revisions and request id when retrying the same intent.
    const intent={...payload,...(payload.agents?{agents:payload.agents.map(a=>a.agent_id).sort()}:{})};
    const signature=JSON.stringify([path,intent]);
    if(pending.current?.signature!==signature)pending.current={signature,body:{...payload,request_id:crypto.randomUUID()}};
    saving.current=true;setBusy(true);setMessage('');setRefreshNeeded(false);
    try{
      const result=await request(path,pending.current.body);
      pending.current=null;setKey('');
      setMessage(`已保存 ${result.agents.length} 位伙伴，${result.skipped.length} 位保持原配置。${result.manager_setup?.configured?'芙芙已接入管理办公室，填入 Key 后按协作设置工作。':''}保存不会调用模型。`);
      await refresh();
    }catch(e){
      if(e.rejected)pending.current=null;
      if(e.code==='agent_revision_conflict'||e.code==='agent_setup_target_unavailable'){
        setMessage('伙伴配置已发生变化，本次整批没有写入。你的输入已保留，请刷新伙伴列表后再保存。');setRefreshNeeded(true);
      }else setMessage(e.rejected?e.message:'保存结果暂未确认，你的输入已保留。再次保存会核对同一次请求，避免重复写入。');
    }finally{saving.current=false;setBusy(false);}
  }
  if(!catalog)return <section className="page-section agent-onboarding" aria-label="伙伴配置引导" aria-busy={catalogState==='loading'}><h2>先选伙伴，再填连接</h2><p role="status">{catalogState==='loading'?'正在加载伙伴模板…':catalogState==='unavailable'?'当前服务尚未提供模板接口。你仍可用“添加伙伴”单独配置。':'伙伴模板暂时没有加载成功。请重试，也可以用“添加伙伴”单独配置。'}</p>{catalogState!=='loading'&&<button className="secondary-button" onClick={()=>setCatalogAttempt(v=>v+1)}>重新加载模板</button>}</section>;
  return <section className="page-section agent-onboarding"><h2>先选伙伴，再填连接</h2><p>模板已填写服务地址、模型与职责。填入 Key 后即可启用；也支持其它服务，缺少连接的伙伴会在宿舍等你。</p>
    <fieldset disabled={busy||refreshing} style={{border:0,padding:0,margin:0,minWidth:0}}>
    <details open={!agents.some(a=>!a.archived)}><summary>添加角色模板</summary>
      <div className="setup-template-grid">{catalog.templates.map(t=><label key={t.template_id}><input type="checkbox" checked={selected.includes(t.template_id)} onChange={e=>setSelected(old=>e.target.checked?[...old,t.template_id]:old.filter(id=>id!==t.template_id))}/><strong>{t.name}</strong><small>{t.model||'本机 Codex 登录'}</small><p>{t.role}</p><small>{t.template_evidence}</small></label>)}</div>
      <button className="primary-button" disabled={busy||!selected.length} onClick={()=>save('/templates',{template_ids:selected})}>添加所选模板</button><p className="muted">已有模板不会重复添加或覆盖；模板不含 Key。</p>
    </details>
    {!!apiAgents.length&&<details><summary>快速配置多个伙伴</summary><p>银子 API 智能 Key 可一次填给所选伙伴；使用其它服务时，也可只统一 URL，再分别编辑各伙伴的 Key 和模型。</p>
      <div className="agent-actions"><button type="button" onClick={()=>setTargets(apiAgents.map(a=>a.agent_id))}>全选伙伴</button><button type="button" onClick={()=>setTargets(apiAgents.filter(a=>!targets.includes(a.agent_id)).map(a=>a.agent_id))}>反选</button></div>
      <div className="setup-targets">{apiAgents.map(a=><label key={a.agent_id}><input type="checkbox" checked={targets.includes(a.agent_id)} onChange={e=>setTargets(old=>e.target.checked?[...old,a.agent_id]:old.filter(id=>id!==a.agent_id))}/>{a.name}</label>)}</div>
      <label>本次要填写<select value={mode} onChange={e=>setMode(e.target.value)}><option value="key">一份 Key 填给所选伙伴</option><option value="url">统一服务 URL，保留各自 Key</option><option value="both">统一 URL 和 Key</option></select></label>
      {mode!=='key'&&<label>服务 URL<input type="url" value={url} onChange={e=>setUrl(e.target.value)} placeholder="https://api.yinziapi.top/v1"/></label>}
      {mode!=='url'&&<label>API Key<input type="password" autoComplete="off" value={key} onChange={e=>setKey(e.target.value)} placeholder="仅本次保存时填入，不在页面回显"/></label>}
      <button className="primary-button" disabled={busy||!targets.length||(mode!=='url'&&!key.trim())||(mode!=='key'&&!url.trim())} onClick={()=>save('/connections',{agents:apiAgents.filter(a=>targets.includes(a.agent_id)).map(a=>({agent_id:a.agent_id,expected_revision:a.revision})),...(mode!=='key'?{base_url:url.trim()}:{}),...(mode!=='url'?{api_key:key.trim()}:{})})}>{busy?'正在保存…':`保存到 ${targets.length} 位伙伴`}</button>
      <p className="muted">银子 API 推荐：创建智能 Key，分组倍率上限设为 2、全选分组，并设置便于管理的额度。这些是建议，不影响其它服务正常使用。希望分别控制消耗时，可给每位伙伴配置独立 Key。</p>
    </details>}</fieldset>{message&&<p role="status">{message}</p>}{refreshNeeded&&<div role="status"><p>伙伴列表尚未同步。输入会保留，刷新列表不会重新保存配置。</p><button className="secondary-button" disabled={busy||refreshing} onClick={refresh}>{refreshing?'正在刷新…':'刷新伙伴列表'}</button></div>}
  </section>;
}
