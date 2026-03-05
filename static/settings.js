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
        { key: "openai", label: "OpenAI" },
        { key: "anthropic", label: "Anthropic" },
        { key: "gemini", label: "Google Gemini" },
        { key: "deepseek", label: "DeepSeek" },
        { key: "kimi", label: "Kimi (Moonshot)" },
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
        let selectedProvider = "openai";
        const dm = data.default_model || "";
        if (dm.startsWith("moonshot/")) selectedProvider = "kimi";
        else if (dm.startsWith("zai/")) selectedProvider = "glm";
        else if (dm.startsWith("minimax/")) selectedProvider = "minimax";
        else if (dm.startsWith("gemini/")) selectedProvider = "gemini";
        else if (dm.startsWith("deepseek/")) selectedProvider = "deepseek";
        else if (dm.includes("claude")) selectedProvider = "anthropic";

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
                const div = document.createElement("div");
                div.className = "skill-row";
                div.innerHTML = `
                    <div class="skill-info">
                        <strong>${icon} ${s.name}</strong>
                        <small>${s.type === 'md' ? 'Markdown Skill' : 'Python Plugin'}</small>
                    </div>
                    <label class="switch">
                        <input type="checkbox" class="skill-toggle" data-name="${s.name}" ${isChecked}>
                        <span class="slider"></span>
                    </label>
                `;
                container.appendChild(div);
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
            default_model: document.getElementById("model-name-select").value || "gpt-4o",
            fallback_models: document.getElementById("fallback-models-input").value
                .split(",").map(s => s.trim()).filter(s => s.length > 0),
            disabled_skills: []
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

    // ── Init ──
    loadConfig();
});
