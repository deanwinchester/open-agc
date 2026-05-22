import { escapeHtml, showStatus, formatTimeAgo, formatTime } from './utils.js';
import { state } from './state.js';
import { cachedFetch } from './cache.js';

// ===================== Settings =====================

const providers = [
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

export async function loadSettingsConfig() {
  if (state.settingsLoaded) return;
  try {
    const sid = state.currentSessionId || 1;
    // Update email section label with current session name
    const label = document.getElementById('email-session-label');
    if (label) {
      const sn = state.sessions.find(s => s.id === sid);
      label.textContent = sn ? sn.name : ('会话 ' + sid);
    }
    const data = await cachedFetch(`/api/settings?session_id=${sid}`);

    buildApiKeysGrid(data.api_keys_masked || {});
    buildModelSelection(data);

    document.getElementById('sandbox-mode-toggle').checked = data.sandbox_mode ?? true;
    document.getElementById('sandbox-dir-input').value = data.sandbox_dir || '';
    document.getElementById('llamacpp-ctx-size').value = data.llamacpp_ctx_size || 32768;
    document.getElementById('http-proxy-input').value = data.http_proxy || '';
    document.getElementById('heartbeat-toggle').checked = data.heartbeat_enabled ?? false;

    document.getElementById('email-listener-toggle').checked = data.email_listener_enabled ?? false;
    document.getElementById('owner-email-input').value = data.owner_email || '';
    document.getElementById('email-account-input').value = data.email_account || '';
    document.getElementById('email-password-input').placeholder = data.email_password ? '***' : '密码或授权码';
    const searxngUrlInput = document.getElementById('searxng-url-input');
    if (searxngUrlInput) searxngUrlInput.value = data.searxng_url || '';
    document.getElementById('email-imap-input').value = data.email_imap_server || '';
    document.getElementById('email-smtp-input').value = data.email_smtp_server || '';
    if (document.getElementById('mcp-config-input')) {
        document.getElementById('mcp-config-input').value = data.mcp_servers && Object.keys(data.mcp_servers).length > 0 ? JSON.stringify(data.mcp_servers, null, 2) : '';
    }

    renderSandboxPaths(data.allowed_paths || [], data.denied_paths || []);
    renderToolPermissions(data.tool_permissions || {});

    state.settingsLoaded = true;
  } catch (err) {
    console.error("Failed to load settings config:", err);
  }
}

function buildApiKeysGrid(maskedKeys) {
  const grid = document.getElementById('api-keys-container');
  if (!grid) return;
  grid.innerHTML = '';
  const helpLinks = {
    deepseek: 'https://platform.deepseek.com/api-docs',
    kimi: 'https://platform.moonshot.cn/docs',
    tavily: 'https://tavily.com/',
    brave_search: 'https://api.search.brave.com/',
  };
  providers.forEach(p => {
    const mask = maskedKeys[p.key] || '';
    const hasSaved = mask.length > 0;
    let placeholder = '请输入密钥...';


    const helpHtml = helpLinks[p.key]
      ? ` <a href="${helpLinks[p.key]}" target="_blank" class="key-help-link" title="${p.label} 配置文档">?</a>`
      : '';

    const wrapper = document.createElement('div');
    wrapper.className = 'key-field';
    wrapper.innerHTML = `
      <label>${p.label}${helpHtml}</label>
      <div class="key-input-wrapper">
        <input type="password" id="key-${p.key}" placeholder="${hasSaved ? mask : placeholder}" autocomplete="new-password" spellcheck="false">
        <button type="button" class="toggle-visibility" data-target="key-${p.key}" title="显示/隐藏">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path>
            <circle cx="12" cy="12" r="3"></circle>
          </svg>
        </button>
      </div>
      <span class="key-status ${hasSaved ? 'saved' : ''}">${hasSaved ? '✓ 已保存' : '未配置'}</span>
      <button type="button" class="btn-icon view-stats-btn" data-provider="${p.key}" title="查看消耗统计" style="margin-left: 10px; font-size: 14px;">📊</button>
    `;
    grid.appendChild(wrapper);
  });
  grid.querySelectorAll('.toggle-visibility').forEach(btn => {
    btn.addEventListener('click', () => {
      const input = document.getElementById(btn.dataset.target);
      input.type = input.type === 'password' ? 'text' : 'password';
    });
  });
  grid.querySelectorAll('.view-stats-btn').forEach(btn => {
    btn.addEventListener('click', () => showProviderStats(btn.dataset.provider));
  });
}

let usageChart = null;
async function showProviderStats(provider) {
  const modal = document.getElementById('stats-modal');
  const title = document.getElementById('stats-modal-title');
  const summary = document.getElementById('stats-summary');
  const canvas = document.getElementById('token-usage-chart');
  
  if (!modal || !canvas) return;
  modal.classList.add('active');
  title.textContent = `📈 ${provider.toUpperCase()} 消耗统计 (近30天)`;
  summary.textContent = '正在加载统计数据...';
  
  try {
    const res = await fetch(`/api/stats/token_usage?provider=${provider}`);
    const result = await res.json();
    const data = result.data || [];
    
    if (data.length === 0) {
      summary.textContent = '暂无消耗数据。';
      if (usageChart) usageChart.destroy();
      return;
    }
    
    const labels = data.map(d => d.day);
    const totals = data.map(d => d.total);
    const prompts = data.map(d => d.prompt);
    const completions = data.map(d => d.completion);
    
    const totalTokens = totals.reduce((a, b) => a + b, 0);
    const totalCost = data.reduce((a, b) => a + b.cost, 0).toFixed(4);
    summary.innerHTML = `总消耗：<strong>${totalTokens.toLocaleString()}</strong> Tokens | 预估成本：<strong>$${totalCost}</strong>`;
    
    if (usageChart) usageChart.destroy();
    
    usageChart = new Chart(canvas, {
      type: 'line',
      data: {
        labels: labels,
        datasets: [
          {
            label: '总 Tokens',
            data: totals,
            borderColor: '#4a90e2',
            backgroundColor: 'rgba(74, 144, 226, 0.1)',
            fill: true,
            tension: 0.3
          },
          {
            label: 'Prompt',
            data: prompts,
            borderColor: '#2ecc71',
            borderDash: [5, 5],
            fill: false
          },
          {
            label: 'Completion',
            data: completions,
            borderColor: '#e67e22',
            borderDash: [2, 2],
            fill: false
          }
        ]
      },
      options: {
        responsive: true,
        plugins: {
          legend: { position: 'top' }
        },
        scales: {
          y: { beginAtZero: true }
        }
      }
    });
    
  } catch (e) {
    summary.textContent = '获取统计数据失败。';
    console.error(e);
  }
}

export async function buildModelSelection(data) {
  let selectedProvider = 'kimi';
  const dm = data.default_model || '';
  if (dm.startsWith('moonshot/')) selectedProvider = 'kimi';
  else if (dm.startsWith('llamacpp/')) selectedProvider = 'llamacpp';

  else if (dm.startsWith('zai/')) selectedProvider = 'glm';
  else if (dm.startsWith('minimax/')) selectedProvider = 'minimax';
  else if (dm.startsWith('gemini/')) selectedProvider = 'gemini';
  else if (dm.startsWith('deepseek/')) selectedProvider = 'deepseek';
  else if (dm.includes('claude')) selectedProvider = 'anthropic';
  else if (dm.startsWith('gpt')) selectedProvider = 'openai';

  const providerSelect = document.getElementById('provider-select');
  if (providerSelect) providerSelect.value = selectedProvider;

  const fallbackInput = document.getElementById('fallback-models-input');
  if (fallbackInput) fallbackInput.value = (data.fallback_models || []).join(', ');
  await fetchModels(selectedProvider, dm);
}

export async function fetchModels(provider, modelToSelect = null) {
  const select = document.getElementById('model-name-select');
  if (!select) return;
  select.innerHTML = '<option>加载中...</option>';
  try {
    const res = await fetch('/api/provider-models?provider=' + provider);
    const data = await res.json();
    select.innerHTML = '';
    (data.models || []).forEach(m => {
      const opt = document.createElement('option');
      opt.value = m;
      opt.textContent = m;
      select.appendChild(opt);
    });
    if (modelToSelect && data.models.includes(modelToSelect)) {
      select.value = modelToSelect;
    } else if (data.models.length > 0) {
      select.value = data.models[0];
    }
  } catch (e) {
    select.innerHTML = '<option value="">获取失败</option>';
  }
  const pullGroup = document.getElementById('pull-model-group');
  if (pullGroup) {
    pullGroup.style.display = 'none';
  }
}

export function initSettingsListeners() {
  document.getElementById('new-agent-btn')?.addEventListener('click', () => openAgentModal(null));
  document.getElementById('agent-modal-close')?.addEventListener('click', closeAgentModal);
  document.getElementById('agent-modal-cancel')?.addEventListener('click', closeAgentModal);
  document.getElementById('agent-modal-save')?.addEventListener('click', saveAgentFromModal);
  document.getElementById('agent-temp-input')?.addEventListener('input', (e) => {
    document.getElementById('agent-temp-display').textContent = e.target.value;
  });
  document.getElementById('agent-modal')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) closeAgentModal();
  });

  document.getElementById('provider-select')?.addEventListener('change', (e) => {
    fetchModels(e.target.value);
  });
  document.getElementById('fetch-models-btn')?.addEventListener('click', () => {
    fetchModels(document.getElementById('provider-select').value);
  });

  document.getElementById('pull-model-btn')?.addEventListener('click', async () => {
    const statusEl = document.getElementById('pull-model-status');
    const modelName = document.getElementById('pull-model-input').value.trim();
    const tool = document.getElementById('pull-tool-select').value;
    if (!modelName) { statusEl.textContent = '⚠️ 请先输入模型名称！'; return; }
    try {
      const res = await fetch('/api/sglang/pull', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model_name: modelName, tool })
      });
      const data = await res.json();
      statusEl.textContent = res.ok ? '✓ ' + (data.message || '已开始下载') : '✗ 拉取失败: ' + data.detail;
      statusEl.style.color = res.ok ? '#10b981' : '#ef4444';
    } catch (e) {
      statusEl.textContent = '✗ 网络错误或超时。';
      statusEl.style.color = '#ef4444';
    }
  });

  document.getElementById('save-settings-btn')?.addEventListener('click', saveSettings);
  document.getElementById('save-mcp-btn')?.addEventListener('click', saveSettings);
}

