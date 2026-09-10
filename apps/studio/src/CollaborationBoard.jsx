import { useEffect, useRef, useState } from 'react';
const STATES = {waiting:'等待上游',starting:'启动中',running:'执行中',awaiting_review:'成果待验收',completed:'已验收',failed:'未完成',uncertain:'结果待核对',interrupted:'已中断',cancelled:'已取消',changes_requested:'需要修改'};
const ACTIVE = new Set(['waiting','starting','running']);

function MessageComposer({runs,agents,nameOf}) {
  const [target,setTarget]=useState(''),[body,setBody]=useState(''),[notice,setNotice]=useState(''),[busy,setBusy]=useState(false);
  const [general,setGeneral]=useState(false),[recipients,setRecipients]=useState([]);
  const pending=useRef(null), selected=runs.find(r=>r.run_id===target);
  const ordered=[...runs].sort((a,b)=>Number(ACTIVE.has(b.state))-Number(ACTIVE.has(a.state)));
  useEffect(()=>{setTarget(old=>old||runs.find(r=>ACTIVE.has(r.state))?.run_id||'');},[runs]);
  async function send(event) {
    event.preventDefault(); if(busy||!body.trim()||(general?!recipients.length:!selected))return;
    const payload=general?{sender:'工作台',recipients,body:body.trim()}:{sender:'工作台',recipient:selected.agent_id,task_id:selected.run_id,body:body.trim()};
    const fingerprint=JSON.stringify(payload);
    if(pending.current?.fingerprint!==fingerprint)pending.current={fingerprint,id:crypto.randomUUID()};
    setBusy(true);setNotice('');
    try {
      const response=await fetch('/v1/ap-vibe/collaboration/'+(general?'broadcast':'message'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...payload,request_id:pending.current.id})});
      const result=await response.json();if(!response.ok||result.ok===false)throw Error(result.error?.message||'消息未确认保存，请重试。');
      setNotice(general?'留言已保存，可在消息目录按需查阅。':ACTIVE.has(selected.state)?'已保存到这项任务。伙伴下一次调用 AP-Vibe 工具时会收到；是否采用请查看后续成果。':'消息已保存，继续这项任务时可读取。任务没有被自动启动。');
      setBody('');pending.current=null;
    } catch(error) {setNotice(error.message+' 内容与请求编号已保留，可以重试。');}
    finally{setBusy(false);}
  }
  return <div className="collaboration-compose">
    <h3>给任务补充信息</h3><p className="muted">发现了遗漏、找到资料或需要提醒伙伴？选对任务，直接告诉它。正在执行的任务排在前面。</p>
    <form onSubmit={send}>
      {!general&&<label>关联哪项任务<select value={target} onChange={e=>setTarget(e.target.value)} disabled={busy}><option value="">请选择一项任务</option>{ordered.map(r=><option key={r.run_id} value={r.run_id}>{STATES[r.state]||r.state} · {r.name} · {(r.task_snapshot?.title||r.prompt||'').slice(0,70)}</option>)}</select></label>}
      {!general&&selected&&<p className="muted">收件伙伴：{nameOf(selected.agent_id)}。{ACTIVE.has(selected.state)?'新消息随下一次 AP-Vibe 工具返回递送；等待模型响应时不会被强行打断。':'该任务已停止；消息留待后续接续，不会自动启动或收费。'}</p>}
      {!general&&!runs.length&&<p>还没有托管任务。先在“任务与输出”安排一项工作，再给它补充信息。</p>}
      <details open={general} onToggle={e=>{if(!busy)setGeneral(e.currentTarget.open);}}><summary>给多位伙伴留一条普通留言</summary>
        <p className="muted">普通留言保存在协作目录供按需查看，不会自动加入这些伙伴今后的无关任务。要影响正在进行的工作，请收起这里并选择具体任务。</p>
        <div className="collaboration-recipients">{agents.filter(a=>!a.archived).map(a=><label key={a.agent_id}><input type="checkbox" disabled={busy} checked={recipients.includes(a.agent_id)} onChange={e=>setRecipients(old=>e.target.checked?[...old,a.agent_id]:old.filter(id=>id!==a.agent_id))}/>{a.name}</label>)}</div>
      </details>
      <label>补充内容<textarea disabled={busy} rows={3} value={body} onChange={e=>setBody(e.target.value)} placeholder="例如：我找到了接口说明，在项目 docs/api.md。金额单位是分，验收时请核对，不要自行猜测币种。"/></label>
      <button className="primary-button" disabled={busy||!body.trim()||(general?!recipients.length:!selected)}>{busy?'正在保存…':general?'保存普通留言':'发送给这项任务'}</button>
      {notice&&<p role="status" className="agent-notice">{notice}</p>}
    </form>
  </div>;
}

