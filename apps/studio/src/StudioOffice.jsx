import { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowSquareOut, ArrowsInSimple, ArrowsOutSimple, Briefcase, Buildings, ChatCircleDots, ListBullets, MagnifyingGlass, PaperPlaneTilt, PencilSimple, Users } from '@phosphor-icons/react';
import { AgentPortrait, useReducedMotion } from './AgentAppearance';
import { OfficeActors } from './OfficeActors';

const ROOMS=[{id:'planning',name:'协作办公室',detail:'启动与通用执行',color:'#367f75'},
  {id:'library',name:'资料查阅区',detail:'读取资料与检索',color:'#347ca0'},
  {id:'engineering',name:'工程办公区',detail:'编辑代码与文件',color:'#76649b'},
  {id:'review',name:'验收测试区',detail:'独立检查与待验收',color:'#ad6579'},
  {id:'waiting',name:'等候与支持',detail:'等待上游或需要帮助',color:'#9b781d'},
  {id:'rest',name:'休息区',detail:'当前没有活动任务',color:'#548667'}];
const roomById=Object.fromEntries(ROOMS.map(r=>[r.id,r]));
const localDate=v=>v?new Date(v).toLocaleString('zh-CN'):'尚无记录';

export function officeMembers(agents,runs,projectId='',query=''){
  const groups=new Map(agents.filter(a=>!a.archived).map(a=>[a.agent_id,{agent:a,runs:[]} ]));
  for(const run of runs){
    if(!groups.has(run.agent_id)&&run.active)groups.set(run.agent_id,{agent:{...run,archived:true},runs:[]});
    if(!projectId||run.project_id===projectId)groups.get(run.agent_id)?.runs.push(run);
  }
  return [...groups.values()].map(member=>{
    member.runs.sort((a,b)=>Number(b.active)-Number(a.active)||String(b.updated_at||b.created_at).localeCompare(String(a.updated_at||a.created_at)));
    const current=member.runs[0];
    return {...member,current,room:current?.room||'rest',active:member.runs.filter(r=>r.active).length};
  }).filter(m=>(!projectId||m.runs.length)&&(!query||[m.agent.name,m.agent.model,m.agent.role].some(s=>s?.toLowerCase().includes(query.toLowerCase()))))
    .sort((a,b)=>b.active-a.active||a.agent.name.localeCompare(b.agent.name,'zh-CN'));
}

