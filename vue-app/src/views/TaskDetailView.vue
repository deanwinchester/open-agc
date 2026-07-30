<script setup>
// 任务详情视图（批次 2）：迁移旧 view-task-detail。
// 数据契约（api/routes/routes_tasks.py）：
// - GET /api/tasks/{id} → {task{..., output_files[], steps[]}}（steps 用分页端点另取）
// - GET /api/tasks/{id}/steps?page=&page_size= → {steps[], total, page, page_size, total_pages}（created_at DESC）
// - GET /api/tasks/{id}/process → {process | null, processes[{pid,command,alive,uptime,output_file,reaped?,output_file_deleted?}]}
// - GET /api/tasks/{id}/logs?lines= → {logs, lines[]}
// - POST /api/tasks/{id}/interrupt | /complete | /kill | /reset-resume-count | /resume {extra_instruction?}；DELETE /api/tasks/{id}
// 旧实现已知问题，此处规避：
// - 步骤分页闭包错位：步骤详情弹窗直接使用当前页数组里的 step 对象（Vue 响应式 props），无闭包捕获旧页数据
// - _logRefreshInterval 泄漏：本组件所有 interval 统一登记，onUnmounted 全部清理
import { computed, h, nextTick, onMounted, onUnmounted, ref } from 'vue';
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
// 可手动恢复（▶ 继续）的状态：与 POST /api/tasks/{id}/resume 放行集合对齐
// （completed 虽在服务端允许集合内，但已收官任务不提供「继续」入口）
const RESUMABLE = new Set(['interrupted', 'backgrounded', 'background_failed', 'failed']);
// 「中断原因」区块只在真正处于中断语义的状态展示——进行中/已完成一律不显示历史原因
const INTERRUPT_SEMANTIC = new Set(['interrupted', 'background_failed']);

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
const canResume = computed(() => task.value && RESUMABLE.has(task.value.status));
const isRunning = computed(() => task.value && INTERRUPTIBLE.has(task.value.status));
const interruptReasonText = computed(() => {
  const reason = task.value?.interruption_reason;
  if (!reason) return '';
  return t.reasons[reason] || reason;
});
// 历史中断原因只对中断语义状态可见（进行中/已完成/失败不显示）
const showInterruptReason = computed(() =>
  Boolean(interruptReasonText.value) && INTERRUPT_SEMANTIC.has(task.value?.status)
);
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

// ── 进程信息（多行：主表存活进程 + 一次性回收行） ──

// GET /api/tasks/{id}/process → {process, processes[]}：逐行渲染 processes
// （兼容旧契约：无 processes 字段时回退单行 process）。alive:false →「已结束」
// 灰色徽标；reaped 行是死 pid 被惰性回收的一次性回显，下次拉取自然消失。
const processList = ref([]);
const processLoading = ref(false);
const PROC_POLL_MS = 5000;
let procTimerId = null;

function stopProcTimer() {
  if (procTimerId) {
    clearInterval(procTimerId);
    procTimerId = null;
  }
}

// 有进程存活时保持 5s 轮询；全部结束/消失即停止（onUnmounted 统一清理）
function syncProcTimer() {
  if (processList.value.some((p) => p.alive)) {
    if (!procTimerId) procTimerId = setInterval(() => loadProcess({ silent: true }), PROC_POLL_MS);
  } else {
    stopProcTimer();
  }
}

async function loadProcess({ silent = false } = {}) {
  if (!silent) processLoading.value = true;
  try {
    const data = await request(`/api/tasks/${taskId}/process`);
    processList.value = Array.isArray(data?.processes) && data.processes.length
      ? data.processes
      : (data?.process ? [data.process] : []);
  } catch {
    processList.value = [];
  } finally {
    if (!silent) processLoading.value = false;
    syncProcTimer();
  }
}

function uptimeMin(p) {
  return p?.uptime ? Math.floor(p.uptime / 60) : 0;
}

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

// ── 进程日志查看弹窗（复用 GET /api/tasks/{id}/logs tail 端点） ──

const procLogDialog = ref(false);
const procLogText = ref('');
const procLogLoading = ref(false);

