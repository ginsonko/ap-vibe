import {useEffect,useRef,useState} from 'react';
import {MessageContent} from './MessageContent';

export function ParticipationSwitch({scope='global',target='',label='自动 Agent 协作',onChanged}) {
  const [settings,setSettings]=useState(null),[busy,setBusy]=useState(false),[error,setError]=useState('');
  const pending=useRef(null);
  useEffect(()=>{const controller=new AbortController();setSettings(null);setError('');
    fetch('/v1/ap-vibe/studio/participation?'+new URLSearchParams({scope,target}),{signal:controller.signal}).then(async r=>{const v=await r.json();if(!r.ok||!v.settings)throw new Error('协作设置暂时无法读取，请刷新后重试。');return v;}).then(v=>{if(!controller.signal.aborted)setSettings(v.settings);}).catch(e=>{if(!controller.signal.aborted)setError(e.message);});
    return()=>controller.abort();},[scope,target]);
  async function change(enabled,paused=settings.paused) {
    const payload={scope,target,enabled,paused,expected_revision:settings.revision};
    const signature=JSON.stringify(payload);if(pending.current?.signature!==signature)pending.current={signature,id:crypto.randomUUID()};
    setBusy(true);setError('');
    try{const r=await fetch('/v1/ap-vibe/studio/participation',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...payload,request_id:pending.current.id})});const v=await r.json();if(!r.ok||v.ok===false)throw new Error(v.error?.message||'设置未保存');setSettings(v.settings);pending.current=null;onChanged?.();}
    catch(e){setError(e.message);}finally{setBusy(false);}
  }
  return <div className="studio-participation">
    <label>{scope==='global'?<input type="checkbox" checked={!!settings?.enabled} disabled={!settings||busy} onChange={e=>change(e.target.checked)}/>:null}<b>{label}</b>
      {scope!=='global'&&<select disabled={!settings||busy} value={settings?.enabled===null?'inherit':String(!!settings?.enabled)} onChange={e=>change(e.target.value==='inherit'?null:e.target.value==='true')}><option value="inherit">跟随上级设置</option><option value="true">开启协作</option><option value="false">专注工作 · 不参与协作</option></select>}
    </label>
    {scope==='global'&&settings&&<button className="text-action" disabled={busy} onClick={()=>change(settings.enabled,!settings.paused)}>{settings.paused?'恢复全部协作':'暂停全部协作'}</button>}
    <small>{!settings&&!error?'正在读取设置…':scope==='global'?'开启后，合适的工作可交给伙伴；关闭后停止新增自动分工，已运行的任务继续。项目和会话可以单独设置。':'仅影响自动分工和主动提醒；项目资料与公开进展始终可以查询。'}</small>
    {settings?.paused&&<p>全部自动协作已暂停。</p>}{error&&<p role="alert">{error}</p>}
  </div>;
}

export function OrdinarySessionDetails({actor}) {
  const [snapshot,setSnapshot]=useState(null),[error,setError]=useState(''),[loading,setLoading]=useState(false);
  const scroll=useRef(null),following=useRef(true);
  useEffect(()=>{let timer;const controller=new AbortController();setSnapshot(null);setError('');following.current=true;
    if(!actor.source_id)return()=>controller.abort();
    async function read(){try{const r=await fetch('/v1/ap-vibe/sessions/read?'+new URLSearchParams({source_id:actor.source_id,limit:20}),{signal:controller.signal});const v=await r.json();if(!r.ok||v.ok===false)throw new Error(v.error?.message||'正在重连');if(!controller.signal.aborted){setSnapshot(old=>old&& !following.current?old:v);setError('');}}catch(e){if(!controller.signal.aborted)setError(e.message);}if(!controller.signal.aborted)timer=setTimeout(read,5000);}
    read();return()=>{controller.abort();clearTimeout(timer);};},[actor.source_id]);
  useEffect(()=>{if(following.current&&scroll.current)scroll.current.scrollTop=scroll.current.scrollHeight;},[snapshot]);
  async function older(){setLoading(true);following.current=false;try{const r=await fetch('/v1/ap-vibe/sessions/read?'+new URLSearchParams({source_id:actor.source_id,before:snapshot.history_before,limit:20}));const v=await r.json();if(!r.ok||v.ok===false)throw new Error('暂时无法读取更早消息');const el=scroll.current,oldHeight=el?.scrollHeight||0;setSnapshot(old=>({...v,events:[...v.events,...old.events]}));requestAnimationFrame(()=>{if(el)el.scrollTop=el.scrollHeight-oldHeight;});}catch(e){setError(e.message);}finally{setLoading(false);}}
  return <div>
    <p>{actor.harness==='codex'?'Codex':'Claude Code'} · {actor.collaboration?.enabled?'参与协作':'专注工作'} · {actor.activity_label}</p>
    <ParticipationSwitch scope="session" target={actor.actor_id} label="这条会话的协作方式"/>
    {snapshot?.history_before!=null&&snapshot?.has_older&&<button onClick={older} disabled={loading}>{loading?'正在读取…':'查看更早消息'}</button>}
    <div className="ordinary-session-messages" ref={scroll} onScroll={()=>{const el=scroll.current;following.current=el.scrollHeight-el.clientHeight-el.scrollTop<48;}}>
      {(snapshot?.events||[]).map(m=><article key={m.id||m.offset}><small>{{assistant:'伙伴',user:'用户',tool:'工具'}[m.role]||m.role} · {m.timestamp?new Date(m.timestamp).toLocaleTimeString('zh-CN'):''}</small><MessageContent text={m.text}/></article>)}
      {!snapshot&&<p>{actor.source_id?'正在读取最新公开消息…':'公开记录尚未被扫描到，会话已在工作室登记。'}</p>}
    </div>
    <button className="text-action" onClick={()=>{following.current=true;if(scroll.current)scroll.current.scrollTop=scroll.current.scrollHeight;}}>回到最新消息</button>
    {error&&<p role="status">{error}，保留已读取内容。</p>}
    <small>留言会进入此会话的工作室收件箱，由下一次 Skill 读取。记录被发现不代表可随时中断原终端。</small>
  </div>;
}
