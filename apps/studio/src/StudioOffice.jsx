import { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowSquareOut, ArrowsInSimple, ArrowsOutSimple, Briefcase, Buildings, ChatCircleDots, ListBullets, MagnifyingGlass, PaperPlaneTilt, PencilSimple, Users } from '@phosphor-icons/react';
import { AgentPortrait, useReducedMotion } from './AgentAppearance';
import { OfficeActors } from './OfficeActors';
import { OfficeSceneSVG } from './OfficeSceneSVG';
import floorConfig from './office-floors.json';
import { LAYOUT_V2,mapPageCount,workstationSlots } from './OfficeLayout';
import { ParticipationSwitch,OrdinarySessionDetails } from './StudioParticipation';
import {StudioManagerPanel} from './StudioManagerPanel';
import {StudioReplay} from './StudioReplay';
import {OfficeViewport} from './OfficeViewport';

// Build ROOMS from layout v2 zones, keeping unique room_ids
const ROOMS = (() => {
  const roomMap = new Map();
  for (const zone of LAYOUT_V2.zones) {
    if (!roomMap.has(zone.room_id)) {
      roomMap.set(zone.room_id, {
        id: zone.room_id,
        name: zone.display_name,
        detail: zone.name,
        color: zone.color
      });
    }
  }
  return Array.from(roomMap.values());
})();
const roomById = Object.fromEntries(ROOMS.map(r => [r.id, r]));
const FLOOR_THEMES = floorConfig.floors;
const localDate=v=>v?new Date(v).toLocaleString('zh-CN'):'尚无记录';
const modelLabel=profile=>profile?.model||((profile?.executor_kind||profile?.harness)==='codex'?'沿用 Codex 当前模型':profile?.manager?'仅本地调度':'模型尚未记录');

export function officeMembers(agents,runs,projectId='',query=''){
  const groups=new Map(agents.filter(a=>!a.archived).map(a=>[a.agent_id,{agent:a,runs:[]} ]));
  for(const run of runs){
    if(!groups.has(run.agent_id)&&run.active)groups.set(run.agent_id,{agent:{...run,archived:true},runs:[]});
    if(!projectId||run.project_id===projectId)groups.get(run.agent_id)?.runs.push(run);
  }
  return [...groups.values()].flatMap(member=>{
    member.runs.sort((a,b)=>Number(b.active)-Number(a.active)||String(b.updated_at||b.created_at).localeCompare(String(a.updated_at||a.created_at)));
    const activeRuns=member.runs.filter(r=>r.active);
    const current=member.runs[0];
    if(activeRuns.length)return activeRuns.map((r,i)=>({...member,agent:{...member.agent,profile_id:member.agent.agent_id,
      agent_id:member.agent.agent_id+':'+r.run_id,name:member.agent.name+(activeRuns.length>1?' · '+(i+1):'')},current:r,runs:[r],room:r.room||'rest',active:1}));
    if(member.agent.activated===false)return [{...member,current:{...current,state:'inactive',animation:'rest',activity_label:'未激活 · 等待配置'},room:'rest',active:0}];
    return [{...member,current,room:current?.room||'rest',active:activeRuns.length}];
  }).filter(m=>(!projectId||m.runs.length)&&(!query||[m.agent.name,m.agent.model,m.agent.role].some(s=>s?.toLowerCase().includes(query.toLowerCase()))))
    .sort((a,b)=>b.active-a.active||a.agent.name.localeCompare(b.agent.name,'zh-CN'));
}