async function saveSettings() {
  const saveBtn = document.getElementById('save-settings-btn');
  const statusEl = document.getElementById('save-status');

  const payload = {
    api_keys: {},
    default_model: document.getElementById('model-name-select')?.value || 'moonshot/kimi-latest',
    fallback_models: (document.getElementById('fallback-models-input')?.value || '')
      .split(',').map(s => s.trim()).filter(s => s.length > 0),
    disabled_skills: [],
    sandbox_mode: document.getElementById('sandbox-mode-toggle')?.checked ?? true,
    sandbox_dir: document.getElementById('sandbox-dir-input')?.value?.trim() || '',
    llamacpp_ctx_size: parseInt(document.getElementById('llamacpp-ctx-size')?.value) || 32768,
    http_proxy: document.getElementById('http-proxy-input')?.value?.trim() || '',
    heartbeat_enabled: document.getElementById('heartbeat-toggle')?.checked ?? false,
    heartbeat_interval: 60,
    email_listener_enabled: document.getElementById('email-listener-toggle')?.checked ?? false,
    email_account: document.getElementById('email-account-input')?.value?.trim() || '',
    email_password: document.getElementById('email-password-input')?.value || (document.getElementById('email-password-input')?.placeholder === '***' ? '***' : ''),
    email_imap_server: document.getElementById('email-imap-input')?.value?.trim() || '',
    email_smtp_server: document.getElementById('email-smtp-input')?.value?.trim() || '',
    owner_email: document.getElementById('owner-email-input')?.value?.trim() || '',
    session_id: state.currentSessionId || 1,
    tool_permissions: null,
    searxng_url: document.getElementById('searxng-url-input')?.value?.trim() || '',
    searxng_port: 8888
  };

  // Include current tool_permissions in save
  if (Object.keys(_toolPermissions).length > 0) {
    payload.tool_permissions = JSON.parse(JSON.stringify(_toolPermissions));
  }

  try {
    const mcpStr = document.getElementById('mcp-config-input')?.value?.trim();
    payload.mcp_servers = mcpStr ? JSON.parse(mcpStr) : {};
  } catch (e) {
    if (statusEl) { statusEl.textContent = '✗ MCP JSON 格式错误'; statusEl.className = 'save-status error'; }
    if (saveBtn) saveBtn.disabled = false;
    return;
  }

  providers.forEach(p => {
    const el = document.getElementById(`key-${p.key}`);
    if (el && el.value && el.value.trim().length > 0) {
      payload.api_keys[p.key] = el.value.trim();
    }
  });

  document.querySelectorAll('.skill-toggle').forEach(cb => {
    if (!cb.checked) payload.disabled_skills.push(cb.dataset.name);
  });

  if (saveBtn) saveBtn.disabled = true;
  if (statusEl) { statusEl.textContent = '保存中...'; statusEl.className = 'save-status'; }

  try {
    const res = await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (data.status === 'success') {
      if (statusEl) { statusEl.textContent = '✓ 保存成功！'; statusEl.className = 'save-status success'; }
      document.getElementById('current-model-badge').textContent = payload.default_model;
      state.settingsLoaded = false;
      setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 2000);
    } else {
      if (statusEl) { statusEl.textContent = '✗ 保存失败: ' + (data.detail || '未知错误'); statusEl.className = 'save-status error'; }
    }
  } catch (e) {
    if (statusEl) { statusEl.textContent = '✗ 网络错误'; statusEl.className = 'save-status error'; }
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
}

