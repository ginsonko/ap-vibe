import React, { useState } from 'react';
import './agent-routing.css';

/**
 * 8 种常见工作类别定义（符合经济调度方案规范）
 */
export const BUILTIN_CATEGORIES = [
  { key: 'routine_code', label: '常规编码', desc: '日常功能开发、脚本编写与常规业务实现' },
  { key: 'frontend', label: '前端界面', desc: 'React/HTML/CSS 布局、交互细节与视觉适配' },
  { key: 'writing', label: '文案撰写', desc: '技术文档、使用指南、更新说明与文字润色' },
  { key: 'research', label: '资料调研', desc: '代码库检索、外部信息查证与事实比对' },
  { key: 'reasoning', label: '复杂推理', desc: '算法分析、疑难诊断、架构推演与多方案权衡' },
  { key: 'planning', label: '任务规划', desc: '任务切分、依赖分析与执行步骤规划' },
  { key: 'review', label: '成果核验', desc: '对照验收标准独立核查、代码评审与把关' },
  { key: 'critical', label: '关键攻坚', desc: '核心高危逻辑、紧急排障与高可靠性攻关' },
];

/**
 * 4 个费用档位定义
 */
export const COST_TIERS = [
  {
    key: 'economy',
    label: '经济伙伴',
    badge: '经济',
    desc: '适合常规、批量或高频次日常任务，优先控制用量与成本',
  },
  {
    key: 'standard',
    label: '标准伙伴',
    badge: '标准',
    desc: '适合综合性日常工作，综合性能与费用表现均衡',
  },
  {
    key: 'premium',
    label: '高价攻坚',
    badge: '高价攻坚',
    desc: '单价较高，日常调度有软成本权衡，关键/疑难任务或用户明确指定时优先调用',
  },
  {
    key: 'unknown',
    label: '未知档位',
    badge: '未知档位',
    desc: '尚未设定费用档位或渠道费率未知，调度时不作强制排除，也不视作免费',
  },
];

/**
 * 安全 clamp 数值到 [min, max]
 */
function clamp(val, min, max) {
  const n = Number(val);
  if (Number.isNaN(n)) return min;
  return Math.min(Math.max(n, min), max);
}

/**
 * 格式化百分比显示
 */
function formatPercent(score) {
  const n = clamp(score, 0, 1);
  return `${Math.round(n * 100)}%`;
}

/**
 * 伙伴卡片紧凑摘要组件：展示费用档位与主要擅长方向
 */
export function AgentRoutingSummary({ value }) {
  if (!value) {
    return (
      <div className="agent-routing-summary agent-routing-summary-empty" role="region" aria-label="伙伴简历摘要">
        <span className="agent-routing-badge tier-unknown">未设简历</span>
        <span className="agent-routing-summary-text">未配置分工简历，调度器按默认印象及通用策略分配</span>
      </div>
    );
  }

  const costTierKey = value.cost_tier || 'unknown';
  const tierMeta = COST_TIERS.find(t => t.key === costTierKey) || {
    key: costTierKey,
    label: costTierKey,
    badge: costTierKey,
    desc: '自定义费用档位',
  };

  const strengths = value.strengths && typeof value.strengths === 'object' ? value.strengths : {};
  const entries = Object.entries(strengths)
    .map(([key, score]) => ({
      key,
      score: clamp(score, 0, 1),
      label: BUILTIN_CATEGORIES.find(b => b.key === key)?.label || key,
    }))
    .filter(item => item.score > 0)
    .sort((a, b) => b.score - a.score);

  // 挑选最高匹配度的前 3 项
  const topStrengths = entries.slice(0, 3);
  const priorStrength = typeof value.prior_strength === 'number' && value.prior_strength > 0 ? value.prior_strength : 6;
  const sourceText = typeof value.source === 'string' && value.source.trim() ? value.source.trim() : '用户初始偏好';

  return (
    <div className="agent-routing-summary" role="region" aria-label="伙伴简历与调度摘要">
      <div className="agent-routing-summary-header">
        <span className={`agent-routing-badge tier-${tierMeta.key}`} title={tierMeta.desc}>
          {tierMeta.badge}
        </span>
        <span className="agent-routing-summary-tier-name">{tierMeta.label}</span>
        <span className="agent-routing-summary-prior" title={`先验样本数：累计 ${priorStrength} 个有效验收样本后，战绩与简历各占 50% 权重`}>
          战绩过渡: {priorStrength}有效样本
        </span>
      </div>

      <div className="agent-routing-summary-strengths" aria-label="主要擅长方向">
        {topStrengths.length > 0 ? (
          topStrengths.map(s => (
            <span
              key={s.key}
              className="agent-routing-tag"
              title={`${s.label}：初始匹配度 ${formatPercent(s.score)}（非测得智力分）`}
            >
              {s.label} <b>{formatPercent(s.score)}</b>
            </span>
          ))
        ) : (
          <span className="agent-routing-tag-empty">全科常规分工（未指定特别偏好）</span>
        )}
      </div>

      {sourceText && (
        <div className="agent-routing-summary-source" title={`来源说明：${sourceText}`}>
          <small>来源: {sourceText}</small>
        </div>
      )}
    </div>
  );
}

