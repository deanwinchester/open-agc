<script setup>
// 任务详情视图（批次 2）：迁移旧 view-task-detail。
// 数据契约（api/routes/routes_tasks.py）：
// - GET /api/tasks/{id} → {task{..., output_files[], steps[]}}（steps 用分页端点另取）
// - GET /api/tasks/{id}/steps?page=&page_size= → {steps[], total, page, page_size, total_pages}（created_at DESC）
// - GET /api/tasks/{id}/process → {process{pid,command,alive,uptime,output_file} | null}
// - GET /api/tasks/{id}/logs?lines= → {logs, lines[]}
// - POST /api/tasks/{id}/interrupt | /complete | /kill | /reset-resume-count；DELETE /api/tasks/{id}
// 旧实现已知问题，此处规避：
// - 步骤分页闭包错位：步骤详情弹窗直接使用当前页数组里的 step 对象（Vue 响应式 props），无闭包捕获旧页数据
// - _logRefreshInterval 泄漏：本组件所有 interval 统一登记，onUnmounted 全部清理
import { computed, nextTick, onMounted, onUnmounted, ref } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { ElMessage, ElMessageBox } from 'element-plus';
import { ArrowLeft, Refresh } from '@element-plus/icons-vue';
import { request } from '../api/client';
import zh from '../i18n/zh';

const t = zh.taskDetail;
const route = useRoute();
const router = useRouter();

const taskId = Number(route.params.id);
const STEP_PAGE_SIZE = 50;
const STEP_POLL_MS = 5000;
const LOG_POLL_MS = 3000;
const LOG_LINE_OPTIONS = [50, 100, 200, 500];
const INTERRUPTIBLE = new Set(['running', 'detached', 'backgrounded']);

// ── 任务元信息 ──

const task = ref(null);
const loading = ref(true);
const loadError = ref('');

// 状态 → 公共 status-pill 变体（与任务列表同一映射：运行蓝/后台紫/成功绿/失败红/中断橙）
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
  return { label: zh.tasks.status[status] || status, cls: map[status] || 'default' };
}

const typeLabel = computed(() => zh.tasks.type[task.value?.task_type] || task.value?.task_type || zh.tasks.type.oneshot);
const canInterrupt = computed(() => task.value && INTERRUPTIBLE.has(task.value.status));
const canComplete = computed(() => task.value && task.value.status !== 'completed');
const isRunning = computed(() => task.value && INTERRUPTIBLE.has(task.value.status));
const interruptReasonText = computed(() => {
  const reason = task.value?.interruption_reason;
  if (!reason) return '';
  return t.reasons[reason] || reason;
});
const tokenInfo = computed(() => {
  const tk = task.value;
  if (!tk) return null;
  const total = tk.total_tokens || 0;
  const cost = tk.total_cost || 0;
  if (!total && !cost) return null;
  return {
    total,
    cost: Number(cost).toFixed(4),
    prompt: tk.prompt_tokens || 0,
    completion: tk.completion_tokens || 0,
    cached: tk.cached_tokens || 0,
  };
});

async function loadTask({ silent = false } = {}) {
  if (!silent) loading.value = true;
  try {
    const data = await request(`/api/tasks/${taskId}`);
    task.value = data?.task || null;
    loadError.value = '';
  } catch (err) {
    loadError.value = err.message;
    if (!silent) ElMessage.error(`${t.loadFailed}: ${err.message}`);
  } finally {
    if (!silent) loading.value = false;
  }
}

// ── 步骤（分页，DESC；展示序号换算为正序） ──

const steps = ref([]);
const stepsTotal = ref(0);
const stepsPage = ref(1);
const stepsLoading = ref(false);

async function loadSteps() {
  stepsLoading.value = true;
  try {
    const data = await request(`/api/tasks/${taskId}/steps?page=${stepsPage.value}&page_size=${STEP_PAGE_SIZE}`);
    steps.value = Array.isArray(data?.steps) ? data.steps : [];
    stepsTotal.value = data?.total ?? 0;
  } catch (err) {
    ElMessage.error(`${t.steps.loadFailed}: ${err.message}`);
  } finally {
    stepsLoading.value = false;
  }
}

