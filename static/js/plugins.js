import { escapeHtml, showStatus } from './utils.js';
import { cachedFetch } from './cache.js';

export async function loadPlugins() {
  try {
    const data = await cachedFetch('/api/plugins');
    if (!data || data.status === 'error') return;
    const plugins = data.plugins || [];
    for (const p of plugins) {
      const cssLink = document.createElement('link');
      cssLink.rel = 'stylesheet';
      cssLink.href = `/static/plugins/${p.name}/plugin.css`;
      document.head.appendChild(cssLink);
      try {
        const script = document.createElement('script');
        script.src = `/static/plugins/${p.name}/plugin.js`;
        await new Promise((resolve, reject) => {
          script.onload = resolve;
          script.onerror = () => { script.remove(); resolve(); };
          document.head.appendChild(script);
        });
      } catch (e) { /* JS optional */ }
      if (p.menu && p.menu.section) {
        renderPluginMenu(p);
      }
    }
  } catch (e) { /* no plugins installed */ }
}

function renderPluginMenu(plugin) {
  const menu = plugin.menu;
  const sidebar = document.querySelector('.sidebar-content');
  if (!sidebar) return;
  let section = sidebar.querySelector(`[data-plugin="${plugin.name}"]`);
  if (!section) {
    const label = menu.label || plugin.name;
    section = document.createElement('div');
    section.className = 'sidebar-section';
    section.dataset.plugin = plugin.name;
    section.innerHTML = `<div class="sidebar-section-header collapsible collapsed" data-section="${menu.section}">
      <div class="section-icon-title">
        <span class="section-toggle-icon">▶</span>
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="flex-shrink:0;">
          <path d="M12 2L2 7l10 5 10-5-10-5z"></path>
          <path d="M2 17l10 5 10-5"></path>
          <path d="M2 12l10 5 10-5"></path>
        </svg>
        <span>${label}</span>
      </div>
    </div><div class="sidebar-subnav" style="display:none;"></div>`;
    sidebar.appendChild(section);
  }
  const body = section.querySelector('.sidebar-subnav');
  if (!body || !menu.views) return;
  body.innerHTML = menu.views.map(v =>
    `<nav class="nav-item" data-view="${v.id}"><span class="nav-dot"></span><span>${v.label}</span></nav>`
  ).join('');
}

export async function loadPluginManager() {
  document.querySelectorAll('.plugin-tab').forEach(tab => {
    tab.addEventListener('click', () => {
      document.querySelectorAll('.plugin-tab').forEach(t => {
        t.classList.remove('active');
        t.style.color = 'var(--text-secondary)';
        t.style.borderBottomColor = 'transparent';
      });
      tab.classList.add('active');
      tab.style.color = 'var(--theme-color)';
      tab.style.borderBottomColor = 'var(--theme-color)';
      document.getElementById('plugin-tab-installed').style.display = tab.dataset.tab === 'installed' ? '' : 'none';
      document.getElementById('plugin-tab-marketplace').style.display = tab.dataset.tab === 'marketplace' ? '' : 'none';
      document.getElementById('plugin-tab-install').style.display = tab.dataset.tab === 'install' ? '' : 'none';
      if (tab.dataset.tab === 'marketplace') loadMarketplace();
    });
  });

  const container = document.getElementById('plugin-list-container');
  if (!container) return;
  try {
    const data = await cachedFetch('/api/plugins');
    if (!data) throw new Error('no data');
    const plugins = data.plugins || [];
    document.getElementById('plugin-dir-path').textContent = data.plugins_dir || '--';
    const el = document.getElementById('plugin-dir-path2'); if (el) el.textContent = (data.plugins_dir || 'plugins/');
    if (!plugins.length) {
      container.innerHTML = '<div class="empty-state"><p>暂无安装的插件</p><small>将插件文件夹放入 plugins/ 目录后点击"扫描新插件"</small></div>';
    } else {
      container.innerHTML = plugins.map(p => {
        const statusBadge = p.loaded
          ? (p.enabled ? '<span style="color:var(--success);font-size:0.7rem;">● 已启用</span>'
            : '<span style="color:#9ca3af;font-size:0.7rem;">● 已禁用</span>')
          : '<span style="color:#f59e0b;font-size:0.7rem;">○ 未加载</span>';
        return `<div class="config-item" style="margin-bottom:0.4rem;">
          <div class="config-item-body">
            <div class="config-item-title">📦 ${escapeHtml(p.name)} <span style="font-size:0.7rem;color:var(--text-secondary);">v${p.version}</span> ${statusBadge}</div>
            <div class="config-item-meta">${escapeHtml(p.description)}${p.author ? ' · ' + escapeHtml(p.author) : ''}</div>
          </div>
          <div style="display:flex; gap:0.3rem;">
            ${p.loaded ? `<button class="download-action-btn plugin-toggle-btn" data-name="${p.name}" data-enabled="${p.enabled}" title="${p.enabled ? '禁用' : '启用'}" style="font-size:0.7rem;">${p.enabled ? '⏸' : '▶'}</button>` : ''}
            <button class="download-action-btn delete plugin-delete-btn" data-name="${p.name}" title="卸载">✕</button>
          </div>
        </div>`;
      }).join('');
    }
    container.querySelectorAll('.plugin-toggle-btn').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        try {
          const tres = await fetch('/api/plugins/' + btn.dataset.name + '/toggle', { method: 'POST' });
          const td = await tres.json();
          showStatus('✅ 插件已' + (td.enabled ? '启用' : '禁用'), 'success');
          loadPluginManager();
        } catch (e) { showStatus('❌ 操作失败', 'error'); }
      });
    });
    container.querySelectorAll('.plugin-delete-btn').forEach(btn => {
      btn.addEventListener('click', async (e) => {
        e.stopPropagation();
        if (!confirm('确定卸载插件 "' + btn.dataset.name + '"? 此操作将删除插件目录。')) return;
        try {
          const delRes = await fetch('/api/plugins/' + btn.dataset.name, { method: 'DELETE' });
          const delData = await delRes.json();
          showStatus(delData.status === 'ok' ? '✅ ' + delData.message : '❌ ' + (delData.detail || '删除失败'), delData.status === 'ok' ? 'success' : 'error');
          if (delData.status === 'ok') loadPluginManager();
        } catch (e) { showStatus('❌ 网络错误', 'error'); }
      });
    });
  } catch (e) { container.innerHTML = '<div class="empty-state"><p>加载失败</p></div>'; }

  document.getElementById('plugin-scan-btn')?.addEventListener('click', async function () {
    this.disabled = true; this.textContent = '扫描中...';
    try {
      const res = await fetch('/api/plugins/scan', { method: 'POST' });
      const data = await res.json();
      showStatus('✅ 扫描完成，已加载 ' + data.count + ' 个插件', 'success');
      loadPluginManager();
    } catch (e) { showStatus('❌ 扫描失败', 'error'); }
    this.disabled = false; this.textContent = '🔍 扫描新插件';
  });

  document.getElementById('plugin-git-install-btn')?.addEventListener('click', async function () {
    const url = document.getElementById('plugin-git-url')?.value.trim();
    if (!url) { showStatus('⚠️ 请输入 Git 仓库 URL', 'error'); return; }
    const name = url.split('/').pop().replace('.git', '');
    this.disabled = true; this.textContent = '安装中...';
    try {
      const res = await fetch('/api/plugins/install', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: name, url: url })
      });
      const d = await res.json();
      if (d.status === 'ok') {
        showStatus('✅ ' + d.message + ' — 请点击扫描加载', 'success');
        document.getElementById('plugin-git-url').value = '';
      } else {
        showStatus('❌ ' + (d.detail || '安装失败'), 'error');
      }
    } catch (e) { showStatus('❌ 网络错误', 'error'); }
    this.disabled = false; this.textContent = '安装';
  });
}

