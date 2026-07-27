<script setup>
// 设置 · 插件管理：本地管理（列表/启停/扫描/git 安装/卸载）+ 插件市场
// （/api/marketplace 远程索引，搜索/安装，Vue 迁移期曾跳过，本文件已补齐）。
// 数据契约（api/routes/routes_plugins.py，见 dev-docs/API契约.md）：
// - GET /api/plugins → {plugins: [{name, version, description, enabled, loaded, author, homepage}], plugins_dir}
// - POST /api/plugins/scan → {status, count, plugins}
// - POST /api/plugins/{name}/toggle → {status, enabled}
// - POST /api/plugins/install {name, url}（注意请求字段是 url，不是 repo_url）
// - DELETE /api/plugins/{name}
// - GET /api/marketplace → {marketplace: {plugins: [{name, version, description, author, rating, installs, verified, source.repo}]}}
import { computed, onMounted, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { Refresh, Search } from '@element-plus/icons-vue';
import { cachedFetch, invalidateCache, request } from '../../api/client';
import { refreshPluginViews } from '../../plugins/registry';
import zh from '../../i18n/zh';

const t = zh.settings.plugins;
const JSON_HEADERS = { 'Content-Type': 'application/json' };
const NAME_RE = /^[A-Za-z0-9_-]+$/;

const loading = ref(true);
const plugins = ref([]);
const pluginsDir = ref('');

async function loadPlugins() {
  loading.value = true;
  try {
    const data = await cachedFetch('/api/plugins');
    plugins.value = Array.isArray(data?.plugins) ? data.plugins : [];
    pluginsDir.value = data?.plugins_dir || '';
  } catch (err) {
    ElMessage.error(`${t.loadFailed}: ${err.message}`);
  } finally {
    loading.value = false;
  }
}

function refresh() {
  invalidateCache('/api/plugins');
  loadPlugins();
}

// ── 启停 ──

async function togglePlugin(p) {
  p.toggling = true;
  try {
    const res = await request(`/api/plugins/${encodeURIComponent(p.name)}/toggle`, { method: 'POST' });
    p.enabled = !!res?.enabled;
    invalidateCache('/api/plugins');
    ElMessage.success(p.enabled ? t.statusEnabled : t.statusDisabled);
  } catch (err) {
    ElMessage.error(`${t.toggleFailed}: ${err.message}`);
  } finally {
    p.toggling = false;
  }
}

// ── 卸载 ──

async function removePlugin(p) {
  try {
    await ElMessageBox.confirm(`${p.name} — ${t.deleteConfirmText}`, t.deleteConfirmTitle, {
      confirmButtonText: t.uninstall,
      cancelButtonText: t.cancel,
      type: 'warning',
    });
  } catch {
    return; // 用户取消
  }
  try {
    await request(`/api/plugins/${encodeURIComponent(p.name)}`, { method: 'DELETE' });
    ElMessage.success(t.deleteSuccess);
    refresh();
  } catch (err) {
    ElMessage.error(`${t.deleteFailed}: ${err.message}`);
  }
}

// ── 扫描 ──

const scanning = ref(false);

async function scan() {
  scanning.value = true;
  try {
    const res = await request('/api/plugins/scan', { method: 'POST' });
    // 后端已热重载插件代码；同步刷新前端插件视图（破缓存重载 vue-entry.js、
    // 移除旧路由/导航后重新注册），无需刷新页面
    await refreshPluginViews();
    ElMessage.success(`${t.scanDonePrefix}${res?.count ?? 0}${t.scanDoneSuffix}`);
    refresh();
  } catch (err) {
    ElMessage.error(`${t.scanFailed}: ${err.message}`);
  } finally {
    scanning.value = false;
  }
}

// ── 从 Git 安装 ──

const installUrl = ref('');
const installName = ref('');
const installing = ref(false);

function deriveName(url) {
  const tail = url.split('/').pop() || '';
  return tail.replace(/\.git$/i, '');
}

async function install() {
  const url = installUrl.value.trim();
  if (!url) {
    ElMessage.error(t.urlRequired);
    return;
  }
  const name = installName.value.trim() || deriveName(url);
  if (!NAME_RE.test(name)) {
    ElMessage.error(t.nameInvalid);
    return;
  }
  installing.value = true;
  try {
    await request('/api/plugins/install', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ name, url }),
    });
    ElMessage.success(t.installHint);
    installUrl.value = '';
    installName.value = '';
    refresh();
  } catch (err) {
    ElMessage.error(err.message);
  } finally {
    installing.value = false;
  }
}

