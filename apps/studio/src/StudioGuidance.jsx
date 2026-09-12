import {useEffect,useRef,useState} from 'react';
import {BUILTIN_CATEGORIES} from './AgentRouting';
import './studio-guidance.css';

async function request(path,body){
  const res=await fetch('/v1/ap-vibe/agents/'+path,{method:body?'POST':'GET',headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});
  const result=await res.json();
  if(!res.ok||result.ok===false)throw new Error(result.error?.message||'暂时无法读取，请重试。');
  return result;
}
export function StudioGuidance({agents=[]}){
  const [open,setOpen]=useState(false),[state,setState]=useState(null),[draft,setDraft]=useState(null);
  const [busy,setBusy]=useState(false),[message,setMessage]=useState(''),[category,setCategory]=useState('routine_code'),[ranking,setRanking]=useState(null);
  const pending=useRef(null),generation=useRef(0);
  async function read(){setBusy(true);try{const value=await request('routing-guidance');setState(value);setDraft(value.settings);setMessage('');}catch(e){setMessage(e.message);}finally{setBusy(false);}}
  useEffect(()=>{if(open&&!state)read();},[open]);
  async function recommend(){const seq=++generation.current;try{const value=await request('recommendations?tags='+encodeURIComponent(JSON.stringify([category])));if(seq===generation.current)setRanking(value);}catch(e){if(seq===generation.current)setMessage(e.message);}}
  async function save(){
    const payload={guidance:draft.guidance,premium_routine_penalty:draft.premium_routine_penalty,experience_scale:draft.experience_scale,expected_revision:draft.revision};
    const signature=JSON.stringify(payload);if(pending.current?.signature!==signature)pending.current={signature,request_id:crypto.randomUUID()};
    setBusy(true);setMessage('');
    try{const value=await request('routing-guidance',{...payload,request_id:pending.current.request_id});pending.current=null;setDraft(value.settings);setState(old=>({...old,settings:value.settings}));setRanking(null);setMessage('已保存第 '+value.settings.revision+' 版。后续管理决策与本地排序将采用新偏好；已有任务和战绩保留。');}
    catch(e){setMessage(e.message+' 可以保留当前内容重试；如版本冲突，请重新读取后合并。');}finally{setBusy(false);}
  }
  const edit=(key,value)=>setDraft(old=>({...old,[key]:value}));
  return <details className="studio-guidance" open={open} onToggle={e=>setOpen(e.currentTarget.open)}>
    <summary>分工指导纲领与经验 · 按你的习惯合作</summary>
    <p>先看能力与错误代价，再权衡费用和效率。默认纲领可修改；真实同类验收经验持续积累，不会自动覆盖你写下的要求。</p>
    {!draft?<button type="button" disabled={busy} onClick={read}>{busy?'读取中…':'重新读取'}</button>:<>
      <label>给管理员的指导纲领<textarea rows={9} value={draft.guidance} onChange={e=>edit('guidance',e.target.value)} placeholder="例如：关键架构请交给强模型，明确模块由经济伙伴实现；普通测试独立检查即可。"/></label>
      <div className="guidance-controls">
        <label>高价伙伴处理普通工作的成本权重<input type="number" min="0" max="1" step="0.05" value={draft.premium_routine_penalty} onChange={e=>edit('premium_routine_penalty',Number(e.target.value))}/><small>0 不降低优先级；越高越倾向经济伙伴。关键任务与明确指定不受此项限制。</small></label>
        <label>真实经验的过渡倍率<input type="number" min="0" max="10" step="0.25" value={draft.experience_scale} onChange={e=>edit('experience_scale',Number(e.target.value))}/><small>1 为默认；增大后更快采用同类战绩，0 暂按简历。原始记录始终保留。</small></label>
      </div>
      <div className="guidance-actions"><button type="button" disabled={busy} onClick={save}>{busy?'保存中…':'保存分工纲领'}</button><button type="button" disabled={busy} onClick={()=>{setDraft(old=>({...old,...state.defaults}));setMessage('默认内容已填回，保存后生效。');}}>填回默认纲领</button><button type="button" disabled={busy} onClick={read}>重新读取已保存内容</button></div>
      <small>当前已保存：{state.settings.source==='default'?'初始默认':'用户第 '+state.settings.revision+' 版'}。这里只调整分工，不调用模型、不改变 Key 或用量上限。</small>
      <div className="guidance-preview"><label>看看系统会怎样建议<select value={category} onChange={e=>{generation.current++;setCategory(e.target.value);setRanking(null);}}>{BUILTIN_CATEGORIES.map(c=><option key={c.key} value={c.key}>{c.label}</option>)}</select></label><button type="button" onClick={recommend}>查看当前推荐依据</button>
        {ranking&&<><p>采用已保存的第 {ranking.guidance?.revision??0} 版；以下是建议，实际分配还会核对任务、连接和忙碌情况。</p><ol>{ranking.recommendations.slice(0,4).map(r=><li key={r.agent_id}><b>{agents.find(a=>a.agent_id===r.agent_id)?.name||r.agent_id}</b><span>简历 {Math.round(r.prior_score*100)}% · 同类样本 {r.effective_samples} · 战绩权重 {Math.round(r.experience_weight*100)}%</span><small>{r.reason}</small>{r.experience_evidence?.run_ids?.length>0&&<small>有 {r.experience_evidence.reviewed_attempts} 条已裁决尝试，详情见「实际战绩」。{r.experience_evidence.median_duration_ms!=null?'同类耗时中位数约 '+Math.round(r.experience_evidence.median_duration_ms/60000)+' 分钟；任务规模可能不同。':''}</small>}</li>)}</ol></>}
      </div>
    </>}{message&&<p role="status">{message}</p>}
  </details>;
}
