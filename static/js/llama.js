import { escapeHtml, showStatus, formatTimeAgo } from './utils.js';
import { state } from './state.js';
import { cachedFetch } from './cache.js';

// ===================== Llama.cpp Management =====================

export async function refreshLlamaStatus() {
  try {
    const status = await cachedFetch('/api/llamacpp/status');

    const binDot = document.getElementById('llama-bin-status-dot');
    const binText = document.getElementById('llama-bin-status-text');
    const runDot = document.getElementById('llama-run-status-dot');
    const runText = document.getElementById('llama-run-status-text');
    const startBtn = document.getElementById('llama-start-btn');
    const stopBtnLlama = document.getElementById('llama-stop-btn');
    const modelSelect = document.getElementById('llama-model-select');

    if (status.installed) {
      binDot.style.background = 'var(--success)';
      binText.textContent = '已安装';
      startBtn.disabled = status.running;
    } else {
      binDot.style.background = 'var(--error)';
      binText.textContent = '未安装';
      startBtn.disabled = true;
    }

    if (status.running) {
      runDot.style.background = 'var(--success)';
      runText.textContent = '运行中 (端口: ' + status.port + ')';
      stopBtnLlama.disabled = false;
    } else {
      runDot.style.background = 'var(--text-secondary)';
      runText.textContent = '已停止';
      stopBtnLlama.disabled = true;
    }

    const currentVal = modelSelect.value;
    modelSelect.innerHTML = '<option value="">-- 请选择本地模型 --</option>';
    (status.models || []).forEach(m => {
      const opt = document.createElement('option');
      opt.value = m;
      opt.textContent = m;
      modelSelect.appendChild(opt);
    });
    if (status.models.includes(currentVal)) {
      modelSelect.value = currentVal;
    }

    if (status.download && status.download.active) {
      // Call back into chat.js for download progress
      window.handleLlamaDownloadProgress?.({
        task: status.download.type,
        label: status.download.label,
        progress: status.download.progress,
        stage: status.download.stage,
        error: status.download.error
      });
    }
  } catch (e) {
    console.error('Failed to fetch llama status', e);
  }
}

// ===================== Download History =====================

export async function loadDownloadHistory() {
  let container = document.getElementById('downloads-view-container') || document.getElementById('download-history-container');
  let emptyState = document.getElementById('download-history-empty');
  if (!container) return;

  try {
    const data = await cachedFetch('/api/downloads');

    container.querySelectorAll('.download-item').forEach(el => el.remove());
    container.querySelectorAll('.empty-state').forEach(el => {
      if (el.querySelector('.spinner')) el.remove();
    });

    if (!data.downloads || data.downloads.length === 0) {
      if (emptyState) {
        emptyState.style.display = 'flex';
      } else {
        container.innerHTML = '<div class="empty-state"><p>暂无下载记录</p><small>模型和数据集下载记录将在此显示</small></div>';
      }
      return;
    }

    if (emptyState) emptyState.style.display = 'none';

    data.downloads.forEach(dl => {
      const iconMap = { 'downloading': '📥', 'paused': '⏸️', 'completed': '✅', 'failed': '❌' };
      const icon = iconMap[dl.status] || '📋';
      const isDataset = dl.type === 'dataset' || (dl.label && dl.label.startsWith('数据集:'));
      const statusText = { 'downloading': '下载中', 'paused': '已暂停', 'completed': '已完成', 'failed': '失败' }[dl.status] || dl.status;

      const progressPct = dl.total_size > 0
        ? Math.round((dl.progress || 0) * 100) + '%'
        : (dl.downloaded_bytes > 0 ? (dl.downloaded_bytes / 1024 / 1024).toFixed(1) + ' MB' : '');

      const showActions = dl.status === 'paused' || dl.status === 'failed';
      const showProgress = dl.status === 'downloading' || dl.status === 'paused';

      const item = document.createElement('div');
      item.className = 'download-item';
      item.id = `download-item-${dl.id}`;
      item.dataset.dlId = dl.id;
      item.innerHTML = `
        <div class="download-item-icon">${isDataset ? (dl.status === 'completed' ? '📊' : icon) : icon}</div>
        <div class="download-item-body">
          <div class="download-item-title">${escapeHtml(dl.label || dl.filename || 'Unknown')}</div>
          <div class="download-item-meta">
            <span class="download-status-badge ${dl.status}">${statusText}</span>
            <span>${progressPct}</span>
            <span>${formatTimeAgo(dl.created_at)}</span>
          </div>
          ${dl.status === 'completed' && dl.target_path ? `
          <div class="download-item-path" title="${escapeHtml(dl.target_path)}">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" style="vertical-align:-1px;margin-right:3px;opacity:0.6"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path></svg>${escapeHtml(dl.target_path)}
          </div>` : ''}
          ${showProgress ? `
          <div class="download-item-progress">
            <div class="download-item-progress-bar ${dl.status === 'failed' ? 'failed' : ''}"
               style="width: ${Math.max(Math.min((dl.progress || 0) * 100, 100), 0)}%;"></div>
          </div>` : ''}
        </div>
        <div class="download-item-actions">
          ${showActions ? `
            <button class="download-action-btn resume" data-id="${dl.id}" title="续传">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                <polygon points="5 3 19 12 5 21 5 3"></polygon>
              </svg>
            </button>` : ''}
          ${dl.status !== 'downloading' ? `
          <button class="download-action-btn delete" data-id="${dl.id}" title="删除">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
              <polyline points="3 6 5 6 21 6"></polyline>
              <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>
            </svg>
          </button>` : ''}
        </div>
      `;
      container.appendChild(item);

      const resumeBtn = item.querySelector('.download-action-btn.resume');
      if (resumeBtn) {
        resumeBtn.addEventListener('click', async (e) => {
          e.stopPropagation();
          resumeBtn.disabled = true;
          const origHTML = resumeBtn.innerHTML;
          resumeBtn.innerHTML = '<div class="spinner" style="width:12px;height:12px;border-width:2px;"></div>';
          try {
            const res = await fetch(`/api/downloads/${resumeBtn.dataset.id}/resume`, { method: 'POST' });
            if (res.ok) {
              showStatus('🔄 正在续传...', 'success');
              loadDownloadHistory();
            } else {
              const err = await res.json();
              showStatus('❌ ' + (err.detail || '续传失败'), 'error');
              resumeBtn.disabled = false;
              resumeBtn.innerHTML = origHTML;
            }
          } catch (e) {
            showStatus('❌ 网络错误', 'error');
            resumeBtn.disabled = false;
            resumeBtn.innerHTML = origHTML;
          }
        });
      }

      const deleteBtn = item.querySelector('.download-action-btn.delete');
      if (deleteBtn) {
        deleteBtn.addEventListener('click', async (e) => {
          e.stopPropagation();
          if (!confirm('确定删除此下载记录？相关的未完成文件也将被删除。')) return;
          try {
            const res = await fetch(`/api/downloads/${deleteBtn.dataset.id}`, { method: 'DELETE' });
            if (res.ok) showStatus('🗑 记录已删除', 'success');
            else { const err = await res.json(); showStatus('❌ ' + (err.detail || '删除失败'), 'error'); }
          } catch (e) { showStatus('❌ 网络错误', 'error'); }
          loadDownloadHistory();
        });
      }
    });
  } catch (e) {
    console.error('Failed to load download history:', e);
  }
}

