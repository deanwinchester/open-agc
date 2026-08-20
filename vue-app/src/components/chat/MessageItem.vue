<script setup>
// 单条聊天消息：user 纯文本气泡；agent/system 走 MarkdownView（DOMPurify 消毒）。
// system 渲染为居中通知；带 taskId 的消息附任务链接。
// 用户气泡可携带图片缩略图（dataURL 数组，flex 排列）与附件 chips
//（文件名+大小、下载链接 /api/upload/{name}）——对齐旧 static/app.js appendMessage。
// 气泡下方显示消息时间（DB UTC 时间戳或本地 ISO）；有 DB id 的消息悬停出现删除按钮。
// agent 消息（有 DB id）附 👍/👎 反馈按钮（M3 好评率采集，/api/feedback）。
import { computed, ref, watch } from 'vue';
import MarkdownView from '../MarkdownView.vue';
import { request } from '../../api/client';
import zh from '../../i18n/zh';

const t = zh.chat;

const props = defineProps({
  // { role: 'user'|'agent'|'system', content, taskId?, id?, timestamp?, feedback?: 0|1|-1, images?: string[]|null, files?: {name,path,size}[]|null }
  item: { type: Object, required: true },
});

const emit = defineEmits(['delete']);

// 反馈状态（-1/0/1）；历史消息由 /api/history 带入，实时消息默认 0
const feedback = ref(props.item.feedback || 0);
watch(() => props.item.feedback, (v) => { feedback.value = v || 0; });
const feedbackBusy = ref(false);

const canFeedback = computed(() => props.item.role === 'agent' && !!props.item.id);

async function onFeedback(score) {
  if (feedbackBusy.value) return;
  const next = feedback.value === score ? 0 : score; // 再点一次撤销
  feedbackBusy.value = true;
  try {
    await request('/api/feedback', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message_id: props.item.id, score: next }),
    });
    feedback.value = next;
  } catch {
    // 失败静默：保持原状态（非关键路径）
  } finally {
    feedbackBusy.value = false;
  }
}

// DB timestamp 是 UTC 'YYYY-MM-DD HH:MM:SS'（sqlite CURRENT_TIMESTAMP）；
// 实时消息是本地 ISO 串。统一转本地显示：今天 HH:mm，其余 MM-DD HH:mm。
const timeText = computed(() => {
  const raw = props.item.timestamp;
  if (!raw) return '';
  const isIso = raw.includes('T');
  const d = new Date(isIso ? raw : raw.replace(' ', 'T') + 'Z');
  if (Number.isNaN(d.getTime())) return '';
  const now = new Date();
  const sameDay = d.toDateString() === now.toDateString();
  const pad = (n) => String(n).padStart(2, '0');
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return sameDay ? hm : `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`;
});

const canDelete = computed(() => !!props.item.id);

function onDelete() {
  emit('delete', props.item);
}

// 后端 /static 挂载的图标；动态绑定避免 Vite 当作构建期资源解析（同 App.vue）。
// 头像跟随自定义 Logo（ui_theme.logo_file），未设置时回退默认熊猫图标。
import { themeState } from '../../stores/theme';

const avatarUrl = computed(() => themeState.logoUrl);

// 附件大小格式化阈值与旧 app.js:1574-1578 一致
function formatSize(size) {
  if (size > 1024 * 1024) return (size / (1024 * 1024)).toFixed(1) + ' MB';
  if (size > 1024) return (size / 1024).toFixed(1) + ' KB';
  return size + ' B';
}

function dlHref(name) {
  return '/api/upload/' + encodeURIComponent(name);
}
</script>

<template>
  <div v-if="item.role === 'system'" class="msg-row system">
    <div class="msg-system-inner">
      <MarkdownView :content="item.content" />
      <router-link v-if="item.taskId" class="task-link" :to="`/tasks/${item.taskId}`">#{{ item.taskId }}</router-link>
      <div v-if="timeText" class="msg-time">{{ timeText }}</div>
    </div>
  </div>

  <div v-else-if="item.role === 'user'" class="msg-row user">
    <div class="msg-col">
      <div class="msg-bubble user">
        <button v-if="canDelete" class="msg-del" :title="t.deleteMessage" @click="onDelete">×</button>
        <div v-if="item.images && item.images.length" class="msg-images">
          <img v-for="(url, i) in item.images" :key="i" :src="url" class="msg-thumb" alt="" />
        </div>
        <div v-if="item.files && item.files.length" class="msg-attach">
          <div v-for="(f, i) in item.files" :key="i" class="attach-chip">
            <span class="attach-chip-name" :title="`${f.name} (${formatSize(f.size)})`">{{ f.name }}</span>
            <a class="attach-chip-dl" :href="dlHref(f.name)" :title="t.download" download>
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
            </a>
          </div>
        </div>
        <div>{{ item.content }}</div>
      </div>
      <div v-if="timeText" class="msg-time user">{{ timeText }}</div>
    </div>
  </div>

  <div v-else class="msg-row agent">
    <img class="msg-avatar" :src="avatarUrl" alt="Panda" />
    <div class="msg-col">
      <div class="msg-bubble agent">
        <button v-if="canDelete" class="msg-del" :title="t.deleteMessage" @click="onDelete">×</button>
        <MarkdownView :content="item.content" />
        <router-link v-if="item.taskId" class="task-link" :to="`/tasks/${item.taskId}`">#{{ item.taskId }}</router-link>
      </div>
      <div class="msg-foot">
        <div v-if="timeText" class="msg-time">{{ timeText }}</div>
        <div v-if="canFeedback" class="msg-feedback">
          <button
            class="fb-btn" :class="{ active: feedback === 1 }"
            :title="t.feedbackGood" :disabled="feedbackBusy"
            @click="onFeedback(1)"
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M7 10v12"/><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z"/></svg>
          </button>
          <button
            class="fb-btn" :class="{ active: feedback === -1 }"
            :title="t.feedbackBad" :disabled="feedbackBusy"
            @click="onFeedback(-1)"
          >
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="transform: rotate(180deg)"><path d="M7 10v12"/><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2a3.13 3.13 0 0 1 3 3.88Z"/></svg>
          </button>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.msg-row {
  display: flex;
  margin-bottom: 16px;
}

