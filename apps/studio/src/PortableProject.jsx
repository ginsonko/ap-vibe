import { useRef, useState } from 'react';
import { UploadSimple } from '@phosphor-icons/react';
import { workspaceApi, requestId } from './workspace-api';
import { MessageContent } from './MessageContent';
import './portable-project.css';

const KEY = 'ap-vibe:portable-pending';
function pending() { try { return JSON.parse(sessionStorage.getItem(KEY) || 'null'); } catch { return null; } }
function remember(value) { try { value ? sessionStorage.setItem(KEY, JSON.stringify(value)) : sessionStorage.removeItem(KEY); } catch {} }

export function PortableProject({ onImported }) {
  const [saved] = useState(pending);
  const [open, setOpen] = useState(!!saved);
  const [file, setFile] = useState(null);
  const [name, setName] = useState('');
  const [root, setRoot] = useState('');
  const [draft, setDraft] = useState(saved?.draft || null);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const previewRequest = useRef(null);
  const confirmId = useRef(saved?.confirmId || requestId());
  const fileVersion = useRef(0);
  function reset() { setDraft(null); setResult(null); setError(''); previewRequest.current = null; confirmId.current = requestId(); remember(null); }
  async function choose(event) {
    const selected = event.target.files[0], version = ++fileVersion.current;
    reset(); setFile(null);
    if (!selected) return;
    try {
      if (selected.size > 2 * 1024 * 1024 + 20000) throw new Error('文件超过当前便携包的 2 MB 范围，请在原工作台重新导出当前项目。');
      const fileText = await selected.text(), raw = JSON.parse(fileText), bundle = raw.bundle || raw;
      if (bundle.protocol !== 'ap-vibe.portable-project.v1' || !bundle.payload || !bundle.content_hash) throw new Error('这不是 AP-Vibe 项目导出包。请从“项目与记忆 → 导出当前项目”保存 JSON 文件。');
      if (version !== fileVersion.current) return;
      // Targets belong to this machine; never reuse targets embedded in a file.
      setFile({name:selected.name, fileText}); setName((bundle.project?.display_name || '项目') + '（导入副本）');
    } catch (e) { if (version === fileVersion.current) setError(e instanceof SyntaxError ? '文件不是有效 JSON，可能尚未下载完成。请重新选择完整的导出文件。' : e.message); }
  }
  async function preview(event) {
    event.preventDefault(); if (busy || !file) return; setBusy(true); setError('');
    // Send the original JSON text: parsing and reserializing in JavaScript
    // changes 1.0 to 1 (and can round large integers), invalidating its hash.
    const payload = {file_text:file.fileText, target_root:root.trim(), target_display_name:name.trim()};
    const identity = JSON.stringify(payload);
    if (previewRequest.current?.identity !== identity) previewRequest.current = {identity, body:{...payload, request_id:requestId()}};
    try {
      const data = await workspaceApi('portable/import/preview', previewRequest.current.body);
      setDraft(data.draft); remember({draft:data.draft, confirmId:confirmId.current});
    } catch(e) { setError(e.message + '；本次请求已保留，重试会核对同一份预览。'); }
    finally { setBusy(false); }
  }
  async function confirm() {
    if (busy || !draft) return; setBusy(true); setError('');
    remember({draft, confirmId:confirmId.current});
    try {
      const data = await workspaceApi('portable/import/confirm', {request_id:confirmId.current, confirmation:{draft_id:draft.draft_id, bundle_hash:draft.content_hash}});
      setResult(data.project_id); remember(null);
    } catch(e) { setError(e.message + '；可以重试这次导入，已保存的阶段会继续，已有项目不会被覆盖。'); }
    finally { setBusy(false); }
  }
  return <div className="portable-project">
    <button className="secondary-button" onClick={()=>setOpen(!open)} aria-expanded={open}><UploadSimple size={16}/>导入项目</button>
    {open && <section className="portable-panel" aria-label="导入项目档案">
      <header><div><span className="eyebrow">把项目资料带到这里</span><h2>导入项目档案</h2></div><button className="text-action" disabled={busy} onClick={()=>setOpen(false)} aria-label="收起导入">收起</button></header>
      <p>选择另一套 AP-Vibe 导出的 JSON，再填写项目代码或文档在此电脑上的目录。导入会创建一个独立副本，旧项目保留。</p>
      {!draft && <form onSubmit={preview}>
        <label>1. 选择项目导出文件<input type="file" accept=".json,application/json" onChange={choose} disabled={busy}/></label>
        {file && <><p className="portable-file">已读取：{file.name}</p><label>2. 这个项目在此电脑的目录<input required disabled={busy} value={root} onChange={e=>setRoot(e.target.value)} placeholder="例如：D:\我的项目\设备借用登记"/></label><small>选择已有目录。代码和文件需要单独复制；这个包保存的是项目资料。</small><label>副本名称<input required disabled={busy} maxLength={160} value={name} onChange={e=>setName(e.target.value)}/></label><button className="primary-button" disabled={busy || !root.trim() || !name.trim()}>{busy ? '正在读取导入内容…' : '预览导入内容'}</button></>}
      </form>}
      {draft && <><h3>{draft.target_display_name}</h3><div className="portable-counts"><div><strong>{draft.documents?.chapter_count || 0}<small> / 11</small></strong><span>结构化档案章节</span></div><div><strong>{draft.documents?.assessment_documented_count || 0}<small> / 10</small></strong><span>带理由的维度评估</span></div><div><strong>{draft.documents?.source_revision ?? '—'}</strong><span>来源档案版本</span></div></div>
        {!draft.documents?.chapter_count && <p role="status" className="portable-warning">这份旧包没有结构化项目档案。可以导入历史恢复资料；要带走完整档案，请在更新后的原工作台重新导出。</p>}
        <p className="muted">包含 {draft.memory_count || 0} 条历史摘要。导入保留已有结论和未知分数；本地路径会脱敏，迁移后需补充新位置。代码、密钥、完整会话和所有旧版本不包含在内。</p>
        {result ? <div role="status"><p className="portable-success">导入已保存。可以打开新项目，按章继续阅读。</p><button className="primary-button" onClick={async()=>{await onImported(result);setOpen(false);reset();}}>打开导入的项目</button></div> : <div className="portable-actions"><button className="primary-button" disabled={busy} onClick={confirm}>{busy ? '正在保存项目档案…' : error ? '重试这次导入' : '创建副本并导入'}</button><button className="text-action" disabled={busy} onClick={reset}>重新选择</button></div>}
      </>}
      {error && <p role="alert" className="portable-warning">{error}</p>}
    </section>}
  </div>;
}

export function PortableHistory({ history }) {
  if (!history) return null;
  const memories = Array.isArray(history.memories) ? history.memories : [];
  return <details className="page-section portable-history"><summary>导入的历史摘要 · {memories.length} 条</summary><p className="muted">这些记录来自导出包，按原样留作历史参考，不会伪装成本机新活动或触发新的学习调用。当前项目的新记录继续单独保存。</p><div className="portable-history-scroll" tabIndex={0} aria-label="导入的历史摘要">{memories.map((item,i)=><article key={item.activity_id || i}><small>{item.occurred_at || '原记录未提供时间'}</small><MessageContent text={item.summary || item.activity?.summary || '原记录没有摘要'} annotations={false}/></article>)}</div></details>;
}
