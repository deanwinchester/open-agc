// 界面主题共享状态：ui_theme（主题色/侧边栏背景色/Logo/会话背景图）。
// App.vue 启动加载 + theme_updated 广播触发重载；MessageItem 头像跟随 Logo。
import { reactive } from 'vue';
import { request } from '../api/client';

export const DEFAULT_LOGO = '/static/icon_rounded.png';

export const themeState = reactive({
  primaryColor: '',
  sidebarColor: '',
  pageColor: '',
  logoUrl: DEFAULT_LOGO,
  chatBgUrl: '',
  appName: '',
  dark: false,
  glass: false,
  bordered: false,
  animations: false,
  decor: 'none',
});

function _shade(hex, ratio) {
  // ratio>0 向白混合，<0 向黑混合（Element Plus 档位色生成）
  const n = parseInt(hex.slice(1), 16);
  const r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
  const t = ratio > 0 ? 255 : 0;
  const p = Math.abs(ratio);
  const mix = (c) => Math.round(c + (t - c) * p);
  return `#${((1 << 24) | (mix(r) << 16) | (mix(g) << 8) | mix(b)).toString(16).slice(1)}`;
}

function _luminance(hex) {
  // 感知亮度 0~255（ITU-R BT.601），用于背景色 → 文字色自动对比
  const n = parseInt(hex.slice(1), 16);
  return 0.299 * ((n >> 16) & 255) + 0.587 * ((n >> 8) & 255) + 0.114 * (n & 255);
}

function _rgba(hex, alpha) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${alpha})`;
}

// 樱花花瓣 SVG（不对称心形花瓣，缺口朝上），跟随主题色填充
function _petalSvg(hex) {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">`
    + `<path fill="${hex}" fill-opacity="0.55" d="M12 1 C8 4, 4 8, 4 13 C4 18, 8 22, 12 22 `
    + `C16 22, 20 18, 20 13 C20 8, 16 4, 12 1 Z M12 1 C11 4, 11 6, 12 8"/>`
    + `<path fill="${hex}" fill-opacity="0.35" d="M12 8 C10 10, 9 13, 9 16 C9 18, 10 20, 12 22 C14 20, 15 18, 15 16 C15 13, 14 10, 12 8 Z"/>`
    + `</svg>`;
  return `url("data:image/svg+xml,${encodeURIComponent(svg)}")`;
}

function _starSvg() {
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">`
    + `<path fill="white" fill-opacity="0.85" d="M12 2 L13.5 10.5 L22 12 L13.5 13.5 L12 22 L10.5 13.5 L2 12 L10.5 10.5 Z"/>`
    + `</svg>`;
  return `url("data:image/svg+xml,${encodeURIComponent(svg)}")`;
}

function _setVar(root, name, value) {
  if (value) root.style.setProperty(name, value);
  else root.style.removeProperty(name);
}

