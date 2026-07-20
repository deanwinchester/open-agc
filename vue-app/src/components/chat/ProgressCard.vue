<script setup>
// Agent 执行进度卡片：实时（live）与历史（history）两种模式。
// 条目类型：tool（工具步骤）/ thinking（思考过程）/ response（LLM 中间回复）/
// note（模型切换等提示）/ ask（Agent 提问）。shellLines 为实时 shell 输出。
import { computed } from 'vue';
import zh from '../../i18n/zh';
import AskUserForm from './AskUserForm.vue';

const t = zh.chat;

const props = defineProps({
  card: { type: Object, required: true },
});
const emit = defineEmits(['resume', 'submit-ask']);

const stepCount = computed(() => props.card.entries.filter((e) => e.kind === 'tool').length);

const currentStepLabel = computed(() => {
  if (!props.card.live) return '';
  for (let i = props.card.entries.length - 1; i >= 0; i--) {
    const e = props.card.entries[i];
    if (e.kind === 'tool' && e.status === 'running') return e.toolLabel;
  }
  return '';
});

const title = computed(() => {
  const n = stepCount.value;
  if (props.card.live) return `🐼 ${t.working} · ${n}${t.stepsSuffix}`;
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
        <span v-if="currentStepLabel" class="pc-current"> : {{ currentStepLabel }}</span>
      </div>
      <div class="pc-right">
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

    <div v-show="!card.collapsed" class="pc-steps">
      <template v-for="(entry, idx) in card.entries" :key="idx">
        <div v-if="entry.kind === 'tool'" class="pc-step" :class="entry.status">
          <span class="step-icon">{{ stepIcon(entry) }}</span>
          <div class="step-body">
            <span class="step-label">{{ entry.step }}. {{ entry.toolLabel }}</span>
            <span v-if="entry.argsPreview" class="step-detail">{{ entry.argsPreview }}</span>
            <span v-if="entry.resultPreview" class="step-detail">{{ entry.resultPreview }}</span>
          </div>
        </div>

        <div v-else-if="entry.kind === 'thinking'" class="pc-step thinking">
          <span class="step-icon">🧠</span>
          <div class="step-body">
            <details>
              <summary class="step-label">{{ t.thinkingProcess }}</summary>
              <div class="step-detail thinking-content">{{ entry.content }}</div>
            </details>
          </div>
        </div>

        <div v-else-if="entry.kind === 'response'" class="pc-step">
          <span class="step-icon">💬</span>
          <div class="step-body">
            <span class="step-label">{{ t.llmResponse }}</span>
            <span class="step-detail">{{ entry.content }}</span>
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
    </div>
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