function statusOf(p) {
  if (!p.loaded) return { label: t.statusNotLoaded, type: 'warning' };
  return p.enabled
    ? { label: t.statusEnabled, type: 'success' }
    : { label: t.statusDisabled, type: 'info' };
}

onMounted(() => {
  loadPlugins();
  loadMarketplace();
});

// ── 插件市场（/api/marketplace 远程索引） ──
const mpLoading = ref(false);
const mpPlugins = ref([]);
const mpSearch = ref('');

async function loadMarketplace() {
  mpLoading.value = true;
  try {
    const data = await request('/api/marketplace');
    mpPlugins.value = data?.marketplace?.plugins || [];
  } catch (err) {
    ElMessage.error(`${t.mpLoadFailed}: ${err.message}`);
  } finally {
    mpLoading.value = false;
  }
}

const mpFiltered = computed(() => {
  const q = mpSearch.value.trim().toLowerCase();
  if (!q) return mpPlugins.value;
  return mpPlugins.value.filter((p) =>
    ((p.name || '') + (p.description || '') + (p.tags || []).join(' ')).toLowerCase().includes(q));
});

async function installFromMarket(p) {
  const repo = p.source?.repo || '';
  if (!repo) { ElMessage.warning(t.mpNoSource); return; }
  p.installing = true;
  try {
    const res = await request('/api/plugins/install', {
      method: 'POST', headers: JSON_HEADERS,
      body: JSON.stringify({ name: p.name, url: `https://github.com/${repo}.git` }),
    });
    if (res?.status === 'ok' || res?.status === 'success') {
      ElMessage.success(t.mpInstallOk);
      refresh();
    } else {
      ElMessage.error(res?.detail || t.installFailed);
    }
  } catch (err) {
    ElMessage.error(`${t.installFailed}: ${err.message}`);
  } finally {
    p.installing = false;
  }
}
</script>

