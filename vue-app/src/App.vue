<script setup>
import { onMounted, onUnmounted, ref } from 'vue';
import zh from './i18n/zh';
import { pluginNav } from './plugins/registry';

// Logo is served by the backend's existing /static mount (static/icon_rounded.png);
// bound dynamically so Vite doesn't try to resolve it as a build-time asset.
const logoUrl = '/static/icon_rounded.png';

// 移动端抽屉：≤768px 侧栏默认隐藏，汉堡按钮滑出；遮罩/菜单项点击关闭。
// 视口变回桌面时强制收起，避免抽屉状态残留。
const sidebarOpen = ref(false);
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
  { path: '/settings', label: zh.menu.settings },
  { path: '/debug', label: zh.menu.debug },
];
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
        <span class="logo-text">Open-AGC</span>
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
          <div class="plugin-label">{{ plugin.icon }} {{ plugin.label }}</div>
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
        </div>
      </nav>
    </aside>
    <div v-if="sidebarOpen" class="sidebar-overlay" @click="closeSidebar"></div>
    <main class="content">
      <router-view v-slot="{ Component }">
        <Transition name="route-fade" mode="out-in">
          <component :is="Component" />
        </Transition>
      </router-view>
    </main>
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
  color: var(--panda-on-accent);
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
  font-size: 11px;
  letter-spacing: 0.12em;
  color: var(--panda-sidebar-text-dim);
}

.content {
  flex: 1;
  overflow: auto;
  background: var(--el-bg-color-page);
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