// ===================== Skills =====================

let _allSkills = []; // cache for client-side search
let _skillsInitDone = false;

export async function loadSkillsConfig() {
  if (state.skillsLoaded) return;
  const container = document.getElementById('skills-config-container');
  if (!container) return;
  container.innerHTML = `<div class="loading-indicator"><div class="spinner"></div><span>加载中...</span></div>`;

  try {
    const res = await fetch('/api/skills');
    const data = await res.json();
    _allSkills = data.skills || [];
    renderSkills(_allSkills);
    state.skillsLoaded = true;

    // One-time init for search and modal listeners
    if (!_skillsInitDone) {
      initSkillsUI();
      _skillsInitDone = true;
    }
  } catch (e) {
    container.innerHTML = '<div class="empty-state"><p style="color:var(--error)">加载技能列表失败</p></div>';
  }
}

function initSkillsUI() {
  const searchInput = document.getElementById('skills-search-input');
  if (searchInput) {
    searchInput.addEventListener('input', (e) => {
      const q = e.target.value.toLowerCase().trim();
      if (!q) { renderSkills(_allSkills); return; }
      const filtered = _allSkills.filter(s => {
        const name = (s.name || s.filename || '').toLowerCase();
        return name.includes(q);
      });
      renderSkills(filtered);
    });
  }
  document.getElementById('edit-skill-close')?.addEventListener('click', closeEditSkillModal);
  document.getElementById('edit-skill-close2')?.addEventListener('click', closeEditSkillModal);
  document.getElementById('edit-skill-save')?.addEventListener('click', saveSkillEdit);
  document.getElementById('delete-skill-cancel')?.addEventListener('click', closeDeleteSkillModal);
  document.getElementById('delete-skill-confirm')?.addEventListener('click', confirmDeleteSkill);
}

