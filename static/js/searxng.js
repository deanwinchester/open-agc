// SearXNG management
import { showStatus } from './utils.js';

export async function refreshSearXNGStatus() {
    try {
        const res = await fetch('/api/searxng/status');
        if (!res.ok) return;
        const data = await res.json();

        const dockerDot = document.getElementById('searxng-docker-dot');
        const dockerText = document.getElementById('searxng-docker-text');
        const runDot = document.getElementById('searxng-run-status-dot');
        const runText = document.getElementById('searxng-run-status-text');
        const installBtn = document.getElementById('searxng-install-btn');
        const startBtn = document.getElementById('searxng-start-btn');
        const stopBtn = document.getElementById('searxng-stop-btn');
        const urlInput = document.getElementById('searxng-url-input');

        if (!dockerDot) return; // Element not rendered yet

        // Docker status
        if (data.docker_available) {
            dockerDot.style.background = 'var(--success-color, #22c55e)';
            dockerText.textContent = '可用';
            if (installBtn) installBtn.disabled = false;
        } else {
            dockerDot.style.background = 'var(--error-color, #ef4444)';
            dockerText.textContent = '不可用';
            if (installBtn) installBtn.disabled = true;
        }

        // Runtime status
        if (data.running) {
            runDot.style.background = 'var(--success-color, #22c55e)';
            runText.textContent = '运行中 (' + (data.url || 'http://localhost:' + data.port) + ')';
            if (startBtn) startBtn.disabled = true;
            if (stopBtn) stopBtn.disabled = false;
        } else {
            runDot.style.background = 'var(--text-secondary, #666)';
            runText.textContent = '已停止';
            if (startBtn) startBtn.disabled = !data.docker_available;
            if (stopBtn) stopBtn.disabled = true;
        }

        // Set placeholder from existing config
        if (urlInput && data.url && !urlInput.value) {
            urlInput.placeholder = data.url;
        }
    } catch (e) {
        console.error('Failed to refresh SearXNG status', e);
    }
}

export function initSearXNGListeners() {
    // Install button
    document.getElementById('searxng-install-btn')?.addEventListener('click', async () => {
        const btn = document.getElementById('searxng-install-btn');
        if (!btn) return;
        btn.disabled = true;
        btn.textContent = '正在安装...';
        try {
            const res = await fetch('/api/searxng/install', { method: 'POST' });
            const data = await res.json();
            if (data.status === 'success') {
                showStatus('✅ SearXNG 安装/启动成功', 'success');
            } else {
                showStatus('❌ 安装失败: ' + (data.detail || '未知错误'), 'error');
            }
        } catch (e) {
            showStatus('❌ 网络错误', 'error');
        } finally {
            btn.disabled = false;
            btn.textContent = '安装/更新 SearXNG';
            refreshSearXNGStatus();
        }
    });

    // Start button
    document.getElementById('searxng-start-btn')?.addEventListener('click', async () => {
        try {
            const res = await fetch('/api/searxng/control', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'start' })
            });
            const data = await res.json();
            if (data.status === 'success') {
                showStatus('🚀 SearXNG 已启动', 'success');
            } else {
                showStatus('❌ 启动失败: ' + (data.detail || '未知错误'), 'error');
            }
            setTimeout(refreshSearXNGStatus, 3000);
        } catch (e) {
            showStatus('❌ 网络错误', 'error');
        }
    });

    // Stop button
    document.getElementById('searxng-stop-btn')?.addEventListener('click', async () => {
        try {
            const res = await fetch('/api/searxng/control', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ action: 'stop' })
            });
            const data = await res.json();
            if (data.status === 'success') {
                showStatus('⏹ SearXNG 已停止', 'success');
            } else {
                showStatus('❌ 停止失败: ' + (data.detail || '未知错误'), 'error');
            }
            setTimeout(refreshSearXNGStatus, 2000);
        } catch (e) {
            showStatus('❌ 网络错误', 'error');
        }
    });
}
