import { Component, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Archive, ArrowClockwise, ArrowRight, BookOpen, Brain, Broadcast, CaretRight,
  ChartLineUp, CheckCircle, CircleNotch, Clock, Copy, Database, Desktop, DownloadSimple,
  Eye, FileText, FolderOpen, GearSix, GitBranch, House, Info, List, MagnifyingGlass,
  PauseCircle, Pulse, ShieldCheck, Sparkle, UsersThree, WarningCircle, X,
} from "@phosphor-icons/react";

import { WorkbenchUpdates } from "./WorkbenchUpdates";
import { LogicWorkbench } from "./LogicWorkbench";
import { projectDescription } from "./dossier-presentation.mjs";
import { registeredProject } from "./monitor-model.mjs";
import { SessionArchive } from "./SessionArchive";
import { CognitionPage } from "./CognitionPage";
import { OrganizationButtons, OrganizationPage, ProjectManagement } from "./OrganizationPage";
import { ProjectKnowledge } from "./ProjectKnowledge";
import { PortableProject, PortableHistory } from "./PortableProject";
import { availableProjectId, createProjectRequests } from "./project-request.mjs";
import { MessageContent } from "./MessageContent";
import { AgentStudio } from "./AgentStudio";
import { prepareMessage } from "./message-model.mjs";

import { messageBuckets, selectSnapshot, sessionForActivity } from "./monitor-model.mjs";

const API = "/v1/ap-vibe";
import { PAGE_KEY, PROJECT_KEY, readPreference, savePreference, readWorkbenchCache, saveWorkbenchCache } from "./workbench-cache.mjs";
const NAV_ITEMS = [
  { id: "home", label: "首页", description: "实时监看", icon: House },
  { id: "sessions", label: "会话", description: "按标题查看", icon: UsersThree },
  { id: "agents", label: "Agent 工作室", description: "伙伴与托管任务", icon: Sparkle },
  { id: "logic", label: "逻辑观察", description: "查清代码关系", icon: GitBranch },
  { id: "cognition", label: "AP 认知", description: "可选认知核心", icon: Brain },
  { id: "projects", label: "项目与记忆", description: "管理本地数据", icon: Database },
  { id: "help", label: "帮助", description: "一步一步学会", icon: BookOpen },
];
const sessionDraftKey = session => session ? (session.harness || "codex") + ":" + (session.session_id || session.source_key || session.source_id) : "";
const ROLE_NAMES = { user: "你", assistant: "Codex" };
const TITLE_SOURCES = {
  "Codex 子任务身份": "协作子任务 · 按主任务归属区分",
  "Codex 会话标题": "与 Codex 任务标题一致",
  首条可见用户消息: "暂用已采集用户消息摘要",
  首条可见助手消息: "暂用已采集助手消息摘要",
  尚无可见消息: "当前还没有可见消息",
};

class ApiError extends Error {
  constructor(message, solution, status) {
    super(message);
    this.name = "ApiError";
    this.solution = solution;
    this.status = status;
  }
}

function uid(prefix) {
  if (globalThis.crypto && globalThis.crypto.randomUUID) return prefix + "-" + globalThis.crypto.randomUUID();
  return prefix + "-" + Date.now() + "-" + Math.random().toString(16).slice(2);
}

function number(value, fallback) {
  return typeof value === "number" && Number.isFinite(value) ? value : (fallback || 0);
}

function text(value, fallback) {
  return typeof value === "string" && value.trim() ? value.trim() : (fallback || "");
}

function truncate(value, limit) {
  const clean = prepareMessage(text(value, "")).body;
  const max = limit || 160;
  return clean.length > max ? clean.slice(0, max) + "…" : clean;
}

function formatTime(value, withDate) {
  if (!value) return "尚无时间";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间未知";
  const options = withDate
    ? { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }
    : { hour: "2-digit", minute: "2-digit" };
  return new Intl.DateTimeFormat("zh-CN", options).format(date);
}

function formatRelative(value) {
  if (!value) return "尚无活动";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "时间未知";
  const minutes = Math.max(0, Math.round((Date.now() - date.getTime()) / 60000));
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return minutes + " 分钟前";
  if (minutes < 1440) return Math.floor(minutes / 60) + " 小时前";
  return Math.floor(minutes / 1440) + " 天前";
}

function getEpisode(item) { return item && (item.episode || item) || {}; }
function getActivity(episode) {
  const input = episode && episode.input || {};
  return input.activity || input.feedback || input.query || {};
}
function getEpisodeTitle(episode) {
  const activity = getActivity(episode);
  return text(activity.summary || activity.natural_language || activity.target, "未命名工程活动");
}
function toneForStatus(status) {
  if (["ok", "ready", "success", "running", "active", "complete"].includes(status)) return "good";
  if (["failed", "error", "unavailable"].includes(status)) return "bad";
  return "warn";
}

function healthLabel(status, compact = false) {
  if (status === "ok") return compact ? "在线" : "本地服务在线";
  if (status === "degraded") return compact ? "部分更新" : "服务部分可用";
  if (status === "unavailable") return compact ? "未连接" : "等待本地服务";
  return compact ? "读取中" : "正在连接";
}

async function requestJson(url, options) {
  const read = !options || !options.method || options.method.toUpperCase() === "GET";
  const attempts = read ? 3 : 1;
  let last;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    options?.signal?.throwIfAborted();
    const timeoutSignal = AbortSignal.timeout(12000);
    try {
      const response = await fetch(url, { ...(options || {}), signal: options?.signal ? AbortSignal.any([options.signal, timeoutSignal]) : timeoutSignal, headers: { Accept: "application/json", ...((options || {}).headers || {}) } });
      let payload = null;
      try { payload = await response.json(); } catch { payload = null; }
      if (response.ok) return payload;
      const detail = payload && payload.error || {};
      const error = new ApiError(detail.message || ("请求失败（" + response.status + "）。"), detail.solution || "查看帮助页中的故障处理步骤。", response.status);
      last = error;
      if (!read || response.status < 500 || attempt === attempts - 1) throw error;
    } catch (error) {
      options?.signal?.throwIfAborted();
      last = error;
      if (!read || attempt === attempts - 1 || (error instanceof ApiError && error.status < 500)) throw error;
    }
    await new Promise(resolve => setTimeout(resolve, 250 * (attempt + 1)));
  }
  throw last || new ApiError("无法连接 AP‑Vibe 本地服务。", "确认本地服务仍在运行，然后点击右上角刷新。", 0);
}

function StatusPill({ status, label }) {
  return <span className={"status-pill " + toneForStatus(status)}><i />{label || status || "未知"}</span>;
}

function IconButton({ label, onClick, children, disabled, className }) {
  return <button type="button" className={"icon-button " + (className || "")} aria-label={label} title={label} onClick={onClick} disabled={disabled}>{children}</button>;
}

function SectionHeading({ eyebrow, title, description, action, icon: Icon }) {
  const HeadingIcon = Icon || Sparkle;
  return <div className="section-heading"><div className="section-heading-main"><span className="section-heading-icon"><HeadingIcon size={18} weight="duotone" /></span><div><span className="eyebrow">{eyebrow}</span><h2>{title}</h2>{description && <p>{description}</p>}</div></div>{action}</div>;
}

function PageHeader({ eyebrow, title, description, action }) {
  return <div className="page-header"><div><span className="eyebrow">{eyebrow}</span><h1>{title}</h1>{description && <p>{description}</p>}</div>{action}</div>;
}

function EmptyState({ icon: Icon, title, detail, action }) {
  const EmptyIcon = Icon || Info;
  return <div className="empty-state"><span className="empty-state-icon"><EmptyIcon size={25} weight="duotone" /></span><strong>{title}</strong><p>{detail}</p>{action}</div>;
}

function StatCard({ icon: Icon, label, value, detail, tone }) {
  return <div className={"stat-card " + (tone || "teal")}><span className="stat-icon"><Icon size={20} weight="duotone" /></span><div><span>{label}</span><strong>{value}</strong><small>{detail}</small></div></div>;
}

