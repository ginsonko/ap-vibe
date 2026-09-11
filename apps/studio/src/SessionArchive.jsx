import {useEffect, useRef, useState} from 'react';
import {SessionTimeline} from './SessionTimeline';
import {SessionComposer} from './SessionComposer';
import {HarnessSources} from './HarnessSources';
import './session-archive.css';

async function read(path, signal) {
  const timeout=AbortSignal.timeout(15000);
  const response=await fetch('/v1/ap-vibe/'+path,{signal:signal?AbortSignal.any([signal,timeout]):timeout});
  const value=await response.json();
  if(!response.ok || value.ok===false)throw new Error(value.error?.message || '记录暂时无法读取，请稍后刷新。');
  return value;
}
const mergeEvents=(left,right)=>[...new Map([...left,...right].map(e=>[e.offset+':'+e.id,e])).values()].sort((a,b)=>a.offset-b.offset);
const harnessName=h=>({codex:'Codex',claude:'Claude Code',opencode:'OpenCode',openclaw:'OpenClaw',pi:'PI CLI','pi-desktop':'PI Desktop',mimocode:'MiMo Code',zcode:'ZCode','ga-admin':'GenericAgent Admin',hermes:'Hermes',dsh:'DSH Desktop'}[h]||h);
const stateName=s=>({starting:'正在启动',running:'执行中',waiting:'等待依赖',awaiting_review:'成果待验收',completed:'已验收',uncertain:'执行中断 · 结果待核对',failed:'执行失败',interrupted:'已中断',cancelled:'已停止',cancelling:'正在停止',changes_requested:'需要修改',budget_paused:'等待投喂'}[s]||s);
const readableSource=source=>source?{...source,...(source.sources?.find(s=>s.source_id===source.source_id&&s.available)||source.sources?.find(s=>s.managed&&s.available)||source.sources?.find(s=>s.available))}:null;
const sameSession=(left,right)=>left?.harness===right?.harness&&left?.session_id===right?.session_id;
const sourceForRequest=source=>source?{...source,harness:source.harness||'codex',source_id:source.source_id||'codex-'+source.source_key,source_project_ids:source.source_project_ids||(source.project_id?[source.project_id]:[])}:null;
const readError=e=>e.name==='TimeoutError'?'读取时间较长，稍后会自动重试。':e.message||'记录暂时无法读取，请稍后刷新。';

