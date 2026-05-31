import { escapeHtml, showStatus, formatTimeAgo, formatTime } from './utils.js';
import { state } from './state.js';
import { cachedFetch } from './cache.js';

// ===================== Task Step Detail Modal =====================
let _taskStepModal = null;

function ensureStepModal() {
  if (!_taskStepModal) {
    _taskStepModal = document.createElement('div');
    _taskStepModal.className = 'task-step-detail-modal';
    _taskStepModal.innerHTML = `
      <div class="task-step-detail-backdrop"></div>
      <div class="task-step-detail-panel">
        <div class="task-step-detail-header">
          <span class="task-step-detail-title">步骤详情</span>
          <button class="task-step-detail-close">&times;</button>
        </div>
        <div class="task-step-detail-body"></div>
      </div>`;
    document.body.appendChild(_taskStepModal);
    _taskStepModal.querySelector('.task-step-detail-close').addEventListener('click', () => {
      _taskStepModal.classList.remove('active');
    });
    _taskStepModal.querySelector('.task-step-detail-backdrop').addEventListener('click', () => {
      _taskStepModal.classList.remove('active');
    });
  }
  return _taskStepModal;
}

function showTaskStepDetail(step) {
  const modal = ensureStepModal();
  const body = modal.querySelector('.task-step-detail-body');
  const title = modal.querySelector('.task-step-detail-title');
  title.textContent = `步骤 ${step.step_number} 详情`;

  const statusText = step.success ? '✅ 成功' : '❌ 失败';
  body.innerHTML = `
    <div class="detail-section">
      <div class="detail-section-title">工具</div>
      <div><strong>${escapeHtml(step.tool_label || step.tool_name)}</strong></div>
    </div>
    <div class="detail-section">
      <div class="detail-section-title">状态</div>
      <div>${statusText}</div>
    </div>
    ${step.full_args ? `
    <div class="detail-section">
      <div class="detail-section-title">参数</div>
      <div class="detail-content-block"><pre style="white-space:pre-wrap;word-break:break-all;margin:0;font-size:0.8rem">${escapeHtml(step.full_args)}</pre></div>
    </div>` : ''}
    ${step.result_preview ? `
    <div class="detail-section">
      <div class="detail-section-title">结果摘要</div>
      <div class="detail-content-block">${escapeHtml(step.result_preview)}</div>
    </div>` : ''}
    ${step.full_result ? `
    <div class="detail-section">
      <div class="detail-section-title">完整结果</div>
      <div class="detail-content-block"><pre style="white-space:pre-wrap;word-break:break-all;margin:0;font-size:0.8rem;max-height:400px;overflow-y:auto">${escapeHtml(step.full_result)}</pre></div>
    </div>` : ''}
    ${step.thinking_content ? `
    <div class="detail-section">
      <div class="detail-section-title">思考过程</div>
      <div class="detail-content-block">${escapeHtml(step.thinking_content)}</div>
    </div>` : ''}
  `;
  _taskStepModal.classList.add('active');
}

export function initTaskFilters() {
  document.querySelectorAll('.filter-pill').forEach(pill => {
    pill.addEventListener('click', () => {
      document.querySelectorAll('.filter-pill').forEach(p => p.classList.remove('active'));
      pill.classList.add('active');
      state.taskFilter = pill.dataset.filter;
      loadTasks(true);
    });
  });
  document.getElementById('task-search-input')?.addEventListener('input', (e) => {
    state.taskSearchQuery = e.target.value.trim();
    clearTimeout(state.taskRefreshInterval);
    state.taskRefreshInterval = setTimeout(() => loadTasks(true), 300);
  });
  document.getElementById('task-detail-back')?.addEventListener('click', () => {
    switchView('tasks');
  });

  // ── Sub-tab switching: Task list vs Process management ──
  document.querySelectorAll('.sub-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.sub-tab').forEach(t => {
        t.style.color = 'var(--text-secondary)';
        t.style.borderBottomColor = 'transparent';
      });
      tab.style.color = 'var(--text-primary)';
      tab.style.borderBottomColor = 'var(--accent-color)';
      const subview = tab.dataset.subview;
      const toolbar = document.getElementById('task-view-toolbar');
      const taskList = document.getElementById('task-list-container');
      const processes = document.getElementById('processes-container');
      if (subview === 'processes') {
        if (toolbar) toolbar.style.display = 'none';
        if (taskList) taskList.style.display = 'none';
        if (processes) processes.style.display = 'block';
        loadProcessList();
      } else {
        if (toolbar) toolbar.style.display = 'flex';
        if (taskList) taskList.style.display = 'block';
        if (processes) processes.style.display = 'none';
      }
    });
  });

  document.getElementById('refresh-processes-btn')?.addEventListener('click', loadProcessList);
}

