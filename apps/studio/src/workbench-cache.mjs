export const CACHE_KEY = 'ap-vibe:monitor-snapshot:v2';
export const PROJECT_KEY = 'ap-vibe:selected-project';
export const PAGE_KEY = 'ap-vibe:selected-page';

export function readPreference(storage, key, fallback) {
  try { return storage.getItem(key) || fallback; } catch { return fallback; }
}

export function savePreference(storage, key, value) {
  try { storage.setItem(key, value); } catch { /* Storage can be disabled. */ }
}

export function readWorkbenchCache(storage) {
  try {
    const value = JSON.parse(storage.getItem(CACHE_KEY) || 'null');
    if (!value || typeof value !== 'object') return null;
    const projectId = readPreference(storage, PROJECT_KEY, value.projectId);
    // Old versions cached full histories; restore only the directory. Current
    // project data and service health must come from this page's requests.
    return { projectId, projects: Array.isArray(value.projects) ? value.projects : [] };
  } catch { return null; }
}

export function saveWorkbenchCache(storage, snapshot) {
  savePreference(storage, PROJECT_KEY, snapshot.projectId);
  // Never copy all task histories into localStorage: large real workspaces
  // exceed its quota, silently leaving an old selected project behind.
  try { storage.setItem(CACHE_KEY, JSON.stringify({ projectId: snapshot.projectId, projects: snapshot.projects || [] })); } catch { /* Cache is optional. */ }
}
