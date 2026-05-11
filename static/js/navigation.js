// View navigation system
import { state } from './state.js';
import { switchSession, renameSession, deleteSession } from './sessions.js';

export function switchView(viewId) {
  const views = document.querySelectorAll('.view');
  views.forEach(v => v.classList.remove('active'));
  document.querySelectorAll('.nav-item[data-view]').forEach(n => n.classList.remove('active'));

  const targetView = document.getElementById('view-' + viewId);
  const targetNav = document.querySelector(`.nav-item[data-view="${viewId}"]`);

  if (targetView) targetView.classList.add('active');
  if (targetNav) targetNav.classList.add('active');

  // Load view-specific data — these are attached to window by app.js
  const w = window;
  if (viewId === 'settings-models') { w.loadSettingsConfig?.(); w.loadDownloadHistory?.(); }
  if (viewId === 'settings-skills') w.loadSkillsConfig?.();
  if (viewId === 'settings-mcp') w.loadAgents?.();
  if (viewId === 'settings-plugins') w.loadPluginManager?.();
  if (viewId === 'tasks') w.loadTasks?.();
  if (viewId === 'downloads') w.loadDownloadHistory?.();
}

export function initNavigation() {
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
      return;
    }

    // Session actions
    const sessionItem = e.target.closest('.session-item');
    if (!sessionItem) return;
    const sessionId = parseInt(sessionItem.dataset.sessionId);

    // Session click (switch to this session)
    if (!e.target.closest('.session-rename-btn') && !e.target.closest('.session-delete-btn')) {
      switchSession(sessionId);
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

    // Delete
    if (e.target.closest('.session-delete-btn')) {
      if (confirm('确定删除此会话？')) {
        deleteSession(sessionId);
      }
    }
  });
}
