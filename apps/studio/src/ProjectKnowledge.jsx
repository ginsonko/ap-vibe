import { assessmentCoverage } from "./dossier-presentation.mjs";
import { DocumentEditor } from "./DocumentEditor";
import { useEffect, useState } from "react";
import { MessageContent } from "./MessageContent";
import { readProjectJson } from "./project-request.mjs";

const FIELD_NAMES = {
  name: "项目名称", summary: "概述", audience: "面向谁", purpose: "用途", goals: "预期效果", acceptance: "验收标准", redlines: "设计红线",
  flow: "工作流程", modules: "模块职责", intentional_design: "刻意设计与理由", documents: "文档入口", title: "名称", path: "位置", items: "详细记录",
  id: "记录编号", status: "状态", old_logic: "原来的逻辑", new_logic: "现在的逻辑", reason: "理由", evidence_refs: "依据", source_refs: "来源",
  completed: "已完成", remaining: "待完成", not_implemented: "尚未实现", blocked: "当前阻碍", next_action: "下一步", incidents: "事故与教训",
  checks: "验证结果", discoveries: "新发现", unknown: "仍不确定", phase: "当前阶段", entry_points: "恢复入口", do_not_repeat: "无需重复",
  risk: "风险", improvement: "改进方向", notes: "备注", rationale: "设计原因", conclusion: "结论", result: "结果", trigger: "触发条件", impact: "影响",
  fix: "修复方式", lesson: "经验", changed_at: "变更时间", entry: "查询入口", version: "版本", authority: "资料来源", project_id: "项目编号",
  vibe_formal_write: "正式 Vibe 写入", reviewed_at: "已审时间", confidence: "记录置信度", uncertainty: "不确定性", conflicts: "冲突",
  source_completeness: "来源完整度", resolved_remaining: "已解决待办", resolved_unknown: "已查明问题", evidence: "证据", sources: "来源", requirement: "需求",
  provenance: "整理来源", source_files: "参考文件",
};
Object.assign(FIELD_NAMES, {code_roots:"代码目录",connections:"连接与服务器",server:"服务器或访问地址",method:"连接方法",credential_location:"密钥存放位置（不含内容）",configuration_path:"配置文件位置",configuration_location:"配置位置",server_location:"服务器目录",helper_path:"连接脚本",release:"发布目录",rollback:"回滚入口",data_location:"数据目录",application_credentials:"应用凭据管理",verification:"核对范围",exists_at_curation:"整理时文件存在",as_of:"状态时间",previous_pending_snapshots:"历史待办快照",superseded_at:"被更新的时间",related_session_ids:"关联任务",observed_at:"核对时间",boundary:"证据范围",choice:"设计选择",refs:"证据入口"});
const VALUES = { true: "是", false: "否", active: "当前采用", completed: "已完成", superseded: "已由新方案替代", revoked: "已撤销", partial: "部分完成", pending: "待处理", planned: "已计划", complete: "完整", unknown: "未知", agent_reported: "Codex 整理", local_reviewed_recovery: "本地已审记录", auto_detected: "自动整理线索", unverified: "证据待补" };
Object.assign(VALUES, { evidence_reviewed: "已结合参考资料整理", verified_against_visible_project_evidence: "已对照当前可见项目资料" });
const DIMENSIONS = [
  ["intent", "目标一致"], ["logic", "逻辑闭合"], ["completeness", "完整程度"], ["reliability", "可靠性"], ["security", "安全性"],
  ["performance", "性能"], ["maintainability", "可维护性"], ["compatibility", "兼容性"], ["usability", "易用性"], ["validation", "验证充分"],
];

function objectValue(value) { return value && typeof value === "object" && !Array.isArray(value) ? value : {}; }