function onStepsPageChange(p) {
  stepsPage.value = p;
  loadSteps();
}

// 步骤按 created_at DESC 返回，显示序号 = 正序编号
function stepDisplayNum(index) {
  return stepsTotal.value - (stepsPage.value - 1) * STEP_PAGE_SIZE - index;
}

function stepState(st) {
  if (st.success === true || st.success === 1) return 'success';
  if (st.success === false || st.success === 0) return 'failed';
  return 'running';
}

function stepIcon(st) {
  return { success: '✅', failed: '❌', running: '⏳' }[stepState(st)];
}

// ── 步骤详情弹窗：直接接收当前页 steps 数组中的对象，杜绝旧版闭包错位 ──

const stepDialog = ref(false);
const activeStep = ref(null);
const activeStepNum = ref(0);

function openStepDetail(st, index) {
  activeStep.value = st;
  activeStepNum.value = stepDisplayNum(index);
  stepDialog.value = true;
}

// ── 进程信息 ──

const processInfo = ref(null);
const processLoading = ref(false);
const PROC_POLL_MS = 5000;
let procTimerId = null;

function stopProcTimer() {
  if (procTimerId) {
    clearInterval(procTimerId);
    procTimerId = null;
  }
}

// 进程存活时保持 5s 轮询；消失/死亡即停止（onUnmounted 统一清理）
function syncProcTimer() {
  if (processInfo.value?.alive) {
    if (!procTimerId) procTimerId = setInterval(() => loadProcess({ silent: true }), PROC_POLL_MS);
  } else {
    stopProcTimer();
  }
}

async function loadProcess({ silent = false } = {}) {
  if (!silent) processLoading.value = true;
  try {
    const data = await request(`/api/tasks/${taskId}/process`);
    processInfo.value = data?.process || null;
  } catch {
    processInfo.value = null;
  } finally {
    if (!silent) processLoading.value = false;
    syncProcTimer();
  }
}

const processUptimeMin = computed(() =>
  processInfo.value?.uptime ? Math.floor(processInfo.value.uptime / 60) : 0
);

async function killProcess() {
  try {
    await ElMessageBox.confirm(t.process.killConfirm, t.process.kill, { type: 'warning' });
  } catch {
    return;
  }
  try {
    await request(`/api/tasks/${taskId}/kill`, { method: 'POST' });
    ElMessage.success(t.process.killSuccess);
    loadTask({ silent: true });
    loadProcess({ silent: true });
  } catch (err) {
    ElMessage.error(`${t.process.killFailed}: ${err.message}`);
  }
}

// ── 进程日志 tail ──

const logLines = ref([]);
const logLinesCount = ref(100);
const logLoading = ref(false);
const logAuto = ref(false);
const logPreRef = ref(null);

const logText = computed(() => (logLines.value.length ? logLines.value.join('\n') : t.logs.empty));

async function loadLogs() {
  logLoading.value = true;
  try {
    const data = await request(`/api/tasks/${taskId}/logs?lines=${logLinesCount.value}`);
    logLines.value = Array.isArray(data?.lines) ? data.lines : [];
    await nextTick();
    if (logPreRef.value) logPreRef.value.scrollTop = logPreRef.value.scrollHeight;
  } catch {
    // 日志文件可能不存在，静默显示空态
    logLines.value = [];
  } finally {
    logLoading.value = false;
  }
}

// ── 自动刷新（步骤 + 任务状态 / 日志），onUnmounted 统一清理，杜绝旧版 interval 泄漏 ──

const stepAuto = ref(false);
let stepTimerId = null;
let logTimerId = null;

function stopStepTimer() {
  if (stepTimerId) {
    clearInterval(stepTimerId);
    stepTimerId = null;
  }
}

function stopLogTimer() {
  if (logTimerId) {
    clearInterval(logTimerId);
    logTimerId = null;
  }
}

function onStepAutoChange(val) {
  stopStepTimer();
  if (!val) return;
  stepTimerId = setInterval(() => {
    loadTask({ silent: true });
    loadSteps();
    // 任务已终态：自动关闭自动刷新
    if (task.value && !INTERRUPTIBLE.has(task.value.status)) {
      stepAuto.value = false;
      stopStepTimer();
    }
  }, STEP_POLL_MS);
}

