import { useEffect, useMemo, useRef, useState } from 'react';
import { workspaceApi, requestId } from './workspace-api';
import { curationError } from './dossier-presentation.mjs';
import { MessageContent } from './MessageContent';

const MODES = [
  ['recent_unclassified', '整理最近 7 天', '只整理最近活跃、尚未归类的任务。推荐从这里开始。'],
  ['all_unclassified', '整理全部未归类', '包含更早的历史任务，可能需要更多模型用量。'],
  ['rebuild_all', '重新整理全部会话', '重新核对已归类和未归类任务，按项目重建档案；保留旧版本与人工修改。'],
];
const STATES = { ready_for_codex: '等待整理', awaiting_client: '等待当前软件接单', running: '正在整理', applying: '正在写回', completed: '整理完成', failed: '未完成', interrupted: '服务重启中断' };
const PROJECT_STATES = { queued: '排队中', running: '正在读取与整理', retrying: '正在重试', completed: '草稿已保存，等待写回', saved: '已写回，可立即查询', failed: '未完成，可继续' };

function ExternalHandoff({ task }) {
  const [copied, setCopied] = useState(false);
  if (task.status !== 'awaiting_client') return null;
  const handoff = '请使用 AP-Vibe 整理任务 ' + task.task_id + '。先调用 ap_vibe_organization_read({task_id:"' + task.task_id + '"})，读取冻结清单、真实会话和旧档案，遵照输出合同生成完整JSON提案。然后用 ap_vibe_organization_submit 提交本地提案文件，handoff_id使用本次读取值；最后回读任务和档案版本。没有MCP时按AP-Vibe原生Skill的native_client.py tool回退调用同名工具。保留人工修改和历史，不改原会话。无需安装Codex。';
  return <section className="page-section curation-handoff"><h2>把接单说明发给你正在使用的软件</h2><p>正在等待当前会话接单，尚未调用模型。</p><textarea aria-label="当前软件接单说明" readOnly value={handoff} rows={5}/><button className="secondary-button" onClick={async () => { try { await navigator.clipboard.writeText(handoff); setCopied(true); } catch { setCopied(false); } }}>{copied ? '已复制' : '复制接单说明'}</button></section>;
}

function OrganizationTask({ task, busy, onDispatch }) {
  const total = task.total ?? task.result?.project_ids?.length ?? 0;
  return <section className="page-section" aria-live="polite"><p className="muted">整理执行端：{task.result?.executor?.name || "本机 Codex（历史任务）"}{task.result?.executor?.model ? " · " + task.result.executor.model : ""}</p><span className="eyebrow">{task.scope === 'project_refresh' ? '档案刷新任务' : '整理任务'}</span><div className="organization-toolbar"><div><h2>{STATES[task.status] || '整理任务'} · {total} 项</h2><p>{task.result?.phase || '点击开始后，所选执行端会读取真实来源并写回档案。'}</p></div>{['ready_for_codex', 'failed', 'interrupted', 'awaiting_client'].includes(task.status) && <button className="primary-button" disabled={busy} onClick={onDispatch}>{task.status === 'ready_for_codex' ? '开始整理' : task.status === 'awaiting_client' ? '使用所选执行端接单' : '继续未完成的项目'}</button>}</div><p className="muted">按所选执行端配置产生模型用量。每个项目整理完成后立即保存；连接中断可继续剩余项目，已保存的档案可直接查询。</p>{task.result?.project_progress?.map(item => <article className="organization-history-item" key={item.project_id}><strong>{item.name}</strong><span>{task.result?.completed_projects?.includes(item.project_id) ? '已写回，可立即查询' : PROJECT_STATES[item.status] || item.status}{item.attempts ? ` · 尝试 ${item.attempts} 次` : ''}{item.cached ? ' · 已复用保存结果' : ''}</span>{item.error && item.status === 'failed' && <div><small>{curationError(item.error)}</small><details><summary>技术详情</summary><small>{item.error}</small></details></div>}</article>)}{task.result?.error && ['failed', 'interrupted'].includes(task.status) && <div role="alert"><p>{curationError(task.result.detail || task.result.error)}</p><details><summary>技术详情</summary><p>{task.result.detail || task.result.error}</p></details></div>}{task.result?.project_receipts?.map(item => <p key={item.project_id}>✓ {item.name} · 档案第 {item.revision} 版 · {item.sections}/11 章 · 已分析 {item.assessment_recorded ?? item.assessment_count ?? 0}/10 · 已评分 {item.assessment_count ?? 0}/10</p>)}<details><summary>查看完整任务说明与来源任务</summary><MessageContent text={task.prompt} annotations={false}/>{task.result?.session_id && (!task.result?.executor || task.result.executor.kind === 'codex') && <a className="text-action" href={'codex://threads/' + task.result.session_id}>打开当前整理任务 ↗</a>}</details></section>;
}

