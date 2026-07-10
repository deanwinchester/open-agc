/**
 * Settings: API Key management module.
 * Extracted from settings.js for better code organization.
 */
import { escapeHtml, showStatus } from './utils.js';
import { state } from './state.js';
import { cachedFetch } from './cache.js';

export const providers = [
  { key: "deepseek", label: "DeepSeek" },
  { key: "kimi", label: "Kimi (Moonshot)" },
  { key: "llamacpp", label: "Llama.cpp (本地/Local)" },
  { key: "openai", label: "OpenAI" },
  { key: "anthropic", label: "Anthropic" },
  { key: "gemini", label: "Google Gemini" },
  { key: "glm", label: "GLM (智谱)" },
  { key: "minimax", label: "MiniMax" },
  { key: "huggingface", label: "HuggingFace Token" },
  { key: "tavily", label: "Tavily Search" },
  { key: "brave_search", label: "Brave Search" },
  { key: "searxng", label: "SearXNG API Key" }
];

export function buildApiKeysGrid(maskedKeys) {
  const keys = maskedKeys || {};
  return providers.map(p => {
    const masked = keys[p.key] || '';
    const displayVal = masked ? (masked.startsWith('http') ? masked : masked.substring(0, 8) + '••••' + masked.slice(-4)) : '';
    return `<div class="api-key-row">
      <label>${p.label}</label>
      <div class="api-key-input-group">
        <input type="password" class="api-key-input" id="key-${p.key}" value=""
          placeholder="${masked ? escapeHtml(displayVal) : '未配置'}" />
        <button class="btn-icon toggle-key-btn" title="显示/隐藏密钥">👁️</button>
        ${masked && !masked.startsWith('http') ? `<button class="btn-icon test-key-btn" data-provider="${p.key}" title="测试连接">🔍</button>
        <button class="btn-icon stats-key-btn" data-provider="${p.key}" title="消耗统计">📊</button>` : ''}
      </div>
    </div>`;
  }).join('');
}

export async function showProviderStats(provider) {
  try {
    const data = await cachedFetch(`/api/settings/provider-stats?provider=${provider}`, {}, 30000);
    if (!data || !data.stats) {
      showStatus('暂无统计数据', 'info');
      return;
    }
    const s = data.stats;
    const existing = document.getElementById('provider-stats-modal');
    if (existing) existing.remove();

    const modal = document.createElement('div');
    modal.id = 'provider-stats-modal';
    modal.className = 'modal-overlay active';
    modal.innerHTML = `<div class="modal-content">
      <div class="modal-header">
        <h3>📊 ${provider} 消耗统计</h3>
        <button class="modal-close" id="stats-modal-close">&times;</button>
      </div>
      <div class="modal-body">
        <p>总 Token: <strong>${(s.total_tokens || 0).toLocaleString()}</strong></p>
        <p>总费用: <strong>¥${(s.total_cost || 0).toFixed(2)}</strong></p>
        <p>调用次数: <strong>${(s.call_count || 0).toLocaleString()}</strong></p>
        <p>最后调用: <strong>${s.last_call || 'N/A'}</strong></p>
        <canvas id="stats-chart" height="200"></canvas>
      </div>
    </div>`;
    document.body.appendChild(modal);
    document.getElementById('stats-modal-close').onclick = () => modal.remove();
    modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
  } catch (e) {
    showStatus('加载统计数据失败', 'error');
  }
}

export async function buildModelSelection(data) {
  const sel = document.getElementById('model-name-select');
  if (!sel) return;
  let html = '<option value="">-- 选择模型 --</option>';
  const model = data?.default_model || '';
  const models = data?.available_models || [];
  for (const m of models) {
    const selected = m === model ? 'selected' : '';
    html += `<option value="${escapeHtml(m)}" ${selected}>${escapeHtml(m)}</option>`;
  }
  sel.innerHTML = html;
}

export async function fetchModels(provider, modelToSelect = null) {
  try {
    const data = await cachedFetch(`/api/provider-models?provider=${provider}`, {}, 60000);
    const sel = document.getElementById('model-name-select');
    if (!sel) return;
    let html = '<option value="">-- 选择模型 --</option>';
    const models = data?.models || [];
    for (const m of models) {
      const selected = (m === modelToSelect) ? 'selected' : '';
      html += `<option value="${escapeHtml(m)}" ${selected}>${escapeHtml(m)}</option>`;
    }
    sel.innerHTML = html;
  } catch (e) {
    showStatus(`获取 ${provider} 模型列表失败`, 'error');
  }
}