function onLogAutoChange(val) {
  stopLogTimer();
  if (val) logTimerId = setInterval(loadLogs, LOG_POLL_MS);
}

// ── 任务操作 ──

async function interruptTask() {
  try {
    await ElMessageBox.confirm(
      zh.tasks.actions.interruptConfirmText,
      zh.tasks.actions.interruptConfirmTitle,
      { confirmButtonText: zh.tasks.actions.interrupt, cancelButtonText: zh.goals.cancel, type: 'warning' }
    );
  } catch {
    return;
  }
  try {
    await request(`/api/tasks/${taskId}/interrupt`, { method: 'POST' });
    ElMessage.success(zh.tasks.actions.interruptSuccess);
    loadTask({ silent: true });
  } catch (err) {
    ElMessage.error(`${zh.tasks.actions.interruptFailed}: ${err.message}`);
  }
}

async function completeTask() {
  try {
    await ElMessageBox.confirm(t.actions.completeConfirm, t.actions.complete, { type: 'warning' });
  } catch {
    return;
  }
  try {
    await request(`/api/tasks/${taskId}/complete`, { method: 'POST' });
    ElMessage.success(t.actions.completeSuccess);
    loadTask({ silent: true });
  } catch (err) {
    ElMessage.error(`${t.actions.completeFailed}: ${err.message}`);
  }
}

async function deleteTask() {
  try {
    await ElMessageBox.confirm(t.actions.deleteConfirmText, t.actions.deleteConfirmTitle, {
      confirmButtonText: t.actions.delete,
      cancelButtonText: zh.goals.cancel,
      type: 'warning',
    });
  } catch {
    return;
  }
  try {
    await request(`/api/tasks/${taskId}`, { method: 'DELETE' });
    ElMessage.success(t.actions.deleteSuccess);
    router.push('/tasks');
  } catch (err) {
    ElMessage.error(`${t.actions.deleteFailed}: ${err.message}`);
  }
}

async function resetResume() {
  try {
    await ElMessageBox.confirm(t.resumeResetConfirm, t.resumeReset, { type: 'warning' });
  } catch {
    return;
  }
  try {
    await request(`/api/tasks/${taskId}/reset-resume-count`, { method: 'POST' });
    ElMessage.success(t.resumeResetSuccess);
    loadTask({ silent: true });
  } catch (err) {
    ElMessage.error(`${t.resumeResetFailed}: ${err.message}`);
  }
}

onMounted(async () => {
  await loadTask();
  loadSteps();
  loadLogs();
  // 总是尝试加载进程信息（端点会先认领孤儿进程）；存活时自动进入 5s 轮询
  loadProcess();
  if (isRunning.value) {
    stepAuto.value = true;
    onStepAutoChange(true);
  }
});

onUnmounted(() => {
  stopStepTimer();
  stopLogTimer();
  stopProcTimer();
});
</script>