function normalizedAssessment(value) {
  const source = Array.isArray(value) ? value : [];
  return DIMENSIONS.map(([key, label]) => {
    const item = source.find(entry => entry && typeof entry === "object" && entry.key === key) || {};
    const score = typeof item.score === "number" && Number.isFinite(item.score) ? Math.max(0, Math.min(100, item.score)) : null;
    return { ...item, key, label: typeof item.label === "string" && item.label.trim() ? item.label : label, score,
      reason: typeof item.reason === "string" && item.reason.trim() ? item.reason : "目前没有足够的证据给出分数，先保留为未知。",
      risk: typeof item.risk === "string" ? item.risk : "证据不足，暂不能判断真实风险。",
      improvement: typeof item.improvement === "string" ? item.improvement : "继续从真实代码、运行记录或验收结果补充证据。",
      evidence_refs: Array.isArray(item.evidence_refs) ? item.evidence_refs.filter(ref => typeof ref === "string") : [],
    };
  });
}

Object.assign(FIELD_NAMES, { user_note:'用户备注' });

function StructuredValue({ value, level = 0 }) {
  if (value == null) return <span className="muted">暂无记录</span>;
  if (Array.isArray(value)) return value.length ? <div className="document-list">{value.map((item, i) => <div className="document-list-item" key={i}><span className="document-item-number">{String(i + 1).padStart(2, "0")}</span><StructuredValue value={item} level={level + 1} /></div>)}</div> : <span className="muted">当前没有记录</span>;
  if (typeof value === "object") return <dl className={"document-fields level-" + Math.min(level, 2)}>{Object.entries(value).filter(([key]) => key !== "assessment").map(([key, item]) => <div key={key}><dt>{FIELD_NAMES[key] || key}</dt><dd><StructuredValue value={item} level={level + 1} /></dd></div>)}</dl>;
  return <MessageContent annotations={false} text={VALUES[String(value)] || String(value)} />;
}

function Radar({ values, selected, onSelect }) {
  const cx = 205, cy = 190, radius = 123;
  const safeValues = Array.isArray(values) && values.length ? values : normalizedAssessment([]);
  const point = (i, ratio) => { const a = i * Math.PI * 2 / safeValues.length - Math.PI / 2; return [cx + Math.cos(a) * radius * ratio, cy + Math.sin(a) * radius * ratio]; };
  const assessed = safeValues.filter(item => typeof item.score === "number");
  return <svg className="assessment-radar" viewBox="0 0 410 380" role="img" aria-label={`项目十维评估雷达图，${assessed.length} 个维度已评估，满分一百分`}>
    {[.25, .5, .75, 1].map(r => <polygon className="radar-grid" key={r} points={safeValues.map((_, i) => point(i, r).join(",")).join(" ")} />)}
    {safeValues.map((item, i) => { const end = point(i, 1), label = point(i, 1.25); return <g key={item.key}><line className="radar-axis" x1={cx} y1={cy} x2={end[0]} y2={end[1]} /><text x={label[0]} y={label[1]} textAnchor="middle" dominantBaseline="central">{item.label}</text></g>; })}
    {assessed.length === safeValues.length && <polygon className="radar-area" points={safeValues.map((item, i) => point(i, item.score / 100).join(",")).join(" ")} />}
    {safeValues.map((item, i) => { if (typeof item.score !== "number") return null; const p = point(i, item.score / 100); return <g key={item.key}><circle className={selected === item.key ? "radar-point selected" : "radar-point"} cx={p[0]} cy={p[1]} r={selected === item.key ? 6 : 4} onClick={() => onSelect(item.key)}><title>{item.label}：{item.score} 分 · {item.reason}</title></circle></g>; })}
    {!assessed.length && <text className="radar-empty" x={cx} y={cy} textAnchor="middle">暂无数值评分</text>}
    {assessed.length > 0 && assessed.length < safeValues.length && <text className="radar-empty" x={cx} y={cy} textAnchor="middle">已评分 {assessed.length} / {safeValues.length}</text>}
    <text className="radar-scale" x={cx + 5} y={cy - radius + 12}>100</text>
  </svg>;
}