function Sparkline({ values, tooltips, color }) {
  const clean = (values || []).map((value) => Math.max(0, number(value)));
  const max = Math.max(1, ...clean);
  const width = 420;
  const height = 110;
  const points = clean.length > 1
    ? clean.map((value, index) => ((index / (clean.length - 1)) * width) + "," + (height - 16 - (value / max) * (height - 32))).join(" ")
    : "0," + (height - 24) + " " + width + "," + (height - 24);
  return <svg className="sparkline" viewBox={"0 0 " + width + " " + height} role="img" aria-label="真实数据趋势图"><path d={"M 0 " + (height - 16) + " H " + width} className="sparkline-baseline" /><polyline points={points} fill="none" stroke={color || "#167d70"} strokeWidth="3" strokeLinecap="round" strokeLinejoin="round" />{clean.map((value, index) => <circle key={index} cx={clean.length > 1 ? (index / (clean.length - 1)) * width : width / 2} cy={clean.length > 1 ? height - 16 - (value / max) * (height - 32) : height - 24} r="4" fill={color || "#167d70"}><title>{(tooltips || [])[index] || `${value} 条消息`}</title></circle>)}</svg>;
}

function BarChart({ values, labels, color }) {
  const clean = values || [];
  const max = Math.max(1, ...clean.map((value) => number(value)));
  return <div className="bar-chart" role="img" aria-label="真实数据柱状图">{clean.map((value, index) => <div className="bar-column" key={index}><span className="bar-value">{value}</span><div className="bar-track"><i style={{ height: Math.max(4, (number(value) / max) * 100) + "%", background: color || "#d58a3a" }} /></div><small>{(labels || [])[index] || ""}</small></div>)}</div>;
}

function SessionCard({ session, selected, onClick }) {
  return <button type="button" className={"session-card " + (selected ? "selected" : "")} onClick={onClick}><div className="session-card-top"><span className={"session-dot " + (session.active ? "active" : "")} /><span>{session.active ? "近期活跃" : "暂未活动"}</span><time>{formatRelative(session.last_activity_at)}</time></div><h3>{session.title || "标题尚未取得"}</h3><p className="session-title-source">{TITLE_SOURCES[session.title_source] || session.title_source || "标题来源未知"}</p><div className="session-card-bottom"><span><FolderOpen size={14} />{session.project_name || session.project_id || "未命名项目"}</span><span><List size={14} />{number(session.message_count)} 条可见消息</span><ArrowRight size={16} /></div>{session.last_activity_text && <p className="session-preview"><b>{session.last_activity_role === "user" ? "你" : "Codex"}：</b>{truncate(session.last_activity_text, 100)}</p>}</button>;
}

function EventRow({ item, session, onClick }) {
  const episode = getEpisode(item);
  const activity = getActivity(episode);
  const isLogic = episode.episode_kind === "logic_field_observation" || Boolean(episode.logic_field);
  return <button type="button" className="event-row" onClick={onClick}><span className={"event-marker " + (isLogic ? "logic" : "")}><i /></span><span className="event-row-content"><strong>{session?.title || (isLogic ? "局部逻辑观察" : "项目工程事件")}</strong><span>{truncate(getEpisodeTitle(episode), 150)}</span><small>{formatTime(item.updated_at || episode.created_at, true)} · {number(episode.ticks && episode.ticks.length)} 个认知帧</small></span><CaretRight size={17} /></button>;
}

function NoticeBanner({ tone, icon: Icon, title, detail, action }) {
  const BannerIcon = Icon || Info;
  return <div className={"notice-banner " + (tone || "info")}><BannerIcon size={20} weight="duotone" /><div><strong>{title}</strong><p>{detail}</p></div>{action}</div>;
}

function HomePage({ health, sessions, events, state, loading = false, onRefresh, onOpenSession, onOpenEvent, onInstallLauncher }) {
  const [chartRange, setChartRange] = useState("1h");
  if (loading) return <div className="page-stack" aria-busy="true"><PageHeader eyebrow="本机实时监看" title="每一条任务，都在眼前" description="先看谁有新进展，再点标题查看上下文。" /><NoticeBanner tone="info" title="正在读取任务标题和消息" detail="首次读取可能需要几秒，完成后自动显示。已有会话和项目不会因此消失。" /><section className="stats-grid"><StatCard icon={Broadcast} label="本地服务" value={healthLabel(health?.status, true)} detail="正在连接本机数据" tone="teal" />{[[Pulse,"近期活跃任务","blue"],[ChartLineUp,"最近消息","amber"],[UsersThree,"可回看任务","purple"]].map(([Icon,label,tone])=><StatCard key={label} icon={Icon} label={label} value="读取中" detail="读取完成后显示实际数量" tone={tone}/>)}</section></div>;
  const ranges = { "1h": { label: "最近 1 小时", count: 12, minutes: 5 }, "6h": { label: "最近 6 小时", count: 18, minutes: 20 }, "24h": { label: "最近 24 小时", count: 24, minutes: 60 }, "7d": { label: "最近 7 天", count: 28, minutes: 360 } };
  const selectedRange = ranges[chartRange] || ranges["1h"];
  const populated = sessions.filter(item => item.message_count > 0);
  const active = populated.filter(item => item.active);
  const inactive = populated.filter(item => !item.active);
  const empty = sessions.filter(item => !item.message_count);
  const buckets = messageBuckets(sessions, Date.now(), selectedRange.count, selectedRange.minutes);
  const values = buckets.map(item => item.value);
  const labels = Array.from({ length: 5 }, (_, i) => buckets[Math.round(i * (buckets.length - 1) / 4)])
    .map(item => new Date(item.time).toLocaleString("zh-CN", chartRange === "7d" ? { month: "numeric", day: "numeric" } : { hour: "2-digit", minute: "2-digit", hour12: false }));
  const tooltips = buckets.map(item => `${formatTime(item.time, true)} - ${formatTime(item.end, true)}：${item.value} 条消息`);
  const total = values.reduce((a, b) => a + b, 0);
  const monitor = state?.monitor || {};
  const pendingSources = monitor.last_error?.pending_assignment_count;
  const monitorLabel = !monitor.running ? "监控已暂停" : monitor.status === "degraded" ? (pendingSources > 0 ? `监控运行中 · ${pendingSources} 个来源待处理` : "监控运行中 · 部分采集待检查") : "自动监控运行中";
  const monitorHeadline = !monitor.running ? "后台监控当前没有运行" : monitor.status === "degraded" ? "监控仍在运行，部分来源需要检查" : "新消息会自动出现在这里";
  const recent = events.slice(0, 8);
  const ranking = populated.slice().sort((a, b) => b.message_count - a.message_count).slice(0, 5);
  const maxCount = Math.max(1, ...ranking.map(item => item.message_count));
  return <div className="page-stack home-page">
    <PageHeader eyebrow="本机实时监看 · 每 10 秒自动更新" title="每一条任务，都在眼前" description="先看谁有新进展，再点标题查看上下文。其他功能在左侧各自的页面里。" action={<div className="page-header-actions"><button className="secondary-button" onClick={onRefresh}><ArrowClockwise size={17} />刷新现场</button><button className="text-action launcher-action" onClick={onInstallLauncher} title="在 Windows 桌面创建 AP-Vibe 启动快捷方式"><Desktop size={16} />添加桌面启动器</button></div>} />
    <WorkbenchUpdates />
    <section className="stats-grid">
      <StatCard icon={Broadcast} label="本地服务" value={healthLabel(health?.status, true)} detail="本机运行 · 无需云端上传" tone="teal" />
      <StatCard icon={Pulse} label="近期活跃任务" value={active.length} detail="最近 15 分钟出现可见消息" tone="blue" />
      <StatCard icon={ChartLineUp} label={`${selectedRange.label}消息`} value={total} detail="当前已采集窗口内的消息" tone="amber" />
      <StatCard icon={UsersThree} label="可回看任务" value={populated.length} detail={"另有 " + empty.length + " 个来源等待内容"} tone="purple" />
    </section>
    <section className="monitor-strip"><div><span className="eyebrow">自动监控</span><strong>{monitorHeadline}</strong><p>共发现 {sessions.length} 个任务来源。可见消息按任务和项目分别保存；待处理来源不会阻塞其他任务。</p></div><div className="monitor-strip-meta"><StatusPill status={monitor.status === "degraded" ? "warn" : (monitor.running ? "running" : "stopped")} label={monitorLabel} /><span>最近检查：{formatTime(monitor.last_poll_at || monitor.last_success_at, true)}</span></div></section>
    <section className="charts-grid">
      <div className="chart-panel"><div className="chart-panel-head"><div><span className="eyebrow">消息节奏 · {selectedRange.label}</span><h2>任务在什么时候有新进展</h2></div><div className="chart-panel-actions"><label className="chart-range"><span>时间范围</span><select value={chartRange} onChange={event => setChartRange(event.target.value)}>{Object.entries(ranges).map(([key, item]) => <option key={key} value={key}>{item.label}</option>)}</select></label><span className="chart-total">{total} 条</span></div></div><Sparkline values={values} tooltips={tooltips} /><div className="chart-labels">{labels.map((label, i) => <span key={i}>{label}</span>)}</div><small className="chart-footnote">每个点表示 {selectedRange.minutes < 60 ? `${selectedRange.minutes} 分钟` : `${selectedRange.minutes / 60} 小时`} 内已采集的消息数；将鼠标放到数据点上可查看具体时间和值。</small></div>
      <div className="chart-panel"><div className="chart-panel-head"><div><span className="eyebrow">任务动态</span><h2>哪些任务积累了更多上下文</h2></div><UsersThree size={21} /></div><div className="task-ranking">{ranking.length ? ranking.map(item => <button key={item.source_key} onClick={() => onOpenSession(item)}><span>{item.title}</span><b>{item.message_count} 条</b><i style={{ width: (item.message_count / maxCount * 100) + "%" }} /></button>) : <EmptyState title="正在等待任务内容" detail="采集到可见消息后，真实任务统计会出现在这里。" />}</div><small className="chart-footnote">统计来自最近已采集窗口，可点击任务继续查看。</small></div>
    </section>
    <section className="page-section"><SectionHeading eyebrow="优先查看" title="近期活跃任务" description="有新消息的任务在前。活跃表示最近有消息，不等同于 Codex 正在生成。" icon={Pulse} action={<span className="section-count">{active.length} 条</span>} />{active.length ? <div className="session-grid">{active.slice(0, 6).map(item => <SessionCard key={item.source_key} session={item} onClick={() => onOpenSession(item)} />)}</div> : <EmptyState icon={PauseCircle} title="最近暂时没有新消息" detail="监控开启时，下一条可见消息会自动刷新到这里。" />}</section>
    <section className="page-section"><SectionHeading eyebrow="随时回看" title="暂未活跃的任务" description="历史内容仍然保留，点击标题就能接着看。" icon={Clock} action={<span className="section-count">{inactive.length} 条</span>} />{inactive.length ? <div className="session-grid">{inactive.slice(0, 6).map(item => <SessionCard key={item.source_key} session={item} onClick={() => onOpenSession(item)} />)}</div> : <p className="muted">目前没有已采集且暂未活跃的任务。</p>}</section>
    {empty.length > 0 && <details className="empty-sources"><summary>已发现、尚无可见内容的来源 <b>{empty.length}</b></summary><p>来源存在，但当前读取窗口还没有可展示的消息。展开后仍可按 Codex 标题定位；这不代表任务已删除。</p><div className="session-grid">{empty.map(item => <SessionCard key={item.source_key} session={item} onClick={() => onOpenSession(item)} />)}</div></details>}
    <section className="page-section"><SectionHeading eyebrow="当前项目 · 最近记录" title="刚刚发生了什么" description="带任务标题的记录可以直接打开原任务上下文。" icon={List} />{recent.length ? <div className="event-list">{recent.map(item => <EventRow key={item.request_id} item={item} session={sessionForActivity(sessions, getActivity(getEpisode(item)))} onClick={() => onOpenEvent(item)} />)}</div> : <EmptyState title="还没有工程记录" detail="读取到消息或执行逻辑观察后，这里会显示真实记录。" />}</section>
  </div>;
}

