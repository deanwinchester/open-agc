// View navigation system (History API routing)
import { state } from './state.js';
import { switchSession, renameSession, deleteSession, clearSession } from './sessions.js';

// View change listeners — subscribe instead of monkey-patching window.switchView
const _viewListeners = [];
export function onViewChange(fn) {
  _viewListeners.push(fn);
}

// Internal: DOM switch only (no history.pushState)
function _activateView(viewId) {
  // Clean up auto-refresh intervals and modals when leaving any view
  if (window._taskDetailRefresh) { clearInterval(window._taskDetailRefresh); window._taskDetailRefresh = null; }
  if (window._stepAR) { clearInterval(window._stepAR); window._stepAR = null; }
  // Close any open modals
  document.querySelectorAll('.modal-overlay').forEach(function(m) { m.style.display = 'none'; });

  const views = document.querySelectorAll('.view');
  views.forEach(v => v.classList.remove('active'));
  document.querySelectorAll('.nav-item[data-view]').forEach(n => n.classList.remove('active'));
  // Clear session item highlight when switching views
  document.querySelectorAll('.session-item.active').forEach(s => s.classList.remove('active'));

  const targetView = document.getElementById('view-' + viewId);
  const targetNav = document.querySelector(`.nav-item[data-view="${viewId}"]`);

  if (targetView) targetView.classList.add('active');
  if (targetNav) targetNav.classList.add('active');

  // Clear chat unread badge when entering chat
  if (viewId === 'chat') {
    const badge = document.getElementById('chat-unread-badge');
    if (badge) { badge.textContent = '0'; badge.style.display = 'none'; }
  }

  // Load view-specific data — these are attached to window by app.js
  const w = window;
  if (viewId === 'settings-models') { w.loadSettingsConfig?.(); w.loadDownloadHistory?.(); w.refreshSearXNGStatus?.(); }
  if (viewId === 'settings-skills') w.loadSkillsConfig?.();
  if (viewId === 'settings-mcp') w.loadAgents?.();
  if (viewId === 'settings-plugins') w.loadPluginManager?.();
  if (viewId === 'tasks') w.loadTasks?.();
  if (viewId === 'goals') w.loadGoals?.();
  if (viewId === 'downloads') w.loadDownloadHistory?.();
  if (viewId === 'logs') w.loadLogs?.();
  if (viewId === 'debug') w.loadLogs?.();

  // Notify view change listeners (subscribed via onViewChange instead of monkey-patching)
  for (var i = 0; i < _viewListeners.length; i++) {
    try { _viewListeners[i](viewId); } catch (e) { console.error('view listener error:', e); }
  }
}

// Public: switch to a view, updating browser history
// Optionally pass extra URL path segments, e.g. switchView('task-detail', '/178')
export function switchView(viewId, extraPath) {
  // For chat view, include session_id in URL
  let pathExtra = extraPath;
  if (viewId === 'chat' && !extraPath && state?.currentSessionId) {
    pathExtra = '/' + state.currentSessionId;
  }
  const url = '/' + viewId + (pathExtra || '');
  if (window.location.pathname !== url) {
    history.pushState({view: viewId, extraPath: pathExtra}, '', url);
  }
  _activateView(viewId);
}

