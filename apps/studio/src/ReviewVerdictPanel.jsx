import { ArrowSquareOut, CheckCircle, Question, Wrench } from '@phosphor-icons/react';

const LABELS = { accepted: '验收通过', changes_requested: '需要修改', inconclusive: '暂时无法判断' };
const CHECKS = { passed: '通过', failed: '需修改', unknown: '未确定' };

export function ReviewVerdictPanel({ task, openRun }) {
  const verdict = task?.last_verdict || task?.verdict;
  if (!verdict) return null;
  const Icon = verdict.outcome === 'accepted' ? CheckCircle : verdict.outcome === 'changes_requested' ? Wrench : Question;
  return <section className="review-verdict" aria-label="逐项验收结论">
    <div className="review-verdict-heading">
      <div><h3><Icon size={22} />{LABELS[verdict.outcome] || verdict.outcome}</h3><small>{task.title} · 第 {verdict.source_epoch} 版成果{verdict.status === 'pending' ? ' · 结论已保存，正在核对执行结束与文件版本' : ''}</small></div>
      {openRun && <button type="button" onClick={() => openRun(verdict.reviewer_run_id)}><ArrowSquareOut size={18} />打开完整报告</button>}
    </div>
    <p>{verdict.note}</p>
    {verdict.recovered_after_executor_exit && <p className="muted">检查末尾连接中断；已从保存的报告恢复结论，文件版本一致，没有重新调用模型。</p>}
    {task.reviewer_agent_id && <p className="muted">自动返工 {task.rework_round || 0} / {task.max_rework_rounds ?? 2} 次{task.rework_pending && task.state !== 'completed' ? ' · 正在按意见修订并再次送审' : ''}</p>}
    <div className="review-check-list">{verdict.checks?.map((check, i) => <article key={i} className={'review-check ' + check.status}>
      <div><strong>{check.criterion}</strong><span>{CHECKS[check.status] || check.status}</span></div>
      <p>{check.evidence}</p>{check.required_change && <p><b>修改要求：</b>{check.required_change}</p>}
    </article>)}</div>
    {task.verdict_history?.length > 1 && <details><summary>历次验收（{task.verdict_history.length} 次）</summary>
      {task.verdict_history.map(record => <p key={record.request_id}>第 {record.source_epoch} 版 · {LABELS[record.outcome]}：{record.note} <button type="button" onClick={() => openRun?.(record.reviewer_run_id)}>查看报告</button></p>)}
    </details>}
  </section>;
}
