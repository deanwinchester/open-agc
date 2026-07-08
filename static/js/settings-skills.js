/**
 * Settings: Skills & Agent Profiles management module.
 * Extracted from settings.js for better code organization.
 */
import { escapeHtml, showStatus } from './utils.js';
import { state } from './state.js';
import { cachedFetch } from './cache.js';

// ═══════════════ Skills Management ═══════════════

export async function loadSkillsConfig() {
  try {
    const data = await cachedFetch('/api/skills', {}, 10000);
    const skills = data?.skills || [];
    renderSkills(skills);
  } catch (e) {
    showStatus('加载技能配置失败', 'error');
  }
}

function initSkillsUI() {
  // No standalone init needed — functions are called from settings.js
}

function formatSize(bytes) {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0;
  let size = bytes;
  while (size >= 1024 && i < units.length - 1) { size /= 1024; i++; }
  return `${size.toFixed(1)} ${units[i]}`;
}

function renderSkills(skills) {
  const container = document.getElementById('skills-list') || document.getElementById('skills-config-container');
  if (!container) return;
  if (!skills.length) {
    container.innerHTML = '<p class="empty-state">暂无技能</p>';
    return;
  }
  container.innerHTML = skills.map(s => `
    <div class="skill-row">
      <div class="skill-info">
        <strong>${escapeHtml(s.filename || s.name || '未命名')}</strong>
        ${s.description ? `<p style="margin:0.25rem 0 0;font-size:0.85rem;color:var(--text-secondary);">${escapeHtml(s.description)}</p>` : ''}
        ${s.usage_count ? `<span style="font-size:0.78rem;color:var(--text-secondary);margin-left:0.5rem;">📊 使用 ${s.usage_count} 次</span>` : ''}
      </div>
      <div style="display:flex;gap:0.4rem;">
        <button class="btn-mini edit-skill-btn" data-filename="${escapeHtml(s.filename || s.name)}">编辑</button>
        <button class="btn-mini btn-danger delete-skill-btn" data-filename="${escapeHtml(s.filename || s.name)}">删除</button>
      </div>
    </div>
  `).join('');

  container.querySelectorAll('.edit-skill-btn').forEach(btn =>
    btn.addEventListener('click', () => openEditSkillModal(btn.dataset.filename)));
  container.querySelectorAll('.delete-skill-btn').forEach(btn =>
    btn.addEventListener('click', () => openDeleteSkillModal(btn.dataset.filename)));
}

// ---- Edit Skill ----
function openEditSkillModal(filename) {
  const modal = document.getElementById('edit-skill-modal');
  const nameEl = document.getElementById('edit-skill-name');
  const contentEl = document.getElementById('edit-skill-content');
  if (!modal || !nameEl || !contentEl) return;
  nameEl.value = filename;
  contentEl.value = '加载中...';
  modal.classList.add('active');
  cachedFetch(`/api/skills?filename=${encodeURIComponent(filename)}`, {}, 10000)
    .then(data => {
      if (data?.content) contentEl.value = data.content;
      else contentEl.value = '// 无法加载技能内容';
    })
    .catch(() => contentEl.value = '// 加载失败');
}

function closeEditSkillModal() {
  const modal = document.getElementById('edit-skill-modal');
  if (modal) modal.classList.remove('active');
}

async function saveSkillEdit() {
  const name = document.getElementById('edit-skill-name')?.value;
  const content = document.getElementById('edit-skill-content')?.value;
  if (!name || !content) return;
  try {
    const resp = await fetch('/api/skills', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filename: name, content })
    });
    if (resp.ok) {
      showStatus('技能已保存', 'success');
      closeEditSkillModal();
      loadSkillsConfig();
    } else {
      const err = await resp.json();
      showStatus(err.detail || '保存失败', 'error');
    }
  } catch (e) {
    showStatus('保存技能失败', 'error');
  }
}

// ---- Delete Skill ----
function openDeleteSkillModal(filename) {
  const modal = document.getElementById('delete-skill-modal');
  const msg = document.getElementById('delete-skill-msg');
  const confirmBtn = document.getElementById('confirm-delete-skill');
  if (!modal || !msg || !confirmBtn) return;
  msg.textContent = `确定要删除技能 "${filename}" 吗？`;
  confirmBtn.dataset.filename = filename;
  modal.classList.add('active');
}

function closeDeleteSkillModal() {
  const modal = document.getElementById('delete-skill-modal');
  if (modal) modal.classList.remove('active');
}

