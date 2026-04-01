document.addEventListener('DOMContentLoaded', () => {
    // ==========================================
    // DOM Elements
    // ==========================================
    const chatContainer = document.getElementById('chat-container');
    const messageInput = document.getElementById('message-input');
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
        if (viewId === 'settings-models') loadSettingsConfig();
        if (viewId === 'settings-skills') loadSkillsConfig();
        if (viewId === 'settings-mcp') loadMcpConfig();
        if (viewId === 'tasks') loadTasks();
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
        if (!isBackground && document.querySelector('.view.active')?.id !== 'view-chat') {
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
    }

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

    function handleSend() {
        const text = messageInput.value.trim();
        if (!text || !isConnected || isAgentThinking) return;
        appendMessage(text, 'user');
        ws.send(JSON.stringify({ query: text }));
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
            .catch(() => {});
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
                    fetch(`/api/tasks/${currentTaskId}/interrupt`, { method: 'POST' }).catch(() => {});
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
    initI18n();
    initSettingsListeners();
    fetchInitialData();
    connectWebSocket();
    updateInputState();
    updateTaskBadge();
});