export async function loadTasks(resetPage = false) {
  const container = document.getElementById('task-list-container');
  if (!container) return;
  window._perf.loadTasksStart = performance.now();

  try {
    if (resetPage) state.taskPage = 1;

    let url = '/api/tasks';
    const params = [];
    if (state.taskFilter !== 'all') params.push(`status=${state.taskFilter}`);
    if (state.taskSearchQuery) params.push(`q=${encodeURIComponent(state.taskSearchQuery)}`);
    params.push(`page=${state.taskPage}`);
    params.push(`page_size=50`);
    if (params.length > 0) url += '?' + params.join('&');

    const data = await cachedFetch(url);
    const fetchDonePerf = performance.now();
    if (data._dbg) {
      const perfStart = window._perf.start;
      const dateStart = window._perf.dateStart;
      const clientSentMs = window._perf.loadTasksStart
        ? (dateStart + (window._perf.loadTasksStart - perfStart))
        : 0;
      console.log('[DBG]', JSON.stringify(data._dbg),
        '| client_sent_at:', Math.round(clientSentMs),
        '| client_recv_at:', Math.round(fetchDonePerf + dateStart),
        '| rtt:', Math.round(fetchDonePerf - (window._perf.loadTasksStart || 0)), 'ms');
    }
    state.totalTaskCount = data.total_count || 0;

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
      const statusIcon = { running: '⏳', completed: '✅', failed: '❌', interrupted: '⏸️', scheduled: '⏰', paused: '⏸️', detached: '🟢', stuck: '🔴' }[task.status] || '📋';
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
            ${task.session_id ? `<span class="task-meta-chip" style="font-size:0.75rem">会话 #${task.session_id}${task.session_name ? ' · ' + escapeHtml(task.session_name) : ''}</span>` : ''}
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
          ${task.status === 'running' || task.status === 'detached' ? `
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

    // Pagination controls
    const totalPages = Math.ceil(state.totalTaskCount / 50);
    if (totalPages > 1) {
      const pagination = document.createElement('div');
      pagination.className = 'task-pagination';
      pagination.innerHTML = `
        <button class="pagination-btn" data-page="${state.taskPage - 1}" ${state.taskPage <= 1 ? 'disabled' : ''}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="15 18 9 12 15 6"></polyline></svg>
          上一页
        </button>
        <span class="pagination-info">${state.taskPage} / ${totalPages} (共 ${state.totalTaskCount} 条)</span>
        <button class="pagination-btn" data-page="${state.taskPage + 1}" ${state.taskPage >= totalPages ? 'disabled' : ''}>
          下一页
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="9 18 15 12 9 6"></polyline></svg>
        </button>
      `;
      container.appendChild(pagination);

      pagination.querySelectorAll('.pagination-btn:not([disabled])').forEach(btn => {
        btn.addEventListener('click', () => {
          state.taskPage = parseInt(btn.dataset.page);
          loadTasks();
          container.scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
      });
    }
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

  // Step pagination state
  const stepState = { page: 1, pageSize: 50, totalPages: 1, total: 0 };

  try {
    const res = await fetch(`/api/tasks/${taskId}`);
    const data = await res.json();
    const task = data.task;

    title.textContent = task.title;
    const statusIcon = { running: '⏳', completed: '✅', failed: '❌', interrupted: '⏸️', scheduled: '⏰', paused: '⏸️', detached: '🟢', stuck: '🔴' }[task.status] || '📋';
    const typeBadge = { oneshot: '一次性', scheduled: '⏰ 定时', longrun: '🔬 长期' }[task.task_type] || '一次性';

    // Token display
    const tokenInfo = (task.total_tokens || task.total_cost)
      ? `<span class="task-meta-chip">🔤 ${task.total_tokens || 0} tokens${task.total_cost ? ` ($${Number(task.total_cost).toFixed(4)})` : ''}</span>`
      : '';

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

    // Store current task data for resume
    window._currentTaskDetail = task;

    // Render main layout (steps loaded lazily)
    content.innerHTML = `
      <div class="task-detail-meta">
        <span class="task-meta-chip">${statusIcon} ${task.status}</span>
        <span class="task-meta-chip task-type-badge ${task.task_type}">${typeBadge}</span>
        <span class="task-meta-chip">🕐 ${formatTime(task.created_at)}</span>
        <span class="task-meta-chip">📊 <span class="task-step-count-label">${task.steps ? task.steps.length : '...'}</span> 步</span>
        ${tokenInfo}
        ${task.session_id ? `<span class="task-meta-chip">💬 会话 #${task.session_id}${task.session_name ? ' · ' + escapeHtml(task.session_name) : ''}</span>` : ''}
      </div>
      <div class="detail-section">
        <div class="detail-section-title">任务目标</div>
        <div class="detail-content-block">${escapeHtml(task.user_query)}</div>
      </div>
      ${scheduleSection}
      ${task.status === 'interrupted' || task.status === 'failed' || task.status === 'background_failed' ? `
      <div class="detail-section" style="display:flex;align-items:center;gap:1rem">
        <button class="btn-resume-task-detail" data-task-id="${task.id}">▶ 继续执行</button>
        <span style="font-size:0.8rem;color:var(--text-secondary)">${task.status === 'failed' ? '❌ 错误: ' : '中断原因: '}${task.interruption_reason === 'server_restart' ? '🔌 服务器重启' : task.interruption_reason === 'user' ? '🛑 用户中断' : task.interruption_reason === 'max_iterations' ? '⚠️ 循环上限' : task.interruption_reason === 'error' ? '⚠️ 执行出错' : task.interruption_reason === 'process_lost' ? '🔌 进程丢失（服务重启）' : task.interruption_reason || '未知'}</span>
      </div>` : ''}
      ${task.result_summary ? `
      <div class="detail-section">
        <div class="detail-section-title">执行结果</div>
        <div class="detail-content-block">${escapeHtml(task.result_summary)}</div>
      </div>` : ''}
      <div id="process-info-card" class="detail-section" style="display:none;">
        <div class="detail-section-title">🟢 进程管理</div>
        <div id="process-info-content" style="font-size:0.85rem;"></div>
      </div>
      ${task.output_files && task.output_files.length > 0 ? `
      <div class="detail-section">
        <div class="detail-section-title">生成文件</div>
        <div>${task.output_files.map(f => `<div class="task-meta-chip">📄 ${escapeHtml(f)}</div>`).join('')}</div>
      </div>` : ''}
      <div class="detail-section">
        <div class="detail-section-title" style="display:flex;justify-content:space-between;align-items:center">
          <span>执行步骤</span>
          <span class="task-step-pagination-info" style="font-size:0.75rem;color:var(--text-secondary)"></span>
        </div>
        <div class="task-detail-steps">
          <div class="loading-indicator" style="padding:1rem"><div class="spinner"></div><span>加载步骤...</span></div>
        </div>
        <div class="task-step-pagination" style="display:flex;justify-content:center;align-items:center;gap:0.5rem;padding:0.5rem 0">
          <button class="pagination-btn step-page-prev" disabled>◀ 上一页</button>
          <span class="step-page-info" style="font-size:0.8rem;color:var(--text-secondary)"></span>
          <button class="pagination-btn step-page-next" disabled>下一页 ▶</button>
        </div>
      </div>
    `;

    // Load steps with pagination
    async function loadStepPage(page) {
      const container = content.querySelector('.task-detail-steps');
      const paginationInfo = content.querySelector('.task-step-pagination-info');
      const pageInfo = content.querySelector('.step-page-info');
      const prevBtn = content.querySelector('.step-page-prev');
      const nextBtn = content.querySelector('.step-page-next');
      const countLabel = content.querySelector('.task-step-count-label');

      container.innerHTML = '<div class="loading-indicator" style="padding:1rem"><div class="spinner"></div><span>加载步骤...</span></div>';

      try {
        const resp = await fetch(`/api/tasks/${taskId}/steps?page=${page}&page_size=${stepState.pageSize}`);
        const stepData = await resp.json();
        const steps = stepData.steps || [];
        stepState.page = stepData.page || page;
        stepState.totalPages = stepData.total_pages || 1;
        stepState.total = stepData.total || 0;

        if (countLabel) countLabel.textContent = stepState.total;

        if (steps.length === 0) {
          container.innerHTML = '<div style="padding:1rem;color:var(--text-secondary);text-align:center">暂无步骤</div>';
          return;
        }

        container.innerHTML = steps.map(step => `
          <div class="task-step-card ${step.success ? 'success' : step.success === false ? 'failed' : 'running'}" data-step-number="${step.step_number}">
            <div class="task-step-header">
              <span>${step.success ? '✅' : step.success === false ? '❌' : '⏳'}</span>
              <span class="task-step-title">${step.step_number}. ${escapeHtml(step.tool_label || step.tool_name)}</span>
              <span class="task-step-expand-hint" style="margin-left:auto;font-size:0.7rem;color:var(--text-secondary)">点击查看详情 ▸</span>
            </div>
            ${step.args_preview ? `<div class="task-step-result" style="color:var(--text-secondary);margin-bottom:0.3rem">${escapeHtml(step.args_preview)}</div>` : ''}
            ${step.result_preview ? `<div class="task-step-result">${escapeHtml(step.result_preview.substring(0, 300))}${step.result_preview.length > 300 ? '...' : ''}</div>` : ''}
          </div>
        `).join('');

        // Click to show full detail modal
        container.querySelectorAll('.task-step-card').forEach(card => {
          card.addEventListener('click', () => {
            const num = parseInt(card.dataset.stepNumber);
            const step = steps.find(s => s.step_number === num);
            if (step) showTaskStepDetail(step);
          });
        });

        // Update pagination
        const rangeStart = (stepState.page - 1) * stepState.pageSize + 1;
        const rangeEnd = Math.min(stepState.page * stepState.pageSize, stepState.total);
        paginationInfo.textContent = `(${rangeStart}-${rangeEnd} / ${stepState.total})`;
        pageInfo.textContent = `${stepState.page} / ${stepState.totalPages}`;
        prevBtn.disabled = stepState.page <= 1;
        nextBtn.disabled = stepState.page >= stepState.totalPages;
        prevBtn.dataset.page = stepState.page - 1;
        nextBtn.dataset.page = stepState.page + 1;
      } catch (e) {
        container.innerHTML = '<div style="padding:1rem;color:var(--error);text-align:center">加载步骤失败</div>';
      }
    }

    // Wire pagination buttons
    content.querySelector('.step-page-prev').addEventListener('click', () => {
      const p = parseInt(content.querySelector('.step-page-prev').dataset.page);
      if (p >= 1) loadStepPage(p);
    });
    content.querySelector('.step-page-next').addEventListener('click', () => {
      const p = parseInt(content.querySelector('.step-page-next').dataset.page);
      if (p <= stepState.totalPages) loadStepPage(p);
    });

    // Load first page
    loadStepPage(1);

    // Load process info (for running/detached tasks)
    if (task.status === 'running' || task.status === 'detached') {
      loadProcessInfo(taskId, content);
    }

    // Wire up resume button
    content.querySelector('.btn-resume-task-detail')?.addEventListener('click', function() {
      const tid = parseInt(this.dataset.taskId);
      if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        state.ws.send(JSON.stringify({ type: 'resume', task_id: tid }));
        this.textContent = '⏳ 已发送恢复请求...';
        this.disabled = true;
        window.switchView?.('chat');
      } else {
        alert('WebSocket 未连接，请刷新页面后重试');
      }
    });

    // Auto-refresh if task is running
    let refreshInterval;
    if (task.status === 'running') {
      refreshInterval = setInterval(() => {
        loadStepPage(stepState.page);
        // Also refresh task status
        fetch(`/api/tasks/${taskId}`).then(r => r.json()).then(d => {
          if (d.task && d.task.status !== 'running') {
            clearInterval(refreshInterval);
            // Update status chip
            const statusChip = content.querySelector('.task-detail-meta .task-meta-chip:first-child');
            if (statusChip) {
              const newIcon = { completed: '✅', failed: '❌', interrupted: '⏸️' }[d.task.status] || '📋';
              statusChip.textContent = `${newIcon} ${d.task.status}`;
            }
          }
        }).catch(() => {});
      }, 5000);
    }

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

// ── Process List (Processes sub-tab) ──

async function loadProcessList() {
  const container = document.getElementById('process-list');
  const countLabel = document.getElementById('process-count-label');
  if (!container) return;
  try {
    const resp = await fetch('/api/processes');
    const data = await resp.json();
    const procs = data.processes || {};
    const orphans = data.orphans || {};
    const allProcs = { ...procs, ...orphans };
    const entries = Object.entries(allProcs).filter(([, info]) => info.alive);

    if (countLabel) countLabel.textContent = `${entries.length} 个进程`;

    if (entries.length === 0) {
      container.innerHTML = `
        <div class="empty-state">
          <p>暂无活跃进程</p>
          <small>长时间运行的 shell 命令（如服务器、守护进程）会显示在此</small>
        </div>`;
      return;
    }

    container.innerHTML = entries.map(([id, info]) => {
      const pid = info.pid || '?';
      const uptime = info.uptime ? Math.floor(info.uptime / 60) : 0;
      const cmd = (info.command || '(unknown)').substring(0, 150);
      const outFile = info.output_file || '';
      return `
        <div class="task-item" style="border-left:3px solid #22c55e;">
          <div class="task-item-icon running">🟢</div>
          <div class="task-item-body">
            <div class="task-item-title"><span style="font-family:monospace;">PID ${escapeHtml(String(pid))}</span> · ${escapeHtml(cmd)}</div>
            <div class="task-item-meta">
              <span>⏱ ${uptime} 分钟</span>
              ${outFile ? `<span style="font-size:0.75rem;color:var(--text-secondary)">📄 ${escapeHtml(outFile)}</span>` : ''}
            </div>
            ${outFile ? `<div class="process-log-preview" style="margin-top:0.3rem;font-size:0.75rem;color:var(--text-secondary);background:var(--bg-terminal, #1a1b26);padding:0.3rem 0.5rem;border-radius:4px;max-height:60px;overflow:hidden;font-family:monospace;" id="log-preview-${id}">加载中...</div>` : ''}
          </div>
          <div class="task-item-actions" style="display:flex;flex-direction:column;gap:0.3rem;">
            <button class="task-action-btn kill-process-btn" data-pid="${pid}" data-task-id="${id}" title="终止进程" style="color:var(--error-color, #ef4444);">
              <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                <rect x="6" y="6" width="12" height="12"></rect>
              </svg>
            </button>
            ${outFile ? `<button class="view-log-btn" data-outfile="${escapeHtml(outFile)}" title="查看日志" style="background:none;border:none;cursor:pointer;color:var(--text-secondary);font-size:1rem;">📋</button>` : ''}
          </div>
        </div>`;
    }).join('');

    // Load log previews for each process
    entries.forEach(([id, info]) => {
      const outFile = info.output_file || '';
      if (outFile) {
        fetchLogPreview(id, outFile);
      }
    });

    // Wire kill buttons
    container.querySelectorAll('.kill-process-btn').forEach(btn => {
      btn.addEventListener('click', async () => {
        const taskId = btn.dataset.taskId;
        const pid = btn.dataset.pid;
        if (!confirm(`确定要终止进程 PID ${pid} 吗？`)) return;
        // Try killing by task_id first, then by orphan_id pattern
        try {
          const resp = await fetch(`/api/tasks/${taskId}/kill`, { method: 'POST' });
          const data = await resp.json();
          alert(data.message || '进程已终止');
        } catch (e) {
          alert('终止失败: ' + e.message);
        }
        loadProcessList();
      });
    });

    // Wire log view buttons
    container.querySelectorAll('.view-log-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        const outFile = btn.dataset.outfile;
        showProcessLogModal(outFile);
      });
    });

  } catch (e) {
    container.innerHTML = `<div style="padding:1rem;color:var(--error);text-align:center;">加载进程列表失败: ${e.message}</div>`;
  }
}

async function fetchLogPreview(id, outFile) {
  const previewEl = document.getElementById(`log-preview-${id}`);
  if (!previewEl) return;
  try {
    const resp = await fetch(`/api/tasks/${id}/logs?lines=5`);
    if (!resp.ok) {
      // Try orphan: use file-based approach
      previewEl.textContent = '(日志文件不可用)';
      return;
    }
    const data = await resp.json();
    const lines = (data.lines || []);
    previewEl.textContent = lines.join('\n') || '(空)';
  } catch (e) {
    previewEl.textContent = '(日志加载失败)';
  }
}

function showProcessLogModal(outFile) {
  // Use a simple modal to display full logs
  const modal = document.createElement('div');
  modal.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;z-index:9999;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,0.5);';
  modal.innerHTML = `
    <div style="background:var(--bg-primary,#fff);border-radius:8px;width:80%;max-width:800px;max-height:80%;display:flex;flex-direction:column;">
      <div style="display:flex;justify-content:space-between;align-items:center;padding:0.8rem 1rem;border-bottom:1px solid var(--border-color);">
        <span style="font-weight:600;">进程日志</span>
        <button id="log-modal-close" style="background:none;border:none;cursor:pointer;font-size:1.2rem;">&times;</button>
      </div>
      <pre id="log-modal-content" style="flex:1;overflow-y:auto;padding:1rem;margin:0;font-size:0.8rem;background:var(--bg-terminal,#1a1b26);color:#c0caf5;white-space:pre-wrap;word-break:break-all;font-family:monospace;">加载中...</pre>
    </div>`;
  document.body.appendChild(modal);
  modal.querySelector('#log-modal-close').addEventListener('click', () => modal.remove());
  modal.addEventListener('click', (e) => { if (e.target === modal) modal.remove(); });

  // Fetch full log
  fetch(outFile).then(r => r.text()).then(text => {
    modal.querySelector('#log-modal-content').textContent = text || '(空)';
  }).catch(() => {
    modal.querySelector('#log-modal-content').textContent = '(无法读取日志文件)';
  });
}

// ── Process Management (Task Detail) ──

async function loadProcessInfo(taskId, content) {
  const card = content.querySelector('#process-info-card');
  const infoEl = content.querySelector('#process-info-content');
  if (!card || !infoEl) return;

  async function refresh() {
    try {
      const resp = await fetch(`/api/tasks/${taskId}/process`);
      if (!resp.ok) {
        card.style.display = 'none';
        return;
      }
      const data = await resp.json();
      card.style.display = 'block';

      const alive = data.alive;
      const uptime = data.uptime ? Math.floor(data.uptime / 60) : 0;
      const pid = data.pid;
      const cmd = (data.command || '').substring(0, 120);

      infoEl.innerHTML = `
        <div style="display:flex;align-items:center;gap:1rem;flex-wrap:wrap;">
          <span style="display:inline-flex;align-items:center;gap:0.3rem;">
            <span style="width:8px;height:8px;border-radius:50%;background:${alive ? '#22c55e' : '#999'};display:inline-block;"></span>
            ${alive ? '运行中' : '已停止'}
          </span>
          <span>PID: ${pid}</span>
          <span>已运行: ${uptime} 分钟</span>
          <span style="font-family:monospace;font-size:0.8rem;color:var(--text-secondary);max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;" title="${escapeHtml(data.command || '')}">${escapeHtml(cmd)}</span>
        </div>
        ${alive ? `
        <div style="margin-top:0.5rem;">
          <button class="btn-kill-process" style="padding:0.3rem 0.8rem;background:var(--error-color, #ef4444);color:white;border:none;border-radius:4px;cursor:pointer;font-size:0.8rem;">⏹ 停止进程</button>
          <button class="btn-view-logs" style="margin-left:0.5rem;padding:0.3rem 0.8rem;background:var(--accent-color, #3b82f6);color:white;border:none;border-radius:4px;cursor:pointer;font-size:0.8rem;">📋 查看日志</button>
        </div>
        <div id="process-logs-container" style="display:none;margin-top:0.5rem;">
          <div style="font-size:0.75rem;color:var(--text-secondary);margin-bottom:0.3rem;">实时日志（每 3 秒刷新）</div>
          <pre id="process-logs-viewer" style="background:var(--bg-terminal, #1a1b26);color:#c0caf5;padding:0.5rem;border-radius:4px;font-size:0.75rem;max-height:200px;overflow-y:auto;white-space:pre-wrap;word-break:break-all;margin:0;"></pre>
        </div>` : '<div style="margin-top:0.3rem;font-size:0.8rem;color:var(--text-secondary)">进程已终止，信息保留以供查阅</div>'}
      `;

      // Bind kill button
      infoEl.querySelector('.btn-kill-process')?.addEventListener('click', async () => {
        if (!confirm(`确定要终止进程 PID ${pid} 吗？`)) return;
        try {
          const kr = await fetch(`/api/tasks/${taskId}/kill`, { method: 'POST' });
          const kd = await kr.json();
          alert(kd.message || '进程已终止');
          refresh();
        } catch (e) {
          alert('终止失败: ' + e.message);
        }
      });

      // Bind logs toggle
      infoEl.querySelector('.btn-view-logs')?.addEventListener('click', async () => {
        const container = infoEl.querySelector('#process-logs-container');
        const viewer = infoEl.querySelector('#process-logs-viewer');
        if (container.style.display !== 'none') {
          container.style.display = 'none';
          return;
        }
        container.style.display = 'block';
        // Initial load
        await fetchLogs(taskId, viewer);
        // Auto-refresh every 3s
        if (window._logRefreshInterval) clearInterval(window._logRefreshInterval);
        window._logRefreshInterval = setInterval(() => {
          if (container.style.display !== 'none') fetchLogs(taskId, viewer);
          else clearInterval(window._logRefreshInterval);
        }, 3000);
      });
    } catch (e) {
      card.style.display = 'none';
    }
  }

  await refresh();
}

async function fetchLogs(taskId, viewerEl) {
  try {
    const resp = await fetch(`/api/tasks/${taskId}/logs?lines=50`);
    if (!resp.ok) return;
    const data = await resp.json();
    viewerEl.textContent = (data.lines || []).join('\n');
    viewerEl.scrollTop = viewerEl.scrollHeight;
  } catch (e) {
    // silent
  }
}

// Expose to window for legacy navigation
window.loadTasks = loadTasks;
window.updateTaskBadge = updateTaskBadge;