export function PublicSessionReader({source, projects, onSource, draft, setDraft}) {
  const [events,setEvents]=useState([]),[generation,setGeneration]=useState('');
  const [metadata,setMetadata]=useState({});
  const [older,setOlder]=useState(null),[olderBusy,setOlderBusy]=useState(false);
  const [error,setError]=useState(''),[ready,setReady]=useState(false),[boundary,setBoundary]=useState('');
  const [retry,setRetry]=useState(0),[copied,setCopied]=useState(false);
  const epoch=useRef(null),position=useRef({generation:'',cursor:null});
  useEffect(()=>{
    const controller=new AbortController();epoch.current=controller;
    let timer;position.current={generation:'',cursor:null};setEvents([]);setMetadata({});setGeneration('');setOlder(null);setOlderBusy(false);setReady(false);setError('');setBoundary('');
    async function poll(){
      try{
        const params=new URLSearchParams({source_id:source.source_id,limit:40});
        const prior=position.current;
        if(prior.cursor!==null){params.set('after',prior.cursor);params.set('generation',prior.generation);}
        const value=await read('sessions/read?'+params,controller.signal);
        if(controller.signal.aborted)return;
        setMetadata(Object.fromEntries(['title','title_source','model','model_source','observed_models','state','managed','run_id','task_id','imported_from','parent_session_id','stale'].filter(key=>key in value).map(key=>[key,value[key]])));
        if(prior.cursor===null || value.reset){setEvents(value.events);setOlder(value.has_older?value.history_before:null);}
        else if(value.events.length)setEvents(old=>mergeEvents(old,value.events));
        position.current={generation:value.generation,cursor:value.cursor};setGeneration(value.generation);setReady(true);setError('');
        if(value.stale)setBoundary('原应用正在保存记录，暂时展示上一份完整内容，稍后自动更新。');
        else if(value.reset)setBoundary('记录已更新，已重新读取当前内容。');
        else if(value.invalid_lines)setBoundary('部分记录未写完整或无法解析；其余公开消息仍可阅读。');
        else setBoundary('');
        timer=setTimeout(poll,value.has_more?150:2500);
      }catch(e){if(!controller.signal.aborted){setError(source.available===false?'原始日志暂时不可读，任务目录仍然保留；文件恢复后可重新读取。':readError(e));setReady(true);timer=setTimeout(poll,10000);}}
    }
    poll();return()=>{controller.abort();clearTimeout(timer);};
  },[source.source_id,retry]);
  async function loadOlder(){
    if(older===null || olderBusy)return;
    const controller=epoch.current,gen=position.current.generation;
    setOlderBusy(true);
    try{
      let before=older,value,skipped=0;
      const deadline=performance.now()+10000;
      do {
        value=await read('sessions/read?'+new URLSearchParams({source_id:source.source_id,before,generation:gen,limit:40}),controller.signal);
        if(controller.signal.aborted || position.current.generation!==gen)return;
        if(value.reset || value.events.length || !value.has_older || value.history_before>=before)break;
        before=value.history_before;skipped++;
      } while(performance.now()<deadline);
      if(controller.signal.aborted || position.current.generation!==gen)return;
      if(value.reset){setRetry(n=>n+1);return;}
      setEvents(old=>mergeEvents(value.events,old));setOlder(value.has_older?value.history_before:null);setError('');
      if(skipped)setBoundary(value.events.length?'已略过没有公开对话的记录片段，较早消息已补入，阅读位置保持不变。':value.has_older?'这一段没有公开对话；已向前查找，可以继续读取更早消息。':'已查到文件开头，其他片段没有公开对话。');
    }catch(e){if(!controller.signal.aborted && position.current.generation===gen)setError(readError(e));}
    finally{if(!controller.signal.aborted)setOlderBusy(false);}
  }
  const projectNames=(source.source_project_ids||[]).map(id=>projects.find(p=>p.project_id===id)?.display_name||id);
  const display={...source,...metadata};
  const timeline={identity:source.source_id+':'+generation,session_id:source.session_id,project_id:source.project_id,
    messages:events.map(e=>({...e,message_id:source.source_id+':'+generation+':'+e.offset+':'+e.id}))};
  return <div className="session-detail archive-reader">
    <header className="session-detail-head"><span className="eyebrow">{harnessName(source.harness)} · 公开记录</span><h2>{display.title}</h2>{source.harness!=='codex'&&<p className="session-model">模型：<strong>{display.model||'尚未在公开回复中记录'}</strong>{display.model&&(display.model_source==='run_configuration'?' · 本次执行配置':' · 最近实际回复')}{display.observed_models?.length>1&&<span> · 已观察到 {display.observed_models.length} 个模型</span>}</p>}{display.managed&&<p><strong>工作室托管 · {stateName(display.state)}</strong> · {source.agent_name}<br/><small>{display.task_id} · {display.run_id}</small></p>}<p>{projectNames.length?projectNames.join(' / '):'尚未归类 · 查询无需先建项目'}</p><details><summary>任务编号与工作目录</summary><code>{source.session_id}</code><p className="agent-path">{source.cwd || '目录未记录'}</p>{source.harness==='claude'&&<p>{display.title_source==='studio_task'?'标题来自工作室逻辑任务。':display.title_source==='claude_title'?'标题来自 Claude Code 的任务名称。':display.title_source==='first_visible_message'?'尚未设置任务名称，暂用首条公开用户消息。':'日志没有记录任务名称，暂用任务编号。'}{display.managed?'托管任务保留本次执行的模型配置与公共工具记录。':'模型取自已读取回复的元数据，可能与当前待执行请求的配置不同。'}</p>}{source.sources?.length>1&&<><p>此任务保留 {source.sources.length} 条来源；项目名称只说明已登记的来源，不据此更改归属。</p><label className="archive-harness">阅读哪份记录<select value={source.source_id} onChange={e=>onSource({...source,...source.sources.find(s=>s.source_id===e.target.value)})}>{source.sources.map((s,i)=><option key={s.source_id} value={s.source_id}>记录 {i+1} · {projects.find(p=>p.project_id===s.project_id)?.display_name||s.project_id||'未归类'} · {s.available?'可读取':'暂不可读'}</option>)}</select></label></>}</details></header>
    <div className="archive-reader-status" role="status">{!ready?'正在读取最新公开消息…':error?<><span>{error}</span><button className="text-action" onClick={()=>setRetry(n=>n+1)}>重新读取</button></>:boundary||`已读取 ${events.length} 条公开消息；新消息会自动显示。`}</div>
    {display.imported_from&&<p className="session-model">历史由 {harnessName(display.imported_from.harness)} 导入 · 原会话 <code>{display.imported_from.session_id}</code>。后续在当前应用继续的内容仍保留在此记录中。</p>}
    {display.parent_session_id&&<p className="session-model">关联主会话：<code>{display.parent_session_id}</code></p>}
    <SessionTimeline session={timeline} assistantLabel={harnessName(source.harness)} onOlder={loadOlder} hasOlder={older!==null} olderLoading={!ready||olderBusy}/>
    {source.managed?<div className="archive-continue"><span>工作室托管任务：在 Agent 工作室的任务池查看依赖、接手和验收，公开输出会持续显示在这里。</span></div>:source.harness==='codex'&&source.session_id?<SessionComposer key={source.session_id} session={source} draft={draft} setDraft={setDraft}/>:<div className="archive-continue"><span>普通 {harnessName(source.harness)} 会话显示公开记录；在原应用继续后，可通过 AP-Vibe 读取项目、其它任务进展和收件箱。</span>{source.harness==='claude'&&<button className="secondary-button" onClick={async()=>{try{await navigator.clipboard.writeText('claude --resume '+JSON.stringify(source.session_id));setCopied(true);}catch{setError('剪贴板不可用，可在原终端使用 claude --resume '+source.session_id);}}}>{copied?'继续命令已复制':'复制继续命令'}</button>}</div>}
  </div>;
}