function renderSkills(skills) {
  const container = document.getElementById('skills-config-container');
  if (!container) return;
  container.innerHTML = '';

  // Update count
  const countEl = document.getElementById('skills-count');
  if (countEl) {
    const total = _allSkills.length;
    const shown = skills.length;
    countEl.textContent = shown < total ? `显示 ${shown}/${total} 个技能` : `共 ${total} 个技能`;
  }

  if (!skills || skills.length === 0) {
    container.innerHTML = '<div class="empty-state"><p>暂无可管理的技能</p></div>';
    return;
  }

  skills.forEach(s => {
    const isChecked = s.enabled ? 'checked' : '';
    const icon = s.type === 'md' ? '📄' : '🐍';
    const displayName = s.name && s.name !== 'undefined' ? s.name : (s.filename || 'Undefined Skill');
    const sizeStr = s.size ? formatSize(s.size) : '';
    const metaParts = [];
    if (s.type === 'md') metaParts.push('Markdown');
    if (s.type === 'py') metaParts.push('Python');
    if (sizeStr) metaParts.push(sizeStr);
    if (s.lines) metaParts.push(`${s.lines} 行`);
    if (s.usage_count != null && s.usage_count > 0) metaParts.push(`使用 ${s.usage_count} 次`);
    if (s.success_rate != null && s.usage_count > 0) metaParts.push(`成功率 ${(s.success_rate * 100).toFixed(0)}%`);
    const metaStr = metaParts.join(' · ');

    const div = document.createElement('div');
    div.className = 'skill-row';
    div.dataset.filename = s.filename;
    div.innerHTML = `
      <div class="skill-info">
        <strong>${icon} ${displayName}</strong>
        <small>${metaStr || (s.type === 'md' ? 'Markdown Prompt' : '大模型技能')}</small>
      </div>
      <div class="skill-actions">
        <button class="btn-text btn-delete-skill" title="删除技能" data-filename="${s.filename}">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
            <polyline points="3 6 5 6 21 6"></polyline>
            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
          </svg>
        </button>
        <button class="btn-secondary btn-edit-skill" style="padding: 0.2rem 0.6rem; font-size: 0.85rem;" data-filename="${s.filename}">编辑</button>
        <label class="switch">
          <input type="checkbox" class="skill-toggle" data-name="${s.filename || s.name}" ${isChecked}>
          <span class="slider"></span>
        </label>
      </div>
    `;
    container.appendChild(div);
  });

  // Event listeners
  container.querySelectorAll('.btn-edit-skill').forEach(btn => {
    btn.addEventListener('click', () => openEditSkillModal(btn.dataset.filename));
  });
  container.querySelectorAll('.btn-delete-skill').forEach(btn => {
    btn.addEventListener('click', () => openDeleteSkillModal(btn.dataset.filename));
  });
}

function formatSize(bytes) {
  if (!bytes) return '';
  if (bytes < 1024) return `${bytes}B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)}KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)}MB`;
}

// ---- Edit Skill ----

function openEditSkillModal(filename) {
  if (!filename) return;
  const editSkillFilename = document.getElementById('edit-skill-filename');
  const editSkillContent = document.getElementById('edit-skill-content');
  editSkillFilename.textContent = filename;
  editSkillContent.value = '正在读取内容...';
  document.getElementById('edit-skill-modal').classList.add('show');

  fetch(`/api/skills/${encodeURIComponent(filename)}`)
    .then(res => res.ok ? res.json() : { content: '读取失败' })
    .then(data => { editSkillContent.value = data.content || ''; })
    .catch(() => { editSkillContent.value = '读取网络错误。'; });
}

