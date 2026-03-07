document.addEventListener("DOMContentLoaded", () => {
    // ── Theme ──
    const savedTheme = localStorage.getItem("theme") || "light";
    document.documentElement.setAttribute("data-theme", savedTheme);

    document.getElementById("theme-toggle").addEventListener("click", () => {
        const cur = document.documentElement.getAttribute("data-theme");
        const next = cur === "light" ? "dark" : "light";
        document.documentElement.setAttribute("data-theme", next);
        localStorage.setItem("theme", next);
    });

    // ── Tab Navigation ──
    const tabs = document.querySelectorAll(".tab");
    const panes = document.querySelectorAll(".pane");
    tabs.forEach(tab => {
        tab.addEventListener("click", () => {
            tabs.forEach(t => t.classList.remove("active"));
            panes.forEach(p => p.classList.remove("active"));
            tab.classList.add("active");
            document.getElementById(tab.dataset.target).classList.add("active");
        });
    });

    // ── Constants ──
    const providers = [
        { key: "kimi", label: "Kimi (Moonshot)" },
        { key: "ollama", label: "Ollama (本地/Local)" },
        { key: "openai", label: "OpenAI" },
        { key: "anthropic", label: "Anthropic" },
        { key: "gemini", label: "Google Gemini" },
        { key: "deepseek", label: "DeepSeek" },
        { key: "glm", label: "GLM (智谱)" },
        { key: "minimax", label: "MiniMax" }
    ];

    // ── Load Config ──
    async function loadConfig() {
        try {
            const res = await fetch("/api/settings");
            const data = await res.json();

            buildApiKeysGrid(data.api_keys_masked || {});
            buildModelSelection(data);
            loadSkillsConfig(data.disabled_skills || []);

            document.getElementById("sandbox-mode-toggle").checked = data.sandbox_mode ?? true;
            document.getElementById("sandbox-dir-input").value = data.sandbox_dir || "";
            document.getElementById("heartbeat-toggle").checked = data.heartbeat_enabled ?? false;

            // Email Configs
            document.getElementById("email-listener-toggle").checked = data.email_listener_enabled ?? false;
            document.getElementById("owner-email-input").value = data.owner_email || "";
            document.getElementById("email-account-input").value = data.email_account || "";
            document.getElementById("email-password-input").placeholder = data.email_password ? "***" : "密码或独立应用授权码";
            document.getElementById("email-imap-input").value = data.email_imap_server || "";
            document.getElementById("email-smtp-input").value = data.email_smtp_server || "";

            // Agents and MCP Configs
            if (data.agent_profiles) {
                document.getElementById("agents-config-input").value = typeof data.agent_profiles === "string" ? data.agent_profiles : JSON.stringify(data.agent_profiles, null, 2);
            }
            if (data.mcp_servers) {
                document.getElementById("mcp-config-input").value = typeof data.mcp_servers === "string" ? data.mcp_servers : JSON.stringify(data.mcp_servers, null, 2);
            }

        } catch (err) {
            console.error("Failed to load config:", err);
        }
    }

    // ── API Keys Grid ──
    function buildApiKeysGrid(maskedKeys) {
        const grid = document.getElementById("api-keys-container");
        grid.innerHTML = "";

        providers.forEach(p => {
            const mask = maskedKeys[p.key] || "";
            const hasSaved = mask.length > 0;

            const wrapper = document.createElement("div");
            wrapper.className = "key-field";
            wrapper.innerHTML = `
                <label>${p.label}</label>
                <div class="key-input-wrapper">
                    <input type="password"
                           id="key-${p.key}"
                           placeholder="${hasSaved ? mask : '请输入密钥...'}"
                           autocomplete="new-password"
                           spellcheck="false">
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

        // Toggle password visibility
        grid.querySelectorAll(".toggle-visibility").forEach(btn => {
            btn.addEventListener("click", () => {
                const input = document.getElementById(btn.dataset.target);
                input.type = input.type === "password" ? "text" : "password";
            });
        });
    }

    // ── Model Selection ──
    async function buildModelSelection(data) {
        let selectedProvider = "kimi";
        const dm = data.default_model || "";
        if (dm.startsWith("moonshot/")) selectedProvider = "kimi";
        else if (dm.startsWith("ollama/")) selectedProvider = "ollama";
        else if (dm.startsWith("zai/")) selectedProvider = "glm";
        else if (dm.startsWith("minimax/")) selectedProvider = "minimax";
        else if (dm.startsWith("gemini/")) selectedProvider = "gemini";
        else if (dm.startsWith("deepseek/")) selectedProvider = "deepseek";
        else if (dm.includes("claude")) selectedProvider = "anthropic";
        else if (dm.startsWith("gpt")) selectedProvider = "openai";

        document.getElementById("provider-select").value = selectedProvider;

        const fallbackInput = document.getElementById("fallback-models-input");
        fallbackInput.value = (data.fallback_models || []).join(", ");

        await fetchModels(selectedProvider, dm);
    }

    async function fetchModels(provider, modelToSelect = null) {
        const select = document.getElementById("model-name-select");
        select.innerHTML = '<option>加载中...</option>';
        try {
            const res = await fetch("/api/provider-models?provider=" + provider);
            const data = await res.json();
            select.innerHTML = "";
            (data.models || []).forEach(m => {
                const opt = document.createElement("option");
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
    }

    document.getElementById("provider-select").addEventListener("change", (e) => {
        fetchModels(e.target.value);
    });

    document.getElementById("fetch-models-btn").addEventListener("click", () => {
        fetchModels(document.getElementById("provider-select").value);
    });

    // ── Skills Config ──
    async function loadSkillsConfig() {
        const container = document.getElementById("skills-config-container");
        container.innerHTML = `<div class="loading-indicator"><div class="spinner"></div><span>加载中...</span></div>`;
        try {
            const res = await fetch("/api/skills");
            const data = await res.json();
            container.innerHTML = "";

            if (!data.skills || data.skills.length === 0) {
                container.innerHTML = '<p style="color:var(--s-text-hint);text-align:center;padding:2rem;">暂无可管理的技能</p>';
                return;
            }

            data.skills.forEach(s => {
                const isChecked = s.enabled ? "checked" : "";
                const icon = s.type === "md" ? "📄" : "🐍";
                const displayName = s.name && s.name !== "undefined" ? s.name : (s.filename || "Undefined Skill");
                const skillLabel = s.type === 'md' ? 'Markdown Prompt' : '大模型技能';

                const div = document.createElement("div");
                div.className = "skill-row";
                div.innerHTML = `
                    <div class="skill-info">
                        <strong>${icon} ${displayName}</strong>
                        <small>${skillLabel}</small>
                    </div>
                    <div class="skill-actions" style="display:flex; align-items:center; gap: 0.8rem;">
                        <button class="btn-secondary btn-edit-skill" style="padding: 0.2rem 0.6rem; font-size: 0.85rem;" data-filename="${s.filename}">编辑</button>
                        <label class="switch">
                            <input type="checkbox" class="skill-toggle" data-name="${s.filename || s.name}" ${isChecked}>
                            <span class="slider"></span>
                        </label>
                    </div>
                `;
                container.appendChild(div);
            });

            // Bind edit buttons
            container.querySelectorAll('.btn-edit-skill').forEach(btn => {
                btn.addEventListener('click', () => openEditSkillModal(btn.dataset.filename));
            });
        } catch (e) {
            container.innerHTML = '<p style="color:#ef4444;text-align:center;padding:2rem;">加载技能列表失败</p>';
        }
    }

    // ── Save ──
    document.getElementById("save-settings-btn").addEventListener("click", async () => {
        const saveBtn = document.getElementById("save-settings-btn");
        const statusEl = document.getElementById("save-status");

        const payload = {
            api_keys: {},
            default_model: document.getElementById("model-name-select").value || "moonshot/kimi-latest",
            fallback_models: document.getElementById("fallback-models-input").value
                .split(",").map(s => s.trim()).filter(s => s.length > 0),
            disabled_skills: [],
            sandbox_mode: document.getElementById("sandbox-mode-toggle").checked,
            sandbox_dir: document.getElementById("sandbox-dir-input").value.trim(),
            heartbeat_enabled: document.getElementById("heartbeat-toggle").checked,
            heartbeat_interval: 60,
            email_listener_enabled: document.getElementById("email-listener-toggle").checked,
            email_account: document.getElementById("email-account-input").value.trim(),
            email_password: document.getElementById("email-password-input").value || (document.getElementById("email-password-input").placeholder === "***" ? "***" : ""),
            email_imap_server: document.getElementById("email-imap-input").value.trim(),
            email_smtp_server: document.getElementById("email-smtp-input").value.trim(),
            owner_email: document.getElementById("owner-email-input").value.trim(),
            agent_profiles: document.getElementById("agents-config-input").value.trim() || "[]",
            mcp_servers: document.getElementById("mcp-config-input").value.trim() || "{}"
        };

        providers.forEach(p => {
            const val = document.getElementById(`key-${p.key}`).value;
            if (val && val.trim().length > 0) {
                payload.api_keys[p.key] = val.trim();
            }
        });

        document.querySelectorAll(".skill-toggle").forEach(cb => {
            if (!cb.checked) {
                payload.disabled_skills.push(cb.dataset.name);
            }
        });

        saveBtn.disabled = true;
        statusEl.textContent = "保存中...";
        statusEl.className = "save-status";

        try {
            const res = await fetch("/api/settings", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify(payload)
            });
            const data = await res.json();

            if (data.status === "success") {
                statusEl.textContent = "✓ 保存成功！";
                statusEl.className = "save-status success";
                // Reload to reflect saved key masks
                setTimeout(() => {
                    statusEl.textContent = "";
                    loadConfig();
                }, 2000);
            } else {
                statusEl.textContent = "✗ 保存失败: " + (data.detail || "未知错误");
                statusEl.className = "save-status error";
            }
        } catch (e) {
            statusEl.textContent = "✗ 网络错误";
            statusEl.className = "save-status error";
        } finally {
            saveBtn.disabled = false;
        }
    });

    // ── Edit Skill Modal ──
    const editSkillModal = document.getElementById("edit-skill-modal");
    const editSkillFilename = document.getElementById("edit-skill-filename");
    const editSkillContent = document.getElementById("edit-skill-content");
    const editSkillClose = document.getElementById("edit-skill-close");
    const editSkillSave = document.getElementById("edit-skill-save");

    async function openEditSkillModal(filename) {
        if (!filename) return;
        editSkillFilename.textContent = filename;
        editSkillContent.value = "正在读取内容...";
        editSkillModal.classList.add("show");

        try {
            const res = await fetch(`/api/skills/${encodeURIComponent(filename)}`);
            if (res.ok) {
                const data = await res.json();
                editSkillContent.value = data.content || "";
            } else {
                editSkillContent.value = "读取技能内容失败。";
            }
        } catch (err) {
            editSkillContent.value = "读取技能内容发生网络错误。";
        }
    }

    editSkillClose.addEventListener("click", () => {
        editSkillModal.classList.remove("show");
    });

    editSkillSave.addEventListener("click", async () => {
        const filename = editSkillFilename.textContent;
        const content = editSkillContent.value;
        const ogText = editSkillSave.textContent;

        editSkillSave.textContent = "保存中...";
        editSkillSave.disabled = true;

        try {
            const res = await fetch("/api/skills/import", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ filename, content, force: true })
            });
            const data = await res.json();

            if (data.success) {
                editSkillSave.textContent = "✓ 已保存";
                setTimeout(() => {
                    editSkillModal.classList.remove("show");
                    editSkillSave.textContent = ogText;
                    editSkillSave.disabled = false;
                }, 1000);
            } else {
                alert("保存失败: " + data.message);
                editSkillSave.textContent = ogText;
                editSkillSave.disabled = false;
            }
        } catch (e) {
            alert("保存时发生网络错误");
            editSkillSave.textContent = ogText;
            editSkillSave.disabled = false;
        }
    });

    // ── Init ──
    loadConfig();
});
