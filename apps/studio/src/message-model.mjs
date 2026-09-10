export function prepareMessage(text) {
  const notes = [];
  let body = String(text || "");
  // Private scaffolding is omitted, never moved to the expandable footnotes.
  body = body.replace(/<(thinking|analysis)>[\s\S]*?(?:<\/\1>|$)/gi, "");
  body = body.replace(/<oai-mem-citation>([\s\S]*?)(?:<\/oai-mem-citation>|$)/g, (_, content) => {
    notes.push(content.replace(/<[^>]*>/g, "").trim()); return "";
  });
  body = body.replace(/:{1,2}codex-annotation\{([^}]*)\}/g, (_, content) => { notes.push("引用原回复：" + content); return ""; });
  if (body.includes("# Response annotations:") && body.includes("## My request:")) {
    const parts = body.split("## My request:");
    const selected = parts.shift().match(/<response-annotations>([\s\S]*?)<\/response-annotations>/)?.[1];
    if (selected) { try { JSON.parse(selected).forEach(item => notes.push(String(item.text || ""))); } catch { notes.push("这条消息带有对原回复的引用。"); } }
    body = parts.join("## My request:");
  }
  return { body: body.trim(), notes: notes.filter(Boolean) };
}

export function nearBottom(element) {
  return element.scrollHeight - element.scrollTop - element.clientHeight <= 48;
}

export function newerMessageCount(messages, seen) {
  const previous=messages.filter(m=>seen.has(m.message_id));
  const lastOffset=Math.max(-1,...previous.map(m=>Number.isFinite(m.offset)?m.offset:-1));
  const lastTime=Math.max(0,...previous.map(m=>Date.parse(m.timestamp)||0));
  return messages.filter(m=>!seen.has(m.message_id)&&(Number.isFinite(m.offset)&&lastOffset>=0?m.offset>lastOffset:(Date.parse(m.timestamp)||0)>=lastTime)).length;
}

export function captureAnchor(element) {
  if (!element) return null;
  const top = element.getBoundingClientRect().top;
  const child = Array.from(element.querySelectorAll("[data-message-id]")).find(node => node.getBoundingClientRect().bottom > top);
  return child ? { id: child.dataset.messageId, offset: child.getBoundingClientRect().top - top } : null;
}

export function restoreAnchor(element, anchor) {
  if (!anchor) return false;
  const child = Array.from(element.querySelectorAll("[data-message-id]")).find(node => node.dataset.messageId === anchor.id);
  if (!child) return false;
  element.scrollTop += child.getBoundingClientRect().top - element.getBoundingClientRect().top - anchor.offset;
  return true;
}
