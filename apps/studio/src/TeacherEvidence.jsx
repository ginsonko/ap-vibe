const FAMILIES = { recall: '语义召回', prediction: '预测建议', appraisal: '感受评价', thought: '内部思考支架', lesson: '经验建议', action: '行动评分建议', paradigm: '范式建议', attention: '注意力建议', expression: '表达建议', parameter: '参数建议' };
const MODES = { provider_off: '基础模式', shadow_only: '影子观察', bounded_score_assist: '有界评分辅助' };
const REASONS = { no_governance_record: '尚未获得建议采用授权', shadow_only_current_wave: '当前仅观察，尚未采用', 'shadow_only_current_wave;requires_later_event_readback_or_user_feedback': '当前仅观察，等待后续结果或用户反馈', same_action_arena_bounded_score_component: '建议只影响有界评分，最终由 AP 选择' };
const list = (value) => Array.isArray(value) ? value : [];
const count = (value) => Array.isArray(value) ? value.length : '未记录';
const label = (names, value) => names[value] || value || '未记录';

export function TeacherEvidence({ teacher }) {
  const families = teacher?.adoption?.families || {};
  const candidates = teacher?.candidates || {};
  const keys = [...new Set([...Object.keys(candidates), ...Object.keys(families)])];
  const receipt = teacher?.call_receipt || {};
  const summary = teacher?.adoption?.summary || {};
  return <section className="cognition-teacher-evidence">
    <div className="organization-toolbar"><div><span className="eyebrow">本次认知过程 · 教师证据</span><h3>教师有没有真正介入？</h3></div><span className="evidence-muted">{label(MODES, teacher?.mode)}</span></div>
    <p className="muted">这里对应整次认知过程，不随帧切换。配置开关表示是否允许调用；下方回执和采用记录说明实际发生了什么。</p>
    <div className="teacher-evidence-foot">
      <span>调用回执：<b>{receipt.call_id ? label({success:'成功', succeeded:'成功', failed:'失败', timeout:'超时', completed:'完成'},receipt.status) : '未记录调用回执'}</b></span>
      <span>模型：<b>{teacher?.model || '未记录'}</b></span>
      <span>提出建议：<b>{summary.teacher_proposed ?? '未记录'}</b></span>
      <span>实际采用：<b>{summary.adopted ?? '未记录'}</b></span>
      <span>未采用记录：<b>{summary.rejected ?? '未记录'}</b></span>
    </div>
    {receipt.call_id && <p className="muted">回执编号：<code className="inline-id">{receipt.call_id}</code> · 耗时：{receipt.latency_ms == null ? '未记录' : `${receipt.latency_ms} 毫秒`}</p>}
    <div className="teacher-family-grid">{keys.map(key => {
      const family = families[key] || {};
      const proposed = Array.isArray(family.teacher_proposed) ? family.teacher_proposed : candidates[key];
      return <article key={key} className="teacher-family-card">
        <strong>{FAMILIES[key] || key}</strong><small className="inline-id">{key}</small>
        <p>提出 {count(proposed)} · 采用 {count(family.adopted)} · 未采用 {count(family.rejected)}</p>
        <span className={list(family.adopted).length ? 'evidence-positive' : 'evidence-muted'}>{label(MODES,family.mode)}</span>
        <p>{label(REASONS,family.reason)}</p>
        {list(proposed).length > 0 && <details><summary>查看候选来源与处置</summary>{list(proposed).map((item,index) => {
          const id = item.candidate_id || item.candidate_ref;
          const adopted = id && list(family.adopted).some(value => (value.candidate_id || value.candidate_ref) === id);
          const rejected = id && list(family.rejected).find(value => (value.candidate_id || value.candidate_ref) === id);
          return <p className="teacher-candidate" key={id || index}><code className="inline-id">{id || '无候选编号'}</code><br/>{adopted ? '已有采用记录' : rejected ? `未采用：${label(REASONS,rejected.reason)}` : '尚无采用记录'}</p>;
        })}<small>只展示来源和处置，不展开内部思考内容；一次采用也不代表已长期学会。</small></details>}
      </article>;
    })}</div>
    {!keys.length && <p className="muted">这次过程没有教师参与记录，不能据此推断调用次数或学习效果。</p>}
  </section>;
}
