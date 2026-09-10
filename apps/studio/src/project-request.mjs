import { registeredProject } from './monitor-model.mjs';

export function availableProjectId(projects, selected, preferred) {
  const entries = Array.isArray(projects) ? projects.filter(p => p && typeof p.project_id === 'string') : [];
  if (entries.some(p => p.project_id === selected)) return selected;
  const active = entries.filter(p => p.status === 'active');
  if (active.some(p => p.project_id === preferred)) return preferred;
  return (active.find(registeredProject) || active[0])?.project_id || '';
}

// A request generation belongs to one selection, including A → B → A.
export function createProjectRequests() {
  let current;
  return {
    begin(projectId) {
      current?.controller.abort();
      const token = { projectId, controller: new AbortController() };
      current = token;
      return token;
    },
    isCurrent(token) { return current === token && !token.controller.signal.aborted; },
    cancel() { current?.controller.abort(); current = undefined; },
  };
}

export async function readProjectJson(url, projectId, { signal, fetcher = fetch, timeout = 12000 } = {}) {
  for (let attempt = 0; attempt < 3; attempt++) {
    signal?.throwIfAborted();
    const controller = new AbortController();
    const abort = () => controller.abort(signal.reason);
    signal?.addEventListener('abort', abort, { once: true });
    const timer = setTimeout(() => controller.abort(new DOMException('读取超时', 'TimeoutError')), timeout);
    try {
      const response = await fetcher(url, { signal: controller.signal, headers: { Accept: 'application/json' } });
      const body = await response.json();
      if (!response.ok) {
        const error = new Error(body?.error?.message || '项目资料暂时无法读取');
        error.status = response.status;
        throw error;
      }
      if (!body || body.project_id !== projectId) throw new Error('项目资料响应与当前选择不一致，正在重新读取');
      return body;
    } catch (error) {
      signal?.throwIfAborted();
      if (attempt === 2 || (error.status && error.status < 500)) throw error;
    } finally {
      clearTimeout(timer);
      signal?.removeEventListener('abort', abort);
    }
  }
}