function closeEditSkillModal() {
  document.getElementById('edit-skill-modal').classList.remove('show');
}

async function saveSkillEdit() {
  const filename = document.getElementById('edit-skill-filename').textContent;
  const content = document.getElementById('edit-skill-content').value;
  const saveBtn = document.getElementById('edit-skill-save');
  const ogText = saveBtn.textContent;
  saveBtn.textContent = '保存中...';
  saveBtn.disabled = true;
  try {
    const res = await fetch('/api/skills/import', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename, content, force: true })
    });
    const data = await res.json();
    if (data.success) {
      saveBtn.textContent = '✓ 已保存';
      state.skillsLoaded = false;
      setTimeout(() => {
        document.getElementById('edit-skill-modal').classList.remove('show');
        saveBtn.textContent = ogText;
        saveBtn.disabled = false;
        loadSkillsConfig(); // Refresh list
      }, 800);
    } else {
      alert('保存失败: ' + data.message);
      saveBtn.textContent = ogText;
      saveBtn.disabled = false;
    }
  } catch (e) {
    alert('保存时发生网络错误');
    saveBtn.textContent = ogText;
    saveBtn.disabled = false;
  }
}

// ---- Delete Skill ----

let _deleteTargetFilename = null;

function openDeleteSkillModal(filename) {
  _deleteTargetFilename = filename;
  document.getElementById('delete-skill-name').textContent = filename;
  document.getElementById('delete-skill-modal').classList.add('show');
}

function closeDeleteSkillModal() {
  _deleteTargetFilename = null;
  document.getElementById('delete-skill-modal').classList.remove('show');
}

async function confirmDeleteSkill() {
  const filename = _deleteTargetFilename;
  if (!filename) return;
  const confirmBtn = document.getElementById('delete-skill-confirm');
  confirmBtn.textContent = '删除中...';
  confirmBtn.disabled = true;
  try {
    const res = await fetch(`/api/skills/${encodeURIComponent(filename)}`, { method: 'DELETE' });
    const data = await res.json();
    if (data.success) {
      state.skillsLoaded = false;
      closeDeleteSkillModal();
      loadSkillsConfig(); // Refresh list
    } else {
      alert('删除失败: ' + (data.detail || '未知错误'));
    }
  } catch (e) {
    alert('删除时发生网络错误');
  } finally {
    confirmBtn.textContent = '确认删除';
    confirmBtn.disabled = false;
  }
}

// ===================== Agents =====================

export async function loadAgents() {
  try {
    const res = await fetch('/api/agents');
    const data = await res.json();
    renderAgentList(data.agents || []);
    populateAgentSelector(data.agents || []);
    loadAvailableModels();

    const settingsData = await cachedFetch('/api/settings');
    const mcpEl = document.getElementById('mcp-config-input');
    if (mcpEl && settingsData.mcp_servers) {
      mcpEl.value = typeof settingsData.mcp_servers === 'string' ? settingsData.mcp_servers : JSON.stringify(settingsData.mcp_servers, null, 2);
    }
  } catch (e) {
    console.error('Failed to load agents:', e);
  }
}

function renderAgentList(agents) {
  const container = document.getElementById('agent-list-container');
  if (!container) return;
  if (!agents.length) {
    container.innerHTML = '<div class="empty-state"><p>暂无 Agent，点击上方按钮创建</p></div>';
    return;
  }
  container.innerHTML = agents.map((a, i) => `
    <div class="agent-card" data-index="${i}">
      <div class="agent-card-info">
        <div class="agent-card-name">${a.name}</div>
        <div class="agent-card-model">${a.model || '使用默认模型'}</div>
        <div class="agent-card-prompt">${(a.prompt || '').substring(0, 80)}${(a.prompt || '').length > 80 ? '...' : ''}</div>
      </div>
      <div class="agent-card-actions">
        <button class="agent-edit-btn" data-name="${a.name}" title="编辑">✎</button>
        <button class="agent-delete-btn" data-name="${a.name}" title="删除">×</button>
      </div>
    </div>
  `).join('');
  container.querySelectorAll('.agent-edit-btn').forEach(btn => {
    btn.addEventListener('click', () => openAgentModal(btn.dataset.name));
  });
  container.querySelectorAll('.agent-delete-btn').forEach(btn => {
    btn.addEventListener('click', async () => {
      if (confirm(`确定删除 Agent "${btn.dataset.name}"？`)) {
        const res = await fetch(`/api/agents/${encodeURIComponent(btn.dataset.name)}`, { method: 'DELETE' });
        if (res.ok) await loadAgents();
      }
    });
  });
}

