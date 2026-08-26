<script setup>
// 设置 · 界面主题：当前主题摘要、导出/导入、恢复默认、主题市场。
// 契约：GET/POST /api/theme，GET /api/theme/market，GET /api/theme/export
// （见 api/routes/routes_settings.py；应用/导入/恢复后整页刷新，保证全部样式面生效）。
import { computed, onMounted, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { request } from '../../api/client';
import zh from '../../i18n/zh';

const t = zh.settings.models; // 复用 models 节下 theme 文案

const themeInfo = ref({});
const themeMarket = ref([]);
const themeMarketLoading = ref(false);
const themeImportVisible = ref(false);
const themeImportText = ref('');
const themeImporting = ref(false);

const themeSummary = computed(() => {
  const th = themeInfo.value || {};
  const parts = [];
  if (!th.primary_color && !th.sidebar_color && !th.logo_url && !th.chat_bg_url
      && !th.glass && !th.bordered && !th.animations && (th.decor || 'none') === 'none'
      && !th.custom_css) return t.theme.summaryDefault;
  if (th.primary_color) parts.push(`${t.theme.summaryColor} ${th.primary_color}`);
  if (th.sidebar_color) parts.push(`${t.theme.summarySidebar} ${th.sidebar_color}`);
  if (th.logo_url) parts.push(t.theme.summaryLogo);
  if (th.chat_bg_url) parts.push(t.theme.summaryBg);
  for (const [k, label] of [['glass', t.theme.fx.glass], ['bordered', t.theme.fx.bordered],
                            ['animations', t.theme.fx.animations]]) {
    if (th[k]) parts.push(label);
  }
  if (th.decor && th.decor !== 'none') parts.push(t.theme.fx[th.decor] || th.decor);
  if (th.custom_css) parts.push(t.theme.summaryCustomCss);
  return parts.join(' · ');
});

async function loadThemeInfo() {
  try { themeInfo.value = await request('/api/theme'); } catch { /* 忽略 */ }
}

async function loadThemeMarket() {
  themeMarketLoading.value = true;
  try {
    const data = await request('/api/theme/market');
    themeMarket.value = (data?.themes || []).map((x) => ({ ...x, applying: false }));
  } catch (err) {
    ElMessage.error(`${t.theme.marketFailed}: ${err.message}`);
  } finally {
    themeMarketLoading.value = false;
  }
}

async function exportTheme() {
  // 走后端导出：主题包内嵌 Logo/背景图（base64）与自定义 CSS，好友导入即完整还原
  try {
    const pkg = await request('/api/theme/export');
    const blob = new Blob([JSON.stringify(pkg, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `open-agc-theme-${Date.now()}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
    ElMessage.success(t.theme.exportSuccess);
  } catch (err) {
    ElMessage.error(`${t.theme.exportFailed}: ${err.message}`);
  }
}

async function importTheme() {
  if (!themeImportText.value.trim() || themeImporting.value) return;
  themeImporting.value = true;
  try {
    const parsed = JSON.parse(themeImportText.value);
    const theme = parsed.theme || parsed;
    const res = await request('/api/theme', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ theme }),
    });
    if (res && res.status === 'success') {
      ElMessage.success(t.theme.importSuccess);
      themeImportVisible.value = false;
      themeImportText.value = '';
      // 导入后整页刷新，保证全部样式面生效（用户反馈：局部热更新不完整）
      setTimeout(() => location.reload(), 400);
    } else {
      ElMessage.error(`${t.theme.importFailed}: ${(res && res.detail) || t.unknownError}`);
    }
  } catch (err) {
    ElMessage.error(`${t.theme.importFailed}: ${err.message}`);
  } finally {
    themeImporting.value = false;
  }
}

async function applyMarketTheme(th) {
  th.applying = true;
  try {
    const res = await request('/api/theme', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ theme: th.theme }),
    });
    if (res && res.status === 'success') {
      ElMessage.success(`${t.theme.applySuccess}: ${th.name}`);
      setTimeout(() => location.reload(), 400);
    } else {
      ElMessage.error(`${t.theme.applyFailed}: ${(res && res.detail) || t.unknownError}`);
    }
  } catch (err) {
    ElMessage.error(`${t.theme.applyFailed}: ${err.message}`);
  } finally {
    th.applying = false;
  }
}

async function resetTheme() {
  try {
    await ElMessageBox.confirm(t.theme.resetConfirmText, t.theme.resetConfirmTitle,
      { confirmButtonText: t.theme.reset, cancelButtonText: zh.goals.cancel, type: 'warning' });
  } catch { return; }
  try {
    await request('/api/theme', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: 'replace', theme: {} }),
    });
    ElMessage.success(t.theme.resetSuccess);
    setTimeout(() => location.reload(), 400);
  } catch (err) {
    ElMessage.error(`${t.theme.resetFailed}: ${err.message}`);
  }
}

onMounted(() => {
  loadThemeInfo();
  loadThemeMarket();
});
</script>

<template>
  <div class="theme-view">
    <header class="view-header">
      <h1>🎨 {{ zh.settings.nav.theme }}</h1>
    </header>

    <el-card class="settings-card" shadow="never">
      <template #header>
        <div class="card-header">
          <span class="card-title">{{ t.theme.title }}</span>
          <p class="card-desc">{{ t.theme.desc }}</p>
        </div>
      </template>
      <div class="theme-current">
        <span class="theme-label">{{ t.theme.current }}:</span>
        <span v-if="themeInfo.primary_color" class="color-chip" :style="{ background: themeInfo.primary_color }" :title="themeInfo.primary_color"></span>
        <span v-if="themeInfo.sidebar_color" class="color-chip" :style="{ background: themeInfo.sidebar_color }" :title="themeInfo.sidebar_color"></span>
        <span class="theme-summary">{{ themeSummary }}</span>
      </div>
      <div class="theme-actions">
        <el-button size="small" @click="exportTheme">{{ t.theme.export }}</el-button>
        <el-button size="small" @click="themeImportVisible = true">{{ t.theme.import }}</el-button>
        <el-button size="small" type="danger" plain @click="resetTheme">{{ t.theme.reset }}</el-button>
      </div>
      <el-divider content-position="left">{{ t.theme.marketTitle }}</el-divider>
      <div v-loading="themeMarketLoading" class="theme-market">
        <div v-for="th in themeMarket" :key="th.name + th.source" class="theme-row">
          <div class="theme-row-info">
            <strong>{{ th.name }}</strong>
            <el-tag size="small" :type="th.source === 'preset' ? 'info' : 'success'" disable-transitions>
              {{ th.source === 'preset' ? t.theme.sourcePreset : t.theme.sourceMarket }}
            </el-tag>
            <span v-if="th.author" class="theme-author">{{ th.author }}</span>
            <div class="theme-desc">{{ th.desc }}</div>
          </div>
          <el-button size="small" type="primary" plain :loading="th.applying" @click="applyMarketTheme(th)">
            {{ t.theme.apply }}
          </el-button>
        </div>
        <div v-if="!themeMarket.length && !themeMarketLoading" class="empty-state">
          <p>{{ t.theme.marketEmpty }}</p>
        </div>
      </div>

      <el-dialog :append-to-body="true" v-model="themeImportVisible" :title="t.theme.importTitle" width="480px">
        <el-input
          v-model="themeImportText"
          type="textarea"
          :rows="8"
          :placeholder="t.theme.importPlaceholder"
        />
        <div class="field-hint">{{ t.theme.importHint }}</div>
        <template #footer>
          <el-button @click="themeImportVisible = false">{{ zh.goals.cancel }}</el-button>
          <el-button type="primary" :loading="themeImporting" @click="importTheme">{{ t.theme.importDo }}</el-button>
        </template>
      </el-dialog>
    </el-card>
  </div>
</template>

<style scoped>
.theme-view {
  padding: 24px 28px 40px;
  max-width: 1080px;
  margin: 0 auto;
}

.view-header h1 {
  margin: 0 0 20px;
  font-size: 20px;
}

.settings-card {
  margin-bottom: 20px;
}

.card-header .card-title {
  font-size: 15px;
  font-weight: 600;
}

.card-desc {
  margin: 6px 0 0;
  font-size: 12px;
  font-weight: 400;
  color: var(--el-text-color-secondary);
}

.field-hint {
  width: 100%;
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}

.theme-current {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.theme-label {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.color-chip {
  display: inline-block;
  width: 18px;
  height: 18px;
  border-radius: 5px;
  border: 1px solid var(--el-border-color);
}

.theme-summary {
  font-size: 13px;
  color: var(--el-text-color-regular);
}

.theme-actions {
  display: flex;
  gap: 8px;
  margin-bottom: 4px;
}

.theme-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 4px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.theme-row:last-child {
  border-bottom: none;
}

.theme-row-info {
  flex: 1;
  min-width: 0;
}

.theme-author {
  margin-left: 6px;
  font-size: 12px;
  color: var(--el-text-color-placeholder);
}

.theme-desc {
  margin-top: 2px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

@media (max-width: 768px) {
  .theme-view {
    padding: 16px 16px 32px;
  }

  .theme-row {
    flex-direction: column;
    align-items: flex-start;
  }
}
</style>