export function CollaborationBoard({collab, runs, nameOf, openRun, agents=[]}) {
  const chains=runs.filter(r=>r.depends_on);
  const task=id=>runs.find(r=>r.run_id===id);
  return <section className="page-section">
    <h2>伙伴怎样一起完成任务</h2>
    <p className="muted">给后一项任务选择“先等哪项任务完成”。上游正常返回后，后一位伙伴会自动开始，并收到真实成果目录。这里可以查看每一步、工作消息和交接记录。</p>
    <MessageComposer runs={runs} agents={agents} nameOf={nameOf}/>
    <div className="agent-summary">
      <article><strong>{chains.filter(r=>r.state==='waiting').length}</strong><span>等待上游的任务</span></article>
      <article><strong>{chains.filter(r=>['starting','running'].includes(r.state)).length}</strong><span>已唤醒，正在执行</span></article>
      <article><strong>{collab?.messages?.length||0}</strong><span>近期公开工作消息</span></article>
    </div>
    <h3>任务交接顺序</h3>
    {chains.map(r=><article className="agent-event" key={r.run_id}>
      <div className="agent-actions">{[r.depends_on].flat().map(id=><button key={id} onClick={()=>openRun(id)}>{task(id)?.name||'上游任务'} · {STATES[task(id)?.state]||'历史记录'}</button>)}<span aria-label="然后">→</span><button onClick={()=>openRun(r.run_id)}>{r.name} · {STATES[r.state]||r.state}</button></div>
      <p>{r.prompt.slice(0,180)}</p>
      {r.state==='waiting' && [r.depends_on].flat().some(id=>['uncertain','failed','interrupted','cancelled','changes_requested'].includes(task(id)?.state)) && <p className="agent-notice">有上游未正常完成，后续任务保持等待。可打开任务查看已有成果，或取消这项等待。</p>}
    </article>)}
    {!chains.length && <p>还没有相互依赖的任务。例：让资料员写一份说明，再让核验员检查；安排核验时选择资料员的任务作为上游。</p>}
    <h3>公开工作消息</h3>
    <div className="collaboration-history" tabIndex={0} role="region" aria-label="公开工作消息历史">
    {(collab?.messages||[]).map(m=><article className="agent-event assistant" key={m.message_id}><small>{nameOf(m.sender)} → {nameOf(m.recipient)} · {new Date(m.created_at).toLocaleString('zh-CN')}</small><p style={{whiteSpace:'pre-wrap'}}>{m.body}</p>{task(m.task_id) && <button onClick={()=>openRun(m.task_id)}>查看关联任务</button>}</article>)}
    {!collab?.messages?.length && <p>伙伴会在需要时用 AP-Vibe 协作工具发送工作消息；这里仅展示公开输出。</p>}
    </div>
    {!!collab?.handoffs?.length && <><h3>接手记录</h3>{collab.handoffs.map(h=><article key={h.handoff_id} className="agent-event"><small>{nameOf(h.from_agent)} → {nameOf(h.to_agent)} · 交接已登记</small><p>{h.note}</p><button onClick={()=>openRun(h.task_id)}>查看原任务和成果</button></article>)}</>}
  </section>;
}
