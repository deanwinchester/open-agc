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
  container.innerHTML = state.sessions.map(s => {
    const isDefault = s.id === 1;
    const deleteBtn = isDefault
      ? `<button class="session-clear-btn" title="清空数据">⟳</button>`
      : `<button class="session-delete-btn" title="删除">×</button>`;
    return `
      <div class="session-item${s.id === state.currentSessionId ? ' active' : ''}" data-session-id="${s.id}">
        <span class="session-name" title="${s.name}">${s.name}</span>
        <span class="session-actions">
          <button class="session-rename-btn" title="重命名">✎</button>
          ${deleteBtn}
        </span>
      </div>
    `;
  }).join('');
}

export async function switchSession(sessionId) {
  if (sessionId === state.currentSessionId) {
    // Still switch to chat view even if same session
    window.switchView?.('chat');
    return;
  }
  var prevId = state.currentSessionId;
  state.currentSessionId = sessionId;
  state.settingsLoaded = false;  // Reload settings for new session's email config
  window.switchView?.('chat');
  localStorage.setItem('lastSessionId', sessionId);
  // Don't close WebSocket — the agent task keeps running regardless of which
  // session we're viewing. Messages arrive through the same connection.
  const chatContainer = document.getElementById('chat-container');
  // Cache current chat DOM for the session we're leaving (max 10 entries, LRU)
  if (!window._sessionChatCache) window._sessionChatCache = {};
  if (!window._sessionChatOrder) window._sessionChatOrder = [];
  if (prevId !== sessionId && chatContainer) {
    window._sessionChatCache[prevId] = chatContainer.innerHTML;
    // Track LRU order
    var idx = window._sessionChatOrder.indexOf(prevId);
    if (idx !== -1) window._sessionChatOrder.splice(idx, 1);
    window._sessionChatOrder.push(prevId);
    // Evict oldest entries beyond the cap
    while (window._sessionChatOrder.length > 10) {
      var oldId = window._sessionChatOrder.shift();
      delete window._sessionChatCache[oldId];
      delete window._sessionChatCache['_evt_' + oldId];
    }
  }
  // Reset pagination state for this session
  if (!window._sessionPageState) window._sessionPageState = {};
  window._sessionPageState[sessionId] = { oldestId: 0, hasMore: true, loading: false };

  chatContainer.innerHTML = '';
  await loadHistoryPage(sessionId, true);

  // Remove the previous scroll handler before adding a new one
  if (window._sessionScrollHandler) {
    chatContainer.removeEventListener('scroll', window._sessionScrollHandler);
  }
  // Wire up scroll-to-top to load older messages
  const onScroll = async () => {
    // Guard: only load history for the currently active session
    if (state.currentSessionId !== sessionId) return;
    const st = window._sessionPageState[sessionId];
    if (!st || st.loading || !st.hasMore) return;
    if (chatContainer.scrollTop < 100) {
      st.loading = true;
      const prevHeight = chatContainer.scrollHeight;
      await loadHistoryPage(sessionId, false, st.oldestId);
      // Maintain scroll position after prepending
      if (chatContainer.scrollHeight > prevHeight) {
        chatContainer.scrollTop = chatContainer.scrollHeight - prevHeight;
      }
      st.loading = false;
    }
  };
  window._sessionScrollHandler = onScroll;
  chatContainer.addEventListener('scroll', onScroll);
  // Reconnect WebSocket to the new session so history_steps are replayed
  if (prevId !== sessionId && window.connectWebSocket) {
    window._intentionalClose = true;
    if (state.ws) {
      // Detach old handlers so they don't fire reconnect after we close
      state.ws.onclose = null;
      state.ws.onerror = null;
      state.ws.close();
    }
    // Clear any pending reconnect timer
    if (window._wsReconnectTimer) {
      clearTimeout(window._wsReconnectTimer);
      window._wsReconnectTimer = null;
    }
    window.connectWebSocket();
  }
  renderSessionList();
}

async function loadHistoryPage(sessionId, scrollToBottom, beforeId) {
  const chatContainer = document.getElementById('chat-container');
  if (!chatContainer) return;
  const params = `session_id=${sessionId}&limit=100${beforeId ? '&before_id=' + beforeId : ''}`;
  try {
    const res = await fetch(`/api/history?${params}`);
    const data = await res.json();
    const st = window._sessionPageState?.[sessionId];
    if (st) { st.oldestId = data.oldest_id; st.hasMore = data.has_more; }
    if (data.history && data.history.length > 0) {
      window._loadingHistory = true;
      if (beforeId) {
        // Render and move each message to top (reverse to preserve order)
        const msgs = data.history;
        for (let i = msgs.length - 1; i >= 0; i--) {
          window.appendMessage?.(msgs[i].content, msgs[i].role);
          const added = chatContainer.lastChild;
          if (added) chatContainer.insertBefore(added, chatContainer.firstChild);
        }
      } else {
        chatContainer.innerHTML = '';
        data.history.forEach(msg => {
          window.appendMessage?.(msg.content, msg.role);
        });
      }
      window._loadingHistory = false;

      if (!beforeId && scrollToBottom) {
        // Use instant scroll and multiple timeouts to ensure it scrolls to the bottom after layout rendering
        window.scrollToBottom?.(true, false);
        setTimeout(() => {
          window.scrollToBottom?.(true, false);
        }, 50);
        setTimeout(() => {
          window.scrollToBottom?.(true, false);
        }, 150);
        setTimeout(() => {
          window.scrollToBottom?.(true, false);
        }, 300);
      }
    } else if (!beforeId) {
      chatContainer.innerHTML = '';
      window.appendMessage?.('你好！我是熊猫，你的专属电脑控制智能体。我可以帮你执行命令行、管理文件或运行代码。今天需要我做什么？', 'system');
    }
  } catch (e) {
    console.error('Failed to load history:', e);
  }
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
    const res = await fetch(`/api/sessions/${sessionId}`, { method: 'DELETE' });
    if (!res.ok) {
      const err = await res.json();
      alert(err.detail || '删除失败');
      return;
    }
    // Clean cached DOM and events for the deleted session
    if (window._sessionChatCache) {
      delete window._sessionChatCache[sessionId];
      delete window._sessionChatCache['_evt_' + sessionId];
    }
    if (state.currentSessionId === sessionId) {
      const next = state.sessions.find(s => s.id !== sessionId);
      if (next) await switchSession(next.id);
    }
    await loadSessions();
  } catch (e) {
    console.error('Failed to delete session', e);
  }
}

export async function clearSession(sessionId) {
  try {
    const res = await fetch(`/api/sessions/${sessionId}/clear`, { method: 'POST' });
    const data = await res.json();
    if (data.ok) {
      // Clear chat DOM cache and reload
      if (window._sessionChatCache) {
        delete window._sessionChatCache[sessionId];
        delete window._sessionChatCache['_evt_' + sessionId];
      }
      if (state.currentSessionId === sessionId) {
        const chatContainer = document.getElementById('chat-container');
        if (chatContainer) chatContainer.innerHTML = '';
        window.appendMessage?.('*控制台 — 数据已清空*', 'system');
      }
      alert('会话数据已清空');
    }
  } catch (e) {
    console.error('Failed to clear session', e);
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
