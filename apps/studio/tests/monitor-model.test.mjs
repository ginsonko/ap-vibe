import test from 'node:test';
import assert from 'node:assert/strict';
import { messageBuckets, selectSnapshot, sessionForActivity } from '../src/monitor-model.mjs';

test('chart buckets use timestamps and omit old or future messages', () => {
  const now = Date.parse('2026-09-05T12:04:00Z');
  const data = [{ messages: ['12:00', '12:02', '11:59', '10:00', '13:00'].map(t => ({ timestamp: `2026-09-05T${t}:00Z` })) }];
  const buckets = messageBuckets(data, now);
  assert.equal(buckets.at(-1).value, 2);
  assert.equal(buckets.at(-2).value, 1);
  assert.equal(buckets.reduce((sum, b) => sum + b.value, 0), 3);
});

test('partial refresh retains the previous project and marks stale', () => {
  const before = { projectId: 'a', state: { marker: 'a' }, data: { marker: 'a' } };
  const results = [{ status: 'fulfilled', value: { status: 'ok' } }, ...Array(4).fill({ status: 'rejected' })];
  const same = selectSnapshot(before, 'a', results);
  assert.equal(same.state.marker, 'a');
  assert.equal(same.stale, true);
  const changed = selectSnapshot(before, 'b', results);
  assert.equal(changed.state, null);
  assert.equal(changed.data, null);
});

test('health status degrades before showing a disconnected service', () => {
  const before = { projectId: 'a', health: { status: 'ok' }, state: { marker: 'a' } };
  const failed = [{ status: 'rejected' }, ...Array(4).fill({ status: 'fulfilled', value: {} })];
  const once = selectSnapshot(before, 'a', failed);
  assert.equal(once.health.status, 'degraded');
  assert.equal(once.health_failures, 1);
  const twice = selectSnapshot(once, 'a', failed);
  assert.equal(twice.health.status, 'degraded');
  const thrice = selectSnapshot(twice, 'a', failed);
  assert.equal(thrice.health.status, 'unavailable');
});

test('activity navigation never guesses an ambiguous or foreign project', () => {
  const source_key = 'a'.repeat(64);
  const session = { project_id: 'p', source_key };
  const activity = { project_id: 'p', source_ref: `codex-jsonl://source?source=${source_key.slice(0,16)}` };
  assert.equal(sessionForActivity([session], activity), session);
  assert.equal(sessionForActivity([session, { ...session, source_key: 'a'.repeat(63)+'b' }], activity), null);
  assert.equal(sessionForActivity([session], { ...activity, project_id: 'other' }), null);
});

test('merged task navigation matches the original project and exact source together', () => {
  const a='a'.repeat(64),b='b'.repeat(64);
  const task={project_id:'confirmed',source_key:a,source_keys:[a,b],source_projects:[
    {project_id:'confirmed',source_key:a},{project_id:'old-workspace',source_key:b}]};
  const activity={project_id:'old-workspace',source_ref:`codex-jsonl://source?source=${b}`};
  assert.equal(sessionForActivity([task],activity),task);
  assert.equal(sessionForActivity([task],{...activity,project_id:'confirmed'}),null);
});
