import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {advance,makeNavigation,routeBetween,samePoint,MessageWatermark} from '../src/office-motion.js';
const layout=JSON.parse(fs.readFileSync(new URL('../src/office-layout.json',import.meta.url)));
const nav=makeNavigation(layout);
test('Every room pair crosses divisions only through the shared corridor',()=>{
  const slots=Object.values(nav.points);
  for(const from of slots)for(const to of slots){
    const path=[from,...routeBetween(from,to,nav)];
    assert.ok(samePoint(path.at(-1),to));
    for(let i=1;i<path.length;i++){
      const a=path[i-1],b=path[i];
      assert.ok(a.x===b.x||a.y===b.y,'orthogonal path');
      if(Math.floor(a.x/320)!==Math.floor(b.x/320))assert.equal(a.y,layout.corridor_y);
    }
  }
});
test('Retarget midway from current location without teleport or overshoot',()=>{
  const start={x:80,y:150},end={x:880,y:585};
  let actor=advance({at:start,path:routeBetween(start,end,nav)},0.7,190);
  const middle=actor.at;
  const target={x:400,y:150};
  actor={...actor,path:routeBetween(middle,target,nav)};
  assert.equal(actor.at,middle);
  actor=advance(actor,100,190);assert.deepEqual(actor.at,target);assert.equal(actor.moving,false);
  assert.deepEqual(routeBetween({x:600,y:300},{x:550,y:300},nav),[{x:550,y:300}]);
});
test('Existing, duplicate and stale messages never replay; fresh messages retain identities',()=>{
  const now=Date.now(),m=(id,age=0)=>({message_id:id,created_at:new Date(now-age).toISOString()});
  const mark=new MessageWatermark();
  assert.deepEqual(mark.ingest([m('a')],now),[]);
  assert.deepEqual(mark.ingest([m('c'),m('b'),m('a')],now).map(x=>x.message_id),['b','c']);
  assert.deepEqual(mark.ingest([m('c'),m('b'),m('old',40000)],now),[]);
});
