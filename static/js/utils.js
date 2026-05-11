import { state } from './state.js';

export function escapeHtml(text) {
  const div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

export function showStatus(message, type) {
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

export function formatTimeAgo(isoStr) {
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

export function formatTime(isoStr) {
  if (!isoStr) return '';
  if (!isoStr.includes('T')) isoStr = isoStr.replace(' ', 'T');
  if (!isoStr.endsWith('Z') && !isoStr.includes('+')) isoStr += 'Z';
  return new Date(isoStr).toLocaleString('zh-CN');
}

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

export function t(key) {
  return (translations[state.currentLang] || translations['en'])[key] || key;
}

export function initI18n() {
  const userLang = navigator.language || navigator.userLanguage;
  state.currentLang = userLang.startsWith('zh') ? 'zh-CN' : 'en';
}
