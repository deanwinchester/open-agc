<script setup>
// 定时任务视图（一级菜单，与任务并列）：从 TasksView 拆出的定时任务管理。
// 数据契约（api/routes/routes_tasks.py）：
// - GET /api/tasks?status=scheduled → 按 task_type='scheduled' 过滤
// - POST /api/tasks/schedule {title, query, cron, session_id=1}
// - PUT /api/tasks/{id}/schedule、POST /api/tasks/{id}/toggle-schedule、DELETE /api/tasks/{id}
// 数量上限：后端 MAX_SCHEDULED_TASKS=10，超限时 400 并带中文提示。
import { onMounted, onUnmounted, ref } from 'vue';
import { useRouter } from 'vue-router';
import { ElMessage, ElMessageBox } from 'element-plus';
import { Plus, Refresh, Delete } from '@element-plus/icons-vue';
import { request } from '../api/client';
import { formatDbTime } from '../utils/time';
import { formToCron, cronToForm, describeCron, utcToLocalShort } from '../utils/schedule';
import zh from '../i18n/zh';

const t = zh.scheduledView;
const ts = zh.tasks;
const router = useRouter();
const JSON_HEADERS = { 'Content-Type': 'application/json' };

const loading = ref(true);
const tasks = ref([]);

async function loadTasks({ silent = false } = {}) {
  if (!silent) loading.value = true;
  try {
    const data = await request('/api/tasks?status=scheduled&page_size=200');
    tasks.value = Array.isArray(data?.tasks) ? data.tasks : [];
  } catch (err) {
    if (!silent) ElMessage.error(`${t.loadFailed}: ${err.message}`);
  } finally {
    if (!silent) loading.value = false;
  }
}

// ── 创建 / 编辑弹窗（从 TasksView 迁入，逻辑不变） ──

const scheduleDialog = ref(false);
const scheduleSaving = ref(false);
const scheduleEditId = ref(null); // null=创建
const DEFAULT_FREQ = { mode: 'interval_min', every: 10, hour: 9, minute: 0, weekday: 1, monthDay: 1, raw: '' };
const scheduleForm = ref({ title: '', query: '', freq: { ...DEFAULT_FREQ } });
const CRON_RE = /^\S+\s+\S+\s+\S+\s+\S+\s+\S+$/;

function openScheduleCreate() {
  scheduleEditId.value = null;
  scheduleForm.value = { title: '', query: '', freq: { ...DEFAULT_FREQ } };
  scheduleDialog.value = true;
}

function openScheduleEdit(task) {
  scheduleEditId.value = task.id;
  // 认识的表达式反解成友好表单；不认识的（复杂 cron）进高级模式保留原文
  const parsed = cronToForm(task.schedule_cron);
  const freq = parsed || { ...DEFAULT_FREQ, mode: 'advanced', raw: task.schedule_cron || '' };
  scheduleForm.value = { title: task.title || '', query: task.user_query || '', freq };
  scheduleDialog.value = true;
}

async function saveSchedule() {
  const form = scheduleForm.value;
  const title = form.title.trim();
  const query = form.query.trim();
  const cron = formToCron(form.freq);
  if (!title) return ElMessage.error(ts.schedule.titleRequired);
  if (!query) return ElMessage.error(ts.schedule.queryRequired);
  if (!cron) return ElMessage.error(ts.schedule.cronRequired);
  if (!CRON_RE.test(cron)) return ElMessage.error(ts.schedule.cronInvalid);

  scheduleSaving.value = true;
  try {
    const body = JSON.stringify({ title, query, cron, session_id: 1 });
    if (scheduleEditId.value) {
      await request(`/api/tasks/${scheduleEditId.value}/schedule`, { method: 'PUT', headers: JSON_HEADERS, body });
      ElMessage.success(ts.schedule.saveSuccess);
    } else {
      await request('/api/tasks/schedule', { method: 'POST', headers: JSON_HEADERS, body });
      ElMessage.success(ts.schedule.createSuccess);
    }
    scheduleDialog.value = false;
    loadTasks({ silent: true });
  } catch (err) {
    ElMessage.error(`${ts.schedule.saveFailed}: ${err.message}`);
  } finally {
    scheduleSaving.value = false;
  }
}

async function toggleSchedule(task) {
  try {
    const res = await request(`/api/tasks/${task.id}/toggle-schedule`, { method: 'POST' });
    task.schedule_enabled = !!res.enabled;
  } catch (err) {
    ElMessage.error(`${ts.actions.toggleFailed}: ${err.message}`);
  }
}

