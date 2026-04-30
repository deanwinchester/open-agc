document.addEventListener('DOMContentLoaded', () => {
    // ==========================================
    // DOM Elements
    // ==========================================
    const chatContainer = document.getElementById('chat-container');
    const messageInput = document.getElementById('message-input');
    const imageFileInput = document.getElementById('image-file-input');
    const imageBtn = document.getElementById('image-btn');
    const imagePreviewBar = document.getElementById('image-preview-bar');
    let pendingImages = [];  // Array of base64 data URLs
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

    // State
    let ws = null;
    let isConnected = false;
    let isAgentThinking = false;
    let currentLang = 'zh-CN';
    let wasVoiceQuery = false;
    let currentTaskId = null; // Track current task from WebSocket

    function showStatus(message, type) {
        let el = document.getElementById('global-status-toast');
        if (!el) {
            el = document.createElement('div');
            el.id = 'global-status-toast';
            el.style.cssText = 'position:fixed; top:1rem; left:50%; transform:translateX(-50%); z-index:9999;'
                + 'padding:0.6rem 1.2rem; border-radius:8px; font-size:0.85rem; font-weight:500;'
                + 'pointer-events:none; transition:opacity 0.3s; white-space:nowrap;';
            document.body.appendChild(el);
        }
        el.textContent = message;
        el.style.background = type === 'success' ? 'var(--success)' : type === 'error' ? 'var(--error)' : 'var(--surface)';
        el.style.color = type === 'success' || type === 'error' ? '#fff' : 'var(--text-primary)';
        el.style.border = type === 'success' || type === 'error' ? 'none' : '1px solid var(--border-color)';
        el.style.opacity = '1';
        clearTimeout(el._timeout);
        el._timeout = setTimeout(() => { el.style.opacity = '0'; }, 3000);
    }

    // ==========================================
    // Navigation System
    // ==========================================
    const navItems = document.querySelectorAll('.nav-item[data-view]');
    const views = document.querySelectorAll('.view');

    function switchView(viewId) {
        views.forEach(v => v.classList.remove('active'));
        navItems.forEach(n => n.classList.remove('active'));

        const targetView = document.getElementById('view-' + viewId);
        const targetNav = document.querySelector(`.nav-item[data-view="${viewId}"]`);

        if (targetView) targetView.classList.add('active');
        if (targetNav) targetNav.classList.add('active');

        // Load view-specific data
        if (viewId === 'settings-models') { loadSettingsConfig(); loadDownloadHistory(); }
        if (viewId === 'settings-skills') loadSkillsConfig();
        if (viewId === 'settings-mcp') loadMcpConfig();
        if (viewId === 'tasks') loadTasks();
        if (viewId === 'downloads') loadDownloadsView();
        if (viewId === 'training-designer') { loadModelConfigs(); checkAndOfferTrainingInstall(); }
        if (viewId === 'training-datasets') { loadDatasets(); checkAndOfferTrainingInstall(); }
        if (viewId === 'training-finetune') { loadBaseModels(); loadDatasets(); checkAndOfferTrainingInstall(); }
        if (viewId === 'training-history') { loadTrainingRuns(); checkAndOfferTrainingInstall(); }
        if (viewId === 'training-monitor') { initTrainingMonitor(); checkAndOfferTrainingInstall(); }
        if (viewId === 'training-benchmark') { loadBenchmarkView(); checkAndOfferTrainingInstall(); }
    }

    navItems.forEach(item => {
        item.addEventListener('click', () => {
            switchView(item.dataset.view);
        });
    });

    // ==========================================
    // i18n
    // ==========================================
    const translations = {
        'zh-CN': {
            agent_thinking: '熊猫正在思考对策...',
            agent_error: '哎呀，熊猫摔了一跤 (发生错误)',
            working: '🐼 执行中...',
            done: '✨ 执行完成'
        },
        'en': {
            agent_thinking: 'Panda is thinking...',
            agent_error: 'Panda Encountered an Error',
            working: '🐼 Working...',
            done: '✨ Done'
        }
    };

    function t(key) {
        return (translations[currentLang] || translations['en'])[key] || key;
    }

    function initI18n() {
        const userLang = navigator.language || navigator.userLanguage;
        if (userLang.startsWith('zh')) {
            currentLang = 'zh-CN';
        } else {
            currentLang = 'en';
        }
    }

    // ==========================================
    // Fetch Initial Data
    // ==========================================
    async function fetchInitialData() {
        try {
            const settingsRes = await fetch('/api/settings');
            if (settingsRes.ok) {
                const data = await settingsRes.json();
                currentModelBadge.textContent = data.default_model || 'gpt-4o';
            }

            const historyRes = await fetch('/api/history');
            if (historyRes.ok) {
                const data = await historyRes.json();
                if (data.history && data.history.length > 0) {
                    chatContainer.innerHTML = '';
                    data.history.forEach(msg => {
                        appendMessage(msg.content, msg.role);
                    });
                }
            }
        } catch (e) {
            console.error("Failed to load initial data", e);
        }
    }

    // ==========================================
    // WebSocket & Chat Logic
    // ==========================================
    function connectWebSocket() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws`;
        ws = new WebSocket(wsUrl);

        ws.onopen = () => {
            isConnected = true;
            updateInputState();
            // Restore download progress state on reconnect
            refreshLlamaStatus();
            loadDownloadHistory();
        };

        ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            handleServerMessage(data);
        };

        ws.onclose = () => {
            isConnected = false;
            setTimeout(connectWebSocket, 3000);
            updateInputState();
        };

        ws.onerror = (error) => {
            console.error('WebSocket Error:', error);
        };
    }

    function handleServerMessage(data) {
        const isBackground = data.background === true;

        // Auto-switch to chat view when receiving non-background messages
        // Don't auto-switch for download progress — user may be managing downloads in settings
        if (!isBackground && data.type !== 'llamacpp_download' && data.type !== 'training_install_progress'
            && data.type !== 'benchmark_progress' && data.type !== 'benchmark_complete'
            && document.querySelector('.view.active')?.id !== 'view-chat') {
            switchView('chat');
        }

        if (data.type === 'status') {
            if (!isBackground) showThinkingStatus(t('agent_thinking'));
        }
        else if (data.type === 'progress') {
            // Track task_id from progress events
            if (data.task_id && !isBackground) currentTaskId = data.task_id;
            if (!isBackground) handleProgressEvent(data);
            // Background progress: just update task badge count
            if (isBackground) updateTaskBadge();
        }
        else if (data.type === 'message') {
            if (!isBackground) {
                hideThinkingStatus();
                hideProgressContainer();
            }
            appendMessage(data.content, data.role || 'agent');
            if (!isBackground && wasVoiceQuery) {
                speakText(data.content);
                wasVoiceQuery = false;
            }
            if (!isBackground) {
                isAgentThinking = false;
                currentTaskId = null;
                updateInputState();
            }
            updateTaskBadge();
        }
        else if (data.type === 'error') {
            if (!isBackground) {
                hideThinkingStatus();
                hideProgressContainer();
                showRetryBar(data.original_query || '', data.content);
                checkPermissionError(data.content);
                isAgentThinking = false;
                currentTaskId = null;
                updateInputState();
            } else {
                appendMessage(`**后台任务错误**: ${data.content}`, 'system');
            }
            updateTaskBadge();
        }
        else if (data.type === 'llamacpp_download') {
            handleLlamaDownloadProgress(data);
        }
        else if (data.type === 'training_progress') {
            handleTrainingProgress(data);
        }
        else if (data.type === 'training_step_paused') {
            handleTrainingStepPaused(data);
        }
        else if (data.type === 'training_complete') {
            handleTrainingComplete(data);
            loadTrainingRuns();
        }
        else if (data.type === 'training_error') {
            handleTrainingError(data);
            loadTrainingRuns();
        }
        else if (data.type === 'training_install_progress') {
            handleInstallProgress(data);
        }
        else if (data.type === 'benchmark_progress') {
            handleBenchmarkProgress(data);
        }
        else if (data.type === 'benchmark_complete') {
            handleBenchmarkComplete(data);
        }
    }

    // ==========================================
    // Llama Download Progress
    // ==========================================
    // Download file path for resume support (set when download begins)
    let downloadResumeInfo = null;

    function handleLlamaDownloadProgress(data) {
        // --- Download history in-place update ---
        const ratio = data.progress || 0;
        const pctText = Math.round(ratio * 100) + '%';
        const historyContainer = document.getElementById('download-history-container');
        if (historyContainer) {
            // Update active download items in-place for smooth progress
            const downloadingItems = historyContainer.querySelectorAll('.download-item');
            downloadingItems.forEach(item => {
                const statusBadge = item.querySelector('.download-status-badge');
                const isDownloading = statusBadge && statusBadge.classList.contains('downloading');
                if (isDownloading) {
                    const bar = item.querySelector('.download-item-progress-bar');
                    if (bar) { bar.style.width = Math.max(ratio * 100, 0) + '%'; }
                    // Update percentage in meta
                    const metaSpans = item.querySelectorAll('.download-item-meta span');
                    if (metaSpans.length >= 2) metaSpans[1].textContent = pctText;
                }
            });
        }

        // --- Global banner ---
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
            datasetsLoaded = false;
            if (document.getElementById('view-training-datasets')?.classList.contains('active')) loadDatasets();
        } else if (data.stage === 'error') {
            if (banner) {
                banner.style.display = 'block';
                bannerIcon.textContent = '❌';
                bannerLabel.textContent = data.error || data.label || '下载失败';
                bannerPct.textContent = '✗';
                bannerBar.style.width = '0%';
                bannerBar.style.background = 'var(--error)';
            }
            if (downloadResumeInfo) {
                showStatus('❌ 下载中断，已保存进度，可重新点击下载按钮续传', 'error');
            } else {
                showStatus('❌ ' + (data.error || '下载失败'), 'error');
            }
            setTimeout(() => { if (bannerBar) bannerBar.style.background = 'var(--theme-color)'; }, 8000);
            loadDownloadHistory();
        } else {
            // Downloading or extracting
            if (banner) {
                banner.style.display = 'block';
                bannerIcon.textContent = data.stage === 'extracting' ? '📦' : '📥';
                bannerLabel.textContent = data.label || '下载中...';
            }
            if (bannerPct) bannerPct.textContent = pctText;
            if (bannerBar) { bannerBar.style.width = (ratio * 100) + '%'; bannerBar.style.background = 'var(--theme-color)'; }
        }
    }

    // Close button for global download banner
    document.getElementById('global-download-close')?.addEventListener('click', () => {
        const banner = document.getElementById('global-download-banner');
        if (banner) banner.style.display = 'none';
    });

    // ==========================================
    // Retry Bar
    // ==========================================
    function showRetryBar(originalQuery, errorContent) {
        const retryBar = document.createElement('div');
        retryBar.className = 'retry-bar';
        retryBar.innerHTML = `
            <button class="retry-btn" title="重试">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                    <polyline points="23 4 23 10 17 10"></polyline>
                    <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"></path>
                </svg>
                重试
            </button>
            <button class="retry-btn retry-btn-alt" title="继续">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                    <polygon points="5 3 19 12 5 21 5 3"></polygon>
                </svg>
                继续
            </button>
        `;
        chatContainer.appendChild(retryBar);
        scrollToBottom();

        retryBar.querySelector('.retry-btn').addEventListener('click', () => {
            retryBar.remove();
            if (ws && isConnected && originalQuery) {
                isAgentThinking = true;
                updateInputState();
                ws.send(JSON.stringify({ type: "retry", query: originalQuery }));
            }
        });

        retryBar.querySelector('.retry-btn-alt').addEventListener('click', () => {
            retryBar.remove();
            if (ws && isConnected) {
                const msg = '上一步操作失败了，请跳过这一步，继续完成剩余的任务。';
                appendMessage(msg, 'user');
                isAgentThinking = true;
                updateInputState();
                ws.send(JSON.stringify({ query: msg }));
            }
        });
    }

    // ==========================================
    // Progress Tracking UI with Clickable Steps
    // ==========================================
    let progressContainer = null;
    let progressSteps = {};
    let progressStepData = {}; // Store full step data for detail panel

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
                <div class="progress-steps"></div>
            `;
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
                        </div>
                    `;
                    stepsEl.appendChild(thinkEl);

                    // Store thinking data
                    const thinkKey = `thought-${data.iteration || 0}`;
                    progressStepData[thinkKey] = {
                        type: 'thinking',
                        label: '思考过程',
                        content: data.content,
                        iteration: data.iteration
                    };
                    thinkEl.addEventListener('click', () => showStepDetail(thinkKey));
                } else {
                    const detailEl = thinkEl.querySelector('.step-detail');
                    if (detailEl) detailEl.textContent = data.content;
                    const thinkKey = `thought-${data.iteration || 0}`;
                    if (progressStepData[thinkKey]) {
                        progressStepData[thinkKey].content = data.content;
                    }
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
            switchNote.innerHTML = `
                <span class="step-icon">🔄</span>
                <span class="step-text">模型已切换: ${data.from} → <strong>${data.to}</strong></span>
            `;
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
                <span class="step-icon">
                    <div class="step-spinner"></div>
                </span>
                <div class="step-body">
                    <span class="step-label">${data.step}. ${data.tool_label || data.tool}</span>
                    ${data.args_preview ? `<span class="step-detail">${escapeHtml(data.args_preview)}</span>` : ''}
                </div>
            `;
            stepsEl.appendChild(stepEl);
            progressSteps[data.step] = stepEl;

            // Store step data for detail panel
            progressStepData[data.step] = {
                type: 'tool',
                step: data.step,
                tool: data.tool,
                tool_label: data.tool_label || data.tool,
                args_preview: data.args_preview || '',
                full_args: data.args_preview || '',
                result_preview: '',
                full_result: '',
                success: null,
                status: 'running'
            };

            // Click to show detail
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
                    if (detailEl) {
                        detailEl.textContent = data.result_preview;
                    } else {
                        const bodyEl = stepEl.querySelector('.step-body');
                        const newDetail = document.createElement('span');
                        newDetail.className = 'step-detail';
                        newDetail.textContent = data.result_preview;
                        bodyEl.appendChild(newDetail);
                    }
                }
            }

            // Update stored step data
            if (progressStepData[data.step]) {
                progressStepData[data.step].result_preview = data.result_preview || '';
                progressStepData[data.step].full_result = data.result_preview || '';
                progressStepData[data.step].success = data.success;
                progressStepData[data.step].status = data.success ? 'done' : 'failed';
            }

            // Update detail panel if it's showing this step
            if (detailPanel.style.display !== 'none' && detailPanel.dataset.stepKey == data.step) {
                showStepDetail(data.step);
            }

            scrollToBottom();
            return;
        }
    }

    // ==========================================
    // Step Detail Panel (Split View)
    // ==========================================
    function showStepDetail(stepKey) {
        const stepData = progressStepData[stepKey];
        if (!stepData) return;

        detailPanel.dataset.stepKey = stepKey;
        chatBody.classList.add('split-view');
        detailPanel.style.display = 'flex';

        if (stepData.type === 'thinking') {
            detailPanelTitle.textContent = '🧠 思考过程';
            detailPanelBody.innerHTML = `
                <div class="detail-section">
                    <div class="detail-section-title">迭代轮次</div>
                    <div>第 ${stepData.iteration || 1} 轮</div>
                </div>
                <div class="detail-section">
                    <div class="detail-section-title">思考内容</div>
                    <div class="detail-content-block">${escapeHtml(stepData.content)}</div>
                </div>
            `;
        } else {
            const statusClass = stepData.success === true ? 'success' : stepData.success === false ? 'failed' : 'running';
            const statusText = stepData.success === true ? '✅ 成功' : stepData.success === false ? '❌ 失败' : '⏳ 执行中';
            detailPanelTitle.textContent = `步骤 ${stepData.step} 详情`;
            detailPanelBody.innerHTML = `
                <div class="detail-section">
                    <div class="detail-section-title">工具</div>
                    <div><strong>${stepData.tool_label}</strong> <code style="font-size:0.75rem; color:var(--text-secondary)">(${stepData.tool})</code></div>
                </div>
                <div class="detail-section">
                    <div class="detail-section-title">状态</div>
                    <span class="detail-status ${statusClass}">${statusText}</span>
                </div>
                ${stepData.full_args ? `
                <div class="detail-section">
                    <div class="detail-section-title">参数</div>
                    <div class="detail-content-block">${escapeHtml(stepData.full_args)}</div>
                </div>` : ''}
                ${stepData.full_result ? `
                <div class="detail-section">
                    <div class="detail-section-title">结果</div>
                    <div class="detail-content-block">${escapeHtml(stepData.full_result)}</div>
                </div>` : ''}
            `;
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
            // Don't clear progressStepData - keep for detail panel clicks
        }
    }

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // ==========================================
    // Permission error detection
    // ==========================================
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

    // Modal close / copy
    (function initPermModal() {
        const modal = document.getElementById('permission-modal');
        const closeBtn = document.getElementById('perm-modal-close');
        const copyBtn = document.getElementById('perm-modal-copy');
        if (!modal) return;
        closeBtn?.addEventListener('click', () => modal.classList.remove('active'));
        modal.addEventListener('click', (e) => {
            if (e.target === modal) modal.classList.remove('active');
        });
        copyBtn?.addEventListener('click', () => {
            const code = document.getElementById('perm-modal-code')?.textContent || '';
            navigator.clipboard.writeText(code.trim()).then(() => {
                copyBtn.textContent = '✓ 已复制';
                setTimeout(() => copyBtn.textContent = '复制命令', 2000);
            });
        });
    })();

    // ==========================================
    // UI Helpers
    // ==========================================
    function appendMessage(content, role) {
        const messageDiv = document.createElement('div');
        messageDiv.className = `message ${role}`;

        let avatarSvg = '';
        if (role === 'user') {
            avatarSvg = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path><circle cx="12" cy="7" r="4"></circle></svg>`;
        } else {
            avatarSvg = `<svg width="20" height="20" viewBox="0 0 100 100" fill="none" xmlns="http://www.w3.org/2000/svg">
                <circle cx="50" cy="50" r="45" fill="#f4f6f5" stroke="#1a1c1e" stroke-width="6"/>
                <circle cx="25" cy="25" r="14" fill="#1a1c1e"/>
                <circle cx="75" cy="25" r="14" fill="#1a1c1e"/>
                <circle cx="35" cy="45" r="10" fill="#1a1c1e"/>
                <circle cx="65" cy="45" r="10" fill="#1a1c1e"/>
                <circle cx="35" cy="43" r="3.5" fill="#fff"/>
                <circle cx="65" cy="43" r="3.5" fill="#fff"/>
                <ellipse cx="50" cy="62" rx="5" ry="3.5" fill="#1a1c1e"/>
                <path d="M44 68 C 50 73, 50 73, 56 68" stroke="#1a1c1e" stroke-width="3" stroke-linecap="round"/>
            </svg>`;
        }

        let formattedContent = content;
        if (role === 'agent' || role === 'system') {
            formattedContent = marked.parse(content);
        }

        messageDiv.innerHTML = `
            <div class="avatar">${avatarSvg}</div>
            <div class="content">${formattedContent}</div>
        `;

        chatContainer.appendChild(messageDiv);
        messageDiv.querySelectorAll('pre code').forEach((block) => {
            hljs.highlightElement(block);
        });
        scrollToBottom();
    }

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
        if (currentStatusBubble) {
            currentStatusBubble.remove();
            currentStatusBubble = null;
        }
    }

    function scrollToBottom() {
        chatContainer.scrollTo({ top: chatContainer.scrollHeight, behavior: 'smooth' });
    }

    function updateInputState() {
        if (isAgentThinking) {
            messageInput.disabled = true;
            sendBtn.style.display = 'none';
            if (stopBtn) { stopBtn.style.display = 'flex'; stopBtn.disabled = false; stopBtn.style.opacity = '1'; }
        } else {
            messageInput.disabled = false;
            sendBtn.style.display = 'flex';
            if (stopBtn) stopBtn.style.display = 'none';
            if (isConnected) messageInput.focus();
        }
        sendBtn.disabled = !isConnected || messageInput.value.trim() === '';
    }

    // ==========================================
    // Image Upload / Paste
    // ==========================================
    function addPendingImage(dataUrl) {
        if (pendingImages.length >= 5) { showStatus('⚠️ 最多添加 5 张图片', 'error'); return; }
        pendingImages.push(dataUrl);
        renderImagePreviews();
    }

    function removePendingImage(index) {
        pendingImages.splice(index, 1);
        renderImagePreviews();
    }

    function renderImagePreviews() {
        if (!imagePreviewBar) return;
        if (pendingImages.length === 0) {
            imagePreviewBar.style.display = 'none';
            imagePreviewBar.innerHTML = '';
            return;
        }
        imagePreviewBar.style.display = 'flex';
        imagePreviewBar.innerHTML = pendingImages.map((url, i) => `
            <div style="position:relative; width:48px; height:48px; border-radius:6px; overflow:hidden; border:1px solid var(--border-color); flex-shrink:0;">
                <img src="${url}" style="width:100%; height:100%; object-fit:cover;">
                <button onclick="removePendingImage(${i})" style="position:absolute; top:0; right:0; background:rgba(0,0,0,0.5); color:#fff; border:none; width:16px; height:16px; font-size:10px; line-height:16px; cursor:pointer; padding:0; border-radius:0 0 0 4px;">&times;</button>
            </div>
        `).join('');
    }

    // Expose removePendingImage globally for inline onclick
    window.removePendingImage = removePendingImage;

    function handleImageFiles(files) {
        for (const file of files) {
            if (!file.type.startsWith('image/')) continue;
            const reader = new FileReader();
            reader.onload = () => addPendingImage(reader.result);
            reader.readAsDataURL(file);
        }
    }

    // Paste handler — extract images from clipboard
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

    // Image button triggers file input
    if (imageBtn) {
        imageBtn.addEventListener('click', () => imageFileInput.click());
    }

    // File input change handler
    if (imageFileInput) {
        imageFileInput.addEventListener('change', () => {
            handleImageFiles(imageFileInput.files);
            imageFileInput.value = '';
        });
    }

    function handleSend() {
        const text = messageInput.value.trim();
        if ((!text && pendingImages.length === 0) || !isConnected || isAgentThinking) return;
        appendMessage(text || '[图片]', 'user');
        const msg = { query: text || '请分析这张图片' };
        if (pendingImages.length > 0) {
            msg.images = [...pendingImages];
            pendingImages = [];
            renderImagePreviews();
        }
        ws.send(JSON.stringify(msg));
        messageInput.value = '';
        messageInput.style.height = 'auto';
        isAgentThinking = true;
        updateInputState();
    }

    // ==========================================
    // Voice Interaction
    // ==========================================
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
            messageInput.placeholder = "请说话...";
        };

        recognition.onresult = (event) => {
            const transcript = event.results[0][0].transcript;
            messageInput.value = transcript;
            wasVoiceQuery = true;
            handleSend();
        };

        recognition.onerror = () => stopListening();
        recognition.onend = () => stopListening();
    } else {
        if (micBtn) micBtn.style.display = 'none';
    }

    function stopListening() {
        isListening = false;
        if (micBtn) micBtn.classList.remove('listening');
        messageInput.placeholder = "告诉熊猫，你想在电脑上做什么...";
    }

    if (micBtn) {
        micBtn.addEventListener('click', () => {
            if (!recognition) return;
            if (isListening) { recognition.stop(); }
            else {
                try { recognition.lang = 'zh-CN'; recognition.start(); }
                catch (e) { }
            }
        });
    }

    function speakText(text) {
        if (!('speechSynthesis' in window)) return;
        window.speechSynthesis.cancel();
        const cleanText = text.replace(/[*#`_]|<\/?[\w\s="\/.':\;#-]+>/gi, '');
        const utterance = new SpeechSynthesisUtterance(cleanText);
        utterance.lang = 'zh-CN';
        window.speechSynthesis.speak(utterance);
    }

    // ==========================================
    // Settings Management (migrated from settings.js)
    // ==========================================
    const providers = [
        { key: "kimi", label: "Kimi (Moonshot)" },
        { key: "ollama", label: "Ollama (本地/Local)" },
        { key: "llamacpp", label: "Llama.cpp (本地/Local)" },
        { key: "sglang", label: "SGLang (本地/Local)" },
        { key: "vllm", label: "vLLM (本地/Local)" },
        { key: "openai", label: "OpenAI" },
        { key: "anthropic", label: "Anthropic" },
        { key: "gemini", label: "Google Gemini" },
        { key: "deepseek", label: "DeepSeek" },
        { key: "glm", label: "GLM (智谱)" },
        { key: "minimax", label: "MiniMax" }
    ];

    let settingsLoaded = false;

    async function loadSettingsConfig() {
        if (settingsLoaded) return;
        try {
            const res = await fetch('/api/settings');
            const data = await res.json();

            buildApiKeysGrid(data.api_keys_masked || {});
            buildModelSelection(data);

            document.getElementById('sandbox-mode-toggle').checked = data.sandbox_mode ?? true;
            document.getElementById('sandbox-dir-input').value = data.sandbox_dir || '';
            document.getElementById('llamacpp-ctx-size').value = data.llamacpp_ctx_size || 32768;
            document.getElementById('http-proxy-input').value = data.http_proxy || '';
            document.getElementById('heartbeat-toggle').checked = data.heartbeat_enabled ?? false;

            document.getElementById('email-listener-toggle').checked = data.email_listener_enabled ?? false;
            document.getElementById('owner-email-input').value = data.owner_email || '';
            document.getElementById('email-account-input').value = data.email_account || '';
            document.getElementById('email-password-input').placeholder = data.email_password ? '***' : '密码或授权码';
            document.getElementById('email-imap-input').value = data.email_imap_server || '';
            document.getElementById('email-smtp-input').value = data.email_smtp_server || '';

            settingsLoaded = true;
        } catch (err) {
            console.error("Failed to load settings config:", err);
        }
    }

    function buildApiKeysGrid(maskedKeys) {
        const grid = document.getElementById('api-keys-container');
        if (!grid) return;
        grid.innerHTML = '';

        providers.forEach(p => {
            const mask = maskedKeys[p.key] || '';
            const hasSaved = mask.length > 0;
            let placeholder = '请输入密钥...';
            if (p.key === 'ollama') placeholder = '默认 http://localhost:11434';
            if (p.key === 'sglang') placeholder = '默认 http://localhost:8009/v1';
            if (p.key === 'vllm') placeholder = '默认 http://localhost:8000/v1';

            const wrapper = document.createElement('div');
            wrapper.className = 'key-field';
            wrapper.innerHTML = `
                <label>${p.label}</label>
                <div class="key-input-wrapper">
                    <input type="password" id="key-${p.key}" placeholder="${hasSaved ? mask : placeholder}" autocomplete="new-password" spellcheck="false">
                    <button type="button" class="toggle-visibility" data-target="key-${p.key}" title="显示/隐藏">
                        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                            <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"></path>
                            <circle cx="12" cy="12" r="3"></circle>
                        </svg>
                    </button>
                </div>
                <span class="key-status ${hasSaved ? 'saved' : ''}">${hasSaved ? '✓ 已保存' : '未配置'}</span>
            `;
            grid.appendChild(wrapper);
        });

        grid.querySelectorAll('.toggle-visibility').forEach(btn => {
            btn.addEventListener('click', () => {
                const input = document.getElementById(btn.dataset.target);
                input.type = input.type === 'password' ? 'text' : 'password';
            });
        });
    }

    async function buildModelSelection(data) {
        let selectedProvider = 'kimi';
        const dm = data.default_model || '';
        if (dm.startsWith('moonshot/')) selectedProvider = 'kimi';
        else if (dm.startsWith('llamacpp/')) selectedProvider = 'llamacpp';
        else if (dm.startsWith('ollama/')) selectedProvider = 'ollama';
        else if (dm.startsWith('sglang/')) selectedProvider = 'sglang';
        else if (dm.startsWith('vllm/')) selectedProvider = 'vllm';
        else if (dm.startsWith('zai/')) selectedProvider = 'glm';
        else if (dm.startsWith('minimax/')) selectedProvider = 'minimax';
        else if (dm.startsWith('gemini/')) selectedProvider = 'gemini';
        else if (dm.startsWith('deepseek/')) selectedProvider = 'deepseek';
        else if (dm.includes('claude')) selectedProvider = 'anthropic';
        else if (dm.startsWith('gpt')) selectedProvider = 'openai';

        const providerSelect = document.getElementById('provider-select');
        if (providerSelect) providerSelect.value = selectedProvider;

        const fallbackInput = document.getElementById('fallback-models-input');
        if (fallbackInput) fallbackInput.value = (data.fallback_models || []).join(', ');

        await fetchModels(selectedProvider, dm);
    }

    async function fetchModels(provider, modelToSelect = null) {
        const select = document.getElementById('model-name-select');
        if (!select) return;
        select.innerHTML = '<option>加载中...</option>';
        try {
            const res = await fetch('/api/provider-models?provider=' + provider);
            const data = await res.json();
            select.innerHTML = '';
            (data.models || []).forEach(m => {
                const opt = document.createElement('option');
                opt.value = m;
                opt.textContent = m;
                select.appendChild(opt);
            });
            if (modelToSelect && data.models.includes(modelToSelect)) {
                select.value = modelToSelect;
            } else if (data.models.length > 0) {
                select.value = data.models[0];
            }
        } catch (e) {
            select.innerHTML = '<option value="">获取失败</option>';
        }

        // Show/hide pull model section
        const pullGroup = document.getElementById('pull-model-group');
        if (pullGroup) {
            pullGroup.style.display = (provider === 'sglang' || provider === 'ollama') ? 'block' : 'none';
        }
    }

    // Settings event listeners (deferred setup)
    function initSettingsListeners() {
        document.getElementById('provider-select')?.addEventListener('change', (e) => {
            fetchModels(e.target.value);
        });

        document.getElementById('fetch-models-btn')?.addEventListener('click', () => {
            fetchModels(document.getElementById('provider-select').value);
        });

        document.getElementById('pull-model-btn')?.addEventListener('click', async () => {
            const statusEl = document.getElementById('pull-model-status');
            const modelName = document.getElementById('pull-model-input').value.trim();
            const tool = document.getElementById('pull-tool-select').value;
            if (!modelName) { statusEl.textContent = '⚠️ 请先输入模型名称！'; return; }
            try {
                const res = await fetch('/api/sglang/pull', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ model_name: modelName, tool })
                });
                const data = await res.json();
                statusEl.textContent = res.ok ? '✓ ' + (data.message || '已开始下载') : '✗ 拉取失败: ' + data.detail;
                statusEl.style.color = res.ok ? '#10b981' : '#ef4444';
            } catch (e) {
                statusEl.textContent = '✗ 网络错误或超时。';
                statusEl.style.color = '#ef4444';
            }
        });

        // Save settings
        document.getElementById('save-settings-btn')?.addEventListener('click', saveSettings);
        document.getElementById('save-mcp-btn')?.addEventListener('click', saveSettings);
    }

    async function saveSettings() {
        const saveBtn = document.getElementById('save-settings-btn');
        const statusEl = document.getElementById('save-status');

        const payload = {
            api_keys: {},
            default_model: document.getElementById('model-name-select')?.value || 'moonshot/kimi-latest',
            fallback_models: (document.getElementById('fallback-models-input')?.value || '')
                .split(',').map(s => s.trim()).filter(s => s.length > 0),
            disabled_skills: [],
            sandbox_mode: document.getElementById('sandbox-mode-toggle')?.checked ?? true,
            sandbox_dir: document.getElementById('sandbox-dir-input')?.value?.trim() || '',
            llamacpp_ctx_size: parseInt(document.getElementById('llamacpp-ctx-size')?.value) || 32768,
            http_proxy: document.getElementById('http-proxy-input')?.value?.trim() || '',
            heartbeat_enabled: document.getElementById('heartbeat-toggle')?.checked ?? false,
            heartbeat_interval: 60,
            email_listener_enabled: document.getElementById('email-listener-toggle')?.checked ?? false,
            email_account: document.getElementById('email-account-input')?.value?.trim() || '',
            email_password: document.getElementById('email-password-input')?.value || (document.getElementById('email-password-input')?.placeholder === '***' ? '***' : ''),
            email_imap_server: document.getElementById('email-imap-input')?.value?.trim() || '',
            email_smtp_server: document.getElementById('email-smtp-input')?.value?.trim() || '',
            owner_email: document.getElementById('owner-email-input')?.value?.trim() || '',
            agent_profiles: document.getElementById('agents-config-input')?.value?.trim() || '[]',
            mcp_servers: document.getElementById('mcp-config-input')?.value?.trim() || '{}'
        };

        providers.forEach(p => {
            const el = document.getElementById(`key-${p.key}`);
            if (el && el.value && el.value.trim().length > 0) {
                payload.api_keys[p.key] = el.value.trim();
            }
        });

        document.querySelectorAll('.skill-toggle').forEach(cb => {
            if (!cb.checked) payload.disabled_skills.push(cb.dataset.name);
        });

        if (saveBtn) saveBtn.disabled = true;
        if (statusEl) { statusEl.textContent = '保存中...'; statusEl.className = 'save-status'; }

        try {
            const res = await fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });
            const data = await res.json();
            if (data.status === 'success') {
                if (statusEl) { statusEl.textContent = '✓ 保存成功！'; statusEl.className = 'save-status success'; }
                // Update model badge
                currentModelBadge.textContent = payload.default_model;
                settingsLoaded = false; // Force reload next time
                setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 2000);
            } else {
                if (statusEl) { statusEl.textContent = '✗ 保存失败: ' + (data.detail || '未知错误'); statusEl.className = 'save-status error'; }
            }
        } catch (e) {
            if (statusEl) { statusEl.textContent = '✗ 网络错误'; statusEl.className = 'save-status error'; }
        } finally {
            if (saveBtn) saveBtn.disabled = false;
        }
    }

    // ==========================================
    // Skills Management
    // ==========================================
    let skillsLoaded = false;

    async function loadSkillsConfig() {
        if (skillsLoaded) return;
        const container = document.getElementById('skills-config-container');
        if (!container) return;
        container.innerHTML = `<div class="loading-indicator"><div class="spinner"></div><span>加载中...</span></div>`;

        try {
            const res = await fetch('/api/skills');
            const data = await res.json();
            container.innerHTML = '';

            if (!data.skills || data.skills.length === 0) {
                container.innerHTML = '<div class="empty-state"><p>暂无可管理的技能</p></div>';
                return;
            }

            data.skills.forEach(s => {
                const isChecked = s.enabled ? 'checked' : '';
                const icon = s.type === 'md' ? '📄' : '🐍';
                const displayName = s.name && s.name !== 'undefined' ? s.name : (s.filename || 'Undefined Skill');

                const div = document.createElement('div');
                div.className = 'skill-row';
                div.innerHTML = `
                    <div class="skill-info">
                        <strong>${icon} ${displayName}</strong>
                        <small>${s.type === 'md' ? 'Markdown Prompt' : '大模型技能'}</small>
                    </div>
                    <div class="skill-actions">
                        <button class="btn-secondary btn-edit-skill" style="padding: 0.2rem 0.6rem; font-size: 0.85rem;" data-filename="${s.filename}">编辑</button>
                        <label class="switch">
                            <input type="checkbox" class="skill-toggle" data-name="${s.filename || s.name}" ${isChecked}>
                            <span class="slider"></span>
                        </label>
                    </div>
                `;
                container.appendChild(div);
            });

            container.querySelectorAll('.btn-edit-skill').forEach(btn => {
                btn.addEventListener('click', () => openEditSkillModal(btn.dataset.filename));
            });

            skillsLoaded = true;
        } catch (e) {
            container.innerHTML = '<div class="empty-state"><p style="color:var(--error)">加载技能列表失败</p></div>';
        }
    }

    // Edit Skill Modal
    const editSkillModal = document.getElementById('edit-skill-modal');
    const editSkillFilename = document.getElementById('edit-skill-filename');
    const editSkillContent = document.getElementById('edit-skill-content');

    async function openEditSkillModal(filename) {
        if (!filename) return;
        editSkillFilename.textContent = filename;
        editSkillContent.value = '正在读取内容...';
        editSkillModal.classList.add('show');

        try {
            const res = await fetch(`/api/skills/${encodeURIComponent(filename)}`);
            if (res.ok) {
                const data = await res.json();
                editSkillContent.value = data.content || '';
            } else {
                editSkillContent.value = '读取技能内容失败。';
            }
        } catch (err) {
            editSkillContent.value = '读取技能内容发生网络错误。';
        }
    }

    document.getElementById('edit-skill-close')?.addEventListener('click', () => {
        editSkillModal.classList.remove('show');
    });

    document.getElementById('edit-skill-save')?.addEventListener('click', async () => {
        const filename = editSkillFilename.textContent;
        const content = editSkillContent.value;
        const saveBtn = document.getElementById('edit-skill-save');
        const ogText = saveBtn.textContent;

        saveBtn.textContent = '保存中...';
        saveBtn.disabled = true;

        try {
            const res = await fetch('/api/skills/import', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ filename, content, force: true })
            });
            const data = await res.json();
            if (data.success) {
                saveBtn.textContent = '✓ 已保存';
                skillsLoaded = false;
                setTimeout(() => {
                    editSkillModal.classList.remove('show');
                    saveBtn.textContent = ogText;
                    saveBtn.disabled = false;
                }, 1000);
            } else {
                alert('保存失败: ' + data.message);
                saveBtn.textContent = ogText;
                saveBtn.disabled = false;
            }
        } catch (e) {
            alert('保存时发生网络错误');
            saveBtn.textContent = ogText;
            saveBtn.disabled = false;
        }
    });

    // MCP Config loader
    async function loadMcpConfig() {
        try {
            const res = await fetch('/api/settings');
            const data = await res.json();
            if (data.agent_profiles) {
                const el = document.getElementById('agents-config-input');
                if (el) el.value = typeof data.agent_profiles === 'string' ? data.agent_profiles : JSON.stringify(data.agent_profiles, null, 2);
            }
            if (data.mcp_servers) {
                const el = document.getElementById('mcp-config-input');
                if (el) el.value = typeof data.mcp_servers === 'string' ? data.mcp_servers : JSON.stringify(data.mcp_servers, null, 2);
            }
        } catch (e) {
            console.error('Failed to load MCP config:', e);
        }
    }

    // ==========================================
    // Task Management
    // ==========================================
    let taskFilter = 'all';
    let taskSearchQuery = '';
    let taskRefreshInterval = null;

    async function loadTasks() {
        const container = document.getElementById('task-list-container');
        if (!container) return;

        try {
            let url = '/api/tasks';
            const params = [];
            if (taskFilter !== 'all') params.push(`status=${taskFilter}`);
            if (taskSearchQuery) params.push(`q=${encodeURIComponent(taskSearchQuery)}`);
            if (params.length > 0) url += '?' + params.join('&');

            const res = await fetch(url);
            const data = await res.json();

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

                // Task type badge
                const typeBadge = {
                    oneshot: '<span class="task-type-badge oneshot">一次性</span>',
                    scheduled: '<span class="task-type-badge scheduled">⏰ 定时</span>',
                    longrun: '<span class="task-type-badge longrun">🔬 长期</span>'
                }[task.task_type] || '';

                // Schedule info line
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

            // Bind action buttons
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
        switchView('task-detail');

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

    function updateTaskBadge() {
        fetch('/api/tasks?status=running')
            .then(res => res.json())
            .then(data => {
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

    function formatTimeAgo(isoStr) {
        if (!isoStr) return '';
        if (!isoStr.includes('T')) isoStr = isoStr.replace(' ', 'T');
        if (!isoStr.endsWith('Z') && !isoStr.includes('+')) isoStr += 'Z';
        const d = new Date(isoStr);
        const now = new Date();
        const diff = Math.floor((now - d) / 1000);
        if (diff < 60) return '刚刚';
        if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
        if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
        return Math.floor(diff / 86400) + ' 天前';
    }

    function formatTime(isoStr) {
        if (!isoStr) return '';
        if (!isoStr.includes('T')) isoStr = isoStr.replace(' ', 'T');
        if (!isoStr.endsWith('Z') && !isoStr.includes('+')) isoStr += 'Z';
        return new Date(isoStr).toLocaleString('zh-CN');
    }

    // Task filter & search event listeners
    document.querySelectorAll('.filter-pill').forEach(pill => {
        pill.addEventListener('click', () => {
            document.querySelectorAll('.filter-pill').forEach(p => p.classList.remove('active'));
            pill.classList.add('active');
            taskFilter = pill.dataset.filter;
            loadTasks();
        });
    });

    document.getElementById('task-search-input')?.addEventListener('input', (e) => {
        taskSearchQuery = e.target.value.trim();
        clearTimeout(taskRefreshInterval);
        taskRefreshInterval = setTimeout(loadTasks, 300);
    });

    document.getElementById('task-detail-back')?.addEventListener('click', () => {
        switchView('tasks');
    });

    // ==========================================
    // Schedule Modal
    // ==========================================
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
    // ==========================================
    // Core Event Listeners
    // ==========================================
    sendBtn.addEventListener('click', handleSend);

    if (stopBtn) {
        stopBtn.addEventListener('click', () => {
            if (ws && isConnected && isAgentThinking) {
                ws.send(JSON.stringify({ type: "interrupt" }));
                // Also try to interrupt current task
                if (currentTaskId) {
                    fetch(`/api/tasks/${currentTaskId}/interrupt`, { method: 'POST' }).catch(() => { });
                }
                stopBtn.disabled = true;
                stopBtn.style.opacity = '0.5';
            }
        });
    }

    messageInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend(); }
    });

    messageInput.addEventListener('input', function () {
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

    // Apply saved theme
    const savedTheme = localStorage.getItem('theme');
    if (savedTheme) htmlElement.setAttribute('data-theme', savedTheme);

    document.getElementById('new-chat-btn').addEventListener('click', () => {
        switchView('chat');
        chatContainer.innerHTML = '';
        appendMessage('*控制台*', 'system');
    });

    // ==========================================
    // Real-time Task Updates (polling)
    // ==========================================
    setInterval(() => {
        if (document.getElementById('view-tasks')?.classList.contains('active')) {
            loadTasks();
        }
        updateTaskBadge();
    }, 5000);

    // ==========================================
    // Initialization
    // ==========================================
    // ==========================================
    // Llama.cpp Management
    // ==========================================
    async function refreshLlamaStatus() {
        try {
            const res = await fetch('/api/llamacpp/status');
            const status = await res.json();

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

            // Update model list
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

            // Sync download progress (fallback for when WebSocket message is missed)
            if (status.download && status.download.active) {
                handleLlamaDownloadProgress({
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

    // ==========================================
    // Download History
    // ==========================================
    async function loadDownloadHistory() {
        // Try new downloads view first, fall back to old settings container
        let container = document.getElementById('downloads-view-container');
        let emptyState = document.getElementById('download-history-empty');
        if (!container) {
            container = document.getElementById('download-history-container');
        }
        if (!container) return;

        try {
            const res = await fetch('/api/downloads');
            const data = await res.json();

            // Remove existing download items and any loading placeholder
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
                const iconMap = {
                    'downloading': '📥',
                    'paused': '⏸️',
                    'completed': '✅',
                    'failed': '❌'
                };
                const icon = iconMap[dl.status] || '📋';
                const isDataset = dl.type === 'dataset' || (dl.label && dl.label.startsWith('数据集:'));
                const statusText = {
                    'downloading': '下载中',
                    'paused': '已暂停',
                    'completed': '已完成',
                    'failed': '失败'
                }[dl.status] || dl.status;

                const progressPct = dl.total_size > 0
                    ? Math.round((dl.progress || 0) * 100) + '%'
                    : (dl.downloaded_bytes > 0 ? (dl.downloaded_bytes / 1024 / 1024).toFixed(1) + ' MB' : '');

                const showActions = dl.status === 'paused' || dl.status === 'failed';
                const showProgress = dl.status === 'downloading' || dl.status === 'paused';

                const item = document.createElement('div');
                item.className = 'download-item';
                item.id = `download-item-${dl.id}`;
                item.innerHTML = `
                    <div class="download-item-icon">${isDataset ? (dl.status === 'completed' ? '📊' : icon) : icon}</div>
                    <div class="download-item-body">
                        <div class="download-item-title">${escapeHtml(dl.label || dl.filename || 'Unknown')}</div>
                        <div class="download-item-meta">
                            <span class="download-status-badge ${dl.status}">${statusText}</span>
                            <span>${progressPct}</span>
                            <span>${formatTimeAgo(dl.created_at)}</span>
                        </div>
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

                // Bind resume button
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

                // Bind delete button
                const deleteBtn = item.querySelector('.download-action-btn.delete');
                if (deleteBtn) {
                    deleteBtn.addEventListener('click', async (e) => {
                        e.stopPropagation();
                        if (!confirm('确定删除此下载记录？相关的未完成文件也将被删除。')) return;
                        try {
                            const res = await fetch(`/api/downloads/${deleteBtn.dataset.id}`, { method: 'DELETE' });
                            if (res.ok) {
                                showStatus('🗑 记录已删除', 'success');
                            } else {
                                const err = await res.json();
                                showStatus('❌ ' + (err.detail || '删除失败'), 'error');
                            }
                        } catch (e) {
                            showStatus('❌ 网络错误', 'error');
                        }
                        loadDownloadHistory();
                    });
                }
            });
        } catch (e) {
            console.error('Failed to load download history:', e);
        }
    }

    function initLlamaListeners() {
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

        // Search for GGUF models
        document.getElementById('llama-search-btn')?.addEventListener('click', async () => {
            const query = document.getElementById('llama-search-input').value.trim();
            const source = document.getElementById('llama-source-select')?.value || 'modelscope';
            const resultsDiv = document.getElementById('llama-search-results');
            const filesDiv = document.getElementById('llama-model-files');

            if (!query) {
                showStatus('⚠️ 请输入模型名称搜索', 'error');
                return;
            }

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
                    if (source === 'huggingface') {
                        resultsDiv.innerHTML = '<span class="field-hint">未找到模型（HuggingFace 可能无法访问，请尝试切换到 ModelScope）</span>';
                    } else {
                        resultsDiv.innerHTML = '<span class="field-hint">未找到匹配的模型</span>';
                    }
                    return;
                }
                renderSearchResults(data.models, resultsDiv, filesDiv);
            } catch (e) {
                resultsDiv.innerHTML = '<span class="field-hint">搜索失败，请重试</span>';
            }
        });

        // Allow Enter key in search input
        document.getElementById('llama-search-input')?.addEventListener('keydown', (e) => {
            if (e.key === 'Enter') {
                document.getElementById('llama-search-btn')?.click();
            }
        });

        document.getElementById('llama-start-btn')?.addEventListener('click', async () => {
            const model = document.getElementById('llama-model-select').value;
            if (!model) {
                showStatus('⚠️ 请先选择一个模型', 'error');
                return;
            }
            try {
                await fetch('/api/llamacpp/control', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ action: 'start', model: model })
                });
                showStatus('🚀 正在启动服务...', 'success');
                setTimeout(refreshLlamaStatus, 2000);
            } catch (e) {
                showStatus('❌ 启动失败', 'error');
            }
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
            } catch (e) {
                showStatus('❌ 停止失败', 'error');
            }
        });
    }

    // Polling llama status when in settings view
    setInterval(() => {
        if (document.getElementById('view-settings')?.classList.contains('active')) {
            refreshLlamaStatus();
            loadDownloadHistory();
        }
    }, 10000);

    function renderSearchResults(models, container, filesDiv) {
        let html = '';
        models.forEach(m => {
            const dl = (m.downloads || 0).toLocaleString();
            html += `
                <div class="search-result-item" style="padding:0.5rem; border:1px solid var(--border-color);
                    border-radius:6px; margin-bottom:0.4rem; cursor:pointer; transition: background 0.2s;"
                    onmouseenter="this.style.background='var(--surface-hover)'"
                    onmouseleave="this.style.background=''"
                    data-repo="${m.repo_id}">
                    <div style="font-weight:600; font-size:0.9rem;">${m.repo_id}</div>
                    <div style="font-size:0.75rem; color:var(--text-secondary); margin-top:0.15rem;">
                        作者: ${m.author || '未知'} | ⬇ ${dl} | 👍 ${m.likes || 0}
                    </div>
                </div>`;
        });
        container.innerHTML = html;

        // Bind click on each result
        container.querySelectorAll('.search-result-item').forEach(item => {
            item.addEventListener('click', async () => {
                const repoId = item.dataset.repo;
                filesDiv.innerHTML = '<span class="field-hint">加载文件列表中...</span>';

                // Highlight selected
                container.querySelectorAll('.search-result-item').forEach(el => {
                    el.style.background = '';
                    el.style.borderColor = 'var(--border-color)';
                });
                item.style.background = 'var(--surface-hover)';
                item.style.borderColor = 'var(--primary-color)';

                const source = document.getElementById('llama-source-select')?.value || 'modelscope';
                try {
                    const res = await fetch('/api/llamacpp/model-files', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ repo_id: repoId, source })
                    });
                    const data = await res.json();
                    if (data.status !== 'success' || !data.files.length) {
                        filesDiv.innerHTML = '<span class="field-hint">该仓库没有 GGUF 文件</span>';
                        return;
                    }
                    renderModelFiles(data.files, repoId, filesDiv);
                } catch (e) {
                    filesDiv.innerHTML = '<span class="field-hint">加载失败</span>';
                }
            });
        });
    }

    function renderModelFiles(files, repoId, container) {
        let html = '<div style="font-weight:600; font-size:0.85rem; margin-bottom:0.35rem;">GGUF 文件:</div>';
        files.forEach(f => {
            const shortName = f.filename.split('/').pop();
            html += `
                <div style="display:flex; align-items:center; justify-content:space-between;
                    padding:0.35rem 0.5rem; border:1px solid var(--border-color);
                    border-radius:6px; margin-bottom:0.25rem; font-size:0.8rem;">
                    <span style="flex:1; word-break:break-all;">${shortName}</span>
                    <span style="margin:0 0.75rem; color:var(--text-secondary); white-space:nowrap;">${f.size}</span>
                    <button class="btn-secondary" style="padding:0.2rem 0.6rem; font-size:0.75rem; white-space:nowrap;"
                        data-repo="${repoId}" data-file="${f.filename}">下载</button>
                </div>`;
        });
        container.innerHTML = html;

        // Bind download buttons
        container.querySelectorAll('button').forEach(btn => {
            btn.addEventListener('click', async function() {
                const repo = this.dataset.repo;
                const file = this.dataset.file;
                const shortName = file.split('/').pop();

                this.disabled = true;
                this.textContent = '启动中...';

                const source = document.getElementById('llama-source-select')?.value || 'modelscope';
                try {
                    const res = await fetch('/api/llamacpp/download-from-hf', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ repo_id: repo, filename: file, source })
                    });
                    const data = await res.json();
                    if (data.status === 'started') {
                        loadDownloadHistory();
                    } else if (data.status === 'success') {
                        showStatus('✅ 模型已下载: ' + shortName, 'success');
                        loadDownloadHistory();
                    } else {
                        showStatus('❌ 下载失败: ' + (data.detail || '未知错误'), 'error');
                    }
                } catch (e) {
                    showStatus('❌ 网络错误', 'error');
                } finally {
                    this.disabled = false;
                    this.textContent = '下载';
                    refreshLlamaStatus();
                }
            });
        });
    }

    // ==========================================
    // Training Functions
    // ==========================================
    let modelConfigLoaded = false;
    let datasetsLoaded = false;
    let baseModelsLoaded = false;
    let trainingHistoryLoaded = false;
    let lossData = [];
    let currentSelectedArch = 'gpt_decoder';
    let currentTrainingRunId = null;

    // --- Architecture Selector ---
    let fineTuneScope = 'all';

    function initArchSelector() {
        document.querySelectorAll('.arch-option').forEach(btn => {
            btn.addEventListener('click', () => {
                document.querySelectorAll('.arch-option').forEach(b => b.classList.remove('selected'));
                btn.classList.add('selected');
                currentSelectedArch = btn.dataset.arch;
                updateArchFieldVisibility();
            });
        });
        // GQA KV heads visibility
        const attnSelect = document.getElementById('hp-attention-type');
        const gqaField = document.getElementById('gqa-kv-field');
        if (attnSelect && gqaField) {
            attnSelect.addEventListener('change', () => {
                gqaField.style.display = attnSelect.value === 'gqa' ? '' : 'none';
            });
        }
    }

    function updateArchFieldVisibility() {
        const moeFields = document.getElementById('moe-fields');
        if (moeFields) moeFields.style.display = currentSelectedArch === 'moe' ? 'flex' : 'none';
    }

    function getModelConfigFromForm() {
        return JSON.stringify({
            num_layers: parseInt(document.getElementById('hp-num-layers').value) || 12,
            hidden_size: parseInt(document.getElementById('hp-hidden-size').value) || 768,
            num_attention_heads: parseInt(document.getElementById('hp-num-heads').value) || 12,
            intermediate_size: parseInt(document.getElementById('hp-intermediate').value) || 3072,
            vocab_size: parseInt(document.getElementById('hp-vocab-size').value) || 50000,
            max_seq_length: parseInt(document.getElementById('hp-max-seq').value) || 2048,
            num_experts: currentSelectedArch === 'moe' ? (parseInt(document.getElementById('hp-num-experts').value) || 8) : undefined,
            active_experts: currentSelectedArch === 'moe' ? (parseInt(document.getElementById('hp-active-experts').value) || 2) : undefined,
            attention_type: document.getElementById('hp-attention-type')?.value || 'scaled_dot',
            kv_heads: document.getElementById('hp-kv-heads')?.value || 4,
            norm_position: document.getElementById('hp-norm-position')?.value || 'pre_norm',
            norm_type: document.getElementById('hp-norm-type')?.value || 'layer_norm',
            pos_encoding: document.getElementById('hp-pos-encoding')?.value || 'rope',
            activation: document.getElementById('hp-activation')?.value || 'gelu',
            attn_dropout: parseFloat(document.getElementById('hp-attn-dropout')?.value) || 0.1,
            resid_dropout: parseFloat(document.getElementById('hp-resid-dropout')?.value) || 0.1,
            embd_dropout: parseFloat(document.getElementById('hp-embd-dropout')?.value) || 0.1
        });
    }

    // --- Fine-tune Scope Selection ---

    window.onBaseModelSelected = function() {
        const sel = document.getElementById('finetune-base-model');
        const modelId = sel?.value;
        const card = document.getElementById('model-structure-card');
        if (!modelId || !card) {
            if (card) card.style.display = 'none';
            return;
        }
        card.style.display = '';
        const info = document.getElementById('model-structure-info');
        if (modelId.endsWith('.gguf')) {
            info.textContent = 'GGUF 格式模型 — 无法解析结构';
            document.getElementById('model-structure-viz').innerHTML = '<span class="field-hint">GGUF 模型不支持结构可视化，微调时需先转换为 HuggingFace 格式</span>';
        } else {
            info.textContent = modelId + ' — 典型结构预览';
            showTypicalStructure(modelId);
        }
        updateFineTuneScope();
    };

    function showTypicalStructure(modelId) {
        const viz = document.getElementById('model-structure-viz');
        const layers = [
            {name: 'Embedding', type: 'embed'},
            {name: 'LayerNorm (Pre)', type: 'norm'},
        ];
        for (let i = 0; i < 8; i++) {
            layers.push({name: `Block[${i}].SelfAttn`, type: 'attn'});
            layers.push({name: `Block[${i}].LayerNorm`, type: 'norm'});
            layers.push({name: `Block[${i}].FFN`, type: 'ffn'});
        }
        layers.push({name: 'LayerNorm (Final)', type: 'norm'});
        layers.push({name: 'LM Head', type: 'head'});

        const scope = document.querySelector('.arch-option.selected[data-finetune-scope]')?.dataset.finetuneScope || 'all';
        let html = '<div style="max-height:300px; overflow-y:auto;">';
        layers.forEach((l, i) => {
            const isFrozen = (scope === 'lora_attn' && l.type !== 'attn') ||
                             (scope === 'lora_custom' && l.type === 'head');
            html += `<div class="model-structure-layer" style="${isFrozen ? 'opacity:0.45;' : ''}">
                <span style="flex:0 0 30px; font-size:0.65rem; color:var(--text-secondary);">${i+1}</span>
                <span class="model-structure-tag ${l.type}">${l.type.toUpperCase()}</span>
                <span style="flex:1;">${l.name}</span>
                ${isFrozen ? '<span style="font-size:0.65rem; color:var(--text-secondary);">❄ 冻结</span>' : '<span style="font-size:0.65rem; color:var(--success);">🔥 训练</span>'}
            </div>`;
        });
        html += '</div>';
        viz.innerHTML = html;
    }

    function updateFineTuneScope() {
        document.querySelectorAll('[data-finetune-scope]').forEach(btn => {
            btn.addEventListener('click', function() {
                document.querySelectorAll('[data-finetune-scope]').forEach(b => b.classList.remove('selected'));
                this.classList.add('selected');
                fineTuneScope = this.dataset.finetuneScope;
                const modelId = document.getElementById('finetune-base-model')?.value;
                if (modelId) showTypicalStructure(modelId);
                document.getElementById('custom-finetune-modules').style.display = fineTuneScope === 'lora_custom' ? '' : 'none';
                if (fineTuneScope === 'lora_custom') renderCustomModuleSelector();
                if (fineTuneScope === 'lora_attn') {
                    document.getElementById('lora-targets').value = 'q_proj, k_proj, v_proj, o_proj';
                } else if (fineTuneScope === 'lora_all') {
                    document.getElementById('lora-targets').value = 'q_proj, k_proj, v_proj, o_proj, gate_proj, up_proj, down_proj';
                }
            });
        });
    }

    function renderCustomModuleSelector() {
        const container = document.getElementById('custom-finetune-modules');
        if (!container) return;
        const modules = [
            {id:'q_proj', name:'Query 投影', checked:true},
            {id:'k_proj', name:'Key 投影', checked:true},
            {id:'v_proj', name:'Value 投影', checked:true},
            {id:'o_proj', name:'Output 投影', checked:true},
            {id:'gate_proj', name:'Gate 投影 (SwiGLU)', checked:false},
            {id:'up_proj', name:'Up 投影 (FFN)', checked:false},
            {id:'down_proj', name:'Down 投影 (FFN)', checked:false},
            {id:'embed_tokens', name:'嵌入层', checked:false},
            {id:'lm_head', name:'输出头', checked:false},
        ];
        container.innerHTML = `<div style="font-size:0.75rem; color:var(--text-secondary); margin-bottom:0.3rem;">选择要微调的模块:</div>` +
            modules.map(m => `<label class="finetune-module-check"><input type="checkbox" value="${m.id}" ${m.checked?'checked':''} onchange="updateLoraTargets()"> ${m.name}</label>`).join('');
    }

    window.updateLoraTargets = function() {
        const checked = [...document.querySelectorAll('#custom-finetune-modules input:checked')].map(cb => cb.value);
        document.getElementById('lora-targets').value = checked.join(', ');
    };

    // --- Model Designer ---
    async function loadModelConfigs() {
        if (modelConfigLoaded) return;
        modelConfigLoaded = true;
        initArchSelector();
        try {
            const res = await fetch('/api/training/model-configs');
            const data = await res.json();
            renderSavedConfigs(data.configs || []);
        } catch (e) { console.error('loadModelConfigs:', e); }
    }

    function renderSavedConfigs(configs) {
        const container = document.getElementById('saved-configs-list');
        if (!container) return;
        if (!configs.length) {
            container.innerHTML = '<div class="empty-state"><p>暂无保存的配置</p></div>';
            return;
        }
        container.innerHTML = configs.map(c => {
            const cfg = JSON.parse(c.config_json || '{}');
            const params = c.param_count_estimate;
            const paramsStr = params > 1e9 ? (params/1e9).toFixed(2)+'B' : (params/1e6).toFixed(1)+'M';
            return `<div class="config-item" data-id="${c.id}">
                <div class="config-item-body">
                    <div class="config-item-title">${escapeHtml(c.name)}</div>
                    <div class="config-item-meta">${c.architecture} | ${paramsStr} 参数</div>
                </div>
                <button class="download-action-btn delete" data-id="${c.id}" title="删除">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
                </button>
            </div>`;
        }).join('');
        container.querySelectorAll('.config-item').forEach(el => {
            el.addEventListener('click', async () => {
                const id = el.dataset.id;
                const res = await fetch(`/api/training/model-configs/${id}`);
                const cfg = await res.json();
                fillConfigForm(cfg);
            });
        });
        container.querySelectorAll('.download-action-btn.delete').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                await fetch(`/api/training/model-configs/${btn.dataset.id}`, { method: 'DELETE' });
                modelConfigLoaded = false;
                loadModelConfigs();
            });
        });
    }

    function fillConfigForm(cfg) {
        const c = JSON.parse(cfg.config_json || '{}');
        document.getElementById('hp-num-layers').value = c.num_layers || 12;
        document.getElementById('hp-hidden-size').value = c.hidden_size || 768;
        document.getElementById('hp-num-heads').value = c.num_attention_heads || 12;
        document.getElementById('hp-intermediate').value = c.intermediate_size || 3072;
        document.getElementById('hp-vocab-size').value = c.vocab_size || 50000;
        document.getElementById('hp-max-seq').value = c.max_seq_length || 2048;
        if (c.num_experts) document.getElementById('hp-num-experts').value = c.num_experts;
        if (c.active_experts) document.getElementById('hp-active-experts').value = c.active_experts;
        if (c.activation) document.getElementById('hp-activation').value = c.activation;
        currentSelectedArch = cfg.architecture;
        document.querySelectorAll('.arch-option').forEach(b => {
            b.classList.toggle('selected', b.dataset.arch === cfg.architecture);
        });
        updateArchFieldVisibility();
    }

    async function estimateModel() {
        const configJson = getModelConfigFromForm();
        const res = await fetch('/api/training/model-configs/estimate', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ architecture: currentSelectedArch, config_json: configJson })
        });
        const data = await res.json();
        const preview = document.getElementById('model-preview-content');
        if (preview) {
            preview.innerHTML = `<div class="model-preview">
                <div class="preview-item"><label>总参数量</label><span>${data.total_params_formatted}</span></div>
                <div class="preview-item"><label>层数</label><span>${data.num_layers}</span></div>
                <div class="preview-item"><label>每层参数量</label><span>${(data.per_layer_params/1e6).toFixed(1)}M</span></div>
                <div class="preview-item"><label>嵌入参数量</label><span>${(data.embed_params/1e6).toFixed(1)}M</span></div>
                <div class="preview-item"><label>每Token FLOPs</label><span>${(data.flops_per_forward/1e6).toFixed(1)}M</span></div>
                <div class="preview-item"><label>架构</label><span>${data.architecture}</span></div>
            </div>`;
        }
    }

    async function saveModelConfig() {
        const name = prompt('配置名称:');
        if (!name) return;
        const configJson = getModelConfigFromForm();
        const est = await fetch('/api/training/model-configs/estimate', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ architecture: currentSelectedArch, config_json: configJson })
        });
        const estData = await est.json();
        await fetch('/api/training/model-configs', {
            method: 'POST', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({ name, architecture: currentSelectedArch, config_json: configJson, param_count_estimate: estData.total_params })
        });
        showStatus('✅ 配置已保存', 'success');
        modelConfigLoaded = false;
        loadModelConfigs();
    }

    setTimeout(() => {
        initArchSelector();
        document.getElementById('estimate-model-btn')?.addEventListener('click', estimateModel);
        document.getElementById('save-model-config-btn')?.addEventListener('click', saveModelConfig);
        document.getElementById('ds-upload-btn')?.addEventListener('click', uploadDataset);
        document.getElementById('ds-hf-import-btn')?.addEventListener('click', importHFDataset);
        document.getElementById('start-training-btn')?.addEventListener('click', startTraining);
        initDatasetEditor();

        document.getElementById('monitor-pause-btn')?.addEventListener('click', () => trainingControl('pause'));
        document.getElementById('monitor-resume-btn')?.addEventListener('click', () => trainingControl('resume'));
        document.getElementById('monitor-step-btn')?.addEventListener('click', () => trainingControl('step'));
        document.getElementById('monitor-abort-btn')?.addEventListener('click', () => trainingControl('abort'));

        // Collapsible sidebar section toggle
        document.querySelectorAll('.sidebar-section-header.collapsible').forEach(header => {
            header.addEventListener('click', () => {
                const section = header.closest('.sidebar-section');
                const subnav = section?.querySelector('.sidebar-subnav');
                const isCollapsed = header.classList.contains('collapsed');
                if (isCollapsed) {
                    header.classList.remove('collapsed');
                    if (subnav) subnav.style.display = '';
                } else {
                    header.classList.add('collapsed');
                    if (subnav) subnav.style.display = 'none';
                }
            });
        });

        // Auto-install training deps if not available
        checkAndOfferTrainingInstall();
    }, 200);

    // Training deps install — uses static HTML cards in each training view
    async function startTrainingInstall() {
        document.querySelectorAll('.training-deps-missing').forEach(card => {
            card.style.display = '';
            card.querySelector('.training-deps-progress').style.display = '';
            card.querySelector('.training-deps-label').textContent = '正在启动安装...';
            card.querySelector('.training-deps-bar').style.width = '0%';
            card.querySelector('.training-deps-install').style.display = 'none';
        });
        try {
            const ir = await fetch('/api/training/install-deps', { method: 'POST' });
            if (ir.status === 409) { showStatus('⚠️ 安装已在进行中', 'error'); return; }
            const data = await ir.json();
            showStatus('📦 ' + (data.message || '正在安装...'), 'success');
        } catch (e) { showStatus('❌ 安装请求失败', 'error'); }
    }

    function handleInstallProgress(data) {
        const pct = Math.round((data.progress || 0) * 100);
        document.querySelectorAll('.training-deps-missing').forEach(card => {
            card.style.display = '';
            card.querySelector('.training-deps-progress').style.display = '';
            card.querySelector('.training-deps-label').textContent = data.label || '';
            card.querySelector('.training-deps-bar').style.width = pct + '%';
            card.querySelector('.training-deps-install').style.display = 'none';
        });
        if (data.stage === 'complete') setTimeout(() => location.reload(), 2000);
        if (data.stage === 'error') {
            document.querySelectorAll('.training-deps-missing').forEach(card => {
                card.querySelector('.training-deps-msg').textContent = '错误: ' + (data.error || '');
                card.querySelector('.training-deps-install').style.display = '';
                card.querySelector('.training-deps-progress').style.display = 'none';
            });
        }
    }

    async function checkAndOfferTrainingInstall() {
        try {
            const res = await fetch('/api/training/status');
            const data = await res.json();
            if (!data.available || data.install_state?.active) {
                const state = data.install_state || {};
                document.querySelectorAll('.training-deps-missing').forEach(card => {
                    card.style.display = '';
                    card.querySelector('.training-deps-msg').textContent = data.import_error || '';
                    if (state.active) {
                        card.querySelector('.training-deps-install').style.display = 'none';
                        card.querySelector('.training-deps-progress').style.display = '';
                        card.querySelector('.training-deps-label').textContent = state.label || '安装中...';
                        card.querySelector('.training-deps-bar').style.width = Math.round((state.progress||0)*100) + '%';
                    } else {
                        card.querySelector('.training-deps-install').style.display = '';
                        card.querySelector('.training-deps-progress').style.display = 'none';
                    }
                });
                document.querySelectorAll('.training-deps-install').forEach(btn => {
                    btn.onclick = () => startTrainingInstall();
                });
            } else {
                document.querySelectorAll('.training-deps-missing').forEach(card => { card.style.display = 'none'; });
            }
        } catch (e) { /* ignore */ }
    }

    // --- Dataset Manager ---
    async function loadDatasets() {
        if (datasetsLoaded) return;
        datasetsLoaded = true;
        try {
            const res = await fetch('/api/training/datasets');
            const data = await res.json();
            renderDatasetList(data.datasets || []);
        } catch (e) { console.error('loadDatasets:', e); }
        loadRecommendedDatasets();
    }

    // --- Recommended Datasets ---

    async function loadRecommendedDatasets() {
        try {
            const res = await fetch('/api/training/recommended-datasets');
            const data = await res.json();
            renderRecommendedDatasets(data.datasets || []);
        } catch (e) { console.error('loadRecommended:', e); }
    }

    function renderRecommendedDatasets(datasets) {
        const grid = document.getElementById('recommended-datasets-grid');
        if (!grid) return;
        grid.innerHTML = datasets.map(d => `
            <div class="rec-ds-card">
                <div class="rec-ds-name">📦 ${escapeHtml(d.name)}</div>
                <div class="rec-ds-desc">${escapeHtml(d.desc)}</div>
                <div class="rec-ds-meta">${d.size} · ${(d.splits||[]).join(', ')}</div>
                <button class="btn-secondary rec-ds-dl-btn" data-repo="${d.repo_id}" data-name="${d.name}" data-config="${d.config||''}" style="margin-top:0.4rem; width:100%;">一键下载</button>
            </div>
        `).join('');
        grid.querySelectorAll('.rec-ds-dl-btn').forEach(btn => {
            btn.addEventListener('click', async function() {
                this.disabled = true;
                this.textContent = '启动中...';
                try {
                    const res = await fetch('/api/downloads/dataset', {
                        method: 'POST', headers: {'Content-Type': 'application/json'},
                        body: JSON.stringify({ repo_id: this.dataset.repo, name: this.dataset.name, config: this.dataset.config || null })
                    });
                    const d = await res.json();
                    if (d.status === 'started') {
                        showStatus('📥 ' + d.message, 'success');
                        if (document.getElementById('view-settings-models')?.classList.contains('active') ||
                            document.getElementById('view-settings')?.classList.contains('active')) {
                            loadDownloadHistory();
                        }
                    } else {
                        showStatus('❌ ' + (d.detail || '启动失败'), 'error');
                        this.disabled = false;
                        this.textContent = '一键下载';
                    }
                } catch (e) {
                    showStatus('❌ 网络错误', 'error');
                    this.disabled = false;
                    this.textContent = '一键下载';
                }
            });
        });
    }

    function renderDatasetList(datasets) {
        const container = document.getElementById('dataset-list-container');
        if (!container) return;
        if (!datasets.length) {
            container.innerHTML = '<div class="empty-state"><p>暂无数据集</p></div>';
            return;
        }
        container.innerHTML = datasets.map(d => {
            const size = d.sample_count || 0;
            return `<div class="download-item">
                <div class="download-item-icon">📊</div>
                <div class="download-item-body">
                    <div class="download-item-title">${escapeHtml(d.name)}</div>
                    <div class="download-item-meta">
                        <span>${d.format}</span><span>${size} 条</span><span>${d.source}</span>
                    </div>
                </div>
                <div class="download-item-actions">
                    <button class="download-action-btn" data-id="${d.id}" data-action="preview" title="预览">👁</button>
                    <button class="download-action-btn" data-id="${d.id}" data-action="edit" title="编辑">✏️</button>
                    <button class="download-action-btn delete" data-id="${d.id}" title="删除">
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
                    </button>
                </div>
            </div>`;
        }).join('');
        container.querySelectorAll('[data-action="preview"]').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const id = btn.dataset.id;
                const res = await fetch(`/api/training/datasets/${id}/preview?n=5`);
                const data = await res.json();
                showStatus(`预览: ${(data.samples||[]).length} 条`, 'success');
            });
        });
        container.querySelectorAll('[data-action="edit"]').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                const id = btn.dataset.id;
                const res = await fetch(`/api/training/datasets/${id}`);
                const ds = await res.json();
                if (ds.storage_path) {
                    const contentRes = await fetch(`/api/training/datasets/${id}/preview?n=9999`);
                    const contentData = await contentRes.json();
                    document.getElementById('ds-editor-name').value = ds.name;
                    document.getElementById('ds-editor-content').value = (contentData.samples||[]).map(s => JSON.stringify(s)).join('\n');
                    document.getElementById('ds-editor-save').textContent = '更新数据集';
                    document.getElementById('ds-editor-save').dataset.editId = id;
                    document.getElementById('ds-editor-status').textContent = `正在编辑: ${ds.name} (${ds.sample_count} 条)`;
                }
            });
        });
        container.querySelectorAll('.download-action-btn.delete').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                if (!confirm('确定删除此数据集？')) return;
                await fetch(`/api/training/datasets/${btn.dataset.id}`, { method: 'DELETE' });
                datasetsLoaded = false;
                loadDatasets();
            });
        });
    }

    async function uploadDataset() {
        const name = document.getElementById('ds-upload-name').value.trim();
        const fileInput = document.getElementById('ds-file-input');
        if (!fileInput.files.length) { showStatus('⚠️ 请选择文件', 'error'); return; }
        const formData = new FormData();
        formData.append('file', fileInput.files[0]);
        formData.append('name', name || fileInput.files[0].name);
        try {
            const res = await fetch('/api/training/datasets/upload', { method: 'POST', body: formData });
            const data = await res.json();
            if (data.status === 'success') {
                showStatus(`✅ 数据集已上传 (${data.sample_count} 条)`, 'success');
                datasetsLoaded = false;
                loadDatasets();
            } else { showStatus('❌ 上传失败', 'error'); }
        } catch (e) { showStatus('❌ 网络错误', 'error'); }
    }

    async function importHFDataset() {
        const repo = document.getElementById('ds-hf-custom').value.trim();
        if (!repo) { showStatus('⚠️ 请输入仓库ID', 'error'); return; }
        try {
            const res = await fetch('/api/downloads/dataset', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ repo_id: repo, name: repo })
            });
            const data = await res.json();
            if (data.status === 'started') {
                showStatus('📥 ' + data.message, 'success');
            } else {
                showStatus('❌ ' + (data.detail || '下载失败'), 'error');
            }
        } catch (e) { showStatus('❌ 网络错误', 'error'); }
    }

    // --- Dataset Editor ---

    function initDatasetEditor() {
        document.getElementById('ds-editor-add-sample')?.addEventListener('click', () => {
            const ta = document.getElementById('ds-editor-content');
            ta.value += (ta.value ? '\n' : '') + '{"instruction": "", "input": "", "output": ""}';
        });
        document.getElementById('ds-editor-validate')?.addEventListener('click', () => {
            const content = document.getElementById('ds-editor-content').value.trim();
            if (!content) { showStatus('⚠️ 内容为空', 'error'); return; }
            let errors = 0;
            content.split('\n').forEach((line, i) => {
                if (!line.trim()) return;
                try { JSON.parse(line); } catch (e) { errors++; }
            });
            const status = document.getElementById('ds-editor-status');
            if (errors) { status.textContent = `❌ ${errors} 行 JSON 格式错误`; status.style.color = 'var(--error)'; }
            else { status.textContent = `✅ JSON 格式正确 (${content.split('\n').filter(l => l.trim()).length} 条)`; status.style.color = 'var(--success)'; }
        });
        document.getElementById('ds-editor-save')?.addEventListener('click', async () => {
            const name = document.getElementById('ds-editor-name').value.trim();
            const content = document.getElementById('ds-editor-content').value.trim();
            if (!name) { showStatus('⚠️ 请输入数据集名称', 'error'); return; }
            if (!content) { showStatus('⚠️ 内容为空', 'error'); return; }
            const status = document.getElementById('ds-editor-status');
            const editId = document.getElementById('ds-editor-save').dataset.editId;
            try {
                const url = editId ? `/api/training/datasets/${editId}` : '/api/training/datasets/create';
                const method = editId ? 'PUT' : 'POST';
                const res = await fetch(url, { method, headers: {'Content-Type': 'application/json'}, body: JSON.stringify({ name, samples: content }) });
                const data = await res.json();
                if (data.status === 'success') {
                    showStatus(`✅ 数据集已${editId ? '更新' : '创建'} (${data.sample_count} 条)`, 'success');
                    document.getElementById('ds-editor-name').value = '';
                    document.getElementById('ds-editor-content').value = '';
                    document.getElementById('ds-editor-save').textContent = '保存数据集';
                    delete document.getElementById('ds-editor-save').dataset.editId;
                    status.textContent = '';
                    datasetsLoaded = false;
                    loadDatasets();
                } else { status.textContent = '❌ ' + (data.detail || '失败'); status.style.color = 'var(--error)'; }
            } catch (e) { status.textContent = '❌ 网络错误'; status.style.color = 'var(--error)'; }
        });
    }

    // --- Fine-tuning ---
    async function loadBaseModels() {
        if (baseModelsLoaded) return;
        baseModelsLoaded = true;
        try {
            const res = await fetch('/api/training/base-models');
            const data = await res.json();
            const sel = document.getElementById('finetune-base-model');
            if (sel) {
                sel.innerHTML = '<option value="">-- 选择模型 --</option>' +
                    (data.models || []).map(m => `<option value="${m.id}">${m.name} (${m.source})</option>`).join('');
            }
        } catch (e) { console.error('loadBaseModels:', e); }
        try {
            const dsRes = await fetch('/api/training/datasets');
            const dsData = await dsRes.json();
            const sel = document.getElementById('finetune-dataset');
            if (sel) {
                sel.innerHTML = '<option value="">-- 选择数据集 --</option>' +
                    (dsData.datasets || []).map(d => `<option value="${d.id}">${d.name} (${d.sample_count}条)</option>`).join('');
            }
        } catch (e) { console.error('loadDatasets for finetune:', e); }
    }

    async function startTraining() {
        const baseModel = document.getElementById('finetune-base-model')?.value;
        const datasetId = parseInt(document.getElementById('finetune-dataset')?.value) || null;
        if (!baseModel) { showStatus('⚠️ 请选择基座模型', 'error'); return; }

        const params = {
            lora: {
                rank: parseInt(document.getElementById('lora-rank')?.value) || 8,
                alpha: parseInt(document.getElementById('lora-alpha')?.value) || 16,
                dropout: parseFloat(document.getElementById('lora-dropout')?.value) || 0.05,
                target_modules: (document.getElementById('lora-targets')?.value || 'q_proj,v_proj').split(',').map(s => s.trim()).filter(Boolean)
            },
            learning_rate: parseFloat(document.getElementById('train-lr')?.value) || 2e-4,
            epochs: parseInt(document.getElementById('train-epochs')?.value) || 3,
            batch_size: parseInt(document.getElementById('train-batch')?.value) || 4,
            gradient_accumulation: parseInt(document.getElementById('train-grad-accum')?.value) || 1,
            max_steps: parseInt(document.getElementById('train-max-steps')?.value) || -1
        };

        try {
            const res = await fetch('/api/training/runs', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    name: `${baseModel.split('/').pop()} 微调`,
                    dataset_id: datasetId,
                    base_model_id: baseModel,
                    base_model_source: 'huggingface',
                    training_params_json: JSON.stringify(params)
                })
            });
            const data = await res.json();
            if (data.status === 'started') {
                currentTrainingRunId = data.run_id;
                showStatus('🚀 训练已启动', 'success');
                switchView('training-monitor');
            } else {
                showStatus('❌ 启动失败: ' + (data.detail || ''), 'error');
            }
        } catch (e) { showStatus('❌ 网络错误', 'error'); }
    }

    // --- Training Monitor ---
    function initTrainingMonitor() {
        const canvas = document.getElementById('loss-chart-canvas');
        if (canvas) {
            const ctx = canvas.getContext('2d');
            canvas.width = canvas.parentElement.clientWidth;
            canvas.height = 280;
        }
    }

    function drawLossChart() {
        const canvas = document.getElementById('loss-chart-canvas');
        if (!canvas || !lossData.length) return;
        const ctx = canvas.getContext('2d');
        const w = canvas.width, h = canvas.height;
        ctx.clearRect(0, 0, w, h);

        const margin = { top: 20, right: 20, bottom: 30, left: 50 };
        const pw = w - margin.left - margin.right;
        const ph = h - margin.top - margin.bottom;

        const maxLoss = Math.max(...lossData.map(d => d.loss)) * 1.1 || 1;
        const maxStep = Math.max(...lossData.map(d => d.step)) || 1;

        ctx.strokeStyle = 'var(--border-color)';
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.moveTo(margin.left, margin.top);
        ctx.lineTo(margin.left, margin.top + ph);
        ctx.lineTo(margin.left + pw, margin.top + ph);
        ctx.stroke();

        ctx.strokeStyle = 'var(--theme-color)';
        ctx.lineWidth = 2;
        ctx.beginPath();
        lossData.forEach((d, i) => {
            const x = margin.left + (d.step / maxStep) * pw;
            const y = margin.top + ph - (d.loss / maxLoss) * ph;
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
        });
        ctx.stroke();

        const legend = document.getElementById('loss-chart-legend');
        if (legend && lossData.length > 0) {
            const last = lossData[lossData.length - 1];
            legend.textContent = `Step: ${last.step} | Loss: ${last.loss}`;
        }
    }

    function handleTrainingProgress(data) {
        if (data.status === 'initializing' || data.status === 'loading_model') {
            document.getElementById('monitor-run-name').textContent = data.status === 'initializing' ? '正在初始化...' : '正在加载模型...';
            return;
        }
        currentTrainingRunId = data.run_id;
        document.getElementById('monitor-run-name').textContent = `运行 #${data.run_id} — 训练中`;
        document.getElementById('monitor-loss').textContent = data.loss != null ? data.loss.toFixed(4) : '--';
        document.getElementById('monitor-grad-norm').textContent = data.grad_norm != null ? data.grad_norm.toFixed(4) : '--';
        document.getElementById('monitor-lr').textContent = data.learning_rate != null ? data.learning_rate.toExponential(2) : '--';
        document.getElementById('monitor-epoch').textContent = data.epoch != null ? data.epoch : '--';
        document.getElementById('monitor-step').textContent = data.global_step != null ? data.global_step : '--';
        document.getElementById('monitor-status-badge').textContent = '训练中';
        document.getElementById('monitor-status-badge').className = 'task-type-badge';

        if (data.loss != null) {
            lossData.push({ step: data.global_step || lossData.length, loss: data.loss });
            if (lossData.length > 200) lossData.shift();
            drawLossChart();
        }
    }

    function handleTrainingStepPaused(data) {
        document.getElementById('monitor-status-badge').textContent = '已暂停 (单步)';
        document.getElementById('monitor-status-badge').className = 'task-type-badge scheduled';
        document.getElementById('monitor-pause-btn').style.display = 'none';
        document.getElementById('monitor-resume-btn').style.display = 'inline-flex';
        document.getElementById('monitor-loss').textContent = data.loss != null ? data.loss.toFixed(4) : '--';
        document.getElementById('monitor-grad-norm').textContent = data.grad_norm != null ? data.grad_norm.toFixed(4) : '--';

        const actStats = data.act_stats;
        const container = document.getElementById('activation-stats-container');
        if (container && actStats) {
            container.innerHTML = `<div style="font-size:0.8rem; margin-bottom:0.5rem;">
                <span>均值: ${actStats.mean != null ? actStats.mean.toFixed(4) : 'N/A'}</span>
                <span style="margin-left:1rem;">标准差: ${actStats.std != null ? actStats.std.toFixed(4) : 'N/A'}</span>
            </div>`;
            (actStats.per_layer || []).forEach(l => {
                const maxVal = Math.max(Math.abs(l.mean || 0), Math.abs(l.std || 0), 0.1);
                const row = document.createElement('div');
                row.className = 'layer-stat-row';
                row.innerHTML = `
                    <span class="layer-stat-name" title="${l.name}">${l.name.split('.').slice(-2).join('.')}</span>
                    <div class="layer-stat-bar"><div class="layer-stat-bar-fill" style="width:${Math.min(Math.abs(l.mean||0)/maxVal*100, 100)}%"></div></div>
                    <span class="layer-stat-values">μ=${(l.mean||0).toFixed(3)} σ=${(l.std||0).toFixed(3)}</span>
                `;
                container.appendChild(row);
            });
        }
    }

    function handleTrainingComplete(data) {
        const badge = document.getElementById('monitor-status-badge');
        badge.textContent = data.aborted ? '已中止' : '已完成';
        badge.className = data.aborted ? 'task-type-badge longrun' : 'task-type-badge oneshot';
        document.getElementById('monitor-pause-btn').style.display = 'none';
        document.getElementById('monitor-resume-btn').style.display = 'none';
        document.getElementById('monitor-step-btn').style.display = 'none';
        document.getElementById('monitor-run-name').textContent = `运行 #${data.run_id} — ${data.aborted ? '已中止' : '已完成'}`;
        showStatus(data.aborted ? '⏹ 训练已中止' : `✅ 训练完成! 最佳Loss: ${data.best_loss}`, data.aborted ? 'error' : 'success');
        drawLossChart();
    }

    function handleTrainingError(data) {
        document.getElementById('monitor-status-badge').textContent = '错误';
        document.getElementById('monitor-status-badge').className = 'task-type-badge longrun';
        document.getElementById('monitor-run-name').textContent = `运行 #${data.run_id} — 错误`;
        showStatus('❌ 训练错误: ' + (data.error || '未知'), 'error');
    }

    async function trainingControl(action) {
        if (!currentTrainingRunId) return;
        try {
            const res = await fetch(`/api/training/runs/${currentTrainingRunId}/${action}`, { method: 'POST' });
            const data = await res.json();
            if (data.status === 'paused') {
                document.getElementById('monitor-pause-btn').style.display = 'none';
                document.getElementById('monitor-resume-btn').style.display = 'inline-flex';
                document.getElementById('monitor-status-badge').textContent = '已暂停';
                document.getElementById('monitor-status-badge').className = 'task-type-badge scheduled';
            } else if (data.status === 'resumed' || data.status === 'stepping') {
                document.getElementById('monitor-pause-btn').style.display = 'inline-flex';
                document.getElementById('monitor-resume-btn').style.display = 'none';
                document.getElementById('monitor-status-badge').textContent = '训练中';
                document.getElementById('monitor-status-badge').className = 'task-type-badge';
            } else if (data.status === 'aborted') {
                document.getElementById('monitor-pause-btn').style.display = 'none';
                document.getElementById('monitor-resume-btn').style.display = 'none';
            }
        } catch (e) { showStatus('❌ 操作失败', 'error'); }
    }

    // --- Training History ---
    async function loadTrainingRuns() {
        if (trainingHistoryLoaded) return;
        trainingHistoryLoaded = true;
        try {
            const res = await fetch('/api/training/runs');
            const data = await res.json();
            renderTrainingRuns(data.runs || []);
        } catch (e) { console.error('loadTrainingRuns:', e); }
    }

    function renderTrainingRuns(runs) {
        const container = document.getElementById('training-runs-list');
        if (!container) return;
        if (!runs.length) {
            container.innerHTML = '<div class="empty-state"><p>暂无训练记录</p></div>';
            return;
        }
        const statusIcon = { running: '⏳', paused: '⏸️', completed: '✅', failed: '❌', aborted: '⏹' };
        const statusText = { running: '训练中', paused: '已暂停', completed: '已完成', failed: '失败', aborted: '已中止' };
        container.innerHTML = runs.map(r => `
            <div class="config-item" data-id="${r.id}">
                <div class="config-item-body">
                    <div class="config-item-title">${statusIcon[r.status]||'📋'} ${escapeHtml(r.name)}</div>
                    <div class="config-item-meta">
                        <span>${r.base_model_id}</span>
                        <span>${statusText[r.status]||r.status}</span>
                        <span>${r.current_epoch.toFixed(1)} epoch</span>
                        <span>${formatTimeAgo(r.created_at)}</span>
                    </div>
                </div>
                <button class="download-action-btn delete" data-id="${r.id}" title="删除">
                    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
                </button>
            </div>
        `).join('');
        container.querySelectorAll('.download-action-btn.delete').forEach(btn => {
            btn.addEventListener('click', async (e) => {
                e.stopPropagation();
                if (!confirm('确定删除此训练记录？')) return;
                await fetch(`/api/training/runs/${btn.dataset.id}`, { method: 'DELETE' });
                trainingHistoryLoaded = false;
                loadTrainingRuns();
            });
        });
    }

    // ==========================================
    // Benchmark Functions
    // ==========================================
    let benchmarkRunning = false;
    let benchmarkResults = null;

    async function loadBenchmarkView() {
        const sel = document.getElementById('bench-model-select');
        if (!sel) return;
        sel.innerHTML = '<option value="">加载中...</option>';

        // Render benchmark dataset download cards
        const benchList = document.getElementById('benchmark-download-list');
        if (benchList) {
            const benchmarks = [
                {id:'mmlu', name:'MMLU', hf:'cais/mmlu', size:'~100MB'},
                {id:'hellaswag', name:'HellaSwag', hf:'Rowan/hellaswag', size:'~50MB'},
                {id:'hle', name:'HLE', hf:'cais/hle', size:'~10MB'},
                {id:'swe_bench', name:'SWE-bench', hf:'princeton-nlp/SWE-bench_Verified', size:'~200MB'},
            ];
            benchList.innerHTML = benchmarks.map(b => `
                <div class="rec-ds-card">
                    <div class="rec-ds-name">📦 ${b.name}</div>
                    <div class="rec-ds-meta">${b.hf} · ${b.size}</div>
                    <div style="display:flex; gap:0.3rem; margin-top:0.4rem;">
                        <button class="btn-secondary bench-dl-btn" data-bench="${b.id}" data-name="${b.name}" style="flex:1; font-size:0.72rem;">一键下载</button>
                        <a href="https://huggingface.co/datasets/${b.hf}" target="_blank" class="btn-secondary" style="font-size:0.72rem; text-decoration:none; display:flex; align-items:center;">🔗</a>
                    </div>
                </div>
            `).join('');
            // Check cache status to show "已下载" where applicable
            try {
                const csRes = await fetch('/api/training/benchmark/cache-status');
                const csData = await csRes.json();
                if (csData.status === 'ok' && csData.caches) {
                    benchList.querySelectorAll('.bench-dl-btn').forEach(btn => {
                        const btype = btn.dataset.bench;
                        if (csData.caches[btype] && csData.caches[btype].cached) {
                            btn.textContent = '已下载 ✓';
                            btn.dataset.cached = 'true';
                            btn.dataset.count = csData.caches[btype].count;
                        }
                    });
                }
            } catch(e) { /* non-critical, buttons default to 一键下载 */ }
            benchList.querySelectorAll('.bench-dl-btn').forEach(btn => {
                btn.addEventListener('click', async function() {
                    this.disabled = true; this.textContent = '下载中...';
                    try {
                        const res = await fetch('/api/training/benchmark/pre-download', {
                            method:'POST', headers:{'Content-Type':'application/json'},
                            body: JSON.stringify({benchmark_type:this.dataset.bench})
                        });
                        const d = await res.json();
                        if (d.status === 'ok') {
                            this.textContent = '已下载 ✓';
                            this.dataset.cached = 'true';
                            this.dataset.count = d.count;
                            showStatus('📥 '+d.message, 'success');
                        } else {
                            showStatus('❌ '+(d.detail||'失败'), 'error');
                        }
                    } catch(e) { showStatus('❌ 网络错误','error'); }
                    this.disabled = false;
                });
            });
        }
        try {
            const res = await fetch('/api/training/all-models');
            const data = await res.json();
            const online = (data.models || []).filter(m => m.source === 'online');
            const local = (data.models || []).filter(m => m.source === 'local');
            let html = online.map(m => `<option value="${m.id}">${m.name}</option>`).join('');
            if (local.length) {
                html += '<option disabled>── 本地模型 ──</option>';
                html += local.map(m => `<option value="${m.id}">${m.name}</option>`).join('');
            }
            sel.innerHTML = html;
        } catch (e) { console.error(e); }
        loadCheckpointStatus();
        loadBenchmarkHistory();
    }

    async function loadCheckpointStatus() {
        const container = document.getElementById('benchmark-resume-container');
        if (!container) return;
        try {
            const res = await fetch('/api/training/benchmark/checkpoint-status');
            const data = await res.json();
            const ckpts = data.checkpoints || [];
            if (!ckpts.length) { container.innerHTML = ''; return; }
            container.innerHTML = ckpts.map(ck => {
                const progress = Object.entries(ck.progress || {}).map(([k,v]) => `${k}: ${v}`).join(', ');
                return `<div class="card" style="border-color:var(--theme-color); margin-bottom:0.5rem; padding:0.6rem 0.8rem;">
                    <div style="display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:0.4rem;">
                        <div>
                            <span style="font-weight:600;">📋 ${escapeHtml(ck.model_id)}</span>
                            <span style="color:var(--text-secondary); margin-left:0.5rem; font-size:0.8rem;">${progress}</span>
                        </div>
                        <button class="btn-primary resume-bench-btn"
                            data-model="${escapeHtml(ck.model_id)}"
                            data-types="${ck.benchmark_types.join(',')}"
                            style="font-size:0.75rem; padding:0.3rem 0.8rem;">恢复测评</button>
                    </div>
                </div>`;
            }).join('');
            container.querySelectorAll('.resume-bench-btn').forEach(btn => {
                btn.addEventListener('click', () => {
                    const mid = btn.dataset.model;
                    const types = btn.dataset.types.split(',');
                    // Select the model and checkboxes
                    const sel = document.getElementById('bench-model-select');
                    if (sel) sel.value = mid;
                    document.querySelectorAll('#view-training-benchmark input[type=checkbox]').forEach(cb => {
                        cb.checked = types.includes(cb.value);
                    });
                    runBenchmark(true);
                });
            });
        } catch(e) { /* non-critical */ }
    }

    async function runBenchmark(resume = false) {
        if (benchmarkRunning) return;
        const modelId = document.getElementById('bench-model-select')?.value;
        if (!modelId) { showStatus('⚠️ 请选择模型', 'error'); return; }

        const types = [...document.querySelectorAll('#view-training-benchmark input[type=checkbox]:checked')].map(cb => cb.value);
        if (!types.length) { showStatus('⚠️ 请选择测评类型', 'error'); return; }

        benchmarkRunning = true;
        const btn = document.getElementById('run-benchmark-btn');
        btn.disabled = true;
        btn.textContent = resume ? '恢复中...' : '测评中...';
        document.getElementById('benchmark-progress-card').style.display = '';
        document.getElementById('benchmark-results-card').style.display = 'none';
        document.getElementById('benchmark-progress-container').innerHTML = '<div class="field-hint">' + (resume ? '正在恢复测评...' : '正在测评...') + '</div>';

        try {
            const res = await fetch('/api/training/benchmark', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ model_id: modelId, model_source: 'online', benchmark_types: types, resume: resume })
            });
            const data = await res.json();
            displayBenchmarkResults(data);
            if (resume) loadCheckpointStatus(); // refresh resume list
        } catch (e) { showStatus('❌ 测评失败', 'error'); }
        benchmarkRunning = false;
        btn.disabled = false;
        btn.textContent = '开始测评';
    }

    function handleBenchmarkProgress(data) {
        const card = document.getElementById('benchmark-progress-card');
        const container = document.getElementById('benchmark-progress-container');
        if (!card || !container) return;
        card.style.display = '';

        const pct = Math.round((data.progress || 0) * 100);
        if (data.stage === 'loaded') {
            container.innerHTML = `<div style="padding:0.3rem 0;"><span>📋</span> <span>${escapeHtml(data.label||'')}</span></div>`;
        } else {
            container.innerHTML = `
                <div style="margin-bottom:0.4rem;">
                    <span style="font-weight:500;">${escapeHtml(data.task||'')}</span>
                    <span style="color:var(--text-secondary); margin-left:0.5rem;">${escapeHtml(data.label||'')}</span>
                </div>
                <div style="display:flex; align-items:center; gap:0.5rem;">
                    <div style="flex:1; height:6px; background:var(--border-color); border-radius:3px; overflow:hidden;">
                        <div style="width:${pct}%; height:100%; background:var(--theme-color); border-radius:3px; transition:width 0.3s;"></div>
                    </div>
                    <span style="font-size:0.75rem; color:var(--text-secondary); min-width:35px; text-align:right;">${pct}%</span>
                </div>`;
        }
    }

    function handleBenchmarkComplete(data) {
        showStatus(`✅ ${data.model_id} 测评完成`, 'success');
        loadBenchmarkHistory();
    }

    function displayBenchmarkResults(data) {
        const card = document.getElementById('benchmark-results-card');
        const content = document.getElementById('benchmark-results-content');
        if (!card || !content) return;
        card.style.display = '';

        const results = data.results || [];
        let html = `<div class="model-preview" style="margin-bottom:1rem;">
            <div class="preview-item"><label>模型</label><span>${escapeHtml(data.model_id||'')}</span></div>
            <div class="preview-item"><label>平均延迟</label><span>${(data.avg_latency_ms||0).toFixed(0)} ms</span></div>
            <div class="preview-item"><label>Token/秒</label><span>${(data.tokens_per_second||0).toFixed(1)}</span></div>
            <div class="preview-item"><label>总题数</label><span>${data.total_questions||0}</span></div>
        </div>`;

        results.forEach((r, ri) => {
            const accColor = r.accuracy >= 0.7 ? 'var(--success)' : r.accuracy >= 0.4 ? '#f59e0b' : 'var(--error)';
            html += `<div style="margin-bottom:1rem; border:1px solid var(--border-color); border-radius:8px; padding:0.8rem;">
                <div style="font-weight:700; margin-bottom:0.5rem; font-size:0.9rem;">${escapeHtml(r.name)} — 准确率: <span style="color:${accColor}">${(r.accuracy*100).toFixed(0)}%</span> (${r.correct}/${r.num_questions})</div>`;
            // Per-subject breakdown
            if (r.subjects && Object.keys(r.subjects).length > 1) {
                html += '<div style="display:flex; flex-wrap:wrap; gap:0.3rem; margin-bottom:0.5rem;">';
                Object.entries(r.subjects).forEach(([subj, s]) => {
                    const subjColor = s.accuracy >= 0.7 ? 'var(--success)' : s.accuracy >= 0.4 ? '#f59e0b' : 'var(--error)';
                    html += `<span style="font-size:0.65rem; padding:0.15rem 0.4rem; background:var(--bg-inner); border:1px solid var(--border-color); border-radius:10px;" title="${subj}: ${(s.accuracy*100).toFixed(0)}%">${subj} <b style="color:${subjColor}">${(s.accuracy*100).toFixed(0)}%</b></span>`;
                });
                html += '</div>';
            }
            // Score distribution bar
            const details = r.details || [];
            const scoreColors = {'1.0': 'var(--success)', '0.8': '#10b981', '0.7': '#34d399'};
            html += '<div style="display:flex; gap:2px; margin-bottom:0.4rem; height:4px; border-radius:2px; overflow:hidden;">';
            details.forEach(d => {
                const sc = d.score || 0;
                const scColor = sc >= 0.8 ? 'var(--success)' : sc >= 0.5 ? '#f59e0b' : sc > 0 ? 'var(--error)' : '#9ca3af';
                html += `<div style="flex:1; background:${scColor};" title="#${(d.idx||0)+1}: ${sc}"></div>`;
            });
            html += '</div>';

            // Expandable question cards
            const batchId = `bench-batch-${ri}-${Date.now()}`;
            html += `<div style="max-height:360px; overflow-y:auto; border:1px solid var(--border-color); border-radius:6px;">`;
            details.forEach((d, di) => {
                const qid = `${batchId}-q${di}`;
                const sc = d.score || 0;
                const scColor = sc >= 0.8 ? 'var(--success)' : sc >= 0.5 ? '#f59e0b' : 'var(--error)';
                const scLabel = d.error ? 'ERR' : sc.toFixed(1);
                // Use full question/answer if available, fall back to legacy truncated fields
                const question = d.question || '';
                const questionPreview = question.length > 80 ? question.substring(0, 80) + '...' : question;
                const answer = d.answer || d.answer_preview || '';
                const expected = d.expected || '';
                const hasFull = !!(d.answer);
                html += `<div style="border-bottom:1px solid var(--border-color); font-size:0.75rem;">
                    <div class="bench-q-header" data-qid="${qid}" style="display:flex; align-items:center; gap:0.4rem; padding:0.35rem 0.5rem; cursor:pointer; user-select:none; hover:bg:var(--bg-inner);">
                        <span style="font-weight:600; min-width:28px; color:var(--text-secondary);">#${(d.idx||di)+1}</span>
                        <span style="flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">${escapeHtml(questionPreview)}</span>
                        <span style="font-weight:700; min-width:32px; text-align:center; padding:0.1rem 0.3rem; border-radius:4px; font-size:0.7rem; background:${scColor}22; color:${scColor};">${scLabel}</span>
                        <span style="color:var(--text-secondary); font-size:0.65rem; min-width:45px; text-align:right;">${d.latency_ms||0}ms</span>
                        <span style="font-size:0.65rem; color:var(--text-secondary);">▶</span>
                    </div>
                    <div id="${qid}" style="display:none; padding:0.4rem 0.6rem; background:var(--bg-inner); border-top:1px solid var(--border-color);">
                        <div style="margin-bottom:0.35rem;"><span style="font-weight:600; color:var(--text-secondary);">题目:</span><div style="white-space:pre-wrap; margin-top:0.15rem;">${escapeHtml(question)}</div></div>`;
                if (d.choices && d.choices.length) {
                    const labels = ['A','B','C','D','E','F'];
                    html += `<div style="margin-bottom:0.35rem;"><span style="font-weight:600; color:var(--text-secondary);">选项:</span><div style="margin-top:0.15rem;">${d.choices.map((c,i) => `<span style="margin-right:0.6rem;">${labels[i]}) ${escapeHtml(c)}</span>`).join('')}</div></div>`;
                }
                if (d.error) {
                    html += `<div style="margin-bottom:0.35rem; color:var(--error);"><span style="font-weight:600;">错误:</span> ${escapeHtml(d.error)}</div>`;
                } else {
                    html += `<div style="margin-bottom:0.35rem;"><span style="font-weight:600; color:var(--text-secondary);">模型回答:</span><div style="white-space:pre-wrap; margin-top:0.15rem;">${escapeHtml(answer)}</div></div>`;
                }
                html += `<div style="margin-bottom:0.35rem;"><span style="font-weight:600; color:var(--text-secondary);">期望答案:</span> ${escapeHtml(expected || '(无)')}</div>
                        <div style="display:flex; gap:0.8rem; flex-wrap:wrap; font-size:0.7rem; color:var(--text-secondary);">
                            <span>得分: <b style="color:${scColor}">${sc.toFixed(2)}</b></span>
                            <span>评分方式: ${escapeHtml(d.scoring||'keyword_match')}</span>
                            <span>延迟: ${d.latency_ms||0}ms</span>
                            <span>Token: ${d.tokens||0}</span>
                            <span>科目: ${escapeHtml(d.subject||'general')}</span>
                        </div>`;
                // Scoring method explanation
                if (d.scoring === 'multiple_choice') {
                    html += `<div style="font-size:0.65rem; color:var(--text-secondary); margin-top:0.2rem; padding:0.2rem 0.35rem; background:var(--bg-color); border-radius:4px;">📋 评分规则: 首字母完全匹配=1.0，期望答案出现在前5字符=0.8，≥0.5 判为正确</div>`;
                } else if (d.scoring !== 'latency_only') {
                    html += `<div style="font-size:0.65rem; color:var(--text-secondary); margin-top:0.2rem; padding:0.2rem 0.35rem; background:var(--bg-color); border-radius:4px;">📋 评分规则: 完全匹配关键词=0.8，按词重叠比例计算补充分(max 0.7)，≥0.4 判为正确</div>`;
                }
                html += '</div></div>';
            });
            html += '</div></div>';
        });
        content.innerHTML = html;

        // Wire expand/collapse
        content.querySelectorAll('.bench-q-header').forEach(header => {
            header.addEventListener('click', function() {
                const qid = this.dataset.qid;
                const body = document.getElementById(qid);
                if (!body) return;
                const arrow = this.querySelector('span:last-child');
                if (body.style.display === 'none') {
                    body.style.display = '';
                    if (arrow) arrow.textContent = '▼';
                } else {
                    body.style.display = 'none';
                    if (arrow) arrow.textContent = '▶';
                }
            });
        });
    }

    async function loadBenchmarkHistory() {
        try {
            const res = await fetch('/api/training/benchmarks');
            const data = await res.json();
            const container = document.getElementById('benchmark-history-list');
            if (!container) return;
            const benchmarks = data.benchmarks || [];
            if (!benchmarks.length) {
                container.innerHTML = '<div class="empty-state"><p>暂无测评记录</p></div>';
                return;
            }
            container.innerHTML = benchmarks.map(b => {
                const metrics = typeof b.metrics_json === 'string' ? JSON.parse(b.metrics_json) : (b.metrics_json || []);
                let accStr = '';
                metrics.forEach(m => { accStr += `${m.name}: ${(m.accuracy*100).toFixed(0)}% `; });
                return `<div class="download-item benchmark-history-item" data-id="${b.id}" style="cursor:pointer;">
                    <div class="download-item-icon">📊</div>
                    <div class="download-item-body">
                        <div class="download-item-title">${escapeHtml(b.model_id)}</div>
                        <div class="download-item-meta">
                            <span>${accStr}</span><span>${b.avg_latency_ms}ms</span><span>${b.tokens_per_second} tok/s</span><span>${formatTimeAgo(b.created_at)}</span>
                        </div>
                    </div>
                    <button class="download-action-btn delete" data-id="${b.id}" title="删除">
                        <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polyline points="3 6 5 6 21 6"></polyline><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path></svg>
                    </button>
                </div>`;
            }).join('');
            // Click to view detail
            container.querySelectorAll('.benchmark-history-item').forEach(item => {
                item.addEventListener('click', async (e) => {
                    if (e.target.closest('.download-action-btn')) return;
                    const id = item.dataset.id;
                    const res = await fetch(`/api/training/benchmarks/${id}`);
                    const b = await res.json();
                    const metrics = typeof b.metrics_json === 'string' ? JSON.parse(b.metrics_json) : (b.metrics_json || []);
                    displayBenchmarkResults({
                        model_id: b.model_id,
                        results: metrics,
                        avg_latency_ms: b.avg_latency_ms,
                        tokens_per_second: b.tokens_per_second,
                        total_questions: b.num_questions
                    });
                    document.getElementById('view-training-benchmark').scrollIntoView({behavior:'smooth'});
                });
            });
            container.querySelectorAll('.download-action-btn.delete').forEach(btn => {
                btn.addEventListener('click', async (e) => {
                    e.stopPropagation();
                    await fetch(`/api/training/benchmarks/${btn.dataset.id}`, { method: 'DELETE' });
                    loadBenchmarkHistory();
                });
            });
        } catch (e) { console.error('loadBenchmarkHistory:', e); }
    }

    // Wire benchmark button
    setTimeout(() => {
        document.getElementById('run-benchmark-btn')?.addEventListener('click', () => runBenchmark());
    }, 300);

    // ==========================================
    // Download Manager
    // ==========================================
    async function loadDownloadsView() {
        const dst = document.getElementById('downloads-view-container');
        if (dst) dst.innerHTML = '<div class="empty-state"><div class="spinner"></div><span>加载中...</span></div>';
        await loadDownloadHistory();
        document.getElementById('scan-import-btn')?.addEventListener('click', async () => {
            const btn = document.getElementById('scan-import-btn');
            btn.disabled = true; btn.textContent = '扫描中...';
            try {
                const res = await fetch('/api/training/datasets/scan-import', { method: 'POST' });
                const data = await res.json();
                const parts = [];
                if (data.imported > 0) {
                    parts.push(`已导入 ${data.imported} 个数据集`);
                    datasetsLoaded = false;
                } else {
                    parts.push('未发现新数据集文件');
                }
                if (data.benchmarks_populated && data.benchmarks_populated.length > 0) {
                    parts.push(`已关联测评缓存: ${data.benchmarks_populated.join(', ')}，请刷新测评页面`);
                }
                showStatus('✅ ' + parts.join('；'), 'success');
            } catch (e) { showStatus('❌ 扫描失败', 'error'); }
            btn.disabled = false; btn.textContent = '扫描导入';
        });
    }

    initI18n();
    initSettingsListeners();
    initLlamaListeners();
    fetchInitialData();
    connectWebSocket();
    updateInputState();
    updateTaskBadge();
    refreshLlamaStatus();
});
