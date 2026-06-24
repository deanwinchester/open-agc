import { escapeHtml, showStatus, formatTimeAgo, formatTime } from './utils.js';
import { state } from './state.js';
import { cachedFetch } from './cache.js';


// Import sub-modules (moved from this file for better organization)
import {
  providers, buildApiKeysGrid, showProviderStats, buildModelSelection, fetchModels
} from './settings-api.js';
import {
  loadSkillsConfig, loadAgents
} from './settings-skills.js';

// Re-export for app.js compatibility
export { loadSkillsConfig, loadAgents };

// ===================== Settings =====================

// providers moved to settings-api.js

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
    const maxCorrectionEl = document.getElementById('max-correction-attempts');
    if (maxCorrectionEl) maxCorrectionEl.value = data.max_correction_attempts ?? 5;
    const coldCacheEl = document.getElementById('cold-cache-ttl');
    if (coldCacheEl) coldCacheEl.value = data.cold_cache_ttl ?? 3600;
    const maxResumeEl = document.getElementById('max-resume-count-input');
    if (maxResumeEl) maxResumeEl.value = data.max_resume_count ?? 10;
    const maxTokensEl = document.getElementById('max-total-tokens');
    if (maxTokensEl) maxTokensEl.value = (data.context_budget?.max_total_tokens) ?? 128000;
    document.getElementById('http-proxy-input').value = data.http_proxy || '';
    document.getElementById('heartbeat-toggle').checked = data.heartbeat_enabled ?? false;
    const hbInterval = document.getElementById('heartbeat-interval');
    if (hbInterval) hbInterval.value = String(data.heartbeat_interval ?? 60);

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

// buildApiKeysGrid moved to settings-api.js

// showProviderStats moved to settings-api.js
// showProviderStats moved to settings-api.js

// buildModelSelection moved to settings-api.js

// fetchModels moved to settings-api.js

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
    heartbeat_interval: parseInt(document.getElementById('heartbeat-interval')?.value) || 60,
    email_listener_enabled: document.getElementById('email-listener-toggle')?.checked ?? false,
    email_account: document.getElementById('email-account-input')?.value?.trim() || '',
    email_password: document.getElementById('email-password-input')?.value || (document.getElementById('email-password-input')?.placeholder === '***' ? '***' : ''),
    email_imap_server: document.getElementById('email-imap-input')?.value?.trim() || '',
    email_smtp_server: document.getElementById('email-smtp-input')?.value?.trim() || '',
    owner_email: document.getElementById('owner-email-input')?.value?.trim() || '',
    session_id: state.currentSessionId || 1,
    tool_permissions: null,
    searxng_url: document.getElementById('searxng-url-input')?.value?.trim() || '',
    searxng_port: 8888,
    max_correction_attempts: parseInt(document.getElementById('max-correction-attempts')?.value) || 5,
    cold_cache_ttl: parseInt(document.getElementById('cold-cache-ttl')?.value) || 3600,
    max_resume_count: parseInt(document.getElementById('max-resume-count-input')?.value) || 10,
    max_total_tokens: parseInt(document.getElementById('max-total-tokens')?.value) || 128000
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

// loadSkillsConfig moved to settings-skills.js

// initSkillsUI moved to settings-skills.js

// renderSkills moved to settings-skills.js

// formatSize moved to settings-skills.js

// ---- Edit Skill ----

// openEditSkillModal moved to settings-skills.js

// closeEditSkillModal moved to settings-skills.js

// saveSkillEdit moved to settings-skills.js

// ---- Delete Skill ----

let _deleteTargetFilename = null;

// openDeleteSkillModal moved to settings-skills.js

// closeDeleteSkillModal moved to settings-skills.js

// confirmDeleteSkill moved to settings-skills.js

// ===================== Agents =====================

// loadAgents moved to settings-skills.js

// renderAgentList moved to settings-skills.js

// populateAgentSelector moved to settings-skills.js

// loadAvailableModels moved to settings-skills.js

// openAgentModal moved to settings-skills.js

// closeAgentModal moved to settings-skills.js

// saveAgentFromModal moved to settings-skills.js

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