<template>
  <div class="task-detail-view" v-loading="loading">
    <header class="view-header">
      <el-button :icon="ArrowLeft" text @click="router.push('/tasks')">{{ t.back }}</el-button>
      <h1 class="task-heading">#{{ taskId }} {{ task?.title || '' }}</h1>
    </header>

    <div v-if="loadError && !task" class="empty-state">
      <div class="empty-icon">🔍</div>
      <p>{{ t.notFound }}</p>
    </div>

    <template v-if="task">
      <!-- 元信息 -->
      <el-card class="detail-card" shadow="never">
        <div class="meta-chips">
          <span class="status-pill" :class="`status-pill--${statusPill(task.status).cls}`">
            <span class="pill-dot"></span>{{ statusPill(task.status).label }}
          </span>
          <el-tag type="info" disable-transitions>{{ typeLabel }}</el-tag>
          <span class="meta-chip">🕐 {{ task.created_at }}</span>
          <span class="meta-chip">📊 {{ stepsTotal }}{{ zh.tasks.stepsSuffix }}</span>
          <span v-if="task.session_id" class="meta-chip">
            💬 {{ zh.tasks.sessionPrefix }}{{ task.session_id }}<template v-if="task.session_name"> · {{ task.session_name }}</template>
          </span>
          <span v-if="tokenInfo" class="meta-chip token-chip">
            🔤 {{ t.tokens.total }} {{ tokenInfo.total.toLocaleString() }} tokens
            <template v-if="Number(tokenInfo.cost) > 0"> (¥{{ tokenInfo.cost }})</template>
            <span class="token-detail">
              {{ t.tokens.input }} {{ tokenInfo.prompt.toLocaleString() }} ·
              {{ t.tokens.output }} {{ tokenInfo.completion.toLocaleString() }} ·
              {{ t.tokens.cached }} {{ tokenInfo.cached.toLocaleString() }}
            </span>
          </span>
        </div>

        <div class="action-bar">
          <el-button v-if="canInterrupt" size="small" type="warning" plain @click="interruptTask">
            ⏹ {{ t.actions.interrupt }}
          </el-button>
          <el-button v-if="canComplete" size="small" type="success" plain @click="completeTask">
            ✅ {{ t.actions.complete }}
          </el-button>
          <el-button size="small" type="danger" plain @click="deleteTask">
            🗑 {{ t.actions.delete }}
          </el-button>
        </div>

        <div class="section">
          <div class="section-title">{{ t.goal }}</div>
          <div class="section-block">{{ task.user_query }}</div>
        </div>

        <div v-if="task.task_type === 'scheduled'" class="section">
          <div class="section-title">{{ t.scheduleConfig }}</div>
          <div class="section-block">
            <div><strong>{{ t.cron }}:</strong> <code>{{ task.schedule_cron || '--' }}</code></div>
            <div>
              <strong>{{ zh.goals.statusLabel }}:</strong>
              {{ task.schedule_enabled ? '✅ ' + t.scheduleEnabled : '⏸️ ' + t.scheduleDisabled }}
            </div>
            <div><strong>{{ t.nextRun }}:</strong> {{ task.next_run_at || '—' }}</div>
          </div>
        </div>

        <div v-if="interruptReasonText" class="section">
          <div class="section-title">{{ t.interruptReason }}</div>
          <div class="section-block">{{ interruptReasonText }}</div>
        </div>

        <div v-if="task.resume_count > 0" class="section">
          <div class="section-title">{{ t.resumeStats }}</div>
          <div class="section-block resume-row">
            <span>{{ t.resumedPrefix }}{{ task.resume_count }} / {{ task.max_resume_count || 10 }}{{ t.resumeUnit }}</span>
            <el-button size="small" @click="resetResume">{{ t.resumeReset }}</el-button>
          </div>
        </div>

        <div v-if="task.result_summary" class="section">
          <div class="section-title">{{ t.result }}</div>
          <div class="section-block">{{ task.result_summary }}</div>
        </div>

        <div v-if="task.output_files && task.output_files.length" class="section">
          <div class="section-title">{{ t.outputFiles }}</div>
          <div class="file-chips">
            <span v-for="(f, i) in task.output_files" :key="i" class="meta-chip">📄 {{ f }}</span>
          </div>
        </div>

        <!-- 进程信息（存在后台进程时显示） -->
        <div v-if="processInfo" class="section">
          <div class="section-title process-title-row">
            <span>{{ t.process.title }}</span>
            <el-button
              size="small"
              text
              :icon="Refresh"
              :loading="processLoading"
              :title="t.process.refresh"
              @click="loadProcess()"
            />
          </div>
          <div class="section-block process-row">
            <span class="process-status">
              <span class="dot" :class="{ alive: processInfo.alive }"></span>
              {{ processInfo.alive ? t.process.alive : t.process.dead }}
            </span>
            <span>PID: {{ processInfo.pid }}</span>
            <span>{{ t.process.uptimePrefix }}{{ processUptimeMin }}{{ t.process.uptimeSuffix }}</span>
            <code class="process-cmd" :title="processInfo.command">{{ processInfo.command }}</code>
            <el-button v-if="processInfo.alive" size="small" type="danger" plain @click="killProcess">
              ⏹ {{ t.process.kill }}
            </el-button>
          </div>
          <div v-if="processInfo.output_file" class="process-output">
            {{ t.process.outputFile }}: <code>{{ processInfo.output_file }}</code>
          </div>
        </div>
      </el-card>

      <!-- 执行步骤 -->
      <el-card class="detail-card" shadow="never">
        <div class="card-toolbar">
          <span class="section-title">{{ t.steps.title }}</span>
          <el-checkbox v-model="stepAuto" size="small" @change="onStepAutoChange">
            {{ t.steps.autoRefresh }}
          </el-checkbox>
        </div>

        <div v-if="!steps.length && !stepsLoading" class="empty-state">
          <div class="empty-icon">🪜</div>
          <p>{{ t.steps.empty }}</p>
        </div>

        <div v-loading="stepsLoading">
          <div
            v-for="(st, i) in steps"
            :key="st.id ?? `${stepsPage}-${i}`"
            class="step-card"
            :class="stepState(st)"
            @click="openStepDetail(st, i)"
          >
            <div class="step-header">
              <span>{{ stepIcon(st) }}</span>
              <span class="step-title">{{ stepDisplayNum(i) }}. {{ st.tool_label || st.tool_name }}</span>
              <span class="step-hint">{{ t.steps.viewDetail }} ▸</span>
            </div>
            <div v-if="st.args_preview" class="step-preview args">{{ st.args_preview }}</div>
            <div v-if="st.result_preview" class="step-preview">
              {{ st.result_preview.substring(0, 300) }}<template v-if="st.result_preview.length > 300">...</template>
            </div>
          </div>
        </div>

        <div v-if="stepsTotal > STEP_PAGE_SIZE" class="pagination-bar">
          <el-pagination
            background
            layout="total, prev, pager, next"
            :total="stepsTotal"
            :page-size="STEP_PAGE_SIZE"
            :current-page="stepsPage"
            @current-change="onStepsPageChange"
          />
        </div>
      </el-card>

      <!-- 进程日志 -->
      <el-card class="detail-card" shadow="never">
        <div class="card-toolbar">
          <span class="section-title">{{ t.logs.title }}</span>
          <div class="log-toolbar">
            <el-checkbox v-model="logAuto" size="small" @change="onLogAutoChange">
              {{ t.logs.autoRefresh }}
            </el-checkbox>
            <el-select v-model="logLinesCount" size="small" class="lines-select" @change="loadLogs">
              <el-option v-for="n in LOG_LINE_OPTIONS" :key="n" :label="n" :value="n" />
            </el-select>
            <el-button size="small" :icon="Refresh" :loading="logLoading" @click="loadLogs">
              {{ t.logs.refresh }}
            </el-button>
          </div>
        </div>
        <pre ref="logPreRef" class="log-content" v-loading="logLoading">{{ logText }}</pre>
      </el-card>
    </template>

    <!-- 步骤详情弹窗 -->
    <el-dialog v-model="stepDialog" :title="`${t.steps.stepPrefix}${activeStepNum} ${t.steps.detailTitle}`" width="640px">
      <template v-if="activeStep">
        <div class="section">
          <div class="section-title">{{ t.steps.tool }}</div>
          <div><strong>{{ activeStep.tool_label || activeStep.tool_name }}</strong></div>
        </div>
        <div class="section">
          <div class="section-title">{{ t.steps.status }}</div>
          <div>
            {{ stepState(activeStep) === 'success' ? t.steps.success
              : stepState(activeStep) === 'failed' ? t.steps.failed : t.steps.running }}
          </div>
        </div>
        <div v-if="activeStep.full_args" class="section">
          <div class="section-title">{{ t.steps.args }}</div>
          <pre class="dialog-pre">{{ activeStep.full_args }}</pre>
        </div>
        <div v-if="activeStep.result_preview" class="section">
          <div class="section-title">{{ t.steps.resultPreview }}</div>
          <div class="section-block">{{ activeStep.result_preview }}</div>
        </div>
        <div v-if="activeStep.full_result" class="section">
          <div class="section-title">{{ t.steps.fullResult }}</div>
          <pre class="dialog-pre tall">{{ activeStep.full_result }}</pre>
        </div>
        <div v-if="activeStep.thinking_content" class="section">
          <div class="section-title">{{ t.steps.thinking }}</div>
          <div class="section-block">{{ activeStep.thinking_content }}</div>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.task-detail-view {
  padding: 24px 28px 40px;
  max-width: 1080px;
  margin: 0 auto;
}

