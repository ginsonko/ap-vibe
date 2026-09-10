import test from 'node:test';
import assert from 'node:assert/strict';
import { CACHE_KEY, PROJECT_KEY, readWorkbenchCache, saveWorkbenchCache } from '../src/workbench-cache.mjs';

test('large real histories cannot exhaust cache quota or restore the wrong project', () => {
  const saved = new Map();
  const storage = {getItem:key=>saved.get(key), setItem:(key,value)=>{if(value.length>10000) throw Error('quota'); saved.set(key,value);}};
  const snapshot = {projectId:'B',projects:[{project_id:'B'}],overview:{sessions:[{messages:[{text:'x'.repeat(6000000)}]}]},data:{project_id:'B'}};
  saveWorkbenchCache(storage, snapshot);
  assert.equal(readWorkbenchCache(storage).projectId, 'B');
  assert.ok(saved.get(CACHE_KEY).length < 1000);
  assert.equal(saved.get(PROJECT_KEY),'B');
  assert.equal(readWorkbenchCache(storage).overview,undefined);
});

test('project selection survives reload even if the optional directory cache fails', () => {
  const saved = new Map([[CACHE_KEY, JSON.stringify({projectId:'A',projects:[],data:{project_id:'A'}})]]);
  const storage={getItem:k=>saved.get(k),setItem:(k,v)=>{if(k===CACHE_KEY)throw Error('quota');saved.set(k,v);}};
  saveWorkbenchCache(storage,{projectId:'B'});
  assert.equal(readWorkbenchCache(storage).projectId,'B');
  assert.equal(readWorkbenchCache(storage).data,undefined);
});
