// =============================================
// Open-AGC Frontend — Main Entry Point
// =============================================
import './style.css';
import { state } from './js/state.js';
import { escapeHtml, showStatus, t, initI18n, formatTimeAgo, formatTime } from './js/utils.js';
import { switchView, initNavigation } from './js/navigation.js';
import { loadPlugins, loadPluginManager, loadMarketplace } from './js/plugins.js';
import { loadSessions, createSession, switchSession, deleteSession, renameSession } from './js/sessions.js';
import { initSettingsListeners, loadSkillsConfig, loadAgents, openAIDesignModal, closeAIDesignModal, initAIDesignListeners, initSkillsModalListeners } from './js/settings.js';
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
function initApp() {
  initI18n();
  initNavigation();
  loadPlugins();
  initSettingsListeners();
  initSkillsModalListeners();
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

  function handleLlamaDownloadProgress(data) {
    const ratio = data.progress || 0;
    const pctText = Math.round(ratio * 100) + '%';
    const historyContainer = document.getElementById('download-history-container') || document.getElementById('downloads-view-container');
    if (historyContainer) {
      const downloadingItems = historyContainer.querySelectorAll('.download-item');
      downloadingItems.forEach(item => {
        const statusBadge = item.querySelector('.download-status-badge');
        const isDownloading = statusBadge && statusBadge.classList.contains('downloading');
        if (isDownloading) {
          const bar = item.querySelector('.download-item-progress-bar');
          if (bar) bar.style.width = Math.max(ratio * 100, 0) + '%';
          const metaSpans = item.querySelectorAll('.download-item-meta span');
          if (metaSpans.length >= 2) metaSpans[1].textContent = pctText;
        }
      });
    }

    const banner = document.getElementById('global-download-banner');
    const bannerLabel = document.getElementById('global-download-label');
    const bannerPct = document.getElementById('global-download-pct');
    const bannerBar = document.getElementById('global-download-bar');
    const bannerIcon = document.getElementById('global-download-icon');

    if (data.stage === 'complete') {
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
      setTimeout(() => { if (banner) banner.style.display = 'none'; }, 4000);
      refreshLlamaStatus();
      loadDownloadHistory();
    } else if (data.stage === 'error') {
      if (banner) {
        banner.style.display = 'block';
        bannerIcon.textContent = '❌';
        bannerLabel.textContent = data.error || data.label || '下载失败';
        bannerPct.textContent = '✗';
        bannerBar.style.width = '0%';
        bannerBar.style.background = 'var(--error)';
      }
      showStatus('❌ ' + (data.error || '下载失败'), 'error');
      setTimeout(() => { if (bannerBar) bannerBar.style.background = 'var(--theme-color)'; }, 8000);
      loadDownloadHistory();
    } else {
      if (banner) {
        banner.style.display = 'block';
        bannerIcon.textContent = data.stage === 'extracting' ? '📦' : '📥';
        bannerLabel.textContent = data.label || '下载中...';
      }
      if (bannerPct) bannerPct.textContent = pctText;
      if (bannerBar) { bannerBar.style.width = (ratio * 100) + '%'; bannerBar.style.background = 'var(--theme-color)'; }
    }
  }

  window.handleLlamaDownloadProgress = handleLlamaDownloadProgress;

  // =============================================
  // WebSocket & Agent Communication
  // =============================================
  let wsReconnectAttempt = 0;
  let wsReconnectTimer = null;

  function connectWebSocket() {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${window.location.host}/ws?session_id=${state.currentSessionId}`;
    state.ws = new WebSocket(wsUrl);

    state.ws.onopen = () => {
      state.isConnected = true;
      wsReconnectAttempt = 0;
      updateInputState();
      refreshLlamaStatus();
      loadDownloadHistory();
    };

    state.ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      handleServerMessage(data);
    };

    state.ws.onclose = () => {
      state.isConnected = false;
      const delay = Math.min(1000 * Math.pow(2, wsReconnectAttempt), 30000);
      wsReconnectAttempt++;
      wsReconnectTimer = setTimeout(connectWebSocket, delay);
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

  function handleServerMessage(data) {
    const isBackground = data.background === true;

    if (!isBackground && data.type !== 'llamacpp_download'
      && document.querySelector('.view.active')?.id !== 'view-chat') {
      switchView('chat');
    }

    if (data.type === 'status') {
      if (!isBackground) showThinkingStatus(t('agent_thinking'));
    } else if (data.type === 'progress') {
      if (data.task_id && !isBackground) state.currentTaskId = data.task_id;
      if (!isBackground) handleProgressEvent(data);
      if (isBackground) updateTaskBadge();
    } else if (data.type === 'message') {
      if (!isBackground) { hideThinkingStatus(); hideProgressContainer(); }
      appendMessage(data.content, data.role || 'agent');
      if (!isBackground && state.wasVoiceQuery) { speakText(data.content); state.wasVoiceQuery = false; }
      if (!isBackground) { state.isAgentThinking = false; state.currentTaskId = null; updateInputState(); }
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
    } else if (data.type === 'llamacpp_download') {
      handleLlamaDownloadProgress(data);
    }
  }

  // =============================================
  // Progress Tracking UI
  // =============================================
  let progressContainer = null;
  let progressSteps = {};
  let progressStepData = {};

  function ensureProgressContainer() {
    if (!progressContainer) {
      hideThinkingStatus();
      progressContainer = document.createElement('div');
      progressContainer.className = 'progress-container';
      progressContainer.innerHTML = `
        <div class="progress-header">
          <div class="progress-spinner"></div>
          <span class="progress-title">${t('working')}</span>
        </div>
        <div class="progress-steps"></div>`;
      chatContainer.appendChild(progressContainer);
      scrollToBottom();
    }
    return progressContainer;
  }

  function handleProgressEvent(data) {
    const event = data.event;

    if (event === 'thinking') {
      if (data.content) {
        ensureProgressContainer();
        const stepsEl = progressContainer.querySelector('.progress-steps');
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
          thinkEl.addEventListener('click', () => showStepDetail(thinkKey));
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

    if (event === 'model_switched') {
      ensureProgressContainer();
      const stepsEl = progressContainer.querySelector('.progress-steps');
      const switchNote = document.createElement('div');
      switchNote.className = 'progress-step model-switch';
      switchNote.innerHTML = `<span class="step-icon">🔄</span><span class="step-text">模型已切换: ${data.from} → <strong>${data.to}</strong></span>`;
      stepsEl.appendChild(switchNote);
      scrollToBottom();
      return;
    }

    if (event === 'tool_start') {
      ensureProgressContainer();
      const stepsEl = progressContainer.querySelector('.progress-steps');
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

      progressStepData[data.step] = {
        type: 'tool', step: data.step, tool: data.tool,
        tool_label: data.tool_label || data.tool,
        args_preview: data.args_preview || '', full_args: data.args_preview || '',
        result_preview: '', full_result: '', success: null, status: 'running'
      };
      stepEl.addEventListener('click', () => showStepDetail(data.step));
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
    if (progressContainer) {
      progressContainer.classList.add('completed');
      const titleEl = progressContainer.querySelector('.progress-title');
      if (titleEl) titleEl.textContent = t('done');
      const spinnerEl = progressContainer.querySelector('.progress-spinner');
      if (spinnerEl) spinnerEl.style.display = 'none';
      progressContainer = null;
      progressSteps = {};
    }
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
        <circle cx="50" cy="50" r="45" fill="#f4f6f5" stroke="#1a1c1e" stroke-width="6"/>
        <circle cx="25" cy="25" r="14" fill="#1a1c1e"/><circle cx="75" cy="25" r="14" fill="#1a1c1e"/>
        <circle cx="35" cy="45" r="10" fill="#1a1c1e"/><circle cx="65" cy="45" r="10" fill="#1a1c1e"/>
        <circle cx="35" cy="43" r="3.5" fill="#fff"/><circle cx="65" cy="43" r="3.5" fill="#fff"/>
        <ellipse cx="50" cy="62" rx="5" ry="3.5" fill="#1a1c1e"/>
        <path d="M44 68 C 50 73, 50 73, 56 68" stroke="#1a1c1e" stroke-width="3" stroke-linecap="round"/></svg>`;
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

  window.appendMessage = appendMessage;

  let currentStatusBubble = null;

  function showThinkingStatus(text) {
    if (!currentStatusBubble) {
      currentStatusBubble = document.createElement('div');
      currentStatusBubble.className = 'status-bubble';
      currentStatusBubble.innerHTML = `<div class="spinner"></div><span>${text}</span>`;
      chatContainer.appendChild(currentStatusBubble);
      scrollToBottom();
    } else {
      currentStatusBubble.querySelector('span').textContent = text;
    }
  }

  function hideThinkingStatus() {
    if (currentStatusBubble) { currentStatusBubble.remove(); currentStatusBubble = null; }
  }

  function scrollToBottom() {
    chatContainer.scrollTo({ top: chatContainer.scrollHeight, behavior: 'smooth' });
  }

  function updateInputState() {
    if (state.isAgentThinking) {
      messageInput.disabled = true;
      sendBtn.style.display = 'none';
      if (stopBtn) { stopBtn.style.display = 'flex'; stopBtn.disabled = false; stopBtn.style.opacity = '1'; }
    } else {
      messageInput.disabled = false;
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
    if ((!text && state.pendingImages.length === 0) || !state.isConnected || state.isAgentThinking) return;
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

    recognition.onstart = () => { isListening = true; micBtn.classList.add('listening'); messageInput.placeholder = "请说话..."; };
    recognition.onresult = (event) => {
      messageInput.value = event.results[0][0].transcript;
      state.wasVoiceQuery = true;
      handleSend();
    };
    recognition.onerror = () => { if (isListening) { isListening = false; micBtn.classList.remove('listening'); } };
    recognition.onend = () => { if (isListening) { isListening = false; micBtn.classList.remove('listening'); } };
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
  window.startModelDownload = async function(repo, filename) {
    try {
      const res = await fetch('/api/llamacpp/download-model', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ repo, filename })
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
    state.ws.close();
    connectWebSocket();
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
  }

  fetchInitialData();
  connectWebSocket();
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initApp);
} else {
  initApp();
}

