import {useEffect,useMemo,useRef,useState} from 'react';
import {AgentPortrait,useDocumentHidden} from './AgentAppearance';
import {LAYOUT_V2 as layout,workstationSlots} from './OfficeLayout';
import {advance,makeNavigation,meetingReached,MessageWatermark,routeBetween,samePoint} from './office-motion';

const navigation=makeNavigation(layout);

export function OfficeActors({members,messages,selected,choose,enabled,frozen,openMessages,page=0,playback=false,playbackRate=1}){
  const hidden=useDocumentHidden();
  const slots=useMemo(()=>workstationSlots(members,page),[members,page]);
  const actors=useRef(new Map()),[positions,setPositions]=useState({}),[meeting,setMeeting]=useState(null);
  const watermark=useRef(new MessageWatermark());
  const replaySeen=useRef(new Set());
  const meetingRef=useRef(null),lastTick=useRef(null);
  const queuedMeetings=useRef([]);
  const [arrived,setArrived]=useState(false);
  useEffect(()=>{
    if(!messages||frozen)return;
    const fresh=playback?messages.filter(m=>!replaySeen.current.has(m.message_id)):watermark.current.ingest(messages);
    if(playback)messages.forEach(m=>replaySeen.current.add(m.message_id));
    if(!fresh.length)return;
    const resolve=(id,taskId)=>members.find(m=>m.agent.agent_id===id||m.runs.some(r=>r.run_id===id))||
      members.find(m=>m.agent.profile_id===id&&m.runs.some(r=>r.run_id===taskId))||members.find(m=>m.agent.profile_id===id);
    const visible=new Set(slots.map(s=>s.member.agent.agent_id));
    const eligible=fresh.map(m=>({...m,senderId:resolve(m.sender)?.agent.agent_id,recipientId:resolve(m.recipient,m.task_id)?.agent.agent_id})).filter(m=>visible.has(m.recipientId));
    if(!eligible.length)return;
    queuedMeetings.current.push(...eligible.map(item=>({...item,
      senderName:resolve(item.sender)?.agent.name||(item.sender==='工作台'?'工作台':'其他会话'),
      recipientName:resolve(item.recipient,item.task_id)?.agent.name||'伙伴',
      senderId:visible.has(item.senderId)?item.senderId:null,count:1,elapsed:0,arrivedAt:null})));
    if(!meetingRef.current){const value=queuedMeetings.current.shift();meetingRef.current=value;setMeeting(value);setArrived(false);}
  },[messages,members,slots,frozen,playback]);
  const goals=useMemo(()=>{
    const result=new Map(slots.map(s=>[s.member.agent.agent_id,s.point]));
    if(enabled&&meeting?.senderId&&meeting.senderId!==meeting.recipientId){
      const target=result.get(meeting.recipientId);
      if(target){const targetSlot=slots.find(s=>s.member.agent.agent_id===meeting.recipientId);const zone=layout.zones.find(r=>r.room_id===targetSlot?.room);const center=zone?zone.bounds.x+zone.bounds.width/2:layout.scene.width/2;
        result.set(meeting.senderId,{x:target.x+(target.x<center?58:-58),y:target.y});}
    }
    return result;
  },[slots,meeting,enabled]);
  useEffect(()=>{
    if(frozen){
      for(const [id,goal] of goals)if(!actors.current.has(id))actors.current.set(id,{at:goal,target:goal,path:[],direction:'front',moving:false});
      setPositions(Object.fromEntries(actors.current));return;
    }
    const next=new Map();
    for(const [id,goal] of goals){
      const current=actors.current.get(id);
      if(!current||!enabled)next.set(id,{at:goal,target:goal,path:[],direction:'front',moving:false});
      else if(!samePoint(current.target,goal))next.set(id,{...current,target:goal,path:routeBetween(current.at,goal,navigation)});
      else next.set(id,current);
    }
    actors.current=next;setPositions(Object.fromEntries(next));
  },[goals,enabled,frozen]);
  useEffect(()=>{
    if(frozen||hidden||(!enabled&&!meeting))return;
    let frame;
    const tick=time=>{
      const seconds=lastTick.current===null?0:Math.min((time-lastTick.current)/1000,0.06)*(playback?playbackRate:1);lastTick.current=time;
      if(!frozen&&!document.hidden){
        let changed=false;
        if(enabled)for(const [id,actor] of actors.current){if(actor.path.length){actors.current.set(id,advance(actor,seconds,layout.walking_speed));changed=true;}}
        if(changed)setPositions(Object.fromEntries(actors.current));
        const current=meetingRef.current;
        // The message ref is queued before React installs its matching goals.
        // A previous frame must not time arrival against the old workstation.
        if(current&&current===meeting){
          current.elapsed+=seconds;
          if(meetingReached(current,actors.current,goals,enabled)&&current.arrivedAt===null){current.arrivedAt=current.elapsed;setArrived(true);}
          if((current.arrivedAt!==null&&current.elapsed-current.arrivedAt>6)||current.elapsed>15){
            const next=queuedMeetings.current.shift()||null;meetingRef.current=next;setMeeting(next);setArrived(false);
          }
        }
      }
      frame=requestAnimationFrame(tick);
    };
    frame=requestAnimationFrame(tick);return()=>{cancelAnimationFrame(frame);lastTick.current=null;};
  },[enabled,frozen,hidden,goals,meeting,playback,playbackRate]);
  const speaker=meeting&&(meeting.senderId||meeting.recipientId);
  const speaking=meeting&&arrived;
  const bubbleAt=speaker&&(positions[speaker]?.at||goals.get(speaker));
  const bubbleBelow=bubbleAt&&bubbleAt.y<190;
  return <>
    {slots.map(({member:m,point,station})=>{
      const actor=positions[m.agent.agent_id],at=actor?.at||point;
      const moving=!!actor?.moving&&enabled&&!frozen;
      const talk=!!speaking&&speaker===m.agent.agent_id;
      const action=talk?'talk':moving?'walk':m.current?.animation||'rest';
      return <button key={m.agent.agent_id} data-agent-id={m.agent.agent_id} data-motion={moving?'walking':talk?'talking':'stationary'} data-direction={actor?.direction||'front'} data-idle={!moving&&!talk&&enabled&&!frozen} data-station={station.id} className={'office-actor'+(selected===m.agent.agent_id?' selected':'')} style={{left:at.x,top:at.y,zIndex:2+Math.floor(at.y/10),'--idle-delay':-(m.agent.agent_id.charCodeAt(m.agent.agent_id.length-1)%7)+'s'}} onClick={()=>choose(m)} aria-label={`${m.agent.name}，${m.current?.activity_label||'空闲'}${m.active>1?'，'+m.active+'项活动任务':''}`}>
        <AgentPortrait profile={m.agent} size={68} action={action} direction={moving?actor.direction:'front'} animate={enabled&&!frozen}/>
        {station.type==='bed'&&!moving&&!talk&&action==='rest'&&<span className="office-lap-blanket" aria-hidden="true"/>}
        <span className="office-actor-name">{m.ordinary&&!m.ordinary.collaboration?.enabled?'◉ ':''}{m.agent.name}</span><small>{m.current?.activity_label||'空闲'}{m.ordinary?(m.ordinary.collaboration?.enabled?' · 协作中':' · 专注中'):''}{m.active>1?' · '+m.active+'项任务':''}</small>
      </button>;
    })}
    {speaking&&bubbleAt&&<button className={'office-message-bubble'+(bubbleBelow?' below':'')} style={{left:Math.max(8,Math.min(layout.scene.width-268,bubbleAt.x-125)),top:bubbleBelow?bubbleAt.y+62:bubbleAt.y-186}} onClick={openMessages} aria-label="打开这次工作消息"><b title={meeting.senderName+' → '+meeting.recipientName}>{meeting.senderName} → {meeting.recipientName}</b><span>{meeting.body}</span><small>工作消息已保存{meeting.count>1?' · '+meeting.count+'条新消息':''} · 查看原文</small></button>}
  </>;
}