.msg-col {
  display: flex;
  flex-direction: column;
  width: fit-content;   /* 短消息按内容收缩，长消息受 max-width 约束 */
  max-width: 85%;
}

.msg-time {
  margin-top: 4px;
  font-size: 11px;
  color: var(--el-text-color-placeholder);
}

.msg-time.user {
  text-align: right;
}

.msg-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 4px;
}

.msg-foot .msg-time {
  margin-top: 0;
}

.msg-feedback {
  display: flex;
  gap: 4px;
  opacity: 0;
  transition: opacity 0.15s;
}

.msg-row:hover .msg-feedback,
.msg-feedback:has(.fb-btn.active) {
  opacity: 1;
}

.fb-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  background: var(--el-bg-color);
  color: var(--el-text-color-placeholder);
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s, background 0.15s;
}

.fb-btn:hover {
  color: var(--el-color-primary);
  border-color: var(--el-color-primary-light-5);
}

.fb-btn.active {
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  border-color: var(--el-color-primary-light-5);
}

.fb-btn:disabled {
  cursor: default;
  opacity: 0.6;
}

.msg-bubble {
  position: relative;
}

.msg-del {
  position: absolute;
  top: -7px;
  right: -7px;
  width: 18px;
  height: 18px;
  border: none;
  border-radius: 50%;
  background: var(--el-fill-color-dark);
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1;
  cursor: pointer;
  opacity: 0;
  transition: opacity 0.15s;
  z-index: 2;
}

.msg-bubble:hover .msg-del {
  opacity: 1;
}

.msg-del:hover {
  background: var(--el-color-danger);
  color: #fff;
}

.msg-row.user {
  justify-content: flex-end;
}

.msg-row.system {
  justify-content: center;
}

.msg-system-inner {
  max-width: 85%;
  padding: 8px 16px;
  border-radius: var(--panda-radius-card);
  background: var(--el-fill-color-light);
  color: var(--el-text-color-secondary);
  font-size: 13px;
  text-align: center;
}

.msg-avatar {
  width: 32px;
  height: 32px;
  border-radius: 10px;
  margin-right: 10px;
  margin-top: 2px;
  flex-shrink: 0;
  box-shadow: var(--panda-avatar-ring);
}

.msg-bubble {
  max-width: 100%;   /* 宽度约束由外层 .msg-col 承担（85%/窄屏 92%） */
  padding: 10px 16px;
  font-size: 14px;
  line-height: 1.65;
  word-break: break-word;
}

/* 气泡圆角不对称：尾巴一侧小圆角 */
.msg-bubble.user {
  background: linear-gradient(
    135deg,
    var(--el-color-primary) 0%,
    var(--el-color-primary-dark-2) 130%
  );
  color: var(--panda-on-accent);
  border-radius: var(--panda-radius-lg) var(--panda-radius-lg) 4px var(--panda-radius-lg);
  box-shadow: var(--panda-bubble-user-shadow);
  white-space: pre-wrap;
}

.msg-bubble.agent {
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-light);
  border-radius: 4px var(--panda-radius-lg) var(--panda-radius-lg) var(--panda-radius-lg);
  box-shadow: var(--panda-shadow-card);
}

/* 气泡内图片（含未来用户附件 <img> 直渲）一律不超出气泡宽度 */
.msg-bubble :deep(img),
.msg-system-inner :deep(img) {
  max-width: 100%;
  height: auto;
  border-radius: 8px;
}

/* 用户消息图片缩略图：多图 flex 排列（对齐旧 appendMessage 的图片区） */
.msg-images {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 6px;
}

.msg-thumb {
  width: 96px;
  height: 96px;
  object-fit: cover;
  border-radius: 8px;
  max-width: 100%;
}

/* 用户消息附件 chips：叠加在强调色气泡上，配色走主题变量 */
.msg-attach {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 6px;
}

.msg-bubble.user .attach-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 240px;
  padding: 3px 8px;
  font-size: 12px;
  background: var(--panda-bubble-chip-bg);
  border: 1px solid var(--panda-bubble-chip-border);
  border-radius: 999px;
  color: var(--panda-on-accent);
}

.attach-chip-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.attach-chip-dl {
  display: inline-flex;
  color: var(--panda-on-accent);
  text-decoration: none;
  opacity: 0.85;
}

.attach-chip-dl:hover {
  opacity: 1;
}

.task-link {
  display: inline-block;
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-color-primary);
  text-decoration: none;
}

.task-link:hover {
  text-decoration: underline;
}

@media (max-width: 768px) {
  /* 窄屏气泡放宽，减少换行损耗 */
  .msg-col,
  .msg-system-inner {
    max-width: 92%;
  }

  .msg-avatar {
    width: 28px;
    height: 28px;
    margin-right: 8px;
  }
}
</style>