.view-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 16px;
}

.task-heading {
  margin: 0;
  font-size: 20px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.detail-card {
  margin-bottom: 20px;
}

.meta-chips {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.meta-chip {
  font-size: 12px;
  color: var(--el-text-color-regular);
  background: var(--el-fill-color-light);
  border-radius: 999px;
  padding: 4px 12px;
}

.token-chip {
  display: inline-flex;
  flex-direction: column;
  gap: 2px;
}

.token-detail {
  font-size: 11px;
  color: var(--el-text-color-secondary);
}

.action-bar {
  display: flex;
  gap: 8px;
  margin-top: 12px;
  flex-wrap: wrap;
}

.section {
  margin-top: 16px;
}

.section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-secondary);
  margin-bottom: 6px;
}

.section-block {
  font-size: 13px;
  background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  padding: 10px 12px;
  white-space: pre-wrap;
  word-break: break-word;
}

.section-block code {
  background: var(--el-fill-color);
  padding: 1px 4px;
  border-radius: 4px;
}

.resume-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.file-chips {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

.process-row {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}

.process-title-row {
  display: flex;
  align-items: center;
  gap: 4px;
}

.process-output {
  margin-top: 6px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  word-break: break-all;
}

.process-output code {
  background: var(--el-fill-color);
  padding: 1px 4px;
  border-radius: 4px;
}

.process-status {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

.dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--el-text-color-placeholder);
  display: inline-block;
}

