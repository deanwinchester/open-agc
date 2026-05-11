// Session management — calls back into main app via window for chat/WS operations
import { state } from './state.js';

export async function loadSessions() {
  try {
    const res = await fetch('/api/sessions');
    const data = await res.json();
    state.sessions = data.sessions || [];
    renderSessionList();
  } catch (e) {
    console.error('Failed to load sessions', e);
  }
}

function renderSessionList() {
  const container = document.getElementById('session-list');
  if (!container) return;
  container.innerHTML = state.sessions.map(s => `
    <div class="session-item${s.id === state.currentSessionId ? ' active' : ''}" data-session-id="${s.id}">
      <span class="session-name" title="${s.name}">${s.name}</span>
      <span class="session-actions">
        <button class="session-rename-btn" title="重命名">✎</button>
        <button class="session-delete-btn" title="删除">×</button>
      </span>
    </div>
  `).join('');
}

export async function switchSession(sessionId) {
  if (sessionId === state.currentSessionId) return;
  state.currentSessionId = sessionId;
  window.switchView?.('chat');
  localStorage.setItem('lastSessionId', sessionId);
  if (state.ws) state.ws.close();
  window.connectWebSocket?.();
  const chatContainer = document.getElementById('chat-container');
  chatContainer.innerHTML = '';
  try {
    const res = await fetch(`/api/history?session_id=${sessionId}`);
    const data = await res.json();
    if (data.history && data.history.length > 0) {
      data.history.forEach(msg => window.appendMessage?.(msg.content, msg.role));
    } else {
      window.appendMessage?.('*控制台*', 'system');
    }
  } catch (e) {
    console.error('Failed to load session history', e);
    window.appendMessage?.('*控制台*', 'system');
  }
  renderSessionList();
}

export async function createSession() {
  try {
    const res = await fetch('/api/sessions', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}'
    });
    const data = await res.json();
    await loadSessions();
    await switchSession(data.session.id);
  } catch (e) {
    console.error('Failed to create session', e);
  }
}

export async function deleteSession(sessionId) {
  if (state.sessions.length <= 1) return;
  try {
    await fetch(`/api/sessions/${sessionId}`, { method: 'DELETE' });
    if (state.currentSessionId === sessionId) {
      const next = state.sessions.find(s => s.id !== sessionId);
      if (next) await switchSession(next.id);
    }
    await loadSessions();
  } catch (e) {
    console.error('Failed to delete session', e);
  }
}

export async function renameSession(sessionId, newName) {
  if (!newName.trim()) return;
  try {
    await fetch(`/api/sessions/${sessionId}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: newName.trim() })
    });
    if (sessionId === state.currentSessionId) {
      const s = state.sessions.find(s => s.id === sessionId);
      if (s) s.name = newName.trim();
    }
    renderSessionList();
  } catch (e) {
    console.error('Failed to rename session', e);
  }
}