export function SessionArchive({projects=[],onBack,getDraft,changeDraft,mode='history',requestedSession=null,onSelectionChange}) {
  const [showSources,setShowSources]=useState(false);
  const [search,setSearch]=useState(''),[query,setQuery]=useState(''),[harness,setHarness]=useState('');
  const [harnessOptions,setHarnessOptions]=useState([{harness:'codex',name:'Codex'},{harness:'claude',name:'Claude Code'}]);
  const [offset,setOffset]=useState(0),[catalog,setCatalog]=useState(null),[selected,setSelected]=useState(()=>sourceForRequest(requestedSession));
  const [loading,setLoading]=useState(true),[error,setError]=useState(''),[refresh,setRefresh]=useState(0);
  const reader=useRef(null),requestedIdentity=useRef(null);
  const requestedSource=sourceForRequest(requestedSession);
  useEffect(()=>{
    if(!requestedSource?.source_id||requestedIdentity.current===requestedSource.source_id)return;
    requestedIdentity.current=requestedSource.source_id;setSelected(requestedSource);
  },[requestedSource?.source_id]);
  useEffect(()=>{onSelectionChange?.(selected);},[selected,onSelectionChange]);
  useEffect(()=>{
    const controller=new AbortController();let timer;
    async function load(){
      setLoading(true);
      try{
        const params=new URLSearchParams({offset,limit:20});if(query)params.set('query',query);if(harness)params.set('harness',harness);
        const value=await read('sessions?'+params,controller.signal);if(controller.signal.aborted)return;
        setCatalog(value);if(value.harnesses?.length)setHarnessOptions(value.harnesses);setError('');setSelected(old=>{
          if(!old)return readableSource(value.sessions.find(s=>s.available)||value.sessions[0]);
          const current=value.sessions.find(item=>sameSession(item,old));
          if(!current)return old;
          // Keep the chosen source and the mounted reader when the directory refreshes.
          return {...current,...current.sources?.find(item=>item.source_id===old.source_id)};
        });
      }catch(e){if(!controller.signal.aborted)setError(readError(e));}
      finally{if(!controller.signal.aborted)setLoading(false);}
      if(!controller.signal.aborted&&offset===0)timer=setTimeout(load,15000);
    }
    load();return()=>{controller.abort();clearTimeout(timer);};
  },[query,harness,offset,refresh]);
  const filter=(kind,value)=>{if(kind==='harness')setHarness(value);else setQuery(value);setOffset(0);setCatalog(null);setSelected(null);setError('');setLoading(true);setRefresh(n=>n+1);};
  const paginate=value=>{setOffset(value);setCatalog(null);setError('');setLoading(true);};
  const choose=source=>{setSelected(readableSource(source));if(matchMedia('(max-width:1050px)').matches)requestAnimationFrame(()=>reader.current?.scrollIntoView({block:'start',behavior:'auto'}));};
  return <div className="page-stack sessions-page archive-page">
    {showSources&&<HarnessSources onClose={()=>setShowSources(false)} onSaved={()=>setRefresh(n=>n+1)}/>}
    <div className="page-header"><div><span className="eyebrow">会话{mode==='history'?' · 历史查询':''}</span><h1>{mode==='history'?'更早的任务，也能接着读':'所有任务，在这里接着看'}</h1><p>来自各应用的任务按最近记录更新排列；选择任务就能看最新消息，也可以向上翻阅历史。</p></div>{onBack&&<button className="secondary-button" onClick={onBack}>返回近期监看</button>}</div>
    <div className="session-workspace"><aside className="session-browser archive-browser"><div className="session-browser-head"><strong>{catalog?`已发现 ${catalog.total} 条匹配任务`:'正在读取任务目录…'}</strong><button className="text-action" onClick={()=>setRefresh(n=>n+1)} disabled={loading}>刷新目录</button></div>
      <form className="archive-search" onSubmit={e=>{e.preventDefault();filter('query',search.trim());}}><label className="search-field"><input aria-label="查找任务" value={search} onChange={e=>setSearch(e.target.value)} placeholder="标题、模型、任务编号、工作目录"/></label><button className="secondary-button" type="submit">查找</button></form>
      <label className="archive-harness">来自哪个应用<select aria-label="会话应用筛选" value={harness} onChange={e=>filter('harness',e.target.value)}><option value="">全部应用</option>{harnessOptions.map(h=><option key={h.harness} value={h.harness}>仅 {h.name}</option>)}</select></label>
      <button className="text-action" onClick={()=>setShowSources(true)}>管理会话来源</button>
      {error&&<p role="status" className="agent-notice">{error} {catalog?'已保留本页成功读取的目录。':'可以点击刷新目录重试。'}</p>}
      <div className="session-browser-list" aria-busy={loading}>{catalog?.sessions.map(s=><button className={'session-card '+(sameSession(selected,s)?'selected':'')} key={s.harness+':'+(s.session_id||s.source_id)} onClick={()=>choose(s)}><small>{harnessName(s.harness)} · {s.managed?'工作室托管 · '+stateName(s.state):s.available?'可读取记录':'日志暂不可读'}</small><h3>{s.title}</h3>{s.harness!=='codex'&&<p className="session-model">{s.model||'模型尚未记录'}</p>}<p className="session-title-source">{s.modified_at?new Date(s.modified_at).toLocaleString('zh-CN'):'更新时间未记录'}</p></button>)}{!catalog?.sessions.length&&<p className="muted">{loading?'正在查找任务…':'没有匹配任务，换一个关键词或应用试试。'}</p>}</div>
      <div className="archive-pagination"><button className="secondary-button" disabled={offset===0||loading} onClick={()=>paginate(Math.max(0,offset-20))}>上一页</button><span>第 {Math.floor(offset/20)+1} 页</span><button className="secondary-button" disabled={catalog?.next_offset==null||loading} onClick={()=>paginate(catalog.next_offset)}>下一页</button></div>
      <details className="archive-coverage"><summary>当前目录包含哪些记录？</summary><p>自动发现已接入应用的原生会话，并包含工作室托管任务。托管任务显示已保存的真实执行状态；普通日志的更新时间不代表仍在执行。</p>{catalog?.coverage?.claude?.has_more_sources&&<p>Claude 发现目录当前有读取上限，尚不包含更早的全部文件。</p>}{catalog?.warnings?.map((w,i)=><p key={i}>{w}</p>)}</details>
    </aside><section ref={reader} className="session-detail-pane" aria-label="所选任务的对话">{selected?<PublicSessionReader key={selected.source_id} source={selected} projects={projects} onSource={setSelected} draft={getDraft(selected)} setDraft={value=>changeDraft(selected,value)}/>:<div className="page-section"><h2>{loading?'正在读取任务目录…':'选择一条任务'}</h2><p>左侧可搜索、翻页或切换应用。打开后先显示最新公开消息。</p></div>}</section></div>
  </div>;
}