function LogicObservation({ record, onOpen }) {
  const episode = getEpisode(record);
  const field = episode.logic_field || {};
  const activity = getActivity(episode);
  const summary = text(field.summary || (episode.causal_summary || []).find((item) => item.key === "activity")?.detail, "已生成一份局部静态观察");
  return <article className="logic-observation-card"><div className="logic-observation-head"><div><span className="eyebrow">{"项目源码报告"}</span><h3>{summary}</h3></div><StatusPill status={field.status || "static_only"} label={field.status === "static_chain_observed" ? "静态链路已观察" : "仅静态线索"} /></div><div className="logic-metrics"><span><b>{number(field.nodes && field.nodes.length)}</b> 个源码对象</span><span><b>{number(field.edges && field.edges.length)}</b> 条静态关系</span><span><b>{number(episode.ticks && episode.ticks.length)}</b> 个认知帧</span><span><b>{formatTime(record.updated_at || episode.created_at)}</b> 最近观察</span></div><p>{truncate(field.detail || field.explanation || (episode.input?.logic_query || episode.input?.query)?.target || getEpisodeTitle(episode), 260)}</p><div className="logic-observation-foot"><span>{field.runtime_state ? "运行证据：" + field.runtime_state : "运行入口和动态调用仍需真实回读"}</span><button type="button" className="text-action" onClick={onOpen}><Eye size={16} />打开完整观察</button></div></article>;
}

