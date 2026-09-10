import { useEffect, useState } from 'react';
import { ArrowClockwise, ArrowLeft, ArrowRight, ArrowSquareOut, PencilSimple } from '@phosphor-icons/react';
import { AgentPortrait } from './AgentAppearance';
import './agent-metrics.css';

const outcomeLabels = {independent_accepted:'独立验收通过',independent_changes_requested:'独立验收需修改',recorded_accepted:'已记录通过',recorded_changes_requested:'已记录需修改',self_reported:'作者自评',unreviewed:'尚无质量结论',review_applied:'验收报告已应用',review_unverified:'验收任务待核对'};
const states = {waiting:'等待依赖',starting:'启动中',running:'执行中',completed:'已验收',awaiting_review:'等待验收',changes_requested:'需修改',failed:'执行失败',uncertain:'结果不确定',interrupted:'中断',cancelled:'已停止',cancelling:'停止中'};
const money = value => value == null ? '未知' : `$${value.toFixed(3)}`;
const duration = value => value == null ? '未知' : value < 60000 ? `${(value/1000).toFixed(1)} 秒` : `${(value/60000).toFixed(1)} 分钟`;

export function AgentMetrics({openRun, editAgent, arrange, profiles}) {
  const [days,setDays] = useState('7');
  const [tag,setTag] = useState('');
  const [agentId,setAgentId] = useState('');
  const [configuration,setConfiguration] = useState('all');
  const [archived,setArchived] = useState(false);
  const [offset,setOffset] = useState(0);
  const [refresh,setRefresh] = useState(0);
  const [data,setData] = useState(null);
  const [error,setError] = useState('');
  const [loading,setLoading] = useState(true);
  const key = JSON.stringify([days,tag,agentId,configuration,offset]);
  useEffect(()=>{
    const controller=new AbortController();let timer;
    setLoading(true);setError('');
    async function load(){
      try {
        const query=new URLSearchParams({days,tag,agent_id:agentId,configuration,offset:String(offset),limit:'12'});
        const response=await fetch('/v1/ap-vibe/agents/metrics?'+query,{signal:controller.signal});
        const value=await response.json();
        if(!response.ok||!value.ok)throw new Error(value.error?.message||'暂时无法读取实测记录');
        if(!controller.signal.aborted){setData({...value,requestKey:key});setError('');}
      }catch(e){if(!controller.signal.aborted)setError(e.message);}
      finally {if(!controller.signal.aborted){setLoading(false);timer=setTimeout(load,15000);}}
    }
    load();return()=>{controller.abort();clearTimeout(timer);};
  },[key,refresh]);
  function filter(setter,value){setter(value);setOffset(0);}
  const current=data?.requestKey===key?data:null;
  const summary=current?.summary;
  const visible=current?.agents.filter(a=>archived||!a.archived)||[];
  const selectedProfile=profiles.find(a=>a.agent_id===agentId);
  return <section className="agent-metrics" aria-label="伙伴实测表现">
    <div className="metrics-heading"><div><h2>用真实成果认识伙伴</h2><p>每次验收留下经验，让下一次分工有据可依。</p></div><button type="button" title="重新读取实测记录，不调用模型" aria-label="刷新实测表现" disabled={loading} onClick={()=>setRefresh(v=>v+1)}><ArrowClockwise size={19}/></button></div>
    <div className="metrics-filters">
      <label>时间范围<select aria-label="时间范围" value={days} onChange={e=>filter(setDays,e.target.value)}><option value="7">最近 7 天</option><option value="30">最近 30 天</option><option value="0">全部记录</option></select></label>
      <label>任务类型<select aria-label="任务类型" value={tag} onChange={e=>filter(setTag,e.target.value)}><option value="">全部类型</option>{(data?.tags||[]).map(t=><option key={t} value={t}>{t}</option>)}</select></label>
      <label>伙伴<select aria-label="伙伴" value={agentId} onChange={e=>filter(setAgentId,e.target.value)}><option value="">全部伙伴</option>{profiles.map(a=><option key={a.agent_id} value={a.agent_id}>{a.name}{a.archived?'（已归档）':''}</option>)}</select></label>
      <label>配置范围<select aria-label="配置范围" value={configuration} onChange={e=>filter(setConfiguration,e.target.value)}><option value="all">包含历史版本</option><option value="current">仅当前配置版本</option></select></label>
    </div>
    {error&&<p role="status" className="metrics-warning">{error}。{current?'保留上次成功数据，正在自动重连。':'当前筛选的数据尚未获取，正在自动重连。'}</p>}
    {!current?<p role="status" aria-busy={loading}>正在读取这个范围的实测记录…</p>:<>
      <div className="metrics-summary">
        <div><strong>{summary.attempts}</strong><span>运行记录</span><small>{summary.logical_tasks} 项逻辑任务 · 含规划与验收开销</small></div>
        <div><strong>{summary.independent_accepted}<em> / {summary.independent_accepted+summary.independent_changes_requested}</em></strong><span>独立验收通过</span><small>{summary.recorded_accepted} 次其他通过记录单独保留</small></div>
        <div><strong>{summary.independent_changes_requested}</strong><span>独立验收需修改</span><small>{summary.states.failed||0} 次执行失败 · {summary.states.uncertain||0} 次结果不确定</small></div>
        <div><strong>{money(summary.estimated_cost_usd)}</strong><span>已知部分的执行器估算</span><small>{summary.cost_known_samples} 次有金额 · {summary.cost_unknown_samples} 次费用未知</small></div>
      </div>
      <div className="metrics-caption"><small>更新于 {new Date(current.observed_at).toLocaleTimeString('zh-CN')} · 独立通过率仅统计有检查项与文件哈希的作者成果</small><label><input type="checkbox" checked={archived} onChange={e=>setArchived(e.target.checked)}/>显示已归档伙伴</label></div>
      {(current.coverage.truncated||current.coverage.invalid_records>0)&&<p className="metrics-warning">当前展示最近 {current.coverage.scanned_runs} / {current.coverage.matched_runs} 次记录，{current.coverage.invalid_records} 条无法解析。可缩小时间范围查看。</p>}
      <div className="metrics-comparison" role="region" aria-label="伙伴表现对照" tabIndex={0}>
        <table><thead><tr><th>伙伴 / 配置</th><th>独立验收</th><th>运行 / 任务</th><th>运行耗时中位数</th><th>费用估算</th><th>操作</th></tr></thead><tbody>{visible.map(a=><tr key={a.agent_id}>
          <td><div className="metrics-person"><AgentPortrait profile={a} size={48}/><div><button className="metrics-name" onClick={()=>filter(setAgentId,a.agent_id)}>{a.name}</button><small>{a.configurations.length?Array.from(new Set(a.configurations.map(c=>c.model||'本机 Codex 模型'))).join(' / '):(a.model||'本机 Codex 模型')}</small><small>{a.configurations.length} 个有记录的配置版本</small>{a.configurations.length>1&&<details><summary>各版本样本</summary>{a.configurations.map(c=><small key={c.profile_revision}>第 {c.profile_revision??'未知'} 版 · {c.model||'本机模型'}：{c.metrics.attempts} 次运行，{c.metrics.independent_accepted} 次独立通过</small>)}</details>}</div></div></td>
          <td>{a.metrics.independent_pass_rate==null?<span className="metrics-unknown">暂无独立结论</span>:<><b>{Math.round(a.metrics.independent_pass_rate*100)}%</b><div className="metrics-bar" title={`${a.metrics.independent_accepted} 次通过 / ${a.metrics.independent_changes_requested} 次需修改`}><span style={{width:`${a.metrics.independent_pass_rate*100}%`}}/></div><small>{a.metrics.independent_accepted} 通过 · {a.metrics.independent_changes_requested} 需修改</small></>}</td>
          <td>{a.metrics.attempts} / {a.metrics.logical_tasks}<small>{a.metrics.review_attempts} 次为验收任务</small></td>
          <td>{duration(a.metrics.median_duration_ms)}<small>{a.metrics.duration_known_samples} 次有耗时</small></td>
          <td>{money(a.metrics.estimated_cost_usd)}<small>{a.metrics.cost_unknown_samples} 次未知</small></td>
          <td><button title="编辑伙伴职责与配置" aria-label={`编辑${a.name}的配置`} disabled={a.archived} onClick={()=>editAgent(profiles.find(p=>p.agent_id===a.agent_id))}><PencilSimple size={17}/></button></td>
        </tr>)}</tbody></table>
      </div>
      {selectedProfile&&<div className="metrics-role"><b>{selectedProfile.name}的职责偏好</b><p>{selectedProfile.role||'尚未填写；可在配置中说明适合交给它的工作。'}</p><button disabled={selectedProfile.archived} onClick={()=>arrange(selectedProfile.agent_id)}>安排一次实际工作</button></div>}
      <details className="metrics-help"><summary>这些数据怎么用于分工？</summary><p>先选择相同任务类型，再看具体成果与需修改的原因。例如准备写前端页面，可先查“前端”记录，把布局任务交给已有相似成果的伙伴。任务标签由规划者填写，没有标签的历史记录留在全部类型中。</p><p>职责是你的偏好，数据是已有经历。100% 可能只来自一次简单任务，并不代表所有事情都可靠；不同任务难度、服务和配置不能直接排智力榜。验收员的报告被应用也不代表它的评审质量经过了二次独立验证。</p><p>金额仅为执行器估算，包含规划、验收和失败的已知开销，最终以服务账单为准。费用未知不等于免费。耗时不包含等待用户验收，历史缺少执行时间就显示未知。刷新本页不调用模型。</p></details>
      <div className="metrics-heading"><h3>具体经历与验收依据</h3><small>{current.total} 条符合条件</small></div>
      {!current.samples.length?<p className="metrics-empty">这个范围还没有实际运行。可以换一个范围，或给伙伴安排一件有明确交付标准的工作。</p>:<div className="metrics-samples">{current.samples.map(s=><article key={s.run_id}>
        <div className="metrics-sample-heading"><div><b>{s.title||'未命名任务'}</b><small>{profiles.find(a=>a.agent_id===s.agent_id)?.name||s.agent_id} · {s.model||'本机模型'} · 配置第 {s.profile_revision??'未知'} 版{s.current_configuration?'':'（历史配置）'}</small></div><span className={'metrics-outcome '+s.outcome}>{outcomeLabels[s.outcome]||s.outcome}</span></div>
        <div className="metrics-sample-facts"><span>{states[s.state]||s.state}</span><span>{new Date(s.created_at).toLocaleString('zh-CN')}</span><span>耗时 {duration(s.duration_ms)}</span><span>估算 {money(s.estimated_cost_usd)}</span></div>
        {s.review_note&&<p>{s.review_note}</p>}
        {!!s.checks.length&&<details><summary>{s.checks.length} 项检查与证据</summary>{s.checks.map((c,i)=><div className="metrics-check" key={i}><b>{c.status==='passed'?'通过':'需修改'} · {c.criterion}</b><p>{c.evidence}</p></div>)}</details>}
        <button className="metrics-open" onClick={()=>openRun(s.run_id)}><ArrowSquareOut size={17}/>查看过程与实际成果</button>
      </article>)}</div>}
      <div className="metrics-pagination"><button aria-label="上一页经历" disabled={!offset} onClick={()=>setOffset(Math.max(0,offset-12))}><ArrowLeft size={18}/></button><span>第 {Math.floor(offset/12)+1} 页</span><button aria-label="下一页经历" disabled={current.next_offset==null} onClick={()=>setOffset(current.next_offset)}><ArrowRight size={18}/></button></div>
    </>}
  </section>;
}
