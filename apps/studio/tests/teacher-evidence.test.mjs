import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createServer } from 'vite';

const server = await createServer({ server: { middlewareMode: true, hmr: false }, appType: 'custom' });
after(() => server.close());
const { TeacherEvidence } = await server.ssrLoadModule('/src/TeacherEvidence.jsx');
const render = teacher => renderToStaticMarkup(createElement(TeacherEvidence, { teacher }));

test('missing episode evidence remains unknown even without candidates', () => {
  const html = render(undefined);
  assert.match(html, /未记录调用回执/);
  assert.match(html, /实际采用：<b>未记录/);
  assert.doesNotMatch(html, /实际采用：<b>0/);
});

test('real candidate family schema shows adoption and rejection independently', () => {
  const html = render({
    mode: 'provider_off',
    candidates: { action: [{ candidate_ref: 'action-one', delta: 0.1 }], thought: [{ candidate_id: 'thought-one', text: 'PRIVATE_SCAFFOLD' }] },
    adoption: {
      summary: { teacher_proposed: 2, adopted: 1, rejected: 1 },
      families: {
        action: { mode: 'bounded_score_assist', teacher_proposed: [{ candidate_ref: 'action-one' }], adopted: [{ candidate_ref: 'action-one' }], rejected: [] },
        thought: { mode: 'shadow_only', teacher_proposed: [{ candidate_id: 'thought-one', text: 'PRIVATE_SCAFFOLD' }], adopted: [], rejected: [{ candidate_id: 'thought-one', reason: 'shadow_only_current_wave' }] },
      },
    },
  });
  assert.match(html, /行动评分建议/);
  assert.match(html, /已有采用记录/);
  assert.match(html, /当前仅观察，尚未采用/);
  assert.match(html, /实际采用：<b>1/);
  assert.doesNotMatch(html, /PRIVATE_SCAFFOLD/);
});

test('unknown families and original failure statuses remain visible', () => {
  const html = render({ mode: 'future_mode', candidates: { future_family: [{ candidate_id: 'future-id' }] },
    call_receipt: { call_id: 'receipt-failed', status: 'provider_rejected', latency_ms: 0 } });
  assert.match(html, /future_family/);
  assert.match(html, /future_mode/);
  assert.match(html, /provider_rejected/);
  assert.match(html, /receipt-failed/);
  assert.match(html, /耗时：0 毫秒/);
  assert.doesNotMatch(html, /已有采用记录/);
});