export async function loadMarketplace() {
  const container = document.getElementById('marketplace-list');
  const countEl = document.getElementById('marketplace-count');
  if (!container) return;
  try {
    const res = await fetch('/api/marketplace');
    const data = await res.json();
    const mp = data.marketplace || {};
    const plugins = mp.plugins || [];
    if (countEl) countEl.textContent = plugins.length + ' 个可用';
    if (!plugins.length) {
      container.innerHTML = '<div class="empty-state"><p>插件市场暂无数据</p><small>请检查网络连接或稍后重试</small></div>';
    } else {
      const searchInput = document.getElementById('marketplace-search');
      const filter = searchInput?.value.toLowerCase() || '';
      const filtered = filter ? plugins.filter(p => (p.name + p.description + (p.tags || []).join(' ')).toLowerCase().includes(filter)) : plugins;
      container.innerHTML = filtered.map(p => `
        <div class="rec-ds-card" style="margin-bottom:0.5rem;">
          <div class="rec-ds-name">📦 ${escapeHtml(p.name)} <span style="font-size:0.7rem;color:var(--text-secondary);">v${p.version}</span> ${p.verified ? '<span style="color:var(--success);font-size:0.65rem;">✓ 已验证</span>' : ''}</div>
          <div class="rec-ds-desc">${escapeHtml(p.description)}</div>
          <div class="rec-ds-meta">${escapeHtml(p.author || '')} · ⭐ ${p.rating || '--'} · 📥 ${p.installs || 0}</div>
          <button class="btn-primary marketplace-install-btn" data-name="${p.name}" data-url="${p.source?.repo || ''}" style="margin-top:0.4rem; width:100%; font-size:0.75rem;">安装</button>
        </div>
      `).join('');
      container.querySelectorAll('.marketplace-install-btn').forEach(btn => {
        btn.addEventListener('click', async function () {
          const pname = this.dataset.name;
          const repo = this.dataset.url;
          if (!repo) { showStatus('⚠️ 该插件无安装源', 'error'); return; }
          const gitUrl = 'https://github.com/' + repo + '.git';
          this.disabled = true; this.textContent = '安装中...';
          try {
            const res = await fetch('/api/plugins/install', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ name: pname, url: gitUrl })
            });
            const d = await res.json();
            showStatus(d.status === 'ok' ? '✅ 安装完成 — 请到"已安装"页签扫描加载' : '❌ ' + (d.detail || '失败'), d.status === 'ok' ? 'success' : 'error');
          } catch (e) { showStatus('❌ 网络错误', 'error'); }
          this.disabled = false; this.textContent = '安装';
        });
      });
      if (searchInput && !searchInput._wired) {
        searchInput._wired = true;
        searchInput.addEventListener('input', () => loadMarketplace());
      }
    }
  } catch (e) { container.innerHTML = '<div class="empty-state"><p>市场加载失败</p></div>'; }
}
