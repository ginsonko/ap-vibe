import { useRef, useState } from 'react';
import {useDialogFocus} from './useDialogFocus';

const number = value => value === '' ? null : Number(value);
const display = value => Number(value || 0).toLocaleString('zh-CN', {maximumFractionDigits: 4});

export function Satiety({budget}) {
  if (!budget) return null;
  return <div className="agent-satiety">
    <span>{budget.exhausted ? '🍽 饿昏了 · 等待投喂' : budget.saturation == null ? '🌿 自由用量' : '🍚 饱食度'}</span>
    {budget.saturation != null && <progress max="1" value={budget.saturation} aria-label="剩余用量比例"/>}
    <small>{display(budget.tokens_used)} token · {display(budget.amount_used)} {budget.currency}
      {budget.price_status === 'unpriced' ? '（未配置价格，金额未估算）' : budget.price_basis === 'blended' ? '（历史混合参考价估算）' : '（参考价估算）'}</small>
    {budget.price_status !== 'unpriced' && budget.unestimated_requests>0 && <small>另有 {budget.unestimated_requests} 次历史请求未估算，未计入上述金额。</small>}
  </div>;
}

export function AgentBudget({agent, onClose, onSaved}) {
  const [draft, setDraft] = useState(agent.budget || {revision:0, prices:{}, currency:'CNY'});
  const [feedTokens, setFeedTokens] = useState('');
  const [feedAmount, setFeedAmount] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const pending = useRef(null);
  const dialog = useDialogFocus(onClose, busy);
  const patch = (name, value) => setDraft(old => ({...old, [name]:value}));
  async function submit(feed=false) {
    const body = feed ? {tokens:Number(feedTokens || 0), amount:Number(feedAmount || 0)} : {
      token_limit:draft.token_limit ?? null, amount_limit:draft.amount_limit ?? null,
      currency:draft.currency, prices:draft.prices, price_basis:draft.price_basis || 'per_token', price_source:draft.price_source || ''};
    const payload = {...body, agent_id:agent.agent_id, expected_revision:draft.revision};
    const signature = JSON.stringify({feed,payload});
    if (pending.current?.signature !== signature) pending.current = {signature, request_id:crypto.randomUUID()};
    setBusy(true);setError('');setNotice('');
    try {
      const res = await fetch('/v1/ap-vibe/agents/budget/'+(feed?'feed':'save'), {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({...payload,request_id:pending.current.request_id})});
      const value = await res.json();
      if (!res.ok || value.ok === false) throw new Error(value.error?.message || '暂未保存，请重试。');
      setDraft(value.budget);pending.current=null;setFeedTokens('');setFeedAmount('');
      setNotice(feed?'已补充用量；因额度暂停的自动任务会继续，手动停止的任务不会被唤醒。':value.budget.exhausted?'已保存，当前额度已用尽。补充用量或清空上限后可以继续。':value.budget.token_limit == null && value.budget.amount_limit == null?'已保存，当前不限制用量。':'已保存，后续请求按当前上限检查用量。');
      onSaved();
    } catch (e) {setError(e.message);} finally {setBusy(false);}
  }
  return <div className="agent-dialog-backdrop"><section ref={dialog} className="agent-dialog budget-dialog" role="dialog" aria-modal="true" aria-label="用量与投喂">
    <div className="agent-output-head"><h2>{agent.name} · 用量与投喂</h2><button disabled={busy} aria-label="关闭" onClick={onClose}>×</button></div>
    <Satiety budget={draft}/>
    <details open={draft.exhausted}><summary>投喂：增加已有上限</summary>
      <label>补充 token<input type="number" min="0" disabled={draft.token_limit == null} value={feedTokens} onChange={e=>setFeedTokens(e.target.value)}/></label>
      <label>补充金额<input type="number" min="0" step="any" disabled={draft.amount_limit == null} value={feedAmount} onChange={e=>setFeedAmount(e.target.value)}/></label>
      <button disabled={busy || !(Number(feedTokens)>0 || Number(feedAmount)>0)} onClick={()=>submit(true)}>投喂并恢复等待任务</button>
      {draft.token_limit == null && draft.amount_limit == null && <small>目前不限额，无需投喂。需要控制用量时再设置下面的上限。</small>}
    </details>
    {error && <p role="alert" className="agent-error">{error}</p>}{notice && <p role="status">{notice}</p>}
    <p>不填上限即可自由运行。这里是从开始记录以来的本地用量，不是服务商账户余额；同一个 Key 在其它软件的消耗不会计入。</p>
    <label>累计 token 上限（选填）<input type="number" min="0" value={draft.token_limit ?? ''} onChange={e=>patch('token_limit',number(e.target.value))} placeholder="留空不限制"/></label>
    <label>累计金额上限（选填）<input type="number" min="0" step="any" value={draft.amount_limit ?? ''} onChange={e=>patch('amount_limit',number(e.target.value))} placeholder="需填写下面的价格才可估算"/></label>
    <details><summary>参考价格与费用估算（可交给 Codex 填写）</summary>
      <p>告诉 Codex：“查看我的服务商公开价格页，为这位伙伴配置每百万 token 的参考价格。”不同分组可能价格不同，请提供正确页面。没有配置时金额记为 0 并标明未估算，功能照常使用。</p>
      <label>币种<input value={draft.currency} maxLength={12} onChange={e=>patch('currency',e.target.value)}/></label>
      <label>价格依据<select aria-label="价格依据" value={draft.price_basis || 'per_token'} onChange={e=>patch('price_basis',e.target.value)}><option value="per_token">分别配置输入、输出和缓存价格</option><option value="blended">历史费用反推的混合参考价</option></select></label>
      {draft.price_basis==='blended' && <p>这是历史费用除以已记录 token 的混合估值，各类价格通常填相同值。它不代表服务商精确单价；缓存比例、用量缺失或渠道改变时会偏离，不能用来确认账户余额。</p>}
      {[['input','输入'],['output','输出'],['cache_read','缓存读取'],['cache_write','缓存写入']].map(([key,title])=><label key={key}>{title} / 百万 token<input type="number" min="0" step="any" value={draft.prices?.[key] ?? ''} onChange={e=>patch('prices',{...draft.prices,[key]:number(e.target.value)})}/></label>)}
      <label>价格来源与备注<textarea rows={3} value={draft.price_source || ''} onChange={e=>patch('price_source',e.target.value)} placeholder="公开页面地址、分组与查询日期"/></label>
      <small>缓存价留空时按输入价估算。修改价格从后续请求起生效，保留历史记录。更换币种后分别累计，不自动换汇。</small>
    </details>
    <button className="primary-button" disabled={busy} onClick={()=>submit(false)}>{busy?'保存中…':'保存用量设置'}</button>
    <p className="muted">已发送请求会正常结算，可能略超上限。Claude 托管请求之间检查额度；Codex 本机登录目前在两次任务之间检查。建议为每位伙伴单独配置 Key，并在服务商侧设置额度。</p>
    {draft.unknown_usage_requests>0 && <p>有 {draft.unknown_usage_requests} 次请求的用量未完整回报，当前累计值可能偏低。</p>}
  </section></div>;
}
