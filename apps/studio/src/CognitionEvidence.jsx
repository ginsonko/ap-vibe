const recordedArray = (value, unit) => Array.isArray(value) ? `${value.length} ${unit}` : '未记录';

export function CognitionEvidence({ frame }) {
  const slow = frame?.slow_affect;
  const states = [
    ['当前感知 SA', frame?.sa?.occurrence_id && frame.sa.source ? '已获取来源' : '来源未记录', '感知是输入记录，仍需后续核对。'],
    ['认知感受', recordedArray(frame?.feelings, '项感受'), '显示当前帧保存的计算状态。'],
    ['情绪慢量', slow && typeof slow === 'object' ? `${Object.values(slow).filter(value => typeof value === 'number' && Number.isFinite(value)).length} 项数值` : '未记录', '观察随活动逐步变化的内部量。'],
    ['行动候选', recordedArray(frame?.actions, '个候选'), '有候选、已选中和已执行分别核对。'],
    ['范式', Array.isArray(frame?.paradigms) && !frame.paradigms.length ? '本帧无候选' : recordedArray(frame?.paradigms, '个候选'), '无候选不代表故障，也不代表已经学会。'],
    ['行动回读', frame?.decision?.result_status ? ({success:'执行成功', failed:'执行失败', partial:'部分完成'})[frame.decision.result_status] || frame.decision.result_status : '本帧无执行结果', '回读帧可能仅接收结果，不再次执行行动。'],
  ];
  return <div className="cognition-evidence-grid" aria-label="当前帧证据概览">{states.map(([title, status, detail]) => <article key={title}><span>{title}</span><strong>{status}</strong><small>{detail}</small></article>)}</div>;
}