function LogicModal({ selected, history, project, onClose, onSelect }) {
  const dialogRef = useRef(null);
  const closeRef = useRef(onClose); closeRef.current = onClose;
  useEffect(() => {
    const previousFocus = document.activeElement;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    dialogRef.current?.querySelector("button")?.focus();
    const handle = event => {
      if (event.key === "Escape") closeRef.current();
      if (event.key !== "Tab") return;
      const nodes = Array.from(dialogRef.current?.querySelectorAll('button, [href], input, select, textarea, summary, [tabindex="0"]') || []).filter(node => !node.disabled && node.getClientRects().length);
      if (!nodes.length) return;
      const first = nodes[0], last = nodes[nodes.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    };
    document.addEventListener("keydown", handle);
    return () => { document.removeEventListener("keydown", handle); document.body.style.overflow = previousOverflow; previousFocus?.focus?.(); };
  }, []);
  const episode = getEpisode(selected), field = episode.logic_field || {};
  const query = field.query || episode.input?.logic_query || {};
  const types = { module_responsibility: "它负责什么", change_impact: "改这里会影响谁", first_broken_link: "预期链先断在哪" };
  const evidence = [...new Set((field.nodes || []).flatMap(node => node.evidence_refs || []))];
  return <div className="modal-backdrop" role="presentation" onMouseDown={event => { if (event.target === event.currentTarget) onClose(); }}>
    <section ref={dialogRef} className="logic-modal" role="dialog" aria-modal="true" aria-labelledby="logic-modal-title">
      <header className="modal-header"><div><span className="eyebrow">{project?.display_name || "当前项目"} · 局部逻辑观察</span><h2 id="logic-modal-title">{query.target || "观察报告"}</h2><p>{formatTime(selected.updated_at || field.snapshot?.created_at, true)} · {types[query.kind] || "局部观察"}</p></div><IconButton label="关闭观察" onClick={onClose}><X size={19} /></IconButton></header>
      <div className="logic-modal-grid"><div className="logic-current" tabIndex={0} aria-label="完整观察内容">
        <div className="modal-section-title"><strong>本次结论</strong><StatusPill status="ready" label="静态源码观察" /></div>
        <p className="logic-lead">{field.summary || "当前没有详细结论"}</p>
        <div className="context-note"><Info size={16} /><span>这是项目级代码查询。报告未记录来源任务时，不会将它猜测归属于某条 Codex 任务。</span></div>
        <div className="logic-snapshot-grid"><div><span>源码对象</span><strong>{field.nodes?.length || 0}</strong></div><div><span>静态关系</span><strong>{field.edges?.length || 0}</strong></div></div>
        <div className="logic-list-section"><h3>下一步怎么检查</h3><p>{field.next_check || "回到 Codex，让它结合真实运行结果检查这份观察。"}</p></div>
        <div className="logic-list-section"><h3>证据来自哪里</h3>{evidence.length ? <ul>{evidence.slice(0, 16).map(item => <li key={item}><code>{item}</code></li>)}</ul> : <p>这次没有提取到可定位的源码证据。</p>}</div>
        <div className="logic-list-section"><h3>还不能确定什么</h3>{field.unknowns?.length ? <ul>{field.unknowns.map((item, i) => <li key={i}>{item.detail || item.message || item.reason || item.code || JSON.stringify(item)}</li>)}</ul> : <p>本次没有额外未知项；静态关系仍不能证明运行正确。</p>}</div>
        {field.nodes?.length > 0 && <details className="research-details"><summary>查看源码对象明细</summary><div className="source-object-list">{field.nodes.map(node => <article key={node.node_id}><strong>{node.qualname || node.name}</strong><p>{node.summary || "源码已定位，暂无职责描述。"}</p><code>{node.path} · 第 {node.start_line} 行</code></article>)}</div></details>}
        {field.edges?.length > 0 && <details className="research-details"><summary>查看静态关系明细</summary><pre>{JSON.stringify(field.edges, null, 2)}</pre></details>}
        <p className="muted">源码观察有读取上限；动态调用、运行结果和任务收益需要分别验证。</p>
      </div><aside className="logic-history"><div className="modal-section-title"><strong>项目历史上下文</strong><span>{history.length} 条</span></div><p className="muted">最多保留最近 40 份已完成报告。点击任意一条查看当时结论，长列表可以上下滚动。</p><div className="logic-history-scroll" tabIndex={0} aria-label="历史观察列表">{history.map((item, i) => { const f = getEpisode(item).logic_field || {}; return <button type="button" key={item.request_id || f.report_id} className={"logic-history-item " + (f.report_id === field.report_id ? "active" : "")} onClick={() => onSelect(item)}><span className="logic-history-index">{history.length - i}</span><span><strong>{f.query?.target || "局部观察"}</strong><p>{f.summary}</p><small>{formatTime(item.updated_at || f.snapshot?.created_at, true)}</small></span><CaretRight size={16} /></button>; })}</div></aside></div>
    </section>
  </div>;
}

function MemoriesList({ memories, busy, onDisposition }) {
  if (!memories.length) return <EmptyState icon={Database} title="还没有项目记忆" detail="Codex 的可见活动同步后，会按项目保留在这里。" />;
  return <div className="memory-list">{memories.slice(0, 24).map((item) => { const active = !item.disposition || item.disposition.state !== "archived"; return <article key={item.activity_id || item.event_id} className={active ? "" : "archived"}><div className="memory-row-head"><span className="memory-type">{item.activity?.kind === "codex_visible_user_message" ? "用户消息" : item.activity?.kind === "codex_visible_assistant_message" ? "Codex 消息" : "工程活动"}</span><time>{formatTime(item.occurred_at || item.created_at, true)}</time></div><h3>{truncate(item.summary || item.activity?.summary, 180)}</h3><p>{truncate(item.activity?.detail || item.activity?.summary, 240)}</p><div className="memory-row-foot"><span>{active ? "参与正常召回" : "已归档，可恢复"}</span><button type="button" className="text-action" disabled={Boolean(busy)} onClick={() => onDisposition(item, active ? "archived" : "active")}>{active ? <><Archive size={15} />归档</> : <><ArrowClockwise size={15} />恢复</>}</button></div></article>; })}</div>;
}

function ProjectsPage({ projects, registeredProjects = projects, projectId, onProjectChange, data, health, busy, onDisposition, onExport, onToggleMonitor, onOrganize, onRefreshCurrent, onRefreshAll, onChanged, onImported }) {
  const [projectFilter, setProjectFilter] = useState("registered");
  const [projectSearch, setProjectSearch] = useState("");
  const selected = projects.find((item) => item.project_id === projectId) || null;
  const registeredSelected = registeredProjects.find((item) => item.project_id === projectId) || null;
  const selectorValue = registeredSelected ? projectId : "";
  const memories = Array.isArray(data?.memories) ? data.memories : [];
  const knowledge = data?.knowledge || {};
  const milestone = knowledge.milestone || {};
  const sampler = health?.ap_vibe?.codex_sampler || health?.codex_sampler || {};
  const catalog = data?.documents?.catalog || [];
  const curatedCount = catalog.filter(item => ["agent_reported", "user_edited", "local_reviewed_recovery"].includes(item.state)).length;
  const autoDetectedCount = catalog.filter(item => item.state === "auto_detected").length;
  const projectCounts = { registered: registeredProjects.length, archived: projects.filter(p => p.status === "archived").length, all: projects.length, maintained: projects.filter(item => item.documentation_quality === "ready").length, auto_detected: projects.filter(item => item.documentation_quality !== "ready" && item.documentation_state === "auto_detected").length, needs_curation: projects.filter(item => item.documentation_quality !== "ready" && item.documentation_state !== "auto_detected").length };
  const visibleProjects = useMemo(() => {
    const needle = projectSearch.trim().toLowerCase();
    return projects.filter(item => {
      if (projectFilter === "registered" && (item.status !== "active" || !registeredProject(item))) return false;
      if (projectFilter === "archived" && item.status !== "archived") return false;
      if (!["archived", "all"].includes(projectFilter) && item.status === "archived") return false;
      if (projectFilter === "maintained" && item.documentation_quality !== "ready") return false;
      if (projectFilter === "auto_detected" && !(item.documentation_quality !== "ready" && item.documentation_state === "auto_detected")) return false;
      if (projectFilter === "needs_curation" && !(item.documentation_quality !== "ready" && item.documentation_state !== "auto_detected")) return false;
      return !needle || (item.display_name + " " + (item.description || "")).toLowerCase().includes(needle);
    });
  }, [projectFilter, projectSearch, projects]);
  const qualityReady = selected?.documentation_quality === "ready";
  const selectedDescription = projectDescription(selected);
  return <div className="page-stack projects-page"><PageHeader eyebrow="项目与记忆" title="让每个项目，都有一本能接着用的说明书" description="项目是什么、想达到什么效果、为何这样设计、接下来做什么，都按章节保存。原始活动可以在页面底部展开。" action={<button type="button" className="secondary-button" onClick={onExport}><DownloadSimple size={16} />导出当前项目</button>} /><PortableProject onImported={onImported} />{!projects.some(p => p.status === "active" && p.documentation_quality === "ready") && <NoticeBanner tone="warn" title="项目档案仍有资料待补" detail="当前页面仍可监看会话和直接查询章节；自动识别的简介或未完成十维评估会标明证据不足。可以让 Codex 刷新最近使用的项目。" action={<button className="primary-button" onClick={() => onOrganize("project_refresh", registeredProjects.map(p => p.project_id))}>推荐：更新项目档案</button>} />}<section className="project-overview-grid"><div className="project-selector-panel"><SectionHeading eyebrow="当前项目" title={selected?.display_name || "等待项目"} description={selectedDescription} icon={FolderOpen} /><label className="select-field"><span>切换已登记项目 · {registeredProjects.length} 个</span><select value={selectorValue} onChange={(event) => event.target.value && onProjectChange(event.target.value)}><option value="">{registeredProjects.length ? "请选择已登记项目" : "暂无已登记项目"}</option>{registeredProjects.map((project) => <option value={project.project_id} key={project.project_id}>{project.display_name}</option>)}</select></label><div className="project-properties"><span><b>项目状态</b><StatusPill status={selected?.status || "unknown"} label={selected?.status === "active" ? "启用" : selected?.status === "archived" ? "已归档" : "未知"} /></span><span><b>自动监控</b><button type="button" className={"toggle-control " + (selected?.auto_monitor_enabled ? "on" : "")} aria-label="切换自动监控" disabled={selected?.status === "archived"} onClick={() => selected && onToggleMonitor(selected.project_id, !selected.auto_monitor_enabled)}><i />{selected?.auto_monitor_enabled ? "已开启" : "已关闭"}</button></span><span><b>逻辑观察</b><span>{selected?.logic_configured ? "已配置" : "未配置"}</span></span></div><div className="project-refresh-actions"><span className={"quality-badge " + (qualityReady ? "good" : "warn")}>{qualityReady ? "档案已整理" : "档案证据待补"}</span><button type="button" className="secondary-button" disabled={!selected || !registeredProject(selected)} onClick={() => onRefreshCurrent?.([selected.project_id])}>更新当前档案</button><button type="button" className="secondary-button" disabled={!registeredProjects.length} onClick={() => onRefreshAll?.(registeredProjects.map(p => p.project_id))}>更新全部档案</button></div></div><div className="project-metrics-panel"><div><span>已整理资料章节</span><strong>{data?.documents ? `${curatedCount} / 11` : "读取中…"}</strong><small>{autoDetectedCount ? `另有 ${autoDetectedCount} 章来自自动识别线索` : "点下方目录按需阅读"}</small></div><div><span>当前来源</span><strong>{data ? number(data.sources?.length) : "读取中…"}</strong><small>{sampler.configured ? "已发现 Codex 来源" : "尚未发现来源"}</small></div><div><span>Codex 整理次数</span><strong>{data?.documents ? number(data.documents.revision) : "读取中…"}</strong><small>{data?.documents?.updated_at ? formatTime(data.documents.updated_at, true) : "等待任务首次整理"}</small></div></div></section><ProjectKnowledge key={projectId} manifest={data?.documents} projectId={projectId} onUpdated={onChanged} /><details className="page-section raw-memory-details"><summary>原始活动与记忆管理 · {memories.length} 条</summary><SectionHeading eyebrow="自动采集 · 状态可见" title="最近保存的可见活动" description="归档只会改变正常召回范围，原始活动不会被物理删除。" icon={Database} action={<span className="section-count">{memories.length} 条已读</span>} /><MemoriesList memories={memories} busy={busy} onDisposition={onDisposition} /></details><PortableHistory history={data?.imported_history} /><section className="page-section knowledge-section"><SectionHeading eyebrow="本地恢复" title="知识版本与未审增量" description="本地版本可以恢复；正式 Yinzi Vibe 写入仍然关闭。" icon={ShieldCheck} /><div className="knowledge-overview"><div><span>当前版本</span><strong>{milestone.revision_number ? "第 " + milestone.revision_number + " 版" : "尚未建立"}</strong><small>{milestone.content_hash ? milestone.content_hash.slice(0, 16) + "…" : "没有可显示的 hash"}</small></div><div><span>未审增量</span><strong>{number(knowledge.latest_delta?.pending_count)} 条</strong><small>{knowledge.latest_delta?.summary || "没有新的未审摘要"}</small></div><div><span>正式写入</span><strong>关闭</strong><small>页面不会把本地结果冒充外部 Vibe 知识</small></div></div></section><section className="project-directory page-section"><SectionHeading eyebrow="项目目录与管理" title="所有项目在这里统一管理" description="这里与上方切换器使用同一份项目名单；归档可恢复，自动发现的工作区保留为待补资料线索。" icon={FolderOpen} action={<span className="section-count">{visibleProjects.length} / {projects.length}</span>} /><label className="search-field project-search"><MagnifyingGlass size={17} /><input value={projectSearch} onChange={(event) => setProjectSearch(event.target.value)} placeholder="搜索项目名称或简介" /></label><div className="project-filters" role="tablist" aria-label="项目资料状态"><button type="button" className={projectFilter === "registered" ? "active" : ""} onClick={() => setProjectFilter("registered")}>已登记项目 <b>{projectCounts.registered}</b></button><button type="button" className={projectFilter === "archived" ? "active" : ""} onClick={() => setProjectFilter("archived")}>已归档 <b>{projectCounts.archived}</b></button><button type="button" className={projectFilter === "all" ? "active" : ""} onClick={() => setProjectFilter("all")}>全部 <b>{projectCounts.all}</b></button><button type="button" className={projectFilter === "maintained" ? "active" : ""} onClick={() => setProjectFilter("maintained")}>资料已整理 <b>{projectCounts.maintained}</b></button><button type="button" className={projectFilter === "auto_detected" ? "active" : ""} onClick={() => setProjectFilter("auto_detected")}>自动识别 <b>{projectCounts.auto_detected}</b></button><button type="button" className={projectFilter === "needs_curation" ? "active" : ""} onClick={() => setProjectFilter("needs_curation")}>证据待补 <b>{projectCounts.needs_curation}</b></button></div>{visibleProjects.length ? <div className="project-directory-grid">{visibleProjects.map(item => <button type="button" key={item.project_id} className={"project-directory-item " + (item.project_id === projectId ? "selected" : "")} onClick={() => onProjectChange(item.project_id)}><span className="project-directory-status"><i className={item.documentation_quality === "ready" ? "maintained" : item.documentation_state === "auto_detected" ? "detected" : "pending"} />{item.status === "archived" ? "已归档 · 可恢复" : registeredProject(item) ? "已登记" : "自动发现 · 待补资料"}</span><strong>{item.display_name}</strong><p>{projectDescription(item)}</p><small>{item.document_revision ? "资料第 " + item.document_revision + " 版 · " + (item.maintained_section_count ?? "待刷新") + "/11章 · 已分析 " + (item.assessment_documented_count ?? 0) + "/10 · 已评分 " + (item.assessment_count ?? 0) + "/10" : "尚无 Codex 资料版本"}</small></button>)}</div> : <EmptyState icon={FolderOpen} title="没有匹配的项目" detail="换一个名称，或切换资料状态。" />}<div className="project-management-inline"><ProjectManagement key={projectId} project={selected && (registeredProject(selected) || selected.status === "archived") ? selected : null} embedded onChanged={onChanged} /></div></section><OrganizationButtons onOpen={onOrganize} /></div>;
}

function HelpPage({ onNavigate, onCopy }) {
  const helpCards = [
    { icon: Sparkle, title: "第一步：提出第一件事", when: "刚安装好，手里只有图片、素材或一句想法时。", steps: ["打开你常用的 Codex 或 Claude Code，新建一条任务。", "把图片拖进去，或直接说你想做什么；不需要先懂 Skill 名称。", "回到工作台，在首页点击这条任务标题查看消息与进展。"], example: "我是第一次使用。请先打开 AP-Vibe 工作台，再看这张图片，告诉我能怎么做；先分析素材，不上传或付费生成。" },
    { icon: ArrowRight, title: "第二步：换个会话继续同一个项目", when: "上一条会话关掉了、换了终端，或想在新窗口接着做同一个项目时。", steps: ["在新的 Codex 或 Claude Code 里说明要继续哪个项目。", "直接说这次想做什么；它会按需读取项目资料，保留原决定。", "在会话页按标题确认新会话已归到同一项目，历史仍在原会话保留。"], example: "继续这个项目，先说目前做到哪里，再完成下一步；保留原来的设计约束。" },
    { icon: Broadcast, title: "第三步（可选）：让工作室伙伴一起做", when: "一件事明显能拆成几块，或你想让别的模型帮忙检查成果时。", steps: ["在 Agent 工作室按推荐模板添加伙伴并填写 Key；其它服务可调整地址和模型。", "打开“自动 Agent 协作”，再用自然语言交代值得拆分的整件事。", "在任务与输出看成果文件，用故事回放看经过；简单的事由原任务直接完成，不必拆。"], example: "这件事可以拆给工作室伙伴：请安排合适的分工并各自交出文件，最后让另一位独立检查成果，再把结论告诉我。" },
    { icon: House, title: "首页：只看现场", when: "想知道现在有没有 Codex 正在工作，或服务是否在线时。", steps: ["看顶部四个数字，先确认服务在线。", "优先点“近期活跃的会话”。", "需要完整内容时，进入会话页。"], example: "我想知道刚才哪条 Codex 还在继续工作。" },
    { icon: UsersThree, title: "会话：按标题回看", when: "你记得任务名字，但不知道消息属于哪条 Codex 会话时。", steps: ["在左侧搜索任务标题或项目名。", "点击一张会话卡片。", "在右侧向上或向下滚动查看历史。"], example: "打开“修复支付回调”的会话，帮我接着看上次结论。" },
    { icon: GitBranch, title: "逻辑观察：查清代码关系", when: "你想知道一个文件负责什么、改动会影响谁，或者一条预期链路先断在哪里时。", steps: ["选择项目，默认使用“交给 Codex 分析”。", "用中文描述问题；若需要 Python 静态关系，再切换本地查询并填写限定名。", "查看进度和报告，先看结论，再看证据、未知项和历史。"], example: "请梳理当前项目从用户点击开始到结果保存的流程，指出可能中断的位置，并保留旧档案后更新相关章节。" },
    { icon: Database, title: "项目与记忆：项目说明书", when: "你想知道项目做什么、为什么这样设计、哪些完成了，或有哪些薄弱环节时。", steps: ["切换到正确项目，先看简介与用户目标。", "按目录打开需要的章节；资料由 Codex 在工作中整理。", "查看十维雷达图，点击每项了解评分理由与改进建议。"], example: "请使用 AP-Vibe 读取这个项目的资料目录，核对简介、红线与待办；完成工作后更新相关章节，评分必须附理由和证据。" },
    { icon: Brain, title: "进阶 · AP 认知：按需尝试", when: "想查看感知、行动回读，或试用外部模型辅助认知时。", steps: ["先看基础模式的实际记录，不配置 Key 也可以使用。", "需要增强时，填写服务商地址、Key 和真实模型名称；阅读费用提示后开启。", "查看实际调用与采用记录，效果不好时可关闭并继续使用本地功能。"], example: "请解释 AP-Vibe 最近一次感知、行动和回读；指出已发生的行为和仍未验证的收益。" },
  ];
  return <div className="page-stack help-page"><PageHeader eyebrow="帮助" title="不用懂技术，也能知道下一步做什么" description="每项能力都写清楚了用途、适用时机、操作步骤和可以直接复制给 Codex 的示例。" action={<button type="button" className="secondary-button" onClick={() => onNavigate("home")}><House size={16} />回到首页</button>} /><section className="help-hero"><div className="help-hero-icon"><Sparkle size={28} weight="duotone" /></div><div><span className="eyebrow">第一次使用</span><h2>把 AP‑Vibe 当成一个项目现场管家</h2><p>它会在本机观察 Codex 与 Claude Code 的可见消息，按项目分开保存，并把最近发生了什么、为什么这样处理、哪些内容仍不确定，用中文展示给你。</p></div></section><section className="help-grid">{helpCards.map((card) => { const CardIcon = card.icon; return <article className="help-card" key={card.title}><span className="help-card-icon"><CardIcon size={21} weight="duotone" /></span><h2>{card.title}</h2><div><span>什么时候用</span><p>{card.when}</p></div><div><span>怎么做</span><ol>{card.steps.map((step) => <li key={step}>{step}</li>)}</ol></div><div className="help-example"><span>示例口令</span><code>{card.example}</code><button type="button" className="text-action" onClick={() => onCopy(card.example)}><Copy size={15} />复制示例</button></div></article>; })}</section><section className="help-card troubleshooting"><h2>遇到问题，照着做</h2><details><summary>有任务标题，为什么没有消息？</summary><p>任务已发现，但当前采集窗口没有读到可见文本。先让该任务继续一次，再等约十秒；仍为空时，把“请检查 AP-Vibe 的会话采集与游标状态”发给 Codex。</p></details><details><summary>如何发消息给任务？</summary><p>会话页先选对标题，填写消息，再点击“发送给 Codex”。任务正在执行时消息会排队，空闲后自动续接；你可以撤回尚未发送的消息。若出现“需要查看结果”，请先打开原任务确认，不要重复发送。</p></details><details><summary>页面提示数据未更新怎么办？</summary><p>先点重新连接。页面会保留上一次成功数据；如果仍未恢复，双击桌面 AP-Vibe 启动器，或把“请使用安装配置恢复 AP-Vibe 本地服务，并打开它实际监听的工作台地址”发给 Codex。</p></details><details><summary>怎样知道 AP-Vibe 真正帮助了任务？</summary><p>可以让 Codex 报告“读取了哪些项目记忆、采用了什么、具体减少了哪次重复解释或返工”。活跃数量和消息曲线只反映活动，不能直接当作质量提升。</p></details></section><section className="help-boundaries"><SectionHeading eyebrow="看懂状态" title="几个容易误解的词" description="这些提示是为了让你知道 AP‑Vibe 做到了什么，以及还没有做到什么。" icon={Info} /><div className="boundary-grid"><div><StatusPill status="running" label="近期活跃" /><p>最近 15 分钟内发现了新的可见消息。</p></div><div><StatusPill status="stopped" label="暂未活动" /><p>最近没有新消息，历史仍然可以打开。</p></div><div><StatusPill status="ready" label="静态链路已观察" /><p>看到了源码中的关系，不代表真实运行一定正确。</p></div><div><StatusPill status="warn" label="显示上一次现场" /><p>刷新失败时保留上一次成功数据，不把失败误显示成零。</p></div></div></section></div>;
}

function Sidebar({ page, onNavigate, project, health, sessions, loading = false }) {
  page = page === "organization" ? "projects" : page;
  return <aside className="app-sidebar"><div className="brand"><span className="brand-mark"><Brain size={24} weight="duotone" /></span><div><strong>AP‑Vibe</strong><small>项目心智工作台</small></div></div><div className="sidebar-status"><StatusPill status={health?.status || "unknown"} label={healthLabel(health?.status)} /><span>{loading ? (page === "home" ? '正在读取会话…' : '会话页查看全部任务') : sessions.length + ' 条 Codex 监看来源'}</span></div><nav className="main-nav" aria-label="主导航">{NAV_ITEMS.map(({ id, label, description, icon: Icon }) => <button type="button" key={id} className={page === id ? "active" : ""} onClick={() => onNavigate(id)}><span className="nav-icon"><Icon size={20} weight="duotone" /></span><span><b>{label}</b><small>{description}</small></span>{page === id && <span className="nav-current" />}</button>)}</nav><div className="sidebar-project"><span className="eyebrow">当前项目</span><strong>{project?.display_name || "等待项目"}</strong><small>{!project ? "正在读取项目…" : project.auto_monitor_enabled ? "自动监控已开启" : "自动监控已关闭"}</small><button type="button" className="text-action" onClick={() => onNavigate("projects")}><GearSix size={15} />管理项目</button></div><div className="sidebar-foot"><ShieldCheck size={17} /><span><b>本地与有界</b><small>隐藏推理不会被采集</small></span></div></aside>;
}

function Topbar({ page, project, health, refreshing, onRefresh, onSync, syncBusy }) {
  const current = page === "organization" ? { label: "档案整理", description: "项目与记忆 / Codex 整理进度" } : NAV_ITEMS.find((item) => item.id === page) || NAV_ITEMS[0];
  return <header className="topbar"><div className="topbar-title"><span className="mobile-brand">AP‑Vibe</span><span className="topbar-separator">/</span><strong>{current.label}</strong><span>{current.description}</span></div><div className="topbar-actions"><span className="topbar-project"><FolderOpen size={16} />{project?.display_name || "等待项目"}</span><StatusPill status={health?.status || "unknown"} label={healthLabel(health?.status, true)} /><button type="button" className="secondary-button compact-button" onClick={onSync} disabled={syncBusy}><ArrowClockwise size={16} className={syncBusy ? "spin" : ""} />{syncBusy ? "同步中" : "同步 Codex"}</button><IconButton label="刷新页面数据" onClick={onRefresh} disabled={refreshing}>{refreshing ? <CircleNotch size={19} className="spin" /> : <ArrowClockwise size={19} />}</IconButton></div></header>;
}

function Toast({ notice, onClose }) {
  if (!notice) return null;
  const ToastIcon = notice.tone === "good" ? CheckCircle : notice.tone === "bad" ? WarningCircle : Info;
  return <div className={"toast " + (notice.tone || "info")} role="status"><span className="toast-icon"><ToastIcon size={20} /></span><div><strong>{notice.title}</strong><p>{notice.detail}</p></div><IconButton label="关闭提示" onClick={onClose}><X size={16} /></IconButton></div>;
}

class PageErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error) {
    // Keep a broken project card from taking down the entire workbench.  The
    // browser console retains the technical detail for diagnosis.
    console.error("AP-Vibe 页面渲染失败", error);
  }

  render() {
    if (this.state.error) {
      return <NoticeBanner tone="warn" title="这一页暂时没有显示完整" detail="当前项目的数据格式或连接正在恢复，其他页面仍可继续使用。请刷新本页；原始项目和历史记录没有被删除。" action={<button type="button" className="text-action" onClick={() => this.setState({ error: null })}>重试显示</button>} />;
    }
    return this.props.children;
  }
}

