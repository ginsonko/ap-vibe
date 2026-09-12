import { useState, useRef, useEffect } from 'react';
import { DownloadSimple, UploadSimple, CheckCircle, Warning, ArrowsClockwise } from '@phosphor-icons/react';
import { AgentPortrait, PixelSprite } from './AgentAppearance';
import './agent-sharing.css';

const MAX_BUNDLE_BYTES = 32 * 1024 * 1024; // 32 MiB

async function request(path, body, signal) {
  const controller = new AbortController();
  const cancel = () => controller.abort();
  signal?.addEventListener('abort', cancel, { once: true });
  if (signal?.aborted) controller.abort();
  const timeout = setTimeout(cancel, 30000);
  try {
    const response = await fetch('/v1/ap-vibe/agents/share' + path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
      signal: controller.signal,
    });
    const value = await response.json().catch(() => null);
    if (!response.ok || value?.ok !== true) {
      const detail = value?.error || {};
      const errorMsg = [detail.message, detail.code, detail.solution].filter(Boolean).join(' · ')
        || value?.message
        || '分享服务暂时没有返回预期结果，请重试。';
      const error = new Error(errorMsg);
      error.code = detail.code;
      error.status = response.status;
      // 明确拒绝（4xx且非408客户端超时）
      error.rejected = !!error.code && response.status >= 400 && response.status < 500 && response.status !== 408;
      throw error;
    }
    return value;
  } finally {
    clearTimeout(timeout);
    signal?.removeEventListener('abort', cancel);
  }
}

