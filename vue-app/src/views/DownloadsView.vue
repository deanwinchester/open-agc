<script setup>
// 下载记录视图（批次 2）：迁移旧 view-downloads（static/js/llama.js loadDownloadHistory）。
// 数据契约（api/routes/routes_settings.py，见 dev-docs/API契约.md §1.11）：
// - GET /api/downloads?status= → {downloads: [记录]}，记录字段：
//   id/type/label/repo_id/filename/source/url/target_path/partial_path/
//   total_size/downloaded_bytes/status/downloading|paused|completed|failed)/progress/error_message/created_at
// - POST /api/downloads/{id}/resume（仅 paused/failed）
// - DELETE /api/downloads/{id}
// - GET /api/downloads/{id}/events → {download_id, events[]}
// 旧视图的模型搜索/安装（/api/llamacpp/*）属于设置·模型页范畴，不在本视图。
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { Refresh, Delete } from '@element-plus/icons-vue';
import { request } from '../api/client';
import zh from '../i18n/zh';

const t = zh.downloads;
const POLL_MS = 3000;

const loading = ref(true);
const downloads = ref([]);

const hasDownloading = computed(() => downloads.value.some((d) => d.status === 'downloading'));

// 状态 → 公共 status-pill 变体（下载蓝/暂停橙/完成绿/失败红）
function statusPill(status) {
  const map = { downloading: 'info', paused: 'warning', completed: 'success', failed: 'danger' };
  return { label: t.status[status] || status, cls: map[status] || 'default' };
}

function typeLabel(dl) {
  if (dl.type === 'dataset' || (dl.label || '').startsWith('数据集:')) return t.type.dataset;
  return t.type[dl.type] || t.type.model;
}

function typeIcon(dl) {
  const label = typeLabel(dl);
  if (label === t.type.dataset) return '📊';
  if (dl.type === 'binary') return '⚙️';
  return '📥';
}

// 进度百分比（0-100）；total_size 未知时退化为已下载 MB 文本
function progressPct(dl) {
  return Math.round((dl.progress || 0) * 100);
}

function progressText(dl) {
  if (dl.total_size > 0) return `${progressPct(dl)}%`;
  if (dl.downloaded_bytes > 0) return `${(dl.downloaded_bytes / 1024 / 1024).toFixed(1)} MB`;
  return '';
}

function progressStatus(status) {
  if (status === 'failed') return 'exception';
  if (status === 'completed') return 'success';
  return '';
}

function canResume(dl) {
  return dl.status === 'paused' || dl.status === 'failed';
}

function showProgress(dl) {
  return dl.status === 'downloading' || dl.status === 'paused';
}

async function loadDownloads({ silent = false } = {}) {
  if (!silent) loading.value = true;
  try {
    const data = await request('/api/downloads');
    downloads.value = Array.isArray(data?.downloads) ? data.downloads : [];
  } catch (err) {
    if (!silent) ElMessage.error(`${t.loadFailed}: ${err.message}`);
  } finally {
    if (!silent) loading.value = false;
  }
}

// ── 自动刷新：存在下载中记录时轮询，卸载时清理 ──

let pollTimer = null;

onMounted(() => {
  loadDownloads();
  pollTimer = setInterval(() => {
    if (hasDownloading.value) loadDownloads({ silent: true });
  }, POLL_MS);
});

onUnmounted(() => {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
});

// ── 续传 / 删除 ──

const resumingId = ref(null);

async function resumeDownload(dl) {
  resumingId.value = dl.id;
  try {
    await request(`/api/downloads/${dl.id}/resume`, { method: 'POST' });
    ElMessage.success(t.actions.resumeSuccess);
    loadDownloads({ silent: true });
  } catch (err) {
    ElMessage.error(`${t.actions.resumeFailed}: ${err.message}`);
  } finally {
    resumingId.value = null;
  }
}

async function deleteDownload(dl) {
  try {
    await ElMessageBox.confirm(
      `${dl.label || dl.filename || `#${dl.id}`} — ${t.actions.deleteConfirmText}`,
      t.actions.deleteConfirmTitle,
      { confirmButtonText: t.actions.delete, cancelButtonText: zh.goals.cancel, type: 'warning' }
    );
  } catch {
    return; // 用户取消
  }
  try {
    await request(`/api/downloads/${dl.id}`, { method: 'DELETE' });
    ElMessage.success(t.actions.deleteSuccess);
    loadDownloads({ silent: true });
  } catch (err) {
    ElMessage.error(`${t.actions.deleteFailed}: ${err.message}`);
  }
}

// ── 事件弹窗 ──

const eventsDialog = ref(false);
const eventsLoading = ref(false);
const eventsList = ref([]);
const eventsTarget = ref(null);

async function openEvents(dl) {
  eventsTarget.value = dl;
  eventsList.value = [];
  eventsDialog.value = true;
  eventsLoading.value = true;
  try {
    const data = await request(`/api/downloads/${dl.id}/events`);
    eventsList.value = Array.isArray(data?.events) ? data.events : [];
  } catch (err) {
    ElMessage.error(`${t.eventsLoadFailed}: ${err.message}`);
  } finally {
    eventsLoading.value = false;
  }
}