<template>
  <div class="plugins-view" v-loading="loading">
    <header class="view-header">
      <h1>{{ t.title }}</h1>
      <p class="view-desc">{{ t.desc }}</p>
    </header>

    <!-- 已安装插件 -->
    <el-card class="settings-card" shadow="never">
      <div class="toolbar">
        <span class="dir-path">{{ t.dirLabel }}: {{ pluginsDir || '--' }}</span>
        <div class="toolbar-actions">
          <el-button :icon="Refresh" :title="t.refresh" @click="refresh" />
          <el-button type="primary" :icon="Search" :loading="scanning" @click="scan">
            {{ scanning ? t.scanning : t.scan }}
          </el-button>
        </div>
      </div>

      <div v-if="!plugins.length && !loading" class="empty-state">
        <div class="empty-icon">🧩</div>
        <p>{{ t.empty }}</p>
        <small>{{ t.emptyHint }}</small>
      </div>

      <div v-for="p in plugins" :key="p.name" class="plugin-row">
        <div class="plugin-info">
          <div class="plugin-title">
            <strong>📦 {{ p.name }}</strong>
            <span class="plugin-version">v{{ p.version }}</span>
            <el-tag size="small" :type="statusOf(p).type" disable-transitions>{{ statusOf(p).label }}</el-tag>
          </div>
          <div class="plugin-meta">{{ p.description }}<template v-if="p.author"> · {{ p.author }}</template></div>
        </div>
        <div class="plugin-actions">
          <el-button
            v-if="p.loaded"
            size="small"
            :loading="p.toggling"
            @click="togglePlugin(p)"
          >
            {{ p.enabled ? t.disable : t.enable }}
          </el-button>
          <el-button size="small" type="danger" plain @click="removePlugin(p)">{{ t.uninstall }}</el-button>
        </div>
      </div>
    </el-card>

    <!-- 从 Git 安装 -->
    <el-card class="settings-card" shadow="never">
      <template #header>
        <div class="card-header">
          <span class="card-title">{{ t.installTitle }}</span>
        </div>
      </template>
      <el-form label-position="top" @submit.prevent>
        <el-form-item :label="t.gitUrlLabel">
          <el-input v-model="installUrl" :placeholder="t.gitUrlPlaceholder" @keyup.enter="install" />
        </el-form-item>
        <el-form-item :label="t.nameLabel">
          <el-input v-model="installName" :placeholder="t.namePlaceholder" @keyup.enter="install" />
        </el-form-item>
        <el-button type="primary" :loading="installing" @click="install">
          {{ installing ? t.installing : t.install }}
        </el-button>
        <div class="field-hint">{{ t.installHint }}</div>
      </el-form>
    </el-card>

    <!-- 插件市场 -->
    <el-card class="settings-card" shadow="never">
      <template #header>
        <div class="card-header">
          <span class="card-title">{{ t.mpTitle }}</span>
          <el-input v-model="mpSearch" :placeholder="t.mpSearchPlaceholder" clearable class="mp-search" />
        </div>
      </template>
      <div v-loading="mpLoading">
        <div v-if="!mpFiltered.length && !mpLoading" class="empty-state">
          <div class="empty-icon">🛒</div>
          <p>{{ t.mpEmpty }}</p>
        </div>
        <div v-for="p in mpFiltered" :key="p.name" class="plugin-row">
          <div class="plugin-info">
            <div class="plugin-title">
              <strong>📦 {{ p.name }}</strong>
              <span class="plugin-version">v{{ p.version }}</span>
              <el-tag v-if="p.verified" size="small" type="success" disable-transitions>✓ {{ t.mpVerified }}</el-tag>
            </div>
            <div class="plugin-meta">{{ p.description }}</div>
            <div class="plugin-meta">{{ p.author || '--' }} · ⭐ {{ p.rating || '--' }} · 📥 {{ p.installs || 0 }}</div>
          </div>
          <div class="plugin-actions">
            <el-button size="small" type="primary" :loading="p.installing" @click="installFromMarket(p)">
              {{ p.installing ? t.installing : t.install }}
            </el-button>
          </div>
        </div>
      </div>
    </el-card>
  </div>
</template>

<style scoped>
.plugins-view {
  padding: 24px 28px 40px;
  max-width: 1080px;
  margin: 0 auto;
}

.view-header h1 {
  margin: 0 0 6px;
  font-size: 20px;
}

.view-desc {
  margin: 0 0 20px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.settings-card {
  margin-bottom: 20px;
}

.mp-search {
  width: 220px;
  margin-left: auto;
}

.card-header .card-title {
  font-size: 15px;
  font-weight: 600;
}

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}

.dir-path {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  word-break: break-all;
}

.toolbar-actions {
  display: flex;
  gap: 8px;
}

.empty-state {
  padding: 32px 0;
  text-align: center;
  color: var(--el-text-color-secondary);
}

.empty-state p {
  margin: 0 0 4px;
}

.plugin-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 8px;
  border-top: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  transition: background-color var(--panda-transition);
}

.plugin-row:hover {
  background: var(--el-color-primary-light-9);
}

.plugin-info {
  flex: 1;
  min-width: 0;
}

.plugin-title {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.plugin-version {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.plugin-meta {
  margin-top: 2px;
  font-size: 13px;
  color: var(--el-text-color-regular);
}

.plugin-actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}

.field-hint {
  margin-top: 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

@media (max-width: 768px) {
  .plugins-view {
    padding: 16px 16px 32px;
  }

  .plugin-row {
    flex-wrap: wrap;
  }

  .plugin-actions {
    margin-left: auto;
    flex-wrap: wrap;
  }
}
</style>
