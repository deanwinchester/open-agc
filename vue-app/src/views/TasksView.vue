<script setup>
// 任务列表视图（批次 2）：迁移旧 view-tasks 的任务列表部分。
// 数据契约（api/routes/routes_tasks.py，见 dev-docs/API契约.md §1.3）：
// - GET /api/tasks?status=&q=&page=&page_size= → {tasks[], total_count, page, page_size}（分页 clamp 1-200）
//   status=scheduled 时按 task_type='scheduled' 过滤；其余按 status 过滤
// - POST /api/tasks/{id}/interrupt、DELETE /api/tasks/{id}
// - POST /api/tasks/schedule {title, query, cron, session_id=1}（注意字段是 query/cron，
//   旧前端发送 user_query/schedule_cron 与后端模型不符，创建一直失败——本视图按实际契约实现）
// - PUT /api/tasks/{id}/schedule {title, query, cron, session_id}、POST /api/tasks/{id}/toggle-schedule
// - GET /api/processes → {processes, discovered[]}（含孤儿进程，验收修复 B 接入）
//   processes 键为 "{task_id}:{pid}"（一任务多进程展平；孤儿进程仍为 orphan_id），
//   每项含 task_id/pid/command/output_file/started_at/uptime/alive，可带 detached:true；
//   discovered 为 OS 扫描发现的、与工作目录相关但未追踪的进程 {pid,name,cmdline,create_time,uptime}
// - POST /api/processes/{pid}/kill（按 pid 终止进程树，tracked/discovered 通用；后端有安全校验，失败返回 403/404）
import { computed, h, onMounted, onUnmounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { ElMessage, ElMessageBox } from 'element-plus';
import { Plus, Refresh, Search, Delete } from '@element-plus/icons-vue';
import { request } from '../api/client';
import zh from '../i18n/zh';

const t = zh.tasks;
const router = useRouter();
const JSON_HEADERS = { 'Content-Type': 'application/json' };
const PAGE_SIZE = 50;
const POLL_MS = 5000;
// 这些状态视为「仍活跃」，列表页存在活跃任务时轮询刷新。
// 'detached' 为历史遗留状态：后端已无任何写入点（全仓 grep 无 status='detached'
// 的 UPDATE），保留仅用于兼容旧数据库中可能残留的行，新数据不会出现。
const ACTIVE_STATUSES = new Set(['running', 'detached', 'backgrounded']);
// 可中断的状态（'detached' 同上，仅为旧数据兼容保留）
const INTERRUPTIBLE = new Set(['running', 'detached', 'backgrounded']);

const loading = ref(true);
const tasksData = ref([]);
const total = ref(0);
const page = ref(1);
const filter = ref('all');
const searchQuery = ref('');

// 状态 → 公共 status-pill 变体（运行蓝/后台紫/成功绿/失败红/中断橙/定时竹绿）
function statusPill(status) {
  const map = {
    running: 'info',
    detached: 'purple',
    backgrounded: 'purple',
    completed: 'success',
    failed: 'danger',
    background_failed: 'danger',
    stuck: 'danger',
    interrupted: 'warning',
    scheduled: 'primary',
  };
  return { label: t.status[status] || status, cls: map[status] || 'default' };
}

// 标题兜底：空 title 回退 user_query 截断 80 字符，再空显示（无标题）
function displayTitle(task) {
  const title = (task.title || '').trim();
  if (title) return title;
  const query = (task.user_query || '').trim();
  if (query) return query.length > 80 ? `${query.slice(0, 80)}…` : query;
  return t.noTitle;
}

function typeLabel(taskType) {
  return t.type[taskType] || taskType || t.type.oneshot;
}

function canInterrupt(task) {
  return INTERRUPTIBLE.has(task.status);
}

function isScheduled(task) {
  return task.task_type === 'scheduled';
}

async function loadTasks({ silent = false } = {}) {
  if (!silent) loading.value = true;
  try {
    const params = new URLSearchParams({ page: String(page.value), page_size: String(PAGE_SIZE) });
    if (filter.value !== 'all') params.set('status', filter.value);
    if (searchQuery.value) params.set('q', searchQuery.value);
    const data = await request(`/api/tasks?${params.toString()}`);
    tasksData.value = Array.isArray(data?.tasks) ? data.tasks : [];
    total.value = data?.total_count ?? 0;
  } catch (err) {
    if (!silent) ElMessage.error(`${t.loadFailed}: ${err.message}`);
  } finally {
    if (!silent) loading.value = false;
  }
}

// ── 进程管理（验收修复 B）：列表行内进程徽标 + 顶部「全部进程」折叠面板 ──

const processes = ref({}); // {"{task_id}:{pid}" 或 orphan_id: {task_id?,pid,command,alive,uptime,detached?,...}}
const discoveredProcs = ref([]); // OS 扫描发现的未追踪进程 [{pid,name,cmdline,create_time,uptime}]
const processesLoading = ref(false);
const processPanelOpen = ref([]); // el-collapse v-model（数组）

async function loadProcesses({ silent = true } = {}) {
  if (!silent) processesLoading.value = true;
  try {
    const data = await request('/api/processes');
    processes.value = data?.processes || {};
    // 旧后端无 discovered 字段，兜底为空数组（分区不显示）
    discoveredProcs.value = Array.isArray(data?.discovered) ? data.discovered : [];
  } catch (err) {
    if (!silent) ElMessage.error(`${t.processes.loadFailed}: ${err.message}`);
    processes.value = {};
    discoveredProcs.value = [];
  } finally {
    if (!silent) processesLoading.value = false;
  }
}

const processList = computed(() =>
  Object.entries(processes.value).map(([id, p]) => ({ id, ...(p || {}) }))
);

function aliveProcess(task) {
  // 一任务可有多个进程：按 task_id 匹配，取第一个存活进程供徽标显示
  return processList.value.find((p) => p.alive && String(p.task_id) === String(task.id)) || null;
}

function aliveCount(task) {
  return processList.value.filter((p) => p.alive && String(p.task_id) === String(task.id)).length;
}

// 行对应的任务 id：新格式取项内 task_id；旧格式（键即 task_id）回退用 id。孤儿进程返回 null。
function rowTaskId(row) {
  const tid = row.task_id ?? row.id;
  return isNumericId(tid) ? String(tid) : null;
}

function isNumericId(id) {
  return /^\d+$/.test(String(id));
}

function fmtUptime(sec) {
  const s = Math.max(0, Math.floor(sec || 0));
  if (s < 60) return `${s}s`;
  if (s < 3600) return `${Math.floor(s / 60)}m ${s % 60}s`;
  return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`;
}

// 按 pid 终止单个进程树：tracked（含孤儿）与 discovered 行通用，
// 后端强制安全校验（仅 sandbox 相关进程），失败时 err.message 即后端 detail。
async function killProcess(proc) {
  try {
    await ElMessageBox.confirm(`PID ${proc.pid} — ${t.processes.killConfirm}`, t.processes.kill, { type: 'warning' });
  } catch {
    return; // 用户取消
  }
  try {
    await request(`/api/processes/${proc.pid}/kill`, { method: 'POST' });
    ElMessage.success(t.processes.killSuccess);
    loadProcesses({ silent: true });
    loadTasks({ silent: true });
  } catch (err) {
    ElMessage.error(`${t.processes.killFailed}: ${err.message}`);
  }
}

// ── 筛选 / 搜索 / 分页 ──

function onFilterChange() {
  page.value = 1;
  loadTasks();
}

let searchTimer = null;
function onSearchInput() {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    page.value = 1;
    loadTasks();
  }, 300);
}

function onPageChange(p) {
  page.value = p;
  loadTasks();
}

// ── 自动刷新：仅当当前页存在活跃任务时轮询 ──

let pollTimer = null;

function hasActiveTask() {
  return tasksData.value.some((task) => ACTIVE_STATUSES.has(task.status));
}

function startPolling() {
  stopPolling();
  pollTimer = setInterval(() => {
    // 有活跃任务或进程面板展开时静默刷新任务与进程
    if (hasActiveTask() || processPanelOpen.value.length) {
      loadTasks({ silent: true });
      loadProcesses({ silent: true });
    }
  }, POLL_MS);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

// ── 行操作 ──

function openDetail(task) {
  router.push(`/tasks/${task.id}`);
}

async function interruptTask(task) {
  try {
    await ElMessageBox.confirm(
      `#${task.id} ${task.title} — ${t.actions.interruptConfirmText}`,
      t.actions.interruptConfirmTitle,
      { confirmButtonText: t.actions.interrupt, cancelButtonText: t.schedule.cancel, type: 'warning' }
    );
  } catch {
    return; // 用户取消
  }
  try {
    await request(`/api/tasks/${task.id}/interrupt`, { method: 'POST' });
    ElMessage.success(t.actions.interruptSuccess);
    loadTasks({ silent: true });
  } catch (err) {
    ElMessage.error(`${t.actions.interruptFailed}: ${err.message}`);
  }
}

