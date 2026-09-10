export function sessionForActivity(sessions, activity = {}) {
  const ref = activity.source_ref;
  if (typeof ref !== 'string' || !ref.startsWith('codex-jsonl://')) return null;
  let key;
  try { key = new URL(ref).searchParams.get('source'); } catch { return null; }
  if (!/^[a-f0-9]{16,64}$/i.test(key || '')) return null;
  const matches = sessions.filter(s => s.source_projects?.length
    ? s.source_projects.some(source => source.project_id === activity.project_id && source.source_key.startsWith(key.toLowerCase()))
    : s.project_id === activity.project_id && (s.source_keys || [s.source_key]).some(value => value.startsWith(key.toLowerCase())));
  return matches.length === 1 ? matches[0] : null;
}

export function messageBuckets(sessions, now = Date.now(), count = 12, minutes = 5) {
  const width = minutes * 60000;
  const end = Math.floor(now / width) * width + width;
  const start = end - count * width;
  const buckets = Array.from({ length: count }, (_, i) => ({ time: start + i * width, end: start + (i + 1) * width, value: 0 }));
  for (const session of sessions) {
    for (const message of session.messages || []) {
      const at = Date.parse(message.timestamp);
      if (Number.isFinite(at) && at >= start && at <= now && at < end) {
        const index = Math.min(count - 1, Math.max(0, Math.floor((at - start) / width)));
        buckets[index].value += 1;
      }
    }
  }
  return buckets;
}

export function selectSnapshot(previous, projectId, results) {
  const next = { ...previous, projectId };
  if (previous.projectId !== projectId) { next.state = null; next.data = null; }
  const names = ['health', 'projects', 'state', 'data', 'overview'];
  const failures = [];
  results.forEach((result, i) => {
    if (result.status === 'fulfilled') {
      next[names[i]] = i === 1 ? result.value.projects || [] : result.value;
    } else failures.push(names[i]);
  });
  // Last successful content survives. A single failed health probe is shown as
  // partial availability until repeated failures justify "未连接".
  if (failures.includes('health')) {
    const healthFailures = (Number(previous.health_failures) || 0) + 1;
    next.health_failures = healthFailures;
    next.health = { ...next.health, status: healthFailures >= 3 ? 'unavailable' : 'degraded', health_failures: healthFailures };
  } else {
    next.health_failures = 0;
  }
  next.stale = failures.length > 0;
  next.failures = failures;
  if (!next.stale) next.savedAt = new Date().toISOString();
  return next;
}
export function registeredProject(project) {
  return project.registration_state ? project.registration_state === 'registered' :
    project.source !== 'machine_auto_workspace' || ['agent_reported', 'user_edited'].includes(project.documentation_authority);
}
