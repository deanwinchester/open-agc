import { escapeHtml, showStatus, formatTimeAgo, formatTime } from './utils.js';
import { state } from './state.js';
import { cachedFetch } from './cache.js';

// ===================== Task Management =====================

export function initTaskFilters() {
  document.querySelectorAll('.filter-pill').forEach(pill => {
    pill.addEventListener('click', () => {
      document.querySelectorAll('.filter-pill').forEach(p => p.classList.remove('active'));
      pill.classList.add('active');
      state.taskFilter = pill.dataset.filter;
      loadTasks();
    });
  });
  document.getElementById('task-search-input')?.addEventListener('input', (e) => {
    state.taskSearchQuery = e.target.value.trim();
    clearTimeout(state.taskRefreshInterval);
    state.taskRefreshInterval = setTimeout(loadTasks, 300);
  });
  document.getElementById('task-detail-back')?.addEventListener('click', () => {
    switchView('tasks');
  });
}

export async function loadTasks() {
  const container = document.getElementById('task-list-container');
  if (!container) return;

  try {
    let url = '/api/tasks';
    const params = [];
    if (state.taskFilter !== 'all') params.push(`status=${state.taskFilter}`);
    if (state.taskSearchQuery) params.push(`q=${encodeURIComponent(state.taskSearchQuery)}`);
    if (params.length > 0) url += '?' + params.join('&');

    const data = await cachedFetch(url);

    if (!data.tasks || data.tasks.length === 0) {
      container.innerHTML = `
        <div class="empty-state">
          <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" opacity="0.3">
            <path d="M9 11l3 3L22 4"></path>
            <path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"></path>
          </svg>
          <p>暂无任务记录</p>
          <small>当 Agent 在会话中执行工具操作时，任务将自动记录在此</small>
        </div>
      `;
      return;
    }

    container.innerHTML = '';
    data.tasks.forEach(task => {
      const statusIcon = { running: '⏳', completed: '✅', failed: '❌', interrupted: '⏸️', scheduled: '⏰', paused: '⏸️' }[task.status] || '📋';
      const timeAgo = formatTimeAgo(task.created_at);

      const typeBadge = {
        oneshot: '<span class="task-type-badge oneshot">一次性</span>',
        scheduled: '<span class="task-type-badge scheduled">⏰ 定时</span>',
        longrun: '<span class="task-type-badge longrun">🔬 长期</span>'
      }[task.task_type] || '';

      let scheduleInfo = '';
      if (task.task_type === 'scheduled' && task.schedule_cron) {
        const enabled = task.schedule_enabled ? '✅ 启用' : '⏸️ 暂停';
        scheduleInfo = `<span class="task-schedule-info">${enabled} | <code>${task.schedule_cron}</code></span>`;
      }
      if (task.task_type === 'longrun' && task.resume_count > 0) {
        scheduleInfo = `<span class="task-schedule-info">🔄 已恢复 ${task.resume_count} 次</span>`;
      }

      const item = document.createElement('div');
      item.className = 'task-item';
      item.innerHTML = `
        <div class="task-item-icon ${task.status}">${statusIcon}</div>
        <div class="task-item-body">
          <div class="task-item-title">${typeBadge} ${escapeHtml(task.title)}</div>
          <div class="task-item-meta">
            <span>${timeAgo}</span>
            <span>${task.step_count || 0} 步</span>
            ${scheduleInfo}
          </div>
        </div>
        <div class="task-item-actions">
          ${task.task_type === 'scheduled' ? `
            <button class="task-action-btn toggle-schedule" data-id="${task.id}" title="${task.schedule_enabled ? '暂停定时' : '启用定时'}">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                ${task.schedule_enabled ? '<rect x="6" y="4" width="4" height="16"></rect><rect x="14" y="4" width="4" height="16"></rect>' : '<polygon points="5 3 19 12 5 21 5 3"></polygon>'}
              </svg>
            </button>` : ''}
          ${task.status === 'running' ? `
            <button class="task-action-btn stop" data-id="${task.id}" title="中断">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                <rect x="6" y="6" width="12" height="12"></rect>
              </svg>
            </button>` : ''}
          <button class="task-action-btn delete" data-id="${task.id}" title="删除">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polyline points="3 6 5 6 21 6"></polyline>
              <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
            </svg>
          </button>
        </div>
      `;

      item.addEventListener('click', (e) => {
        if (e.target.closest('.task-action-btn')) return;
        openTaskDetail(task.id);
      });

      container.appendChild(item);
    });

    container.querySelectorAll('.task-action-btn.stop').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        await fetch(`/api/tasks/${btn.dataset.id}/interrupt`, { method: 'POST' });
        loadTasks();
      });
    });
    container.querySelectorAll('.task-action-btn.toggle-schedule').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        await fetch(`/api/tasks/${btn.dataset.id}/toggle-schedule`, { method: 'POST' });
        loadTasks();
      });
    });
    container.querySelectorAll('.task-action-btn.delete').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (confirm('确定删除此任务记录？')) {
          await fetch(`/api/tasks/${btn.dataset.id}`, { method: 'DELETE' });
          loadTasks();
        }
      });
    });
  } catch (e) {
    console.error('Failed to load tasks:', e);
  }
}

