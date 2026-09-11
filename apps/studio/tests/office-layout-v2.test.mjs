import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {makeNavigation,routeBetween,samePoint} from '../src/office-motion.js';
const layout=JSON.parse(fs.readFileSync(new URL('../src/office-layout-v2.json',import.meta.url)));
const floors=JSON.parse(fs.readFileSync(new URL('../src/office-floors.json',import.meta.url)));
const stations=layout.zones.flatMap(z=>[...(z.workstations||[]),...(z.beds||[])].map(s=>({...s,zone:z})));
const nav=makeNavigation(layout);

test('All simultaneous occupants fit within the scene and do not cover each other',()=>{
  assert.equal(stations.length,35);
  for(let i=0;i<stations.length;i++){
    const a=stations[i].actor,b=stations[i].zone.bounds;
    assert.ok(a.x>=75&&a.x<=layout.scene.width-75&&a.y>=58&&a.y<=layout.scene.height-58);
    assert.ok(a.x-75>=b.x&&a.x+75<=b.x+b.width&&a.y-58>=b.y&&a.y+58<=b.y+b.height,stations[i].id+' card inside room');
    for(const other of stations.slice(i+1))assert.ok(Math.abs(a.x-other.actor.x)>=150||Math.abs(a.y-other.actor.y)>=116,stations[i].id+' overlaps '+other.id);
  }
});

function crossings(a,b,rect){
  const hits=[];
  if(a.x!==b.x)for(const x of [rect.x,rect.x+rect.width]){
    const t=(x-a.x)/(b.x-a.x),y=a.y+(b.y-a.y)*t;
    if(t>1e-7&&t<1-1e-7&&y>=rect.y&&y<=rect.y+rect.height)hits.push({x,y});
  }
  if(a.y!==b.y)for(const y of [rect.y,rect.y+rect.height]){
    const t=(y-a.y)/(b.y-a.y),x=a.x+(b.x-a.x)*t;
    if(t>1e-7&&t<1-1e-7&&x>=rect.x&&x<=rect.x+rect.width)hits.push({x,y});
  }
  return hits;
}
test('Every pair has a real endpoint and crosses enclosed boundaries at a doorway',()=>{
  for(const [a,b] of nav.edges)assert.ok(nav.points[a]&&nav.points[b]);
  for(const from of stations)for(const to of stations){
    assert.deepEqual(nav.points[from.id],from.actor);
    const path=[from.actor,...routeBetween(from.actor,to.actor,nav)];assert.ok(samePoint(path.at(-1),to.actor));
    for(let i=1;i<path.length;i++)for(const zone of layout.zones.filter(z=>z.enclosure!=='open')){
      for(const hit of crossings(path[i-1],path[i],zone.bounds)){
        assert.ok(zone.doors.some(d=>Math.hypot(d.x-hit.x,d.y-hit.y)<=26),`${from.id}->${to.id} crosses ${zone.id} away from door`);
      }
    }
  }
});

test('Four distinct floor themes are data, and runtime placement has no global store writes',()=>{
  assert.equal(new Set(floors.floors.map(f=>f.id)).size,floors.floors.length);
  assert.ok(floors.floors.length>=4);
  assert.equal(new Set(floors.floors.map(f=>JSON.stringify(f.details))).size,floors.floors.length);
  const source=fs.readFileSync(new URL('../src/OfficeLayout.js',import.meta.url),'utf8');
  assert.ok(!/setCurrentFloor|floor-store/.test(source));
});
