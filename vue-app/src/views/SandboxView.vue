<script setup>
// 沙箱治理视图（一期）：顶层条目浏览 + 手动归类 + 手动删除 + 一键清 tmp。
// 数据契约（api/routes/routes_sandbox.py）：
// - GET /api/sandbox/entries → {sandbox, total_size, entries[]}
//   条目字段：name/path(相对)/is_dir/type(project|deliverable|temp|installer|dir|file)/
//   mtime/size/file_count/partial/task_id?(deliverable)；size 为 null 表示后台统计中
// - POST /api/sandbox/delete {path}；POST /api/sandbox/move {path, dest: projects|tmp}
// - POST /api/sandbox/clean_tmp → {removed}
// 安全口径（服务端强制）：仅允许顶层条目；realpath 必须在沙箱根内；
// .checkpoints 禁止删除/移动——前端对非顶层行（交付物）与 .checkpoints 隐藏操作按钮。
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { ElMessage, ElMessageBox } from 'element-plus';
import { Refresh, Delete } from '@element-plus/icons-vue';
import { request } from '../api/client';
import zh from '../i18n/zh';

const t = zh.sandbox;
const router = useRouter();
const POLL_MS = 3000;

const loading = ref(true);
const entries = ref([]);
const totalSize = ref(null);

// 存在 size===null 的目录条目（后台统计中）时保持轮询
const hasPending = computed(() => entries.value.some((e) => e.is_dir && e.size === null));

function fmtSize(bytes) {
  if (bytes === null || bytes === undefined) return '';
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
}

function sizeText(e) {
  if (e.size === null || e.size === undefined) return t.computing;
  return fmtSize(e.size) + (e.partial ? t.partialHint : '');
}

function countText(e) {
  if (e.file_count === null || e.file_count === undefined) return t.computing;
  return String(e.file_count);
}

// 类型 → el-tag 变体（项目绿/交付物蓝/临时橙/安装包紫红/其余灰）
function typeTagType(type) {
  const map = {
    project: 'success',
    deliverable: 'primary',
    temp: 'warning',
    installer: 'danger',
    dir: 'info',
    file: 'info',
  };
  return map[type] || 'info';
}

// 操作按钮仅对顶层条目开放（deliverable 行 path 为 outputs/task_<id>，非顶层）
function isTopLevel(e) {
  return !String(e.path).includes('/');
}

// 保留目录（.checkpoints 与四个分区目录）后端拒绝删除/移动，前端同步隐藏操作
const PROTECTED_NAMES = new Set(['.checkpoints', 'projects', 'outputs', 'tmp', 'downloads']);

function canOperate(e) {
  return isTopLevel(e) && !PROTECTED_NAMES.has(e.name);
}

// ── 统计轮询：连续失败熔断，避免后端宕机时无限打必败请求 ──

let pollFailures = 0;
const MAX_POLL_FAILURES = 5;
let pollTimer = null;

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function loadEntries({ silent = false } = {}) {
  if (!silent) loading.value = true;
  try {
    const data = await request('/api/sandbox/entries');
    entries.value = Array.isArray(data?.entries) ? data.entries : [];
    totalSize.value = data?.total_size ?? null;
    pollFailures = 0;
  } catch (err) {
    if (silent) {
      pollFailures += 1;
      if (pollFailures >= MAX_POLL_FAILURES) stopPolling();
    } else {
      ElMessage.error(`${t.loadFailed}: ${err.message}`);
    }
  } finally {
    if (!silent) loading.value = false;
  }
}

onMounted(() => {
  loadEntries();
  pollTimer = setInterval(() => {
    if (hasPending.value) loadEntries({ silent: true });
  }, POLL_MS);
});

onUnmounted(() => {
  stopPolling();
});

// ── 归类 / 删除 / 清 tmp ──

const movingPath = ref('');

async function moveEntry(e, dest) {
  if (!dest) return;
  try {
    await ElMessageBox.confirm(
      `${e.name} → ${dest}/ — ${t.actions.classifyConfirmText}`,
      t.actions.classifyConfirmTitle,
      { confirmButtonText: t.actions.classify, cancelButtonText: zh.goals.cancel, type: 'warning' }
    );
  } catch {
    return; // 用户取消
  }
  movingPath.value = e.path;
  try {
    await request('/api/sandbox/move', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: e.path, dest }),
    });
    ElMessage.success(`${t.actions.moveSuccess} → ${dest}/`);
    loadEntries({ silent: true });
  } catch (err) {
    ElMessage.error(`${t.actions.moveFailed}: ${err.message}`);
  } finally {
    movingPath.value = '';
  }
}

