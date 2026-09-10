const wait = (ms) => new Promise(resolve => setTimeout(resolve, ms));

export async function workspaceApi(path, value) {
  const isRead = value === undefined;
  const attempts = isRead ? 3 : 1;
  let lastError;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    let response;
    try {
      response = await fetch('/v1/ap-vibe/' + path, {
        ...(isRead ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(value) }),
        signal: AbortSignal.timeout(12000),
      });
      const body = await response.json();
      if (!response.ok) {
        const detail = body.error || {};
        const error = new Error([detail.message, detail.code, detail.solution].filter(Boolean).join(' · ') || '请求未完成');
        error.status = response.status;
        // A read can safely retry a transient daemon response. Never replay a
        // write: POST may have already committed or charged upstream work.
        if (!isRead || response.status < 500 || attempt === attempts - 1) throw error;
        lastError = error;
      } else return body;
    } catch (error) {
      lastError = error;
      if (!isRead || attempt === attempts - 1 || (error?.status && error.status < 500)) throw error;
    }
    await wait(250 * (attempt + 1));
  }
  throw lastError || new Error('请求未完成');
}

export const requestId = () => 'workbench-' + (crypto.randomUUID ? crypto.randomUUID() : Date.now() + '-' + Math.random().toString(16).slice(2));
