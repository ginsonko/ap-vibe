import { useEffect, useRef, useState } from 'react';
import { MessageContent } from './MessageContent';
import { CollaborationBoard } from './CollaborationBoard';
import { TaskBoard } from './TaskBoard';
import { ArtifactsPanel } from './ArtifactsPanel';
import { AgentPortrait, AppearancePicker, AppearanceProvider } from './AgentAppearance';
import {AgentOnboarding} from './AgentOnboarding';
import {StudioManagerPanel} from './StudioManagerPanel';
import { StudioOffice } from './StudioOffice';
import { AgentMetrics } from './AgentMetrics';
import { AgentBudget, Satiety } from './AgentBudget';
import { AgentRoutingEditor, AgentRoutingSummary } from './AgentRouting';
import { useDialogFocus } from './useDialogFocus';
import { appearanceTaskPrompt } from './appearance-task';
import './agent-studio.css';
import './studio-office.css';
import './office-animation.css';

const LABELS = { budget_paused: '等待投喂 · 成果保留', waiting: '等待上游成果', starting: '正在启动', running: '执行中', awaiting_review: '结果待验收', completed: '已验收', changes_requested: '需要修改', failed: '未能完成', uncertain: '结果待核对', interrupted: '已中断 · 成果保留', cancelling: '正在停止', cancelled: '已停止' };
const EMPTY = { executor_kind: 'claude', auth_mode: 'api_key', name: '', base_url: 'https://api.yinziapi.top/v1', api_key: '', model: '', role: '', avatar: 'fish', appearance_id: '', protocol: 'openai', request_timeout_seconds: 300, max_request_retries: 5 };
const uid = () => crypto.randomUUID();

function RunIssue({error}) {
  const summary = /insufficient.*quota|预扣费额度|余额不足/i.test(error)
    ? '模型服务返回额度不足，已保存的成果仍可检查。请核对服务账户或渠道额度，再决定是否继续。'
    : /timed?\s*out|timeout|超时|in flight/i.test(error)
      ? '本轮模型连接没有正常结束，已有成果和请求记录已保留。可以先检查成果，再处理剩余工作。'
      : '这次执行没有正常结束，已有成果已保留。查看原始原因后可以继续处理。';
  return <div className="agent-notice" role="status"><p>{summary}</p><details><summary>查看原始错误与请求编号</summary><pre style={{whiteSpace:'pre-wrap',overflowWrap:'anywhere'}}>{error}</pre></details></div>;
}

async function api(path, payload, signal) {
  const res = await fetch('/v1/ap-vibe/agents' + path, {
    method: payload ? 'POST' : 'GET', headers: { 'Content-Type': 'application/json' },
    body: payload ? JSON.stringify(payload) : undefined, signal,
  });
  const value = await res.json();
  if (!res.ok || value.ok === false) throw new Error(value.error?.message || value.message || '请求未完成，请重试或查看服务状态。');
  return value;
}

export function AgentStudio(props) {
  return <AppearanceProvider><AgentStudioContent {...props}/></AppearanceProvider>;
}

