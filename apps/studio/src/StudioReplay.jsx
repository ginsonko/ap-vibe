import {useEffect,useMemo,useRef,useState} from 'react';
import {OfficeActors} from './OfficeActors';
import {OfficeSceneSVG} from './OfficeSceneSVG';
import {OfficeViewport} from './OfficeViewport';
import {MessageContent} from './MessageContent';
import {useDialogFocus} from './useDialogFocus';
import {useDocumentHidden,useReducedMotion} from './AgentAppearance';

const member=a=>({agent:{...a,agent_id:a.actor_id},current:a,runs:a.run_id?[{run_id:a.run_id}]:[],room:a.room||'planning',active:Number(a.active)});
const timeLabel=e=>new Date(e.created_at).toLocaleString('zh-CN');
const kindLabel=e=>e.kind==='message'?'工作交流':e.kind==='move'?'转移工位':'状态变化';
const label=e=>e.kind==='message'?`${e.sender_actor?.name||e.sender} → ${e.recipient_actor?.name||e.recipient}`:`${e.actor.name} · ${e.actor.activity_label||e.actor.state}`;

export function StudioReplay({close}){
  const hidden=useDocumentHidden(),reduced=useReducedMotion();
  const dialog=useDialogFocus(close),picture=useRef(null);
  const [events,setEvents]=useState([]),[cursor,setCursor]=useState(0),[more,setMore]=useState(false),[hours,setHours]=useState(24);
  const [loading,setLoading]=useState(false),[error,setError]=useState(''),[index,setIndex]=useState(0),[playing,setPlaying]=useState(false),[cast,setCast]=useState([]),[detail,setDetail]=useState(false),[speed,setSpeed]=useState(1);
  const [messagesOnly,setMessagesOnly]=useState(false),[privateView,setPrivateView]=useState(false),[saving,setSaving]=useState(false),[notice,setNotice]=useState('');
  const clips=useMemo(()=>events.filter(e=>e.kind==='message'||!messagesOnly&&(e.kind==='move'||e.previous&&e.previous.state!==e.actor?.state)),[events,messagesOnly]);
  const event=clips[index],request=useRef(0);
  async function load(after=0){
    const generation=++request.current;setLoading(true);setError('');
    try{
      const r=await fetch('/v1/ap-vibe/studio/replay?'+new URLSearchParams({after,limit:200,interactions_only:true,since:new Date(Date.now()-hours*3600000).toISOString()}));
      const v=await r.json();if(generation!==request.current)return;
      if(!r.ok||!v.ok)throw new Error('暂时无法读取历史，已有片段保留。');
      setEvents(old=>after?[...new Map([...old,...v.events].map(e=>[e.event_id,e])).values()]:v.events);
      setCursor(v.next_cursor);setMore(v.has_more);
    }catch(e){if(generation===request.current)setError(e.message);}
    finally{if(generation===request.current)setLoading(false);}
  }
  function jump(next){setPlaying(false);setDetail(false);setNotice('');setIndex(next);}
  useEffect(()=>{jump(0);setEvents([]);load();return()=>{request.current++;};},[hours]);
  useEffect(()=>{
    if(!event){setCast([]);return;}
    const people=event.kind==='message'?[event.sender_actor,event.recipient_actor].filter(Boolean):[event.actor];
    const initial=event.kind==='message'?people:[event.previous||event.actor];
    setCast([...new Map(initial.map(a=>[a.actor_id,member(a)])).values()]);
    const timer=setTimeout(()=>setCast([...new Map(people.map(a=>[a.actor_id,member(a)])).values()]),120);
    return()=>clearTimeout(timer);
  },[event]);
  useEffect(()=>{
    if(!playing||!event||hidden)return;
    const gap=index+1<clips.length?Math.max(0,Date.parse(clips[index+1].created_at)-Date.parse(event.created_at)):0;
    const timer=setTimeout(()=>{if(index+1<clips.length){setIndex(index+1);setDetail(false);}else setPlaying(false);},((event.kind==='message'?16000:10000)+Math.min(6000,gap/720))/speed);
    return()=>clearTimeout(timer);
  },[playing,index,event,clips,hidden,speed]);
  const publicCast=useMemo(()=>privateView?cast.map((m,i)=>({...m,agent:{...m.agent,name:`伙伴${i+1}`},current:{...m.current,summary:''}})):cast,[privateView,cast]);
  const message=event?.kind==='message'?{...event,sender:event.sender_actor?.actor_id||event.sender,recipient:event.recipient_actor?.actor_id||event.recipient,body:privateView?'已保存工作消息（内容已隐藏）':event.body}:null;
  async function savePicture(){
    setPlaying(false);setSaving(true);setError('');setNotice('');
    try{
      const {toPng}=await import('html-to-image');
      await document.fonts.ready;
      const url=await toPng(picture.current,{pixelRatio:2,backgroundColor:'#f6faf7',skipFonts:true,filter:node=>!node.hasAttribute?.('data-export-hide')});
      const link=document.createElement('a');link.href=url;link.download=`AP-Vibe-工作室故事-${index+1}.png`;link.click();
      setNotice(privateView?'图片已生成，已隐藏姓名和消息内容；下载位置由浏览器决定。':'图片已生成，包含当前可见姓名和消息；下载位置由浏览器决定。');
    }catch{setError('图片暂未生成，请稍后重试；也可以使用本机截图。');}
    finally{setSaving(false);}
  }
  return <div className="replay-backdrop"><section ref={dialog} className="replay-panel" role="dialog" aria-modal="true" aria-label="工作室故事回放">
    <div className="agent-output-head"><div><span className="eyebrow">工作室故事</span><h2>看看你离开后发生了什么</h2></div><button onClick={close}>返回实时现场</button></div>
    <p className="muted">漫长等待会折叠；选择倍速可加快走路、交流和片段切换。暂停后可查看原文，不会暂停后台任务。</p>
    <div className="replay-controls">
      <select aria-label="回放时间范围" value={hours} onChange={e=>setHours(Number(e.target.value))}><option value={1}>最近1小时</option><option value={24}>最近24小时</option><option value={168}>最近7天</option></select>
      <button disabled={!event} onClick={()=>setPlaying(!playing)}>{playing?'暂停回放':'播放故事'}</button>
      <label className="replay-speed">速度<select aria-label="回放播放速度" value={speed} onChange={e=>setSpeed(Number(e.target.value))}><option value={1}>正常</option><option value={2}>2 倍</option><option value={4}>4 倍</option><option value={8}>8 倍</option></select></label>
      <button disabled={index===0} onClick={()=>jump(index-1)}>上一幕</button><button disabled={index+1>=clips.length} onClick={()=>jump(index+1)}>下一幕</button>
      <label><input type="checkbox" checked={messagesOnly} onChange={e=>{setMessagesOnly(e.target.checked);jump(0);}}/>只看工作交流</label>
      {more&&<button disabled={loading} onClick={()=>load(cursor)}>载入更多历史</button>}
    </div>
    {!!clips.length&&<select className="replay-scene-picker" aria-label="跳到哪一幕" value={index} onChange={e=>jump(Number(e.target.value))}>
      {clips.map((e,i)=><option key={e.event_id} value={i}>{i+1} / {clips.length} · {timeLabel(e)} · {privateView?kindLabel(e):label(e).slice(0,90)}</option>)}
    </select>}
    <div className="replay-controls"><label><input type="checkbox" checked={privateView} onChange={e=>{setPrivateView(e.target.checked);setDetail(false);}}/>分享时隐藏姓名和消息</label><button disabled={!event||saving} onClick={savePicture}>{saving?'正在生成图片…':'保存这一幕图片'}</button><small>仅保存到本机，不上传。</small></div>
    {error&&<p role="alert">{error}</p>}{notice&&<p role="status">{notice}</p>}{loading&&<p role="status">正在读取历史事件…</p>}
    {event?<>
      <div ref={picture} className="replay-picture">
        <div className="replay-caption"><time>{timeLabel(event)}</time><strong>{privateView?kindLabel(event):label(event)}</strong><button data-export-hide onClick={()=>{setPlaying(false);setDetail(!detail);}}>查看这幕详情</button></div>
        <OfficeViewport fitHeight className={reduced?'static':''}><OfficeSceneSVG/><OfficeActors key={event.event_id+String(privateView)} members={publicCast} messages={message?[message]:[]} enabled={!reduced} playback playbackRate={speed} frozen={!playing} choose={()=>{setPlaying(false);setDetail(true);}} openMessages={()=>{setPlaying(false);setDetail(true);}}/></OfficeViewport>
        <p className="replay-watermark">AP-Vibe · 工作室故事 · 根据已记录事件演绎{privateView?' · 姓名与消息已隐藏':''}</p>
      </div>
      {detail&&<article className="replay-detail"><MessageContent text={privateView?'分享模式已隐藏姓名、消息与任务入口。关闭此选项可查看原始详情。':event.body||event.actor?.summary||'状态依据来自工作室记录。'}/>{!privateView&&<small>事件编号 {event.event_id} · {event.actor?.run_id||event.task_id||event.actor?.session_id||''}</small>}</article>}
    </>:!loading&&<div className="office-empty"><h3>这段时间还没有交互片段</h3><p>伙伴转移工作区、交接或发送工作消息后，会在这里留下故事。初次发现角色的静态登记不会冒充交互。</p></div>}
  </section></div>;
}
