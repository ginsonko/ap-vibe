import test from 'node:test';
import assert from 'node:assert/strict';
import {newerMessageCount} from '../src/message-model.mjs';

const msg=(id,offset,time='2026-09-10T07:00:00Z')=>({message_id:id,offset,timestamp:time});
test('loading preceding public records does not announce new messages at identical timestamps',()=>{
  assert.equal(newerMessageCount([msg('old',2),msg('seen',20)],new Set(['seen'])),0);
});
test('history prepending and a concurrent arrival only count the later record',()=>{
  assert.equal(newerMessageCount([msg('old',2),msg('seen',20),msg('new',30)],new Set(['seen'])),1);
});
test('polling already read records does not grow the unread badge',()=>{
  assert.equal(newerMessageCount([msg('a',2),msg('b',20)],new Set(['a','b'])),0);
});
test('recent monitor windows without offsets still count timestamped arrivals',()=>{
  assert.equal(newerMessageCount([msg('old',undefined,'2026-09-10T06:00:00Z'),msg('seen'),msg('new',undefined,'2026-09-10T07:01:00Z')],new Set(['seen'])),1);
});