function populateAgentSelector(agents) {
  const sel = document.getElementById('agent-selector');
  if (!sel) return;
  sel.innerHTML = '<option value="default">默认智能体</option>';
  agents.forEach(a => {
    const opt = document.createElement('option');
    opt.value = a.name;
    opt.textContent = a.name;
    sel.appendChild(opt);
  });
}

async function loadAvailableModels() {
  try {
    const res = await fetch('/api/models/available');
    const data = await res.json();
    const sel = document.getElementById('agent-model-select');
    if (!sel) return;
    sel.innerHTML = '<option value="">使用默认模型</option>';
    (data.models || []).forEach(m => {
      const opt = document.createElement('option');
      opt.value = m;
      opt.textContent = m;
      sel.appendChild(opt);
    });
  } catch (e) {
    console.error('Failed to load models:', e);
  }
}

function openAgentModal(name) {
  state.editingAgentName = name;
  document.getElementById('agent-modal-title').textContent = name ? '编辑 Agent' : '新建 Agent';
  document.getElementById('agent-name-input').value = '';
  document.getElementById('agent-prompt-input').value = '';
  document.getElementById('agent-model-select').value = '';
  document.getElementById('agent-temp-input').value = '0.7';
  document.getElementById('agent-temp-display').textContent = '0.7';
  document.getElementById('agent-maxtokens-input').value = '4096';
  document.getElementById('agent-edit-original-name').value = name || '';

  if (name) {
    fetch('/api/agents').then(r => r.json()).then(data => {
      const agent = (data.agents || []).find(a => a.name === name);
      if (agent) {
        document.getElementById('agent-name-input').value = agent.name || '';
        document.getElementById('agent-prompt-input').value = agent.prompt || '';
        document.getElementById('agent-model-select').value = agent.model || '';
        document.getElementById('agent-temp-input').value = agent.temperature ?? 0.7;
        document.getElementById('agent-temp-display').textContent = agent.temperature ?? 0.7;
        document.getElementById('agent-maxtokens-input').value = agent.max_tokens || 4096;
      }
    });
  }
  document.getElementById('agent-modal').style.display = 'flex';
}

function closeAgentModal() {
  document.getElementById('agent-modal').style.display = 'none';
  state.editingAgentName = null;
}