async function openTaskDetail(taskId) {
  // Use global switchView to switch views
  window.switchView?.('task-detail');

  const content = document.getElementById('task-detail-content');
  const title = document.getElementById('task-detail-title');
  content.innerHTML = '<div class="loading-indicator"><div class="spinner"></div><span>加载中...</span></div>';

  try {
    const res = await fetch(`/api/tasks/${taskId}`);
    const data = await res.json();
    const task = data.task;

    title.textContent = task.title;
    const statusIcon = { running: '⏳', completed: '✅', failed: '❌', interrupted: '⏸️', scheduled: '⏰', paused: '⏸️' }[task.status] || '📋';
    const typeBadge = { oneshot: '一次性', scheduled: '⏰ 定时', longrun: '🔬 长期' }[task.task_type] || '一次性';

    let scheduleSection = '';
    if (task.task_type === 'scheduled') {
      scheduleSection = `
      <div class="detail-section">
        <div class="detail-section-title">定时配置</div>
        <div class="detail-content-block">
          <div><strong>Cron:</strong> <code>${task.schedule_cron || '未设置'}</code></div>
          <div><strong>状态:</strong> ${task.schedule_enabled ? '✅ 启用中' : '⏸️ 已暂停'}</div>
          <div><strong>下次执行:</strong> ${task.next_run_at ? formatTime(task.next_run_at) : '—'}</div>
          <div><strong>上次执行:</strong> ${task.last_run_at ? formatTime(task.last_run_at) : '—'}</div>
          <div><strong>累计执行:</strong> ${task.run_count || 0} 次</div>
        </div>
      </div>`;
    }
    if (task.task_type === 'longrun') {
      scheduleSection = `
      <div class="detail-section">
        <div class="detail-section-title">长期任务状态</div>
        <div class="detail-content-block">
          <div><strong>已恢复:</strong> ${task.resume_count || 0} / ${task.max_resume_count || 10} 次</div>
          <div><strong>中断原因:</strong> ${task.interruption_reason === 'max_iterations' ? '⚠️ 循环上限' : task.interruption_reason === 'user' ? '🛑 用户中断' : task.interruption_reason || '—'}</div>
        </div>
      </div>`;
    }

    content.innerHTML = `
      <div class="task-detail-meta">
        <span class="task-meta-chip">${statusIcon} ${task.status}</span>
        <span class="task-meta-chip task-type-badge ${task.task_type}">${typeBadge}</span>
        <span class="task-meta-chip">🕐 ${formatTime(task.created_at)}</span>
        <span class="task-meta-chip">📊 ${(task.steps || []).length} 步</span>
      </div>
      <div class="detail-section">
        <div class="detail-section-title">用户指令</div>
        <div class="detail-content-block">${escapeHtml(task.user_query)}</div>
      </div>
      ${scheduleSection}
      ${task.result_summary ? `
      <div class="detail-section">
        <div class="detail-section-title">执行结果</div>
        <div class="detail-content-block">${escapeHtml(task.result_summary)}</div>
      </div>` : ''}
      ${task.output_files && task.output_files.length > 0 ? `
      <div class="detail-section">
        <div class="detail-section-title">生成文件</div>
        <div>${task.output_files.map(f => `<div class="task-meta-chip">📄 ${escapeHtml(f)}</div>`).join('')}</div>
      </div>` : ''}
      <div class="detail-section">
        <div class="detail-section-title">执行步骤</div>
        <div class="task-detail-steps">
          ${(task.steps || []).map(step => `
            <div class="task-step-card ${step.success ? 'success' : step.success === false ? 'failed' : 'running'}">
              <div class="task-step-header">
                <span>${step.success ? '✅' : step.success === false ? '❌' : '⏳'}</span>
                <span class="task-step-title">${step.step_number}. ${escapeHtml(step.tool_label || step.tool_name)}</span>
              </div>
              ${step.args_preview ? `<div class="task-step-result" style="color:var(--text-secondary);margin-bottom:0.3rem">${escapeHtml(step.args_preview)}</div>` : ''}
              ${step.result_preview ? `<div class="task-step-result">${escapeHtml(step.result_preview)}</div>` : ''}
            </div>
          `).join('')}
        </div>
      </div>
    `;
  } catch (e) {
    content.innerHTML = '<div class="empty-state"><p style="color:var(--error)">加载任务详情失败</p></div>';
  }
}

