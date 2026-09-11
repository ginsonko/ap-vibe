import { useEffect, useState } from 'react';
import { ArrowClockwise } from '@phosphor-icons/react';
import { MessageContent } from './MessageContent';

const runtimeFile = name => name.startsWith('.agents/skills/ap-vibe-task-context/') ||
  ['ap-vibe-run.json', 'ap-vibe-handoff.json', 'ap-vibe-dependency.json', 'ap-vibe-messages.json'].includes(name);

async function readFiles(runId, name, signal) {
  const controller = new AbortController();
  const abort = () => controller.abort();
  signal.addEventListener('abort', abort, { once: true });
  if (signal.aborted) controller.abort();
  const timer = setTimeout(abort, 10000);
  try {
    const params = new URLSearchParams({ run_id: runId });
    if (name !== undefined) params.set('name', name);
    const response = await fetch('/v1/ap-vibe/agents/artifacts?' + params, { signal: controller.signal });
    const data = await response.json();
    if (!response.ok || data.ok === false) throw new Error(data.error?.message || '成果读取未完成');
    if (data.run_id !== runId || (name !== undefined && data.name !== name)) throw new Error('成果来源与当前任务不一致');
    return data;
  } finally { clearTimeout(timer); signal.removeEventListener('abort', abort); }
}

export function ArtifactsPanel({ runId, runState, onEvidence }) {
  const [listing, setListing] = useState(null);
  const [selected, setSelected] = useState('');
  const [preview, setPreview] = useState(null);
  const [listError, setListError] = useState('');
  const [readError, setReadError] = useState('');
  const [loading, setLoading] = useState(true);
  const [reading, setReading] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const active = ['starting', 'running', 'cancelling', 'waiting'].includes(runState);
  useEffect(() => {
    const controller = new AbortController(); let timer;
    async function load() {
      setLoading(true);
      try {
        const data = await readFiles(runId, undefined, controller.signal);
        if (controller.signal.aborted) return;
        setListing(data); setListError('');
        setSelected(old => old || data.files.find(file => !runtimeFile(file.name))?.name || data.files[0]?.name || '');
      } catch (e) {
        if (!controller.signal.aborted) setListError('成果列表暂时无法刷新，已加载的内容保留。可稍后刷新。');
      } finally {
        if (!controller.signal.aborted) {
          setLoading(false);
          if (active) timer = setTimeout(load, 6000);
        }
      }
    }
    load();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [runId, active, refresh]);
  const file = listing?.files.find(f => f.name === selected);
  useEffect(() => {
    const controller = new AbortController();
    setReadError('');
    if (!selected) return () => controller.abort();
    setPreview(old => old?.name === selected ? old : null); setReading(true);
    readFiles(runId, selected, controller.signal).then(data => {
      if (!controller.signal.aborted) { setPreview(data); setReadError(''); }
    }).catch(e => {
      if (!controller.signal.aborted) setReadError('这个文件暂时无法读取，可能仍在写入或已被移动。保留的正文是上次读取结果。');
    }).finally(() => { if (!controller.signal.aborted) setReading(false); });
    return () => controller.abort();
  }, [runId, selected, file?.modified_at, file?.size, refresh]);
  return <section className="agent-artifacts" aria-label="实际成果文件">
    <div className="agent-artifacts-heading"><div><h3>成果文件</h3><p>直接阅读伙伴保存的文件。是否通过，以检查结论为准。</p></div><button type="button" aria-label="刷新文件" title={loading ? '正在读取' : '刷新文件'} disabled={loading} onClick={() => setRefresh(n => n + 1)}><ArrowClockwise size={18}/></button></div>
    {listError && <p role="status" className="agent-notice">{listError}</p>}
    {!listing && !listError && <p role="status">正在查看这个任务的成果目录…</p>}
    {listing?.files.length === 0 && <p>{active ? '伙伴还没有保存成果文件，写入后会自动显示在这里。' : '此任务没有可预览的成果文件，可先查看上方的任务输出。'}</p>}
    {(!!listing?.files.length || preview) && <>
      <label>选择文件<select aria-label="选择成果文件" value={selected} onChange={e => setSelected(e.target.value)}>{selected && !file && <option value={selected}>{selected} · 上次读取</option>}{[{label:'任务成果', runtime:false}, {label:'运行与接力资料', runtime:true}].map(group => <optgroup label={group.label} key={group.label}>{listing?.files.filter(f => runtimeFile(f.name) === group.runtime).map(f => <option value={f.name} key={f.name}>{f.name} · {f.size < 1024 ? f.size + ' 字节' : (f.size / 1024).toFixed(1) + ' KB'}</option>)}</optgroup>)}</select></label>
      {reading && <p role="status">正在读取文件正文…</p>}
      {readError && <p role="status" className="agent-notice">{readError}</p>}
      {preview?.name === selected && <>
        {(preview.truncated || preview.changed_during_read) && <p className="agent-notice">{preview.truncated ? '文件较大，当前仅预览前 128 KB。' : '读取期间文件仍有改动，可刷新查看最新内容。'}此预览未提供完整文件哈希。</p>}
        {preview.redacted && <p className="muted">正文中的凭据内容已隐藏，原文件保持不变。</p>}
        <div className="agent-file-preview" tabIndex={0} aria-label={selected + ' 文件预览'}>
          {preview.kind === 'binary' ? <p>这是图片、视频或其它非 UTF-8 文本文件，当前提供文件信息，可在本机打开查看。</p> : preview.text === '' ? <p>文件已经保存，内容为空。</p> : preview.kind === 'markdown' ? <MessageContent text={preview.text} annotations={false}/> : <pre>{preview.text}</pre>}
        </div>
        <small className="muted">读取时间：{new Date(preview.observed_at).toLocaleString('zh-CN')}</small>
        {onEvidence && !runtimeFile(selected) && <button type="button" disabled={reading || !!readError || preview.changed_during_read} onClick={()=>onEvidence(selected)}>用于本次验收</button>}
        <details><summary>文件位置与校验信息</summary><code className="agent-artifact-hash">{preview.local_path}</code>{preview.sha256 && <p className="agent-artifact-hash">SHA-256：{preview.sha256}</p>}<p>用于确认本次读取的文件版本，不能代替质量验收。</p></details>
      </>}
    </>}
    {listing?.truncated && <p className="muted">当前展示可读取文件中的一部分；为保持页面流畅，未遍历完整的大型目录。</p>}
  </section>;
}
