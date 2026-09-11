import {useEffect,useState} from 'react';

const states={planning:'管理员正在安排',needs_configuration:'需要补充可用伙伴',running:'伙伴正在协作',ready_for_review:'整批成果已返回，等待原任务验收',completed:'整批已经验收',needs_help:'已返回原任务处理',cancelled:'已停止，成果保留',queued:'等待依赖或空闲伙伴',waiting_review:'等待独立验收',changes_requested:'正在处理验收意见',paused:'已暂停',budget_waiting:'等待投喂'};
export function StudioPlans({projectId,agents,openRun}) {
  const [plans,setPlans]=useState([]),[error,setError]=useState(''),[busy,setBusy]=useState('');
  const [offset,setOffset]=useState(0),[nextOffset,setNextOffset]=useState(null),[loading,setLoading]=useState(true);
  useEffect(()=>{setOffset(0);setPlans([]);setLoading(true);},[projectId]);
  async function refresh(signal) {
    const response=await fetch('/v1/ap-vibe/studio/plans?limit=30&offset='+offset+(projectId?'&project_id='+encodeURIComponent(projectId):''),{signal});
    const value=await response.json();
    if(!response.ok||value.ok===false)throw new Error(value.error?.message||'工作计划暂时无法读取，稍后会自动刷新。');
    if(signal?.aborted)return;
    setPlans(value.plans||[]);setNextOffset(value.next_offset??null);setError('');setLoading(false);
  }
  useEffect(()=>{
    const controller=new AbortController();let timer;
    const poll=async()=>{try{await refresh(controller.signal);}catch(e){if(e.name!=='AbortError'){setError(e.message);setLoading(false);}}finally{if(!controller.signal.aborted)timer=setTimeout(poll,5000);}};
    poll();return()=>{controller.abort();clearTimeout(timer);};
  },[projectId,offset]);
  async function cancel(plan) {
    setBusy(plan.plan_id);setError('');
    try {
      const response=await fetch('/v1/ap-vibe/studio/plans/cancel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({request_id:crypto.randomUUID(),plan_id:plan.plan_id,expected_revision:plan.revision})});
      const value=await response.json();if(!response.ok||value.ok===false)throw new Error(value.error?.message||'没有停止成功，请刷新后核对当前进展。');
      await refresh();
    }catch(e){setError(e.message);}finally{setBusy('');}
  }
  const name=id=>agents.find(a=>a.agent_id===id)?.name||id||'等待分配';
  return <section className="agent-panel" aria-label="协作工作计划">
    <h2>协作工作计划</h2><p>把复杂需求交给 Codex 或 Claude；开启协作后，管理员安排伙伴，依赖逐项放行，最后一起返回原任务。</p>
    {error&&<p role="status" className="agent-error">{error}</p>}
    {loading&&<p role="status">正在读取工作计划…</p>}
    {!loading&&!plans.length&&!error&&<p className="agent-notice">还没有工作计划。可以对 Codex 说：“用工作室伙伴拆分这项工作，交给管理员安排，最后回来汇总。”几分钟能完成的小事直接处理即可。</p>}
    {plans.map(plan=><article className="agent-notice" key={plan.plan_id}>
      <div className="agent-output-head"><strong>{plan.title}</strong><span>{states[plan.state]||plan.state}</span></div>
      {plan.manager_message&&<p>{plan.manager_message}</p>}
      <small>{plan.manager_acknowledged?'管理员已实际回复并采用分工':plan.assignment_source==='fallback'?'本地候选分配；未收到有效管理决定':'工作目标已保存，等待安排'}</small>
      {(plan.issue||plan.manager_issue)&&<details><summary>查看需要处理的情况</summary><p>{plan.issue||plan.manager_issue}</p></details>}
      <ol>{plan.nodes.map(node=><li key={node.key} style={{margin:'10px 0'}}>
        <b>{node.title}</b> · {states[node.state]||node.state} · {name(node.owner)}
        {node.dependencies?.length>0&&<small> · 依赖：{node.dependencies.map(key=>plan.nodes.find(n=>n.key===key)?.title||key).join('、')}</small>}
        {node.reviewer_agent_id&&<small> · 验收：{name(node.reviewer_agent_id)}</small>}
        {node.run_id&&<button onClick={()=>openRun(node.run_id)}>查看执行与成果</button>}
      </li>)}</ol>
      <div className="agent-actions">
        {plan.manager_run_id&&<button onClick={()=>openRun(plan.manager_run_id)}>查看管理员安排</button>}
        {['planning','needs_configuration','running'].includes(plan.state)&&<button disabled={!!busy} onClick={()=>cancel(plan)}>{busy===plan.plan_id?'正在停止…':'停止这个计划，保留成果'}</button>}
      </div>
      {plan.returns?.map(item=><p key={item.return_id}>整批回传：{item.state==='wake_queued'?
        ({completed:'原 Codex 任务已完成回复',submitted:'Codex 已接受续接，等待原任务处理',running:'正在连接原 Codex 任务',queued:'已排队唤醒原 Codex 任务',uncertain:'唤醒结果待核对，成果已保留',failed:'唤醒未完成，成果在原任务收件箱',cancelled:'唤醒已撤回，成果已保留',desktop_required:'成果已保存，请在原 Codex 任务查看'}[item.wake_status||item.delivery?.status]||'正在核对原任务状态'):
        item.state==='inbox_saved'?'已保存到原任务收件箱'+(item.wake_retry_at&&!item.wake_retry_blocked?'，稍后自动恢复唤醒':''):'正在送回原任务'}{item.wake_issue&&' · '+item.wake_issue}</p>)}
    </article>)}
    {(offset>0||nextOffset!==null)&&<div className="agent-actions" aria-label="工作计划翻页">
      <button disabled={offset===0||loading} onClick={()=>{setLoading(true);setOffset(Math.max(0,offset-30));}}>上一页计划</button>
      <span>第 {Math.floor(offset/30)+1} 页</span>
      <button disabled={nextOffset===null||loading} onClick={()=>{setLoading(true);setOffset(nextOffset);}}>下一页计划</button>
    </div>}
  </section>;
}
