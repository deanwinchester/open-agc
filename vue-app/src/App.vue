<script setup>
import { onMounted, onUnmounted, ref, computed } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import zh from './i18n/zh';
import { pluginNav } from './plugins/registry';
import { request, setUnauthorizedHandler } from './api/client';
import { useWsStore } from './stores/ws';

// Logo/主题走共享 stores/theme.js（customize_theme 工具改动后经
// theme_updated 广播实时应用）；动态绑定避免 Vite 当作构建期资源解析。
import { themeState, loadTheme } from './stores/theme';
const logoUrl = computed(() => themeState.logoUrl);

// ── 访问控制：局域网未认证时的全屏密码遮罩 ──
// 本机访问中间件直接放行，/api/auth/check 返回 authenticated=true，遮罩不出现；
// 任何请求 401（Cookie 过期等）也会通过全局钩子重新拉起遮罩。
const authRequired = ref(false);
const authLocked = ref(false);        // 未配置密码/被 403 拒绝：只展示说明，不提供输入框
const authLockedMessage = ref('');
const authPassword = ref('');
const authError = ref('');
const authSubmitting = ref(false);

// 立即注册（而非 onMounted），保证最早一批请求的 401 也能被拦截
setUnauthorizedHandler(() => {
  authRequired.value = true;
});

onMounted(async () => {
  loadTheme();
  // customize_theme 工具改动后实时应用（广播 theme_updated）
  try {
    const ws = useWsStore();
    if (!ws.connected) ws.connect();
    ws.on('theme_updated', () => loadTheme());
  } catch { /* WS 未就绪则等下次刷新应用 */ }
  try {
    const res = await request('/api/auth/check');
    authRequired.value = !(res && res.authenticated);
  } catch (err) {
    if (err && err.status === 401) {
      authRequired.value = true;
    } else if (err && err.status === 403) {
      // 非本机且未配置密码（或公网）：锁定，无解，只展示后端说明
      authRequired.value = true;
      authLocked.value = true;
      authLockedMessage.value = err.message || '';
    }
    // 其他错误（网络失败等）不拦截，避免本机故障时锁死界面
  }
});

async function submitAuth() {
  if (!authPassword.value || authSubmitting.value) return;
  authSubmitting.value = true;
  authError.value = '';
  try {
    await request('/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ password: authPassword.value }),
    });
    // Cookie 已种下；整体刷新让所有缓存请求与 WebSocket 以新凭据重建
    window.location.reload();
  } catch (err) {
    authError.value = err.message || zh.auth.failed;
  } finally {
    authSubmitting.value = false;
  }
}

// 移动端抽屉：≤768px 侧栏默认隐藏，汉堡按钮滑出；遮罩/菜单项点击关闭。
// 视口变回桌面时强制收起，避免抽屉状态残留。
const sidebarOpen = ref(false);

// 插件子菜单收起状态（按插件名持久化到 localStorage，用户反馈：插件
// 菜单应当能收起子菜单）
const _COLLAPSE_KEY = 'pluginNavCollapsed';
const collapsedPlugins = ref(new Set(
  JSON.parse(localStorage.getItem(_COLLAPSE_KEY) || '[]')));

function togglePluginNav(name) {
  const s = new Set(collapsedPlugins.value);
  if (s.has(name)) s.delete(name);
  else s.add(name);
  collapsedPlugins.value = s;
  localStorage.setItem(_COLLAPSE_KEY, JSON.stringify([...s]));
}
const mobileMq = window.matchMedia('(max-width: 768px)');
function onViewportChange(e) {
  if (!e.matches) sidebarOpen.value = false;
}
onMounted(() => mobileMq.addEventListener('change', onViewportChange));
onUnmounted(() => mobileMq.removeEventListener('change', onViewportChange));
function closeSidebar() {
  sidebarOpen.value = false;
}

const menus = [
  { path: '/chat', label: zh.menu.chat },
  { path: '/tasks', label: zh.menu.tasks },
  { path: '/goals', label: zh.menu.goals },
  { path: '/downloads', label: zh.menu.downloads },
  { path: '/sandbox', label: zh.menu.sandbox },
  { path: '/settings', label: zh.menu.settings },
  { path: '/debug', label: zh.menu.debug },
];