async function viewProcessLog(p) {
  procLogDialog.value = true;
  procLogText.value = '';
  // 回收行已标记输出文件被删：直接提示，不再请求
  if (p?.output_file_deleted) {
    procLogText.value = t.process.logDeleted;
    return;
  }
  procLogLoading.value = true;
  try {
    const data = await request(`/api/tasks/${taskId}/logs?lines=200`);
    procLogText.value = typeof data?.logs === 'string' && data.logs ? data.logs : t.logs.empty;
  } catch {
    // 404 等情况：日志文件已被清理
    procLogText.value = t.process.logDeleted;
  } finally {
    procLogLoading.value = false;
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

// ── 交付物（routes_sandbox：检查点 files_dir 与 outputs/task_<id>/ 合并） ──

const artifacts = ref([]);

async function loadArtifacts() {
  try {
    const data = await request(`/api/tasks/${taskId}/artifacts`);
    artifacts.value = Array.isArray(data?.files) ? data.files : [];
  } catch {
    artifacts.value = [];
  }
}

function fmtSize(bytes) {
  if (bytes === null || bytes === undefined) return '';
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
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

// ▶ 继续：弹出附加指令输入框（可留空），确认后走 POST /api/tasks/{id}/resume
async function resumeTask() {
  let extra = '';
  try {
    const r = await ElMessageBox.prompt(t.actions.resumePromptText, t.actions.resumePromptTitle, {
      confirmButtonText: t.actions.resume,
      cancelButtonText: zh.goals.cancel,
      inputPlaceholder: t.actions.resumePromptPlaceholder,
    });
    extra = (r?.value || '').trim();
  } catch {
    return;
  }
  try {
    const resp = await request(`/api/tasks/${taskId}/resume`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(extra ? { extra_instruction: extra } : {}),
    });
    ElMessage.success(resp?.message || t.actions.resumeSuccess);
    loadTask({ silent: true });
    loadProcess({ silent: true });
  } catch (err) {
    ElMessage.error(`${t.actions.resumeFailed}: ${err.message}`);
  }
}

// 删除任务：若该任务有交付物（outputs/task_<id>/），确认框提供
// 「同时删除交付物目录」勾选（默认不勾）——沙箱治理二期 delete_artifacts 联动。
const deleteArtifacts = ref(false);

async function deleteTask() {
  let hasArtifacts = artifacts.value.length > 0;
  if (!hasArtifacts) {
    try {
      const art = await request(`/api/tasks/${taskId}/artifacts`);
      hasArtifacts = Array.isArray(art?.files) && art.files.length > 0;
    } catch {
      hasArtifacts = false; // 查询失败不阻断删除，按无交付物处理
    }
  }
  deleteArtifacts.value = false;
  const message = hasArtifacts
    ? h('div', null, [
        h('p', { style: 'margin: 0 0 8px;' }, t.actions.deleteConfirmText),
        h('label', { style: 'display: flex; align-items: center; gap: 6px; cursor: pointer;' }, [
          h('input', {
            type: 'checkbox',
            onChange: (ev) => { deleteArtifacts.value = ev.target.checked; },
          }),
          h('span', null, t.actions.deleteArtifactsLabel),
        ]),
      ])
    : t.actions.deleteConfirmText;
  try {
    await ElMessageBox.confirm(message, t.actions.deleteConfirmTitle, {
      confirmButtonText: t.actions.delete,
      cancelButtonText: zh.goals.cancel,
      type: 'warning',
    });
  } catch {
    return;
  }
  try {
    const qs = hasArtifacts && deleteArtifacts.value ? '?delete_artifacts=true' : '';
    const resp = await request(`/api/tasks/${taskId}${qs}`, { method: 'DELETE' });
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
  loadArtifacts();
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
          <el-button v-if="canResume" size="small" type="primary" plain @click="resumeTask">
            ▶ {{ t.actions.resume }}
          </el-button>
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

        <div v-if="showInterruptReason" class="section">
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

        <!-- 交付物（沙箱分区 outputs/task_<id>/ + 检查点 files_dir；空则不显示） -->
        <div v-if="artifacts.length" class="section">
          <div class="section-title">{{ t.artifacts.title }}</div>
          <div class="section-block artifact-block">
            <div v-for="(f, i) in artifacts" :key="i" class="artifact-row">
              <span class="artifact-name" :title="f.name">📄 {{ f.name }}</span>
              <span class="artifact-meta">{{ fmtSize(f.size) }}</span>
              <span class="artifact-meta">{{ f.mtime }}</span>
            </div>
          </div>
        </div>

        <!-- 进程信息（存在后台进程/回收行时显示；一任务可多进程，逐行渲染） -->
        <div v-if="processList.length" class="section">
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
          <div v-for="p in processList" :key="p.pid" class="section-block process-block">
            <div class="process-row">
              <span class="process-status" :class="{ ended: !p.alive }">
                <span class="dot" :class="{ alive: p.alive }"></span>
                {{ p.alive ? t.process.alive : t.process.dead }}
              </span>
              <span>PID: {{ p.pid }}</span>
              <span>{{ t.process.uptimePrefix }}{{ uptimeMin(p) }}{{ t.process.uptimeSuffix }}</span>
              <code class="process-cmd" :title="p.command">{{ p.command }}</code>
              <el-button
                v-if="p.output_file || p.output_file_deleted"
                size="small"
                type="primary"
                plain
                @click="viewProcessLog(p)"
              >
                📄 {{ t.process.viewLog }}
              </el-button>
              <el-button v-if="p.alive" size="small" type="danger" plain @click="killProcess">
                ⏹ {{ t.process.kill }}
              </el-button>
            </div>
            <div v-if="p.output_file" class="process-output">
              {{ t.process.outputFile }}: <code>{{ p.output_file }}</code>
            </div>
            <div v-else-if="p.output_file_deleted" class="process-output">
              {{ t.process.outputFileDeleted }}
            </div>
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

    <!-- 进程日志弹窗（进程区块「查看日志」） -->
    <el-dialog v-model="procLogDialog" :title="t.process.logTitle" width="640px">
      <pre class="dialog-pre tall" v-loading="procLogLoading">{{ procLogText }}</pre>
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

.artifact-block {
  padding: 6px 12px;
}

.artifact-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 4px 0;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.artifact-row:last-child {
  border-bottom: none;
}

.artifact-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.artifact-meta {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  flex-shrink: 0;
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

/* 已结束进程：整体灰色徽标语义（圆点默认灰，文字同步弱化） */
.process-status.ended {
  color: var(--el-text-color-placeholder);
}

/* 一任务多进程：行与行之间留间隔 */
.process-block + .process-block {
  margin-top: 8px;
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
