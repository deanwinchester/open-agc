// =============================================
// Open-AGC Frontend — Main Entry Point
// =============================================
import './style.css';
import { state } from './js/state.js';
import { escapeHtml, showStatus, t, initI18n, formatTimeAgo, formatTime } from './js/utils.js';
import { switchView, initNavigation } from './js/navigation.js';
import { loadPlugins, loadPluginManager, loadMarketplace } from './js/plugins.js';
import { loadSessions, createSession, switchSession, deleteSession, renameSession } from './js/sessions.js';
import { initSettingsListeners, loadSkillsConfig, loadAgents, openAIDesignModal, closeAIDesignModal, initAIDesignListeners } from './js/settings.js';
import { initTaskFilters, initScheduleModal, loadTasks, updateTaskBadge } from './js/tasks.js';
import { refreshLlamaStatus, loadDownloadHistory, initLlamaListeners, renderSearchResults } from './js/llama.js';

// Expose to window for cross-module calls
window.switchView = switchView;
window.showStatus = showStatus;
window.loadPluginManager = loadPluginManager;
window.loadMarketplace = loadMarketplace;
window.loadSkillsConfig = loadSkillsConfig;
window.loadAgents = loadAgents;
window.openAIDesignModal = openAIDesignModal;
window.closeAIDesignModal = closeAIDesignModal;
window.refreshLlamaStatus = refreshLlamaStatus;
window.loadDownloadHistory = loadDownloadHistory;
window.renderSearchResults = renderSearchResults;

// =============================================
// Permission Modal Setup
// =============================================
(() => {
  const modal = document.getElementById('permission-modal');
  if (!modal) return;
  document.getElementById('perm-modal-close')?.addEventListener('click', () => modal.classList.remove('active'));
  modal.addEventListener('click', (e) => { if (e.target === modal) modal.classList.remove('active'); });
  document.getElementById('perm-modal-copy')?.addEventListener('click', () => {
    const code = document.getElementById('perm-modal-code')?.textContent || '';
    navigator.clipboard.writeText(code.trim()).then(() => {
      const btn = document.getElementById('perm-modal-copy');
      btn.textContent = '✓ 已复制';
      setTimeout(() => btn.textContent = '复制命令', 2000);
    });
  });
})();

// Close button for global download banner
document.getElementById('global-download-close')?.addEventListener('click', () => {
  document.getElementById('global-download-banner').style.display = 'none';
});

// =============================================
// Main App Init
// =============================================
function loadVendorScripts() {
  var scripts = [
    '/static/vendor/chart.min.js',
    '/static/vendor/marked.min.js',
    '/static/vendor/highlight.min.js'
  ];
  // Load vendor scripts one at a time with delay, so they don't hog connections
  var i = 0;
  function next() {
    if (i >= scripts.length) return;
    var s = document.createElement('script');
    s.src = scripts[i++];
    s.onload = next;
    s.onerror = next;
    document.head.appendChild(s);
  }
  // Start loading after a delay, giving priority to API calls
  setTimeout(next, 2000);
}