// ===================== Search Results Rendering =====================

export function renderSearchResults(models, container, filesDiv) {
  const source = document.getElementById('llama-source-select')?.value || 'huggingface';
  let html = '';
  models.forEach(m => {
    const dl = (m.downloads || 0).toLocaleString();
    html +=
      '<div class="search-result-item" data-repo="' + m.repo_id + '"'
      + ' style="border:1px solid var(--border-color);border-radius:6px;margin-bottom:0.4rem;overflow:hidden;">'
      + '<div class="search-result-header"'
      + ' style="padding:0.6rem;cursor:pointer;display:flex;justify-content:space-between;align-items:center;">'
      + '<div>'
      + '<div style="font-weight:600;font-size:0.85rem;">' + m.repo_id + '</div>'
      + '<div style="font-size:0.72rem;color:var(--text-secondary);margin-top:0.15rem;">'
      + '作者: ' + (m.author || '未知') + ' | ⬇ ' + dl + ' | 👍 ' + (m.likes || 0)
      + '</div></div>'
      + '<span class="search-result-arrow" style="font-size:0.7rem;color:var(--text-secondary);">▶</span>'
      + '</div>'
      + '<div class="search-result-files" style="display:none;border-top:1px solid var(--border-color);padding:0.5rem;"></div>'
      + '</div>';
  });
  container.innerHTML = html;

  container.querySelectorAll('.search-result-item').forEach(function(item) {
    var header = item.querySelector('.search-result-header');
    header.addEventListener('click', async function() {
      var filesEl = item.querySelector('.search-result-files');
      var arrow = item.querySelector('.search-result-arrow');
      var repo = item.dataset.repo;

      // Collapse all other items
      container.querySelectorAll('.search-result-files').forEach(function(f) {
        if (f !== filesEl) { f.style.display = 'none'; f.innerHTML = ''; }
      });
      container.querySelectorAll('.search-result-arrow').forEach(function(a) {
        if (a !== arrow) a.textContent = '▶';
      });
      container.querySelectorAll('.search-result-item').forEach(function(el) {
        el.style.borderColor = 'var(--border-color)';
      });

      // Toggle this item
      if (filesEl.style.display !== 'none' && filesEl.innerHTML) {
        filesEl.style.display = 'none';
        arrow.textContent = '▶';
        return;
      }
      filesEl.style.display = '';
      arrow.textContent = '▼';
      item.style.borderColor = 'var(--theme-color)';
      filesEl.innerHTML = '<span style="font-size:0.75rem;color:var(--text-secondary);">加载文件列表中...</span>';

      try {
        var res = await fetch('/api/llamacpp/model-files', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ repo_id: repo, source: source })
        });
        var data = await res.json();
        if (data.status !== 'success' || !data.files.length) {
          filesEl.innerHTML = '<span style="font-size:0.75rem;color:var(--text-secondary);">未找到 GGUF 文件</span>';
          return;
        }
        filesEl.innerHTML = data.files.map(function(f) {
          var size = f.size || (f.size_bytes ? (f.size_bytes > 1e9 ? (f.size_bytes/1e9).toFixed(2)+' GB' : (f.size_bytes/1e6).toFixed(1)+' MB') : '?');
          return '<div style="padding:0.35rem 0.5rem;font-size:0.75rem;border:1px solid var(--border-color);'
            + 'border-radius:4px;margin-bottom:0.25rem;display:flex;justify-content:space-between;align-items:center;">'
            + '<span style="word-break:break-all;flex:1;">' + f.filename + '</span>'
            + '<span style="color:var(--text-secondary);margin:0 0.5rem;white-space:nowrap;font-size:0.7rem;">' + size + '</span>'
            + '<button style="padding:0.15rem 0.4rem;font-size:0.7rem;border:1px solid var(--theme-color);background:var(--theme-color);color:#fff;border-radius:3px;cursor:pointer;white-space:nowrap;"'
            + ' onclick="event.stopPropagation();window.startModelDownload&&window.startModelDownload(\'' + repo + '\',\'' + f.filename + '\',\'' + source + '\')">下载</button>'
            + '</div>';
        }).join('');
      } catch (e) {
        filesEl.innerHTML = '<span style="font-size:0.75rem;color:var(--error);">加载失败</span>';
      }
    });
  });
}

