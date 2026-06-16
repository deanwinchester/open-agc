// =============================================
// Goals Management Module
// =============================================
import { cachedFetch } from './cache.js';
import { escapeHtml, showStatus, formatTime } from './utils.js';
import { state } from './state.js';

const STATUS_ICONS = { pending: '⬜', doing: '🔄', done: '✅', stuck: '🔴' };
const STATUS_LABELS = { pending: '待执行', doing: '执行中', done: '已完成', stuck: '受阻' };

// ── Initialization ──

export function initGoalFilters() {
  const searchInput = document.getElementById('goal-search-input');
  if (searchInput) {
    searchInput.addEventListener('input', () => {
      state.goalSearchQuery = searchInput.value.trim();
      clearTimeout(state.goalRefreshInterval);
      state.goalRefreshInterval = setTimeout(() => loadGoals(true), 300);
    });
  }

  document.getElementById('create-goal-btn')?.addEventListener('click', openGoalCreateModal);
  document.getElementById('goal-modal-close')?.addEventListener('click', closeGoalModal);
  document.getElementById('goal-modal-cancel')?.addEventListener('click', closeGoalModal);
  document.getElementById('goal-modal-save')?.addEventListener('click', saveGoalModal);

  // Desc char counter
  const descInput = document.getElementById('goal-desc-input');
  if (descInput) {
    descInput.addEventListener('input', () => {
      const len = descInput.value.length;
      const counter = document.getElementById('goal-desc-counter');
      if (counter) counter.textContent = `${len}/100`;
    });
  }

  // Close modal on overlay click
  document.getElementById('goal-modal')?.addEventListener('click', (e) => {
    if (e.target === e.currentTarget) closeGoalModal();
  });
}

// ── Load & Render ──