async function deleteTask(task) {
  try {
    await ElMessageBox.confirm(ts.actions.deleteConfirmText, ts.actions.deleteConfirmTitle, {
      type: 'warning', confirmButtonText: ts.actions.delete, cancelButtonText: ts.schedule.cancel,
    });
  } catch { return; }
  try {
    await request(`/api/tasks/${task.id}`, { method: 'DELETE' });
    ElMessage.success(ts.actions.deleteSuccess);
    loadTasks({ silent: true });
  } catch (err) {
    ElMessage.error(err.message);
  }
}

function displayTitle(task) {
  const title = (task.title || '').trim();
  if (title) return title;
  const query = (task.user_query || '').trim();
  return query.length > 80 ? `${query.slice(0, 80)}…` : query || ts.noTitle;
}

// 有正在执行的定时任务时静默轮询
let pollTimer = null;
function startPolling() {
  stopPolling();
  pollTimer = setInterval(() => {
    if (tasks.value.some((x) => x.status === 'running')) loadTasks({ silent: true });
  }, 5000);
}
function stopPolling() { if (pollTimer) { clearInterval(pollTimer); pollTimer = null; } }

onMounted(() => { loadTasks(); startPolling(); });
onUnmounted(stopPolling);
</script>

<template>
  <div class="scheduled-view">
    <header class="view-header">
      <h1>{{ t.title }}</h1>
      <p class="view-desc">{{ t.desc }}</p>
    </header>

    <el-card class="list-card" shadow="never">
      <div class="toolbar">
        <span class="quota-hint">{{ t.quotaHint }}</span>
        <div class="toolbar-right">
          <el-button size="small" :icon="Refresh" :title="ts.refresh" @click="loadTasks()" />
          <el-button size="small" type="primary" :icon="Plus" @click="openScheduleCreate">
            {{ ts.createSchedule }}
          </el-button>
        </div>
      </div>

      <div v-if="!tasks.length && !loading" class="empty-state">
        <div class="empty-icon">⏰</div>
        <p>{{ t.empty }}</p>
        <small>{{ t.emptyHint }}</small>
      </div>

      <div v-loading="loading">
        <div v-for="task in tasks" :key="task.id" class="row-card task-card">
          <div class="row-card-head">
            <span class="task-id">#{{ task.id }}</span>
            <span class="row-card-title" :title="displayTitle(task)">{{ displayTitle(task) }}</span>
            <div class="row-card-right" @click.stop>
              <el-switch
                :model-value="task.schedule_enabled"
                size="small"
                :title="task.schedule_enabled ? ts.actions.disableSchedule : ts.actions.enableSchedule"
                @change="toggleSchedule(task)"
              />
              <el-button size="small" text @click="openScheduleEdit(task)">{{ ts.actions.editSchedule }}</el-button>
              <el-button size="small" text type="primary" @click="router.push(`/tasks/${task.id}`)">
                {{ t.detail }}
              </el-button>
              <el-button text type="danger" class="delete-btn" :title="ts.actions.delete" @click="deleteTask(task)">
                <el-icon><Delete /></el-icon>
              </el-button>
            </div>
          </div>
          <div class="row-card-meta">
            <span class="schedule-info">
              {{ task.schedule_enabled ? ts.schedule.enabled : ts.schedule.disabled }}
              <template v-if="task.schedule_cron"> | {{ describeCron(task.schedule_cron) }}</template>
              <template v-if="task.next_run_at"> | {{ ts.schedule.nextRun }}: {{ utcToLocalShort(task.next_run_at) }}</template>
            </span>
            <span v-if="task.last_run_at">{{ t.lastRun }}: {{ formatDbTime(task.last_run_at) }}</span>
            <span>{{ t.runCount }}{{ task.run_count || 0 }}{{ t.timesUnit }}</span>
            <span v-if="task.status === 'running'" class="status-pill status-pill--info">
              <span class="pill-dot"></span>{{ ts.status.running }}
            </span>
          </div>
        </div>
      </div>
    </el-card>

    <!-- 定时任务创建/编辑弹窗（从 TasksView 迁入） -->
    <el-dialog :append-to-body="true"
      v-model="scheduleDialog"
      :title="scheduleEditId ? ts.schedule.editTitle : ts.schedule.createTitle"
      width="480px"
    >
      <el-form label-position="top" @submit.prevent>
        <el-form-item :label="ts.schedule.titleLabel">
          <el-input v-model="scheduleForm.title" :placeholder="ts.schedule.titlePlaceholder" />
        </el-form-item>
        <el-form-item :label="ts.schedule.queryLabel">
          <el-input v-model="scheduleForm.query" type="textarea" :rows="3" :placeholder="ts.schedule.queryPlaceholder" />
        </el-form-item>
        <el-form-item :label="ts.schedule.freqLabel">
          <el-select v-model="scheduleForm.freq.mode" style="width: 100%">
            <el-option :label="ts.schedule.freqIntervalMin" value="interval_min" />
            <el-option :label="ts.schedule.freqIntervalHour" value="interval_hour" />
            <el-option :label="ts.schedule.freqDaily" value="daily" />
            <el-option :label="ts.schedule.freqWeekly" value="weekly" />
            <el-option :label="ts.schedule.freqMonthly" value="monthly" />
            <el-option :label="ts.schedule.freqAdvanced" value="advanced" />
          </el-select>
        </el-form-item>
        <el-form-item v-if="scheduleForm.freq.mode === 'interval_min'" :label="ts.schedule.everyMin">
          <el-input-number v-model="scheduleForm.freq.every" :min="1" :max="1440" />
        </el-form-item>
        <el-form-item v-else-if="scheduleForm.freq.mode === 'interval_hour'" :label="ts.schedule.everyHour">
          <el-input-number v-model="scheduleForm.freq.every" :min="1" :max="168" />
        </el-form-item>
        <el-form-item v-else-if="scheduleForm.freq.mode === 'daily'" :label="ts.schedule.atTime">
          <el-input-number v-model="scheduleForm.freq.hour" :min="0" :max="23" />
          <span class="freq-sep">{{ ts.schedule.hour }}</span>
          <el-input-number v-model="scheduleForm.freq.minute" :min="0" :max="59" />
          <span class="freq-sep">{{ ts.schedule.minute }}</span>
        </el-form-item>
        <el-form-item v-else-if="scheduleForm.freq.mode === 'weekly'" :label="ts.schedule.weekDay + ' / ' + ts.schedule.atTime">
          <el-select v-model="scheduleForm.freq.weekday" style="width: 120px">
            <el-option v-for="(d, i) in ts.schedule.weekDays" :key="i" :label="d" :value="i" />
          </el-select>
          <el-input-number v-model="scheduleForm.freq.hour" :min="0" :max="23" style="margin-left:8px" />
          <span class="freq-sep">{{ ts.schedule.hour }}</span>
          <el-input-number v-model="scheduleForm.freq.minute" :min="0" :max="59" />
          <span class="freq-sep">{{ ts.schedule.minute }}</span>
        </el-form-item>
        <el-form-item v-else-if="scheduleForm.freq.mode === 'monthly'" :label="ts.schedule.monthDay + ' / ' + ts.schedule.atTime">
          <el-input-number v-model="scheduleForm.freq.monthDay" :min="1" :max="31" />
          <span class="freq-sep">{{ ts.schedule.monthDay }}</span>
          <el-input-number v-model="scheduleForm.freq.hour" :min="0" :max="23" style="margin-left:8px" />
          <span class="freq-sep">{{ ts.schedule.hour }}</span>
          <el-input-number v-model="scheduleForm.freq.minute" :min="0" :max="59" />
          <span class="freq-sep">{{ ts.schedule.minute }}</span>
        </el-form-item>
        <el-form-item v-else :label="ts.schedule.cronLabel">
          <el-input v-model="scheduleForm.freq.raw" :placeholder="ts.schedule.cronPlaceholder" />
        </el-form-item>
        <div v-if="scheduleForm.freq.mode !== 'advanced'" class="freq-preview">
          {{ describeCron(formToCron(scheduleForm.freq)) }}
          <span class="freq-tz">{{ ts.schedule.localTimeHint }}</span>
        </div>
      </el-form>
      <template #footer>
        <el-button @click="scheduleDialog = false">{{ ts.schedule.cancel }}</el-button>
        <el-button type="primary" :loading="scheduleSaving" @click="saveSchedule">{{ ts.schedule.save }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.scheduled-view {
  padding: 24px 28px 40px;
  max-width: 1080px;
  margin: 0 auto;
}
.view-header h1 { margin: 0 0 6px; font-size: 20px; }
.view-desc { margin: 0 0 20px; font-size: 13px; color: var(--el-text-color-secondary); }
.toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 16px; flex-wrap: wrap; }
.toolbar-right { display: flex; align-items: center; gap: 8px; }
.quota-hint { font-size: 12px; color: var(--el-text-color-secondary); }
.empty-state { padding: 32px 0; text-align: center; color: var(--el-text-color-secondary); }
.empty-state p { margin: 0 0 4px; }
.task-id { font-size: 12px; color: var(--el-text-color-secondary); }
.schedule-info { color: var(--el-text-color-secondary); }
.freq-sep { margin: 0 6px; color: var(--el-text-color-secondary); }
.freq-preview { font-size: 12px; color: var(--el-color-primary); margin-top: 4px; }
.freq-tz { color: var(--el-text-color-secondary); margin-left: 8px; }
</style>