async function deleteEntry(e) {
  try {
    await ElMessageBox.confirm(
      `${e.name} — ${t.actions.deleteConfirmText}`,
      t.actions.deleteConfirmTitle,
      { confirmButtonText: t.actions.delete, cancelButtonText: zh.goals.cancel, type: 'warning' }
    );
  } catch {
    return; // 用户取消
  }
  try {
    await request('/api/sandbox/delete', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: e.path }),
    });
    ElMessage.success(t.actions.deleteSuccess);
    loadEntries({ silent: true });
  } catch (err) {
    ElMessage.error(`${t.actions.deleteFailed}: ${err.message}`);
  }
}

const cleaning = ref(false);

async function cleanTmp() {
  try {
    await ElMessageBox.confirm(
      t.cleanTmpConfirmText,
      t.cleanTmpConfirmTitle,
      { confirmButtonText: t.cleanTmp, cancelButtonText: zh.goals.cancel, type: 'warning' }
    );
  } catch {
    return;
  }
  cleaning.value = true;
  try {
    const data = await request('/api/sandbox/clean_tmp', { method: 'POST' });
    ElMessage.success(`${t.cleanTmpSuccess}${data?.removed ?? 0}${t.cleanTmpSuccessSuffix}`);
    loadEntries({ silent: true });
  } catch (err) {
    ElMessage.error(`${t.cleanTmpFailed}: ${err.message}`);
  } finally {
    cleaning.value = false;
  }
}

function goTask(e) {
  if (e.task_id) router.push(`/tasks/${e.task_id}`);
}
</script>

<template>
  <div class="sandbox-view">
    <header class="view-header">
      <h1>{{ t.title }}</h1>
      <p class="view-desc">{{ t.desc }}</p>
    </header>

    <el-card class="list-card" shadow="never">
      <div class="toolbar">
        <span class="count-info">
          {{ entries.length }}{{ t.countSuffix }}
          <template v-if="totalSize !== null">
            · {{ t.totalSize }} {{ fmtSize(totalSize) }}
          </template>
        </span>
        <div class="toolbar-right">
          <el-button
            size="small"
            type="warning"
            plain
            :loading="cleaning"
            @click="cleanTmp"
          >
            🧹 {{ t.cleanTmp }}
          </el-button>
          <el-button size="small" :icon="Refresh" :title="t.refresh" @click="loadEntries()" />
        </div>
      </div>

      <div v-if="!entries.length && !loading" class="empty-state">
        <div class="empty-icon">🗂️</div>
        <p>{{ t.empty }}</p>
        <small>{{ t.emptyHint }}</small>
      </div>

      <el-table v-else :data="entries" v-loading="loading" size="small">
        <el-table-column prop="name" :label="t.columns.name" min-width="200" show-overflow-tooltip />
        <el-table-column :label="t.columns.type" width="90">
          <template #default="{ row }">
            <el-tag size="small" :type="typeTagType(row.type)" disable-transitions>
              {{ t.type[row.type] || row.type }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column :label="t.columns.size" width="130">
          <template #default="{ row }">
            <span :class="{ 'stat-pending': row.size === null }">{{ sizeText(row) }}</span>
          </template>
        </el-table-column>
        <el-table-column :label="t.columns.fileCount" width="90" align="right">
          <template #default="{ row }">
            <span :class="{ 'stat-pending': row.file_count === null }">{{ countText(row) }}</span>
          </template>
        </el-table-column>
        <el-table-column prop="mtime" :label="t.columns.mtime" width="150" />
        <el-table-column :label="t.columns.task" width="100">
          <template #default="{ row }">
            <el-button
              v-if="row.task_id"
              size="small"
              text
              type="primary"
              @click="goTask(row)"
            >
              #{{ row.task_id }}
            </el-button>
            <span v-else>—</span>
          </template>
        </el-table-column>
        <el-table-column :label="t.columns.actions" width="200">
          <template #default="{ row }">
            <template v-if="canOperate(row)">
              <el-select
                size="small"
                class="classify-select"
                :model-value="''"
                :placeholder="t.actions.classify"
                :disabled="movingPath === row.path"
                @change="(dest) => moveEntry(row, dest)"
              >
                <el-option :label="t.dest.projects" value="projects" />
                <el-option :label="t.dest.tmp" value="tmp" />
              </el-select>
              <el-button
                text
                type="danger"
                class="delete-btn"
                :title="t.actions.delete"
                @click="deleteEntry(row)"
              >
                <el-icon><Delete /></el-icon>
              </el-button>
            </template>
            <span v-else>—</span>
          </template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>

<style scoped>
.sandbox-view {
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

.toolbar-right {
  display: flex;
  align-items: center;
  gap: 8px;
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

.stat-pending {
  color: var(--el-text-color-placeholder);
}

.classify-select {
  width: 150px;
}

.delete-btn {
  padding: 4px;
  height: auto;
}

@media (max-width: 768px) {
  .sandbox-view {
    padding: 16px 16px 32px;
  }

  .toolbar {
    flex-wrap: wrap;
  }
}
</style>