export function StudioOffice({agents,projects,openRun,arrange,editAgent,openTasks,openMessages}){
  const [snapshot,setSnapshot]=useState(null),[error,setError]=useState('');
  const [view,setView]=useState(()=>window.innerWidth<700?'list':'map'),[projectId,setProjectId]=useState(''),[query,setQuery]=useState(''),[room,setRoom]=useState('');
  const [selected,setSelected]=useState(''),[selectedRun,setSelectedRun]=useState(''),[motion,setMotion]=useState(true);
  const [expanded,setExpanded]=useState(false),[mapPage,setMapPage]=useState(0);
  const [includeHistory,setIncludeHistory]=useState(false);
  const [replaying,setReplaying]=useState(false),[managerOpen,setManagerOpen]=useState(false);
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
  const members=useMemo(()=>{
    const managed=officeMembers(agents.filter(a=>a.agent_id!==snapshot?.manager_actor?.agent_id),snapshot?.runs||[],projectId,query);
    const ordinary=(snapshot?.ordinary?.actors||[]).filter(a=>(!projectId||a.project_id===projectId)&&(!query||[a.name,a.summary,a.harness].some(t=>t?.toLowerCase().includes(query.toLowerCase())))).map(a=>({agent:{...a,agent_id:a.actor_id},current:a,ordinary:a,runs:[],room:a.room,active:Number(a.active)}));
    const special=[snapshot?.manager_actor,...(snapshot?.batch_actors||[])].filter(Boolean).filter(a=>!projectId||!a.project_id||a.project_id===projectId).map(a=>({agent:{...a,profile_id:a.agent_id,agent_id:a.actor_id},current:a,runs:[],room:a.room,active:Number(a.active),special:a.manager?'manager':'batch'}));
    return [...managed,...ordinary,...special].sort((a,b)=>b.active-a.active||a.agent.name.localeCompare(b.agent.name,'zh-CN'));
  },[agents,snapshot,projectId,query]);
  const currentMembers=members.filter(m=>!m.ordinary||m.active||includeHistory||Date.now()-new Date(m.ordinary.modified_at||m.ordinary.updated_at).getTime()<86400000);
  const displayed=currentMembers.filter(m=>!room||m.room===room);
  const pages=Math.max(FLOOR_THEMES.length,mapPageCount(displayed)),page=Math.min(mapPage,pages-1);
  const visibleCount=workstationSlots(displayed,page).length;
  const member=members.find(m=>m.agent.agent_id===selected)||null;
  const run=member?.runs.find(r=>r.run_id===selectedRun)||member?.current;
  const profile=run?{...member.agent,...run}:member?.agent;
  function choose(m){setSelected(m.agent.agent_id);setSelectedRun(m.current?.run_id||'');setNotice('');setBody('');requestRef.current=null;
    if(window.innerWidth<1500)requestAnimationFrame(()=>details.current?.scrollIntoView({block:'nearest',behavior:reduced?'auto':'smooth'}));}
  async function send(){
    if(!body.trim()||!member)return;
    const payload={sender:'工作台',recipient:member.agent.profile_id||member.agent.agent_id,body:body.trim(),...(member.ordinary?{task_id:member.ordinary.actor_id}:run?.active?{task_id:run.run_id}:{})};
    const identity=JSON.stringify(payload);
    if(requestRef.current?.identity!==identity)requestRef.current={identity,id:crypto.randomUUID()};
    setSending(true);setNotice('');
    try{const r=await fetch('/v1/ap-vibe/collaboration/message',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...payload,request_id:requestRef.current.id})});const result=await r.json();if(!r.ok||result.ok===false)throw new Error(result.error?.message||'发送未完成，可以重试同一条消息。');setNotice('消息已保存并提供给伙伴，尚不能确认已读。');setBody('');requestRef.current=null;}
    catch(e){setNotice(e.message);}finally{setSending(false);}
  }
  return <section className={'studio-office'+(expanded?' office-expanded':'')} aria-label="像素工作室">
    <div className="office-heading"><div><h2>伙伴们的工作现场</h2><p><span className={'office-signal'+(error?' offline':'')}/>{error||(snapshot?'现场已连接':'正在读取现场…')}{snapshot&&<span> · 更新于 {new Date(snapshot.observed_at).toLocaleTimeString('zh-CN')}</span>}</p></div><div className="office-commands"><button onClick={openTasks}><Briefcase size={18}/>任务池</button><button onClick={openMessages}><ChatCircleDots size={18}/>工作消息</button><button aria-label={expanded?'收起工作室':'展开工作室'} title={expanded?'收起工作室':'展开工作室'} onClick={()=>setExpanded(old=>!old)}>{expanded?<ArrowsInSimple size={18}/>:<ArrowsOutSimple size={18}/>}</button></div></div>
    <ParticipationSwitch/>
    <div className="office-story-actions"><button onClick={()=>setReplaying(true)}>▷ 故事回放</button><button onClick={()=>setManagerOpen(!managerOpen)}>{managerOpen?'收起管理办公室':'管理办公室 · '+(snapshot?.manager_actor?.name||'芙芙')}</button><small>查看协作经过，或管理伙伴的协调安排</small></div>
    {managerOpen&&<StudioManagerPanel agents={agents} openRun={openRun}/>}
    {projectId&&<ParticipationSwitch key={projectId} scope="project" target={projectId} label="当前项目协作"/>}
    <div className="office-toolbar"><label className="office-search"><MagnifyingGlass size={18}/><input aria-label="搜索伙伴" placeholder="搜索名字、模型或职责" value={query} onChange={e=>setQuery(e.target.value)}/></label>
      <select aria-label="筛选工作室项目" value={projectId} onChange={e=>{setProjectId(e.target.value);setSelected('');}}><option value="">所有项目</option>{projects.map(p=><option key={p.project_id} value={p.project_id}>{p.display_name}</option>)}</select>
      <div className="office-segment" aria-label="工作室视图"><button aria-pressed={view==='map'} onClick={()=>setView('map')}><Buildings size={18}/>地图</button><button aria-pressed={view==='list'} onClick={()=>setView('list')}><ListBullets size={18}/>名单</button></div>
      <label className="office-motion"><input type="checkbox" checked={motion&&!reduced} disabled={reduced} onChange={e=>setMotion(e.target.checked)}/>动态{reduced?'（跟随系统关闭）':''}</label>
      <label className="office-motion"><input type="checkbox" checked={includeHistory} onChange={e=>{setIncludeHistory(e.target.checked);setMapPage(0);}}/>包含更早会话</label>
    </div>
    <div className="office-room-filters"><button aria-pressed={!room} onClick={()=>setRoom('')}>全部 <b>{currentMembers.length}</b></button>{ROOMS.map(r=><button key={r.id} style={{'--room-color':r.color}} aria-pressed={room===r.id} onClick={()=>setRoom(old=>old===r.id?'':r.id)}>{r.name} <b>{currentMembers.filter(m=>m.room===r.id).length}</b></button>)}</div>
    {!snapshot&&!error?<p role="status">正在读取伙伴和运行状态…</p>:<div className={'office-layout'+(!member?' office-layout-unselected':'')}>
      <div className="office-scene-column">
        {view==='map'?<><div className="office-floor-caption"><strong>{FLOOR_THEMES[page % FLOOR_THEMES.length].name}</strong><span>{FLOOR_THEMES[page % FLOOR_THEMES.length].description}</span></div><OfficeViewport fitHeight={expanded} className={!motion||reduced||error?'static':''}>
          <OfficeSceneSVG floor={FLOOR_THEMES[page % FLOOR_THEMES.length].id} />
          <OfficeActors page={page} members={displayed} messages={snapshot?.messages} selected={selected} choose={choose} enabled={motion&&!reduced} frozen={!!error} openMessages={openMessages}/>
        </OfficeViewport></>:<div className="office-member-list">{displayed.map(m=><button key={m.agent.agent_id} className={selected===m.agent.agent_id?'selected':''} onClick={()=>choose(m)}><AgentPortrait profile={m.agent}/><span><b>{m.agent.name}</b><small>{modelLabel(m.agent)}</small><span>{m.current?.title||m.current?.summary||m.agent.role||m.current?.prompt||'还没有安排工作'}</span></span><span className="office-member-state">{m.current?.activity_label||'空闲'}<small>{roomById[m.room]?.name}{m.active>1?' · '+m.active+'项活动任务':''}</small></span></button>)}</div>}
        {view==='map'&&pages>1&&<div className="office-pages"><button disabled={page===0} onClick={()=>setMapPage(page-1)}>上一层</button><span>第 {page+1} / {pages} 层 · 当前显示 {visibleCount} 位，其余保留在其它层和名单中</span><button disabled={page>=pages-1} onClick={()=>setMapPage(page+1)}>下一层</button></div>}
        {!displayed.length&&<div className="office-empty"><Users size={30}/><p>{agents.some(a=>!a.archived)?'没有匹配的伙伴，试试其他项目或关键词。':'先添加一位伙伴，再给它安排一件工作。'}</p></div>}
      </div>
      {member&&<aside className="office-details" ref={details} aria-label="伙伴现场详情">
        <button className="office-detail-close" onClick={()=>setSelected('')}>关闭详情，返回全景</button>
        {!member?<div className="office-detail-empty"><Users size={32}/><h3>选择一位伙伴</h3><p>查看它的当前任务、最近输出和成果。</p><button onClick={openTasks}><Briefcase size={18}/>打开任务池</button></div>:<>
          <div className="office-detail-identity"><AgentPortrait profile={profile} action={run?.animation||'idle'} animate={motion&&!error}/><div><h3>{member.agent.name}</h3><p>{modelLabel(profile)}</p><span>{member.active?`${member.active}项活动任务`:'当前没有活动任务'}</span></div></div>
          {member.ordinary&&<OrdinarySessionDetails key={member.ordinary.actor_id} actor={member.ordinary}/>}
          {!!member.runs.length&&<label>查看哪次运行<select value={run?.run_id||''} onChange={e=>setSelectedRun(e.target.value)}>{member.runs.map(r=><option key={r.run_id} value={r.run_id}>{r.activity_label} · {(r.title||r.prompt||'查看运行记录').slice(0,45)}</option>)}</select></label>}
          {member.special==='manager'&&<StudioManagerPanel agents={agents} openRun={openRun}/>}
          {member.special==='batch'&&<p>这位伙伴正在进行视觉检查。可让当前任务通过工作室工具读取逐项结论、查看进度或暂停，记录会保留在本机。</p>}
          {!member.ordinary&&!member.special&&<><p className="office-run-goal">{run?.prompt||member.agent.role||'还没有安排任务。'}</p>
          {run&&<><p className="office-evidence-label">{roomById[run.room]?.name} · {run.activity_label}</p><div className="office-observation"><small>最近公开事件 · {localDate(run.last_event?.created_at||run.updated_at)}</small><p>{run.last_event?.text||'尚未收到公开输出。'}</p></div></>}
          <div className="office-commands">{run?.run_id&&<button onClick={()=>openRun(run.run_id)}><ArrowSquareOut size={18}/>过程与成果</button>}<button onClick={()=>member.agent.activated===false?editAgent(agents.find(a=>a.agent_id===(member.agent.profile_id||member.agent.agent_id))||member.agent):arrange(member.agent.profile_id||member.agent.agent_id)} disabled={member.agent.archived||member.agent.management_reserved}><Briefcase size={18}/>{member.agent.activated===false?"补齐配置以激活":"安排工作"}</button><button title="编辑伙伴配置与外观" aria-label="编辑伙伴配置与外观" onClick={()=>editAgent(agents.find(a=>a.agent_id===(member.agent.profile_id||member.agent.agent_id))||member.agent)} disabled={member.agent.archived}><PencilSimple size={18}/></button></div></>}
          <div className="office-compose"><label>给伙伴留言<textarea rows={3} value={body} onChange={e=>setBody(e.target.value)} placeholder="例如：重点检查移动端，完成后把截图放进成果目录。"/></label><button disabled={sending||!body.trim()} onClick={send}><PaperPlaneTilt size={18}/>{sending?'发送中…':'发送工作消息'}</button>{notice&&<p role="status">{notice}</p>}</div>
        </>}
      </aside>}
    </div>}
    {replaying&&<StudioReplay close={()=>setReplaying(false)}/>}
  </section>;
}
