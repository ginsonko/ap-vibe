import { ArrowSquareOut, ArrowsClockwise } from '@phosphor-icons/react';
import './task-attempt-history.css';

export function TaskAttemptHistory({ task, agents = [], openRun }) {
  if ((task.attempts?.length || 0) < 2 && !task.takeover_issue) return null;
  return <section className="task-attempt-history" aria-label="任务接力记录">
    <h4><ArrowsClockwise size={18} />任务接力记录</h4>
    {task.handoff_note && <p>{task.handoff_note}</p>}
    {task.author_retry_count > 0 && <p>候选耗尽后自动恢复 {task.author_retry_count} / {task.max_author_retries ?? 0} 次</p>}
    {task.takeover_issue && <p role="status" className="agent-notice">{task.takeover_issue}</p>}
    <ol>{task.attempts?.map(attempt => <li key={attempt.run_id}>
      <span>第 {attempt.epoch} 次分工</span>
      <strong>{agents.find(agent => agent.agent_id === attempt.agent_id)?.name || attempt.agent_id}</strong>
      {openRun && <button type="button" onClick={() => openRun(attempt.run_id)}
        aria-label={`查看第 ${attempt.epoch} 次分工的过程与成果`}><ArrowSquareOut size={16} />过程与成果</button>}
    </li>)}</ol>
  </section>;
}