// ===================== Init Listeners =====================

export function initLlamaListeners() {
  document.getElementById('llama-setup-btn')?.addEventListener('click', async () => {
    const btn = document.getElementById('llama-setup-btn');
    const originalText = btn.textContent;
    btn.disabled = true;
    btn.textContent = '正在启动下载...';
    try {
      const res = await fetch('/api/llamacpp/setup', { method: 'POST' });
      const data = await res.json();
      if (data.status === 'started') {
        loadDownloadHistory();
      } else if (data.status === 'success') {
        showStatus('✅ Llama 安装成功', 'success');
      } else {
        showStatus('❌ 安装失败: ' + (data.detail || '未知错误'), 'error');
      }
    } catch (e) {
      showStatus('❌ 网络错误', 'error');
    } finally {
      btn.disabled = false;
      btn.textContent = originalText;
      refreshLlamaStatus();
    }
  });

  document.getElementById('llama-search-btn')?.addEventListener('click', async () => {
    const query = document.getElementById('llama-search-input').value.trim();
    const source = document.getElementById('llama-source-select')?.value || 'modelscope';
    const resultsDiv = document.getElementById('llama-search-results');
    const filesDiv = document.getElementById('llama-model-files');

    if (!query) { showStatus('⚠️ 请输入模型名称搜索', 'error'); return; }

    filesDiv.innerHTML = '';
    resultsDiv.innerHTML = '<span class="field-hint">搜索中...</span>';

    try {
      const res = await fetch('/api/llamacpp/search-models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query, source })
      });
      const data = await res.json();
      if (data.status !== 'success' || !data.models.length) {
        resultsDiv.innerHTML = source === 'huggingface'
          ? '<span class="field-hint">未找到模型（HuggingFace 可能无法访问，请尝试切换到 ModelScope）</span>'
          : '<span class="field-hint">未找到匹配的模型</span>';
        return;
      }
      renderSearchResults(data.models, resultsDiv, filesDiv);
    } catch (e) {
      resultsDiv.innerHTML = '<span class="field-hint">搜索失败，请重试</span>';
    }
  });

  document.getElementById('llama-search-input')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') document.getElementById('llama-search-btn')?.click();
  });

  document.getElementById('llama-start-btn')?.addEventListener('click', async () => {
    const model = document.getElementById('llama-model-select').value;
    if (!model) { showStatus('⚠️ 请先选择一个模型', 'error'); return; }
    try {
      await fetch('/api/llamacpp/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'start', model })
      });
      showStatus('🚀 正在启动服务...', 'success');
      setTimeout(refreshLlamaStatus, 2000);
    } catch (e) { showStatus('❌ 启动失败', 'error'); }
  });

  document.getElementById('llama-stop-btn')?.addEventListener('click', async () => {
    try {
      await fetch('/api/llamacpp/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'stop' })
      });
      showStatus('⏹ 服务正停止', 'success');
      setTimeout(refreshLlamaStatus, 1000);
    } catch (e) { showStatus('❌ 停止失败', 'error'); }
  });
}

// Expose to window
window.refreshLlamaStatus = refreshLlamaStatus;
window.loadDownloadHistory = loadDownloadHistory;
window.renderSearchResults = renderSearchResults;