function initApp() {
  window._perf.initApp = performance.now();
  loadVendorScripts();
  initI18n();
  initNavigation();
  loadPlugins();
  initSettingsListeners();
  initAIDesignListeners();
  initTaskFilters();
  initScheduleModal();
  initLlamaListeners();

  // =============================================
  // DOM References
  // =============================================
  const chatContainer = document.getElementById('chat-container');
  const messageInput = document.getElementById('message-input');
  const imageFileInput = document.getElementById('image-file-input');
  const imageBtn = document.getElementById('image-btn');
  const imagePreviewBar = document.getElementById('image-preview-bar');
  const sendBtn = document.getElementById('send-btn');
  const stopBtn = document.getElementById('stop-btn');
  const themeToggle = document.getElementById('theme-toggle');
  const htmlElement = document.documentElement;
  const currentModelBadge = document.getElementById('current-model-badge');
  const chatBody = document.getElementById('chat-body');
  const detailPanel = document.getElementById('detail-panel');
  const detailPanelClose = document.getElementById('detail-panel-close');
  const detailPanelTitle = document.getElementById('detail-panel-title');
  const detailPanelBody = document.getElementById('detail-panel-body');

  // =============================================
  // Download Progress
  // =============================================
  let downloadResumeInfo = null;
  let downloadBannerTimer = null;
  let _lastDownloadBannerId = null;  // Track current download for "flash once" behavior

  function handleLlamaDownloadProgress(data) {
    const ratio = data.progress || 0;
    const pctText = Math.round(ratio * 100) + '%';
    const dlId = data.download_id;
    // Update specific download item by ID
    const historyContainer = document.getElementById('download-history-container') || document.getElementById('downloads-view-container');
    if (historyContainer && dlId) {
      const item = historyContainer.querySelector(`.download-item[data-dl-id="${dlId}"]`);
      if (item) {
        const bar = item.querySelector('.download-item-progress-bar');
        if (bar) bar.style.width = Math.max(ratio * 100, 0) + '%';
        const metaSpans = item.querySelectorAll('.download-item-meta span');
        if (metaSpans.length >= 2) metaSpans[1].textContent = pctText;
        if (data.stage === 'complete') {
          var badge = item.querySelector('.download-status-badge');
          if (badge) { badge.textContent = 'completed'; badge.className = 'download-status-badge completed'; }
        }
      }
    }

    const banner = document.getElementById('global-download-banner');
    const bannerLabel = document.getElementById('global-download-label');
    const bannerPct = document.getElementById('global-download-pct');
    const bannerBar = document.getElementById('global-download-bar');
    const bannerIcon = document.getElementById('global-download-icon');

    if (data.stage === 'complete') {
      clearTimeout(downloadBannerTimer);
      if (banner) {
        banner.style.display = 'block';
        bannerIcon.textContent = '✅';
        bannerLabel.textContent = data.label || '下载完成';
        bannerPct.textContent = '100%';
        bannerBar.style.width = '100%';
        bannerBar.style.background = 'var(--success)';
      }
      downloadResumeInfo = null;
      showStatus('✅ ' + (data.label || '下载完成'), 'success');
      downloadBannerTimer = setTimeout(() => { if (banner) banner.style.display = 'none'; }, 2000);
      _lastDownloadBannerId = null;
      refreshLlamaStatus();
      loadDownloadHistory();
    } else if (data.stage === 'error') {
      clearTimeout(downloadBannerTimer);
      if (banner) {
        banner.style.display = 'block';
        bannerIcon.textContent = '❌';
        bannerLabel.textContent = data.error || data.label || '下载失败';
        bannerPct.textContent = '✗';
        bannerBar.style.width = '0%';
        bannerBar.style.background = 'var(--error)';
      }
      showStatus('❌ ' + (data.error || '下载失败'), 'error');
      downloadBannerTimer = setTimeout(() => { if (banner) banner.style.display = 'none'; }, 3000);
      _lastDownloadBannerId = null;
      loadDownloadHistory();
    } else {
      // Flash banner briefly only on first event of a new download
      if (dlId && dlId !== _lastDownloadBannerId) {
        _lastDownloadBannerId = dlId;
        if (banner) {
          banner.style.display = 'block';
          bannerIcon.textContent = data.stage === 'extracting' ? '📦' : '📥';
          bannerLabel.textContent = data.label || '下载中...';
        }
        if (bannerPct) bannerPct.textContent = pctText;
        if (bannerBar) { bannerBar.style.width = (ratio * 100) + '%'; bannerBar.style.background = 'var(--theme-color)'; }
        downloadBannerTimer = setTimeout(() => { if (banner) banner.style.display = 'none'; }, 1500);
      }
      // Always update download manager item progress (handled above by historyContainer querySelector)
    }
  }

  window.handleLlamaDownloadProgress = handleLlamaDownloadProgress;

  // =============================================
  // WebSocket & Agent Communication
  // =============================================
  let wsReconnectAttempt = 0;
  let wsReconnectTimer = null;
  window._wsReconnectTimer = null;  // exposed so sessions.js can clear it

  function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws?session_id=${state.currentSessionId}`;
    state.ws = new WebSocket(wsUrl);

    state.ws.onopen = () => {
      state.isConnected = true;
      wsReconnectAttempt = 0;
      wsReconnectTimer = null;
      window._wsReconnectTimer = null;
      updateInputState();
      refreshLlamaStatus();
      loadDownloadHistory();
    };

    state.ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      // Dispatch to plugin listeners first (they can handle training, benchmark, etc.)
      var listeners = window._pluginWsListeners || [];
      if (listeners.length && (data.type || '').indexOf('training') === 0) {
        console.debug('[app] dispatching', data.type, 'to', listeners.length, 'listener(s)');
      }
      for (var i = 0; i < listeners.length; i++) {
        try { listeners[i](data); } catch(e) { console.error('Plugin WS listener error:', e); }
      }
      handleServerMessage(data);
    };

    state.ws.onclose = () => {
      state.isConnected = false;
      // Skip reconnect if this was an intentional close (e.g. session switch)
      if (window._intentionalClose) {
        window._intentionalClose = false;
        updateInputState();
        return;
      }
      const delay = Math.min(1000 * Math.pow(2, wsReconnectAttempt), 30000);
      wsReconnectAttempt++;
      wsReconnectTimer = setTimeout(connectWebSocket, delay);
      window._wsReconnectTimer = wsReconnectTimer;
      updateInputState();
    };

    state.ws.onerror = (error) => {
      console.error('WebSocket Error:', error);
    };
  }

  // Reconnect on network recovery
  window.addEventListener('online', () => {
    if (!state.isConnected && state.ws) {
      clearTimeout(wsReconnectTimer);
      wsReconnectAttempt = 0;
      connectWebSocket();
    }
  });

  window.connectWebSocket = connectWebSocket;

  // Generic plugin message type registry — plugins register their
  // WebSocket message types here so the main app won't auto-switch
  // to chat view when those messages arrive.
  window._pluginWsTypes = new Set();
  window.registerWsMessageType = function(type) {
    if (type) window._pluginWsTypes.add(type);
  };
  // Plugin WebSocket message listener registry
  window._pluginWsListeners = [];
  window.addWsListener = function(fn) {
    window._pluginWsListeners.push(fn);
  };

  function handleServerMessage(data) {
    const isBackground = data.background === true;
    // Route progress/message/status events to the correct session
    const evtSession = data.session_id != null ? data.session_id : (data.task_session_id != null ? data.task_session_id : null);
    const isForCurrentSession = evtSession == null || evtSession == state.currentSessionId;
    // Cache off-session messages in the session chat cache
    if (evtSession != null && !isForCurrentSession && (data.type === 'message' || data.type === 'progress')) {
      window._sessionChatCache = window._sessionChatCache || {};
      // Store raw events for the session — will be replayed on switch
      if (!window._sessionChatCache['_evt_' + evtSession]) window._sessionChatCache['_evt_' + evtSession] = [];
      window._sessionChatCache['_evt_' + evtSession].push({type: data.type, data: data});
      // Update task badge to show cross-session activity
      if (data.type === 'progress') updateTaskBadge();
      return;
    }

    // Increment unread badge if user is not on chat view
    if (!isBackground
      && (data.type === 'message' || data.type === 'status' || data.type === 'progress')
      && document.querySelector('.view.active')?.id !== 'view-chat') {
      const badge = document.getElementById('chat-unread-badge');
      if (badge) {
        const cur = parseInt(badge.textContent) || 0;
        badge.textContent = cur + 1;
        badge.style.display = 'flex';
      }
    }

    if (data.type === 'history_steps') {
      renderHistorySteps(data);
      return;
    }

    if (data.type === 'status') {
      if (!isBackground && isForCurrentSession) showThinkingStatus(t('agent_thinking'));
    } else if (data.type === 'progress') {
      if (data.task_id && !isBackground && isForCurrentSession) state.currentTaskId = data.task_id;
      if (isForCurrentSession) handleProgressEvent(data);
      if (isBackground) updateTaskBadge();
    } else if (data.type === 'message') {
      if (!isBackground && isForCurrentSession) { hideThinkingStatus(); hideProgressContainer(); }
      appendMessage(data.content, data.role || 'agent');
      if (!isBackground && isForCurrentSession && state.wasVoiceQuery) { speakText(data.content); state.wasVoiceQuery = false; }
      if (!isBackground && isForCurrentSession) { state.isAgentThinking = false; state.currentTaskId = null; updateInputState(); }
      updateTaskBadge();
    } else if (data.type === 'error') {
      if (!isBackground) {
        hideThinkingStatus();
        hideProgressContainer();
        showRetryBar(data.original_query || '', data.content);
        checkPermissionError(data.content);
        state.isAgentThinking = false;
        state.currentTaskId = null;
        updateInputState();
      } else {
        appendMessage(`**后台任务错误**: ${data.content}`, 'system');
      }
      updateTaskBadge();
    } else if (data.type === 'task_backgrounded') {
      appendMessage(`⏸️ **任务已进入后台**\n\n${data.message || '后台任务处理中...'}\n\n完成后将自动恢复执行。`, 'system');
      hideProgressContainer();
      updateTaskBadge();
    } else if (data.type === 'download_success') {
      appendMessage(
        '✅ **下载完成: ' + (data.label || '') + '**',
        'system'
      );
      showStatus('✅ 下载完成: ' + (data.label || ''), 'success');
    } else if (data.type === 'download_failed') {
      appendMessage(
        '❌ **下载失败: ' + (data.label || '') + '**\n\n错误: ' + (data.error || '未知错误'),
        'system'
      );
      showStatus('❌ 下载失败: ' + (data.error || '未知错误'), 'error');
    } else if (data.type === 'llamacpp_download') {
      handleLlamaDownloadProgress(data);
    }
  }

  function showSandboxBlockedModal(data) {
    var modal = document.getElementById('sandbox-blocked-modal');
    var isNetwork = data.block_type === 'network';
    var isPermission = data.block_type === 'permission';
    if (!modal) {
      modal = document.createElement('div');
      modal.id = 'sandbox-blocked-modal';
      modal.className = 'modal-overlay';
      modal.style.display = 'flex';
      document.body.appendChild(modal);
    }
    // Determine UI based on block type
    var headerIcon, headerTitle, descText, buttonsHtml;
    if (isPermission) {
      headerIcon = '⚡';
      headerTitle = '敏感操作授权';
      descText = '请求执行敏感操作：';
      buttonsHtml =
        `<button id="sb-approve-once" class="btn-sandbox" style="flex:1;min-width:45%;">✓ 授权本次</button>
         <button id="sb-approve-session" class="btn-sandbox" style="flex:1;min-width:45%;">🔄 本次会话全部同类</button>
         <button id="sb-approve-always" class="btn-sandbox primary" style="flex:1;min-width:45%;">⭐ 永久授权</button>
         <button id="sb-deny-once" class="btn-sandbox deny" style="flex:1;min-width:45%;">✗ 拒绝本次</button>
         <button id="sb-deny-always" class="btn-sandbox deny" style="flex:1;min-width:45%;">🚫 永久拒绝</button>`;
    } else if (isNetwork) {
      headerIcon = '🌐';
      headerTitle = '网络访问授权';
      descText = '请求访问外部网址：';
      buttonsHtml =
        `<button id="sb-approve-once" class="btn-sandbox" style="flex:1;min-width:45%;">✓ 单次授权</button>
         <button id="sb-approve-always" class="btn-sandbox primary" style="flex:1;min-width:45%;">⭐ 永久授权</button>
         <button id="sb-deny-once" class="btn-sandbox deny" style="flex:1;min-width:45%;">✗ 拒绝本次</button>
         <button id="sb-deny-always" class="btn-sandbox deny" style="flex:1;min-width:45%;">🚫 永久拒绝</button>`;
    } else {
      headerIcon = '🔐';
      headerTitle = '沙箱路径授权';
      descText = '请求访问沙箱外路径：';
      buttonsHtml =
        `<button id="sb-approve-dir" class="btn-sandbox primary" style="flex:1;min-width:45%;">📁 授权整个目录</button>
         <button id="sb-approve-once" class="btn-sandbox" style="flex:1;min-width:45%;">✓ 单次授权</button>
         <button id="sb-approve-always" class="btn-sandbox primary" style="flex:1;min-width:45%;">⭐ 永久授权</button>
         <button id="sb-deny-once" class="btn-sandbox deny" style="flex:1;min-width:45%;">✗ 拒绝本次</button>
         <button id="sb-deny-always" class="btn-sandbox deny" style="flex:1;min-width:45%;">🚫 永久拒绝</button>`;
    }

    // Show description line for permission (e.g. "rm -rf / (删除根目录)")
    var descLine = '';
    if (isPermission && data.description) {
      descLine = `<div style="color:var(--warning,#e67e22);font-size:0.85rem;margin-bottom:0.5rem;">⚠️ ${data.description}</div>`;
    }

    modal.innerHTML = `
      <div class="modal-box" style="max-width:480px;width:90%;">
        <div class="modal-header"><h3>${headerIcon} ${headerTitle}</h3></div>
        <div class="modal-body">
          <p style="margin:0 0 0.5rem">Agent 工具 <b id="sb-tool"></b> ${descText}</p>
          ${descLine}
          <div id="sb-path" style="background:var(--bg-secondary);padding:0.5rem 0.75rem;border-radius:6px;font-family:monospace;font-size:0.82rem;word-break:break-all;margin-bottom:1rem;"></div>
          <div style="display:flex;flex-wrap:wrap;gap:0.5rem;">${buttonsHtml}</div>
        </div>
      </div>`;
    modal.style.display = 'flex';

    document.getElementById('sb-tool').textContent = data.tool_name || data.tool || '?';
    document.getElementById('sb-path').textContent = data.path || '';

    function respond(action) {
      modal.style.display = 'none';
      if (state.ws && state.ws.readyState === WebSocket.OPEN) {
        state.ws.send(JSON.stringify({
          type: 'sandbox_response',
          action: action,
          path: data.path,
          session_id: state.currentSessionId
        }));
      }
    }

    document.getElementById('sb-approve-once').onclick = function() { respond('approve_once'); };
    document.getElementById('sb-approve-always').onclick = function() { respond('approve_always'); };
    document.getElementById('sb-deny-once').onclick = function() { respond('deny_once'); };
    document.getElementById('sb-deny-always').onclick = function() { respond('deny_always'); };
    var sessionBtn = document.getElementById('sb-approve-session');
    if (sessionBtn) sessionBtn.onclick = function() { respond('approve_session'); };
    var dirBtn = document.getElementById('sb-approve-dir');
    if (dirBtn) dirBtn.onclick = function() { respond('approve_dir'); };
  }

  // =============================================
  // Progress Tracking UI
  // =============================================
  // Inline collapsible progress card system
  // =============================================
  let progressInline = null;       // Current .progress-inline element
  let progressStepsEl = null;     // .progress-inline-steps container
  let progressSteps = {};
  let progressStepData = {};
  let progressStepCount = 0;

  function ensureProgressContainer() {
    if (!progressInline) {
      // Remove any completed history card for this task before creating live progress
      if (state.currentTaskId) {
        const oldCard = chatContainer.querySelector(`.progress-inline.completed[data-task-id="${state.currentTaskId}"]`);
        if (oldCard) oldCard.remove();
      }
      hideThinkingStatus();
      progressInline = document.createElement('div');
      progressInline.className = 'progress-inline';
      progressInline.innerHTML = `
        <div class="progress-inline-header" id="progress-inline-header">
          <div class="progress-inline-left">
            <div class="progress-spinner"></div>
            <span class="progress-title">${t('working')}</span>
            <span class="progress-current-step"></span>
          </div>
          <span class="progress-toggle-icon collapsed">▸</span>
        </div>
        <div class="progress-inline-steps" id="progress-inline-steps" style="max-height: 0px;"></div>
        <div class="progress-inline-footer" style="display:none">
          <button class="btn-collapse-steps">收起日志 ▴</button>
        </div>`;
      progressStepsEl = progressInline.querySelector('.progress-inline-steps');
      if (state.currentTaskId) progressInline.dataset.taskId = state.currentTaskId;

      // Toggle collapse/expand on header click
      const header = progressInline.querySelector('.progress-inline-header');
      const footer = progressInline.querySelector('.progress-inline-footer');
      const collapseBtn = footer.querySelector('.btn-collapse-steps');
      
      const toggleCollapse = function(forceCollapse = false) {
        const steps = progressInline.querySelector('.progress-inline-steps');
        const icon = progressInline.querySelector('.progress-toggle-icon');
        const isCurrentlyCollapsed = steps.style.maxHeight === '0px' || !steps.style.maxHeight;
        
        if (!isCurrentlyCollapsed || forceCollapse) {
          // Collapse
          if (steps.style.maxHeight === 'none') steps.style.maxHeight = steps.scrollHeight + 'px';
          steps.offsetHeight; // force reflow
          steps.style.maxHeight = '0px';
          icon.classList.remove('expanded');
          icon.classList.add('collapsed');
          icon.textContent = '▸';
          footer.style.display = 'none';
          // If we are at the bottom, scroll header into view
          progressInline.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        } else {
          // Expand
          steps.style.maxHeight = steps.scrollHeight + 'px';
          icon.classList.remove('collapsed');
          icon.classList.add('expanded');
          icon.textContent = '▾';
          footer.style.display = 'block';
          setTimeout(() => { if(steps.style.maxHeight !== '0px') steps.style.maxHeight = 'none'; }, 350);
        }
      };

      header.addEventListener('click', () => toggleCollapse());
      collapseBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        toggleCollapse(true);
      });

      // Insert after last user message
      const userMsgs = chatContainer.querySelectorAll('.message.user');
      const lastUser = userMsgs[userMsgs.length - 1];
      if (lastUser) {
        lastUser.insertAdjacentElement('afterend', progressInline);
      } else {
        chatContainer.appendChild(progressInline);
      }
      scrollToBottom();
    }
    return progressInline;
  }

  function finishProgressContainer() {
    if (!progressInline) return;
    var card = progressInline;  // Capture before nulling
    var title = card.querySelector('.progress-title');
    var spinner = card.querySelector('.progress-spinner');
    var currentStepEl = card.querySelector('.progress-current-step');

    if (title) title.innerHTML = `✨ ${t('done')} · ${progressStepCount} 步`;
    if (spinner) spinner.style.display = 'none';
    if (currentStepEl) currentStepEl.style.display = 'none';
    // Collapse steps and show the footer with expand button
    var st = card.querySelector('.progress-inline-steps');
    if (st) st.style.maxHeight = '0px';
    var ft = card.querySelector('.progress-inline-footer');
    if (ft) {
      ft.style.display = 'block';
      var btn = ft.querySelector('.btn-collapse-steps');
      if (btn) btn.textContent = '展开步骤 ▾';
      btn.onclick = function(e) {
        e.stopPropagation();
        var s = card.querySelector('.progress-inline-steps');
        var b = this;
        if (!s) return;
        if (s.style.maxHeight === '0px') {
          s.style.maxHeight = s.scrollHeight + 'px';
          b.textContent = '收起步骤 ▴';
          setTimeout(function() { if (s.style.maxHeight !== '0px') s.style.maxHeight = 'none'; }, 350);
        } else {
          s.style.maxHeight = '0px';
          b.textContent = '展开步骤 ▾';
        }
      };
    }
    card.classList.add('completed');

    progressInline = null;
    progressStepsEl = null;
    progressSteps = {};
    progressStepCount = 0;
  }

  window.handleProgressEvent = handleProgressEvent;
  function handleProgressEvent(data) {
    const event = data.event;

    // sandbox_blocked must be handled regardless of progress container state
    if (event === 'sandbox_blocked') {
      showSandboxBlockedModal(data);
      return;
    }

    // Set task_id on state so ensureProgressContainer can clean up stale cards
    if (data.task_id && !state.currentTaskId) {
      state.currentTaskId = data.task_id;
    }

    const stepsEl = ensureProgressContainer() ? progressStepsEl : null;
    if (!stepsEl) return;

    // Attach task_id to the card so renderHistorySteps can detect duplicates
    if (data.task_id && progressInline && !progressInline.dataset.taskId) {
      progressInline.dataset.taskId = data.task_id;
    }

    if (event === 'thinking') {
      if (data.content) {
        let thinkEl = document.getElementById(`progress-thought-${data.iteration || 0}`);
        if (!thinkEl) {
          thinkEl = document.createElement('div');
          thinkEl.className = 'progress-step thinking-process';
          thinkEl.id = `progress-thought-${data.iteration || 0}`;
          thinkEl.innerHTML = `
            <span class="step-icon">🧠</span>
            <div class="step-body">
              <span class="step-label">思考过程</span>
              <span class="step-detail">${escapeHtml(data.content)}</span>
            </div>`;
          stepsEl.appendChild(thinkEl);
          const thinkKey = `thought-${data.iteration || 0}`;
          progressStepData[thinkKey] = { type: 'thinking', label: '思考过程', content: data.content, iteration: data.iteration };
          thinkEl.addEventListener('click', (e) => { e.stopPropagation(); showStepDetail(thinkKey); });
        } else {
          const detailEl = thinkEl.querySelector('.step-detail');
          if (detailEl) detailEl.textContent = data.content;
          const thinkKey = `thought-${data.iteration || 0}`;
          if (progressStepData[thinkKey]) progressStepData[thinkKey].content = data.content;
        }
      } else {
        showThinkingStatus(t('agent_thinking'));
      }
      return;
    }

    if (event === 'shell_output') {
      updateShellOutput(data);
      return;
    }

    if (event === 'model_switched') {
      const switchNote = document.createElement('div');
      switchNote.className = 'progress-step model-switch';
      switchNote.innerHTML = `<span class="step-icon">🔄</span><span class="step-text">模型已切换: ${data.from} → <strong>${data.to}</strong></span>`;
      stepsEl.appendChild(switchNote);
      scrollToBottom();
      return;
    }

    if (event === 'ask_user') {
      hideThinkingStatus();
      const stepEl = document.createElement('div');
      stepEl.className = 'progress-step ask-user';
      stepEl.style.border = '1px solid var(--primary)';
      stepEl.style.padding = '12px';
      stepEl.style.borderRadius = '8px';
      stepEl.style.backgroundColor = 'rgba(74, 144, 226, 0.05)';
      stepEl.innerHTML = `
        <span class="step-icon">❓</span>
        <div class="step-body" style="width: 100%;">
          <span class="step-label" style="color: var(--primary); font-weight: bold;">Agent 提问 / 确认</span>
          <span class="step-detail" style="margin-bottom: 12px; font-size: 1rem;">${escapeHtml(data.question)}</span>
          <div class="ask-user-form" style="display: flex; gap: 8px; flex-direction: column;">
             ${data.options && data.options.length ? 
                data.options.map(o => `<button class="btn-secondary ask-option-btn" data-val="${escapeHtml(o)}">${escapeHtml(o)}</button>`).join('') :
                `<input type="text" class="input-styled ask-user-input" placeholder="请输入您的回答..." />
                 <button class="btn-primary ask-user-submit" style="align-self: flex-start;">提交回答</button>`
             }
          </div>
        </div>`;
      stepsEl.appendChild(stepEl);
      scrollToBottom();
      
      const submitAnswer = (ans) => {
          if (!state.ws || state.ws.readyState !== WebSocket.OPEN) return;
          state.ws.send(JSON.stringify({ type: 'tool_reply', answer: ans }));
          stepEl.querySelector('.ask-user-form').innerHTML = `<span style="color: var(--success, #2ecc71); font-size: 0.9rem;">✓ 您的回答：<strong>${escapeHtml(ans)}</strong></span>`;
      };

      if (data.options && data.options.length) {
          stepEl.querySelectorAll('.ask-option-btn').forEach(btn => {
              btn.addEventListener('click', () => submitAnswer(btn.dataset.val));
          });
      } else {
          const input = stepEl.querySelector('.ask-user-input');
          const btn = stepEl.querySelector('.ask-user-submit');
          input.focus();
          input.addEventListener('keypress', (e) => { if (e.key === 'Enter') btn.click(); });
          btn.addEventListener('click', () => {
              const val = input.value.trim();
              if (val) submitAnswer(val);
          });
      }
      return;
    }

    if (event === 'usage') {
        const usageEl = document.getElementById('token-usage-display');
        if (usageEl) {
            usageEl.textContent = `Tokens: ${data.total_tokens} (P:${data.prompt_tokens} / C:${data.completion_tokens})`;
            usageEl.classList.add('updated');
            setTimeout(() => usageEl.classList.remove('updated'), 500);
        }
        return;
    }

    if (event === 'tool_start') {
      progressStepCount++;
      const stepEl = document.createElement('div');
      stepEl.className = 'progress-step running';
      stepEl.id = `progress-step-${data.step}`;
      stepEl.innerHTML = `
        <span class="step-icon"><div class="step-spinner"></div></span>
        <div class="step-body">
          <span class="step-label">${data.step}. ${data.tool_label || data.tool}</span>
          ${data.args_preview ? `<span class="step-detail">${escapeHtml(data.args_preview)}</span>` : ''}
        </div>`;
      stepsEl.appendChild(stepEl);
      progressSteps[data.step] = stepEl;

      // Update current step in header
      const headerTitle = progressInline.querySelector('.progress-title');
      const currentStepEl = progressInline.querySelector('.progress-current-step');
      if (headerTitle) headerTitle.textContent = `⚡ 执行中 · ${progressStepCount} 步`;
      if (currentStepEl) currentStepEl.textContent = ` : ${data.tool_label || data.tool}`;
      
      // Auto-expand removed to keep progress collapsed by default
      
      scrollToBottom();
      progressStepData[data.step] = {
        type: 'tool', step: data.step, tool: data.tool,
        tool_label: data.tool_label || data.tool,
        args_preview: data.args_preview || '', full_args: data.args_preview || '',
        result_preview: '', full_result: '', success: null, status: 'running'
      };
      stepEl.addEventListener('click', (e) => { e.stopPropagation(); showStepDetail(data.step); });
      // Update title with step count
      const title = progressInline.querySelector('.progress-title');
      if (title) title.textContent = `🐼 执行中 · ${progressStepCount} 步`;
      scrollToBottom();
      return;
    }

    if (event === 'tool_done') {
      const stepEl = progressSteps[data.step];
      if (stepEl) {
        stepEl.classList.remove('running');
        stepEl.classList.add(data.success ? 'done' : 'failed');
        const iconEl = stepEl.querySelector('.step-icon');
        iconEl.innerHTML = data.success ? '✅' : '❌';
        if (data.result_preview) {
          const detailEl = stepEl.querySelector('.step-detail');
          if (detailEl) { detailEl.textContent = data.result_preview; } else {
            const bodyEl = stepEl.querySelector('.step-body');
            const newDetail = document.createElement('span');
            newDetail.className = 'step-detail';
            newDetail.textContent = data.result_preview;
            bodyEl.appendChild(newDetail);
          }
        }
      }
      if (progressStepData[data.step]) {
        progressStepData[data.step].result_preview = data.result_preview || '';
        progressStepData[data.step].full_result = data.result_preview || '';
        progressStepData[data.step].success = data.success;
        progressStepData[data.step].status = data.success ? 'done' : 'failed';
      }
      if (detailPanel.style.display !== 'none' && detailPanel.dataset.stepKey == data.step) {
        showStepDetail(data.step);
      }
      scrollToBottom();
      return;
    }
  }

  function showStepDetail(stepKey) {
    const stepData = progressStepData[stepKey];
    if (!stepData) return;

    detailPanel.dataset.stepKey = stepKey;
    chatBody.classList.add('split-view');
    detailPanel.style.display = 'flex';

    if (stepData.type === 'thinking') {
      detailPanelTitle.textContent = '🧠 思考过程';
      detailPanelBody.innerHTML = `
        <div class="detail-section"><div class="detail-section-title">迭代轮次</div><div>第 ${stepData.iteration || 1} 轮</div></div>
        <div class="detail-section"><div class="detail-section-title">思考内容</div><div class="detail-content-block">${escapeHtml(stepData.content)}</div></div>`;
    } else {
      const statusClass = stepData.success === true ? 'success' : stepData.success === false ? 'failed' : 'running';
      const statusText = stepData.success === true ? '✅ 成功' : stepData.success === false ? '❌ 失败' : '⏳ 执行中';
      detailPanelTitle.textContent = `步骤 ${stepData.step} 详情`;
      detailPanelBody.innerHTML = `
        <div class="detail-section"><div class="detail-section-title">工具</div><div><strong>${stepData.tool_label}</strong> <code style="font-size:0.75rem;color:var(--text-secondary)">(${stepData.tool})</code></div></div>
        <div class="detail-section"><div class="detail-section-title">状态</div><span class="detail-status ${statusClass}">${statusText}</span></div>
        ${stepData.full_args ? `<div class="detail-section"><div class="detail-section-title">参数</div><div class="detail-content-block">${escapeHtml(stepData.full_args)}</div></div>` : ''}
        ${stepData.full_result ? `<div class="detail-section"><div class="detail-section-title">结果</div><div class="detail-content-block">${escapeHtml(stepData.full_result)}</div></div>` : ''}`;
    }
  }

  function closeDetailPanel() {
    chatBody.classList.remove('split-view');
    detailPanel.style.display = 'none';
    detailPanel.dataset.stepKey = '';
  }

  detailPanelClose.addEventListener('click', closeDetailPanel);

  function hideProgressContainer() {
    finishProgressContainer();
  }

  function updateShellOutput(data) {
    if (!progressInline) return;
    var shellBox = progressInline.querySelector('.shell-output-box');
    if (!shellBox) {
      shellBox = document.createElement('div');
      shellBox.className = 'shell-output-box';
      var st = progressInline.querySelector('.progress-inline-steps');
      if (st) st.insertAdjacentElement('afterend', shellBox);
    }
    var text = data.text || '';
    var lineEl = document.createElement('div');
    lineEl.className = 'shell-output-line';
    lineEl.textContent = text;
    shellBox.appendChild(lineEl);
    shellBox.scrollTop = shellBox.scrollHeight;
    var lines = shellBox.getElementsByClassName('shell-output-line');
    while (lines.length > 200) lines[0].remove();
    scrollToBottom();
  }

  function renderHistorySteps(data) {
    // If already showing progress for this task, don't create a duplicate card
    if (progressInline && progressInline.dataset.taskId == data.task_id) return;

    // Remove any existing card for this task (both completed and live)
    const existing = chatContainer.querySelector(
      `.progress-inline.completed[data-task-id="${data.task_id}"],` +
      `.progress-inline:not(.completed)[data-task-id="${data.task_id}"]`);
    if (existing) existing.remove();

    const steps = data.steps || [];
    if (!steps.length) return;

    const historyCard = document.createElement('div');
    historyCard.className = 'progress-inline completed';
    historyCard.dataset.taskId = data.task_id;
    const stepCount = steps.length;
    historyCard.innerHTML = `
      <div class="progress-inline-header" style="min-height: 38px; padding: 0.7rem 1rem;">
        <div class="progress-inline-left">
          <span class="progress-title">⚡ 上次执行 · ${stepCount} 步</span>
          <span class="progress-current-step" style="margin-left: 8px; color: var(--text-secondary); opacity: 0.8; font-size: 0.8rem;"></span>
        </div>
        <div class="progress-inline-right">
          ${data.task_status === 'interrupted'
            ? `<button class="btn-resume-task" data-task-id="${data.task_id}" title="继续执行">▶ 继续</button>`
            : ''}
          <span class="progress-toggle-icon collapsed">▸</span>
        </div>
      </div>
      <div class="progress-inline-steps" style="max-height: none;"></div>
      <div class="progress-inline-footer">
        <button class="btn-collapse-steps">收起步骤 ▴</button>
      </div>`;

    const stepsEl = historyCard.querySelector('.progress-inline-steps');
    // Wire footer collapse button
    var histFooter = historyCard.querySelector('.progress-inline-footer');
    var histBtn = histFooter?.querySelector('.btn-collapse-steps');
    if (histBtn) {
      histBtn.onclick = function(e) {
        e.stopPropagation();
        var s = historyCard.querySelector('.progress-inline-steps');
        var b = this;
        if (!s) return;
        if (s.style.maxHeight === '0px') {
          s.style.maxHeight = s.scrollHeight + 'px';
          b.textContent = '收起步骤 ▴';
          setTimeout(function() { if (s.style.maxHeight !== '0px') s.style.maxHeight = 'none'; }, 350);
        } else {
          s.style.maxHeight = '0px';
          b.textContent = '展开步骤 ▾';
        }
      };
    }
    const toggleIcon = historyCard.querySelector('.progress-toggle-icon');
    if (toggleIcon) {
      toggleIcon.classList.remove('collapsed');
      toggleIcon.classList.add('expanded');
      toggleIcon.textContent = '▾';
    }
    steps.forEach((s, i) => {
      const stepEl = document.createElement('div');
      const isFailed = s.success === false;
      stepEl.className = `progress-step ${isFailed ? 'failed' : 'done'}`;
      stepEl.innerHTML = `
        <span class="step-icon">${isFailed ? '❌' : '✅'}</span>
        <div class="step-body">
          <span class="step-label">${s.step_number}. ${s.tool_label || s.tool_name}</span>
          ${s.args_preview ? `<span class="step-detail">${escapeHtml(s.args_preview)}</span>` : ''}
          ${s.result_preview ? `<span class="step-detail">${escapeHtml((s.full_result || s.result_preview).substring(0, 600))}</span>` : ''}
        </div>`;
      
      // Register step data for detail view
      const stepKey = `hist-${data.task_id}-${s.step_number}`;
      // Use full_result from DB for shell commands
      const stepResult = s.full_result || s.result_preview || '';
      progressStepData[stepKey] = {
        type: 'tool',
        step: s.step_number,
        tool: s.tool_name,
        tool_label: s.tool_label || s.tool_name,
        full_args: s.full_args || s.args_preview,
        full_result: stepResult,
        success: s.success,
        status: 'completed'
      };
      
      stepEl.addEventListener('click', (e) => {
        e.stopPropagation();
        showStepDetail(stepKey);
      });
      
      stepsEl.appendChild(stepEl);
    });

    // Insert after last user message
    const userMsgs = chatContainer.querySelectorAll('.message.user');
    const lastUser = userMsgs[userMsgs.length - 1];
    if (lastUser) {
      lastUser.insertAdjacentElement('afterend', historyCard);
    } else {
      chatContainer.appendChild(historyCard);
    }

    // Toggle collapse/expand
    historyCard.querySelector('.progress-inline-header').addEventListener('click', function(e) {
      if (e.target.closest('.btn-resume-task')) return;
      const st = historyCard.querySelector('.progress-inline-steps');
      const ic = historyCard.querySelector('.progress-toggle-icon');
      const isCurrentlyCollapsed = st.style.maxHeight === '0px';
      if (isCurrentlyCollapsed) {
        st.style.maxHeight = st.scrollHeight + 'px';
      } else {
        // If it was 'none', first set to scrollHeight so transition works
        if (st.style.maxHeight === 'none') st.style.maxHeight = st.scrollHeight + 'px';
        // Force reflow
        st.offsetHeight;
        st.style.maxHeight = '0px';
      }
      ic.classList.toggle('collapsed', !isCurrentlyCollapsed);
      ic.classList.toggle('expanded', isCurrentlyCollapsed);
      ic.textContent = isCurrentlyCollapsed ? '▾' : '▸';
    });

    // Wire this card as the active progress card when resuming
    if (data.task_status === 'resuming') {
      historyCard.classList.remove('completed');
      const titleEl = historyCard.querySelector('.progress-title');
      if (titleEl) titleEl.textContent = `🐼 执行中 · ${stepCount} 步`;
      progressInline = historyCard;
      progressStepsEl = stepsEl;
      progressStepCount = stepCount;
      var rb = historyCard.querySelector('.btn-resume-task');
      if (rb) rb.remove();
    }

    // Resume button handler (for interrupted tasks, not resuming)
    const resumeBtn = historyCard.querySelector('.btn-resume-task');
    if (resumeBtn) {
      resumeBtn.addEventListener('click', function(e) {
        e.stopPropagation();
        const taskId = resumeBtn.dataset.taskId;
        if (state.ws && state.ws.readyState === WebSocket.OPEN) {
          // Mark card as running immediately — don't remove it
          historyCard.classList.remove('completed');
          var t = historyCard.querySelector('.progress-title');
          if (t) t.textContent = `🐼 执行中 · ${stepCount} 步`;
          progressInline = historyCard;
          progressStepsEl = stepsEl;
          progressStepCount = stepCount;
          resumeBtn.remove();
          // Hide the steps (collapsed for resume)
          var st = historyCard.querySelector('.progress-inline-steps');
          if (st) st.style.maxHeight = 'none';
          state.ws.send(JSON.stringify({ type: 'resume', task_id: parseInt(taskId) }));
        }
      });
    }

    scrollToBottom();
  }

  // =============================================
  // Retry Bar
  // =============================================
  function showRetryBar(originalQuery, errorContent) {
    const retryBar = document.createElement('div');
    retryBar.className = 'retry-bar';
    retryBar.innerHTML = `
      <button class="retry-btn" title="重试">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="23 4 23 10 17 10"></polyline><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"></path></svg>重试
      </button>
      <button class="retry-btn retry-btn-alt" title="继续">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polygon points="5 3 19 12 5 21 5 3"></polygon></svg>继续
      </button>`;
    chatContainer.appendChild(retryBar);
    scrollToBottom();
    retryBar.querySelector('.retry-btn').addEventListener('click', () => {
      retryBar.remove();
      if (state.ws && state.isConnected && originalQuery) {
        state.isAgentThinking = true;
        updateInputState();
        state.ws.send(JSON.stringify({ type: "retry", query: originalQuery }));
      }
    });
    retryBar.querySelector('.retry-btn-alt').addEventListener('click', () => {
      retryBar.remove();
      if (state.ws && state.isConnected) {
        const msg = '上一步操作失败了，请跳过这一步，继续完成剩余的任务。';
        appendMessage(msg, 'user');
        state.isAgentThinking = true;
        updateInputState();
        state.ws.send(JSON.stringify({ query: msg }));
      }
    });
  }

  // =============================================
  // Permission Error Detection
  // =============================================
  function checkPermissionError(errorText) {
    const lower = errorText.toLowerCase();
    const permKeywords = ['permission', 'denied', '权限', 'access denied', 'not permitted',
      'operation not permitted', 'eacces', 'eperm', 'chmod'];
    if (!permKeywords.some(kw => lower.includes(kw))) return;

    const modal = document.getElementById('permission-modal');
    const descEl = document.getElementById('perm-modal-desc');
    const codeEl = document.getElementById('perm-modal-code');
    if (!modal) return;

    const pathMatch = errorText.match(/(?:\/[\w\-\.\/]+)+/);
    const path = pathMatch ? pathMatch[0] : '目标路径';
    descEl.textContent = '该操作因系统权限不足而失败。请在终端中执行以下命令后重试：';
    codeEl.textContent = `# macOS / Linux\nsudo chmod -R 755 ${path}\n\n# 或授予当前用户所有权\nsudo chown -R $(whoami) ${path}`;
    modal.classList.add('active');
  }

  // =============================================
  // UI Helpers
  // =============================================
  function appendMessage(content, role, images) {
    const messageDiv = document.createElement('div');
    messageDiv.className = `message ${role}`;

    let avatarSvg = '';
    if (role === 'user') {
      avatarSvg = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path><circle cx="12" cy="7" r="4"></circle></svg>`;
    } else {
      avatarSvg = `<svg width="20" height="20" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
        <ellipse cx="25" cy="22" rx="15" ry="16" fill="#1a1a1a"/>
        <ellipse cx="75" cy="22" rx="15" ry="16" fill="#1a1a1a"/>
        <ellipse cx="50" cy="55" rx="42" ry="40" fill="#f5f5f0"/>
        <ellipse cx="35" cy="50" rx="14" ry="12" fill="#1a1a1a" transform="rotate(-6 35 50)"/>
        <ellipse cx="65" cy="50" rx="14" ry="12" fill="#1a1a1a" transform="rotate(6 65 50)"/>
        <circle cx="37" cy="47" r="5.5" fill="#fff"/>
        <circle cx="63" cy="47" r="5.5" fill="#fff"/>
        <circle cx="39" cy="46" r="2.2" fill="#1a1a1a"/>
        <circle cx="61" cy="46" r="2.2" fill="#1a1a1a"/>
        <circle cx="40" cy="45" r="0.8" fill="#fff"/>
        <circle cx="62" cy="45" r="0.8" fill="#fff"/>
        <ellipse cx="50" cy="60" rx="4" ry="3" fill="#1a1a1a"/>
        <path d="M46 65 C 49 68, 51 68, 54 65" stroke="#1a1a1a" stroke-width="2.5" stroke-linecap="round" fill="none"/></svg>`;
    }

    let formattedContent = content;
    if (role === 'agent' || role === 'system') {
      formattedContent = marked.parse(content);
    }

    let imagesHtml = '';
    if (images && images.length > 0) {
      imagesHtml = '<div style="display:flex; gap:6px; flex-wrap:wrap; margin-bottom:6px;">' +
        images.map(url =>
          `<img src="${url}" style="max-width:200px; max-height:200px; border-radius:8px; border:1px solid var(--border-color); object-fit:contain;">`
        ).join('') + '</div>';
    }

    messageDiv.innerHTML = `<div class="avatar">${avatarSvg}</div><div class="content">${imagesHtml}${formattedContent}</div>`;
    chatContainer.appendChild(messageDiv);
    // Cap chat messages at 100, remove oldest 50 beyond limit
    const msgs = chatContainer.querySelectorAll('.message');
    if (msgs.length > 100) {
      for (let i = 0; i < 50; i++) {
        if (msgs[i] && msgs[i].parentNode) msgs[i].remove();
      }
    }
    messageDiv.querySelectorAll('pre code').forEach((block) => { hljs.highlightElement(block); });
    scrollToBottom();
  }

  // Click delegation for task:// and switch:view/ links in chat messages
  chatContainer.addEventListener('click', function(e) {
    const link = e.target.closest('a[href^="task://"]');
    if (link) {
      e.preventDefault();
      const taskId = link.getAttribute('href').replace('task://', '');
      switchView('tasks');
      setTimeout(function() {
        const openFn = window.openTaskDetail;
        if (typeof openFn === 'function') openFn(parseInt(taskId));
      }, 300);
      return;
    }
    const viewLink = e.target.closest('a[href^="switch:view/"]');
    if (viewLink) {
      e.preventDefault();
      const viewId = viewLink.getAttribute('href').replace('switch:view/', '');
      switchView(viewId);
    }
  });

  window.appendMessage = appendMessage;

  let currentStatusBubble = null;

  function showThinkingStatus(text) {
    if (!currentStatusBubble) {
      currentStatusBubble = document.createElement('div');
      currentStatusBubble.className = 'status-bubble';
      // Random panda animation: 0 = eating bamboo, 1 = rolling
      const animClass = Math.random() < 0.5 ? 'panda-eat' : 'panda-roll';
      currentStatusBubble.innerHTML = `
        <div class="panda-thinking-icon ${animClass}">
          <svg width="32" height="32" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
            <ellipse cx="25" cy="22" rx="15" ry="16" fill="#1a1a1a"/>
            <ellipse cx="75" cy="22" rx="15" ry="16" fill="#1a1a1a"/>
            <ellipse cx="50" cy="55" rx="42" ry="40" fill="#f5f5f0"/>
            <ellipse cx="35" cy="50" rx="14" ry="12" fill="#1a1a1a" transform="rotate(-6 35 50)"/>
            <ellipse cx="65" cy="50" rx="14" ry="12" fill="#1a1a1a" transform="rotate(6 65 50)"/>
            <circle cx="37" cy="47" r="5.5" fill="#fff"/>
            <circle cx="63" cy="47" r="5.5" fill="#fff"/>
            <circle cx="39" cy="46" r="2.2" fill="#1a1a1a"/>
            <circle cx="61" cy="46" r="2.2" fill="#1a1a1a"/>
            <circle cx="40" cy="45" r="0.8" fill="#fff"/>
            <circle cx="62" cy="45" r="0.8" fill="#fff"/>
            <ellipse cx="50" cy="60" rx="4" ry="3" fill="#1a1a1a"/>
            <path d="M46 65 C 49 68, 51 68, 54 65" stroke="#1a1a1a" stroke-width="2.5" stroke-linecap="round" fill="none"/>
          </svg>
        </div>
        <span>${text}</span>`;
      chatContainer.appendChild(currentStatusBubble);
      scrollToBottom();
    } else {
      currentStatusBubble.querySelector('span').textContent = text;
    }
  }

  function hideThinkingStatus() {
    if (currentStatusBubble) { currentStatusBubble.remove(); currentStatusBubble = null; }
  }

  function scrollToBottom(force) {
    // Only auto-scroll if user is near the bottom (within 200px) or forced
    const threshold = 200;
    const atBottom = chatContainer.scrollTop + chatContainer.clientHeight >= chatContainer.scrollHeight - threshold;
    if (force || atBottom || chatContainer.scrollHeight <= chatContainer.clientHeight) {
      chatContainer.scrollTo({ top: chatContainer.scrollHeight, behavior: 'smooth' });
      hideScrollHint();
    } else {
      showScrollHint();
    }
  }

  let scrollHintEl = null;
  function showScrollHint() {
    if (scrollHintEl) return;
    scrollHintEl = document.createElement('div');
    scrollHintEl.className = 'scroll-hint';
    scrollHintEl.innerHTML = '<span>↓ 新消息</span>';
    scrollHintEl.addEventListener('click', () => scrollToBottom(true));
    document.getElementById('chat-body')?.appendChild(scrollHintEl);
  }
  function hideScrollHint() {
    if (scrollHintEl) { scrollHintEl.remove(); scrollHintEl = null; }
  }
  // Reset scroll hint when user manually scrolls
  chatContainer.addEventListener('scroll', function() {
    const threshold = 200;
    if (chatContainer.scrollTop + chatContainer.clientHeight >= chatContainer.scrollHeight - threshold) {
      hideScrollHint();
    }
  });

  function updateInputState() {
    messageInput.disabled = false;
    if (state.isAgentThinking) {
      sendBtn.style.display = 'flex';
      sendBtn.title = '发送';
      if (stopBtn) { stopBtn.style.display = 'flex'; stopBtn.disabled = false; stopBtn.opacity = '1'; }
    } else {
      sendBtn.title = '';
      sendBtn.style.display = 'flex';
      if (stopBtn) stopBtn.style.display = 'none';
      if (state.isConnected) messageInput.focus();
    }
    sendBtn.disabled = !state.isConnected || messageInput.value.trim() === '';
  }

  // =============================================
  // Image Upload / Paste
  // =============================================
  function addPendingImage(dataUrl) {
    if (state.pendingImages.length >= 5) { showStatus('⚠️ 最多添加 5 张图片', 'error'); return; }
    state.pendingImages.push(dataUrl);
    renderImagePreviews();
  }

  window.removePendingImage = function(index) {
    state.pendingImages.splice(index, 1);
    renderImagePreviews();
  };

  function renderImagePreviews() {
    if (!imagePreviewBar) return;
    if (state.pendingImages.length === 0) {
      imagePreviewBar.style.display = 'none';
      imagePreviewBar.innerHTML = '';
      return;
    }
    imagePreviewBar.style.display = 'flex';
    imagePreviewBar.innerHTML = state.pendingImages.map((url, i) => `
      <div style="position:relative; width:48px; height:48px; border-radius:6px; overflow:hidden; border:1px solid var(--border-color); flex-shrink:0;">
        <img src="${url}" style="width:100%; height:100%; object-fit:cover;">
        <button onclick="removePendingImage(${i})" style="position:absolute; top:0; right:0; background:rgba(0,0,0,0.5); color:#fff; border:none; width:16px; height:16px; font-size:10px; line-height:16px; cursor:pointer; padding:0; border-radius:0 0 0 4px;">&times;</button>
      </div>
    `).join('');
  }

  function handleImageFiles(files) {
    for (const file of files) {
      if (!file.type.startsWith('image/')) continue;
      const reader = new FileReader();
      reader.onload = () => addPendingImage(reader.result);
      reader.readAsDataURL(file);
    }
  }

  messageInput.addEventListener('paste', (e) => {
    const items = e.clipboardData?.items;
    if (!items) return;
    for (const item of items) {
      if (item.type.startsWith('image/')) {
        e.preventDefault();
        const file = item.getAsFile();
        const reader = new FileReader();
        reader.onload = () => addPendingImage(reader.result);
        reader.readAsDataURL(file);
      }
    }
  });

  if (imageBtn) imageBtn.addEventListener('click', () => imageFileInput.click());

  if (imageFileInput) {
    imageFileInput.addEventListener('change', () => {
      handleImageFiles(imageFileInput.files);
      imageFileInput.value = '';
    });
  }

  function handleSend() {
    const text = messageInput.value.trim();
    if ((!text && state.pendingImages.length === 0) || !state.isConnected) return;
    const msgImages = [...state.pendingImages];
    appendMessage(text || '[图片]', 'user', msgImages);
    const agentSelector = document.getElementById('agent-selector');
    const msg = {
      query: text || '请分析这张图片',
      session_id: state.currentSessionId,
      agent_name: agentSelector ? agentSelector.value : 'default'
    };
    if (msgImages.length > 0) {
      msg.images = msgImages;
      state.pendingImages = [];
      renderImagePreviews();
    }
    state.ws.send(JSON.stringify(msg));
    messageInput.value = '';
    messageInput.style.height = 'auto';
    // If agent is already running, queue as pending message without resetting state
    if (state.isAgentThinking) return;
    // Reset progress state for new turn
    progressInline = null;
    progressStepsEl = null;
    progressSteps = {};
    progressStepCount = 0;
    state.isAgentThinking = true;
    updateInputState();
  }

  // =============================================
  // Voice Interaction
  // =============================================
  const micBtn = document.getElementById('mic-btn');
  let recognition = null;
  let isListening = false;

  if ('webkitSpeechRecognition' in window || 'SpeechRecognition' in window) {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    recognition = new SpeechRecognition();
    recognition.continuous = false;
    recognition.interimResults = false;

    recognition.onstart = () => { 
      isListening = true; 
      micBtn.classList.add('listening'); 
      messageInput.placeholder = "正在聆听中..."; 
    };
    recognition.onresult = (event) => {
      const transcript = event.results[0][0].transcript;
      if (transcript) {
        const start = messageInput.selectionStart;
        const end = messageInput.selectionEnd;
        const text = messageInput.value;
        const before = text.substring(0, start);
        const after  = text.substring(end);
        messageInput.value = before + transcript + after;
        messageInput.selectionStart = messageInput.selectionEnd = start + transcript.length;
        
        // Trigger resize and state update
        messageInput.dispatchEvent(new Event('input'));
        messageInput.focus();
      }
    };
    recognition.onerror = (event) => { 
      console.error("Speech recognition error", event.error);
      if (isListening) { 
        isListening = false; 
        micBtn.classList.remove('listening'); 
        messageInput.placeholder = "告诉我熊猫，你想在电脑上做什么..."; 
      } 
    };
    recognition.onend = () => { 
      if (isListening) { 
        isListening = false; 
        micBtn.classList.remove('listening'); 
        messageInput.placeholder = "告诉我熊猫，你想在电脑上做什么..."; 
        messageInput.focus();
      } 
    };
  } else {
    if (micBtn) micBtn.style.display = 'none';
  }

  if (micBtn) {
    micBtn.addEventListener('click', () => {
      if (!recognition) return;
      if (isListening) { recognition.stop(); }
      else { try { recognition.lang = 'zh-CN'; recognition.start(); } catch (e) { } }
    });
  }

  function speakText(text) {
    if (!('speechSynthesis' in window)) return;
    window.speechSynthesis.cancel();
    const cleanText = text.replace(/[*#`_]|<\/?[\w\s="\/.':;#-]+>/gi, '');
    const utterance = new SpeechSynthesisUtterance(cleanText);
    utterance.lang = 'zh-CN';
    window.speechSynthesis.speak(utterance);
  }

  // =============================================
  // Model Download (called from llama search results)
  // =============================================
  window.startModelDownload = async function(repo, filename, source) {
    source = source || 'huggingface';
    try {
      const res = await fetch('/api/llamacpp/download-from-hf', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ repo_id: repo, filename: filename, source: source })
      });
      const data = await res.json();
      if (data.status === 'started' || data.status === 'downloading') {
        showStatus('📥 下载已开始: ' + filename, 'success');
      } else if (data.status === 'exists') {
        showStatus('✅ 模型文件已存在', 'success');
      } else {
        showStatus('❌ ' + (data.detail || '启动下载失败'), 'error');
      }
      refreshLlamaStatus();
      loadDownloadHistory();
    } catch (e) {
      showStatus('❌ 网络错误', 'error');
    }
  };

  // =============================================
  // Core Event Listeners
  // =============================================
  sendBtn.addEventListener('click', handleSend);

  if (stopBtn) {
    stopBtn.addEventListener('click', () => {
      if (state.ws && state.isConnected && state.isAgentThinking) {
        state.ws.send(JSON.stringify({ type: "interrupt" }));
        if (state.currentTaskId) {
          fetch(`/api/tasks/${state.currentTaskId}/interrupt`, { method: 'POST' }).catch(() => { });
        }
        stopBtn.disabled = true;
        stopBtn.style.opacity = '0.5';
      }
    });
  }

  messageInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend(); }
  });

  messageInput.addEventListener('input', function() {
    this.style.height = 'auto';
    this.style.height = (this.scrollHeight) + 'px';
    updateInputState();
  });

  themeToggle.addEventListener('click', () => {
    const current = htmlElement.getAttribute('data-theme');
    const next = current === 'dark' ? 'light' : 'dark';
    htmlElement.setAttribute('data-theme', next);
    localStorage.setItem('theme', next);
  });

  const savedTheme = localStorage.getItem('theme');
  if (savedTheme) htmlElement.setAttribute('data-theme', savedTheme);

  document.getElementById('new-chat-btn').addEventListener('click', async () => {
    switchView('chat');
    await createSession();
    // createSession -> switchSession already reconnects the WebSocket
  });

  // =============================================
  // Real-time Polling
  // =============================================
  setInterval(() => {
    if (document.getElementById('view-tasks')?.classList.contains('active')) {
      if (typeof loadTasks === 'function') loadTasks();
    }
    if (typeof updateTaskBadge === 'function') updateTaskBadge();
  }, 5000);

  setInterval(() => {
    if (document.getElementById('view-settings')?.classList.contains('active')) {
      refreshLlamaStatus();
      loadDownloadHistory();
    }
  }, 10000);

  // =============================================
  // Fetch Initial Data
  // =============================================
  async function fetchInitialData() {
    try {
      const settingsRes = await fetch('/api/settings');
      if (settingsRes.ok) {
        const data = await settingsRes.json();
        currentModelBadge.textContent = data.default_model || 'gpt-4o';
      }
      await loadSessions();
      const historyRes = await fetch(`/api/history?session_id=${state.currentSessionId}`);
      if (historyRes.ok) {
        const data = await historyRes.json();
        if (data.history && data.history.length > 0) {
          chatContainer.innerHTML = '';
          data.history.forEach(msg => appendMessage(msg.content, msg.role));
        }
      }
    } catch (e) {
      console.error("Failed to load initial data", e);
    }
    console.log('[PERF] fetchInitialData done', (performance.now() - window._perf.start).toFixed(1), 'ms');
  }

  fetchInitialData().then(() => {
    window._perf.connected = performance.now();
    console.log('[PERF] connectWebSocket', (performance.now() - window._perf.start).toFixed(1), 'ms');
    connectWebSocket();
  });
  console.log('[PERF] initApp done', (performance.now() - window._perf.start).toFixed(1), 'ms');
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initApp);
} else {
  initApp();
}

