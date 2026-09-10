import test from 'node:test';
import assert from 'node:assert/strict';
import { assessmentCoverage, projectDescription, curationError } from '../src/dossier-presentation.mjs';

test('unknown score preserves a documented assessment, without inventing numeric coverage', () => {
  const entry = {key:'intent',score:null,reason:'现有目标有证据',risk:'验收未覆盖移动端',improvement:'补充移动端验收',evidence_refs:['docs/requirements.md']};
  assert.deepEqual(assessmentCoverage([entry]), {documented:1,scored:0,total:10});
  assert.deepEqual(assessmentCoverage([entry, {...entry, score:80}, {key:'logic',score:90}, null]), {documented:1,scored:1,total:10});
  assert.equal(assessmentCoverage([{...entry, score:Infinity}]).scored, 0);
});

test('missing scores cannot replace a curated project identity with a provisional warning', () => {
  const p = {description:'采购平台的真实简介',documentation_state:'maintained',documentation_quality:'needs_curation',quality_issues:['assessment_pending:10']};
  assert.equal(projectDescription(p), p.description);
  assert.match(projectDescription({...p,quality_issues:['identity_auto_detected']}), /自动识别线索/);
});

test('curation errors provide a recoverable Chinese instruction', () => {
  assert.match(curationError('organization_codex_timeout:Too many pending requests'), /上游繁忙/);
  assert.match(curationError('organization_revision_conflict'), /合并最新档案/);
  assert.match(curationError('unknown_upstream_code'), /技术详情/);
});