function AgentStudioContent({ projects, project, currentProjectId, projectsLoading }) {
  const [snapshot, setSnapshot] = useState(null);
  const [runs, setRuns] = useState([]);
  const [selected, setSelected] = useState(null);
  const [selectedDetail, setSelectedDetail] = useState(null);
  const [events, setEvents] = useState([]);
  const [form, setForm] = useState(null);
  const [budgetAgent, setBudgetAgent] = useState(null);
  const [appearanceDraft, setAppearanceDraft] = useState(null);
  const [agentId, setAgentId] = useState('');
  const [projectId, setProjectId] = useState(currentProjectId || project?.project_id || '');
  useEffect(() => { setProjectId(old => old || currentProjectId || project?.project_id || ''); }, [currentProjectId, project?.project_id]);
  const [prompt, setPrompt] = useState('');
  const [mediaTools, setMediaTools] = useState(false);
  const [error, setError] = useState('');
  const [listError, setListError] = useState('');
  const [eventError, setEventError] = useState('');
  const [notice, setNotice] = useState('');
  const [busy, setBusy] = useState(false);
  const partnerDialog = useDialogFocus(() => setForm(null), busy, !!form);
  const [continueFrom, setContinueFrom] = useState(null);
  const [dependsOn, setDependsOn] = useState([]);
  const [reviewNote, setReviewNote] = useState('');
  const [reviewEvidence, setReviewEvidence] = useState([]);
  const [tab, setTab] = useState('office');
  const [collab, setCollab] = useState(null);
  const scroll = useRef(null);
  const outputPanel = useRef(null);
  const taskForm = useRef(null);
  const following = useRef(true);
  const request = useRef(null);
  const reviewRequest = useRef(null);
  const activeAgents = snapshot?.agents.filter(a => !a.archived) || [];
  const selectedRun = selectedDetail?.run_id === selected ? selectedDetail : runs.find(r => r.run_id === selected);
  const chosenAgent = activeAgents.find(a => a.agent_id === agentId);
  const executorReady = snapshot?.executors?.find(e=>e.id===(chosenAgent?.executor_kind||'claude'))?.available ?? (chosenAgent?.executor_kind==='codex'?snapshot?.codex_available:chosenAgent?.executor_kind==='claude'?snapshot?.claude_available:false);
  const nameOf = id => snapshot?.agents.find(a => a.agent_id === id)?.name || id;
  const dependencies = selectedRun?.depends_on ? [selectedRun.depends_on].flat() : [];

  function reveal(panel) {
    requestAnimationFrame(() => {
      panel.current?.focus({preventScroll: true});
      panel.current?.scrollIntoView({block: 'start', behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth'});
    });
  }

  async function reload(signal) {
    const [next, tasks, coordination] = await Promise.all([api('', null, signal), api('/runs', null, signal), fetch('/v1/ap-vibe/collaboration', {signal}).then(async r=>{if(!r.ok) throw new Error('协作状态暂时无法读取'); return r.json();})]);
    if (signal?.aborted) return;
    setListError('');
    setSnapshot(next); setRuns(tasks.runs); setCollab(coordination);
    setAgentId(old => next.agents.some(a => a.agent_id === old && !a.archived) ? old : next.agents.find(a => !a.archived)?.agent_id || '');
    setSelected(old => old || tasks.runs[0]?.run_id || null);
  }
  useEffect(() => {
    const controller = new AbortController(); let timer;
    async function poll() {
      try { if (!document.hidden) await reload(controller.signal); }
      catch (e) { if (!controller.signal.aborted) setListError('伙伴列表暂时无法刷新，保留上次数据并自动重连。'); }
      if (!controller.signal.aborted) timer = setTimeout(poll, 4000);
    }
    poll(); return () => { controller.abort(); clearTimeout(timer); };
  }, []);
  useEffect(() => {
    const controller = new AbortController(); let timer, cursor = 0;
    setEvents([]); setReviewNote(''); setReviewEvidence([]); setEventError(''); following.current = true;
    if (!selected) return () => controller.abort();
    async function poll() {
      try {
        const data = await api('/runs?run_id=' + encodeURIComponent(selected) + '&after=' + cursor, null, controller.signal);
        if (controller.signal.aborted) return;
        setEventError('');
        if (data.runs?.[0]) setSelectedDetail(data.runs[0]);
        if (data.runs?.[0]) setRuns(old => old.some(r => r.run_id === selected) ? old.map(r => r.run_id === selected ? data.runs[0] : r) : [data.runs[0], ...old]);
        if (data.events.length) {
          cursor = data.next_cursor;
          setEvents(old => [...old, ...data.events]);
        }
        timer = setTimeout(poll, data.has_more ? 50 : 1800);
      } catch(e) {
        if (!controller.signal.aborted) { setEventError('任务消息暂时无法刷新，已有输出保留；连接恢复后继续读取。'); timer = setTimeout(poll, 4000); }
      }
    }
    poll(); return () => { controller.abort(); clearTimeout(timer); };
  }, [selected]);
  useEffect(() => { if (following.current && scroll.current) scroll.current.scrollTop = scroll.current.scrollHeight; }, [events]);
  useEffect(() => { if (!projectId && currentProjectId) setProjectId(currentProjectId); }, [currentProjectId, projectId]);

  async function act(action) {
    setBusy(true); setError(''); setNotice('');
    try { await action(); await reload(); return true; } catch (e) { setError(e.message); return false; }
    finally { setBusy(false); }
  }
  async function save(e) {
    e.preventDefault();
    await act(async () => {
      const data = await api('/save', { ...form, expected_revision: form.revision || 0 });
      setAgentId(data.agent.agent_id); setForm(null);
      setNotice(data.agent.auth_mode === 'local_login' ? '伙伴已保存，沿用本机 Codex 连接；保存配置没有调用模型。' : data.agent.activated===false ? '伙伴草稿已保存，补齐服务地址、模型和 Key 后即可激活；保存没有调用模型。' : '伙伴已保存，Key 已加密；保存配置没有调用模型。');
    });
  }
  async function start(e) {
    e.preventDefault();
    const identity = JSON.stringify([agentId, projectId, prompt, continueFrom, dependsOn, mediaTools]);
    if (!request.current || request.current.identity !== identity) request.current = { identity, id: uid() };
    const started = await act(async () => {
      const data = await api('/start', { request_id: request.current.id, agent_id: agentId, project_id: projectId, prompt, continue_run_id: continueFrom, depends_on: dependsOn.length ? dependsOn : null, ...(continueFrom?{}:{extensions:mediaTools?['yinzi-media']:[]}) });
      setSelected(data.run_id); setTab('runs'); setNotice('任务已登记。可以在任务输出区查看进展；模型返回后仍需检查成果。');
      request.current = null; setContinueFrom(null); setDependsOn([]);
    });
    if (started) reveal(outputPanel);
  }
  async function review(accepted) {
    const payload = {run_id: selected, accepted, note: reviewNote, evidence_refs: reviewEvidence, reviewer: '工作台操作', expected_revision: selectedRun?.review?.revision || 0};
    const identity = JSON.stringify(payload);
    if (!reviewRequest.current || reviewRequest.current.identity !== identity) reviewRequest.current = {identity, id: uid()};
    await act(async () => {
      await api('/review', {...payload, request_id: reviewRequest.current.id});
      reviewRequest.current = null; setReviewNote(''); setReviewEvidence([]);
      setNotice(accepted ? '已记录验收结论；它表示本次成果通过检查，不代表模型所有任务都可靠。' : '已记录待修改项，可继续同一会话处理。');
    });
  }

  return <div className="agent-studio">
    <section className="agent-hero">
      <div><span className="eyebrow">AP-Vibe / Agent 工作室</span><h1>伙伴协作，进展尽在眼前</h1><p>安排工作、互相交接、检查成果。</p></div>
      <button className="primary-button" onClick={() => setForm({ ...EMPTY })}>＋ 添加伙伴</button>
    </section>
    {error && <div role="alert" className="agent-error">{error}<button onClick={() => { setError(''); reload().catch(e => setError(e.message)); }}>重新读取</button></div>}
    {(listError || eventError) && <p role="status" className="agent-notice">{[listError, eventError].filter(Boolean).join(' ')}</p>}
    {notice && <p role="status" className="agent-notice">{notice}</p>}
    {appearanceDraft&&<button className="text-action" onClick={()=>{setForm(appearanceDraft);setAppearanceDraft(null);}}>继续编辑伙伴配置</button>}
    {!snapshot ? <section className="page-section" aria-busy="true">正在读取伙伴与任务，请稍候…</section> : <>
      <section className="agent-summary">
        <article><strong>{activeAgents.filter(a=>a.activated!==false).length}</strong><span>已激活伙伴 · 共 {activeAgents.length} 位</span></article>
        <article><strong>{runs.filter(r => ['starting','running'].includes(r.state)).length}</strong><span>正在执行</span></article>
        <article><strong>{runs.filter(r => r.state === 'awaiting_review').length}</strong><span>等待成果验收</span></article>
        <article><strong>{snapshot.executors?.filter(e=>e.available).length ?? [snapshot.codex_available,snapshot.claude_available].filter(Boolean).length}</strong><span>本机可用执行端</span></article>
      </section>
      <div className="agent-tabs"><button aria-pressed={tab === 'office'} onClick={() => setTab('office')}>工作室</button><button aria-pressed={tab === 'runs'} onClick={() => setTab('runs')}>任务与输出</button><button aria-pressed={tab === 'team'} onClick={() => setTab('team')}>伙伴与配置</button><button aria-pressed={tab === 'metrics'} onClick={() => setTab('metrics')}>实测表现</button><button aria-pressed={tab === 'coordination'} onClick={() => setTab('coordination')}>协作看板</button><button aria-pressed={tab === 'tasks'} onClick={() => setTab('tasks')}>任务池</button></div>
      {tab === 'metrics' ? <AgentMetrics profiles={snapshot.agents} openRun={id=>{setSelected(id);setTab('runs');}} editAgent={a=>setForm({...EMPTY,...a,api_key:''})} arrange={id=>{setAgentId(id);setContinueFrom(null);setDependsOn([]);setTab('runs');}}/> : tab === 'office' ? <StudioOffice agents={snapshot.agents} projects={projects} openRun={id=>{setSelected(id);setTab('runs');}} arrange={id=>{setAgentId(id);setContinueFrom(null);setDependsOn([]);setTab('runs');}} editAgent={a=>setForm({...EMPTY,...a,api_key:''})} openTasks={()=>setTab('tasks')} openMessages={()=>setTab('coordination')}/> : tab === 'tasks' ? <TaskBoard projects={projects} projectsLoading={projectsLoading} projectId={projectId} agents={snapshot?.agents || []} openRun={id=>{setSelected(id);setTab('runs');}}/> : tab === 'coordination' ? <CollaborationBoard collab={collab} runs={runs} agents={activeAgents} nameOf={nameOf} openRun={id=>{setSelected(id);setTab('runs');}}/> : tab === 'team' ? <><AgentOnboarding agents={snapshot.agents} onSaved={reload}/><StudioManagerPanel agents={activeAgents} openRun={id=>{setSelected(id);setTab('runs');}}/><section className="agent-roster">{activeAgents.map(a => <article className="agent-card" key={a.agent_id}>
        <AgentPortrait profile={a} /><h2>{a.name}</h2><code>{a.model || (a.auth_mode==='local_login'?'本机 Codex 模型':'尚未填写模型')}</code><small>{snapshot.executors?.find(e=>e.id===a.executor_kind)?.name || a.executor_kind || 'Claude Code'}</small><p>{a.role || '尚未填写职责，可根据任务自由分工。'}</p><small>{a.base_url}</small>{a.activated===false&&<p className="agent-notice">未激活 · 请补充 {a.missing_configuration?.map(k=>({base_url:'URL',model:'模型',api_key:'Key'}[k])).join('、')}</p>}<p>{a.auth_mode === 'local_login' ? '沿用本机 Codex 连接' : a.key_saved?'Key 已加密':'尚未填写 Key'} · 配置第 {a.revision} 版</p><p>近期本版配置：{runs.filter(r=>r.agent_id===a.agent_id && r.profile_revision===a.revision && ['awaiting_review','completed','changes_requested'].includes(r.state)).length} 次返回成果，{runs.filter(r=>r.agent_id===a.agent_id && r.profile_revision===a.revision && r.state==='completed').length} 次记录验收通过。实际能力以具体任务为准。</p>
        <p>{a.connection_label&&<b>{a.connection_label}</b>}{a.capability_notes&&<small> · {a.capability_notes}</small>}</p><AgentRoutingSummary value={a.routing_profile}/><Satiety budget={a.budget}/><div className="agent-actions"><button onClick={()=>setBudgetAgent(a)}>用量与投喂</button><button onClick={() => setForm({ ...EMPTY, ...a, api_key: '' })}>编辑</button><button onClick={() => setForm({ ...EMPTY, executor_kind: a.executor_kind || 'claude', auth_mode: a.auth_mode || 'api_key', name: a.name + '·新伙伴', base_url: a.base_url, model: a.model, role: a.role, persona: a.persona || '', connection_label:a.connection_label||'', capability_notes:a.capability_notes||'', avatar: a.avatar, appearance_id: a.appearance_id || '', protocol: a.protocol, upstream_mode: a.upstream_mode || 'stream', request_timeout_seconds: a.request_timeout_seconds ?? 300, max_request_retries: a.max_request_retries ?? 5, key_saved: true, copy_from_agent_id: a.agent_id, copy_from_revision: a.revision })}>复制配置</button><button disabled={busy} onClick={() => act(async () => { await api('/archive', { agent_id: a.agent_id, expected_revision: a.revision }); setNotice('伙伴已归档，历史任务与成果保留。'); })}>归档</button></div>
      </article>)}{!activeAgents.length && <div className="page-section"><h2>先添加一位伙伴</h2><p>例如“大肥鱼·前端”，填入服务地址、Key 和模型即可。可以为同一模型创建多个独立伙伴。</p><button onClick={() => setForm({ ...EMPTY })}>添加第一位伙伴</button></div>}</section></> : <div className="agent-main">
        <div>
          <form ref={taskForm} tabIndex={-1} style={{scrollMarginTop:100}} className="page-section agent-task-form" onSubmit={start}><h2>{continueFrom ? '接着上次任务继续' : '安排一件事'}</h2>
            {continueFrom && <p role="status">沿用原会话历史和成果目录。<button type="button" onClick={()=>setContinueFrom(null)}>改为独立新任务</button></p>}
            <label>交给谁<select required disabled={!!continueFrom} value={agentId} onChange={e => setAgentId(e.target.value)}><option value="">选择伙伴</option>{activeAgents.map(a => <option disabled={a.activated===false||a.management_reserved} key={a.agent_id} value={a.agent_id}>{a.name} · {a.model||'待配置'}{a.management_reserved?' · 专职管理':''}</option>)}</select></label>
            <label>参考哪个项目<select required disabled={!!continueFrom} value={projectId} onChange={e => {setProjectId(e.target.value);setDependsOn([]);}}><option value="">选择项目</option>{projects.map(p => <option key={p.project_id} value={p.project_id}>{p.display_name}</option>)}</select></label>
            <label>这次希望得到什么<textarea required value={prompt} onChange={e => setPrompt(e.target.value)} placeholder="例如：按需读取这个项目的简介和设计，将你理解的目标写成当前目录中的 summary.md，再列出三个需要核实的问题。" rows={5}/></label>
            {!continueFrom && <label className="agent-media-choice"><input type="checkbox" checked={mediaTools} onChange={e=>setMediaTools(e.target.checked)}/><span>使用已安装的媒体工作流<small>带入生图工具和本地素材处理能力。生成图片可能产生API费用；未安装时会说明缺口。</small></span></label>}
            {!continueFrom && <details open={dependsOn.length>0 || undefined}><summary>先等哪些任务完成（可选）{dependsOn.length ? ` · 已选 ${dependsOn.length} 项` : ' · 当前立即执行'}</summary><div style={{maxHeight:220,overflowY:'auto'}}>{runs.filter(r=>r.project_id===projectId && !['failed','uncertain','interrupted','cancelled','changes_requested','cancelling'].includes(r.state)).map(r=><label key={r.run_id} style={{display:'flex',gap:8,alignItems:'start',fontWeight:400}}><input type="checkbox" style={{width:'auto'}} checked={dependsOn.includes(r.run_id)} onChange={e=>setDependsOn(old=>e.target.checked ? [...old,r.run_id] : old.filter(id=>id!==r.run_id))}/><span>{r.name} · {r.prompt.slice(0,45)} · {LABELS[r.state]}</span></label>)}</div><small>例如：两位伙伴同时写说明和排障清单，验收员勾选两项后会等全部完成再自动开始。真实成果目录会自动提供，你无需复制路径。</small></details>}
            <small>成果保存在独立任务目录。开始后会使用所选服务的 API 费用或 Codex 账户额度。{chosenAgent?.executor_kind==='claude'&&snapshot.features?.request_retries ? (chosenAgent.max_request_retries===0?'这位伙伴已关闭模型请求自动重试。':`偶发模型请求失败后最多重试 ${chosenAgent.max_request_retries??5} 次，每次尝试可能产生用量。`) : '失败后保留已有成果，可查看错误与接手安排。'}</small>
            <button className="primary-button" disabled={busy || !chosenAgent || chosenAgent.activated===false || chosenAgent.management_reserved || !executorReady}>开始执行</button>
          </form>
          <section className="page-section"><h2>最近任务</h2><div className="agent-run-list">{runs.map(r => <button key={r.run_id} className={selected === r.run_id ? 'selected' : ''} onClick={() => {setSelected(r.run_id);reveal(outputPanel);}}><b><AgentPortrait profile={r} compact />{r.name}</b><span>{r.task_snapshot?.title || (r.coordination_only?'管理员协调判断':r.prompt?.slice(0,90))}</span><small>{LABELS[r.state] || '状态待确认'} · {new Date(r.created_at).toLocaleString('zh-CN')}</small></button>)}{!runs.length && <p>还没有任务。先给伙伴安排一个可以独立完成的小目标。</p>}</div></section>
        </div>
        <section ref={outputPanel} tabIndex={-1} style={{scrollMarginTop:100}} className="page-section agent-output"><div className="agent-output-head"><h2>{selectedRun?.name || '任务输出'}</h2>{selectedRun && <span>{LABELS[selectedRun.state] || '状态待确认'}</span>}</div>
          {!selectedRun ? <div className="agent-empty"><span>↖</span><h3>选一项任务，看看伙伴在做什么</h3><p>公开消息、工具名称和执行结果会出现在这里。没有输出时会保留真实等待状态。</p></div> : <>
            <p className="muted">{selectedRun.model} · 独立配置第 {selectedRun.profile_revision} 版</p>
            {!!dependencies.length && <div className="agent-notice">上游：{dependencies.map(id=><button key={id} onClick={()=>setSelected(id)}>{runs.find(r=>r.run_id===id)?.name || '查看上游任务'} · {LABELS[runs.find(r=>r.run_id===id)?.state] || '读取中'}</button>)}<p>{selectedRun.state==='waiting' ? '等待全部上游正常返回；结果不确定时保留等待，不重复请求。' : '已自动提供成果清单，伙伴可按需读取并核验。'}</p></div>}
            {selectedRun.document_maintenance?.revision && <p className="agent-notice">项目档案第 {selectedRun.document_maintenance.revision} 版 · {selectedRun.document_maintenance.readback_complete ? '全部更新章节已回读' : '已写入，尚未观察到完整回读'}。这是写入回执，内容质量仍需验收。</p>}
            <div className="agent-timeline" ref={scroll} onScroll={() => { const el=scroll.current; following.current = el.scrollHeight-el.scrollTop-el.clientHeight < 60; }}>
              {events.map(e => <article key={e.seq} className={'agent-event ' + e.kind}><small>{new Date(e.created_at).toLocaleTimeString('zh-CN')} · {e.kind === 'assistant' ? '伙伴' : e.kind === 'tool' ? '工具' : '状态'}</small><MessageContent text={e.provider?.error ? (e.text+'\n\n'+e.provider.error) : e.text || ''} annotations={false}/></article>)}
              {!events.length && <p>正在等待执行器事件；这不代表任务已经完成。</p>}
            </div>
            <button className="text-action" onClick={() => { following.current=true; scroll.current.scrollTop=scroll.current.scrollHeight; }}>回到最新消息</button>
            <ArtifactsPanel key={selectedRun.run_id} runId={selectedRun.run_id} runState={selectedRun.state} onEvidence={(selectedRun.artifact_recovery_available || ['awaiting_review','completed','changes_requested'].includes(selectedRun.state)) ? name=>setReviewEvidence(old=>old.includes(name)?old:[...old,name]) : undefined}/>
            {selectedRun.error && <RunIssue error={selectedRun.error}/>}
            {selectedRun.review && <p className="agent-notice">最近验收：{selectedRun.review.note} · {selectedRun.review.reviewer}</p>}
            {selectedRun.state==='budget_paused' && <button onClick={()=>{setContinueFrom(selected);setDependsOn([]);setAgentId(selectedRun.agent_id);setProjectId(selectedRun.project_id);setPrompt('额度已补充，核对已完成成果后继续剩余工作。');reveal(taskForm);}}>投喂后接着做</button>}
            {(['awaiting_review','completed','changes_requested'].includes(selectedRun.state) || selectedRun.artifact_recovery_available) && <div className="agent-task-form">
              {selectedRun.artifact_recovery_available && <p className="agent-notice">模型连接已结束，已保存的成果仍可检查。先在上方打开成果文件并点击“用于本次验收”，再填写结论；原来的连接错误和用量记录会保留。</p>}
              {!!reviewEvidence.length && <p>本次检查的文件：{reviewEvidence.map(name=><button key={name} onClick={()=>setReviewEvidence(old=>old.filter(item=>item!==name))} title="从本次验收移除">{name} ×</button>)}</p>}
              <label>检查成果后的结论<textarea value={reviewNote} onChange={e=>setReviewNote(e.target.value)} placeholder="例如：已打开成果文件并核对项目名称；还需要补充什么…" rows={2}/></label>
              <div className="agent-actions"><button disabled={busy || !reviewNote.trim() || (selectedRun.artifact_recovery_available && !reviewEvidence.length)} onClick={()=>review(true)}>记录验收通过</button><button disabled={busy || !reviewNote.trim() || (selectedRun.artifact_recovery_available && !reviewEvidence.length)} onClick={()=>review(false)}>记录需要修改</button>
                <button disabled={busy} onClick={()=>{setContinueFrom(selected);setDependsOn([]);setAgentId(selectedRun.agent_id);setProjectId(selectedRun.project_id);setPrompt('');setNotice('已选择接续，请在任务输入区填写下一步要求，然后点击开始执行。');reveal(taskForm);}}>继续这个会话</button>
                <button disabled={busy || selectedRun.state==='changes_requested' || selectedRun.artifact_recovery_available} onClick={()=>{setContinueFrom(null);setDependsOn([selected]);setProjectId(selectedRun.project_id);setAgentId(activeAgents.find(a=>a.agent_id!==selectedRun.agent_id)?.agent_id||selectedRun.agent_id);setPrompt('读取 ap-vibe-dependency.json 中的真实成果，对照原任务目标独立检查。在当前目录写 acceptance.md，列出通过项、具体问题、证据与建议。不要仅采信上游自述。');setNotice('已准备验收任务，确认伙伴后点击开始执行。');reveal(taskForm);}}>安排伙伴核验</button></div>
            </div>}
            <details><summary>成果位置与执行收据</summary><p>成果目录</p><code className="agent-path">{selectedRun.workspace}</code><p>会话：{selectedRun.session_id}</p><p>费用：{selectedRun.result?.total_cost_usd == null ? '尚无执行器费用回报；不能按零费用计算' : `执行器估计 $${selectedRun.result.total_cost_usd}，最终以服务账单为准`}</p><p>执行器退出与模型回答不代表成果验收通过。</p></details>
            {['waiting','starting','running'].includes(selectedRun.state) && <button disabled={busy} onClick={() => act(async () => { await api('/cancel', { run_id: selected }); })}>{selectedRun.state==='waiting' ? '取消等待任务' : '停止本次任务'}</button>}
          </>}
        </section>
      </div>}
    </>}
    <details className="page-section"><summary>第一次使用？看一个完整例子</summary><ol><li>在“伙伴与配置”添加推荐模板并填写 Key；其它服务可修改地址和模型。保存本身不调用模型，是否连通看首次真实任务。</li><li>打开自动协作，再照常提出值得拆分的需求，例如改善个人网站并独立检查手机显示。管理伙伴会安排合适的人选和依赖，简单事情直接完成。</li><li>点击伙伴查看“过程与成果”，最后回到原任务看汇总；故事回放可选择倍速、暂停查看原文。</li></ol><p>同一模型可以创建多个独立伙伴。外观可以换成自己的PNG或动作图集；岗位显示依据公开工具事件，未知阶段显示在通用执行区。静态外观不代表已经具备完整走路动作。</p></details>
    {budgetAgent && <AgentBudget agent={budgetAgent} onClose={()=>setBudgetAgent(null)} onSaved={()=>reload()}/>}
    {form && <div className="agent-dialog-backdrop" onClick={e => { if(e.target===e.currentTarget && !busy) setForm(null); }}><form ref={partnerDialog} className="agent-dialog" role="dialog" aria-modal="true" aria-label="配置伙伴" onSubmit={save}><div className="agent-output-head"><h2>{form.agent_id ? '编辑伙伴' : '添加伙伴'}</h2><button type="button" disabled={busy} aria-label="关闭" onClick={() => setForm(null)}>×</button></div>
      {form.agent_id && <button type="button" onClick={()=>{setBudgetAgent(snapshot.agents.find(a=>a.agent_id===form.agent_id));setForm(null);}}>用量、参考价格与投喂</button>}
      <label>由谁来执行<select value={form.executor_kind || 'claude'} onChange={e=>setForm({...form,executor_kind:e.target.value,protocol:e.target.value==='codex'?'responses':'openai',auth_mode:e.target.value==='codex'?'local_login':'api_key'})}>{(snapshot?.executors||[{id:"claude",name:"Claude Code",available:snapshot?.claude_available},{id:"codex",name:"Codex",available:snapshot?.codex_available}]).map(e=><option key={e.id} value={e.id}>{e.name}{e.available?"":" · 本机尚未发现"}</option>)}</select></label>
      {form.executor_kind==='codex' && <label>连接方式<select value={form.auth_mode} onChange={e=>setForm({...form,auth_mode:e.target.value})}><option value="local_login">沿用这台电脑的 Codex 连接和登录</option><option value="api_key">自己填写模型服务</option></select></label>}
      {[['name','伙伴名称','例如：大肥鱼·前端'],['base_url','服务地址','https://你的服务地址/v1'],['api_key','API Key',form.key_saved?'留空保留已加密的Key':'只保存到本机加密配置'],['model','模型名称',form.auth_mode==='local_login'?'可留空沿用本机 Codex 模型':'填写服务支持的完整模型名称']].filter(([key])=>form.auth_mode!=='local_login'||!['base_url','api_key'].includes(key)).map(([key,label,placeholder]) => <label key={key}>{label}<input data-dialog-initial-focus={key==='name'||undefined} required={key==='name'} type={key==='api_key'?'password':'text'} autoComplete="off" value={form[key]} placeholder={placeholder} onChange={e => setForm({...form,[key]:e.target.value})}/></label>)}
      <details><summary>形象、人设与职责（可选）</summary><label>连接档位名称（选填）<input value={form.connection_label||''} maxLength={120} onChange={e=>setForm({...form,connection_label:e.target.value})} placeholder="例如：Kiro · 经济渠道 / CC Max · 专用渠道"/></label><label>渠道能力说明（选填）<textarea value={form.capability_notes||''} maxLength={2000} onChange={e=>setForm({...form,capability_notes:e.target.value})} placeholder="说明适合什么难度和成本偏好；管理员也会参考实际任务记录。"/></label><label>角色人设（选填）<textarea value={form.persona || ''} maxLength={4000} onChange={e=>setForm({...form,persona:e.target.value})} placeholder="例如：沉稳的前端设计师，讲究留白与层级，提出建议时附上具体依据。留空也能正常工作。"/></label><AppearancePicker value={form.appearance_id} onChange={appearance_id => setForm({...form,appearance_id})} onCreate={({appearance,requirements})=>{setPrompt(appearanceTaskPrompt({name:form.name,appearance,requirements,origin:window.location.origin}));setMediaTools(true);setContinueFrom(null);setDependsOn([]);setAppearanceDraft(form);setForm(null);setTab('runs');setNotice('已准备外观制作任务，并选中媒体工作流工具。选择制作伙伴与项目后开始，成果会保存在该任务目录。');reveal(taskForm);}} /><label>擅长什么<textarea value={form.role} onChange={e=>setForm({...form,role:e.target.value})} placeholder="例如：擅长前端布局；优先提供可检查的文件和截图。"/></label></details>
      <details><summary>分工简历与费用偏好（可修改）</summary><AgentRoutingEditor value={form.routing_profile} onChange={routing_profile=>setForm({...form,routing_profile})}/></details>
      <label>连接格式<select value={form.protocol} onChange={e=>setForm({...form,protocol:e.target.value})}>{form.executor_kind==='codex' ? <option value="responses">OpenAI Responses（Codex 原生支持）</option> : <><option value="openai">OpenAI Chat Completions（多数中转服务）</option>{form.executor_kind==='claude'&&<option value="anthropic">Anthropic Messages（原生 Claude 接口）</option>}</>}</select></label>
      {form.executor_kind==='claude'&&snapshot?.features?.buffered_upstream&&<label>上游响应方式<select value={form.upstream_mode||'stream'} onChange={e=>setForm({...form,upstream_mode:e.target.value})}><option value="stream">流式响应（默认）</option><option value="buffered">完整响应（工具流兼容）</option></select><small>遇到服务的工具流异常时可选完整响应。开启请求重试时也会在整轮成功后显示，避免失败流重复触发工具。</small></label>}
      {['claude','codex'].includes(form.executor_kind)&&<label>模型响应等待时间（秒）<input type="number" min="1" step="1" required value={form.request_timeout_seconds ?? 300} onChange={e=>setForm({...form,request_timeout_seconds:Number(e.target.value)})}/></label>}
      {form.executor_kind==='claude'&&snapshot?.features?.request_retries&&<label>偶发请求失败后，最多重试几次<input type="number" min="0" step="1" required value={form.max_request_retries??5} onChange={e=>setForm({...form,max_request_retries:Number(e.target.value)})}/><small>默认重试 5 次（加上首次最多 6 次尝试）；0 为关闭。成功后继续同一个 CC 任务，不重做已完成工具。重试可能增加 API 费用；认证和参数错误直接报告，次数用尽再交给管理员。</small></label>}
      {form.executor_kind==='claude'?<small>每次尝试默认等待 300 秒，慢速模型可以调大。CC 的偶发错误按上方次数恢复，已完成工具不重做。</small>:form.executor_kind!=='codex'&&<small>此执行端的模型请求等待与重试由原生客户端控制。工作室保留真实退出状态和成果；已发出的请求可能产生费用。</small>}
      <p className="muted">Codex 自定义服务需支持 Responses；Chat Completions 服务可选择支持该格式的其它执行端。保存不调用模型；每次新执行使用保存的配置，已运行任务保留原配置。</p>
      <button className="primary-button" disabled={busy}>{busy?'保存中…':'保存伙伴'}</button>
    </form></div>}
  </div>;
}
