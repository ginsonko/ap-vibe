import dijkstra from 'dijkstrajs';

export const distance=(a,b)=>Math.hypot(b.x-a.x,b.y-a.y);
export const samePoint=(a,b)=>a&&b&&distance(a,b)<0.1;

export function makeNavigation(layout){
  const points={},edges=[];
  const add=(id,x,y)=>{points[id]={x,y};return id;};
  const corridors=[...new Set(layout.rooms.map(r=>r.x))].sort((a,b)=>a-b);
  corridors.forEach((x,i)=>{add('hall-'+x,x,layout.corridor_y);if(i)edges.push(['hall-'+corridors[i-1],'hall-'+x]);});
  for(const room of layout.rooms){
    const rows=[...room.rows].sort((a,b)=>Math.abs(a-layout.corridor_y)-Math.abs(b-layout.corridor_y));
    let previous='hall-'+room.x;
    for(const y of rows){
      const center=add(room.id+'-'+y,room.x,y);edges.push([previous,center]);previous=center;
      for(const dx of layout.slots_x)edges.push([center,add(room.id+'-'+y+'-'+dx,room.x+dx,y)]);
    }
  }
  return {points,edges};
}

function project(point,a,b){
  const dx=b.x-a.x,dy=b.y-a.y,length=dx*dx+dy*dy;
  const t=Math.max(0,Math.min(1,length?((point.x-a.x)*dx+(point.y-a.y)*dy)/length:0));
  return {x:a.x+dx*t,y:a.y+dy*t};
}

export function routeBetween(start,end,nav){
  if(samePoint(start,end))return [];
  const points={...nav.points},graph={};
  const edge=(a,b)=>{const cost=distance(points[a],points[b]);(graph[a]??={})[b]=cost;(graph[b]??={})[a]=cost;};
  nav.edges.forEach(([a,b])=>edge(a,b));
  const attach=(id,point)=>{
    const nearest=nav.edges.map(([a,b])=>({a,b,at:project(point,points[a],points[b])})).sort((a,b)=>distance(point,a.at)-distance(point,b.at))[0];
    if(!nearest)return null;
    points[id]=point;points[id+'-on']=nearest.at;
    edge(id,id+'-on');edge(id+'-on',nearest.a);edge(id+'-on',nearest.b);
    return nearest;
  };
  const a=attach('from',start),b=attach('to',end);
  if(!a||!b)return [];
  if(a.a===b.a&&a.b===b.b)edge('from-on','to-on');
  const ids=dijkstra.find_path(graph,'from','to');
  let previous=start;
  return ids.slice(1).map(id=>points[id]).filter(point=>{if(samePoint(previous,point))return false;previous=point;return true;});
}

export function advance(actor,seconds,speed){
  let remaining=Math.max(0,seconds)*speed;
  const next={...actor,path:[...actor.path]};
  while(next.path.length&&remaining>0){
    const target=next.path[0],length=distance(next.at,target);
    const dx=target.x-next.at.x,dy=target.y-next.at.y;
    if(length>0.1)next.direction=Math.abs(dx)>Math.abs(dy)?(dx>0?'right':'left'):(dy>0?'front':'back');
    if(length<=remaining){next.at=target;next.path.shift();remaining-=length;}
    else {next.at={x:next.at.x+dx*remaining/length,y:next.at.y+dy*remaining/length};remaining=0;}
  }
  next.moving=next.path.length>0;
  return next;
}

export function roomSlots(members,layout){
  return layout.rooms.flatMap(room=>members.filter(m=>m.room===room.id).slice(0,4).map((member,i)=>({member,room:room.id,
    point:{x:room.x+layout.slots_x[i%layout.slots_x.length],y:room.rows[Math.floor(i/layout.slots_x.length)]}})));
}

// A local watermark is presentation state only. No delivery receipt is modified.
export class MessageWatermark{
  constructor(){this.seen=new Set();this.initialized=false;}
  ingest(messages,now=Date.now()){
    const fresh=messages.filter(m=>!this.seen.has(m.message_id));
    messages.forEach(m=>this.seen.add(m.message_id));
    if(this.seen.size>1000)this.seen=new Set([...this.seen].slice(-600));
    if(!this.initialized){this.initialized=true;return [];}
    return fresh.filter(m=>{const age=now-Date.parse(m.created_at);return age>=-5000&&age<30000;}).reverse();
  }
}
