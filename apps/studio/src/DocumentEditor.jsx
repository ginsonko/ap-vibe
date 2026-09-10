import { useId, useState } from 'react';
import { workspaceApi, requestId } from './workspace-api';

const LABELS = {name:'名称',summary:'概述',goals:'预期效果',acceptance:'验收标准',redlines:'设计红线',completed:'已完成',remaining:'待办',notes:'备注',unknown:'未知',next_action:'下一步',documents:'文档入口',items:'详细记录',reason:'理由',evidence_refs:'证据入口',assessment:'十维评估',score:'分数',key:'维度编号',risk:'风险',improvement:'改进方向',title:'标题',path:'位置',status:'状态',old_logic:'旧逻辑',new_logic:'新逻辑',flow:'流程',modules:'模块',purpose:'用途',audience:'面向谁'};

Object.assign(LABELS, { id:'记录编号', user_note:'用户备注' });

function Field({ value, label, onChange, onDelete, depth=0 }) {
  const fieldId = useId();
  if (Array.isArray(value)) return <fieldset className="editor-group"><legend>{label}</legend>{value.map((item,i)=><Field key={i} label={'第 '+(i+1)+' 项'} value={item} depth={depth+1} onChange={next=>onChange(value.map((v,j)=>j===i?next:v))} onDelete={()=>onChange(value.filter((_,j)=>j!==i))}/>)}<button type="button" className="text-action" onClick={()=>onChange([...value, typeof value[0]==='object' && value[0] ? Object.fromEntries(Object.keys(value[0]).map(k=>[k,k==='score'?null:''])) : ''])}>＋添加一项</button>{onDelete&&<button type="button" className="text-action" onClick={onDelete}>删除此字段</button>}</fieldset>;
  if (value && typeof value==='object') return <fieldset className="editor-group"><legend>{label}</legend>{Object.entries(value).map(([key,item])=><Field key={key} label={LABELS[key]||key} value={item} depth={depth+1} onChange={next=>onChange({...value,[key]:next})} onDelete={()=>onChange(Object.fromEntries(Object.entries(value).filter(([k])=>k!==key)))}/>)}{!('notes' in value)&&<button type="button" className="text-action" onClick={()=>onChange({...value,notes:''})}>＋添加备注</button>}{onDelete&&<button type="button" className="text-action" onClick={onDelete}>删除此项</button>}</fieldset>;
  return <div className="editor-field"><span><label htmlFor={fieldId}>{label}</label>{onDelete&&<button type="button" className="text-action" aria-label={'删除'+label} onClick={onDelete}>删除</button>}</span>{typeof value==='boolean'?<select id={fieldId} value={String(value)} onChange={e=>onChange(e.target.value==='true')}><option value="true">是</option><option value="false">否</option></select>:typeof value==='number'||value===null?<input id={fieldId} type="number" value={value ?? ''} onChange={e=>onChange(e.target.value===''?null:Number(e.target.value))} placeholder="留空表示未知"/>:<textarea id={fieldId} rows={typeof value==='string'&&value.length>120?5:2} value={value || ''} onChange={e=>onChange(e.target.value)}/>}</div>;
}

export function DocumentEditor({ projectId, section, revision, value, label, onClose, onSaved }) {
  const [draft,setDraft]=useState(Object.keys(value||{}).length?value:{summary:'',notes:''});
  const [error,setError]=useState(''),[busy,setBusy]=useState(false);
  const save=async e=>{e.preventDefault();setBusy(true);try{await workspaceApi('organization/documents/update',{request_id:requestId(),project_id:projectId,expected_revision:revision,sections:{[section]:draft}});onSaved();onClose();}catch(e){setError(e.message);}finally{setBusy(false);}};
  return <form className="document-editor" onSubmit={save}><h3>修改：{label}</h3><p>只更新这一章。保存为人工修改的新版本，原版保留；如果其他任务同时更新，会提示重新读取后合并。</p><Field value={draft} label="章节内容" onChange={setDraft}/>{error&&<p role="alert">{error}</p>}<div className="organization-toolbar"><button className="primary-button" disabled={busy}>{busy?'保存中…':'保存新版本'}</button><button type="button" className="secondary-button" onClick={onClose}>取消</button></div></form>;
}