export function ProjectKnowledge({ manifest, projectId, onUpdated }) {
  const [editing, setEditing] = useState(false);
  const [section, setSection] = useState("identity");
  const [loaded, setLoaded] = useState(null);
  const [error, setError] = useState("");
  const [dimension, setDimension] = useState("intent");
  const [retry, setRetry] = useState(0);
  const catalog = Array.isArray(manifest?.catalog) ? manifest.catalog.filter(item => item && typeof item === "object" && typeof item.key === "string") : [];
  const selected = catalog.find(item => item.key === section) || catalog[0] || null;
  const sectionKey = selected?.key || section;
  const scores = normalizedAssessment(manifest?.assessment);
  const score = scores.find(item => item.key === dimension) || scores[0];
  const complete = catalog.filter(item => item.state === "user_edited" || item.state === "agent_reported" || item.state === "local_reviewed_recovery").length;
  const autoDetected = catalog.filter(item => item.state === "auto_detected").length;
  const missing = catalog.filter(item => !item.available).length;
  const coverage = assessmentCoverage(manifest?.assessment);
  // The legacy documentation_state only means that a document revision exists;
  // it never proves that the dossier is actually curated.  Use the strict
  // read-side quality projection from the current backend contract.
  const project = objectValue(manifest?.project);
  const quality = project.documentation_quality === "ready" ? "ready" : "needs_curation";
  const qualityIssues = Array.isArray(project.quality_issues) ? project.quality_issues : [];
  useEffect(() => {
    if (!selected?.read_url) { setLoaded(null); setError(""); return undefined; }
    const controller = new AbortController();
    let active = true;
    setLoaded(null); setError(""); setEditing(false);
    readProjectJson(selected.read_url, projectId, { signal: controller.signal }).then(body => {
      if (active) setLoaded(body);
    }).catch(error => { if (active && error.name !== "AbortError") setError(error.message); });
    return () => { active = false; controller.abort(); };
  }, [projectId, sectionKey, manifest?.revision, manifest?.reviewed_revision, selected?.read_url, retry]);
  if (!manifest || manifest.project_id !== projectId) return <section className="page-section" role="status"><h2>正在读取项目档案</h2><p className="muted">项目目录已切换，正在按需读取这个项目的资料…</p></section>;
  return <>
    {manifest.import_origin && <p className="notice-banner">这份档案来自导入包（原项目第 {manifest.import_origin.revision} 版）。保留原章节来源与结论；本机尚未重新验证这些结论。请按需要补充此电脑的目录和新进展。</p>}
    <section className="page-section assessment-section"><div className="assessment-heading"><div><span className="eyebrow">十个方向，找出薄弱环节</span><h2>项目对抗性评估</h2><p>分数由 Codex 基于证据整理，满分 100。文字评估和数值评分分别统计；未知不按 0 分计算。</p></div><div className="assessment-heading-meta"><span className={"quality-badge " + (quality === "ready" ? "good" : "warn")}>{quality === "ready" ? "资料已整理" : "可直接查询 · 证据待补"}</span><span className="section-count">已记录 {coverage.documented} / 10 · 已评分 {coverage.scored} / 10</span></div></div><div className="assessment-grid"><div><Radar values={scores} selected={dimension} onSelect={setDimension} /><p className="chart-footnote">图表表示记录中的项目评估，不代表模型能力提升。未评分维度不绘制面积；点击维度可查看已有分析和证据。</p></div><div><div className="dimension-grid">{scores.map(item => <button className={dimension === item.key ? "active" : ""} key={item.key} onClick={() => setDimension(item.key)}><span>{item.label}</span><strong>{typeof item.score === "number" ? item.score : "暂无分数"}</strong></button>)}</div>{score && <article className="dimension-detail"><h3>{score.label} <span>{typeof score.score === "number" ? score.score + " / 100" : "暂无分数"}</span></h3><b>为什么这样评分</b><MessageContent text={score.reason} annotations={false} />{score.risk && <><b>目前风险</b><MessageContent text={score.risk} annotations={false} /></>}{score.improvement && <><b>下一步怎么改进</b><MessageContent text={score.improvement} annotations={false} /></>}<b>评分依据</b>{score.evidence_refs.length ? <ul>{score.evidence_refs.map((ref, i) => <li key={i}>{ref}</li>)}</ul> : <p className="muted">目前没有足够的证据，因此保留为未知；这不影响资料查询。</p>}</article>}</div></div>{qualityIssues.length > 0 && <details className="quality-issues"><summary>资料状态与证据边界</summary><p>这份档案可以直接读取；当前还有 {qualityIssues.length} 个内容点等待从真实代码、任务或运行证据补充。</p><p className="muted">系统保留现有版本，不会用猜测补分。可以使用项目页的更新按钮交给 Codex 自动整理。</p></details>}</section>
    <section className="knowledge-intro"><div><span className="eyebrow">项目档案 · 第 {manifest.revision || 0} 次整理</span><h2>先看目录，需要什么再读什么</h2><p>Codex 按章节保存资料。自动识别的内容会标明来源和不确定性，任何时候都可以直接查询。</p></div><div className="knowledge-completion"><strong>{complete}<small> / {catalog.length}</small></strong><span>已有内容的章节</span><div><i style={{ width: complete / Math.max(1, catalog.length) * 100 + "%" }} /></div><small className="knowledge-completion-note">{autoDetected ? `另有 ${autoDetected} 章来自自动识别线索` : missing ? `${missing} 章尚无记录` : "全部章节均有整理内容，结论以证据为准"}</small></div></section>
    {autoDetected > 0 && <div className="knowledge-provisional-note"><strong>自动整理线索</strong><span>这些内容来自项目说明文件或历史可见活动，已保留来源和不确定性；不阻止读取，也不会自动变成确定事实或评分。</span></div>}
    <div className="knowledge-workspace"><nav className="knowledge-catalog" aria-label="项目资料目录">{catalog.map(item => <button className={section === item.key ? "active" : ""} key={item.key} onClick={() => setSection(item.key)}><span className={"chapter-dot " + (item.state === "user_edited" || item.state === "agent_reported" || item.state === "local_reviewed_recovery" ? "ready" : item.state === "auto_detected" ? "detected" : "")} /><span><strong>{item.label}</strong><small>{item.state === "user_edited" ? "人工修改" : item.state === "agent_reported" ? "Codex 已整理" : item.state === "local_reviewed_recovery" ? "来自已审基线" : item.state === "auto_detected" ? "自动整理线索" : "尚无记录"}</small></span><span>→</span></button>)}</nav>
      <section className="knowledge-reader"><div className="knowledge-reader-head"><span className="eyebrow">按需读取 · 独立保存</span><h2>{selected?.label || "项目资料目录"}</h2><p>{selected?.description || "先选择一个章节，系统会按需读取可用内容。"}</p>{loaded && <button className="text-action" onClick={() => setEditing(value => !value)}>修改这一章</button>}</div><div className="knowledge-reader-body">{editing && loaded ? <DocumentEditor projectId={projectId} section={sectionKey} revision={loaded.revision} value={objectValue(loaded.sections)?.[sectionKey]} label={selected?.label} onClose={() => setEditing(false)} onSaved={() => {setRetry(value => value+1);onUpdated?.();}} /> : null}{error ? <div className="notice-banner warn"><span>{error}。本次没有显示其他项目的资料。</span><button onClick={() => setRetry(value => value + 1)}>重试</button></div> : !loaded ? <p className="muted" role="status">正在读取这一章…</p> : Object.keys(objectValue(loaded.sections)?.[sectionKey] || {}).length ? <StructuredValue value={objectValue(loaded.sections)[sectionKey]} /> : <div className="chapter-empty"><strong>这一章还没有整理</strong><p>系统仍保留可见活动；后续 Codex 可以从真实来源补充这一章。</p></div>}</div><details className="chapter-help"><summary>这一章怎么用？看一个示例</summary><p>{selected?.example || "先读取这章内容，再结合当前工程判断是否需要更新。"}</p><p>可以对 Codex 说：“请读取 AP-Vibe 的{selected?.label || "项目资料"}，结合当前工程更新真实变化。”</p>{selected?.read_url && <code>{selected.read_url}</code>}</details><div className="chapter-footer">{selected?.state === "user_edited" ? "人工修改，旧版本已保留" : selected?.state === "agent_reported" ? "Codex 整理记录，结论需结合证据理解" : selected?.state === "local_reviewed_recovery" ? "来自本地已审基线" : selected?.state === "auto_detected" ? "自动整理线索，保留不确定性" : "尚无记录"}{selected?.updated_at && " · " + new Date(selected.updated_at).toLocaleString("zh-CN")}</div></section>
    </div>

  </>;
}
