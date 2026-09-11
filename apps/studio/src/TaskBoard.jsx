import { useEffect, useMemo, useRef, useState } from 'react';
import {StudioPlans} from './StudioPlans';
import { ReviewVerdictPanel } from './ReviewVerdictPanel';
import { TaskAttemptHistory } from './TaskAttemptHistory';
import { MessageContent } from './MessageContent';

const STATES = { budget_waiting: '等待投喂', queued: '待分配', dispatching: '正在派发', running: '执行中', waiting_review: '成果待验收', completed: '已完成', changes_requested: '需要修改', needs_help: '需要接手', paused: '已暂停', archived: '已归档' };
const uid = () => crypto.randomUUID();

async function request(path, payload, signal) {
  const response = await fetch('/v1/ap-vibe/studio/tasks' + path, {method: payload ? 'POST' : 'GET', headers: { 'Content-Type': 'application/json' }, body: payload ? JSON.stringify(payload) : undefined, signal});
  const value = await response.json();
  if (!response.ok || value.ok === false) throw new Error([value.error?.message || value.message || '任务池请求失败', value.error?.code, value.error?.solution].filter(Boolean).join(' · '));
  return value;
}

export function TaskBoard({ projects = [], projectsLoading = false, projectId, agents = [], openRun }) {
  const [tasks, setTasks] = useState([]);
  const [selected, setSelected] = useState(null);
  const [form, setForm] = useState(null);
  const [notice, setNotice] = useState('');
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [selectedDetail, setSelectedDetail] = useState(null);
  const pendingSave = useRef(null);
  const reading = useRef(false);
  const nameOf = id => agents.find(a => a.agent_id === id)?.name || id || '尚未领取';
  const activeAgents = agents.filter(agent => !agent.archived);
  const current = tasks.find(task => task.task_id === selected) || (selectedDetail?.task_id === selected ? selectedDetail : null);
  useEffect(() => {
    // The catalogue may arrive after the dialog opens. Never overwrite a
    // project the user already selected while a refresh was in flight.
    const matching = projects.find(project => project.project_id === projectId);
    if (matching) setForm(old => old && !old.project_id ? { ...old, project_id: matching.project_id } : old);
  }, [projects, projectId]);

  async function refresh(signal) {
    if (reading.current) return;
    reading.current = true;
    try { const data = await request('', null, signal); if (signal?.aborted) return; setTasks(data.tasks || []); setLoaded(true); setSelected(old => old || data.tasks?.find(t => t.state !== 'archived')?.task_id || null); setError(''); }
    catch (err) { if (!signal?.aborted) setError(err.message); }
    finally { reading.current = false; }
  }
  useEffect(() => {
    const controller = new AbortController(); let timer;
    async function poll() { if (!document.hidden) await refresh(controller.signal); if (!controller.signal.aborted) timer = setTimeout(poll, 4000); }
    poll(); return () => { controller.abort(); clearTimeout(timer); };
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    if (selected && !tasks.some(t => t.task_id === selected)) request('?task_id=' + encodeURIComponent(selected), null, controller.signal).then(data => { if (!controller.signal.aborted) setSelectedDetail(data.tasks?.[0] || null); }).catch(err => { if (!controller.signal.aborted) setError(err.message); });
    return () => controller.abort();
  }, [selected, tasks]);
  const projectName = useMemo(() => Object.fromEntries(projects.map(project => [project.project_id, project.display_name])), [projects]);

  async function save(event) {
    event.preventDefault();
    const signature = JSON.stringify(form);
    if (pendingSave.current?.signature !== signature) pendingSave.current = {signature, id: uid()};
    setBusy(true); setNotice(''); setError('');
    try {
      const result = await request('/save', { ...form, request_id: pendingSave.current.id, expected_version: form.version || 0, dependencies: form.dependencies || [], eligible_agents: form.eligible_agents || [], resources: form.resources || [], tags: form.tags || [], reviewer_agent_id: form.reviewer_agent_id || null });
      pendingSave.current = null; setForm(null); setSelected(result.task.task_id); setNotice('任务已保存。成果返回后会自动安排所选验收伙伴检查。'); await refresh();
    } catch (err) { setError(err.message); } finally { setBusy(false); }
  }
  async function claim(task, agentId) {
    setBusy(true); setNotice(''); setError('');
    try { await request('/claim', { request_id: uid(), task_id: task.task_id, agent_id: agentId, expected_version: task.version }); setNotice('已领取并开始派发；执行器返回后仍需检查成果。'); await refresh(); }
    catch (err) { setError(err.message); } finally { setBusy(false); }
  }
  async function archive(task) {
    setBusy(true); setNotice(''); setError('');
    try { await request('/archive', { request_id: uid(), task_id: task.task_id, expected_version: task.version }); setNotice('任务已归档，历史尝试与成果记录仍保留。'); await refresh(); }
    catch (err) { setError(err.message); } finally { setBusy(false); }
  }
  async function release(task) {
    setBusy(true); setNotice(''); setError('');
    try { await request('/release', { request_id: uid(), task_id: task.task_id, expected_version: task.version, note: '用户确认保留原成果，允许下一位伙伴接手。' }); setNotice('已释放回任务池；原执行现场和交接入口已保留。'); await refresh(); }
    catch (err) { setError(err.message); } finally { setBusy(false); }
  }

  function editTask(task) {
    const fields = ['task_id', 'version', 'title', 'goal', 'acceptance', 'project_id', 'eligible_agents',
      'dependencies', 'resources', 'tags', 'extensions', 'reviewer_agent_id', 'max_turns', 'max_rework_rounds', 'max_review_retries', 'max_author_retries', 'auto_run'];
    setForm(Object.fromEntries(fields.map(key => [key, task[key]])));
  }

  return <section className="page-section task-board" aria-label="任务池">
    <StudioPlans projectId={projectId} agents={agents} openRun={openRun}/>
    <div className="task-board-head"><div><span className="eyebrow">持久任务池</span><h2>把大目标拆成可接手的工作</h2><p className="muted">选择执行伙伴和验收伙伴后，成果返回就会自动检查。任务会保留依赖、负责人和每次成果。</p></div><button className="primary-button" onClick={() => setForm({ title: '', goal: '', acceptance: '', project_id: projects.find(project => project.project_id === projectId)?.project_id || '', eligible_agents: [], dependencies: [], resources: [], tags: [], reviewer_agent_id: null, max_rework_rounds: 2, max_review_retries: 1, max_author_retries: 1, auto_run: false })}>＋ 新建任务</button></div>
    {error && <div role="alert" className="agent-error">{error}<button onClick={() => refresh()}>重新读取</button></div>}
    {notice && <p className="agent-notice" role="status">{notice}</p>}
    <div className="task-board-grid"><div className="task-list">{tasks.filter(task => task.state !== 'archived').map(task => <button key={task.task_id} className={selected === task.task_id ? 'selected' : ''} onClick={() => { setNotice(''); setSelected(task.task_id); }}><strong>{task.title}</strong><span>{STATES[task.state] || task.state} · {projectName[task.project_id] || task.project_id}</span><small>{nameOf(task.owner)} · 第 {task.assignment_epoch || 0} 次分工{task.reviewer_agent_id ? ' · 已安排验收伙伴' : ''}</small></button>)}{!tasks.filter(task => task.state !== 'archived').length && <p className="muted">{loaded ? '还没有待办。点击新建任务，写下目标和完成标准。' : '正在读取任务…'}</p>}</div>
      <div className="task-detail">{!current ? <div className="agent-empty"><h3>选择任务查看详情</h3><p>这里会显示依赖、负责人、历史尝试和下一步动作。</p></div> : <><div className="agent-output-head"><div><h3>{current.title}</h3><span className="quality-badge">{STATES[current.state] || current.state}</span></div><small>{projectName[current.project_id] || current.project_id}</small></div><TaskAttemptHistory task={current} agents={agents} openRun={openRun} /><ReviewVerdictPanel task={current} openRun={openRun} /><div className="task-goal" role="region" aria-label="任务目标"><MessageContent text={current.goal} annotations={false}/></div><div className="task-acceptance"><b>完成标准</b><MessageContent text={current.acceptance} annotations={false}/></div>{current.dependencies?.length > 0 && <p className="agent-notice">{current.review_of_task_id ? '检查上游真实成果：' : '依赖任务（验收通过后继续）：'}{current.dependencies.map(id => <button key={id} onClick={() => setSelected(id)}>{tasks.find(t => t.task_id === id)?.title || id}</button>)}</p>}{current.reviewer_agent_id && <p className="agent-notice">验收伙伴：{agents.find(agent => agent.agent_id === current.reviewer_agent_id)?.name || current.reviewer_agent_id}。成果返回后自动开始检查，无需先点“通过”。</p>}{current.review_of_task_id && <p className="agent-notice">这是独立验收任务，读取对应运行的真实文件并写入 acceptance.md。检查意见和原成果分别保存。</p>}{current.review_task_id && <div className="agent-notice"><p>检查进度：{STATES[tasks.find(t => t.task_id === current.review_task_id)?.state] || '已登记'}</p><button onClick={() => setSelected(current.review_task_id)}>查看验收任务与报告</button></div>}{current.review_issue && <p role="status" className="agent-error">{current.review_issue}</p>}<dl className="task-meta"><dt>负责人</dt><dd>{nameOf(current.owner)}</dd><dt>资源范围</dt><dd>{current.resources?.length ? current.resources.join('、') : '未设置'}</dd><dt>执行尝试</dt><dd>{current.attempts?.length || 0} 次，失败后可保留现场再交接</dd></dl><div className="agent-actions">{!current.review_of_task_id && ['queued', 'needs_help', 'paused', 'changes_requested', 'completed'].includes(current.state) && <button disabled={busy} onClick={() => editTask(current)}>编辑任务与候选伙伴</button>}{current.run_id && openRun && <button onClick={() => openRun(current.run_id)}>查看执行过程与成果</button>}{current.state === 'queued' && (current.eligible_agents?.length ? current.eligible_agents : activeAgents.map(agent => agent.agent_id)).filter(id => id !== current.reviewer_agent_id).map(agentId => <button key={agentId} disabled={busy} onClick={() => claim(current, agentId)}>交给 {agents.find(agent => agent.agent_id === agentId)?.name || agentId}</button>)}{['needs_help', 'paused', 'changes_requested'].includes(current.state) && <button disabled={busy} onClick={() => release(current)}>保留成果并重新分配</button>}{['queued', 'waiting_review', 'completed', 'needs_help', 'paused', 'changes_requested'].includes(current.state) && <button disabled={busy} onClick={() => archive(current)}>归档并保留记录</button>}</div></>}</div></div>
    {form && <div className="agent-dialog-backdrop" onClick={event => event.target === event.currentTarget && !busy && setForm(null)}><form className="agent-dialog" role="dialog" aria-modal="true" aria-label={form.task_id ? "编辑持久任务" : "新建持久任务"} onSubmit={save}><div className="agent-output-head"><h2>{form.task_id ? "编辑持久任务" : "新建持久任务"}</h2><button type="button" onClick={() => setForm(null)}>×</button></div><label>任务标题<input required value={form.title} onChange={event => setForm({ ...form, title: event.target.value })} placeholder="例如：整理接口变更清单" /></label><label>所属项目<select required disabled={Boolean(form.task_id) || !projects.length} aria-busy={projectsLoading} value={form.project_id} onChange={event => setForm({ ...form, project_id: event.target.value, dependencies: [] })}><option value="">{projectsLoading ? "正在读取项目目录…" : projects.length ? "请选择所属项目" : "尚无已登记项目，请先整理项目档案"}</option>{projects.map(project => <option key={project.project_id} value={project.project_id}>{project.display_name}</option>)}</select></label><label>要达成什么<textarea required rows={3} value={form.goal} onChange={event => setForm({ ...form, goal: event.target.value })} placeholder="用一句话描述交付目标和范围" /></label><label>怎样算完成<textarea required rows={3} value={form.acceptance} onChange={event => setForm({ ...form, acceptance: event.target.value })} placeholder="例如：文件存在、字段齐全，并由另一位伙伴独立检查" /></label><label>先等哪些同项目任务（可选）<select multiple value={form.dependencies} onChange={event => setForm({ ...form, dependencies: [...event.target.selectedOptions].map(option => option.value) })}>{tasks.filter(task => task.project_id === form.project_id && task.state !== 'archived').map(task => <option key={task.task_id} value={task.task_id}>{task.title} · {STATES[task.state] || task.state}</option>)}</select></label><small>选择后，本任务会保持等待；所有上游任务都完成并经过验收，服务才会自动放行。</small><label>共享资源标识（可选）<input value={(form.resources || []).join(',')} onChange={event => setForm({ ...form, resources: event.target.value.split(',').map(value => value.trim()).filter(Boolean) })} placeholder="例如：api、同一份配置文件" /></label><label>可接任务的伙伴<select multiple required={form.auto_run} value={form.eligible_agents} onChange={event => setForm({ ...form, eligible_agents: [...event.target.selectedOptions].map(option => option.value) })}>{activeAgents.filter(agent => agent.agent_id !== form.reviewer_agent_id).map(agent => <option key={agent.agent_id} value={agent.agent_id}>{agent.name} · {agent.model || agent.executor_kind}</option>)}</select></label><small>可以选择多位候选伙伴。自动领取时会按顺序尝试空闲伙伴；同一任务只会保留一份历史。</small><label>验收伙伴（可选）<select value={form.reviewer_agent_id || ''} onChange={event => setForm({ ...form, reviewer_agent_id: event.target.value || null, eligible_agents: form.eligible_agents.filter(id => id !== event.target.value) })}><option value="">暂不安排独立验收</option>{activeAgents.map(agent => <option key={agent.agent_id} value={agent.agent_id}>{agent.name} · 独立检查成果</option>)}</select></label><small>成果返回后，这位伙伴会自动读取真实文件、逐项判断，通过后继续下游任务，需修改时自动返工。验收伙伴与执行伙伴应当不同。</small>{form.reviewer_agent_id && <><label>自动返工次数<input type="number" min="0" step="1" required value={form.max_rework_rounds ?? 2} onChange={event => setForm({ ...form, max_rework_rounds: event.target.value === '' ? '' : Number(event.target.value) })} /><small>检查出问题后自动修改并再次送审，每轮可能产生 API 费用。填 0 只检查并保留意见。</small></label><label>检查连接失败后自动恢复次数<input type="number" min="0" step="1" required value={form.max_review_retries ?? 1} onChange={event => setForm({ ...form, max_review_retries: event.target.value === '' ? '' : Number(event.target.value) })} /><small>检查进程确认退出后，重新检查同一版成果；可能产生新的 API 费用，原失败请求保留。填 0 关闭。</small></label></>}<label>候选都失败后额外恢复次数<input type="number" min="0" step="1" required value={form.max_author_retries ?? 0} onChange={event => setForm({ ...form, max_author_retries: event.target.value === '' ? '' : Number(event.target.value) })} /><small>最后一位执行伙伴确认退出后，读取保存的现场继续；可能产生新的 API 费用。填 0 关闭，已有外部请求不会自动重放。</small></label><label className="check-row"><input type="checkbox" checked={form.auto_run} onChange={event => setForm({ ...form, auto_run: event.target.checked })} />依赖完成后自动领取并派发</label><small>执行与独立检查均可能产生 API 费用。关闭自动领取时先保存任务，再手动交给伙伴。</small><button className="primary-button" disabled={busy || !form.project_id}>{busy ? '保存中…' : '保存任务'}</button></form></div>}
  </section>;
}
