import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
after(() => server.close());
const { CognitionEvidence } = await server.ssrLoadModule('/src/CognitionEvidence.jsx');
const render = frame => renderToStaticMarkup(createElement(CognitionEvidence, { frame }));

test('absent evidence is not presented as an empty candidate array or successful action', () => {
  const html = render(undefined);
  assert.match(html, /来源未记录/);
  assert.match(html, /未记录/);
  assert.doesNotMatch(html, /本帧无候选|已获取来源|执行成功/);
});

test('projected evidence preserves empty paradigms and failed results', () => {
  const html = render({ sa: { occurrence_id: 'sa1', source: 'external' }, feelings: [],
    slow_affect: { curiosity: 0, unknown: null }, actions: [], paradigms: [], decision: { result_status: 'failed' } });
  assert.match(html, /已获取来源/);
  assert.match(html, /1 项数值/);
  assert.match(html, /本帧无候选/);
  assert.match(html, /执行失败/);
  assert.doesNotMatch(html, /执行成功/);
});

test('unknown result codes survive the projection', () => {
  assert.match(render({ decision: { result_status: 'future_pending_state' } }), /future_pending_state/);
});
