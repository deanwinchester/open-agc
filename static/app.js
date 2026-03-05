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

    // Settings Modal Elements
    const settingsBtn = document.getElementById('settings-btn');
    const settingsModal = document.getElementById('settings-modal');
    const closeSettingsBtn = document.getElementById('close-settings');
    const saveSettingsBtn = document.getElementById('save-settings-btn');
    const providerSelect = document.getElementById('provider-select');
    const modelInput = document.getElementById('model-input');
    const apiKeyInput = document.getElementById('api-key-input');
    const settingsNotification = document.getElementById('settings-notification');

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
                currentModelBadge.textContent = data.model_name || 'gpt-4o';

                if (data.provider) {
                    providerSelect.value = data.provider;
                }

                // Populate the dropdown correctly based on provider and select current model
                populateModelList(providerSelect.value, data.model_name);

                if (data.api_key_masked) {
                    apiKeyInput.placeholder = data.api_key_masked;
                }
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
    // Settings Modal Logic
    // ==========================================
    async function populateModelList(provider, selectedModel = null) {
        modelInput.innerHTML = '<option value="">Loading models...</option>';
        modelInput.disabled = true;

        try {
            const res = await fetch(`/api/provider-models?provider=${provider}`);
            if (res.ok) {
                const data = await res.json();
                const models = data.models || [];

                modelInput.innerHTML = '';
                models.forEach(model => {
                    const option = document.createElement('option');
                    option.value = model;
                    option.textContent = model;
                    modelInput.appendChild(option);
                });

                if (selectedModel && models.includes(selectedModel)) {
                    modelInput.value = selectedModel;
                } else if (!models.includes(selectedModel) && selectedModel) {
                    // In case the currently saved model is no longer in the standard list, still show it
                    const option = document.createElement('option');
                    option.value = selectedModel;
                    option.textContent = selectedModel + ' (Current/Unlisted)';
                    modelInput.appendChild(option);
                    modelInput.value = selectedModel;
                } else if (models.length > 0) {
                    modelInput.value = models[0];
                }
            } else {
                modelInput.innerHTML = '<option value="">Error loading models</option>';
            }
        } catch (e) {
            console.error(e);
            modelInput.innerHTML = '<option value="">Error loading models</option>';
        } finally {
            modelInput.disabled = false;
        }
    }

    settingsBtn.addEventListener('click', () => {
        settingsModal.classList.add('show');
    });

    closeSettingsBtn.addEventListener('click', () => {
        settingsModal.classList.remove('show');
        settingsNotification.style.display = 'none';
    });

    providerSelect.addEventListener('change', () => {
        populateModelList(providerSelect.value);
    });

    saveSettingsBtn.addEventListener('click', async () => {
        const payload = {
            provider: providerSelect.value,
            model_name: modelInput.value,
            api_key: apiKeyInput.value
        };

        if (!payload.api_key || !payload.model_name) {
            showNotification(currentLang === 'zh-CN' ? '请完整填写配置参数' : 'Please fill all fields', 'error');
            return;
        }

        saveSettingsBtn.disabled = true;
        try {
            const res = await fetch('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(payload)
            });

            if (res.ok) {
                showNotification(currentLang === 'zh-CN' ? '修改成功！已保存。' : 'Settings saved successfully!', 'success');
                currentModelBadge.textContent = payload.model_name;
                apiKeyInput.value = ''; // clear key after save
            } else {
                const err = await res.json();
                showNotification(err.detail || 'Failed to save settings', 'error');
            }
        } catch (e) {
            showNotification('Network error occurred.', 'error');
        } finally {
            saveSettingsBtn.disabled = false;
        }
    });

    function showNotification(msg, type) {
        settingsNotification.textContent = msg;
        settingsNotification.className = `notification ${type}`;
        settingsNotification.style.display = 'block';
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
        else if (data.type === 'message') {
            hideThinkingStatus();
            appendMessage(data.content, 'agent');
            isAgentThinking = false;
            updateInputState();
        }
        else if (data.type === 'error') {
            hideThinkingStatus();
            const msg = translations[currentLang] ? translations[currentLang]['agent_error'] : "Error";
            appendMessage(`**${msg}**: ${data.content}`, 'system');
            isAgentThinking = false;
            updateInputState();
        }
    }

    // UI Helpers
    function appendMessage(content, role) {
        const messageDiv = document.createElement('div');
        messageDiv.className = `message ${role}`;

        let avatarSvg = '';
        if (role === 'user') {
            avatarSvg = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"></path><circle cx="12" cy="7" r="4"></circle></svg>`;
        } else {
            avatarSvg = `<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"></path></svg>`;
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