// Read view and parameters from current URL path
function _viewFromPath() {
  const path = window.location.pathname;
  const segments = path.replace(/^\//, '').split('/');
  const viewId = segments[0];
  const param = segments[1] || '';
  // Map known views; fallback to chat for root or unknown paths
  const known = ['chat', 'tasks', 'task-detail', 'goals', 'settings-models', 'settings-skills', 'settings-mcp',
                 'downloads', 'settings-plugins', 'logs', 'debug'];
  // Redirect old /logs to /debug
  if (viewId === 'logs') return 'debug';
  // If view is task-detail, check for extra path segment (task ID)
  if (viewId === 'task-detail' && param) {
    const taskId = parseInt(param);
    if (taskId > 0) {
      window._pendingTaskDetail = taskId;
    }
  }
  // If view is chat, check for session ID in URL
  if (viewId === 'chat' && param) {
    const sessionId = parseInt(param);
    if (sessionId > 0 && sessionId !== state.currentSessionId) {
      window._pendingSessionId = sessionId;
    }
  }
  return known.includes(viewId) ? viewId : 'chat';
}

// Browser back/forward
window.addEventListener('popstate', (e) => {
  const viewId = e.state?.view || _viewFromPath();
  _activateView(viewId);
  // For task-detail, reload the detail data from the URL
  if (viewId === 'task-detail') {
    const path = window.location.pathname;
    const match = path.match(/\/task-detail\/(\d+)/);
    if (match) {
      const taskId = parseInt(match[1]);
      if (typeof window.openTaskDetail === 'function') {
        window.openTaskDetail(taskId);
      }
    }
  }
});

export function initNavigation() {
  // Set initial view from URL on page load
  const initialView = _viewFromPath();
  _activateView(initialView);
  // Sync URL if at root (no path)
  if (window.location.pathname === '/' || window.location.pathname === '') {
    history.replaceState({view: 'chat'}, '', '/chat');
  }

  document.querySelector('.sidebar-content')?.addEventListener('click', (e) => {
    // Collapsible section header (plugin menu sections)
    const sectionHeader = e.target.closest('.sidebar-section-header.collapsible');
    if (sectionHeader) {
      const section = sectionHeader.closest('.sidebar-section');
      const subnav = section?.querySelector('.sidebar-subnav');
      if (subnav) {
        const isCollapsed = subnav.style.display === 'none' || subnav.style.display === '';
        subnav.style.display = isCollapsed ? 'block' : 'none';
        sectionHeader.classList.toggle('collapsed');
        const icon = sectionHeader.querySelector('.section-toggle-icon');
        if (icon) icon.textContent = isCollapsed ? '▼' : '▶';
      }
      return;
    }

    // Nav item click — prefer window.switchView (may be hooked by plugins)
    const navItem = e.target.closest('.nav-item[data-view]');
    if (navItem) {
      (window.switchView || switchView)(navItem.dataset.view);
      closeSidebar();
      return;
    }

    // Session actions
    const sessionItem = e.target.closest('.session-item');
    if (!sessionItem) return;
    const sessionId = parseInt(sessionItem.dataset.sessionId);

    // Session click (switch to this session)
    if (!e.target.closest('.session-rename-btn') && !e.target.closest('.session-delete-btn') && !e.target.closest('.session-clear-btn')) {
      switchSession(sessionId);
      closeSidebar();
      return;
    }

    // Rename
    if (e.target.closest('.session-rename-btn')) {
      const oldName = state.sessions.find(s => s.id === sessionId)?.name || '';
      const newName = prompt('重命名会话:', oldName);
      if (newName && newName !== oldName) {
        renameSession(sessionId, newName);
      }
      return;
    }

    // Delete (non-default sessions)
    if (e.target.closest('.session-delete-btn')) {
      if (confirm('确定删除此会话？')) {
        deleteSession(sessionId);
      }
      return;
    }

    // Clear data (default session only)
    if (e.target.closest('.session-clear-btn')) {
      if (confirm('确定清空默认会话的所有数据？（会话本身将保留）')) {
        clearSession(sessionId);
      }
    }
  });
}

// Mobile: toggle sidebar open/close
export function toggleSidebar() {
  const sidebar = document.querySelector('.sidebar');
  const overlay = document.getElementById('sidebar-overlay');
  if (!sidebar) return;
  const isOpen = sidebar.classList.toggle('open');
  if (overlay) overlay.classList.toggle('open', isOpen);
}

// Mobile: close sidebar
export function closeSidebar() {
  const sidebar = document.querySelector('.sidebar');
  const overlay = document.getElementById('sidebar-overlay');
  if (!sidebar) return;
  sidebar.classList.remove('open');
  if (overlay) overlay.classList.remove('open');
}
