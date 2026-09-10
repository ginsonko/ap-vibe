import test from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'vite';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { availableProjectId, createProjectRequests, readProjectJson } from '../src/project-request.mjs';

test('first installation and obsolete selection use an actual project without inventing one', () => {
  const projects = [{project_id:'old',status:'archived'}, {project_id:'custom',status:'active',classification:'confirmed'}];
  assert.equal(availableProjectId(projects,''),'custom');
  assert.equal(availableProjectId(projects,'deleted'),'custom');
  assert.equal(availableProjectId(projects,'old'),'old');
  assert.equal(availableProjectId([], 'deleted'),'');
  assert.equal(availableProjectId([null], 'deleted'),'');
  assert.equal(availableProjectId([...projects,{project_id:'configured',status:'active'}], '', 'configured'),'configured');
  assert.equal(availableProjectId(projects, 'custom', 'other'),'custom');
});

test('A → B → A discards both earlier generations and aborts their requests', () => {
  const requests = createProjectRequests();
  const firstA = requests.begin('A');
  const b = requests.begin('B');
  const nextA = requests.begin('A');
  assert.equal(requests.isCurrent(firstA), false);
  assert.equal(requests.isCurrent(b), false);
  assert.equal(requests.isCurrent(nextA), true);
  assert.equal(firstA.controller.signal.aborted, true);
  assert.equal(b.controller.signal.aborted, true);
});

test('chapter request retries transient/mismatched responses and accepts only the selected project', async () => {
  let calls = 0;
  const value = await readProjectJson('/chapter', 'B', { fetcher: async () => {
    calls++;
    if (calls === 1) throw new TypeError('connection reset');
    return { ok: true, json: async () => ({ project_id: calls === 2 ? 'A' : 'B' }) };
  }});
  assert.equal(value.project_id, 'B');
  assert.equal(calls, 3);
  const controller = new AbortController();
  controller.abort();
  await assert.rejects(readProjectJson('/chapter', 'B', { signal: controller.signal, fetcher: () => { throw new Error('must not fetch'); } }), { name: 'AbortError' });
});

test('actual project component renders loading, absent and irregular dossier states without crashing', async () => {
  const vite = await createServer({ server: { middlewareMode: true }, appType: 'custom' });
  try {
    const { ProjectKnowledge } = await vite.ssrLoadModule('/src/ProjectKnowledge.jsx');
    for (const manifest of [undefined, null, {}, { project_id: 'A', project: null, catalog: [null], assessment: [null, { key: 'intent', score: 'bad' }] }]) {
      const html = renderToStaticMarkup(React.createElement(ProjectKnowledge, { manifest, projectId: 'A' }));
      assert.ok(html.length > 0);
      assert.doesNotMatch(html, /NaN|\[object Object\]/);
    }
  } finally { await vite.close(); }
});
