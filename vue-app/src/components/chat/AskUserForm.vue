<script setup>
// Agent 提问内联表单：有 options 时渲染选项按钮，否则输入框 + 提交。
// 前台提问经 WS tool_reply 回复，后台任务提问经 REST /api/tasks/{id}/reply 回复
//（由父组件按 entry.background 区分，见 ChatView.onSubmitAsk）。
import { ref } from 'vue';
import zh from '../../i18n/zh';

const t = zh.chat;

const props = defineProps({
  entry: { type: Object, required: true }, // { question, options?, answered, answer?, error? }
});
const emit = defineEmits(['submit']);

const input = ref('');

function choose(opt) {
  if (props.entry.answered) return;
  emit('submit', props.entry, opt);
}

function submit() {
  const v = input.value.trim();
  if (!v || props.entry.answered) return;
  emit('submit', props.entry, v);
  input.value = '';
}
</script>

<template>
  <div class="ask-user">
    <div class="ask-label">❓ {{ t.askUser }}</div>
    <div class="ask-question">{{ entry.question }}</div>

    <div v-if="entry.answered" class="ask-answered">
      {{ t.askAnswered }}：<strong>{{ entry.answer }}</strong>
    </div>
    <div v-else-if="entry.error" class="ask-error">✗ {{ entry.error }}</div>

    <template v-if="!entry.answered">
      <div v-if="entry.options && entry.options.length" class="ask-options">
        <el-button
          v-for="opt in entry.options"
          :key="opt"
          size="small"
          @click="choose(opt)"
        >{{ opt }}</el-button>
      </div>
      <div v-else class="ask-form">
        <el-input
          v-model="input"
          size="small"
          :placeholder="t.askPlaceholder"
          @keydown.enter.prevent="submit"
        />
        <el-button size="small" type="primary" @click="submit">{{ t.askSubmit }}</el-button>
      </div>
    </template>
  </div>
</template>

<style scoped>
.ask-user {
  border: 1px solid var(--el-color-primary-light-7);
  border-radius: var(--panda-radius-card);
  padding: 12px 14px;
  background: var(--el-color-primary-light-9);
}

.ask-label {
  color: var(--el-color-primary);
  font-weight: 600;
  font-size: 13px;
  margin-bottom: 6px;
}

.ask-question {
  font-size: 14px;
  margin-bottom: 10px;
  white-space: pre-wrap;
  word-break: break-word;
}

.ask-options {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.ask-options .el-button {
  margin-left: 0;
}

.ask-form {
  display: flex;
  gap: 8px;
}

.ask-answered {
  color: var(--el-color-success);
  font-size: 13px;
}

.ask-error {
  color: var(--el-color-error);
  font-size: 13px;
  margin-bottom: 6px;
}

@media (max-width: 768px) {
  /* 输入 + 按钮纵向堆叠，避免挤压 */
  .ask-form {
    flex-direction: column;
  }
}
</style>
