import {useEffect,useRef,useState} from 'react';
import {useDialogFocus} from './useDialogFocus';

export function HarnessSources({onClose,onSaved}) {
  const [data,setData]=useState(null),[error,setError]=useState(''),[busy,setBusy]=useState(false),[notice,setNotice]=useState('');
  const pending=useRef(null),dialog=useDialogFocus(onClose,busy);
  useEffect(()=>{const controller=new AbortController();fetch('/v1/ap-vibe/sessions/sources',{signal:controller.signal}).then(r=>r.json()).then(v=>{if(v.ok===false)throw Error(v.error?.message||'读取来源设置失败');setData(v);}).catch(e=>{if(!controller.signal.aborted)setError(e.message);});return()=>controller.abort();},[]);
  function edit(kind,patch){setData(old=>({...old,sources:old.sources.map(s=>s.harness===kind?{...s,...patch,changed:true}:s)}));setNotice('');}
  async function save(e){e.preventDefault();const sources=Object.fromEntries(data.sources.filter(s=>s.changed).map(s=>[s.harness,{enabled:s.enabled,roots:s.custom_roots}]));if(!Object.keys(sources).length){onClose();return;}
    const body={expected_revision:data.revision,sources},signature=JSON.stringify(body);if(pending.current?.signature!==signature)pending.current={signature,request_id:crypto.randomUUID()};
    setBusy(true);setError('');try{const res=await fetch('/v1/ap-vibe/sessions/sources',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...body,request_id:pending.current.request_id}),signal:AbortSignal.timeout(20000)});const value=await res.json();if(!res.ok||value.ok===false)throw Error(value.error?.message||'保存失败，请重试');setData(value);pending.current=null;setNotice('已保存并重新发现会话；原应用的数据和连接配置保持不变。');onSaved();}catch(e){setError(e.message);}finally{setBusy(false);}}
  return <div className="agent-dialog-backdrop"><form ref={dialog} className="agent-dialog harness-sources-dialog" role="dialog" aria-modal="true" aria-label="会话来源设置" onSubmit={save}>
    <header><h2>会话来源</h2><button type="button" disabled={busy} onClick={onClose} aria-label="关闭">×</button></header>
    <div className="harness-sources-body">
    <p>常规安装会自动发现。便携版或自定义数据目录可在这里补充路径，每行一个；填写会话目录，也可以填写单个日志或数据库文件。</p>
    {error&&<p role="alert" className="agent-error">{error}</p>}{notice&&<p role="status">{notice}</p>}{!data&&!error&&<p role="status">正在读取来源配置…</p>}
    {data?.warnings?.map((warning,i)=><p key={i} role="status">{warning}</p>)}
    {data?.sources.map(s=><details key={s.harness}><summary>{s.name} · {s.enabled?`发现 ${s.discovered} 条会话`:'已关闭监看'}</summary>
      <div className="harness-source-fields">
      <label><input type="checkbox" checked={s.enabled} onChange={e=>edit(s.harness,{enabled:e.target.checked})}/>读取该应用公开会话</label>
      <p>{s.executor?'可在工作室选择此执行端；需要已安装对应客户端。':'当前接入用于读取公开会话，托管启动尚未开放。'}</p>
      <small>{s.custom_roots===null?'自动目录':'自定义目录'}：{s.roots.length?s.roots.join('；'):'暂未找到默认目录，请填写此应用的会话存储位置。'}</small>
      <label>{s.name} 会话路径<textarea rows="3" aria-label={s.name+' 会话路径'} value={s.custom_roots?.join('\n')??''} placeholder="留空使用自动目录" onChange={e=>edit(s.harness,{custom_roots:e.target.value.trim()?e.target.value.split('\n').map(p=>p.trim()).filter(Boolean):null})}/></label>
      {s.custom_roots!==null&&<button type="button" onClick={()=>edit(s.harness,{custom_roots:null})}>恢复自动目录</button>}
      </div>
    </details>)}
    </div>
    <footer><span>仅调整本工作台的读取来源</span><button className="primary-button" disabled={!data||busy} type="submit">{busy?'正在保存…':'保存并刷新'}</button></footer>
  </form></div>;
}