// 删除任务：若该任务有交付物（outputs/task_<id>/），确认框提供
// 「同时删除交付物目录」勾选（默认不勾）——沙箱治理二期 delete_artifacts 联动。
const deleteArtifacts = ref(false);

async function deleteTask(task) {
  let hasArtifacts = false;
  try {
    const art = await request(`/api/tasks/${task.id}/artifacts`);
    hasArtifacts = Array.isArray(art?.files) && art.files.length > 0;
  } catch {
    hasArtifacts = false; // 查询失败不阻断删除，按无交付物处理
  }
  deleteArtifacts.value = false;
  const message = hasArtifacts
    ? h('div', null, [
        h('p', { style: 'margin: 0 0 8px;' }, `#${task.id} ${task.title} — ${t.actions.deleteConfirmText}`),
        h('label', { style: 'display: flex; align-items: center; gap: 6px; cursor: pointer;' }, [
          h('input', {
            type: 'checkbox',
            onChange: (ev) => { deleteArtifacts.value = ev.target.checked; },
          }),
          h('span', null, t.actions.deleteArtifactsLabel),
        ]),
      ])
    : `#${task.id} ${task.title} — ${t.actions.deleteConfirmText}`;
  try {
    await ElMessageBox.confirm(
      message,
      t.actions.deleteConfirmTitle,
      { confirmButtonText: t.actions.delete, cancelButtonText: t.schedule.cancel, type: 'warning' }
    );
  } catch {
    return;
  }
  try {
    const qs = hasArtifacts && deleteArtifacts.value ? '?delete_artifacts=true' : '';
    const resp = await request(`/api/tasks/${task.id}${qs}`, { method: 'DELETE' });
    // 交付物联动删除结果如实提示（评审 I3）：成功 N 项 / 失败 N 项 / 无交付物目录
    if (hasArtifacts && deleteArtifacts.value) {
      const removed = Array.isArray(resp?.artifacts_removed) ? resp.artifacts_removed.length : 0;
      const errors = Array.isArray(resp?.artifacts_errors) ? resp.artifacts_errors.length : 0;
      if (removed > 0) ElMessage.success(t.actions.deleteArtifactsDeleted.replace('{n}', removed));
      if (errors > 0) ElMessage.warning(t.actions.deleteArtifactsFailed.replace('{n}', errors));
      if (!removed && !errors) ElMessage.success(t.actions.deleteArtifactsNone);
    } else {
      ElMessage.success(t.actions.deleteSuccess);
    }
    loadTasks({ silent: true });
  } catch (err) {
    ElMessage.error(`${t.actions.deleteFailed}: ${err.message}`);
  }
}