export function App() {
  const cached = useMemo(() => readWorkbenchCache(localStorage), []);
  const [page, setPage] = useState(() => readPreference(localStorage, PAGE_KEY, "home"));
  useEffect(() => { if (page !== "organization") savePreference(localStorage, PAGE_KEY, page); }, [page]);
  const [organizationScope,setOrganizationScope] = useState("recent_unclassified");
  const [organizationProjectIds, setOrganizationProjectIds] = useState([]);
  const openOrganization = (scope, projectIds = []) => { setOrganizationScope(scope); setOrganizationProjectIds(projectIds); setPage("organization"); };
  const [projects, setProjects] = useState((cached?.projects || []));
  const [projectId, setProjectId] = useState(() => readPreference(localStorage, PROJECT_KEY, cached?.projectId || ""));
  const [health, setHealth] = useState(cached?.health || null);
  const [state, setState] = useState(cached?.state || null);
  const [data, setData] = useState(cached?.data || null);
  const [overview, setOverview] = useState(cached?.overview || null);
  const [stale, setStale] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [notice, setNotice] = useState(null);
  const [selectedSession, setSelectedSession] = useState(null);
  const [drafts, setDrafts] = useState({});
  const getDraft = session => drafts[sessionDraftKey(session)] || "";
  const changeDraft = (session, value) => setDrafts(previous => ({ ...previous, [sessionDraftKey(session)]: value }));
  const [logicModal, setLogicModal] = useState(null);
  const [busy, setBusy] = useState("");
  const showNotice = useCallback((tone, title, detail) => setNotice({ tone, title, detail }), []);

  const snapshotRef = useRef(cached || {});
  const currentProjectRef = useRef(projectId);
  currentProjectRef.current = projectId;
  const pendingRef = useRef(null);
  const requestsRef = useRef(createProjectRequests());
  const changeProject = useCallback((nextId) => { if (nextId === currentProjectRef.current) return; requestsRef.current.cancel(); pendingRef.current = null; currentProjectRef.current = nextId; savePreference(localStorage, PROJECT_KEY, nextId); snapshotRef.current = { ...snapshotRef.current, projectId: nextId, state: null, data: null }; setProjectId(nextId); setState(null); setData(null); setLogicModal(null); setStale(false); }, []);
  const load = useCallback((quiet = true) => {
    if (pendingRef.current?.projectId === projectId && pendingRef.current?.page === page) return pendingRef.current.promise;
    const token = requestsRef.current.begin(projectId);
    const read = url => requestJson(url, { signal: token.controller.signal });
    const publish = (name, setter) => value => {
      if (requestsRef.current.isCurrent(token) && currentProjectRef.current === projectId) {
        setter(value);
        snapshotRef.current = { ...snapshotRef.current, projectId, [name]: value };
      }
      return value;
    };
    const promise = (async () => {
      setRefreshing(true);
      const healthRequest = read("/v1/health").then(publish("health", setHealth));
      const catalogue = read(API + "/projects?include_archived=true").then(async value => {
        const liveHealth = await healthRequest.catch(() => null);
        if (requestsRef.current.isCurrent(token) && currentProjectRef.current === projectId) {
          setProjects(value.projects || []);
          snapshotRef.current = { ...snapshotRef.current, projects: value.projects || [] };
          const preferred = value.default_project_id || liveHealth?.ap_vibe?.codex_sampler?.project_id;
          const nextId = availableProjectId(value.projects, projectId, preferred);
          if (nextId !== projectId) changeProject(nextId);
        }
        return value;
      });
      const readSelected = async (name, setter) => {
        const value = await catalogue;
        if (!requestsRef.current.isCurrent(token) || !projectId || !value.projects?.some(p => p.project_id === projectId)) return null;
        return read(API + "/" + name + "?project_id=" + encodeURIComponent(projectId)).then(publish(name, setter));
      };
      const results = await Promise.allSettled([
        healthRequest,
        catalogue,
        ["home", "logic", "cognition"].includes(page) ? readSelected("state", setState) : Promise.resolve(snapshotRef.current.state || null),
        page === "projects" ? readSelected("data", setData) : Promise.resolve(snapshotRef.current.data || null),
        page === "home" ? read(API + "/codex/overview?include_archived=false&limit=128").then(publish("overview", setOverview)) : Promise.resolve(snapshotRef.current.overview || null),
      ]);
      if (!requestsRef.current.isCurrent(token) || currentProjectRef.current !== projectId || pendingRef.current?.token !== token) return;
      const next = selectSnapshot(snapshotRef.current, projectId, results);
      snapshotRef.current = next;
      setHealth(next.health || null); setProjects(next.projects || []);
      setState(next.state || null); setData(next.data || null); setOverview(next.overview || null);
      setStale(next.stale);
      saveWorkbenchCache(localStorage, next);
      if (next.stale && !quiet) showNotice("bad", "部分数据未能刷新", "已保留同一项目的上一次现场，连接恢复后会自动更新。");
      setRefreshing(false);
    })().finally(() => { if (pendingRef.current?.token === token) pendingRef.current = null; });
    pendingRef.current = { projectId, page, promise, token };
    return promise;
  }, [projectId, page, showNotice, changeProject]);
  useEffect(() => {
    let cancelled = false, timer;
    const poll = async () => {
      window.clearTimeout(timer);
      if (document.hidden || cancelled) return;
      await load(true);
      if (!cancelled && !document.hidden) timer = window.setTimeout(poll, 10000);
    };
    const visibility = () => {
      window.clearTimeout(timer);
      if (!document.hidden) poll();
    };
    document.addEventListener("visibilitychange", visibility);
    poll();
    return () => { cancelled = true; window.clearTimeout(timer); document.removeEventListener("visibilitychange", visibility); requestsRef.current.cancel(); pendingRef.current = null; };
  }, [load]);

  const sessions = useMemo(() => Array.isArray(overview?.sessions) ? overview.sessions : [], [overview]);
  const registeredProjects = useMemo(() => projects.filter(item => item.status === "active" && registeredProject(item)), [projects]);
  const episodes = useMemo(() => Array.isArray(state?.episodes) ? state.episodes : [], [state]);
  const logicRecords = useMemo(() => state?.logic_history || episodes.filter(item => Boolean(getEpisode(item).logic_field)), [state, episodes]);
  const currentProject = projects.find((item) => item.project_id === projectId) || state?.project || null;

  const navigate = useCallback((nextPage) => { setPage(nextPage); window.scrollTo({ top: 0, behavior: "smooth" }); }, []);
  const openSession = useCallback((session) => { setSelectedSession(session); setPage("sessions"); }, []);
  const openEvent = useCallback((item) => { const episode = getEpisode(item); const session = sessionForActivity(sessions, getActivity(episode)); if (session) { openSession(session); return; } if (episode.episode_kind === "logic_field_observation" || episode.logic_field) { setLogicModal(item); setPage("logic"); } else showNotice("info", "这是一条 AP 工程事件", "进入会话页可以查看对应 Codex 可见消息；当前事件本身没有稳定的会话来源。"); }, [showNotice, sessions, openSession]);

  const syncCodex = useCallback(async () => {
    if (busy) return;
    setBusy("sync");
    try { const result = await requestJson(API + "/codex/sync", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ project_id: projectId }) }); await load(true); showNotice("good", result.processed_count ? "已同步 " + result.processed_count + " 条新消息" : "已经同步到最新位置", result.processed_count ? "新消息已进入 AP 主流程；隐藏推理和工具载荷没有被采集。" : "没有重复生成认知事件。"); }
    catch (error) { showNotice("bad", error.message || "同步失败", error.solution || "确认 daemon 在线后重试。"); }
    finally { setBusy(""); }
  }, [busy, load, projectId, showNotice]);

  const submitLogic = useCallback(async ({ kind, target, expectedPath }) => {
    setBusy("logic");
    const requestId = uid("logic-request");
    try { const result = await requestJson(API + "/logic/query", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ request_id: requestId, query: { query_id: requestId, project_id: projectId, kind, target, expected_path: kind === "first_broken_link" ? expectedPath.split(/(?:->|→|\n)/).map(item => item.trim()).filter(Boolean) : [], max_nodes: 48, max_edges: 96, max_depth: 4, include_tests: true, source_ref: "user://ap-vibe/logic/" + requestId } }) }); await load(true); if (result.episode) setLogicModal(result.episode); showNotice("good", "局部逻辑观察已完成", "打开最新报告可以同时查看结论、证据和历史上下文。"); }
    catch (error) { showNotice("bad", error.message || "观察失败", error.solution || "请确认目标在项目逻辑根目录内。"); }
    finally { setBusy(""); }
  }, [load, projectId, showNotice]);

  const handoff = useCallback(async (session, message) => {
    const value = message.trim();
    if (!value) return;
    try {
      if (!navigator.clipboard?.writeText) throw new Error("当前浏览器不允许写入剪贴板。");
      await navigator.clipboard.writeText(value);
      showNotice("good", "消息已复制到剪贴板", "页面现在尝试打开原 Codex 会话；如果浏览器拦截协议，请手动打开会话并粘贴发送。");
      const link = document.createElement("a");
      link.href = "codex://threads/" + encodeURIComponent(session.session_id || "");
      link.target = "_blank";
      link.rel = "noreferrer";
      document.body.appendChild(link);
      window.setTimeout(() => { link.click(); link.remove(); }, 80);
    } catch (error) { showNotice("bad", "复制没有完成", error.message || "请手动复制输入框文字，再打开对应 Codex 会话。"); }
  }, [showNotice]);

  const setDisposition = useCallback(async (item, disposition) => {
    if (busy) return;
    setBusy("memory:" + item.activity_id);
    try { await requestJson(API + "/memories/disposition", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ request_id: uid("memory-disposition"), project_id: projectId, activity_id: item.activity_id, state: disposition, reason: disposition === "archived" ? "用户在项目与记忆页将这条记忆暂时移出正常召回" : "用户在项目与记忆页恢复这条记忆参与召回", source_ref: "user://ap-vibe/project-data" }) }); await load(true); showNotice("good", disposition === "archived" ? "记忆已归档" : "记忆已恢复", "原始活动仍然保留，之后可以再次改变处置状态。"); }
    catch (error) { showNotice("bad", error.message || "处置失败", error.solution || "刷新项目数据后重试。"); }
    finally { setBusy(""); }
  }, [busy, load, projectId, showNotice]);
  const exportProject = useCallback(async () => {
    try { const response = await fetch(API + "/portable/export?project_id=" + encodeURIComponent(projectId), { signal: AbortSignal.timeout(30000) }); if (!response.ok) { const body = await response.json(); throw new Error(body.error?.message || "导出未完成"); } const originalJson = await response.text(); const blob = new Blob([originalJson], { type: "application/json" }); const url = URL.createObjectURL(blob); const link = document.createElement("a"); link.href = url; link.download = "ap-vibe-" + projectId + ".json"; link.click(); URL.revokeObjectURL(url); showNotice("good", "项目数据已导出", "导出包经过本地脱敏，适合保存或在另一台机器上预览导入。"); }
    catch (error) { showNotice("bad", error.message || "导出失败", error.solution || "确认当前项目可导出后重试。"); }
  }, [projectId, showNotice]);
  const toggleMonitor = useCallback(async (id, enabled) => {
    try { await requestJson(API + "/projects/monitor", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ project_id: id, enabled }) }); await load(true); showNotice("good", enabled ? "自动监控已开启" : "自动监控已暂停", enabled ? "之后的新可见消息会继续进入这个项目。" : "已经保存的历史仍然可以查看。"); }
    catch (error) { showNotice("bad", error.message || "设置失败", error.solution || "刷新项目后重试。"); }
  }, [load, showNotice]);
  const refreshCurrent = useCallback((ids) => openOrganization("project_refresh", ids), []);
  const refreshAll = useCallback((ids) => openOrganization("project_refresh", ids), []);
  const installDesktopLauncher = useCallback(async () => {
    try {
      const result = await requestJson(API + "/launcher/desktop", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ request_id: uid("desktop-launcher") }) });
      showNotice("good", "桌面启动器已准备好", `快捷方式：${result.shortcut_path || "当前用户桌面/AP-Vibe.lnk"}。之后双击即可自动启动或打开本机工作台。`);
    } catch (error) { showNotice("bad", error.message || "桌面启动器没有创建", error.solution || "确认当前服务由 AP-Vibe 安装脚本管理后重试。"); }
  }, [showNotice]);

  let content;
  if (page === "sessions") content = <SessionArchive mode="sessions" projects={projects} getDraft={getDraft} changeDraft={changeDraft} requestedSession={selectedSession} onSelectionChange={setSelectedSession} />;
  else if (page === "logic") content = <LogicWorkbench records={logicRecords} project={currentProject} projects={registeredProjects} onProjectChange={changeProject} busy={busy === "logic"} onQuery={submitLogic} onChanged={() => load(true)} renderRecord={record => <LogicObservation key={record.request_id} record={record} onOpen={() => setLogicModal(record)} />} />;
  else if (page === "cognition") content = <CognitionPage state={state} project={currentProject} />;
  else if (page === "agents") content = <AgentStudio projects={registeredProjects} project={currentProject} currentProjectId={projectId} projectsLoading={refreshing && !projects.length} />;
  else if (page === "organization") content = <OrganizationPage initialScope={organizationScope} initialProjectIds={organizationProjectIds} projects={registeredProjects} onBack={() => setPage("projects")} onChanged={() => load(false)} />;
  else if (page === "projects") content = <ProjectsPage projects={projects} registeredProjects={registeredProjects} projectId={projectId} onProjectChange={changeProject} data={data} health={health} busy={busy} onDisposition={setDisposition} onExport={exportProject} onToggleMonitor={toggleMonitor} onOrganize={openOrganization} onRefreshCurrent={refreshCurrent} onRefreshAll={refreshAll} onChanged={() => load(false)} onImported={async id => { await load(false); changeProject(id); }} />;
  else if (page === "help") content = <HelpPage onNavigate={navigate} onCopy={async value => { try { await navigator.clipboard.writeText(value); showNotice("good", "示例已复制", "可以粘贴给 Codex 使用。"); } catch { showNotice("bad", "复制未成功", "请选中示例文字手动复制。"); } }} />;
  else content = <HomePage health={health} sessions={sessions} events={episodes} state={state} loading={!overview} stale={stale} onRefresh={() => load(false)} onOpenSession={openSession} onOpenEvent={openEvent} onInstallLauncher={installDesktopLauncher} />;

  return <div className="app-shell"><Sidebar page={page} onNavigate={navigate} project={currentProject} health={health} sessions={sessions} loading={!overview} /><div className="app-content"><Topbar page={page} project={currentProject} health={health} refreshing={refreshing} onRefresh={() => load(false)} onSync={syncCodex} syncBusy={busy === "sync"} /><main className="main-content">{stale && <NoticeBanner tone="warn" title="部分数据暂未更新，保留上一次现场" detail="正在重新连接。当前显示的内容可能已过期；项目切换后不会显示其他项目的数据。" action={<button className="text-action" onClick={() => load(false)}>重新连接</button>} />}<PageErrorBoundary key={["sessions", "agents", "help", "organization", "projects"].includes(page) ? page : page + ":" + projectId}>{content}</PageErrorBoundary></main><footer className="app-footer"><span><ShieldCheck size={15} />数据留在本机，页面只展示已经读取到的可见消息</span><span>{data?.knowledge?.milestone?.revision_number ? "本地恢复版本 " + number(data.knowledge.milestone.revision_number) : "本地项目资料 · 按需读取"}</span></footer></div>{logicModal && <LogicModal project={currentProject} selected={logicModal} history={logicRecords} onClose={() => setLogicModal(null)} onSelect={setLogicModal} />}<Toast notice={notice} onClose={() => setNotice(null)} /></div>;
}
