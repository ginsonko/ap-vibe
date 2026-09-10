import { useCallback, useEffect, useRef, useState } from 'react';
import { workspaceApi, requestId } from './workspace-api';
import { MessageContent } from './MessageContent';

export function LogicWorkbench({ project, projects, onProjectChange, records, busy, onQuery, renderRecord, onChanged }) {
  const [status, setStatus] = useState(null), [error, setError] = useState('');
  const [root, setRoot] = useState(''), [question, setQuestion] = useState('');
  const [mode, setMode] = useState('codex'), [kind, setKind] = useState('module_responsibility');
  const [expectedPath, setExpectedPath] = useState(''), [working, setWorking] = useState(false);
  const current = useRef(project?.project_id); current.current = project?.project_id;
  const completed = useRef(new Set());
  const load = useCallback(async () => {
    const id = project?.project_id;
    if (!id) return;
    try {
      const value = await workspaceApi('logic/status?project_id=' + encodeURIComponent(id));
      if (current.current !== id) return;
      setStatus(value); setError('');
      setRoot(old => old || value.root || value.candidates?.find(c => c.available)?.path || '');
      for (const task of value.analysis_tasks || []) if (task.status === 'completed' && !completed.current.has(task.task_id)) {
        completed.current.add(task.task_id); onChanged?.();
      }
    } catch (e) { if (current.current === id) setError(e.message); }
  }, [project?.project_id]);
  useEffect(() => { setStatus(null); setRoot(''); setQuestion(''); load(); }, [load]);
  useEffect(() => {
    if (!status?.analysis_tasks?.some(t => ['running', 'applying'].includes(t.status))) return;
    const timer = setTimeout(load, 4000); return () => clearTimeout(timer);
  }, [status, load]);
  const act = async fn => { setWorking(true); setError(''); try { await fn(); await load(); } catch (e) { setError(e.message); } finally { setWorking(false); } };
  const analyze = event => {
    event.preventDefault();
    if (mode === 'native') { onQuery({kind, target: question.trim(), expectedPath}); return; }
    act(async () => {
      const result = await workspaceApi('logic/analyze', {request_id: requestId(), project_id: project.project_id, question: question.trim()});
      await workspaceApi('organization/dispatch', {task_id: result.task.task_id});
    });
  };
  const states = {ready_for_codex:'待开始',running:'正在分析',applying:'正在更新档案',completed:'已保存到项目档案',failed:'分析未完成',interrupted:'服务重启中断'};
  return <div className="page-stack logic-page">
    <div className="page-header"><div><span className="eyebrow">逻辑观察</span><h1>选一个项目，把它的逻辑查清楚</h1><p>用中文告诉 Codex 想查什么。结论、源码依据、待确认问题和项目档案会一起保存。</p></div></div>
    <section className="page-section"><h2>观察哪个项目</h2><label className="select-field"><span>已登记项目 · {projects.length} 个</span><select value={projects.some(p => p.project_id === project?.project_id) ? project.project_id : ''} onChange={e => e.target.value && onProjectChange(e.target.value)}><option value="">请选择项目</option>{projects.map(p => <option key={p.project_id} value={p.project_id}>{p.display_name}</option>)}</select></label>
      {!status ? <p role="status">正在读取项目代码入口…</p> : <><p>{status.reason}</p><div className="organization-toolbar"><label className="select-field logic-root-select"><span>代码目录（来自档案或已归类任务）</span><select value={root} onChange={e => setRoot(e.target.value)}><option value="">尚无可用入口</option>{[...new Set([status.root, ...status.candidates.map(c => c.path)].filter(Boolean))].map(p => <option key={p} value={p}>{p}</option>)}</select></label><button className="secondary-button" disabled={working || !root || project?.status !== 'active'} onClick={() => act(async () => { await workspaceApi('logic/configure', {project_id:project.project_id,root_path:root}); onChanged?.(); })}>保存观察目录</button></div><p className="muted">如果这里是空的，可以直接让 Codex 核对项目入口；它会报告缺少什么。密钥只记录存放位置。</p></>}
    </section>
    {error && <div className="notice-banner warn" role="alert">{error}<button className="text-action" onClick={load}>重新读取</button></div>}
    <section className="query-panel"><h2>你想知道什么？</h2><div className="project-filters"><button className={mode === 'codex' ? 'active' : ''} onClick={() => setMode('codex')}>交给 Codex 分析 · 推荐</button><button className={mode === 'native' ? 'active' : ''} onClick={() => setMode('native')}>本地静态查询 · Python</button></div>
      <form onSubmit={analyze} className="logic-query-form"><label><span>{mode === 'codex' ? '分析方式' : '观察类型'}</span>{mode === 'codex' ? <p>按需读取项目档案和源码，整理完整分析，再更新相关章节。使用本机已登录的 Codex，会产生 Codex 用量；无需 AP 教师 Key。</p> : <select value={kind} onChange={e => setKind(e.target.value)}><option value="module_responsibility">它负责什么</option><option value="change_impact">改这里会影响谁</option><option value="first_broken_link">预期链先断在哪</option></select>}</label>
        <label className="wide"><span>{mode === 'codex' ? '用中文描述问题' : '项目内相对文件路径或 Python 限定名'}</span><textarea value={question} onChange={e => setQuestion(e.target.value)} rows={4} required placeholder={mode === 'codex' ? '例如：用户点击开始后，数据经过哪些模块？哪里可能导致结果没有保存？请结合源码给出依据。' : '例如：src/service.py'}/></label>
        {mode === 'native' && kind === 'first_broken_link' && <label className="wide"><span>预期调用顺序</span><textarea value={expectedPath} onChange={e => setExpectedPath(e.target.value)} placeholder="入口函数 → 服务方法 → 保存方法"/></label>}
        <div className="query-example"><b>不知道从哪里开始？</b><button type="button" className="text-action" onClick={() => {setMode('codex');setQuestion('请核对这个项目的代码入口，说明从用户操作到结果保存的核心流程、各模块职责、主要风险和仍未实现的环节。给出源码位置，并把新发现更新进项目档案。');}}>填入“梳理项目核心流程”</button><p>静态关系只说明源码里的连接。运行是否正确，需要测试或实际运行证据。</p></div>
        <div className="query-actions"><span>分析过程与结果会保留，离开页面后可回来查看。</span><button className="primary-button" disabled={working || busy || !question.trim() || project?.status !== 'active' || (mode === 'native' && !status?.available)}>{working || busy ? '正在处理…' : mode === 'codex' ? '交给 Codex 分析并更新档案' : '开始本地观察'}</button></div>
      </form>
    </section>
    <section className="page-section"><h2>Codex 分析记录</h2>{status?.analysis_tasks?.length ? status.analysis_tasks.map(t => <article className="logic-observation-card" key={t.task_id}><span className="eyebrow">{states[t.status] || t.status} · {new Date(t.created_at).toLocaleString('zh-CN')}</span><h3>{t.result.question}</h3>{t.result.session_id && <a className="text-action" href={'codex://threads/'+t.result.session_id}>打开分析任务 ↗</a>}{t.result.error && <p role="alert">{t.result.detail || t.result.error}。已有项目档案保留。</p>}{t.result.analysis && <><MessageContent text={t.result.analysis.summary} annotations={false}/><details><summary>查看发现、证据与下一步 · 档案第 {t.result.document_revision} 版</summary>{t.result.analysis.findings.map((f,i) => <div key={i}><h4>{f.title}</h4><MessageContent text={f.detail} annotations={false}/><p>{f.evidence_refs?.join(' · ')}</p></div>)}<p>{t.result.analysis.next_action}</p><p>待确认：{t.result.analysis.unknown?.join('；') || '以本次报告范围为准'}</p></details></>}</article>) : <p className="muted">还没有 Codex 分析。输入一个问题即可开始，结束后这里会展示结论和档案版本。</p>}</section>
    <section className="page-section"><h2>本地静态观察 · {records.length} 份</h2>{records.length ? <div className="logic-observation-list">{records.slice(0,12).map(renderRecord)}</div> : <p className="muted">暂时没有静态报告。Python 可用本地查询；其他语言推荐交给 Codex 分析。</p>}</section>
  </div>;
}