async function saveAgentFromModal() {
  const name = document.getElementById('agent-name-input').value.trim();
  const prompt = document.getElementById('agent-prompt-input').value.trim();
  if (!name || !prompt) { alert('名称和提示词不能为空'); return; }
  const data = {
    name,
    prompt,
    model: document.getElementById('agent-model-select').value,
    temperature: parseFloat(document.getElementById('agent-temp-input').value) || 0.7,
    max_tokens: parseInt(document.getElementById('agent-maxtokens-input').value) || 4096,
  };
  const originalName = document.getElementById('agent-edit-original-name').value;
  try {
    let res;
    if (originalName) {
      res = await fetch(`/api/agents/${encodeURIComponent(originalName)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
      });
    } else {
      res = await fetch('/api/agents', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
      });
    }
    if (res.ok) {
      closeAgentModal();
      await loadAgents();
    } else {
      const err = await res.json();
      alert('保存失败: ' + (err.detail || '未知错误'));
    }
  } catch (e) {
    alert('网络错误');
  }
}

// ===================== AI Model Designer =====================

export function openAIDesignModal() {
  state.aiDesignResult = null;
  document.getElementById('ai-design-requirements').value = '';
  document.getElementById('ai-design-result').style.display = 'none';
  document.getElementById('ai-design-progress').style.display = 'none';
  document.getElementById('ai-design-submit').style.display = '';
  document.getElementById('ai-design-apply').style.display = 'none';

  const sel = document.getElementById('ai-design-agent');
  if (sel) {
    sel.innerHTML = '<option value="default">默认智能体</option>';
    fetch('/api/agents').then(r => r.json()).then(data => {
      (data.agents || []).forEach(a => {
        const opt = document.createElement('option');
        opt.value = a.name;
        opt.textContent = a.name;
        sel.appendChild(opt);
      });
    });
  }
  document.getElementById('ai-design-modal').style.display = 'flex';
}

export function closeAIDesignModal() {
  document.getElementById('ai-design-modal').style.display = 'none';
}

export function initAIDesignListeners() {
  document.getElementById('ai-design-submit')?.addEventListener('click', submitAIDesign);
  document.getElementById('ai-design-apply')?.addEventListener('click', applyAIDesignToForm);
  document.getElementById('ai-design-modal')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) closeAIDesignModal();
  });
  document.getElementById('ai-design-modal-close')?.addEventListener('click', closeAIDesignModal);
  document.getElementById('ai-design-cancel')?.addEventListener('click', closeAIDesignModal);
}

async function submitAIDesign() {
  const agent = document.getElementById('ai-design-agent').value;
  const requirements = document.getElementById('ai-design-requirements').value.trim();
  if (!requirements) { alert('请输入需求描述'); return; }

  document.getElementById('ai-design-submit').style.display = 'none';
  document.getElementById('ai-design-cancel').textContent = '取消';
  document.getElementById('ai-design-progress').style.display = '';
  document.getElementById('ai-design-result').style.display = 'none';

  const statusEl = document.getElementById('ai-design-status');
  const statusMsgs = ['正在连接 AI 模型...', 'AI 正在分析需求...', 'AI 正在设计模型架构...', '即将完成，请稍候...'];
  let msgIdx = 0;
  statusEl.textContent = statusMsgs[0];
  const statusTimer = setInterval(() => {
    msgIdx = Math.min(msgIdx + 1, statusMsgs.length - 1);
    statusEl.textContent = statusMsgs[msgIdx];
  }, 8000);

  state.aiDesignAbort = new AbortController();
  const timeout = setTimeout(() => state.aiDesignAbort.abort(), 120000);

  try {
    const res = await fetch('/api/agent-design', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ agent_name: agent, requirements }),
      signal: state.aiDesignAbort.signal
    });
    if (!res.ok) {
      const err = await res.json();
      throw new Error(err.detail || '设计失败');
    }
    const data = await res.json();
    const reply = data.response || '';
    clearInterval(statusTimer);
    clearTimeout(timeout);

    let jsonStr = reply;
    const jsonMatch = reply.match(/```(?:json)?\s*([\s\S]*?)```/);
    if (jsonMatch) jsonStr = jsonMatch[1];
    const braceMatch = jsonStr.match(/\{[\s\S]*\}/);
    if (braceMatch) jsonStr = braceMatch[0];

    try {
      state.aiDesignResult = JSON.parse(jsonStr);
    } catch (e) {
      state.aiDesignResult = null;
      document.getElementById('ai-design-result-json').textContent = reply;
      document.getElementById('ai-design-result').style.display = '';
      statusEl.textContent = '⚠️ 返回格式异常，请手动查看原始输出';
      document.getElementById('ai-design-apply').style.display = 'none';
      document.getElementById('ai-design-cancel').textContent = '关闭';
      return;
    }

    document.getElementById('ai-design-result-json').textContent = JSON.stringify(state.aiDesignResult, null, 2);
    document.getElementById('ai-design-result').style.display = '';
    document.getElementById('ai-design-progress').style.display = 'none';
    statusEl.textContent = '✅ 设计完成';
    document.getElementById('ai-design-apply').style.display = '';
    document.getElementById('ai-design-cancel').textContent = '关闭';
  } catch (e) {
    clearInterval(statusTimer);
    clearTimeout(timeout);
    document.getElementById('ai-design-progress').style.display = 'none';
    statusEl.textContent = e.name === 'AbortError' ? '⏱️ 请求超时，请重试或简化需求描述' : '❌ ' + e.message;
    document.getElementById('ai-design-submit').style.display = '';
    document.getElementById('ai-design-apply').style.display = 'none';
    document.getElementById('ai-design-cancel').textContent = '关闭';
  }
}

function applyAIDesignToForm() {
  if (!state.aiDesignResult) return;
  const params = state.aiDesignResult.params || {};
  const arch = state.aiDesignResult.architecture;

  if (arch) {
    document.querySelectorAll('.arch-option').forEach(btn => {
      btn.classList.toggle('selected', btn.dataset.arch === arch);
    });
  }

  const fieldMap = {
    'num_layers': 'hp-num-layers', 'hidden_dim': 'hp-hidden-size',
    'num_attn_heads': 'hp-num-heads', 'intermediate_dim': 'hp-intermediate',
    'vocab_size': 'hp-vocab-size', 'max_seq_len': 'hp-max-seq',
    'dropout': 'hp-attn-dropout', 'head_dim': 'hp-head-dim',
    'rope_theta': 'hp-rope-theta', 'init_range': 'hp-init-range',
  };
  for (const [key, id] of Object.entries(fieldMap)) {
    const el = document.getElementById(id);
    if (el && params[key] !== undefined) el.value = params[key];
  }

  const selectMap = {
    'attn_type': 'hp-attention-type', 'norm_position': 'hp-norm-position',
    'norm_type': 'hp-norm-type', 'pos_encoding': 'hp-pos-encoding',
    'activation': 'hp-activation',
  };
  for (const [key, id] of Object.entries(selectMap)) {
    const el = document.getElementById(id);
    if (el && params[key]) el.value = params[key];
  }

  if (typeof updateArchFieldVisibility === 'function') updateArchFieldVisibility();
  if (typeof renderArchitectureViz === 'function') renderArchitectureViz();
  if (typeof estimateModel === 'function') estimateModel();

  closeAIDesignModal();
}

// ===================== Expose to window for legacy navigation =====================
let _toolPermissions = {};  // Cache for saveSettings()
function renderSandboxPaths(allowed, denied) {
  renderPathChips('allowed-paths-list', allowed, 'allowed');
  renderPathChips('denied-paths-list', denied, 'denied');
}

function renderPathChips(containerId, paths, listType) {
  const container = document.getElementById(containerId);
  if (!container) return;
  container.innerHTML = '';
  if (!paths || paths.length === 0) {
    container.innerHTML = '<span style="color:var(--text-secondary);font-size:0.82rem;">无</span>';
    return;
  }
  paths.forEach(function(p) {
    if (!p) return;
    var chip = document.createElement('span');
    chip.className = 'path-chip';
    chip.title = p;
    chip.innerHTML = `${p.substring(0, 50)}${p.length > 50 ? '...' : ''} <button class="path-chip-del" title="移除">×</button>`;
    chip.querySelector('.path-chip-del').addEventListener('click', function(e) {
      e.stopPropagation();
      fetch('/api/sandbox/remove-path', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path: p, type: listType })
      }).then(function(r) { return r.json(); }).then(function(d) {
        if (d.ok) {
          state.settingsLoaded = false;
          loadSettingsConfig();
        }
      });
    });
    container.appendChild(chip);
  });
}

function renderToolPermissions(perms) {
  _toolPermissions = perms;
  // Network domains
  renderPermChips('network-domain-list', perms.network, 'network');
  // Command categories (all except network)
  var cmdPerms = {};
  for (var cat in perms) {
    if (cat !== 'network' && perms.hasOwnProperty(cat)) {
      cmdPerms[cat] = perms[cat];
    }
  }
  renderCommandPerms('cmd-permission-list', cmdPerms);
}

function renderPermChips(containerId, entries, category) {
  var container = document.getElementById(containerId);
  if (!container) return;
  container.innerHTML = '';
  if (!entries || typeof entries !== 'object' || Object.keys(entries).length === 0) {
    container.innerHTML = '<span style="color:var(--text-secondary);font-size:0.82rem;">无</span>';
    return;
  }
  Object.keys(entries).forEach(function(key) {
    var status = entries[key];
    var statusIcon = status === 'allow' ? '✅' : status === 'session_allow' ? '🔄' : status === 'permanent_deny' || status === 'deny' ? '🚫' : '❓';
    var chip = document.createElement('span');
    chip.className = 'path-chip';
    chip.title = key + ' → ' + status;
    chip.innerHTML = statusIcon + ' ' + key.substring(0, 40) + ' <button class="path-chip-del" title="移除">×</button>';
    chip.querySelector('.path-chip-del').addEventListener('click', function(e) {
      e.stopPropagation();
      fetch('/api/sandbox/remove-permission', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ category: category, key: key })
      }).then(function(r) { return r.json(); }).then(function(d) {
        if (d.ok) {
          state.settingsLoaded = false;
          loadSettingsConfig();
        }
      });
    });
    container.appendChild(chip);
  });
}

function renderCommandPerms(containerId, cmdPerms) {
  var container = document.getElementById(containerId);
  if (!container) return;
  container.innerHTML = '';
  var categories = Object.keys(cmdPerms);
  if (categories.length === 0) {
    container.innerHTML = '<span style="color:var(--text-secondary);font-size:0.82rem;">无</span>';
    return;
  }
  categories.forEach(function(cat) {
    var entries = cmdPerms[cat];
    if (typeof entries !== 'object') {
      entries = { '_': entries };
    }
    Object.keys(entries).forEach(function(key) {
      var status = entries[key];
      var statusIcon = status === 'allow' ? '✅' : status === 'session_allow' ? '🔄' : status === 'permanent_deny' || status === 'deny' ? '🚫' : '❓';
      var label = cat + (key !== '_' && key !== cat ? '/' + key : '');
      var chip = document.createElement('span');
      chip.className = 'path-chip';
      chip.title = cat + ' → ' + status;
      chip.innerHTML = statusIcon + ' ' + label.substring(0, 50) + ' <button class="path-chip-del" title="移除">×</button>';
      chip.querySelector('.path-chip-del').addEventListener('click', function(e) {
        e.stopPropagation();
        fetch('/api/sandbox/remove-permission', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ category: cat, key: key })
        }).then(function(r) { return r.json(); }).then(function(d) {
          if (d.ok) {
            state.settingsLoaded = false;
            loadSettingsConfig();
          }
        });
      });
      container.appendChild(chip);
    });
  });
}

window.loadSettingsConfig = loadSettingsConfig;
window.loadSkillsConfig = loadSkillsConfig;
window.loadAgents = loadAgents;
window.openAIDesignModal = openAIDesignModal;
window.closeAIDesignModal = closeAIDesignModal;

document.getElementById('stats-modal-close')?.addEventListener('click', () => {
  document.getElementById('stats-modal').classList.remove('active');
});
