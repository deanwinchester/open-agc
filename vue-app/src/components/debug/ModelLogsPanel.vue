<script setup>
// 模型调用日志面板（验收修复 B）：迁移旧 view-debug 的 model-logs 子页签。
// 数据契约（api/routes/routes_searxng.py）：
// - GET /api/model-logs/status → {enabled}
// - POST /api/model-logs/toggle {enabled} → {enabled}
// - POST /api/model-logs/clear → {status}
// - GET /api/model-logs/filters → {providers[], models[]}
// - GET /api/model-logs?page=&page_size=&provider=&model= → {logs[], total, page, page_size}（分页 clamp 1-200）
// - GET /api/model-logs/{id} → 完整行 + request_data/response_data（文件路径已解析为内容）；404
import { onMounted, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { Refresh } from '@element-plus/icons-vue';
import { request } from '../../api/client';
import zh from '../../i18n/zh';

const t = zh.debug.modelLogs;
const JSON_HEADERS = { 'Content-Type': 'application/json' };
const PAGE_SIZE = 50;

const enabled = ref(false);
const logs = ref([]);
const total = ref(0);
const page = ref(1);
const loading = ref(false);
const providers = ref([]);
const models = ref([]);
const filterProvider = ref('');
const filterModel = ref('');

const detailDialog = ref(false);
const detail = ref(null);
const detailLoading = ref(false);

// 时间戳为 UTC（DB CURRENT_TIMESTAMP），无时区标记时按 UTC 解析再转本地显示
function fmtTs(ts) {
  if (!ts) return '-';
  const d = new Date(String(ts).includes('Z') || String(ts).includes('+') ? ts : `${ts}Z`);
  if (Number.isNaN(d.getTime())) return ts;
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function fmtLatency(ms) {
  if (!ms) return '-';
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}

function fmtCost(c) {
  const n = Number(c);
  return n > 0 ? n.toFixed(4) : '-';
}

function prettyJson(text) {
  if (!text) return '';
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

async function loadStatus() {
  try {
    const data = await request('/api/model-logs/status');
    enabled.value = !!data?.enabled;
  } catch {
    // 开关状态非关键，失败保持默认
  }
}

async function onToggleChange(val) {
  try {
    const data = await request('/api/model-logs/toggle', {
      method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ enabled: val }),
    });
    enabled.value = !!data?.enabled;
  } catch (err) {
    enabled.value = !val;
    ElMessage.error(`${t.toggleFailed}: ${err.message}`);
  }
}

async function loadFilters() {
  try {
    const data = await request('/api/model-logs/filters');
    providers.value = Array.isArray(data?.providers) ? data.providers : [];
    models.value = Array.isArray(data?.models) ? data.models : [];
  } catch {
    // 筛选项非关键
  }
}

async function loadLogs() {
  loading.value = true;
  try {
    const params = new URLSearchParams({ page: String(page.value), page_size: String(PAGE_SIZE) });
    if (filterProvider.value) params.set('provider', filterProvider.value);
    if (filterModel.value) params.set('model', filterModel.value);
    const data = await request(`/api/model-logs?${params.toString()}`);
    logs.value = Array.isArray(data?.logs) ? data.logs : [];
    total.value = data?.total ?? 0;
  } catch (err) {
    ElMessage.error(`${t.loadFailed}: ${err.message}`);
  } finally {
    loading.value = false;
  }
}

function onFilterChange() {
  page.value = 1;
  loadLogs();
}

function onPageChange(p) {
  page.value = p;
  loadLogs();
}

async function clearLogs() {
  try {
    await ElMessageBox.confirm(t.clearConfirm, t.clear, { type: 'warning' });
  } catch {
    return;
  }
  try {
    await request('/api/model-logs/clear', { method: 'POST' });
    ElMessage.success(t.clearSuccess);
    page.value = 1;
    loadLogs();
    loadFilters();
  } catch (err) {
    ElMessage.error(`${t.clearFailed}: ${err.message}`);
  }
}

async function openDetail(row) {
  detailDialog.value = true;
  detail.value = null;
  detailLoading.value = true;
  try {
    detail.value = await request(`/api/model-logs/${row.id}`);
  } catch (err) {
    ElMessage.error(`${t.loadFailed}: ${err.message}`);
    detailDialog.value = false;
  } finally {
    detailLoading.value = false;
  }
}

onMounted(() => {
  loadStatus();
  loadFilters();
  loadLogs();
});
</script>

<template>
  <div class="model-logs-panel">
    <div class="toolbar">
      <div class="toolbar-left">
        <el-switch
          :model-value="enabled"
          :active-text="t.enableLabel"
          @change="onToggleChange"
        />
        <el-select
          v-model="filterProvider"
          size="small"
          clearable
          class="filter-select"
          :placeholder="t.providerAll"
          @change="onFilterChange"
        >
          <el-option v-for="p in providers" :key="p" :label="p" :value="p" />
        </el-select>
        <el-select
          v-model="filterModel"
          size="small"
          clearable
          filterable
          class="filter-select"
          :placeholder="t.modelAll"
          @change="onFilterChange"
        >
          <el-option v-for="m in models" :key="m" :label="m" :value="m" />
        </el-select>
      </div>
      <div class="toolbar-right">
        <el-button size="small" type="danger" plain @click="clearLogs">{{ t.clear }}</el-button>
        <el-button size="small" :icon="Refresh" :loading="loading" @click="loadLogs">
          {{ zh.debug.refresh }}
        </el-button>
      </div>
    </div>

    <el-table :data="logs" v-loading="loading" size="small" stripe class="logs-table">
      <el-table-column :label="t.colTime" width="160">
        <template #default="{ row }">{{ fmtTs(row.timestamp) }}</template>
      </el-table-column>
      <el-table-column prop="provider" :label="t.colProvider" width="110" show-overflow-tooltip />
      <el-table-column prop="model" :label="t.colModel" min-width="170" show-overflow-tooltip />
      <el-table-column :label="t.colTokens" width="140" align="right">
        <template #default="{ row }">
          {{ row.prompt_tokens ?? 0 }}/{{ row.completion_tokens ?? 0 }}/{{ row.total_tokens ?? 0 }}
        </template>
      </el-table-column>
      <el-table-column :label="t.colCost" width="90" align="right">
        <template #default="{ row }">{{ fmtCost(row.cost_estimate) }}</template>
      </el-table-column>
      <el-table-column :label="t.colLatency" width="90" align="right">
        <template #default="{ row }">{{ fmtLatency(row.latency_ms) }}</template>
      </el-table-column>
      <el-table-column :label="t.colCache" width="110" align="center">
        <template #default="{ row }">
          <span v-if="row.cache_hit === 'hit'" class="cache-hit">
            ✅ {{ (row.cached_tokens || 0).toLocaleString() }}
          </span>
          <span v-else-if="row.cache_hit === 'miss'" class="cache-miss">❌ {{ t.cacheMiss }}</span>
          <span v-else>-</span>
        </template>
      </el-table-column>
      <el-table-column :label="t.colActions" width="80" align="center">
        <template #default="{ row }">
          <el-button size="small" text type="primary" @click="openDetail(row)">{{ t.detail }}</el-button>
        </template>
      </el-table-column>
      <template #empty>{{ t.empty }}</template>
    </el-table>

    <div v-if="total > PAGE_SIZE" class="pagination-bar">
      <el-pagination
        background
        layout="total, prev, pager, next"
        :total="total"
        :page-size="PAGE_SIZE"
        :current-page="page"
        @current-change="onPageChange"
      />
    </div>

    <el-dialog v-model="detailDialog" :title="t.detailTitle" width="720px">
      <div v-loading="detailLoading">
        <template v-if="detail">
          <div class="detail-grid">
            <span><strong>{{ t.colTime }}:</strong> {{ fmtTs(detail.timestamp) }}</span>
            <span><strong>{{ t.colProvider }}:</strong> {{ detail.provider || '-' }}</span>
            <span><strong>{{ t.colModel }}:</strong> {{ detail.model || '-' }}</span>
            <span><strong>{{ t.colLatency }}:</strong> {{ fmtLatency(detail.latency_ms) }}</span>
            <span>
              <strong>Tokens:</strong>
              {{ detail.prompt_tokens ?? 0 }}/{{ detail.completion_tokens ?? 0 }}/{{ detail.total_tokens ?? 0 }}
              ({{ zh.taskDetail.tokens.cached }} {{ (detail.cached_tokens || 0).toLocaleString() }})
            </span>
            <span><strong>{{ t.colCost }}:</strong> {{ fmtCost(detail.cost_estimate) }}</span>
          </div>
          <div class="section-title">{{ t.requestLabel }}</div>
          <pre class="detail-pre">{{ detail.request_data ? prettyJson(detail.request_data) : t.noData }}</pre>
          <div class="section-title">{{ t.responseLabel }}</div>
          <pre class="detail-pre tall">{{ detail.response_data ? prettyJson(detail.response_data) : t.noData }}</pre>
        </template>
      </div>
    </el-dialog>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.toolbar-left,
.toolbar-right {
  display: flex;
  align-items: center;
  gap: 10px;
}

.filter-select {
  width: 170px;
}

.logs-table {
  width: 100%;
}

.cache-hit {
  color: var(--el-color-success);
  font-weight: 600;
}

.cache-miss {
  color: var(--el-text-color-secondary);
}

.pagination-bar {
  display: flex;
  justify-content: center;
  margin-top: 12px;
}

.detail-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 20px;
  font-size: 13px;
  margin-bottom: 12px;
}

.section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-secondary);
  margin: 10px 0 6px;
}

.detail-pre {
  margin: 0;
  padding: 10px;
  background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 220px;
  overflow-y: auto;
}

.detail-pre.tall {
  max-height: 320px;
}
</style>