export function StudioOffice({agents,projects,openRun,arrange,editAgent,openTasks,openMessages}){
  const [snapshot,setSnapshot]=useState(null),[error,setError]=useState('');
  const [view,setView]=useState(()=>window.innerWidth<700?'list':'map'),[projectId,setProjectId]=useState(''),[query,setQuery]=useState(''),[room,setRoom]=useState('');
  const [selected,setSelected]=useState(''),[selectedRun,setSelectedRun]=useState(''),[motion,setMotion]=useState(true);
  const [expanded,setExpanded]=useState(false);
  const [body,setBody]=useState(''),[sending,setSending]=useState(false),[notice,setNotice]=useState('');
  const requestRef=useRef(null),details=useRef(null),reduced=useReducedMotion();
  useEffect(()=>{
    const controller=new AbortController();let timer;
    async function poll(){
      try{if(!document.hidden){const r=await fetch('/v1/ap-vibe/agents/presence',{signal:controller.signal});const value=await r.json();if(!r.ok||value.ok===false)throw new Error('presence');if(!controller.signal.aborted){setSnapshot(value);setError('');}}}
      catch(e){if(!controller.signal.aborted)setError('连接恢复中，保留上次现场');}
      if(!controller.signal.aborted)timer=setTimeout(poll,4000);
    }
    poll();return()=>{controller.abort();clearTimeout(timer);};
  },[]);
  const members=useMemo(()=>officeMembers(agents,snapshot?.runs||[],projectId,query),[agents,snapshot,projectId,query]);
  const displayed=members.filter(m=>!room||m.room===room);
  const member=members.find(m=>m.agent.agent_id===selected)||null;
  const run=member?.runs.find(r=>r.run_id===selectedRun)||member?.current;
  const profile=run?{...member.agent,...run}:member?.agent;
  function choose(m){setSelected(m.agent.agent_id);setSelectedRun(m.current?.run_id||'');setNotice('');setBody('');requestRef.current=null;
    if(window.innerWidth<1500)requestAnimationFrame(()=>details.current?.scrollIntoView({block:'nearest',behavior:reduced?'auto':'smooth'}));}
  async function send(){
    if(!body.trim()||!member)return;
    const payload={sender:'工作台',recipient:member.agent.agent_id,body:body.trim(),...(run?.active?{task_id:run.run_id}:{})};
    const identity=JSON.stringify(payload);
    if(requestRef.current?.identity!==identity)requestRef.current={identity,id:crypto.randomUUID()};
    setSending(true);setNotice('');
    try{const r=await fetch('/v1/ap-vibe/collaboration/message',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...payload,request_id:requestRef.current.id})});const result=await r.json();if(!r.ok||result.ok===false)throw new Error(result.error?.message||'发送未完成，可以重试同一条消息。');setNotice('消息已保存并提供给伙伴，尚不能确认已读。');setBody('');requestRef.current=null;}
    catch(e){setNotice(e.message);}finally{setSending(false);}
  }
  return <section className={'studio-office'+(expanded?' office-expanded':'')} aria-label="像素工作室">
    <div className="office-heading"><div><h2>伙伴们的工作现场</h2><p><span className={'office-signal'+(error?' offline':'')}/>{error||(snapshot?'现场已连接':'正在读取现场…')}{snapshot&&<span> · 更新于 {new Date(snapshot.observed_at).toLocaleTimeString('zh-CN')}</span>}</p></div><div className="office-commands"><button onClick={openTasks}><Briefcase size={18}/>任务池</button><button onClick={openMessages}><ChatCircleDots size={18}/>工作消息</button><button aria-label={expanded?'收起工作室':'展开工作室'} title={expanded?'收起工作室':'展开工作室'} onClick={()=>setExpanded(old=>!old)}>{expanded?<ArrowsInSimple size={18}/>:<ArrowsOutSimple size={18}/>}</button></div></div>
    <div className="office-toolbar"><label className="office-search"><MagnifyingGlass size={18}/><input aria-label="搜索伙伴" placeholder="搜索名字、模型或职责" value={query} onChange={e=>setQuery(e.target.value)}/></label>
      <select aria-label="筛选工作室项目" value={projectId} onChange={e=>{setProjectId(e.target.value);setSelected('');}}><option value="">所有项目</option>{projects.map(p=><option key={p.project_id} value={p.project_id}>{p.display_name}</option>)}</select>
      <div className="office-segment" aria-label="工作室视图"><button aria-pressed={view==='map'} onClick={()=>setView('map')}><Buildings size={18}/>地图</button><button aria-pressed={view==='list'} onClick={()=>setView('list')}><ListBullets size={18}/>名单</button></div>
      <label className="office-motion"><input type="checkbox" checked={motion&&!reduced} disabled={reduced} onChange={e=>setMotion(e.target.checked)}/>动态{reduced?'（跟随系统关闭）':''}</label>
    </div>
    <div className="office-room-filters"><button aria-pressed={!room} onClick={()=>setRoom('')}>全部 <b>{members.length}</b></button>{ROOMS.map(r=><button key={r.id} style={{'--room-color':r.color}} aria-pressed={room===r.id} onClick={()=>setRoom(old=>old===r.id?'':r.id)}>{r.name} <b>{members.filter(m=>m.room===r.id).length}</b></button>)}</div>
    {!snapshot&&!error?<p role="status">正在读取伙伴和运行状态…</p>:<div className="office-layout">
      <div className="office-scene-column">
        {view==='map'?<div className="office-map-scroll" tabIndex={0} role="region" aria-label="可滚动的像素办公室"><div className={'office-world'+(!motion||reduced||error?' static':'')}>
          <img className="office-floor" src="/office-map.png" alt="六个房间连通的像素办公室" onError={e=>{e.target.style.visibility='hidden';}}/>
          {ROOMS.map((r,i)=>{const occupants=displayed.filter(m=>m.room===r.id);return <div key={r.id} className={'office-room-marker'+(room&&room!==r.id?' dimmed':'')} style={{left:(i%3)*320,top:i<3?0:340,'--room-color':r.color}}>
            <div className="office-room-title"><b>{r.name}</b>{occupants.length>4?<button className="office-more" onClick={()=>{setRoom(r.id);setView('list');}}>共 {occupants.length} 位 · 查看全部</button>:<span>{occupants.length} 位伙伴</span>}</div>
            {!occupants.length&&<span className="office-room-empty">{r.detail}</span>}
          </div>;})}
          <OfficeActors members={displayed} messages={snapshot?.messages} selected={selected} choose={choose} enabled={motion&&!reduced} frozen={!!error} openMessages={openMessages}/>
        </div></div>:<div className="office-member-list">{displayed.map(m=><button key={m.agent.agent_id} className={selected===m.agent.agent_id?'selected':''} onClick={()=>choose(m)}><AgentPortrait profile={m.agent}/><span><b>{m.agent.name}</b><small>{m.agent.model||'本机Codex模型'}</small><span>{m.current?.prompt||m.agent.role||'还没有安排工作'}</span></span><span className="office-member-state">{m.current?.activity_label||'空闲'}<small>{roomById[m.room]?.name}{m.active>1?' · '+m.active+'项活动任务':''}</small></span></button>)}</div>}
        {!displayed.length&&<div className="office-empty"><Users size={30}/><p>{agents.some(a=>!a.archived)?'没有匹配的伙伴，试试其他项目或关键词。':'先添加一位伙伴，再给它安排一件工作。'}</p></div>}
      </div>
      <aside className="office-details" ref={details} aria-label="伙伴现场详情">
        {!member?<div className="office-detail-empty"><Users size={32}/><h3>选择一位伙伴</h3><p>查看它的当前任务、最近输出和成果。</p><button onClick={openTasks}><Briefcase size={18}/>打开任务池</button></div>:<>
          <div className="office-detail-identity"><AgentPortrait profile={profile} action={run?.animation||'idle'} animate={motion&&!error&&!!run?.active}/><div><h3>{member.agent.name}</h3><p>{profile?.model||'本机Codex模型'}</p><span>{member.active?`${member.active}项活动任务`:'当前没有活动任务'}</span></div></div>
          {!!member.runs.length&&<label>查看哪次运行<select value={run?.run_id||''} onChange={e=>setSelectedRun(e.target.value)}>{member.runs.map(r=><option key={r.run_id} value={r.run_id}>{r.activity_label} · {r.prompt.slice(0,45)}</option>)}</select></label>}
          <p className="office-run-goal">{run?.prompt||member.agent.role||'还没有安排任务。'}</p>
          {run&&<><p className="office-evidence-label">{roomById[run.room]?.name} · {run.activity_label}</p><div className="office-observation"><small>最近公开事件 · {localDate(run.last_event?.created_at||run.updated_at)}</small><p>{run.last_event?.text||'尚未收到公开输出。'}</p></div></>}
          <div className="office-commands">{run&&<button onClick={()=>openRun(run.run_id)}><ArrowSquareOut size={18}/>过程与成果</button>}<button onClick={()=>arrange(member.agent.agent_id)} disabled={member.agent.archived}><Briefcase size={18}/>安排工作</button><button title="编辑伙伴配置与外观" aria-label="编辑伙伴配置与外观" onClick={()=>editAgent(member.agent)} disabled={member.agent.archived}><PencilSimple size={18}/></button></div>
          <div className="office-compose"><label>给伙伴留言<textarea rows={3} value={body} onChange={e=>setBody(e.target.value)} placeholder="例如：重点检查移动端，完成后把截图放进成果目录。"/></label><button disabled={sending||!body.trim()} onClick={send}><PaperPlaneTilt size={18}/>{sending?'发送中…':'发送工作消息'}</button>{notice&&<p role="status">{notice}</p>}</div>
        </>}
      </aside>
    </div>}
  </section>;
}