// ── 版本号与升级徽标（旧版侧栏底部同款）──
const version = ref('');
const updateAvailable = ref(false);
const latestVersion = ref('');
const upgrading = ref(false);
// 部署通道（desktop/docker/source）与平台，决定升级弹窗文案
const channel = ref('source');
const platform = ref('');

onMounted(async () => {
  try {
    const data = await request('/api/version');
    version.value = 'v' + (data.current || '0.0.0');
    latestVersion.value = data.latest || '';
    updateAvailable.value = !!data.update_available;
    channel.value = data.channel || 'source';
    platform.value = data.platform || '';
  } catch { /* 版本检查失败静默，不影响主界面 */ }
});

function upgradeHint() {
  if (channel.value === 'desktop') {
    return platform.value === 'darwin' ? zh.upgrade.macHint : zh.upgrade.desktopHint;
  }
  return zh.upgrade.sourceHint;
}

async function doUpgrade() {
  try {
    await ElMessageBox.confirm(
      `${zh.upgrade.foundNew} v${latestVersion.value}，${upgradeHint()}${zh.upgrade.confirmSuffix}`,
      zh.upgrade.title,
      { confirmButtonText: zh.upgrade.confirmButton, cancelButtonText: zh.upgrade.cancelButton, type: 'warning' },
    );
  } catch { return; }
  upgrading.value = true;
  try {
    const res = await request('/api/upgrade', { method: 'POST' });
    // desktop Windows 成功后会自动退出重启；macOS 返回手动安装指引
    ElMessage.success((res && res.message) || zh.upgrade.success);
    if (!(res && res.restart)) updateAvailable.value = false;
  } catch (err) {
    ElMessage.error(zh.upgrade.failed + (err.message || ''));
  } finally {
    upgrading.value = false;
  }
}
</script>

<template>
  <div class="layout">
    <button
      class="hamburger"
      type="button"
      aria-label="菜单"
      @click="sidebarOpen = !sidebarOpen"
    >☰</button>
    <aside class="sidebar" :class="{ open: sidebarOpen }">
      <div class="logo">
        <img class="logo-img" :src="logoUrl" alt="Open-AGC" />
        <span class="logo-text">{{ themeState.appName || 'Open-AGC' }}</span>
      </div>
      <nav class="menu">
        <router-link
          v-for="item in menus"
          :key="item.path"
          :to="item.path"
          class="menu-item"
          active-class="active"
          @click="closeSidebar"
        >
          {{ item.label }}
        </router-link>
        <!-- 插件视图导航：由 src/plugins/registry.js 按 manifest vue_entry 动态注册 -->
        <div v-for="plugin in pluginNav" :key="plugin.name" class="plugin-section">
          <div
            class="plugin-label collapsible"
            role="button"
            :title="collapsedPlugins.has(plugin.name) ? '展开' : '收起'"
            @click="togglePluginNav(plugin.name)"
          >
            <span class="plugin-chevron">{{ collapsedPlugins.has(plugin.name) ? '▸' : '▾' }}</span>
            {{ plugin.icon }} {{ plugin.label }}
          </div>
          <template v-if="!collapsedPlugins.has(plugin.name)">
            <router-link
              v-for="view in plugin.views"
              :key="view.path"
              :to="view.path"
              class="menu-item"
              active-class="active"
              @click="closeSidebar"
            >
              {{ view.title }}
            </router-link>
          </template>
        </div>
      </nav>
      <!-- 版本号与升级入口（侧栏底部） -->
      <div class="sidebar-footer">
        <span class="version-text">{{ version || 'v…' }}</span>
        <span
          v-if="updateAvailable"
          class="upgrade-badge"
          :class="{ upgrading }"
          title="发现新版本，点击升级"
          @click="doUpgrade"
        >{{ upgrading ? '升级中…' : '⬆ 升级' }}</span>
      </div>
    </aside>
    <div v-if="sidebarOpen" class="sidebar-overlay" @click="closeSidebar"></div>
    <main class="content">
      <router-view v-slot="{ Component }">
        <Transition name="route-fade" mode="out-in">
          <component :is="Component" />
        </Transition>
      </router-view>
    </main>
    <!-- 全局访问密码遮罩：局域网未认证时拦截整个界面 -->
    <div v-if="authRequired" class="auth-overlay">
      <div class="auth-card">
        <img class="auth-logo" :src="logoUrl" alt="Open-AGC" />
        <h1 class="auth-title">{{ zh.auth.title }}</h1>
        <p class="auth-desc">{{ authLocked ? authLockedMessage : zh.auth.desc }}</p>
        <template v-if="!authLocked">
          <input
            v-model="authPassword"
            class="auth-input"
            type="password"
            :placeholder="zh.auth.passwordPlaceholder"
            autofocus
            @keyup.enter="submitAuth"
          />
          <p v-if="authError" class="auth-error">{{ authError }}</p>
          <button
            class="auth-submit"
            type="button"
            :disabled="authSubmitting || !authPassword"
            @click="submitAuth"
          >{{ authSubmitting ? zh.auth.loggingIn : zh.auth.submit }}</button>
        </template>
      </div>
    </div>
  </div>