export function AgentSharing({ agents = [], onSaved }) {
  const [open, setOpen] = useState(false);

  // 过滤活跃伙伴与非本地登录伙伴
  const activeAgents = agents.filter(a => !a.archived);

  // --- 导出状态 ---
  const [exportSelected, setExportSelected] = useState([]);
  const [exportBusy, setExportBusy] = useState(false);
  const [exportError, setExportError] = useState('');
  const [exportWarnings, setExportWarnings] = useState([]);
  const [exportNotice, setExportNotice] = useState('');

  // 伙伴列表更新时同步已选导出 ID
  useEffect(() => {
    setExportSelected(old => old.filter(id => activeAgents.some(a => a.agent_id === id)));
  }, [activeAgents.map(a => a.agent_id).join(',')]);

  // 全选/清空导出伙伴
  const selectAllExport = () => setExportSelected(activeAgents.map(a => a.agent_id));
  const clearExport = () => setExportSelected([]);

  const toggleExportItem = (id) => {
    setExportSelected(old => old.includes(id) ? old.filter(x => x !== id) : [...old, id]);
  };

  // 执行导出并触发下载
  async function handleExport() {
    if (exportBusy || !exportSelected.length) return;
    setExportBusy(true);
    setExportError('');
    setExportWarnings([]);
    setExportNotice('');

    try {
      const data = await request('/export', { agent_ids: exportSelected });
      if (data.warnings?.length) {
        setExportWarnings(data.warnings);
      }
      const bundle = data.bundle;
      if (!bundle) {
        throw new Error('导出数据为空，请重试。');
      }

      // 转为 JSON Blob 并触发浏览器下载
      const jsonStr = typeof bundle === 'string' ? bundle : JSON.stringify(bundle);
      const blob = new Blob([jsonStr], { type: 'application/json' });
      const objectUrl = URL.createObjectURL(blob);
      try {
        const anchor = document.createElement('a');
        anchor.href = objectUrl;
        anchor.download = 'ap-vibe-agents.json';
        document.body.appendChild(anchor);
        anchor.click();
        document.body.removeChild(anchor);
        setExportNotice(`已生成 ${exportSelected.length} 位伙伴的分享包，浏览器已开始下载 ap-vibe-agents.json。连接密钥、登录会话和实际消耗记录已排除。`);
      } finally {
        setTimeout(() => URL.revokeObjectURL(objectUrl), 1000);
      }
    } catch (err) {
      setExportError(err.message || '导出失败，请重试。');
    } finally {
      setExportBusy(false);
    }
  }

  // --- 导入状态 ---
  const [importFile, setImportFile] = useState(null);
  const [importBundle, setImportBundle] = useState(null);
  const [importPreviewAgents, setImportPreviewAgents] = useState([]);
  const [previewAppearances, setPreviewAppearances] = useState([]);
  const [importSelectedIds, setImportSelectedIds] = useState([]);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [previewError, setPreviewError] = useState('');
  const [previewWarnings, setPreviewWarnings] = useState([]);

  const [importBusy, setImportBusy] = useState(false);
  const [importError, setImportError] = useState('');
  const [importSuccessNotice, setImportSuccessNotice] = useState('');

  // 保持稳定 request_id；文件更换或选择变化才生成新 ID
  const importPendingRef = useRef(null);
  const fileInputRef = useRef(null);

  // 清除旧预览与导入现场
  function clearPreviewState() {
    setImportBundle(null);
    setImportPreviewAgents([]);
    setPreviewAppearances([]);
    setImportSelectedIds([]);
    setPreviewError('');
    setPreviewWarnings([]);
    setImportError('');
    setImportSuccessNotice('');
    importPendingRef.current = null;
  }

  // 选择本地文件进行校验并读取
  async function handleFileSelect(e) {
    const file = e.target.files?.[0];
    clearPreviewState();

    if (!file) {
      setImportFile(null);
      return;
    }

    if (file.size === 0) {
      setImportFile(null);
      setPreviewError('所选文件为空，请选择有效的伙伴分享包 JSON 文件。');
      if (fileInputRef.current) fileInputRef.current.value = '';
      return;
    }

    if (file.size > MAX_BUNDLE_BYTES) {
      setImportFile(null);
      setPreviewError(`文件体积超过 32 MiB 限制（当前大小 ${(file.size / 1024 / 1024).toFixed(2)} MiB）。请检查文件。`);
      if (fileInputRef.current) fileInputRef.current.value = '';
      return;
    }

    setImportFile(file);
    setPreviewBusy(true);

    try {
      const text = await file.text();
      let parsed;
      try {
        parsed = JSON.parse(text);
      } catch {
        throw new Error('所选文件不是合法的 JSON 格式。');
      }

      // 如果 JSON 外层已有 bundle 键则解包，否则整体作为 bundle
      const bundle = parsed.bundle || parsed;
      setImportBundle(bundle);

      // 请求后端预览解析
      const previewRes = await request('/preview', { bundle });
      if (previewRes.warnings?.length) {
        setPreviewWarnings(previewRes.warnings);
      }
      const agentsList = Array.isArray(previewRes.agents) ? previewRes.agents : [];
      setImportPreviewAgents(agentsList);
      setPreviewAppearances((previewRes.appearances || []).map(a => ({...a,
        url:'data:image/png;base64,' + bundle.appearances.find(b=>b.source_id===a.source_id)?.png_base64})));

      // 默认勾选所有非重复的伙伴
      const nonDuplicate = agentsList.filter(a => !a.already_imported).map(a => a.source_id);
      setImportSelectedIds(nonDuplicate);
    } catch (err) {
      setPreviewError(err.message || '读取或预览分享包时发生错误。');
      setImportBundle(null);
    } finally {
      setPreviewBusy(false);
    }
  }

  // 切换导入勾选
  const toggleImportSelection = (sourceId) => {
    setImportSelectedIds(old =>
      old.includes(sourceId) ? old.filter(id => id !== sourceId) : [...old, sourceId]
    );
  };

  const selectAllNew = () => {
    setImportSelectedIds(importPreviewAgents.filter(a => !a.already_imported).map(a => a.source_id));
  };

  const clearAllImport = () => {
    setImportSelectedIds([]);
  };

  // 执行最终导入
  async function handleImport() {
    if (importBusy || !importBundle || !importSelectedIds.length) return;

    setImportBusy(true);
    setImportError('');
    setImportSuccessNotice('');

    // 计算本次意图指纹
    const sortedSelected = [...importSelectedIds].sort();
    const intentSignature = JSON.stringify({
      selected_ids: sortedSelected,
      // 使用文件名加大小标识文件版本
      file_sig: importFile ? `${importFile.name}-${importFile.size}` : '',
    });

    // 如果意图发生变化，重新生成 request_id
    if (!importPendingRef.current || importPendingRef.current.signature !== intentSignature) {
      importPendingRef.current = {
        signature: intentSignature,
        request_id: crypto.randomUUID(),
      };
    }

    const currentRequestId = importPendingRef.current.request_id;
    const payload = {
      request_id: currentRequestId,
      bundle: importBundle,
      selected_ids: sortedSelected,
    };

    try {
      const res = await request('/import', payload);
      // 成功返回后清除 pending
      importPendingRef.current = null;

      const importedAgents = res.agents || [];
      const skippedAgents = res.skipped || [];

      setImportSuccessNotice(
        `导入完成：已新增 ${importedAgents.length} 位伙伴${skippedAgents.length ? `，跳过 ${skippedAgents.length} 位已存在伙伴` : ''}。`
      );

      // 导入后重置文件与预览区，避免重复提交
      setImportBundle(null);
      setImportPreviewAgents([]);
      setImportSelectedIds([]);
      setImportFile(null);
      if (fileInputRef.current) fileInputRef.current.value = '';

      // 刷新外部伙伴列表
      if (typeof onSaved === 'function') {
        try {
          await onSaved();
        } catch {
          // ignore
        }
      }
    } catch (err) {
      // 结构性拒绝清除 pending，不可重试；不确定网络异常保留 pending 的 request_id
      if (err.rejected) {
        importPendingRef.current = null;
      }
      setImportError(
        err.rejected
          ? err.message
          : `${err.message}。本次导入请求已保留（Request ID: ${currentRequestId.slice(0, 8)}…），再次点击将原样重试，避免重复导入。`
      );
    } finally {
      setImportBusy(false);
    }
  }

  // Collapsing must retain uncertain submissions and their idempotency key.
  const handleToggleDetails = (e) => {
    const isNowOpen = e.target.open;
    setOpen(isNowOpen);
  };

  return (
    <section className="page-section agent-sharing-section" aria-label="伙伴分享与导入导出">
      <details className="agent-sharing-details" open={open} onToggle={handleToggleDetails}>
        <summary className="agent-sharing-summary">
          <div className="agent-sharing-summary-title">
            <span className="agent-sharing-badge">分享与迁移</span>
            <strong>伙伴分享：导出与导入</strong>
          </div>
          <span className="agent-sharing-summary-hint">
            {open ? '收起面板' : '导出已有配置为离线包，或预览导入外部伙伴'}
          </span>
        </summary>

        <div className="agent-sharing-content">
          <p className="agent-sharing-lead">
            把伙伴的人设、职责、模型配置与像素动作一起分享。连接密钥、本地登录凭证、用量记录和实测战绩会排除；分享前请检查自己写的人设与说明是否适合公开。
          </p>

          <div className="agent-sharing-grid">
            {/* 导出区域 */}
            <article className="sharing-card sharing-export-card">
              <div className="sharing-card-header">
                <h3>导出伙伴配置</h3>
                <span className="sharing-subhead">选择要打包分享的伙伴</span>
              </div>

              {!activeAgents.length ? (
                <p className="agent-sharing-empty">当前还没有可导出的伙伴。添加伙伴后即可分享，无需先填写 Key。</p>
              ) : (
                <>
                  <div className="sharing-actions-bar">
                    <button
                      type="button"
                      className="sharing-link-btn"
                      disabled={exportBusy}
                      onClick={selectAllExport}
                    >
                      全选 ({activeAgents.length})
                    </button>
                    <span className="sharing-divider">|</span>
                    <button
                      type="button"
                      className="sharing-link-btn"
                      disabled={exportBusy || !exportSelected.length}
                      onClick={clearExport}
                    >
                      清空
                    </button>
                    <span className="sharing-selection-count">已选 {exportSelected.length} 位</span>
                  </div>

                  <div className="sharing-agent-roster" role="group" aria-label="选择要导出的伙伴">
                    {activeAgents.map(agent => {
                      const isChecked = exportSelected.includes(agent.agent_id);
                      return (
                        <label
                          key={agent.agent_id}
                          className={`sharing-agent-item ${isChecked ? 'selected' : ''}`}
                        >
                          <input
                            type="checkbox"
                            checked={isChecked}
                            disabled={exportBusy}
                            onChange={() => toggleExportItem(agent.agent_id)}
                          />
                          <div className="sharing-item-avatar">
                            <AgentPortrait profile={agent} compact size={36} />
                          </div>
                          <div className="sharing-item-info">
                            <span className="sharing-agent-name" title={agent.name}>{agent.name}</span>
                            <span className="sharing-agent-meta">
                              {agent.model || (agent.auth_mode === 'local_login' ? '本机登录' : '通用模型')}
                            </span>
                          </div>
                        </label>
                      );
                    })}
                  </div>

                  <div className="sharing-action-footer">
                    <button
                      type="button"
                      className="primary-button sharing-primary-btn"
                      disabled={exportBusy || !exportSelected.length}
                      onClick={handleExport}
                    >
                      <DownloadSimple size={18} aria-hidden="true" />
                      {exportBusy ? '正在打包…' : `导出并下载 (${exportSelected.length} 位伙伴)`}
                    </button>
                    <small className="sharing-footnote">
                      下载一个包含所选伙伴和外观素材的文件，发给朋友即可导入。
                    </small>
                  </div>

                  {exportWarnings.length > 0 && (
                    <div className="sharing-warnings" role="status">
                      <strong>导出提示：</strong>
                      <ul>
                        {exportWarnings.map((w, idx) => <li key={idx}>{w}</li>)}
                      </ul>
                    </div>
                  )}

                  {exportError && (
                    <div className="agent-error sharing-error-box" role="alert">
                      <span>{exportError}</span>
                      <button type="button" onClick={handleExport} disabled={exportBusy}>重试导出</button>
                    </div>
                  )}

                  {exportNotice && (
                    <div className="agent-notice sharing-notice-box" role="status">
                      <CheckCircle size={18} weight="fill" aria-hidden="true" />
                      <span>{exportNotice}</span>
                    </div>
                  )}
                </>
              )}
            </article>

            {/* 导入与预览区域 */}
            <article className="sharing-card sharing-import-card">
              <div className="sharing-card-header">
                <h3>预览并导入伙伴</h3>
                <span className="sharing-subhead">支持 32 MiB 以内的分享 JSON 文件</span>
              </div>

              <div className="sharing-file-picker-row">
                <label className="sharing-file-label">
                  <UploadSimple size={18} aria-hidden="true" />
                  <span>{importFile ? '更换分享包文件' : '选择本地分享包 (.json)'}</span>
                  <input
                    ref={fileInputRef}
                    type="file"
                    accept=".json,application/json"
                    disabled={previewBusy || importBusy}
                    onChange={handleFileSelect}
                  />
                </label>
                {importFile && (
                  <span className="sharing-current-filename" title={importFile.name}>
                    {importFile.name} ({(importFile.size / 1024).toFixed(1)} KB)
                  </span>
                )}
              </div>

              {previewBusy && (
                <div className="sharing-status-banner" role="status">
                  <ArrowsClockwise className="sharing-spin" size={16} aria-hidden="true" />
                  <span>正在解析并校验分享包…</span>
                </div>
              )}

              {previewError && (
                <div className="agent-error sharing-error-box" role="alert">
                  <span>{previewError}</span>
                </div>
              )}

              {previewWarnings.length > 0 && (
                <div className="sharing-warnings" role="status">
                  <strong>文件解析提示：</strong>
                  <ul>
                    {previewWarnings.map((w, idx) => <li key={idx}>{w}</li>)}
                  </ul>
                </div>
              )}

              {/* 预览伙伴列表 */}
              {importPreviewAgents.length > 0 && (
                <div className="sharing-preview-container">
                  <div className="sharing-preview-header">
                    <h4>分享包包含的伙伴 ({importPreviewAgents.length})</h4>
                    <div className="sharing-preview-batch-actions">
                      <button
                        type="button"
                        className="sharing-link-btn"
                        disabled={importBusy}
                        onClick={selectAllNew}
                      >
                        全选未重复
                      </button>
                      <span className="sharing-divider">|</span>
                      <button
                        type="button"
                        className="sharing-link-btn"
                        disabled={importBusy || !importSelectedIds.length}
                        onClick={clearAllImport}
                      >
                        全不选
                      </button>
                    </div>
                  </div>

                  <div className="sharing-preview-list" role="group" aria-label="待导入伙伴预览">
                    {importPreviewAgents.map(item => {
                      const isDup = !!item.already_imported;
                      const isChecked = importSelectedIds.includes(item.source_id);
                      return (
                        <div
                          key={item.source_id}
                          className={`sharing-preview-item ${isDup ? 'duplicate' : ''} ${isChecked ? 'selected' : ''}`}
                        >
                          <label className="sharing-preview-item-label">
                            <input
                              type="checkbox"
                              checked={isChecked}
                              disabled={isDup || importBusy}
                              onChange={() => toggleImportSelection(item.source_id)}
                            />
                            <div className="sharing-preview-avatar">
                              <PixelSprite character={previewAppearances.find(a=>a.source_id===item.appearance_id)} size={40} animate />
                            </div>
                            <div className="sharing-preview-item-body">
                              <div className="sharing-preview-item-title-row">
                                <strong className="sharing-preview-name">{item.name}</strong>
                                {isDup ? (
                                  <span className="sharing-tag-duplicate" title="本机已存在相同配置的伙伴">已存在 (重复)</span>
                                ) : (
                                  <span className="sharing-tag-new">可导入</span>
                                )}
                              </div>
                              <div className="sharing-preview-details">
                                <span>模型: <code>{item.model || '未设定'}</code></span>
                                {item.base_url && <span title={item.base_url}>URL: {item.base_url}</span>}
                                <span>执行端: {item.executor_kind || 'claude'}</span>
                              </div>
                            </div>
                          </label>
                        </div>
                      );
                    })}
                  </div>

                  {/* 导入说明与警告：补Key/沉睡提示 */}
                  <div className="sharing-import-advisory" role="note">
                    <p>
                      <strong>导入后续须知：</strong>
                      导入成功后，伙伴将被添加至工作台。由于分享包<strong>不含私密密钥</strong>，非本机免密登录的伙伴需前往上方
                      <strong>“快速配置多个伙伴”</strong>或单个伙伴的<strong>“编辑”</strong>入口填入本人可用 API Key。缺少连接配置的伙伴将处于未激活（沉睡）状态，无法开始执行任务。
                    </p>
                    <small>Codex 本机登录环境使用本地会话，通常无需额外补充 API Key。</small>
                  </div>

                  <div className="sharing-action-footer">
                    <button
                      type="button"
                      className="primary-button sharing-primary-btn"
                      disabled={importBusy || !importSelectedIds.length}
                      onClick={handleImport}
                    >
                      <UploadSimple size={18} aria-hidden="true" />
                      {importBusy ? '正在导入并注册…' : `导入选中的 ${importSelectedIds.length} 位伙伴`}
                    </button>
                  </div>
                </div>
              )}

              {importError && (
                <div className="agent-error sharing-error-box" role="alert">
                  <span>{importError}</span>
                  <button type="button" onClick={handleImport} disabled={importBusy}>重试导入</button>
                </div>
              )}

              {importSuccessNotice && (
                <div className="agent-notice sharing-notice-box" role="status">
                  <CheckCircle size={18} weight="fill" aria-hidden="true" />
                  <span>{importSuccessNotice}</span>
                </div>
              )}
            </article>
          </div>
        </div>
      </details>
    </section>
  );
}
