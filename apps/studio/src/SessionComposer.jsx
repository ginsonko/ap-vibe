import { useEffect, useRef, useState } from "react";
import { PaperPlaneTilt, ArrowSquareOut, CircleNotch } from "@phosphor-icons/react";

const endpoint = "/v1/ap-vibe/codex/messages";
const labels = { queued: "等待发送", running: "正在执行", submitted: "已排入 Codex", completed: "回复完成", failed: "未发送", uncertain: "需要查看结果", cancelled: "已撤回", desktop_required: "请在原任务继续" };

async function request(url, body) {
  const response = await fetch(url, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal: AbortSignal.timeout(15000) } : { signal: AbortSignal.timeout(12000) });
  const value = await response.json();
  if (!response.ok) throw new Error(value?.error?.message || "暂时无法连接，请稍后重试。");
  return value;
}

export function SessionComposer({ session, draft, setDraft }) {
  const [deliveries, setDeliveries] = useState([]);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [transport, setTransport] = useState("detecting");
  const attempt = useRef(null);
  const sessionId = session.session_id;
  useEffect(() => {
    let cancelled = false, timer;
    setDeliveries([]); setError(""); setTransport("detecting"); attempt.current = null;
    const poll = async () => {
      try {
        const value = await request(endpoint + "?session_id=" + encodeURIComponent(sessionId));
        if (!cancelled) { setDeliveries(value.deliveries || []); setTransport(value.transport || "cli_resume"); }
      } catch { /* A failed read must not discard the last known delivery. */ }
      if (!cancelled) timer = setTimeout(poll, 3000);
    };
    poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [sessionId]);
  const send = async () => {
    if (sending || !draft.trim()) return;
    const text = draft.trim();
    // A transport timeout does not prove rejection. Retry identical content
    // using the same durable request identity, including after page reload.
    const key = "ap-vibe:message-pending:" + sessionId;
    try { attempt.current ||= JSON.parse(localStorage.getItem(key) || "null"); } catch { /* optional */ }
    if (!attempt.current || attempt.current.message !== text) attempt.current = { session_id: sessionId, request_id: "message-" + crypto.randomUUID(), message: text };
    const body = attempt.current;
    try { localStorage.setItem(key, JSON.stringify(body)); } catch { /* optional */ }
    setSending(true); setError("");
    try {
      const value = await request(endpoint, body);
      setDeliveries(previous => [...previous.filter(item => item.request_id !== value.delivery.request_id), value.delivery]);
      setDraft(""); attempt.current = null;
      try { localStorage.removeItem(key); } catch { /* optional */ }
    } catch (exc) { setError(exc.message + " 草稿已保留；再次点击会核对同一条消息，不会重复提交。"); }
    finally { setSending(false); }
  };
  const latest = deliveries.at(-1);
  const writerConflict = latest?.status === "desktop_required" || (latest?.status === "failed" && latest?.detail?.includes("active writer"));
  const deliveryNote = writerConflict ? "这条任务仍由 Codex 桌面管理，网页没有发送。原消息已保留，可复制到原任务继续。" : latest?.detail;
  const copyMessage = async () => {
    try { await navigator.clipboard.writeText(latest.message); setError("消息已复制；点击“打开 Codex”后粘贴发送。"); }
    catch { setDraft(latest.message); setError("剪贴板不可用，原消息已恢复到输入框，可以手动复制。"); }
  };
  return <div className="handoff-box"><div className="handoff-heading"><strong>直接发给当前任务</strong><a className="text-action" href={"codex://threads/" + encodeURIComponent(sessionId)}><ArrowSquareOut size={15} />打开 Codex</a></div>
    <textarea aria-label="给当前任务的消息草稿" value={draft} onChange={event => setDraft(event.target.value)} placeholder="例如：继续上一项工作，把这次改动和验证结果告诉我。" rows={2} />
    {error && <p role="alert" className="composer-error">{error}</p>}
    {latest && <div className="composer-receipt" role="status"><b>{writerConflict ? "请在原任务继续" : labels[latest.status] || latest.status}</b><span>{deliveryNote}</span>{writerConflict && <button className="text-action" onClick={copyMessage}>复制原消息</button>}{latest.diagnostic && <details><summary>技术详情</summary><pre>{latest.diagnostic}</pre></details>}{latest.response && <details><summary>查看本次回复</summary><p>{latest.response}</p></details>}{latest.status === "queued" && <button className="text-action" onClick={async () => { try { const value = await request(endpoint + "/cancel", { request_id: latest.request_id, session_id: sessionId }); setDeliveries(previous => previous.map(item => item.request_id === latest.request_id ? value.delivery : item)); } catch (exc) { setError(exc.message); } }}>撤回</button>}</div>}
    <div className="handoff-foot"><span>{transport === "desktop_queue" ? "已连接 Codex 桌面队列：消息交给原任务，忙碌时排队，回复会回到工作台。" : transport === "detecting" ? "正在检查本机 Codex 的消息能力…" : "当前 Codex 版本仅支持 CLI 续接；桌面占用时会保留消息，提供原任务入口。"}</span><button className="primary-button" disabled={sending || !draft.trim() || !sessionId} onClick={send}>{sending ? <CircleNotch size={17} className="spin" /> : <PaperPlaneTilt size={17} />}{sending ? "正在提交" : "发送给 Codex"}</button></div></div>;
}