export async function loadGoals(resetPage = false) {
  const container = document.getElementById('goal-list-container');
  if (!container) return;

  try {
    const data = await cachedFetch('/api/goals');
    let items = data.items || [];

    // Search filter
    const q = (state.goalSearchQuery || '').toLowerCase();
    if (q) {
      items = items.filter(g => g.desc.toLowerCase().includes(q));
    }

    if (items.length === 0) {
      container.innerHTML = `
        <div class="empty-state">
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.3">
            <circle cx="12" cy="12" r="10"></circle>
            <path d="M12 6v6l4 2"></path>
          </svg>
          <p>${q ? '没有匹配的目标' : '暂无目标'}</p>
          <small>${q ? '尝试其他搜索词' : '添加大目标以跟踪重要任务进度，也可通过 Agent 的 manage_task_plan 工具自动管理'}</small>
        </div>`;
      return;
    }

    container.innerHTML = items.map(goal => renderGoalCard(goal)).join('');

    // Wire quick status pills
    container.querySelectorAll('.goal-status-btn').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        const id = parseInt(btn.dataset.id);
        const status = btn.dataset.status;
        try {
          await fetch(`/api/goals/${id}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status })
          });
          loadGoals();
        } catch (err) {
          showStatus('更新状态失败', 'error');
        }
      });
    });

    // Wire edit buttons
    container.querySelectorAll('.goal-edit-btn').forEach(btn => {
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        openGoalEditModal(parseInt(btn.dataset.id));
      });
    });

    // Wire delete buttons
    container.querySelectorAll('.goal-delete-btn').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (!confirm('确定删除此目标？')) return;
        try {
          await fetch(`/api/goals/${btn.dataset.id}`, { method: 'DELETE' });
          loadGoals();
        } catch (err) {
          showStatus('删除失败', 'error');
        }
      });
    });
  } catch (e) {
    console.error('[Goals] Load error:', e);
    container.innerHTML = `<div class="empty-state"><p>加载失败</p></div>`;
  }
}

function renderGoalCard(goal) {
  const icon = STATUS_ICONS[goal.status] || '⬜';
  const label = STATUS_LABELS[goal.status] || '待执行';
  const allStatuses = ['pending', 'doing', 'done', 'stuck'];

  return `
    <div class="goal-item" data-goal-id="${goal.id}">
      <div class="goal-item-icon ${goal.status}">${icon}</div>
      <div class="goal-item-body">
        <div class="goal-item-title">${escapeHtml(goal.desc)}</div>
        <div class="goal-item-meta">
          <span class="goal-status-badge ${goal.status}">${label}</span>
          <span>${goal.updated ? formatTime(goal.updated) : ''}</span>
          ${goal.task_id ? `<span class="goal-linked-task">关联任务 #${goal.task_id}</span>` : ''}
        </div>
      </div>
      <div class="goal-item-actions">
        <div class="goal-status-pills">
          ${allStatuses.map(s => `
            <button class="goal-status-btn${s === goal.status ? ' active' : ''}"
                    data-id="${goal.id}" data-status="${s}"
                    title="${STATUS_LABELS[s]}">${STATUS_ICONS[s]}</button>
          `).join('')}
        </div>
        <button class="goal-action-btn goal-edit-btn" data-id="${goal.id}" title="编辑">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"></path>
            <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"></path>
          </svg>
        </button>
        <button class="goal-action-btn goal-delete-btn" data-id="${goal.id}" title="删除">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
            <polyline points="3 6 5 6 21 6"></polyline>
            <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
          </svg>
        </button>
      </div>
    </div>`;
}

// ── Modal ──

function openGoalCreateModal() {
  document.getElementById('goal-modal-title').textContent = '添加目标';
  document.getElementById('goal-desc-input').value = '';
  document.getElementById('goal-desc-counter').textContent = '0/100';
  document.getElementById('goal-status-field').style.display = 'none';
  document.getElementById('goal-save-status').textContent = '';
  document.getElementById('goal-edit-id').value = '';
  document.getElementById('goal-modal').classList.add('active');
  document.getElementById('goal-desc-input')?.focus();
}

function openGoalEditModal(id) {
  // Find the goal data from DOM
  const container = document.getElementById('goal-list-container');
  // Fetch fresh data
  cachedFetch('/api/goals').then(data => {
    const goal = (data.items || []).find(g => g.id === id);
    if (!goal) { showStatus('未找到目标', 'error'); return; }

    document.getElementById('goal-modal-title').textContent = '编辑目标';
    document.getElementById('goal-desc-input').value = goal.desc;
    document.getElementById('goal-desc-counter').textContent = `${goal.desc.length}/100`;
    document.getElementById('goal-status-field').style.display = 'block';
    document.getElementById('goal-status-select').value = goal.status;
    document.getElementById('goal-save-status').textContent = '';
    document.getElementById('goal-edit-id').value = id;
    document.getElementById('goal-modal').classList.add('active');
    document.getElementById('goal-desc-input')?.focus();
  }).catch(() => showStatus('加载目标数据失败', 'error'));
}

function closeGoalModal() {
  document.getElementById('goal-modal').classList.remove('active');
}

async function saveGoalModal() {
  const editId = document.getElementById('goal-edit-id').value;
  const desc = document.getElementById('goal-desc-input').value.trim();
  const statusEl = document.getElementById('goal-save-status');

  if (!desc) {
    statusEl.textContent = '⚠️ 请输入目标描述';
    statusEl.style.color = 'var(--error)';
    return;
  }

  try {
    if (editId) {
      // Update
      const status = document.getElementById('goal-status-select').value;
      const resp = await fetch(`/api/goals/${editId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ desc, status })
      });
      if (!resp.ok) {
        const err = await resp.json();
        statusEl.textContent = `⚠️ ${err.detail || '保存失败'}`;
        statusEl.style.color = 'var(--error)';
        return;
      }
    } else {
      // Create
      const resp = await fetch('/api/goals', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ desc })
      });
      if (!resp.ok) {
        const err = await resp.json();
        statusEl.textContent = `⚠️ ${err.detail || '创建失败'}`;
        statusEl.style.color = 'var(--error)';
        return;
      }
    }
    closeGoalModal();
    loadGoals();
  } catch (e) {
    statusEl.textContent = '⚠️ 网络错误';
    statusEl.style.color = 'var(--error)';
  }
}

// Expose to window
window.loadGoals = loadGoals;
