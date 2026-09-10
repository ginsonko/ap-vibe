import {useEffect, useRef, useState} from 'react';
import {MessageContent} from './MessageContent';

async function read(query='', signal) {
  const response = await fetch('/v1/ap-vibe/agents/claude-sessions'+query,{signal});
  const data = await response.json();
  if (!response.ok || data.ok===false) throw new Error('会话记录暂时无法读取，已保留当前内容并自动重连。');
  return data;
}
const merge = (left,right) => [...new Map([...left,...right].map(x=>[x.id+':'+x.offset,x])).values()].sort((a,b)=>a.offset-b.offset);

export function ClaudeSessions() {
  const [catalog,setCatalog]=useState(null), [selected,setSelected]=useState('');
  const [events,setEvents]=useState([]),[error,setError]=useState(''),[listError,setListError]=useState('');
  const [older,setOlder]=useState(null),[loading,setLoading]=useState(false),[ready,setReady]=useState(false);
  const [boundary,setBoundary]=useState('');
  const generation=useRef(''),current=useRef(''),scroll=useRef(null),following=useRef(true),output=useRef(null),restoreScroll=useRef(null);
  current.current=selected;
  const source=catalog?.sources.find(s=>s.source_id===selected);
  useEffect(()=>{
    const controller=new AbortController();let timer;
    async function poll(){
      try {const data=await read('',controller.signal);if(controller.signal.aborted)return;setCatalog(data);setListError('');setSelected(old=>old||data.sources[0]?.source_id||'');}
      catch(e){if(!controller.signal.aborted)setListError('Claude会话列表暂未更新，已有来源保留。');}
      if(!controller.signal.aborted)timer=setTimeout(poll,4000);
    }
    poll();return()=>{controller.abort();clearTimeout(timer);};
  },[]);
  useEffect(()=>{
    const controller=new AbortController();let timer,cursor=null;
    generation.current='';following.current=true;restoreScroll.current=null;setEvents([]);setOlder(null);setError('');setReady(false);setBoundary('');
    if(!selected)return()=>controller.abort();
    async function poll(){
      try{
        const query=new URLSearchParams({source_id:selected});
        if(cursor!==null){query.set('after',cursor);query.set('generation',generation.current);}
        const data=await read('?'+query,controller.signal);if(controller.signal.aborted)return;
        if(cursor===null||data.reset){setEvents(data.events);setOlder(data.has_older?data.history_before:null);following.current=true;}
        else if(data.events.length)setEvents(old=>merge(old,data.events));
        generation.current=data.generation;cursor=data.cursor;setError('');setReady(true);
        if(data.invalid_lines)setBoundary('有不完整、过长或无法解析的记录未展示；可见消息继续读取。');
        if(data.reset)setBoundary('源记录已截断或更换，已重新读取当前会话，未拼接旧文件内容。');
        timer=setTimeout(poll,data.has_more?100:1800);
      }catch(e){if(!controller.signal.aborted){setError(e.message);timer=setTimeout(poll,4000);}}
    }
    poll();return()=>{controller.abort();clearTimeout(timer);};
  },[selected]);
  useEffect(()=>{
    const el=scroll.current;if(!el)return;
    if(restoreScroll.current){el.scrollTop=restoreScroll.current.top+(el.scrollHeight-restoreScroll.current.height);restoreScroll.current=null;}
    else if(following.current)el.scrollTop=el.scrollHeight;
  },[events]);
  async function history(){
    if(older===null||loading)return;
    const id=selected,gen=generation.current;setLoading(true);
    try{
      const data=await read('?'+new URLSearchParams({source_id:id,before:older,generation:gen}));
      if(current.current!==id||generation.current!==gen)return;
      if(data.reset){setBoundary('源记录已变化，正在重新同步，请稍后读取历史。');return;}
      if(scroll.current)restoreScroll.current={height:scroll.current.scrollHeight,top:scroll.current.scrollTop};
      following.current=false;setEvents(old=>merge(data.events,old));setOlder(data.has_older?data.history_before:null);setError('');
    }catch(e){if(current.current===id)setError(e.message);}
    finally{setLoading(false);}
  }
  function choose(id){setSelected(id);if(window.matchMedia('(max-width:1050px)').matches)requestAnimationFrame(()=>output.current?.scrollIntoView({block:'start',behavior:'auto'}));}
  return <div>
    <p className="agent-notice">这里自动读取本机普通 Claude Code 会话的公开记录。标记“近期有输出”表示记录最近更新，不保证任务仍在运行；已有终端暂时只监看。</p>
    {listError&&<p role="status" className="agent-notice">{listError}</p>}
    {catalog?.warnings.map((w,i)=><p role="status" key={i}>{w}</p>)}
    {!catalog?<p aria-busy="true">正在发现 Claude 会话…</p>:!catalog.sources.length?<section className="page-section"><h2>暂未发现普通 Claude 会话</h2><p>在这台电脑上使用 Claude Code 开始任务后，这里会自动出现。工作室创建的托管任务仍在“任务与输出”中查看。</p><p>使用自定义 Claude 配置目录时，可在本机 data_dir/claude-monitor.json 的 roots 中填写对应 projects 目录；无需移动原会话。</p></section>:<div className="agent-main">
      <section className="page-section"><h2>Claude 会话 · {catalog.sources.length}</h2><p className="muted">按记录更新时间排序{catalog.has_more_sources?'，当前展示最近一批':''}</p><div className="agent-run-list">{catalog.sources.map(s=><button key={s.source_id} className={selected===s.source_id?'selected':''} onClick={()=>choose(s.source_id)}><b>{s.title}</b><small>{s.title_source==='claude_title'?'Claude 标题':s.title_source==='first_visible_message'?'首条可见消息摘要':'尚无标题'} · {s.activity==='recent_output'?'近期有输出':'历史记录'}</small><small>{s.project_membership?`${s.project_membership.display_name}${s.project_membership.status==='archived'?'（已归档）':''}`:'未归类 · 长期任务使用 Skill 时自动整理'}</small><small>{new Date(s.updated_at*1000).toLocaleString('zh-CN')}</small></button>)}</div></section>
      <section ref={output} style={{scrollMarginTop:100}} className="page-section agent-output"><h2>{source?.title||'Claude 会话'}</h2><p className="muted">只读监看 · {source?.session_id}</p><p>{source?.project_membership?`所属项目：${source.project_membership.display_name}`:'尚未归类：一次性问答无需建档，长期任务由 Claude 按实际资料归类。'}</p>{source?.cwd&&<details><summary>来自哪个工作目录</summary><code className="agent-path">{source.cwd}</code></details>}
        {error&&<p role="status" className="agent-notice">{error}</p>}{boundary&&<p className="muted">{boundary}</p>}
        <button disabled={older===null||loading} onClick={history}>{loading?'正在读取历史…':older===null?'已到当前可读记录开头':'向上读取更早消息'}</button>
        <div className="agent-timeline" ref={scroll} onScroll={()=>{const el=scroll.current;following.current=el.scrollHeight-el.scrollTop-el.clientHeight<50;}}>{events.map(e=><article key={e.id+':'+e.offset} className={'agent-event '+e.kind}><small>{e.timestamp?new Date(e.timestamp).toLocaleString('zh-CN'):'时间未记录'} · {e.kind==='user'?'用户':e.kind==='assistant'?'Claude':'工具'}{e.sidechain?' · 子任务':''}</small><MessageContent text={e.text} annotations={false}/></article>)}{!events.length&&<p>{ready?'当前记录窗口中没有公开文字；隐藏推理和附件内容不会显示。':'正在读取公开消息…'}</p>}</div>
        <button className="text-action" onClick={()=>{following.current=true;if(scroll.current)scroll.current.scrollTop=scroll.current.scrollHeight;}}>回到最新消息</button>
      </section>
    </div>}
  </div>;
}
