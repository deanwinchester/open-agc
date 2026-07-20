// WebSocket 连接管理（Pinia store）。
//
// 通道：/ws?session_id=<id>（见 dev-docs/API契约.md 第 3 节）。
// 重连策略沿用旧 static/app.js：指数退避 1s 起步、每次 ×2、30s 封顶；
// 网络恢复（navigator.onLine）时重置退避并立即重连；主动关闭（切会话/登出）不重连。
//
// 事件分发：服务端消息按 `type` 分发给 on(type, fn) 订阅者。
// 已知服务端 → 客户端事件 type（API契约.md §3.2，此处仅做 pub/sub，不在本层处理）：
//   status / progress / message / error / history_steps / task_backgrounded /
//   system_message / llamacpp_download / download_success / download_failed /
//   benchmark_progress / benchmark_complete / training_install_progress /
//   training_progress / training_complete / training_error / training_step_paused / eval_progress

import { defineStore } from 'pinia';

// 退避间隔（毫秒）：第 attempt 次重连前的等待，1s * 2^attempt，上限 30s。纯函数，可测。
export function reconnectDelay(attempt) {
  return Math.min(1000 * Math.pow(2, attempt), 30000);
}

// 纯 pub/sub 注册表：按事件 type 订阅/退订/分发。与浏览器无关，可测。
export function createDispatcher() {
  const subscribers = new Map(); // type -> Set<fn>

  function on(type, fn) {
    if (!subscribers.has(type)) subscribers.set(type, new Set());
    subscribers.get(type).add(fn);
    return () => off(type, fn); // 便于组件卸载时退订
  }

  function off(type, fn) {
    const set = subscribers.get(type);
    if (!set) return;
    set.delete(fn);
    if (set.size === 0) subscribers.delete(type);
  }

  function dispatch(event) {
    const type = event && event.type;
    if (!type) return;
    const set = subscribers.get(type);
    if (!set) return;
    for (const fn of [...set]) {
      try {
        fn(event);
      } catch (err) {
        console.error(`[ws] subscriber for "${type}" threw:`, err);
      }
    }
  }

  return { on, off, dispatch };
}

export const useWsStore = defineStore('ws', {
  state: () => ({
    connected: false,      // 当前是否已连接
    sessionId: 1,          // 当前会话（/ws?session_id=）
    reconnectAttempt: 0,   // 已连续重连次数（决定退避间隔）
  }),

  actions: {
    // 惰性初始化非响应式实例字段（不放进 state，避免被 reactive 包裹）。
    _ensureInternals() {
      if (!this._dispatcher) {
        this._dispatcher = createDispatcher();
        this._ws = null;
        this._reconnectTimer = null;
        this._intentionalClose = false;
        // 网络恢复：重置退避并立即重连（对齐旧 app.js 的 online 处理）。
        window.addEventListener('online', () => {
          if (!this.connected && !this._intentionalClose) {
            clearTimeout(this._reconnectTimer);
            this.reconnectAttempt = 0;
            this.connect();
          }
        });
      }
    },

    connect(sessionId = this.sessionId) {
      this._ensureInternals();
      if (
        this._ws &&
        (this._ws.readyState === WebSocket.OPEN || this._ws.readyState === WebSocket.CONNECTING)
      ) {
        return; // 已有打开/进行中的连接
      }
      this.sessionId = sessionId;
      const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const ws = new WebSocket(`${protocol}//${window.location.host}/ws?session_id=${sessionId}`);
      this._ws = ws;

      ws.onopen = () => {
        if (this._ws !== ws) return; // 已被更新的连接替换（如快速切会话）
        this.connected = true;
        this.reconnectAttempt = 0;
      };

      ws.onmessage = (evt) => {
        let data;
        try {
          data = JSON.parse(evt.data);
        } catch {
          return; // 非 JSON 帧直接忽略
        }
        this._dispatcher.dispatch(data);
      };

      ws.onclose = () => {
        if (this._intentionalClose) {
          this._intentionalClose = false;
          return; // 主动关闭（切换会话等）不重连
        }
        if (this._ws !== ws) return; // 旧连接的迟到 onclose，不影响新连接
        this.connected = false;
        this._ws = null;
        const delay = reconnectDelay(this.reconnectAttempt);
        this.reconnectAttempt += 1;
        clearTimeout(this._reconnectTimer);
        this._reconnectTimer = setTimeout(() => this.connect(), delay);
      };

      ws.onerror = (err) => {
        console.error('[ws] error:', err);
      };
    },

    // 主动断开（不重连）。
    disconnect() {
      this._ensureInternals();
      clearTimeout(this._reconnectTimer);
      if (this._ws) {
        this._intentionalClose = true;
        this._ws.close();
        this._ws = null;
      }
      this.connected = false;
    },

    // 切换会话：旧 app.js 的做法是主动关闭后以新 session_id 重连。
    switchSession(sessionId) {
      this.disconnect();
      this.reconnectAttempt = 0;
      this.connect(sessionId);
    },

    // 发送消息（对象自动 JSON 序列化）；未连接时返回 false，由调用方处理。
    send(payload) {
      if (this._ws && this._ws.readyState === WebSocket.OPEN) {
        this._ws.send(JSON.stringify(payload));
        return true;
      }
      return false;
    },

    // 订阅某类服务端事件，返回退订函数。
    on(type, fn) {
      this._ensureInternals();
      return this._dispatcher.on(type, fn);
    },

    off(type, fn) {
      this._ensureInternals();
      this._dispatcher.off(type, fn);
    },
  },
});
