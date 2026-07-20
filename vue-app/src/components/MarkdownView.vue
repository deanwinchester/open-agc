<script setup>
// Markdown 渲染组件：marked 解析 + DOMPurify 消毒。
import { computed } from 'vue';
import { marked } from 'marked';
import DOMPurify from 'dompurify';

const props = defineProps({
  content: { type: String, default: '' },
});

marked.setOptions({ breaks: true, gfm: true });

const html = computed(() => DOMPurify.sanitize(marked.parse(props.content || '')));
</script>

<template>
  <!-- 内容已经过 DOMPurify 消毒，可安全使用 v-html -->
  <div class="markdown-view" v-html="html"></div>
</template>

<style scoped>
/* v-html 内容不带 scoped 属性，需用 :deep() 命中 */
.markdown-view {
  line-height: 1.7;
  font-size: 14px;
  word-break: break-word;
}

.markdown-view :deep(p) {
  margin: 0 0 10px;
}

/* 消息中的图片不得超出聊天窗口（v-html 内容需 :deep 命中） */
.markdown-view :deep(img) {
  max-width: 100%;
  height: auto;
  border-radius: 8px;
}

.markdown-view :deep(p:last-child) {
  margin-bottom: 0;
}

.markdown-view :deep(pre) {
  background: var(--panda-code-bg);
  border-radius: 8px;
  padding: 12px;
  overflow-x: auto;
}

.markdown-view :deep(code) {
  font-family: 'Consolas', 'Monaco', monospace;
  font-size: 13px;
}

.markdown-view :deep(:not(pre) > code) {
  background: var(--panda-code-bg);
  border-radius: 4px;
  padding: 1px 5px;
}

.markdown-view :deep(a) {
  color: var(--el-color-primary);
}

.markdown-view :deep(table) {
  border-collapse: collapse;
  margin: 10px 0;
}

.markdown-view :deep(th),
.markdown-view :deep(td) {
  border: 1px solid var(--panda-code-border);
  padding: 6px 10px;
}
</style>
