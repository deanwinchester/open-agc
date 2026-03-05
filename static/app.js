document.addEventListener('DOMContentLoaded', () => {
    // DOM Elements
    const chatContainer = document.getElementById('chat-container');
    const messageInput = document.getElementById('message-input');
    const sendBtn = document.getElementById('send-btn');
    const themeToggle = document.getElementById('theme-toggle');
    const htmlElement = document.documentElement;
    const currentModelBadge = document.getElementById('current-model-badge');

    // Skills Elements
    const skillsContainer = document.getElementById('skills-container');

    // Settings Page Transition
    const settingsBtn = document.getElementById('settings-btn');

    // State
    let ws = null;
    let isConnected = false;
    let isAgentThinking = false;

    // ==========================================
    // i18n Localization
    // ==========================================
    const translations = {
        'en': {
            'title': 'Open-AGC Panda 🐼',
            'new_session': 'New Session',
            'skills_lib': 'Skills Library',
            'loading': 'Loading...',
            'workspace': 'Panda Workspace',
            'placeholder': 'Ask Panda to do something on this computer...',
            'footer_warning': 'Open-AGC AI can make mistakes. Verify critical actions before permitting.',
            'settings_title': 'Agent Settings',
            'provider': 'AI Provider',
            'model_name': 'Model Name',
            'api_key': 'API Key',
            'save_changes': 'Save Changes',
            'agent_thinking': 'Panda is thinking...',
            'agent_error': 'Panda Encountered an Error'
        },
        'zh-CN': {
            'title': 'Open-AGC 熊猫 🐼',
            'new_session': '开启新会话',
            'skills_lib': '技能图鉴',
            'loading': '加载中...',
            'workspace': '控制台',
            'placeholder': '告诉熊猫，你想在电脑上做什么...',
            'footer_warning': 'Open-AGC 的操作可能会出现失误。请在危急时移动鼠标到边角强制停止。',
            'settings_title': '全局设置',
            'provider': '大模型厂商',
            'model_name': '模型版本号',
            'api_key': 'API 密钥',
            'save_changes': '保存配置',
            'agent_thinking': '熊猫正在思考对策...',
            'agent_error': '哎呀，熊猫摔了一跤 (发生错误)'
        }
    };

    let currentLang = 'en';
    function initI18n() {
        const userLang = navigator.language || navigator.userLanguage;
        if (userLang.startsWith('zh')) {
            currentLang = 'zh-CN';
        }

        document.querySelectorAll('[data-i18n]').forEach(el => {
            const key = el.getAttribute('data-i18n');
            const prop = el.getAttribute('data-i18n-prop'); // e.g., 'placeholder'

            if (translations[currentLang] && translations[currentLang][key]) {
                if (prop) {
                    el.setAttribute(prop, translations[currentLang][key]);
                } else {
                    el.textContent = translations[currentLang][key];
                }
            }
        });

        // Translate welcome message if present
        const welcomeMsg = document.getElementById('welcome-msg');
        if (welcomeMsg) {
            welcomeMsg.textContent = currentLang === 'zh-CN'
                ? "你好！我是熊猫，你的专属电脑控制智能体。我可以帮你执行命令行、管理文件或运行代码。今天需要我做什么？"
                : "Hello! I am Panda, your Agentic Computer Assistant. I can execute commands, manage files, and run code. How can I help you today?";
        }
    }

    // ==========================================
    // Fetch Settings and Skills on Load
    // ==========================================
    async function fetchInitialData() {
        try {
            // Fetch Current Model Settings
            const settingsRes = await fetch('/api/settings');
            if (settingsRes.ok) {
                const data = await settingsRes.json();
                currentModelBadge.textContent = data.default_model || 'gpt-4o';
            }

            // Fetch Skills
            const skillsRes = await fetch('/api/skills');
            if (skillsRes.ok) {
                const data = await skillsRes.json();
                skillsContainer.innerHTML = '';
                if (data.skills && data.skills.length > 0) {
                    data.skills.forEach(skill => {
                        const icon = skill.type === 'md' ? '📄' : '🐍';
                        const el = document.createElement('div');
                        el.className = 'skill-item';
                        el.innerHTML = `<span>${icon}</span> <span>${skill.name}</span>`;
                        skillsContainer.appendChild(el);
                    });
                } else {
                    skillsContainer.innerHTML = `<div class="skill-item" style="color:var(--text-secondary);justify-content:center;">${currentLang === 'zh-CN' ? '暂无加载技能' : 'No skills loaded'}</div>`;
                }
            }

            // Fetch Chat History
            const historyRes = await fetch('/api/history');
            if (historyRes.ok) {
                const data = await historyRes.json();
                if (data.history && data.history.length > 0) {
                    // Clear the default welcome message since we have history
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
    // Settings Navigation
    // ==========================================
    settingsBtn.addEventListener('click', () => {
        window.location.href = '/static/settings.html';
    });

    // ==========================================
    // WebSocket & Chat Logic
    // ==========================================
    function connectWebSocket() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const wsUrl = `${protocol}//${window.location.host}/ws`;

        ws = new WebSocket(wsUrl);

        ws.onopen = () => {
            isConnected = true;
            console.log('WebSocket connected');
            updateInputState();
        };

        ws.onmessage = (event) => {
            const data = JSON.parse(event.data);
            handleServerMessage(data);
        };

        ws.onclose = () => {
            isConnected = false;
            // Reconnect logic
            setTimeout(connectWebSocket, 3000);
            updateInputState();
        };

        ws.onerror = (error) => {
            console.error('WebSocket Error:', error);
        };
    }

    function handleServerMessage(data) {
        if (data.type === 'status') {
            const msg = translations[currentLang] ? translations[currentLang]['agent_thinking'] : data.message;
            showThinkingStatus(msg);
        }
        else if (data.type === 'progress') {
            handleProgressEvent(data);
        }
        else if (data.type === 'message') {
            hideThinkingStatus();
            hideProgressContainer();
            appendMessage(data.content, 'agent');
            if (wasVoiceQuery) {
                speakText(data.content);
                wasVoiceQuery = false;
            }
            isAgentThinking = false;
            updateInputState();
        }
        else if (data.type === 'error') {
            hideThinkingStatus();
            hideProgressContainer();
            const msg = translations[currentLang] ? translations[currentLang]['agent_error'] : "Error";
            appendMessage(`**${msg}**: ${data.content}`, 'system');

            // Show retry bar
            showRetryBar(data.original_query || '', data.content);

            // Check if this is a permission-related error
            checkPermissionError(data.content);

            isAgentThinking = false;
            updateInputState();
        }
    }

    // ==========================================
    // Retry Bar
    // ==========================================
    function showRetryBar(originalQuery, errorContent) {
        const retryBar = document.createElement('div');
        retryBar.className = 'retry-bar';

        const isZh = currentLang === 'zh-CN';

        retryBar.innerHTML = `
            <button class="retry-btn" title="${isZh ? '使用相同模型重试' : 'Retry with same model'}">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                    <polyline points="23 4 23 10 17 10"></polyline>
                    <path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"></path>
                </svg>
                ${isZh ? '重试' : 'Retry'}
            </button>
            <button class="retry-btn retry-btn-alt" title="${isZh ? '跳过此步骤继续' : 'Continue'}">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5">
                    <polygon points="5 3 19 12 5 21 5 3"></polygon>
                </svg>
                ${isZh ? '继续' : 'Continue'}
            </button>
        `;

        chatContainer.appendChild(retryBar);
        scrollToBottom();

        // Retry button: re-send the same query
        retryBar.querySelector('.retry-btn').addEventListener('click', () => {
            retryBar.remove();
            if (ws && isConnected && originalQuery) {
                isAgentThinking = true;
                updateInputState();
                ws.send(JSON.stringify({ type: "retry", query: originalQuery }));
            }
        });

        // Continue button: tell agent to skip/continue
        retryBar.querySelector('.retry-btn-alt').addEventListener('click', () => {
            retryBar.remove();
            if (ws && isConnected) {
                const continueMsg = currentLang === 'zh-CN'
                    ? '上一步操作失败了，请跳过这一步，继续完成剩余的任务。'
                    : 'The previous step failed. Please skip it and continue with the remaining tasks.';
                appendMessage(continueMsg, 'user');
                isAgentThinking = true;
                updateInputState();
                ws.send(JSON.stringify({ query: continueMsg }));
            }
        });
    }

    // ==========================================
    // Progress Tracking UI
    // ==========================================
    let progressContainer = null;
    let progressSteps = {};

    function ensureProgressContainer() {
        if (!progressContainer) {
            hideThinkingStatus(); // Replace the simple spinner
            progressContainer = document.createElement('div');
            progressContainer.className = 'progress-container';
            progressContainer.innerHTML = `
                <div class="progress-header">
                    <div class="progress-spinner"></div>
                    <span class="progress-title">${currentLang === 'zh-CN' ? '🐼 执行中...' : '🐼 Working...'}</span>
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
            const thinkMsg = currentLang === 'zh-CN' ? '熊猫正在思考对策...' : 'Thinking...';
            showThinkingStatus(thinkMsg);
            return;
        }

        if (event === 'model_switched') {
            ensureProgressContainer();
            const stepsEl = progressContainer.querySelector('.progress-steps');
            const switchNote = document.createElement('div');
            switchNote.className = 'progress-step model-switch';
            switchNote.innerHTML = `
                <span class="step-icon">🔄</span>
                <span class="step-text">${currentLang === 'zh-CN'
                    ? `模型已切换: ${data.from} → <strong>${data.to}</strong>`
                    : `Model switched: ${data.from} → <strong>${data.to}</strong>`}</span>
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
                // Add result preview
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
            scrollToBottom();
            return;
        }
    }

    function hideProgressContainer() {
        if (progressContainer) {
            // Mark it as complete instead of removing
            progressContainer.classList.add('completed');
            const titleEl = progressContainer.querySelector('.progress-title');
            if (titleEl) {
                titleEl.textContent = currentLang === 'zh-CN' ? '✨ 执行完成' : '✨ Done';
            }
            const spinnerEl = progressContainer.querySelector('.progress-spinner');
            if (spinnerEl) spinnerEl.style.display = 'none';
            progressContainer = null;
            progressSteps = {};
        }
    }

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // Permission error detection and dialog
    function checkPermissionError(errorText) {
        const lower = errorText.toLowerCase();
        const permKeywords = ['permission', 'denied', '权限', 'access denied', 'not permitted',
            'operation not permitted', 'eacces', 'eperm', 'chmod'];

        const isPermError = permKeywords.some(kw => lower.includes(kw));
        if (!isPermError) return;

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

    // Modal close / copy handlers
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

    // UI Helpers
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

        // Parse Markdown for Agent & System
        let formattedContent = content;
        if (role === 'agent' || role === 'system') {
            formattedContent = marked.parse(content);
        }

        messageDiv.innerHTML = `
            <div class="avatar">${avatarSvg}</div>
            <div class="content">${formattedContent}</div>
        `;

        chatContainer.appendChild(messageDiv);

        // Highlight code blocks
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
        chatContainer.scrollTo({
            top: chatContainer.scrollHeight,
            behavior: 'smooth'
        });
    }

    function updateInputState() {
        if (isAgentThinking) {
            messageInput.disabled = true;
        } else {
            messageInput.disabled = false;
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
    // Voice Interaction (Web Speech API)
    // ==========================================
    const micBtn = document.getElementById('mic-btn');
    let recognition = null;
    let isListening = false;
    let wasVoiceQuery = false;

    if ('webkitSpeechRecognition' in window || 'SpeechRecognition' in window) {
        const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        recognition = new SpeechRecognition();
        recognition.continuous = false;
        recognition.interimResults = false;

        recognition.onstart = () => {
            isListening = true;
            micBtn.classList.add('listening');
            messageInput.placeholder = currentLang === 'zh-CN' ? "请说话..." : "Listening...";
        };

        recognition.onresult = (event) => {
            const transcript = event.results[0][0].transcript;
            messageInput.value = transcript;
            wasVoiceQuery = true;
            handleSend();
        };

        recognition.onerror = (event) => {
            console.error("Speech recognition error", event.error);
            stopListening();
        };

        recognition.onend = () => {
            stopListening();
        };
    } else {
        if (micBtn) micBtn.style.display = 'none';
    }

    function stopListening() {
        isListening = false;
        if (micBtn) micBtn.classList.remove('listening');
        const defaultPlaceholder = translations[currentLang] ? translations[currentLang]['placeholder'] : "Ask Panda...";
        messageInput.placeholder = defaultPlaceholder;
    }

    if (micBtn) {
        micBtn.addEventListener('click', () => {
            if (!recognition) return;
            if (isListening) {
                recognition.stop();
            } else {
                try {
                    recognition.lang = currentLang === 'zh-CN' ? 'zh-CN' : 'en-US';
                    recognition.start();
                } catch (e) { }
            }
        });
    }

    function speakText(text) {
        if (!('speechSynthesis' in window)) return;
        window.speechSynthesis.cancel();
        // Remove markdown elements to improve speech output pronunciation
        const cleanText = text.replace(/[*#`_]|<\/?[\w\s="\/.\':;#-]+>/gi, '');
        const utterance = new SpeechSynthesisUtterance(cleanText);
        utterance.lang = currentLang === 'zh-CN' ? 'zh-CN' : 'en-US';
        window.speechSynthesis.speak(utterance);
    }

    // Core Event Listeners
    sendBtn.addEventListener('click', handleSend);

    messageInput.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleSend();
        }
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
    });

    document.getElementById('new-chat-btn').addEventListener('click', () => {
        chatContainer.innerHTML = '';
        const msg = translations[currentLang] ? translations[currentLang]['workspace'] : "New Workspace Started";
        appendMessage(`*${msg}*`, 'system');
        // A full implementation would close the active WS connection and open a new one.
    });

    // ==========================================
    // Initialization
    // ==========================================
    initI18n();
    fetchInitialData();
    connectWebSocket();
    updateInputState();
});