/**
 * 伙伴简历编辑组件：外部负责保存，组件仅在用户显式交互时调用 onChange
 */
export function AgentRoutingEditor({ value, onChange }) {
  // 仅维护新增自定义类别相关的内部临时输入状态
  const [newKey, setNewKey] = useState('');
  const [newScore, setNewScore] = useState(50);
  const [customError, setCustomError] = useState('');

  // 提取展示值，若外部未传则展示安全默认值（注意：绝不在此处使用 useEffect 自动触发 onChange）
  const activeTier = value?.cost_tier || 'unknown';
  const activeStrengths = value?.strengths && typeof value.strengths === 'object' ? value.strengths : {};
  const activePrior = typeof value?.prior_strength === 'number' && value.prior_strength > 0 ? value.prior_strength : 6;
  const activeSource = typeof value?.source === 'string' ? value.source : '';

  // 找出所有已存在的未知/自定义类别（不在 8 种内置类别中的）
  const customCategoryKeys = Object.keys(activeStrengths).filter(
    k => !BUILTIN_CATEGORIES.some(b => b.key === k)
  );

  /**
   * 用户发生任何显式交互修改时，构造完整对象通知外部
   */
  function handleUpdate(patch) {
    if (typeof onChange !== 'function') return;

    const nextTier = patch.cost_tier !== undefined ? patch.cost_tier : activeTier;
    const nextStrengths = patch.strengths !== undefined ? patch.strengths : { ...activeStrengths };
    const nextPrior = patch.prior_strength !== undefined ? patch.prior_strength : activePrior;
    const nextSource = patch.source !== undefined ? patch.source : activeSource;

    // 清理与规范化数值，确保完全合法
    const sanitizedStrengths = {};
    for (const [k, v] of Object.entries(nextStrengths)) {
      sanitizedStrengths[k] = clamp(v, 0, 1);
    }

    const completeProfile = {
      cost_tier: nextTier,
      strengths: sanitizedStrengths,
      prior_strength: typeof nextPrior === 'number' && nextPrior > 0 ? nextPrior : 6,
      source: typeof nextSource === 'string' ? nextSource : '',
    };

    onChange(completeProfile);
  }

  // 修改单个类别的匹配度
  function handleCategoryScoreChange(key, rawValue) {
    const score = clamp(Number(rawValue) / 100, 0, 1);
    const updated = {
      ...activeStrengths,
      [key]: score,
    };
    handleUpdate({ strengths: updated });
  }

  // 移除一个自定义类别
  function handleRemoveCustomCategory(key) {
    const updated = { ...activeStrengths };
    delete updated[key];
    handleUpdate({ strengths: updated });
  }

  // 添加新的自定义类别
  function handleAddCustomCategory(e) {
    e?.preventDefault?.();
    const trimmed = newKey.trim();
    if (!trimmed) {
      setCustomError('请输入类别标识符');
      return;
    }
    // 标识符格式检查：推荐字母数字下划线短横线
    if (trimmed.length > 100 || /[\u0000-\u001f]/.test(trimmed)) {
      setCustomError('类别名最多100字，不能包含控制字符');
      return;
    }
    if (activeStrengths[trimmed] !== undefined) {
      setCustomError('该类别已存在，无需重复添加');
      return;
    }

    const score = clamp(Number(newScore) / 100, 0, 1);
    const updated = {
      ...activeStrengths,
      [trimmed]: score,
    };
    setCustomError('');
    setNewKey('');
    setNewScore(50);
    handleUpdate({ strengths: updated });
  }

  return (
    <div className="agent-routing-editor" role="group" aria-label="伙伴分工简历与调度配置">
      {/* 提示与透明度说明 */}
      <div className="agent-routing-notice" role="note">
        <strong>分工简历与调度偏好</strong>
        <p>
          分工简历是伙伴在冷启动或样本稀疏时的初始分工参考，<b>明确不是测得智力排名</b>。
          随着实际任务独立验收的积累，系统将平滑过渡到真实战绩。
        </p>
      </div>

      {/* 费用档位选择 */}
      <div className="agent-routing-section">
        <label className="agent-routing-section-title" id="ar-tier-group-label">
          费用档位（成本调度偏好）
        </label>
        <div
          className="agent-routing-tier-grid"
          role="radiogroup"
          aria-labelledby="ar-tier-group-label"
        >
          {COST_TIERS.map(tier => {
            const isChecked = activeTier === tier.key;
            return (
              <label
                key={tier.key}
                className={`agent-routing-tier-card ${isChecked ? 'selected' : ''}`}
                tabIndex={0}
                onKeyDown={e => {
                  if (e.key === ' ' || e.key === 'Enter') {
                    e.preventDefault();
                    handleUpdate({ cost_tier: tier.key });
                  }
                }}
              >
                <input
                  type="radio"
                  name="ar_cost_tier"
                  value={tier.key}
                  checked={isChecked}
                  onChange={() => handleUpdate({ cost_tier: tier.key })}
                />
                <div className="agent-routing-tier-card-body">
                  <div className="agent-routing-tier-card-header">
                    <span className={`agent-routing-badge tier-${tier.key}`}>{tier.badge}</span>
                    <b>{tier.label}</b>
                  </div>
                  <p>{tier.desc}</p>
                </div>
              </label>
            );
          })}
        </div>
      </div>

      {/* 8 种常见类别擅长工作匹配度 */}
      <div className="agent-routing-section">
        <div className="agent-routing-section-header">
          <label className="agent-routing-section-title">擅长工作初始匹配度（0% ~ 100%）</label>
          <span className="agent-routing-subtext">初始匹配偏好，不是成功率或智力分</span>
        </div>

        <div className="agent-routing-categories-list">
          {BUILTIN_CATEGORIES.map(cat => {
            const currentScore = activeStrengths[cat.key] !== undefined ? activeStrengths[cat.key] : 0.5;
            const percent = Math.round(clamp(currentScore, 0, 1) * 100);

            return (
              <div key={cat.key} className="agent-routing-category-row">
                <div className="agent-routing-category-info">
                  <label htmlFor={`ar-slider-${cat.key}`} className="agent-routing-category-label">
                    {cat.label}
                    <code className="agent-routing-category-code">{cat.key}</code>
                  </label>
                  <span className="agent-routing-category-desc">{cat.desc}</span>
                </div>

                <div className="agent-routing-slider-wrap">
                  <input
                    id={`ar-slider-${cat.key}`}
                    type="range"
                    min="0"
                    max="100"
                    step="5"
                    value={percent}
                    aria-label={`${cat.label} 匹配度`}
                    onChange={e => handleCategoryScoreChange(cat.key, e.target.value)}
                  />
                  <div className="agent-routing-number-input-wrap">
                    <input
                      type="number"
                      min="0"
                      max="100"
                      step="1"
                      value={percent}
                      aria-label={`${cat.label} 百分比`}
                      onChange={e => handleCategoryScoreChange(cat.key, e.target.value)}
                    />
                    <span>%</span>
                  </div>
                </div>
              </div>
            );
          })}
        </div>

        {/* 自定义/未知类别展示（绝不丢失未知类别） */}
        {customCategoryKeys.length > 0 && (
          <div className="agent-routing-custom-categories">
            <h4 className="agent-routing-custom-title">扩展与自定义类别（保留历史及未知项）</h4>
            <div className="agent-routing-categories-list">
              {customCategoryKeys.map(key => {
                const currentScore = activeStrengths[key] !== undefined ? activeStrengths[key] : 0.5;
                const percent = Math.round(clamp(currentScore, 0, 1) * 100);

                return (
                  <div key={key} className="agent-routing-category-row custom-row">
                    <div className="agent-routing-category-info">
                      <label htmlFor={`ar-slider-custom-${key}`} className="agent-routing-category-label">
                        <span className="agent-routing-custom-pill">自定义</span>
                        {key}
                      </label>
                      <span className="agent-routing-category-desc">自定义或历史未知类别，已自动保留</span>
                    </div>

                    <div className="agent-routing-slider-wrap">
                      <input
                        id={`ar-slider-custom-${key}`}
                        type="range"
                        min="0"
                        max="100"
                        step="5"
                        value={percent}
                        aria-label={`自定义类别 ${key} 匹配度`}
                        onChange={e => handleCategoryScoreChange(key, e.target.value)}
                      />
                      <div className="agent-routing-number-input-wrap">
                        <input
                          type="number"
                          min="0"
                          max="100"
                          step="1"
                          value={percent}
                          aria-label={`自定义类别 ${key} 百分比`}
                          onChange={e => handleCategoryScoreChange(key, e.target.value)}
                        />
                        <span>%</span>
                      </div>
                      <button
                        type="button"
                        className="agent-routing-remove-btn"
                        title={`删除类别 ${key}`}
                        aria-label={`删除类别 ${key}`}
                        onClick={() => handleRemoveCustomCategory(key)}
                      >
                        ×
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* 添加新自定义类别 */}
        <div className="agent-routing-add-box">
          <div className="agent-routing-add-inputs">
            <label className="agent-routing-inline-field">
              <span>新增类别</span>
              <input
                type="text"
                placeholder="例如：backend、devops"
                value={newKey}
                onChange={e => {
                  setNewKey(e.target.value);
                  if (customError) setCustomError('');
                }}
                onKeyDown={e => {
                  if (e.key === 'Enter') {
                    e.preventDefault();
                    handleAddCustomCategory(e);
                  }
                }}
              />
            </label>

            <label className="agent-routing-inline-field ar-score-field">
              <span>初始匹配</span>
              <div className="agent-routing-number-input-wrap">
                <input
                  type="number"
                  min="0"
                  max="100"
                  step="5"
                  value={newScore}
                  onChange={e => setNewScore(clamp(e.target.value, 0, 100))}
                />
                <span>%</span>
              </div>
            </label>

            <button
              type="button"
              className="agent-routing-btn-secondary"
              onClick={handleAddCustomCategory}
            >
              ＋ 添加自定义类别
            </button>
          </div>
          {customError && <p className="agent-routing-error-text" role="alert">{customError}</p>}
        </div>
      </div>

      {/* 经验过渡平滑参数 */}
      <div className="agent-routing-section">
        <label className="agent-routing-section-title" htmlFor="ar-prior-strength-input">
          经验过渡平滑度（先验样本数）
        </label>
        <div className="agent-routing-field-row">
          <input
            id="ar-prior-strength-input"
            type="number"
            min="1"
            step="1"
            value={activePrior}
            onChange={e => {
              const val = Number(e.target.value);
              handleUpdate({ prior_strength: val > 0 ? Math.round(val) : 6 });
            }}
          />
          <div className="agent-routing-field-help">
            <b>默认 6 次有效样本。</b> 当积累了该数量的同类任务可归因独立验收时，真实战绩与初始简历各占 50%
            权重（公式：<code>w = n / (n + prior_strength)</code>）。有效样本越多，实际战绩比重越高。
          </div>
        </div>
      </div>

      {/* 初始来源文字 */}
      <div className="agent-routing-section">
        <label className="agent-routing-section-title" htmlFor="ar-source-input">
          初始印象来源与说明
        </label>
        <input
          id="ar-source-input"
          type="text"
          maxLength={200}
          placeholder="例如：用户日常使用印象、模板预设偏好、服务商官方推荐等"
          value={activeSource}
          onChange={e => handleUpdate({ source: e.target.value })}
        />
        <small className="agent-routing-field-desc">
          说明此份初始简历由谁提出或基于什么假设，保持透明，不包装为公开跑分评测机构结论。
        </small>
      </div>
    </div>
  );
}

export default AgentRoutingEditor;