</template>

<style scoped>
.layout {
  display: flex;
  height: 100vh;
  margin: 0;
  font-family: 'Segoe UI', 'PingFang SC', 'Microsoft YaHei', sans-serif;
}

.sidebar {
  width: 208px;
  flex-shrink: 0;
  background: linear-gradient(
    180deg,
    var(--panda-sidebar-bg-start) 0%,
    var(--panda-sidebar-bg-end) 100%
  );
  color: var(--panda-sidebar-text);
  display: flex;
  flex-direction: column;
}

.logo {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 18px 16px;
  border-bottom: 1px solid var(--panda-sidebar-divider);
}

.logo-img {
  width: 36px;
  height: 36px;
  border-radius: 10px;
  /* 品牌感：描边 + 竹绿微光晕 */
  box-shadow: 0 0 0 1px rgba(255, 255, 255, 0.14), var(--panda-shadow-glow);
}

.logo-text {
  font-size: 16px;
  font-weight: 700;
  letter-spacing: 0.02em;
  /* 跟随侧边栏文字色（主题派生），此前固定 --panda-on-accent 换主题不变色 */
  color: var(--panda-sidebar-text, #f9fafb);
}

.menu {
  display: flex;
  flex-direction: column;
  padding: 10px;
  /* 6 主菜单 + 插件区（open-agc-train 有 7 个视图）在矮窗口下会超出视高；
     让菜单区占满侧栏剩余空间并内部滚动，避免底部条目被裁掉 */
  flex: 1;
  min-height: 0;
  overflow-y: auto;
}

.sidebar-footer {
  display: flex;
  align-items: center;
  padding: 10px 16px;
  border-top: 1px solid rgba(255, 255, 255, 0.08);
}

.version-text {
  font-size: 11px;
  color: var(--panda-sidebar-text-dim, rgba(207, 216, 227, 0.55));
}

.upgrade-badge {
  margin-left: 8px;
  font-size: 11px;
  color: #fff;
  background: var(--el-color-primary);
  border-radius: 4px;
  padding: 2px 8px;
  cursor: pointer;
  user-select: none;
}

.upgrade-badge:hover { filter: brightness(1.1); }
.upgrade-badge.upgrading { opacity: 0.6; pointer-events: none; }

.menu-item {
  /* a 标签默认 inline：.menu 的 flex 列布局只约束直接子级，
     .plugin-section 内的链接会按文本流横排折行——显式 block 根治 */
  display: block;
  position: relative;
  padding: 10px 14px;
  margin-bottom: 2px;
  border-radius: 10px;
  color: var(--panda-sidebar-text);
  text-decoration: none;
  font-size: 14px;
  transition: background-color var(--panda-transition), color var(--panda-transition);
}

/* 深色底上的滚动条用反向半透明 */
.menu::-webkit-scrollbar-thumb {
  background: rgba(255, 255, 255, 0.16);
}

.menu::-webkit-scrollbar-thumb:hover {
  background: rgba(255, 255, 255, 0.28);
}

.menu-item:hover {
  background: var(--panda-sidebar-hover-bg);
  color: var(--panda-on-accent);
}

/* 激活态：竹绿左边条 + 浅绿底 */
.menu-item.active {
  background: var(--panda-sidebar-active-bg);
  color: var(--panda-on-accent);
  font-weight: 600;
}

.menu-item.active::before {
  content: '';
  position: absolute;
  left: 0;
  top: 22%;
  bottom: 22%;
  width: 3px;
  border-radius: 2px;
  background: var(--el-color-primary);
  box-shadow: 0 0 8px rgba(74, 222, 128, 0.6);
}

.plugin-section {
  margin-top: 12px;
  border-top: 1px solid var(--panda-sidebar-divider);
  padding-top: 10px;
}

/* 分组小标题：字距加大 */
.plugin-label {
  padding: 4px 14px 8px;
  font-size: 11px;  letter-spacing: 0.12em;
  color: var(--panda-sidebar-text-dim);
}

.plugin-label.collapsible {
  cursor: pointer;
  user-select: none;
  display: flex;
  align-items: center;
}

.plugin-label.collapsible:hover {
  color: var(--panda-sidebar-text, var(--el-text-color-primary));
}

.plugin-chevron {
  display: inline-block;
  width: 14px;
  margin-left: -4px;
  font-size: 10px;
}

.content {
  flex: 1;
  overflow: auto;
  background: var(--el-bg-color-page);
}

/* 访问密码遮罩：全屏拦截层，压过侧栏抽屉（z-index 1002）与汉堡按钮 */
.auth-overlay {
  position: fixed;
  inset: 0;
  z-index: 3000;
  display: flex;
  align-items: center;
  justify-content: center;
  background: var(--el-bg-color-page);
}

.auth-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  width: min(360px, 88vw);
  padding: 36px 32px;
  border: 1px solid var(--el-border-color-light);
  border-radius: 14px;
  background: var(--el-bg-color);
  box-shadow: var(--panda-shadow-card);
}