export function OrganizationButtons({ onOpen }) {
  return <section className="page-section"><span className="eyebrow">跨应用项目整理</span><h2>把散落的会话，整理成项目档案</h2><p className="muted">一个项目可以拥有多个会话。先看范围再开始整理；没有足够信息的任务会保留为未归类，但不会影响读取和继续工作。</p><div className="organization-modes">{MODES.map(([key, label, detail]) => <button className="organization-mode" key={key} onClick={() => onOpen(key)}><strong>{label}</strong><span>{detail}</span><b>查看范围 →</b></button>)}<button className="organization-mode" onClick={() => onOpen('project_refresh')}><strong>刷新项目档案</strong><span>逐个补充已登记项目的 11 章和十维评估，不创建项目、不移动会话。</span><b>查看已登记项目 →</b></button></div></section>;
}

export function OrganizationPage({ initialScope, initialProjectIds = [], projects, onBack, onChanged }) {
  const [scope, setScope] = useState(initialScope || 'recent_unclassified');
  const [offset, setOffset] = useState(0);
  const [harness, setHarness] = useState('');
  const [executors, setExecutors] = useState(null);
  const [executor, setExecutor] = useState('');
  useEffect(() => {
    let alive = true;
    workspaceApi('organization/executors').then(value => {
      if (alive) {
        setExecutors(value);
        let saved = '';
        try { saved = localStorage.getItem('ap-vibe-curation-executor') || ''; } catch {}
        setExecutor(old => old || (value.items?.some(item => item.id === saved && item.available) ? saved : value.default));
      }
    }).catch(e => { if (alive) setError(e.message || '读取整理执行端失败'); });
    return () => { alive = false; };
  }, []);
  const [catalog, setCatalog] = useState(null);
  const [selected, setSelected] = useState([]);
  useEffect(() => {
    if (executor) { try { localStorage.setItem('ap-vibe-curation-executor', executor); } catch {} }
  }, [executor]);
  const [task, setTask] = useState(null);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [tasks, setTasks] = useState([]);
  const [context, setContext] = useState(null);
  const [target, setTarget] = useState('');
  const [rationale, setRationale] = useState('');
  const [revision, setRevision] = useState(0);
  const startRequest = useRef(null);
  const isRefresh = scope === 'project_refresh';
  const refreshProjects = useMemo(() => {
    const allowed = initialProjectIds.length ? new Set(initialProjectIds) : null;
    return (projects || []).filter(project => !allowed || allowed.has(project.project_id));
  }, [projects, initialProjectIds.join(',')]);

  useEffect(() => {
    let alive = true;
    workspaceApi('organization/tasks')
      .then(value => { if (!alive) return; const history = (value.tasks || []).filter(item => item.scope !== 'logic_analysis'); setTasks(history); setTask(current => current || history.find(item => ['running', 'applying'].includes(item.status)) || history.find(item => item.scope === scope && ['failed', 'interrupted'].includes(item.status)) || null); })
      .catch(e => alive && setError(e.message || '读取整理记录失败'));
    return () => { alive = false; };
  }, [revision]);

  useEffect(() => {
    let alive = true;
    setError('');
    if (isRefresh) {
      setCatalog(null);
      setLoading(false);
      setSelected(refreshProjects.map(project => project.project_id));
      return () => { alive = false; };
    }
    setLoading(true);
    workspaceApi(`organization/sessions?scope=${scope}&offset=${offset}&harness=${encodeURIComponent(harness)}`)
      .then(value => {
        if (!alive) return;
        setCatalog(value);
        setSelected((value.items || []).map(item => item.source_key).filter(Boolean));
      })
      .catch(e => alive && setError(e.message || '读取会话失败'))
      .finally(() => alive && setLoading(false));
    return () => { alive = false; };
  }, [scope, offset, harness, isRefresh, refreshProjects.map(project => project.project_id).join(',')]);

  useEffect(() => {
    if (!task || !['running', 'applying', 'awaiting_client'].includes(task.status)) return undefined;
    let timer;
    let alive = true;
    const poll = async () => {
      try {
        const value = await workspaceApi('organization/task?task_id=' + encodeURIComponent(task.task_id));
        if (!alive) return;
        setTask(value);
        setError('');
        if (['completed', 'failed', 'interrupted'].includes(value.status)) {
          onChanged?.();
          setRevision(value => value + 1);
        }
      } catch (e) {
        if (alive) setError(e.message || '读取任务状态失败');
      }
      if (alive && ['running', 'applying', 'awaiting_client'].includes(task.status)) timer = setTimeout(poll, 4000);
    };
    timer = setTimeout(poll, 4000);
    return () => { alive = false; clearTimeout(timer); };
  }, [task?.task_id, task?.status, onChanged]);

  const action = async (fn) => {
    setBusy(true);
    setError('');
    try { await fn(); } catch (e) { setError(e.message || '操作没有完成'); } finally { setBusy(false); }
  };
  const currentItems = isRefresh ? refreshProjects : (catalog?.items || []);
  const selectionKeys = isRefresh ? currentItems.map(item => item.project_id) : currentItems.map(item => item.source_key).filter(Boolean);
  const selectAll = () => setSelected(old => [...new Set([...old.filter(key => !selectionKeys.includes(key)), ...selectionKeys])]);
  const invertSelection = () => setSelected(old => [...new Set([...old.filter(key => !selectionKeys.includes(key)), ...selectionKeys.filter(key => !old.includes(key))])]);
  const clearSelection = () => setSelected(old => old.filter(key => !selectionKeys.includes(key)));
  const prepare = () => action(async () => {
    const selection = [...selected].sort();
    const signature = JSON.stringify([scope, selection, harness, executor]);
    if (startRequest.current?.signature !== signature) startRequest.current = { signature, id: requestId() };
    const payload = { request_id: startRequest.current.id, scope, ...(isRefresh ? { project_ids: selection } : { source_keys: selection }) };
    const value = await workspaceApi('organization/prepare', payload);
    setTask(value.task);
    if (!['running', 'applying', 'completed'].includes(value.task.status)) {
      const launched = await workspaceApi('organization/dispatch', { task_id: value.task.task_id, executor });
      setTask(launched.task);
    }
    setRevision(value => value + 1);
    startRequest.current = null;
  });
  const switchScope = next => {
    setScope(next);
    setOffset(0);
    setTask(null);
    setContext(null);
    setSelected(next === 'project_refresh' ? refreshProjects.map(project => project.project_id) : []);
  };
  const taskTotal = task?.total ?? (task?.result?.project_ids?.length || task?.result?.frozen_projects?.length || 0);

  return <div className="page-stack organization-page"><div className="page-header"><div><span className="eyebrow">项目与记忆 / {isRefresh ? '档案刷新' : '历史整理'}</span><h1>{isRefresh ? '让每个项目都有真实、可解释的档案' : '把任务归到合适的项目'}</h1><p>{isRefresh ? '只刷新已登记项目的 11 章档案和十维评估，不创建项目、不移动会话；没有证据的内容会明确保留为未知。' : '旧任务不会自动变成项目档案。先查看候选，再勾选少量任务试一轮，或按所选范围全部整理。'}</p></div><button className="secondary-button" onClick={onBack}>返回项目档案</button></div>
    {error && <div className="notice-banner warn" role="alert">{error}</div>}
    <section className="page-section">
      <div className="organization-toolbar"><div><h2>由谁负责整理</h2><p className="muted">可整理所有已接入软件的会话。这里选择负责读取和归档的执行端，与会话来自哪个软件无关。</p></div>
      <label>整理执行端<select aria-label="整理执行端" value={executor} onChange={e => setExecutor(e.target.value)} disabled={busy || ['running','applying'].includes(task?.status)}>
        {!executors && <option value="">正在发现…</option>}
        {executors?.items?.map(item => <option key={item.id} value={item.id} disabled={!item.available}>{item.name}{item.model ? ' · ' + item.model : ''}{!item.available ? '（暂不可用）' : ''}</option>)}
      </select></label></div><p className="muted">{executors?.items?.find(item => item.id === executor)?.detail}</p>
      {executor === 'external' && <p>开始后复制接单说明，发给你正在使用的 DSH、Claude、Grok 等软件；已接入的 MCP 或 Skill 会引导它完成整理。</p>}
    </section>
    {task && <ExternalHandoff task={task}/>}
    {task && <OrganizationTask task={task} busy={busy} onDispatch={() => action(async () => setTask((await workspaceApi('organization/dispatch', { task_id: task.task_id, executor })).task))} />}
    <div className="organization-modes">{MODES.map(([key, label, detail]) => <button key={key} className={'organization-mode ' + (scope === key ? 'selected' : '')} onClick={() => switchScope(key)}><strong>{label}</strong><span>{detail}</span></button>)}<button className={'organization-mode ' + (isRefresh ? 'selected' : '')} onClick={() => switchScope('project_refresh')}><strong>刷新项目档案</strong><span>只更新已经登记的项目说明书、证据和十维评估，不改变会话归属。</span></button></div>
    <section className="page-section"><div className="organization-toolbar"><div><h2>{isRefresh ? `已登记项目 · ${loading ? '正在读取…' : `${refreshProjects.length} 个`}` : `待核对任务 · ${loading ? '正在读取…' : `${catalog?.total ?? 0} 条`}`}</h2><p className="muted">{isRefresh ? '读取各客户端真实来源、保留历史和人工修改，完成 11 章档案与十维评估。' : '整理时会核对真实可见消息；没有有效内容的任务会说明原因并跳过。'}</p></div><button className="primary-button" disabled={busy || loading || !executor || !selected.length || ['running', 'applying'].includes(task?.status)} onClick={prepare}>{busy ? '正在启动…' : `开始整理 · ${selected.length} ${isRefresh ? '个项目' : '条任务'}`}</button></div>
      {!isRefresh && <label>来源应用<select aria-label="来源应用" value={harness} onChange={e => { setHarness(e.target.value); setOffset(0); setSelected([]); }}>
        <option value="">全部已接入应用</option>{(catalog?.harnesses || []).filter(item => catalog?.applications?.[item.harness] || item.harness === harness).map(item => <option value={item.harness} key={item.harness}>{item.name} · {catalog.applications?.[item.harness] || 0}</option>)}
      </select></label>}
      {!!catalog?.warnings?.length && <p className="muted">部分来源有读取提示，请在会话接入设置中核对目录。整理范围以本次实际发现结果为准。</p>}
      {!loading && currentItems.length > 0 && <div className="organization-toolbar selection-toolbar"><span>当前范围已选 {selectionKeys.filter(key => selected.includes(key)).length} / {selectionKeys.length} {isRefresh ? '个项目' : '条任务'}</span><div><button className="secondary-button" type="button" onClick={selectAll}>全选</button><button className="secondary-button" type="button" onClick={invertSelection}>一键反选</button><button className="text-action" type="button" onClick={clearSelection}>清空选择</button></div></div>}
      {loading && <div className="loading-state" role="status"><span className="loading-spinner" />正在读取{MODES.find(item => item[0] === scope)?.[1] || "当前范围"}的会话，请稍候…</div>}
      {!loading && isRefresh && currentItems.length > 0 && <div className="organization-table">{refreshProjects.map(project => <article key={project.project_id}><input type="checkbox" aria-label={'选择 ' + project.display_name} checked={selected.includes(project.project_id)} onChange={e => setSelected(old => e.target.checked ? [...old, project.project_id] : old.filter(x => x !== project.project_id))} /><div><strong>{project.display_name}</strong><p>{project.documentation_quality === 'ready' ? '档案已整理' : '档案证据待补'} · 第 {project.document_revision || 0} 版 · 已分析 {project.assessment_documented_count || 0}/10 · 已评分 {project.assessment_count || 0}/10</p><small>{project.quality_issues?.length ? '部分资料或评分证据待补，现有章节仍可直接查询。' : '11 章项目档案已存在，将结合各应用真实来源补充。'}</small></div><span className={'quality-badge ' + (project.documentation_quality === 'ready' ? 'good' : 'warn')}>{project.documentation_quality === 'ready' ? '可继续使用' : '可直接查询'}</span></article>)}</div>}
      {!loading && !isRefresh && catalog?.items?.length > 0 && <div className="organization-table">{catalog.items.map(item => <article key={item.source_key}><input type="checkbox" aria-label={'选择 ' + item.title} checked={selected.includes(item.source_key)} onChange={e => setSelected(old => e.target.checked ? [...old, item.source_key] : old.filter(x => x !== item.source_key))} /><div><strong>{item.title}</strong><p>{catalog.harnesses?.find(h => h.harness === item.harness)?.name || item.harness || 'Codex'} · {item.classified ? '已归类 · ' + item.project_name : '尚未归类'} · {new Date(item.last_activity_at).toLocaleString('zh-CN')}</p><small>{item.preview || '尚无已采集摘要；可先查看最近可见内容。'}</small></div><button className="text-action" disabled={busy} onClick={() => action(async () => { setContext(await workspaceApi('organization/context?source_key=' + encodeURIComponent(item.source_key))); setTarget(''); setRationale(''); })}>查看上下文 / 纠正归属</button></article>)}</div>}
      {!loading && !isRefresh && !error && catalog && !catalog.items?.length && <p className="muted">已读取这个范围，但目前没有待整理任务。可以切换到全部历史，或等待新任务活动。</p>}
      {!isRefresh && <><div className="organization-toolbar"><button className="secondary-button" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - 64))}>上一页</button><span>已勾选 {selected.length} 条 · 第 {Math.floor(offset / 64) + 1} 页</span><button className="secondary-button" disabled={catalog?.next_offset == null} onClick={() => setOffset(catalog.next_offset)}>下一页</button></div><details className="organization-exclusions"><summary>本次排除 {catalog?.excluded_count || 0} 条，查看原因</summary>{catalog?.excluded?.map(item => <p key={item.source_key}>{item.title} — {item.reason}</p>)}<small>这里只显示前 64 条原因；来源文件没有删除。</small></details></>}</section>
    {tasks.length > 0 && <section className="page-section"><h2>最近整理记录</h2><p className="muted">成功数量以项目和档案写回收据为准。失败记录会保留原因，刷新页面也不会丢失。</p>{tasks.map(item => <button className="organization-history-item" key={item.task_id} disabled={busy} onClick={() => action(async () => setTask(await workspaceApi('organization/task?task_id=' + encodeURIComponent(item.task_id))))}><strong>{STATES[item.status] || item.status}</strong><span>{new Date(item.created_at).toLocaleString('zh-CN')} · {item.total || item.result?.project_ids?.length || 0} 项 · 已写回 {item.result?.completed_projects?.length || 0} 个项目</span></button>)}</section>}
    {context && <section className="page-section"><div className="organization-toolbar"><h2>{context.title || '最近可见内容'}</h2><button className="text-action" onClick={() => setContext(null)}>关闭上下文</button></div><div className="organization-context">{context.messages?.length ? context.messages.map((item, index) => <article key={item.source_ref || index}><b>{item.role === 'user' ? '用户' : '助手 · ' + (context.harness || 'codex')}</b><MessageContent text={item.text} annotations={false}/></article>) : <p>最近窗口没有可见消息。可以保留未归类，等待这个任务继续活动。</p>}</div><div className="manual-assignment"><h3>人工修正归属</h3><label>应该属于哪个项目<select value={target} onChange={e => setTarget(e.target.value)}><option value="">请选择项目</option>{projects.filter(item => item.status === 'active').map(item => <option key={item.project_id} value={item.project_id}>{item.display_name}</option>)}</select></label><label>为什么这样归类<textarea value={rationale} onChange={e => setRationale(e.target.value)} placeholder="例如：这是同一个记账产品的移动端任务，与现有项目共用需求。" /></label><button className="primary-button" disabled={!target || !rationale.trim() || busy} onClick={() => action(async () => { await workspaceApi('organization/assign', { request_id: requestId(), source_key: context.source_key, session_id: context.session_id, project_id: target, rationale, actor: 'user', evidence_refs: (context.messages || []).slice(0, 3).map(item => item.source_ref).concat('user://workbench/manual-classification') }); setContext(null); setRevision(value => value + 1); onChanged?.(); })}>确认归类并保留历史</button></div></section>}
  </div>;
}

