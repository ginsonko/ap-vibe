import { useLayoutEffect, useRef, useState } from "react";
import { MessageContent } from "./MessageContent";
import { captureAnchor, nearBottom, restoreAnchor, newerMessageCount } from "./message-model.mjs";

export function SessionTimeline({ session, assistantLabel = "Codex", onOlder, hasOlder, olderLoading }) {
  const viewport = useRef(null);
  const follow = useRef(true);
  const anchor = useRef(null);
  const current = useRef(null);
  const previous = useRef(new Set());
  const [unread, setUnread] = useState(0);
  const [following, setFollowing] = useState(true);
  const [retained, setRetained] = useState([]);
  const incoming = session?.messages || [];
  const identity = session ? session.identity || session.project_id + ":" + (session.session_id || session.source_key) : "";
  // Keep already loaded history while this task is open, even when the server's
  // sliding 80-message window moves forward. This prevents a reading anchor
  // from being removed by a normal poll.
  const same = current.current === identity;
  const messages = same ? [...new Map([...retained, ...incoming].map(m => [m.message_id, m])).values()]
    .sort((a, b) => Number.isFinite(a.offset) && Number.isFinite(b.offset)
      ? a.offset - b.offset : Date.parse(a.timestamp) - Date.parse(b.timestamp)) : incoming;
  const signature = messages.map(m => m.message_id + ":" + m.text).join("\0");
  const toBottom = () => {
    follow.current = true; setFollowing(true); setUnread(0);
    if (viewport.current) viewport.current.scrollTop = viewport.current.scrollHeight;
    anchor.current = null;
  };
  useLayoutEffect(() => {
    const el = viewport.current;
    if (!el) return;
    const changed = current.current !== identity;
    if (changed) { current.current = identity; follow.current = true; setUnread(0); setFollowing(true); }
    const ids = new Set(messages.map(m => m.message_id));
    if (follow.current || changed) el.scrollTop = el.scrollHeight;
    else {
      restoreAnchor(el, anchor.current);
      const added = newerMessageCount(messages, previous.current);
      if (added) setUnread(count => count + added);
    }
    previous.current = ids;
    anchor.current = captureAnchor(el);
    // Retain at most 400 messages once following; while reading history, keep
    // the anchor intact. A different task resets this local-only buffer.
    const next = follow.current ? messages.slice(-400) : messages;
    setRetained(next);
  }, [identity, signature]);
  useLayoutEffect(() => {
    const el = viewport.current;
    if (!el) return;
    const observer = new ResizeObserver(() => { if (follow.current) el.scrollTop = el.scrollHeight; else restoreAnchor(el, anchor.current); });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  const readOlder = () => {
    follow.current = false; setFollowing(false);
    anchor.current = captureAnchor(viewport.current);
    onOlder();
  };
  return <div className="timeline-shell"><div className="timeline-follow-status"><span>{following ? "正在跟随最新消息" : "正在阅读历史 · 新消息不会打断你"}</span>{!following && <button className="text-action" onClick={toBottom}>{unread ? `${unread} 条新消息 · ` : ""}回到最新 ↓</button>}</div>{onOlder && <div className="timeline-history"><button className="text-action" onClick={readOlder} disabled={!hasOlder || olderLoading}>{olderLoading ? "正在读取更早消息…" : hasOlder ? "读取更早消息 ↑" : "已到这份记录的开头"}</button></div>}<div className="message-timeline" ref={viewport} tabIndex={0} aria-label="任务消息历史" onScroll={event => {
    follow.current = nearBottom(event.currentTarget); setFollowing(follow.current);
    if (follow.current) setUnread(0);
    anchor.current = captureAnchor(event.currentTarget);
  }}>{messages.map(message => <article data-message-id={message.message_id} key={message.message_id} className={"message-bubble " + (message.role === "user" ? "user" : message.role === "tool" ? "tool" : "assistant")}><div className="message-meta"><span>{message.role === "user" ? "你" : message.role === "tool" ? "工具" : assistantLabel}</span><time>{new Date(message.timestamp).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" })}</time></div><MessageContent text={message.text} /></article>)}{!messages.length && <p className="muted">这份记录当前没有可见消息；如有更早记录，可以继续向前读取。</p>}</div></div>;
}