.auth-logo {
  width: 56px;
  height: 56px;
  border-radius: 14px;
}

.auth-title {
  margin: 14px 0 6px;
  font-size: 17px;
}

.auth-desc {
  margin: 0 0 18px;
  font-size: 13px;
  line-height: 1.6;
  text-align: center;
  color: var(--el-text-color-secondary);
}

.auth-input {
  width: 100%;
  padding: 9px 12px;
  border: 1px solid var(--el-border-color);
  border-radius: 8px;
  font-size: 14px;
  outline: none;
  box-sizing: border-box;
}

.auth-input:focus {
  border-color: var(--el-color-primary);
}

.auth-error {
  margin: 8px 0 0;
  font-size: 12px;
  color: var(--el-color-danger);
}

.auth-submit {
  width: 100%;
  margin-top: 14px;
  padding: 9px 0;
  border: none;
  border-radius: 8px;
  background: var(--el-color-primary);
  color: #fff;
  font-size: 14px;
  cursor: pointer;
}

.auth-submit:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

/* 汉堡按钮 / 遮罩：仅窄屏出现（默认隐藏，避免桌面端残留状态影响） */
.hamburger,
.sidebar-overlay {
  display: none;
}

@media (max-width: 768px) {
  /* 抽屉式侧栏：滑出 + 遮罩（参考旧版 static/js/navigation.js 模式） */
  .sidebar {
    position: fixed;
    left: 0;
    top: 0;
    bottom: 0;
    z-index: 1002;
    width: 236px;
    transform: translateX(-105%);
    transition: transform 0.22s ease;
  }

  .sidebar.open {
    transform: translateX(0);
    box-shadow: var(--panda-shadow-float);
  }

  .sidebar-overlay {
    display: block;
    position: fixed;
    inset: 0;
    z-index: 1001;
    background: rgba(15, 23, 20, 0.45);
  }

  .hamburger {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    position: fixed;
    top: 10px;
    left: 10px;
    z-index: 1000;
    width: 40px;
    height: 40px;
    border: 1px solid var(--el-border-color-light);
    border-radius: 10px;
    background: var(--el-bg-color);
    color: var(--el-text-color-primary);
    box-shadow: var(--panda-shadow-card);
    font-size: 17px;
    cursor: pointer;
    transition: box-shadow var(--panda-transition);
  }

  .hamburger:active {
    box-shadow: var(--panda-shadow-card-hover);
  }

  /* 顶部留出汉堡按钮带，内容不与其重叠 */
  .content {
    padding-top: 52px;
  }
}
</style>