.dot.alive {
  background: var(--el-color-success);
}

.process-cmd {
  max-width: 420px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.card-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 12px;
}

.step-card {
  border: 1px solid var(--el-border-color-lighter);
  border-left: 3px solid var(--el-color-info);
  border-radius: 8px;
  padding: 8px 12px;
  margin-bottom: 8px;
  cursor: pointer;
  transition: background-color var(--panda-transition), box-shadow var(--panda-transition);
}

.step-card:hover {
  background: var(--el-color-primary-light-9);
  box-shadow: var(--panda-shadow-card);
}

.step-card.success {
  border-left-color: var(--el-color-success);
}

.step-card.failed {
  border-left-color: var(--el-color-danger);
}

.step-header {
  display: flex;
  align-items: center;
  gap: 8px;
}

.step-title {
  font-size: 13px;
  font-weight: 600;
}

.step-hint {
  margin-left: auto;
  font-size: 11px;
  color: var(--el-text-color-secondary);
}

.step-preview {
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-regular);
  word-break: break-all;
}

.step-preview.args {
  color: var(--el-text-color-secondary);
}

.pagination-bar {
  display: flex;
  justify-content: center;
  margin-top: 12px;
}

.log-toolbar {
  display: flex;
  align-items: center;
  gap: 8px;
}

.lines-select {
  width: 90px;
}

.log-content {
  max-height: 320px;
  min-height: 80px;
  margin: 0;
  padding: 12px;
  overflow: auto;
  background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  font-family: 'Cascadia Code', Consolas, monospace;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
}

.dialog-pre {
  margin: 0;
  padding: 10px;
  background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 240px;
  overflow-y: auto;
}

.dialog-pre.tall {
  max-height: 400px;
}

.empty-state {
  padding: 24px 0;
  text-align: center;
  color: var(--el-text-color-secondary);
}

@media (max-width: 768px) {
  .task-detail-view {
    padding: 16px 16px 32px;
  }

  .view-header {
    flex-wrap: wrap;
  }

  /* 长标题窄屏允许换行 */
  .task-heading {
    font-size: 18px;
    white-space: normal;
    word-break: break-word;
  }

  /* 进程信息纵向堆叠；代码块可横向滚动 */
  .process-row {
    flex-direction: column;
    align-items: stretch;
  }

  .process-cmd {
    max-width: 100%;
  }

  .section-block {
    overflow-x: auto;
  }
}
</style>