export function updateTaskBadge() {
  cachedFetch('/api/tasks?status=running').then(data => {
      const badge = document.getElementById('task-count-badge');
      const count = (data.tasks || []).length;
      if (count > 0) {
        badge.textContent = count;
        badge.style.display = 'inline-flex';
      } else {
        badge.style.display = 'none';
      }
    })
    .catch(() => { });
}

// ===================== Schedule Modal =====================

export function initScheduleModal() {
  const scheduleModal = document.getElementById('schedule-modal');

  document.getElementById('create-schedule-btn')?.addEventListener('click', () => {
    scheduleModal?.classList.add('active');
  });
  document.getElementById('schedule-modal-close')?.addEventListener('click', () => {
    scheduleModal?.classList.remove('active');
  });
  document.getElementById('schedule-modal-cancel')?.addEventListener('click', () => {
    scheduleModal?.classList.remove('active');
  });
  scheduleModal?.addEventListener('click', (e) => {
    if (e.target === scheduleModal) scheduleModal.classList.remove('active');
  });

  document.getElementById('schedule-modal-save')?.addEventListener('click', async () => {
    const titleEl = document.getElementById('schedule-title');
    const queryEl = document.getElementById('schedule-query');
    const cronEl = document.getElementById('schedule-cron');
    const statusEl = document.getElementById('schedule-save-status');
    const title = titleEl.value.trim();
    const query = queryEl.value.trim();
    const cron = cronEl.value.trim();
    if (!title || !query || !cron) {
      statusEl.textContent = '⚠️ 请填写所有字段';
      statusEl.style.color = 'var(--error)';
      return;
    }
    try {
      const res = await fetch('/api/tasks/schedule', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, user_query: query, schedule_cron: cron, enabled: true })
      });
      const data = await res.json();
      if (res.ok && data.status === 'success') {
        statusEl.textContent = '✅ 定时任务已创建！';
        statusEl.style.color = 'var(--success)';
        titleEl.value = '';
        queryEl.value = '';
        cronEl.value = '';
        setTimeout(() => {
          scheduleModal.classList.remove('active');
          statusEl.textContent = '';
          loadTasks();
        }, 1000);
      } else {
        statusEl.textContent = '❌ ' + (data.detail || '创建失败');
        statusEl.style.color = 'var(--error)';
      }
    } catch (e) {
      statusEl.textContent = '❌ 网络错误';
      statusEl.style.color = 'var(--error)';
    }
  });
}

// Expose to window for legacy navigation
window.loadTasks = loadTasks;
window.updateTaskBadge = updateTaskBadge;