export function applyTheme(theme) {
  const t = theme || {};
  const root = document.documentElement;
  themeState.primaryColor = t.primary_color || '';
  themeState.sidebarColor = t.sidebar_color || '';
  themeState.logoUrl = t.logo_url || DEFAULT_LOGO;
  themeState.chatBgUrl = t.chat_bg_url || '';
  themeState.appName = t.app_name || '';
  themeState.pageColor = t.page_color || '';
  // 暗色判定：dark 开关 或 深色页面底色（亮度派生，告别二元）
  const pcLum = /^#[0-9a-fA-F]{6}$/.test(themeState.pageColor)
    ? _luminance(themeState.pageColor) : null;
  themeState.dark = !!t.dark || (pcLum !== null && pcLum < 140);
  themeState.glass = !!t.glass;
  themeState.bordered = !!t.bordered;
  themeState.animations = !!t.animations;
  themeState.decor = t.decor || 'none';
  document.title = themeState.appName || 'Open-AGC';

  // 主题色（Element Plus 档位色）
  const color = themeState.primaryColor;
  if (/^#[0-9a-fA-F]{6}$/.test(color)) {
    root.style.setProperty('--el-color-primary', color);
    root.style.setProperty('--el-color-primary-dark-2', _shade(color, -0.2));
    const lights = { 3: 0.3, 5: 0.5, 7: 0.7, 8: 0.8, 9: 0.9 };
    for (const [k, ratio] of Object.entries(lights)) {
      root.style.setProperty(`--el-color-primary-light-${k}`, _shade(color, ratio));
    }
  } else {
    for (const v of ['--el-color-primary', '--el-color-primary-dark-2',
      '--el-color-primary-light-3', '--el-color-primary-light-5',
      '--el-color-primary-light-7', '--el-color-primary-light-8',
      '--el-color-primary-light-9']) {
      root.style.removeProperty(v);
    }
  }

  // 侧边栏背景（主应用变量为渐变两端，端色从主色推导深色档）；
  // 文字/悬停/激活色按背景亮度自动派生对比色（用户反馈：换色后字色怪异）。
  // 毛玻璃开启时：背景色变半透明（靠 backdrop-filter 透出页面底），
  // 不再被 CSS 写死覆盖（生产实证：写死白色渐变把配色全洗掉）。
  const sb = themeState.sidebarColor;
  if (/^#[0-9a-fA-F]{6}$/.test(sb)) {
    const glass = !!t.glass;
    const a1 = glass ? 0.72 : 1, a2 = glass ? 0.66 : 1;
    root.style.setProperty('--panda-sidebar-bg-start', _rgba(_shade(sb, 0.06), a1));
    root.style.setProperty('--panda-sidebar-bg-end', _rgba(_shade(sb, -0.12), a2));
    const dark = _luminance(sb) < 140;
    root.style.setProperty('--panda-sidebar-text', dark ? '#f9fafb' : '#111827');
    root.style.setProperty('--panda-sidebar-text-dim', dark ? 'rgba(249,250,251,.68)' : 'rgba(17,24,39,.62)');
    root.style.setProperty('--panda-sidebar-hover-bg', dark ? 'rgba(255,255,255,.12)' : 'rgba(0,0,0,.07)');
    root.style.setProperty('--panda-sidebar-active-bg', dark ? 'rgba(255,255,255,.20)' : 'rgba(0,0,0,.12)');
    root.style.setProperty('--panda-sidebar-divider', dark ? 'rgba(255,255,255,.14)' : 'rgba(0,0,0,.10)');
  } else {
    for (const v of ['--panda-sidebar-bg-start', '--panda-sidebar-bg-end',
      '--panda-sidebar-text', '--panda-sidebar-text-dim',
      '--panda-sidebar-hover-bg', '--panda-sidebar-active-bg',
      '--panda-sidebar-divider']) {
      root.style.removeProperty(v);
    }
  }

  // 装饰图案：花瓣/星星用 SVG 形状（跟随主题色），几何用 CSS 渐变
  const decorBase = /^#[0-9a-fA-F]{6}$/.test(themeState.primaryColor)
    ? themeState.primaryColor : '#e88fb0';
  const decor = t.decor || 'none';
  if (decor === 'petals') {
    root.style.setProperty('--decor-image', [
      _petalSvg(decorBase), _petalSvg(_shade(decorBase, 0.15)),
      _petalSvg(decorBase), _petalSvg(_shade(decorBase, -0.1)),
      _petalSvg(decorBase),
    ].join(', '));
    root.style.setProperty('--decor-size', '34px 40px, 22px 26px, 42px 50px, 26px 30px, 18px 22px');
    root.style.setProperty('--decor-pos', '12% -60px, 55% -120px, 78% -80px, 30% -160px, 90% -40px');
  } else if (decor === 'stars') {
    root.style.setProperty('--decor-image', [
      _starSvg(), _starSvg(), _starSvg(), _starSvg(), _starSvg(),
    ].join(', '));
    root.style.setProperty('--decor-size', '10px 10px, 6px 6px, 8px 8px, 5px 5px, 7px 7px');
    root.style.setProperty('--decor-pos', '15% 20%, 65% 12%, 85% 55%, 35% 72%, 50% 42%');
  }
  root.style.setProperty('--decor-color-a', _rgba(decorBase, 0.32));
  root.style.setProperty('--decor-color-b', _rgba(decorBase, 0.22));
  root.style.setProperty('--decor-color-c', _rgba(decorBase, 0.16));

  // 暗色模式：Element Plus dark css-vars（html.dark）整体接管背景/文字/边框；
  // 我们内联设置的主题色/侧边栏变量优先级更高，不受影响
  root.classList.toggle('dark', themeState.dark);

  // 页面底色（任意色，亮度派生全套前景色——不再是 dark/非 dark 二元）
  const pcVars = ['--el-bg-color-page', '--el-bg-color', '--el-bg-color-overlay',
    '--el-text-color-primary', '--el-text-color-regular', '--el-text-color-secondary',
    '--el-border-color-light', '--el-border-color-lighter', '--el-fill-color-light'];
  if (pcLum !== null) {
    const pc = themeState.pageColor;
    const isDark = pcLum < 140;
    const derived = isDark ? {
      '--el-bg-color-page': pc,
      '--el-bg-color': _shade(pc, 0.05),
      '--el-bg-color-overlay': _shade(pc, 0.10),
      '--el-text-color-primary': '#f3f4f6',
      '--el-text-color-regular': '#d1d5db',
      '--el-text-color-secondary': '#9ca3af',
      '--el-border-color-light': 'rgba(255,255,255,.14)',
      '--el-border-color-lighter': 'rgba(255,255,255,.08)',
      '--el-fill-color-light': _shade(pc, 0.12),
    } : {
      '--el-bg-color-page': pc,
      '--el-bg-color': '#ffffff',
      '--el-bg-color-overlay': '#ffffff',
      '--el-text-color-primary': '#1f2937',
      '--el-text-color-regular': '#4b5563',
      '--el-text-color-secondary': '#6b7280',
      '--el-border-color-light': 'rgba(0,0,0,.12)',
      '--el-border-color-lighter': 'rgba(0,0,0,.08)',
      '--el-fill-color-light': _shade(pc, -0.05),
    };
    for (const [k, v] of Object.entries(derived)) root.style.setProperty(k, v);
  } else {
    for (const v of pcVars) root.style.removeProperty(v);
  }

  // 自定义 CSS（agent 自由发挥，theme_tool 已做安全消毒）
  let customEl = document.getElementById('custom-theme-css');
  if (t.custom_css) {
    if (!customEl) {
      customEl = document.createElement('style');
      customEl.id = 'custom-theme-css';
      document.head.appendChild(customEl);
    }
    if (customEl.textContent !== t.custom_css) customEl.textContent = t.custom_css;
  } else if (customEl) {
    customEl.remove();
  }

  // 扩展风格（开放给 agent 的装饰能力）：毛玻璃/边框/动画/装饰图案
  root.classList.toggle('theme-glass', !!t.glass);
  root.classList.toggle('theme-bordered', !!t.bordered);
  root.classList.toggle('theme-anim', !!t.animations);
  for (const cls of [...root.classList].filter((c) => c.startsWith('decor-'))) {
    root.classList.remove(cls);
  }
  if (t.decor && t.decor !== 'none') root.classList.add(`decor-${t.decor}`);
}

export async function loadTheme() {
  try {
    applyTheme(await request('/api/theme'));
  } catch { /* 拉取失败用默认，不阻断 */ }
}