<!-- 主题扩展效果（customize_theme 开放给 agent 的装饰能力，全局类由
     stores/theme.js applyTheme 切换）：毛玻璃/描边/动画/装饰图案 -->
<style>
/* ── 毛玻璃：不覆盖配色，只加模糊与轻透（颜色由变量/半透明派生） ── */
.theme-glass .sidebar {
  backdrop-filter: blur(18px) saturate(1.5);
  -webkit-backdrop-filter: blur(18px) saturate(1.5);
}

.theme-glass .el-card,
.theme-glass .msg-bubble.agent,
.theme-glass .msg-system-inner,
.theme-glass .pc-card {
  /* color-mix 跟随当前模式的 overlay 色：浅色模式为白、暗色模式为深灰 */
  background-color: color-mix(in srgb, var(--el-bg-color-overlay) 68%, transparent);
  backdrop-filter: blur(12px) saturate(1.3);
  -webkit-backdrop-filter: blur(12px) saturate(1.3);
}

/* ── 描边：气泡与卡片显性边框 ── */
.theme-bordered .msg-bubble,
.theme-bordered .el-card,
.theme-bordered .msg-system-inner {
  border: 1.5px solid var(--el-color-primary-light-5) !important;
}

.theme-bordered .msg-bubble.user {
  border-color: var(--el-color-primary-light-3) !important;
}

/* ── 动画：消息入场与过渡 ── */
.theme-anim .msg-row {
  animation: theme-msg-in 0.28s ease-out;
}

.theme-anim .menu-item,
.theme-anim .el-button {
  transition: all 0.2s ease;
}

.theme-anim .menu-item:hover {
  transform: translateX(3px);
}

@keyframes theme-msg-in {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}

/* ── 装饰图案：纯 CSS 生成，无外部资源 ── */
body::after {
  content: none;
}

.decor-petals body,
.decor-stars body,
.decor-geometric body {
  position: relative;
}

.decor-petals body::after,
.decor-stars body::after,
.decor-geometric body::after {
  content: "";
  position: fixed;
  inset: 0;
  pointer-events: none;
  z-index: 0;
}

.decor-petals body::after {
  background-image: var(--decor-image);
  background-size: var(--decor-size);
  background-position: var(--decor-pos);
  background-repeat: no-repeat;
  animation: decor-petal-fall 14s linear infinite;
}

@keyframes decor-petal-fall {
  0% { background-position: var(--decor-pos); }
  100% { background-position: 12% 115vh, 55% 108vh, 78% 112vh, 30% 118vh, 90% 106vh; }
}

.decor-stars body::after {
  background-image: var(--decor-image);
  background-size: var(--decor-size);
  background-position: var(--decor-pos);
  background-repeat: no-repeat;
  animation: decor-star-twinkle 3.5s ease-in-out infinite alternate;
}

@keyframes decor-star-twinkle {
  from { opacity: 0.45; }
  to { opacity: 1; }
}

.decor-geometric body::after {
  background-image:
    linear-gradient(45deg, rgba(64, 158, 255, 0.05) 25%, transparent 25%),
    linear-gradient(-45deg, rgba(64, 158, 255, 0.05) 25%, transparent 25%);
  background-size: 40px 40px;
}
</style>
