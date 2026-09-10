const dimensions = ['intent', 'logic', 'completeness', 'reliability', 'security', 'performance', 'maintainability', 'compatibility', 'usability', 'validation'];
const text = value => typeof value === 'string' && value.trim().length > 0;

export function assessmentCoverage(entries) {
  const records = new Map((Array.isArray(entries) ? entries : []).filter(item => item && dimensions.includes(item.key)).map(item => [item.key, item]));
  const documented = [...records.values()].filter(item => ['reason', 'risk', 'improvement'].every(key => text(item[key])) && Array.isArray(item.evidence_refs) && item.evidence_refs.every(text) && (item.score === null || item.evidence_refs.length > 0));
  const scored = documented.filter(item => typeof item.score === 'number' && Number.isFinite(item.score) && item.score >= 0 && item.score <= 100);
  return { documented: documented.length, scored: scored.length, total: dimensions.length };
}

export function projectDescription(project) {
  if (!project) return '正在读取项目简介…';
  const issues = Array.isArray(project.quality_issues) ? project.quality_issues : [];
  const provisional = issues.some(issue => /^identity_|^chapter_auto_detected:identity$/.test(issue)) || project.documentation_state === 'auto_detected';
  const summary = project.description?.trim() || '项目简介尚未填写，可以让 Codex 从真实来源补充。';
  return provisional ? `${summary}（自动识别线索，待结合真实资料补充）` : summary;
}

export function curationError(value) {
  const detail = typeof value === 'string' ? value : '';
  if (/overload|Too many pending|429|503/i.test(detail)) return 'Codex 上游繁忙。已保留草稿，稍后可继续未完成项目。';
  if (/timeout|SSE|timed.out/i.test(detail)) return 'Codex 本次等待超时。已保留草稿，可继续未完成项目。';
  if (/disconnect|stream closed/i.test(detail)) return 'Codex 连接中断。已保留草稿，可继续未完成项目。';
  if (/revision|conflict/i.test(detail)) return '项目有更新，需要合并最新档案。继续时会重新读取，保留既有内容。';
  if (/incomplete/i.test(detail)) return '部分项目尚未完成。已有档案和成功草稿均保留，可继续整理。';
  if (/cli_missing/i.test(detail)) return '没有找到 Codex 命令。请先安装并登录 Codex，再继续整理。';
  return '这次整理没有完成，已有档案仍保留。可查看技术详情并继续整理。';
}