export function ProjectManagement({ project, onChanged, embedded = false }) {
  const [mode, setMode] = useState('');
  const [name, setName] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const submit = async event => {
    event.preventDefault();
    setBusy(true);
    setError('');
    try {
      if (mode === 'create') await workspaceApi('organization/projects/create', { request_id: requestId(), display_name: name });
      else await workspaceApi('organization/projects/update', { request_id: requestId(), project_id: project.project_id, ...(mode === 'rename' ? { display_name: name } : { status: project.status === 'archived' ? 'active' : 'archived' }), reason: '用户在项目管理界面修正项目容器' });
      setMode('');
      onChanged?.();
    } catch (e) { setError(e.message || '保存项目没有完成'); } finally { setBusy(false); }
  };
  const body = <><h2>项目管理</h2><p className="muted">新建项目后，可以在历史整理页把多条任务归到这里。归档只隐藏活动项目，档案、会话和旧版本继续保留；可在上方“已归档”目录选中并恢复。</p><div className="organization-toolbar"><button className="secondary-button" onClick={() => { setMode('create'); setName(''); }}>新建项目</button><button className="secondary-button" disabled={!project} onClick={() => { setMode('rename'); setName(project.display_name); }}>修改项目名称</button><button className="secondary-button" disabled={!project || project.project_id === 'ap-vibe-local'} onClick={() => setMode('archive')}>{project?.status === 'archived' ? '恢复项目' : '归档当前项目'}</button></div>{mode && <form className="manual-assignment" onSubmit={submit}>{mode === 'archive' ? <p>确认{project?.status === 'archived' ? '恢复' : '归档'}“{project?.display_name}”？旧档案和会话保留。</p> : <label>项目名称<input value={name} onChange={e => setName(e.target.value)} required maxLength={160} /></label>}{error && <p role="alert">{error}</p>}<div className="organization-toolbar"><button className="primary-button" disabled={busy}>{busy ? '正在保存…' : '确认保存'}</button><button type="button" className="secondary-button" onClick={() => setMode('')}>取消</button></div></form>}</>;
  return embedded ? <div className="project-management-embedded">{body}</div> : <section className="page-section">{body}</section>;
}