function eventText(ev) {
  if (typeof ev === 'string') return ev;
  // 事件行结构（download_events 表）：{id, download_id, event_type, message, details, created_at}
  const parts = [ev.created_at, ev.event_type, ev.message, ev.details].filter(Boolean);
  return parts.join(' | ') || JSON.stringify(ev);
}
</script>

<template>
  <div class="downloads-view">
    <header class="view-header">
      <h1>{{ t.title }}</h1>
      <p class="view-desc">{{ t.desc }}</p>
    </header>

    <el-card class="list-card" shadow="never">
      <div class="toolbar">
        <span class="count-info">{{ downloads.length }}{{ t.countSuffix }}</span>
        <el-button size="small" :icon="Refresh" :title="t.refresh" @click="loadDownloads()" />
      </div>

      <div v-if="!downloads.length && !loading" class="empty-state">
        <div class="empty-icon">📥</div>
        <p>{{ t.empty }}</p>
        <small>{{ t.emptyHint }}</small>
      </div>

      <div v-loading="loading">
        <div v-for="dl in downloads" :key="dl.id" class="row-card dl-card">
          <div class="row-card-head">
            <span class="download-icon">{{ typeIcon(dl) }}</span>
            <span class="row-card-title" :title="dl.label || dl.filename || ''">
              {{ dl.label || dl.filename || `#${dl.id}` }}
            </span>
            <el-tag size="small" type="info" disable-transitions>{{ typeLabel(dl) }}</el-tag>
            <div class="row-card-right">
              <span class="status-pill" :class="`status-pill--${statusPill(dl.status).cls}`">
                <span class="pill-dot"></span>{{ statusPill(dl.status).label }}
              </span>
              <el-button
                v-if="canResume(dl)"
                size="small"
                type="primary"
                plain
                :loading="resumingId === dl.id"
                @click="resumeDownload(dl)"
              >
                ▶ {{ t.actions.resume }}
              </el-button>
              <el-button size="small" text @click="openEvents(dl)">{{ t.actions.events }}</el-button>
              <el-button
                text
                type="danger"
                class="delete-btn"
                :title="t.actions.delete"
                @click="deleteDownload(dl)"
              >
                <el-icon><Delete /></el-icon>
              </el-button>
            </div>
          </div>
          <div class="row-card-meta">
            <span v-if="progressText(dl)">{{ progressText(dl) }}</span>
            <span v-if="dl.source">{{ t.source }}: {{ dl.source }}</span>
            <span>{{ dl.created_at }}</span>
          </div>
          <el-progress
            v-if="showProgress(dl)"
            :percentage="progressPct(dl)"
            :status="progressStatus(dl.status)"
            class="download-progress"
          />
          <div v-if="dl.status === 'failed' && dl.error_message" class="error-line">
            {{ t.errorPrefix }}{{ dl.error_message }}
          </div>
          <div v-if="dl.status === 'completed' && dl.target_path" class="path-line" :title="dl.target_path">
            📁 {{ dl.target_path }}
          </div>
        </div>
      </div>
    </el-card>

    <!-- 事件弹窗 -->
    <el-dialog :append-to-body="true"
      v-model="eventsDialog"
      :title="`${t.eventsTitle} — ${eventsTarget?.label || eventsTarget?.filename || ''}`"
      width="640px"
    >
      <div v-loading="eventsLoading">
        <div v-if="!eventsList.length && !eventsLoading" class="empty-state">
          <div class="empty-icon">🗒️</div>
          <p>{{ t.eventsEmpty }}</p>
        </div>
        <div v-for="(ev, i) in eventsList" :key="i" class="event-line">{{ eventText(ev) }}</div>
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.downloads-view {
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

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
}

.count-info {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.empty-state {
  padding: 32px 0;
  text-align: center;
  color: var(--el-text-color-secondary);
}

.empty-state p {
  margin: 0 0 4px;
}

/* 行卡片结构由全局 .row-card 提供，这里只补下载特有零件 */
.download-icon {
  font-size: 18px;
  flex-shrink: 0;
}

.delete-btn {
  padding: 4px;
  height: auto;
}

.download-progress {
  margin-top: 8px;
  max-width: 480px;
}

.error-line {
  margin-top: 6px;
  font-size: 12px;
  color: var(--el-color-danger);
  word-break: break-all;
}

.path-line {
  margin-top: 6px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.event-line {
  font-size: 12px;
  font-family: 'Cascadia Code', Consolas, monospace;
  padding: 4px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
  word-break: break-all;
}

@media (max-width: 768px) {
  .downloads-view {
    padding: 16px 16px 32px;
  }

  /* 行首操作区窄屏允许换行，pill 与删除保持成组 */
  .row-card-right {
    flex-wrap: wrap;
    justify-content: flex-end;
    row-gap: 4px;
  }

  .download-progress {
    max-width: 100%;
  }
}
</style>