async function toggleSchedule(task) {
  try {
    const res = await request(`/api/tasks/${task.id}/toggle-schedule`, { method: 'POST' });
    task.schedule_enabled = !!res?.enabled;
  } catch (err) {
    ElMessage.error(`${t.actions.toggleFailed}: ${err.message}`);
  }
}

// ── 定时任务弹窗（创建 / 编辑） ──

const scheduleDialog = ref(false);
const scheduleSaving = ref(false);
const scheduleEditId = ref(null); // null=创建
const scheduleForm = ref({ title: '', query: '', cron: '' });

const CRON_RE = /^\S+\s+\S+\s+\S+\s+\S+\s+\S+$/;

function openScheduleCreate() {
  scheduleEditId.value = null;
  scheduleForm.value = { title: '', query: '', cron: '' };
  scheduleDialog.value = true;
}

function openScheduleEdit(task) {
  scheduleEditId.value = task.id;
  scheduleForm.value = {
    title: task.title || '',
    query: task.user_query || '',
    cron: task.schedule_cron || '',
  };
  scheduleDialog.value = true;
}

async function saveSchedule() {
  const form = scheduleForm.value;
  const title = form.title.trim();
  const query = form.query.trim();
  const cron = form.cron.trim();
  if (!title) return ElMessage.error(t.schedule.titleRequired);
  if (!query) return ElMessage.error(t.schedule.queryRequired);
  if (!cron) return ElMessage.error(t.schedule.cronRequired);
  if (!CRON_RE.test(cron)) return ElMessage.error(t.schedule.cronInvalid);

  scheduleSaving.value = true;
  try {
    const body = JSON.stringify({ title, query, cron, session_id: 1 });
    if (scheduleEditId.value) {
      await request(`/api/tasks/${scheduleEditId.value}/schedule`, {
        method: 'PUT', headers: JSON_HEADERS, body,
      });
      ElMessage.success(t.schedule.saveSuccess);
    } else {
      await request('/api/tasks/schedule', { method: 'POST', headers: JSON_HEADERS, body });
      ElMessage.success(t.schedule.createSuccess);
    }
    scheduleDialog.value = false;
    loadTasks({ silent: true });
  } catch (err) {
    ElMessage.error(`${t.schedule.saveFailed}: ${err.message}`);
  } finally {
    scheduleSaving.value = false;
  }
}

