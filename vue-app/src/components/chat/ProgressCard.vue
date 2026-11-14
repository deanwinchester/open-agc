<script setup>
// Agent 执行进度卡片：实时（live）与历史（history）两种模式。
// 条目类型：tool（工具步骤）/ thinking（思考过程）/ response（LLM 中间回复）/
// note（模型切换等提示）/ ask（Agent 提问）。shellLines 为实时 shell 输出。
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import zh from '../../i18n/zh';
import { request } from '../../api/client';
import { personaEmoji } from '../../stores/theme';
import AskUserForm from './AskUserForm.vue';
import MarkdownView from '../MarkdownView.vue';

const t = zh.chat;

// LLM 中间回复的折叠预览：去 markdown 标记取前 60 字（全文在展开区
// 用 MarkdownView 渲染——此前纯文本平铺，长回复完全无法读，用户反馈）
function respPreview(content) {
  const plain = String(content || '')
    .replace(/[#*`>\-\[\]()!]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
  return plain.length > 60 ? plain.slice(0, 60) + '…' : plain;
}

function hasThinkingContent(entry) {
  return !entry.pending && !!String(entry.content || '').trim();
}

const props = defineProps({
  card: { type: Object, required: true },
});
const emit = defineEmits(['resume', 'submit-ask']);

// ── 动作的友好文案（用户反馈：不要 archive_es_es_search 这类英文内部名）──
// shell→执行命令、python→执行脚本、MCP→使用<展示名>（配置 mcp_servers 的
// display_name，如「历史语料库」；模块级缓存，多卡片共享一次拉取）。
let _mcpLabelCache = null;
const mcpLabels = ref({});
async function loadMcpLabels() {
  if (_mcpLabelCache) { mcpLabels.value = _mcpLabelCache; return; }
  try {
    const data = await request('/api/mcp/servers');
    const map = {};
    for (const s of (data?.servers || [])) map[s.name] = s.display_name || s.name;
    _mcpLabelCache = map;
    mcpLabels.value = map;
  } catch { /* 无则保持原名 */ }
}
loadMcpLabels();

function friendlyAction(entry) {
  const tool = entry.tool || '';
  if (tool === 'execute_shell') return '执行命令';
  if (tool === 'execute_python') return '执行脚本';
  const srv = Object.keys(mcpLabels.value).find((s) => tool.startsWith(s + '_'));
  if (srv) return `使用${mcpLabels.value[srv]}`;
  return entry.toolLabel || tool;
}

// 最近一次工具动作（未展开时也显示在头部）
const lastActionText = computed(() => {
  for (let i = props.card.entries.length - 1; i >= 0; i--) {
    const e = props.card.entries[i];
    if (e.kind === 'tool') {
      const a = friendlyAction(e);
      return e.status === 'running' ? `${t.pcRunningPrefix}${a}` : `${t.pcLastPrefix}${a}`;
    }
  }
  return '';
});

// 收起时保留最近一条思考/助手回复的上下文。流式内容原地更新，预览也会
// 随之刷新；跳过空条目，避免工具完成后预览突然消失。
const collapsedPreview = computed(() => {
  for (let i = props.card.entries.length - 1; i >= 0; i--) {
    const entry = props.card.entries[i];
    if (entry.kind !== 'thinking' && entry.kind !== 'response') continue;
    const content = String(entry.content || '')
      .replace(/!?\[([^\]]*)\]\([^)]*\)/g, '$1')
      .replace(/[*_`#>~]/g, '')
      .replace(/^\s*[-+]\s+/gm, '')
      .replace(/\s+/g, ' ')
      .trim();
    if (content) {
      return {
        content,
        label: entry.kind === 'thinking' ? '🧠' : '💬',
        title: entry.kind === 'thinking' ? t.thinkingProcess : t.llmResponse,
      };
    }
  }
  return null;
});

// 执行耗时：仅运行中的卡片启动秒级时钟，完成后冻结在 endedAt，避免每张
// 历史卡都常驻一个定时器。
const clock = ref(Date.now());
let elapsedTimer = null;
function syncElapsedTimer(live) {
  if (elapsedTimer) {
    clearInterval(elapsedTimer);
    elapsedTimer = null;
  }
  clock.value = Date.now();
  if (live) elapsedTimer = setInterval(() => { clock.value = Date.now(); }, 1000);
}
watch(() => props.card.live, syncElapsedTimer, { immediate: true });
onBeforeUnmount(() => {
  if (elapsedTimer) clearInterval(elapsedTimer);
});

const elapsedText = computed(() => {
  const start = Number(props.card.startedAt || 0);
  if (!start) return '';
  const end = props.card.live ? clock.value : Number(props.card.endedAt || clock.value);
  const total = Math.max(0, Math.floor((end - start) / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  if (hours) {
    return `${hours}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;
  }
  return `${Math.floor(total / 60)}:${String(seconds).padStart(2, '0')}`;
});

// 步骤详情抽屉：行内只显示概览，点击步骤在右侧查看完整结果
const detailEntry = ref(null);
const detailVisible = computed({
  get: () => detailEntry.value !== null,
  set: (v) => { if (!v) detailEntry.value = null; },
});
async function openDetail(entry) {
  if (entry.fullResult) { detailEntry.value = entry; return; }
  // 后端瘦身后的历史步骤不再带 full_result（WS 大帧会冲垮 UOS 旧
  // libsoup2 的网络进程）——有 has_full 标记的按需走 REST 拉全量
  if (entry.hasFull && props.card.taskId && entry.step != null) {
    try {
      const data = await request(`/api/tasks/${props.card.taskId}/steps?page=1&page_size=200`);
      const row = (data?.steps || []).find((s) => s.step_number === entry.step);
      if (row && (row.full_result || row.full_args)) {
        entry.fullResult = row.full_result || row.full_args;
        detailEntry.value = entry;
      }
    } catch { /* 拉取失败则不展开 */ }
  }
}

const stepCount = computed(() => props.card.entries.filter((e) => e.kind === 'tool').length);

const currentStepLabel = computed(() => {
  if (!props.card.live) return '';
  for (let i = props.card.entries.length - 1; i >= 0; i--) {
    const e = props.card.entries[i];
    if (e.kind === 'tool' && e.status === 'running') return `${t.pcRunningPrefix}${friendlyAction(e)}`;
  }
  return '';
});

const title = computed(() => {
  const n = stepCount.value;
  if (props.card.live) { const pe = personaEmoji(); return `${pe ? pe + ' ' : ''}${t.working} · ${n}${t.stepsSuffix}`; }
  if (props.card.history) return `⚡ ${t.lastRun} · ${n}${t.stepsSuffix}`;
  return `✨ ${t.done} · ${n}${t.stepsSuffix}`;
});

function stepIcon(entry) {
  if (entry.status === 'running') return '⏳';
  return entry.status === 'done' ? '✅' : '❌';
}

function toggle() {
  props.card.collapsed = !props.card.collapsed;
}

function onResume() {
  emit('resume', props.card.taskId);
}

function onSubmitAsk(entry, answer) {
  emit('submit-ask', entry, answer);
}
</script>

<template>
  <div class="progress-card" :class="{ live: card.live }">
    <div class="pc-header" @click="toggle">
      <div class="pc-left">
        <span v-if="card.live" class="pc-spinner"></span>
        <span class="pc-title">{{ title }}</span>
        <span v-if="currentStepLabel && !card.collapsed" class="pc-current"> : {{ currentStepLabel }}</span>
        <span v-if="card.collapsed && lastActionText" class="pc-current"> · {{ lastActionText }}</span>
      </div>
      <div class="pc-right">
        <span v-if="elapsedText" class="pc-elapsed">
          ⏱ {{ card.live ? t.executingFor : t.elapsed }} {{ elapsedText }}
        </span>
        <span v-if="card.tokenUsage" class="pc-usage">{{ card.tokenUsage }}</span>
        <el-button
          v-if="card.resumable && !card.live"
          size="small"
          type="primary"
          plain
          class="pc-resume"
          @click.stop="onResume"
        >{{ t.resume }}</el-button>
        <span class="pc-toggle">{{ card.collapsed ? '▸' : '▾' }}</span>
      </div>
    </div>

    <div
      v-if="card.collapsed && collapsedPreview"
      class="pc-collapsed-preview"
      :title="t.expandSteps"
      @click="toggle"
    >
      <div class="pc-preview-clamp">
        <span class="pc-preview-label" :title="collapsedPreview.title">{{ collapsedPreview.label }}</span>
        {{ collapsedPreview.content }}
      </div>
    </div>

    <div v-show="!card.collapsed" class="pc-steps">
      <template v-for="(entry, idx) in card.entries" :key="idx">
        <div v-if="entry.kind === 'tool'" class="pc-step" :class="[entry.status, { clickable: entry.fullResult || entry.hasFull }]" @click.stop="openDetail(entry)">
          <span class="step-icon">{{ stepIcon(entry) }}</span>
          <div class="step-body">
            <span class="step-label">
              <span v-if="entry.subTask" class="subtask-badge" :title="entry.subTask">🧩 {{ entry.subTask.substring(0, 18) }}</span>{{ entry.step }}. {{ friendlyAction(entry) }}<span v-if="entry.fullResult || entry.hasFull" class="step-more"> 🔍</span>
            </span>
          </div>
        </div>

        <div v-else-if="entry.kind === 'thinking' && hasThinkingContent(entry)" class="pc-step thinking">
          <span class="step-icon" :title="t.thinkingProcess">🧠</span>
          <div class="step-body">
            <details>
              <summary class="step-label" :aria-label="t.thinkingProcess"></summary>
              <div class="step-detail thinking-content">{{ entry.content }}</div>
            </details>
          </div>
        </div>

        <div v-else-if="entry.kind === 'response'" class="pc-step response">
          <span class="step-icon" :title="t.llmResponse">💬</span>
          <div class="step-body">
            <details>
              <summary class="step-label" :aria-label="t.llmResponse">
                <span class="resp-preview">{{ respPreview(entry.content) }}</span>
              </summary>
              <MarkdownView :content="entry.content" class="resp-md" />
            </details>
          </div>
        </div>

        <div v-else-if="entry.kind === 'note'" class="pc-step">
          <span class="step-icon">🔄</span>
          <div class="step-body">
            <span class="step-label">{{ entry.text }}</span>
          </div>
        </div>

        <AskUserForm
          v-else-if="entry.kind === 'ask'"
          :entry="entry"
          @submit="onSubmitAsk"
        />
      </template>

      <div v-if="card.shellLines && card.shellLines.length" class="shell-box">
        <div class="shell-title">{{ t.shellOutput }}</div>
        <div v-for="(line, i) in card.shellLines" :key="i" class="shell-line">{{ line }}</div>
      </div>

      <!-- 底部收起栏：长卡片展开后无需滚回顶部即可收起 -->
      <div class="pc-collapse-bottom" @click.stop="toggle">{{ t.collapseSteps }}</div>
    </div>

    <!-- 步骤详情抽屉：点击步骤查看完整结果 -->
    <el-drawer
      v-model="detailVisible"
      direction="rtl"
      size="480px"
      :title="detailEntry ? `${detailEntry.step}. ${friendlyAction(detailEntry)}` : ''"
      append-to-body
    >
      <div v-if="detailEntry" class="step-full-detail">
        <div v-if="detailEntry.argsPreview" class="sfd-block">
          <div class="sfd-title">参数</div>
          <pre class="sfd-pre">{{ detailEntry.argsPreview }}</pre>
        </div>
        <div class="sfd-block">
          <div class="sfd-title">完整结果</div>
          <pre class="sfd-pre">{{ detailEntry.fullResult }}</pre>
        </div>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
/* 进度卡片：左侧竹绿边条标识，live 态边条加深 + 微光 */
.progress-card {
  margin-bottom: 16px;
  border: 1px solid var(--el-border-color-light);
  border-left: 3px solid var(--el-color-primary-light-3);
  border-radius: var(--panda-radius-card);
  background: var(--el-bg-color);
  box-shadow: var(--panda-shadow-card);
  overflow: hidden;
  transition: border-color var(--panda-transition), box-shadow var(--panda-transition);
}

.progress-card.live {
  border-color: var(--el-color-primary-light-5);
  border-left-color: var(--el-color-primary);
  box-shadow: 0 0 0 1px var(--el-color-primary-light-8), var(--panda-shadow-card);
}

.pc-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  cursor: pointer;
  user-select: none;
  background: var(--el-fill-color-light);
  transition: background-color var(--panda-transition);
}

.pc-header:hover {
  background: var(--el-color-primary-light-9);
}

.pc-left {
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.pc-title {
  font-size: 13px;
  font-weight: 600;
}

.pc-current {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pc-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
}

.pc-usage {
  font-size: 11px;
  color: var(--el-text-color-secondary);
}

.pc-elapsed {
  flex-shrink: 0;
  font-size: 11px;
  color: var(--el-text-color-secondary);
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}

.pc-collapsed-preview {
  padding: 9px 14px 11px;
  border-top: 1px solid var(--el-border-color-lighter);
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 20px;
  word-break: break-word;
  cursor: pointer;
  transition: background-color var(--panda-transition);
}

/* 裁切必须放在无 padding 的内层：否则文字会绘制进外层的下 padding，
   即使外层限制四行高度，仍可能露出第五行的上半截。 */
.pc-preview-clamp {
  max-height: 80px;
  overflow: hidden;
}

.pc-collapsed-preview:hover {
  background: var(--el-color-primary-light-9);
}

.pc-preview-label {
  margin-right: 7px;
  color: var(--el-text-color-regular);
  font-weight: 600;
}

.pc-toggle {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  transition: transform var(--panda-transition);
}

.pc-spinner {
  width: 12px;
  height: 12px;
  border: 2px solid var(--el-color-primary-light-5);
  border-top-color: var(--el-color-primary);
  border-radius: 50%;
  animation: pc-spin 0.8s linear infinite;
  flex-shrink: 0;
}

@keyframes pc-spin {
  to { transform: rotate(360deg); }
}

.pc-steps {
  padding: 10px 14px;
  border-top: 1px solid var(--el-border-color-lighter);
  /* v-show 展开时播放一次轻量淡入上滑 */
  animation: pc-expand 0.18s ease;
}

.pc-collapse-bottom {
  margin-top: 8px;
  padding: 7px 0 2px;
  border-top: 1px dashed var(--el-border-color-lighter);
  text-align: center;
  font-size: 12px;
  color: var(--el-text-color-secondary);
  cursor: pointer;
  user-select: none;
  border-radius: 0 0 6px 6px;
  transition: color var(--panda-transition), background var(--panda-transition);
}

.pc-collapse-bottom:hover {
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
}

@keyframes pc-expand {
  from { opacity: 0; transform: translateY(-4px); }
  to { opacity: 1; transform: translateY(0); }
}

.pc-step {
  display: flex;
  gap: 8px;
  padding: 4px 0;
  font-size: 13px;
}

.pc-step.clickable {
  cursor: pointer;
  border-radius: 6px;
  padding: 4px 6px;
  margin: 0 -6px;
  transition: background var(--panda-transition);
}

/* 子任务徽标：紫色区分主代理步骤 */
.subtask-badge {
  display: inline-block;
  margin-right: 6px;
  padding: 0 7px;
  border-radius: 999px;
  font-size: 11px;
  line-height: 17px;
  color: #7c3aed;
  background: rgba(124, 58, 237, 0.1);
  border: 1px solid rgba(124, 58, 237, 0.25);
  vertical-align: 1px;
}

.pc-step.clickable:hover {
  background: var(--el-color-primary-light-9);
}

.step-more {
  opacity: 0.55;
  font-size: 12px;
}

.step-full-detail .sfd-block {
  margin-bottom: 16px;
}

.sfd-title {
  font-size: 12px;
  font-weight: 600;
  color: var(--el-text-color-secondary);
  margin-bottom: 6px;
}

.sfd-pre {
  margin: 0;
  padding: 10px 12px;
  background: var(--el-fill-color-light);
  border-radius: 8px;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 60vh;
  overflow-y: auto;
  font-family: var(--panda-font-mono, monospace);
}

.step-icon {
  flex-shrink: 0;
}

.step-body {
  display: flex;
  flex-direction: column;
  min-width: 0;
  flex: 1;
}

.step-label {
  font-weight: 600;
}

.step-detail {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  word-break: break-word;
  white-space: pre-wrap;
}

.thinking-content {
  max-height: 200px;
  overflow-y: auto;
}

.resp-preview {
  margin-left: 0;
  font-weight: normal;
  color: var(--el-text-color-placeholder);
  font-size: 12px;
}

.resp-md {
  margin-top: 6px;
  padding: 8px 10px;
  background: var(--el-fill-color-light);
  border-radius: 8px;
  font-size: 13px;
}

.pc-steps :deep(.ask-user) {
  margin: 4px 0;
}

.shell-box {
  margin-top: 6px;
  background: var(--panda-shell-bg);
  color: var(--panda-shell-text);
  border-radius: 8px;
  padding: 10px 12px;
  max-height: 200px;
  overflow-y: auto;
  font-family: Consolas, Monaco, monospace;
  font-size: 12px;
}

.shell-title {
  color: var(--panda-shell-dim);
  font-size: 11px;
  margin-bottom: 4px;
}

.shell-line {
  white-space: pre-wrap;
  word-break: break-all;
}

@media (max-width: 768px) {
  /* 头部控件允许换行，token 用量掉到第二行 */
  .pc-header {
    flex-wrap: wrap;
    row-gap: 6px;
  }

  .pc-right {
    flex-wrap: wrap;
    justify-content: flex-end;
  }
}
</style>
