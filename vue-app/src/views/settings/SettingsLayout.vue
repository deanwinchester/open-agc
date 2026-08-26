<script setup>
// 设置区布局：左侧子导航 + 右侧内容。
import { useRoute } from 'vue-router';
import zh from '../../i18n/zh';

const route = useRoute();

const navItems = [
  { path: '/settings/models', label: zh.settings.nav.models },
  { path: '/settings/system', label: zh.settings.nav.system },
  { path: '/settings/theme', label: zh.settings.nav.theme },
  { path: '/settings/skills', label: zh.settings.nav.skills },
  { path: '/settings/mcp', label: zh.settings.nav.mcpAgents },
  { path: '/settings/plugins', label: zh.settings.nav.plugins },
  { path: '/settings/secrets', label: zh.settings.nav.secrets },
];
</script>

<template>
  <div class="settings-layout">
    <aside class="settings-nav">
      <el-menu :default-active="route.path" router class="nav-menu">
        <el-menu-item
          v-for="item in navItems"
          :key="item.path"
          :index="item.path"
        >
          <span>{{ item.label }}</span>
        </el-menu-item>
      </el-menu>
    </aside>
    <section class="settings-content">
      <router-view />
    </section>
  </div>
</template>

<style scoped>
.settings-layout {
  display: flex;
  min-height: 100%;
  align-items: stretch;
}

.settings-nav {
  width: 180px;
  flex-shrink: 0;
  border-right: 1px solid var(--el-border-color-light);
  background: var(--el-bg-color, #fff);
}

.nav-menu {
  border-right: none;
}

.settings-content {
  flex: 1;
  min-width: 0;
}

@media (max-width: 768px) {
  /* 窄屏：子导航变为顶部横向滚动条 */
  .settings-layout {
    flex-direction: column;
  }

  .settings-nav {
    width: 100%;
    border-right: none;
    border-bottom: 1px solid var(--el-border-color-light);
  }

  .nav-menu {
    display: flex;
    overflow-x: auto;
  }

  .nav-menu :deep(.el-menu-item) {
    flex-shrink: 0;
    white-space: nowrap;
  }
}
</style>