onMounted(() => {
  loadTasks();
  loadProcesses({ silent: true });
  startPolling();
});

onUnmounted(() => {
  stopPolling();
  clearTimeout(searchTimer);
});
</script>

<template>
  <div class="tasks-view">
    <header class="view-header">
      <h1>{{ t.title }}</h1>
      <p class="view-desc">{{ t.desc }}</p>
    </header>

    <!-- 全部进程（折叠面板，验收修复 B） -->
    <el-collapse v-model="processPanelOpen" class="process-panel">
      <el-collapse-item name="all">
        <template #title>
          <span class="process-panel-title">🖥 {{ t.processes.panelTitle }}（{{ processList.length }}）</span>
        </template>
        <div class="process-panel-body">
          <div class="process-toolbar">
            <el-button size="small" :icon="Refresh" :loading="processesLoading" @click="loadProcesses({ silent: false })">
              {{ t.refresh }}
            </el-button>
          </div>
          <div v-if="!processList.length" class="empty-state">
            <div class="empty-icon">🖥️</div>
            <p>{{ t.processes.empty }}</p>
          </div>
          <el-table v-else :data="processList" size="small">
            <el-table-column :label="t.processes.colTask" width="100">
              <template #default="{ row }">
                <el-link v-if="rowTaskId(row)" type="primary" @click="router.push(`/tasks/${rowTaskId(row)}`)">
                  #{{ rowTaskId(row) }}
                </el-link>
                <span v-else class="orphan-label">{{ t.processes.orphan }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="pid" :label="t.processes.colPid" width="90" />
            <el-table-column :label="t.processes.colCommand" min-width="220" show-overflow-tooltip>
              <template #default="{ row }">
                <code class="proc-cmd">{{ row.command }}</code>
              </template>
            </el-table-column>
            <el-table-column :label="t.processes.colUptime" width="110" align="right">
              <template #default="{ row }">{{ fmtUptime(row.uptime) }}</template>
            </el-table-column>
            <el-table-column :label="t.processes.colStatus" width="150" align="center">
              <template #default="{ row }">
                <el-tag size="small" :type="row.alive ? 'success' : 'info'" disable-transitions>
                  {{ row.alive ? t.processes.alive : t.processes.dead }}
                </el-tag>
                <el-tag v-if="row.detached" size="small" type="warning" disable-transitions class="detached-tag">
                  {{ t.processes.detached }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column :label="t.processes.colActions" width="90" align="center">
              <template #default="{ row }">
                <el-button
                  v-if="row.alive && row.pid"
                  size="small"
                  type="danger"
                  plain
                  @click="killProcess(row)"
                >
                  {{ t.processes.kill }}
                </el-button>
              </template>
            </el-table-column>
          </el-table>
          <!-- 发现的进程：OS 扫描命中工作目录但未被任务追踪，为空时整块不显示 -->
          <div v-if="discoveredProcs.length" class="discovered-section">
            <div class="discovered-head">
              <span class="discovered-title">🔍 {{ t.processes.discoveredTitle }}（{{ discoveredProcs.length }}）</span>
              <p class="discovered-desc">{{ t.processes.discoveredDesc }}</p>
            </div>
            <el-table :data="discoveredProcs" size="small">
              <el-table-column prop="pid" :label="t.processes.colPid" width="90" />
              <el-table-column prop="name" :label="t.processes.colName" width="140" show-overflow-tooltip />
              <el-table-column :label="t.processes.colCmdline" min-width="220" show-overflow-tooltip>
                <template #default="{ row }">
                  <code class="proc-cmd">{{ row.cmdline }}</code>
                </template>
              </el-table-column>
              <el-table-column :label="t.processes.colUptime" width="110" align="right">
                <template #default="{ row }">{{ fmtUptime(row.uptime) }}</template>
              </el-table-column>
              <el-table-column :label="t.processes.colActions" width="90" align="center">
                <template #default="{ row }">
                  <el-button size="small" type="danger" plain @click="killProcess(row)">
                    {{ t.processes.kill }}
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>
        </div>
      </el-collapse-item>
    </el-collapse>

    <el-card class="list-card" shadow="never">
      <div class="toolbar">
        <el-radio-group v-model="filter" size="small" @change="onFilterChange">
          <el-radio-button v-for="f in t.filters" :key="f.value" :value="f.value">
            {{ f.label }}
          </el-radio-button>
        </el-radio-group>
        <div class="toolbar-right">
          <el-input
            v-model="searchQuery"
            class="search-input"
            size="small"
            clearable
            :prefix-icon="Search"
            :placeholder="t.searchPlaceholder"
            @input="onSearchInput"
          />
          <el-button size="small" :icon="Refresh" :title="t.refresh" @click="loadTasks()" />
          <el-button size="small" type="primary" :icon="Plus" @click="openScheduleCreate">
            {{ t.createSchedule }}
          </el-button>
        </div>
      </div>

      <div v-if="!tasksData.length && !loading" class="empty-state">
        <div class="empty-icon">📋</div>
        <p>{{ searchQuery || filter !== 'all' ? t.emptySearch : t.empty }}</p>
        <small v-if="!searchQuery && filter === 'all'">{{ t.emptyHint }}</small>
      </div>

      <div v-loading="loading">
        <div
          v-for="task in tasksData"
          :key="task.id"
          class="row-card task-card"
          @click="openDetail(task)"
        >
          <div class="row-card-head">
            <span class="task-id">#{{ task.id }}</span>
            <el-tag v-if="task.task_type && task.task_type !== 'oneshot'" size="small" type="warning" disable-transitions>
              {{ typeLabel(task.task_type) }}
            </el-tag>
            <span class="row-card-title" :title="displayTitle(task)">{{ displayTitle(task) }}</span>
            <div class="row-card-right" @click.stop>
              <template v-if="isScheduled(task)">
                <el-switch
                  :model-value="task.schedule_enabled"
                  size="small"
                  :title="task.schedule_enabled ? t.actions.disableSchedule : t.actions.enableSchedule"
                  @change="toggleSchedule(task)"
                />
                <el-button size="small" text @click="openScheduleEdit(task)">
                  {{ t.actions.editSchedule }}
                </el-button>
              </template>
              <el-button
                v-if="canInterrupt(task)"
                size="small"
                type="warning"
                plain
                @click="interruptTask(task)"
              >
                {{ t.actions.interrupt }}
              </el-button>
              <span class="status-pill" :class="`status-pill--${statusPill(task.status).cls}`">
                <span class="pill-dot"></span>{{ statusPill(task.status).label }}
              </span>
              <el-button
                text
                type="danger"
                class="delete-btn"
                :title="t.actions.delete"
                @click="deleteTask(task)"
              >
                <el-icon><Delete /></el-icon>
              </el-button>
            </div>
          </div>
          <div class="row-card-meta">
            <span>{{ task.created_at }}</span>
            <span>{{ task.step_count || 0 }}{{ t.stepsSuffix }}</span>
            <span v-if="task.session_id">
              {{ t.sessionPrefix }}{{ task.session_id }}<template v-if="task.session_name"> · {{ task.session_name }}</template>
            </span>
            <span v-if="aliveProcess(task)" class="process-badge">
              ⚙ {{ t.processes.badge }} PID {{ aliveProcess(task).pid }}<template v-if="aliveCount(task) > 1"> ×{{ aliveCount(task) }}</template>
            </span>
            <span v-if="isScheduled(task) && task.schedule_cron" class="schedule-info">
              {{ task.schedule_enabled ? t.schedule.enabled : t.schedule.disabled }} | <code>{{ task.schedule_cron }}</code>
              <template v-if="task.next_run_at"> | {{ t.schedule.nextRun }}: {{ task.next_run_at }}</template>
            </span>
            <span v-if="task.task_type === 'longrun' && task.resume_count > 0" class="schedule-info">
              🔄 {{ t.resumedPrefix }}{{ task.resume_count }}{{ t.resumedSuffix }}
            </span>
          </div>
        </div>
      </div>

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
    </el-card>

    <!-- 定时任务创建/编辑弹窗 -->
    <el-dialog
      v-model="scheduleDialog"
      :title="scheduleEditId ? t.schedule.editTitle : t.schedule.createTitle"
      width="480px"
    >
      <el-form label-position="top" @submit.prevent>
        <el-form-item :label="t.schedule.titleLabel">
          <el-input v-model="scheduleForm.title" :placeholder="t.schedule.titlePlaceholder" />
        </el-form-item>
        <el-form-item :label="t.schedule.queryLabel">
          <el-input
            v-model="scheduleForm.query"
            type="textarea"
            :rows="3"
            :placeholder="t.schedule.queryPlaceholder"
          />
        </el-form-item>
        <el-form-item :label="t.schedule.cronLabel">
          <el-input v-model="scheduleForm.cron" :placeholder="t.schedule.cronPlaceholder" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="scheduleDialog = false">{{ t.schedule.cancel }}</el-button>
        <el-button type="primary" :loading="scheduleSaving" @click="saveSchedule">
          {{ t.schedule.save }}
        </el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.tasks-view {
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
  margin-bottom: 16px;
  flex-wrap: wrap;
}

.toolbar-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.search-input {
  width: 220px;
}

.empty-state {
  padding: 32px 0;
  text-align: center;
  color: var(--el-text-color-secondary);
}

.empty-state p {
  margin: 0 0 4px;
}

/* 行卡片结构由全局 .row-card 提供，这里只补任务特有零件 */
.task-card {
  cursor: pointer;
}

.task-id {
  flex-shrink: 0;
  font-family: 'Cascadia Code', Consolas, monospace;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.delete-btn {
  padding: 4px;
  height: auto;
}

.schedule-info code {
  background: var(--el-fill-color);
  padding: 1px 4px;
  border-radius: 4px;
}

.pagination-bar {
  display: flex;
  justify-content: center;
  margin-top: 16px;
}

/* 全部进程折叠面板：与行卡片同风格（白底卡片 + 浅边框 + 圆角） */
.process-panel {
  margin-bottom: 16px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 10px;
  background: var(--el-bg-color);
  box-shadow: var(--panda-shadow-card);
  overflow: hidden;
}

.process-panel :deep(.el-collapse-item__header) {
  padding: 0 14px;
  border-bottom: none;
}

.process-panel :deep(.el-collapse-item__wrap) {
  border-top: 1px solid var(--el-border-color-lighter);
  border-bottom: none;
}

.process-panel :deep(.el-collapse-item__content) {
  padding: 8px 14px 14px;
}

.process-panel-title {
  font-size: 13px;
  font-weight: 600;
}

.process-panel-body {
  padding-top: 4px;
}

.process-toolbar {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 8px;
}

.orphan-label {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.proc-cmd {
  font-size: 12px;
  color: var(--el-text-color-regular);
}

/* 「已脱离追踪」徽标：跟在存活状态后，醒目但不刺眼（warning 浅色 tag） */
.detached-tag {
  margin-left: 6px;
}

/* 发现的进程分区：与上方追踪表同面板内分隔 */
.discovered-section {
  margin-top: 14px;
  padding-top: 10px;
  border-top: 1px dashed var(--el-border-color-lighter);
}

.discovered-title {
  font-size: 13px;
  font-weight: 600;
}

.discovered-desc {
  margin: 4px 0 8px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

/* 进程徽标：meta 行内小 chip（等宽） */
.process-badge {
  font-family: 'Cascadia Code', Consolas, monospace;
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 999px;
  background: var(--el-color-success-light-9);
  color: var(--el-color-success-dark-2);
}

@media (max-width: 768px) {
  .tasks-view {
    padding: 16px 16px 32px;
  }

  /* 工具栏纵向，搜索占满一行 */
  .toolbar {
    flex-direction: column;
    align-items: stretch;
  }

  .toolbar-right {
    flex-wrap: wrap;
  }

  .search-input {
    width: 100%;
  }

  /* 行首操作区在窄屏允许换行，但 pill 与删除按钮保持成组 */
  .row-card-right {
    flex-wrap: wrap;
    justify-content: flex-end;
    row-gap: 4px;
  }
}
</style>