async function confirmDeleteSkill() {
  const btn = document.getElementById('confirm-delete-skill');
  const filename = btn?.dataset.filename;
  if (!filename) return;
  try {
    const resp = await fetch(`/api/skills?filename=${encodeURIComponent(filename)}`, { method: 'DELETE' });
    if (resp.ok) {
      showStatus('技能已删除', 'success');
      closeDeleteSkillModal();
      loadSkillsConfig();
    } else {
      showStatus('删除失败', 'error');
    }
  } catch (e) {
    showStatus('删除技能失败', 'error');
  }
}

// ═══════════════ Agent Profiles ═══════════════

export async function loadAgents() {
  try {
    const data = await cachedFetch('/api/settings', {}, 5000);
    const agents = data?.agent_profiles || [];
    renderAgentList(agents);
    populateAgentSelector(agents);
  } catch (e) {
    showStatus('加载 Agent 配置失败', 'error');
  }
}

function renderAgentList(agents) {
  const container = document.getElementById('agent-list');
  if (!container) return;
  container.innerHTML = agents.map((a, i) => `
    <div class="agent-card">
      <div class="agent-info">
        <strong>${escapeHtml(a.name || '未命名')}</strong>
        <p>${escapeHtml(a.prompt || '').substring(0, 100)}</p>
        <span class="agent-meta">模型: ${escapeHtml(a.model || '默认')}</span>
      </div>
      <div class="agent-actions">
        <button class="btn-mini edit-agent-btn" data-index="${i}">编辑</button>
      </div>
    </div>
  `).join('');
  container.querySelectorAll('.edit-agent-btn').forEach(btn =>
    btn.addEventListener('click', () => openAgentModal(agents[parseInt(btn.dataset.index)])));
}

function populateAgentSelector(agents) {
  const sel = document.getElementById('agent-selector');
  if (!sel) return;
  sel.innerHTML = '<option value="">默认 Agent</option>' +
    agents.map(a => `<option value="${escapeHtml(a.name)}">${escapeHtml(a.name)}</option>`).join('');
}

async function loadAvailableModels() {
  try {
    const data = await cachedFetch('/api/settings', {}, 5000);
    const models = data?.available_models || [];
    const sel = document.getElementById('agent-model');
    if (!sel) return;
    sel.innerHTML = models.map(m => `<option value="${escapeHtml(m)}">${escapeHtml(m)}</option>`).join('');
  } catch (e) {
    // Silently fail — model selector is optional
  }
}

function openAgentModal(agent) {
  const modal = document.getElementById('agent-modal');
  if (!modal) return;
  document.getElementById('agent-name').value = agent?.name || '';
  document.getElementById('agent-prompt').value = agent?.prompt || '';
  document.getElementById('agent-model').value = agent?.model || '';
  document.getElementById('agent-temp').value = agent?.temperature ?? 0.7;
  document.getElementById('agent-maxtokens').value = agent?.max_tokens || 4096;
  modal.classList.add('active');
  loadAvailableModels();
}

function closeAgentModal() {
  const modal = document.getElementById('agent-modal');
  if (modal) modal.classList.remove('active');
}

async function saveAgentFromModal() {
  const name = document.getElementById('agent-name')?.value;
  const prompt = document.getElementById('agent-prompt')?.value;
  const model = document.getElementById('agent-model')?.value;
  const temp = parseFloat(document.getElementById('agent-temp')?.value || '0.7');
  const maxTokens = parseInt(document.getElementById('agent-maxtokens')?.value || '4096');
  if (!name) { showStatus('请输入 Agent 名称', 'error'); return; }
  try {
    const resp = await fetch('/api/settings/agent-profiles', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, prompt, model, temperature: temp, max_tokens: maxTokens })
    });
    if (resp.ok) {
      showStatus('Agent 配置已保存', 'success');
      closeAgentModal();
      loadAgents();
    } else {
      showStatus('保存 Agent 配置失败', 'error');
    }
  } catch (e) {
    showStatus('保存 Agent 配置失败', 'error');
  }
}

// ═══════════════ Expose to window ═══════════════
// These are called from settings.js DOM event handlers
window.openEditSkillModal = openEditSkillModal;
window.closeEditSkillModal = closeEditSkillModal;
window.saveSkillEdit = saveSkillEdit;
window.openDeleteSkillModal = openDeleteSkillModal;
window.closeDeleteSkillModal = closeDeleteSkillModal;
window.confirmDeleteSkill = confirmDeleteSkill;
window.openAgentModal = openAgentModal;
window.closeAgentModal = closeAgentModal;
window.saveAgentFromModal = saveAgentFromModal;
