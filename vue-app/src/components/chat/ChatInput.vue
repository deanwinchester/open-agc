<script setup>
// 聊天输入区：Enter 发送 / Shift+Enter 换行。
// Agent 运行中时发送按钮文案变为「追加」（服务端把消息作为插话注入当前任务，
// 见 api/ws.py 运行期接收分支 → agent.queue_message）；停止按钮发送 WS interrupt。
//
// 验收修复 C：补齐旧 static/app.js 的三个输入能力（行为与字符串格式以旧版为准）：
//   图片：按钮选图 / 剪贴板粘贴 → FileReader 读成 dataURL，最多 5 张，预览条可移除
//         （app.js:1497-1554）；发送时由 ChatView 放入 WS 消息 images 字段
//   文件：按钮选文件 / 拖拽到输入区 → XHR POST /api/upload 带进度（app.js:1600-1678）；
//         chips 显示文件名+大小、下载链接 /api/upload/{name}、移除时 DELETE 同名接口
//   语音：SpeechRecognition || webkitSpeechRecognition（不支持则隐藏按钮），
//         continuous/interimResults 均为 false，识别文本插入光标处（app.js:1733-1790）
// paste/drag 均走模板事件绑定，组件卸载时 Vue 自动移除，无遗留监听。
import { ref, computed, nextTick, onUnmounted } from 'vue';
import { ElMessage } from 'element-plus';
import zh from '../../i18n/zh';

const t = zh.chat;
// 与旧 app.js:1601 一致的单文件上限
const MAX_UPLOAD_BYTES = 500 * 1024 * 1024;
// 与旧 app.js:1498 一致的图片上限
const MAX_IMAGES = 5;

const props = defineProps({
  running: { type: Boolean, default: false },
  connected: { type: Boolean, default: false },
});
const emit = defineEmits(['send', 'stop']);

const text = ref('');
const pendingImages = ref([]); // dataURL 数组
const attachedFiles = ref([]); // 已上传完成 {name, path, size}
const uploading = ref([]); // 上传中 {id, name, pct}

// 移动端（≤768px）：图片/附件/语音按钮收进「＋」面板（参考微信输入区），
// 桌面端保持行内按钮。切回桌面时强制收起面板。
const isMobile = ref(window.matchMedia('(max-width: 768px)').matches);
const panelOpen = ref(false);
const _mobileMq = window.matchMedia('(max-width: 768px)');
function _onMobileMq(e) {
  isMobile.value = e.matches;
  if (!e.matches) panelOpen.value = false;
}
_mobileMq.addEventListener('change', _onMobileMq);
onUnmounted(() => _mobileMq.removeEventListener('change', _onMobileMq));

function togglePanel() { panelOpen.value = !panelOpen.value; }
// 面板动作：执行后自动收起（微信交互）
function withPanelClose(fn) {
  return () => { fn(); panelOpen.value = false; };
}

const canSend = computed(() => props.connected
  && (text.value.trim() || pendingImages.value.length || attachedFiles.value.length));

function submit() {
  const v = text.value.trim();
  if ((!v && pendingImages.value.length === 0 && attachedFiles.value.length === 0) || !props.connected) return;
  emit('send', {
    text: v,
    images: [...pendingImages.value],
    files: attachedFiles.value.map((f) => ({ ...f })),
  });
  text.value = '';
  pendingImages.value = [];
  attachedFiles.value = [];
}

function onKeydown(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    submit();
  }
}

// ── 图片：按钮 / 粘贴（对齐旧 app.js:1497-1554） ──
function addPendingImage(dataUrl) {
  if (pendingImages.value.length >= MAX_IMAGES) { ElMessage.warning(t.imageLimit); return; }
  pendingImages.value.push(dataUrl);
}

function removePendingImage(index) {
  pendingImages.value.splice(index, 1);
}

function readImageFiles(files) {
  for (const file of files) {
    if (!file.type.startsWith('image/')) continue;
    const reader = new FileReader();
    reader.onload = () => addPendingImage(reader.result);
    reader.readAsDataURL(file);
  }
}

const imageFileInput = ref(null);
function pickImage() { imageFileInput.value && imageFileInput.value.click(); }
function onImagePicked(e) {
  readImageFiles(e.target.files);
  e.target.value = '';
}

function onPaste(e) {
  const items = e.clipboardData && e.clipboardData.items;
  if (!items) return;
  for (const item of items) {
    if (item.type.startsWith('image/')) {
      e.preventDefault();
      const file = item.getAsFile();
      if (!file) continue;
      const reader = new FileReader();
      reader.onload = () => addPendingImage(reader.result);
      reader.readAsDataURL(file);
    }
  }
}

// ── 文件上传：按钮 / 拖拽（对齐旧 app.js:1559-1678） ──
const fileUploadInput = ref(null);
let uploadSeq = 0;

// 大小格式化阈值与旧 app.js:1574-1578 一致
function formatSize(size) {
  if (size > 1024 * 1024) return (size / (1024 * 1024)).toFixed(1) + ' MB';
  if (size > 1024) return (size / 1024).toFixed(1) + ' KB';
  return size + ' B';
}

function dlHref(name) {
  return '/api/upload/' + encodeURIComponent(name);
}

function removeUploading(id) {
  const idx = uploading.value.findIndex((u) => u.id === id);
  if (idx >= 0) uploading.value.splice(idx, 1);
}

function uploadFiles(files) {
  for (const file of files) {
    if (file.size > MAX_UPLOAD_BYTES) {
      ElMessage.error(file.name + t.uploadTooLargeSuffix);
      continue;
    }
    const id = ++uploadSeq;
    uploading.value.push({ id, name: file.name, pct: 0 });

    const xhr = new XMLHttpRequest();
    const formData = new FormData();
    formData.append('file', file);

    xhr.upload.addEventListener('progress', (e) => {
      if (!e.lengthComputable) return;
      // 必须经响应式数组改值（直接改原始对象不触发视图更新）
      const entry = uploading.value.find((u) => u.id === id);
      if (entry) entry.pct = Math.round((e.loaded / e.total) * 100);
    });

    xhr.addEventListener('load', () => {
      removeUploading(id);
      if (xhr.status === 200) {
        try {
          const data = JSON.parse(xhr.responseText);
          if (data.status === 'success') {
            attachedFiles.value.push({ name: data.filename, path: data.path, size: data.size });
            ElMessage.success(data.filename + t.uploadSuccessSuffix);
          }
        } catch { /* 响应体异常静默，对齐旧版 */ }
      } else {
        let detail = file.name + t.uploadFailedSuffix;
        try { const d = JSON.parse(xhr.responseText); if (d.detail) detail = d.detail; } catch { /* noop */ }
        ElMessage.error(detail);
      }
    });

    xhr.addEventListener('error', () => {
      removeUploading(id);
      ElMessage.error(file.name + t.uploadNetworkErrorSuffix);
    });

    xhr.open('POST', '/api/upload');
    xhr.send(formData);
  }
}

function pickFile() { fileUploadInput.value && fileUploadInput.value.click(); }
function onFilePicked(e) {
  if (e.target.files.length > 0) uploadFiles(e.target.files);
  e.target.value = '';
}

function removeAttachedFile(index) {
  const f = attachedFiles.value[index];
  attachedFiles.value.splice(index, 1);
  if (f) {
    fetch(dlHref(f.name), { method: 'DELETE' }).catch(() => {
      ElMessage.warning(t.removeServerFailedPrefix + f.name + t.removeServerFailedSuffix);
    });
  }
}

// 拖拽文件到输入区（preventDefault 由模板 .prevent 修饰符处理）
function onDragover() { /* noop：仅阻止默认行为以允许 drop */ }
function onDrop(e) {
  const files = e.dataTransfer && e.dataTransfer.files;
  if (files && files.length > 0) uploadFiles(files);
}

// ── 语音输入（对齐旧 app.js:1733-1790；不支持则隐藏按钮） ──
const inputRef = ref(null);
const SpeechRecognitionImpl = window.SpeechRecognition || window.webkitSpeechRecognition;
const speechSupported = !!SpeechRecognitionImpl;
const listening = ref(false);
let recognition = null;

if (speechSupported) {
  recognition = new SpeechRecognitionImpl();
  recognition.continuous = false;
  recognition.interimResults = false;

  recognition.onstart = () => { listening.value = true; };
  recognition.onresult = (event) => {
    const transcript = event.results[0][0].transcript;
    if (!transcript) return;
    // 插入光标处并恢复选区（对齐旧 app.js:1750-1761）
    const ta = inputRef.value && inputRef.value.textarea;
    const current = text.value;
    const start = ta ? ta.selectionStart : current.length;
    const end = ta ? ta.selectionEnd : current.length;
    text.value = current.substring(0, start) + transcript + current.substring(end);
    nextTick(() => {
      if (ta) {
        ta.selectionStart = ta.selectionEnd = start + transcript.length;
        ta.focus();
      }
    });
  };
  recognition.onerror = (event) => {
    console.error('Speech recognition error', event.error);
    listening.value = false;
  };
  recognition.onend = () => { listening.value = false; };
}

function toggleListen() {
  if (!recognition) return;
  if (listening.value) { recognition.stop(); return; }
  try { recognition.lang = 'zh-CN'; recognition.start(); } catch { /* 重复启动忽略，对齐旧版 */ }
}

onUnmounted(() => {
  // 卸载时停掉识别并摘掉回调，避免组件销毁后 onend 改已卸载状态
  if (!recognition) return;
  recognition.onstart = null;
  recognition.onresult = null;
  recognition.onerror = null;
  recognition.onend = null;
  try { recognition.stop(); } catch { /* noop */ }
});
</script>

<template>
  <div class="chat-input-wrap">
    <!-- 待发送图片预览条 -->
    <div v-if="pendingImages.length" class="image-preview-bar">
      <div v-for="(url, i) in pendingImages" :key="i" class="image-thumb">
        <img :src="url" alt="" />
        <button type="button" class="image-thumb-rm" :title="t.remove" @click="removePendingImage(i)">&times;</button>
      </div>
    </div>

    <!-- 附件 chips：上传中（进度条）+ 已上传（下载/移除） -->
    <div v-if="uploading.length || attachedFiles.length" class="attach-bar">
      <div v-for="u in uploading" :key="'up-' + u.id" class="attach-chip uploading">
        <span class="attach-chip-name" :title="u.name">{{ u.name }}</span>
        <span class="attach-progress">
          <span class="attach-progress-bar" :style="{ width: u.pct + '%' }"></span>
        </span>
      </div>
      <div v-for="(f, i) in attachedFiles" :key="f.name" class="attach-chip">
        <span class="attach-chip-name" :title="`${f.name} (${formatSize(f.size)})`">{{ f.name }}</span>
        <a class="attach-chip-dl" :href="dlHref(f.name)" :title="t.download" download>
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
        </a>
        <button type="button" class="attach-chip-rm" :title="t.remove" @click="removeAttachedFile(i)">&times;</button>
      </div>
    </div>

    <div class="chat-input" @dragover.prevent="onDragover" @drop.prevent="onDrop">
      <el-input
        ref="inputRef"
        v-model="text"
        type="textarea"
        :autosize="{ minRows: 1, maxRows: 6 }"
        :placeholder="listening ? t.voiceListening : (running ? t.inputPlaceholderRunning : t.inputPlaceholder)"
        @keydown="onKeydown"
        @paste="onPaste"
      />
      <template v-if="!isMobile">
        <el-button circle class="tool-btn" :title="t.attachImage" @click="pickImage">🖼️</el-button>
        <el-button circle class="tool-btn" :title="t.attachFile" @click="pickFile">📎</el-button>
        <el-button
          v-if="speechSupported"
          circle
          class="tool-btn mic-btn"
          :class="{ listening }"
          :title="t.voiceInput"
          @click="toggleListen"
        >🎤</el-button>
      </template>
      <!-- 移动端：单个「＋」按钮，点击展开动作面板（微信式） -->
      <el-button
        v-else
        circle
        class="tool-btn plus-btn"
        :class="{ open: panelOpen }"
        :title="t.attachImage"
        @click="togglePanel"
      >＋</el-button>
      <el-button
        v-if="running"
        type="danger"
        circle
        class="stop-btn"
        :title="t.stop"
        @click="emit('stop')"
      >⏹</el-button>
      <el-button
        type="primary"
        :disabled="!canSend"
        @click="submit"
      >{{ running ? t.append : t.send }}</el-button>
    </div>

    <!-- 移动端「＋」动作面板：图标卡片式（微信风格） -->
    <div v-if="isMobile && panelOpen" class="ci-plus-panel">
      <button type="button" class="plus-action" @click="withPanelClose(pickImage)">
        <span class="pa-icon">🖼️</span><span class="pa-label">{{ t.attachImage }}</span>
      </button>
      <button type="button" class="plus-action" @click="withPanelClose(pickFile)">
        <span class="pa-icon">📎</span><span class="pa-label">{{ t.attachFile }}</span>
      </button>
      <button
        v-if="speechSupported"
        type="button"
        class="plus-action"
        :class="{ listening }"
        @click="withPanelClose(toggleListen)"
      >
        <span class="pa-icon">🎤</span><span class="pa-label">{{ listening ? t.stop : t.voiceInput }}</span>
      </button>
    </div>

    <input ref="imageFileInput" type="file" accept="image/*" multiple hidden @change="onImagePicked" />
    <input ref="fileUploadInput" type="file" multiple hidden @change="onFilePicked" />
  </div>
</template>

<style scoped>
.chat-input-wrap {
  margin: 4px 16px 16px;
}

/* 待发送图片预览条：48px 缩略图 + 右上角移除（对齐旧 renderImagePreviews） */
.image-preview-bar {
  display: flex;
  gap: 6px;
  padding: 4px 2px 8px;
  flex-wrap: wrap;
}

.image-thumb {
  position: relative;
  width: 48px;
  height: 48px;
  border-radius: 6px;
  overflow: hidden;
  border: 1px solid var(--el-border-color-light);
  flex-shrink: 0;
}

.image-thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.image-thumb-rm {
  position: absolute;
  top: 0;
  right: 0;
  background: var(--panda-code-bg);
  color: var(--el-text-color-primary);
  border: none;
  width: 16px;
  height: 16px;
  font-size: 10px;
  line-height: 16px;
  cursor: pointer;
  padding: 0;
  border-radius: 0 0 0 4px;
}

/* 附件 chips 条 */
.attach-bar {
  display: flex;
  gap: 6px;
  padding: 4px 2px 8px;
  flex-wrap: wrap;
}

.attach-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 240px;
  padding: 3px 8px;
  font-size: 12px;
  background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color-light);
  border-radius: 999px;
  color: var(--el-text-color-regular);
}

.attach-chip-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.attach-chip-dl {
  display: inline-flex;
  color: var(--el-color-primary);
  text-decoration: none;
}

.attach-chip-rm {
  border: none;
  background: transparent;
  color: var(--el-text-color-secondary);
  cursor: pointer;
  padding: 0;
  font-size: 13px;
  line-height: 1;
}

.attach-chip-rm:hover {
  color: var(--el-color-danger);
}

/* 上传中进度（chip 内迷你进度条） */
.attach-progress {
  width: 56px;
  height: 4px;
  border-radius: 2px;
  background: var(--el-fill-color-darker);
  overflow: hidden;
  flex-shrink: 0;
}

.attach-progress-bar {
  display: block;
  height: 100%;
  background: var(--el-color-primary);
  transition: width var(--panda-transition);
}

/* 悬浮卡片化输入区：圆角 + 阴影 + 聚焦描边 */
.chat-input {
  display: flex;
  align-items: flex-end;
  gap: 8px;
  padding: 8px 10px;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-light);
  border-radius: var(--panda-radius-lg);
  box-shadow: var(--panda-shadow-float);
  transition: border-color var(--panda-transition), box-shadow var(--panda-transition);
}

.chat-input:focus-within {
  border-color: var(--el-color-primary-light-3);
  box-shadow: 0 0 0 3px var(--el-color-primary-light-8), var(--panda-shadow-float);
}

.chat-input :deep(.el-textarea) {
  flex: 1;
}

/* 卡片内部不再需要输入框自身描边 */
.chat-input :deep(.el-textarea__inner) {
  background: transparent;
  border: none;
  box-shadow: none;
  padding: 6px 4px;
  resize: none;
}

.tool-btn,
.stop-btn {
  flex-shrink: 0;
}

/* 移动端「＋」按钮与动作面板（微信式） */
.plus-btn {
  transition: transform 0.18s ease;
  font-size: 18px;
  font-weight: 500;
}

.plus-btn.open {
  transform: rotate(45deg);
}

.ci-plus-panel {
  display: flex;
  gap: 14px;
  padding: 10px 4px 2px;
  animation: ci-panel-in 0.16s ease;
}

@keyframes ci-panel-in {
  from { opacity: 0; transform: translateY(6px); }
  to { opacity: 1; transform: translateY(0); }
}

.plus-action {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 5px;
  background: none;
  border: none;
  cursor: pointer;
  padding: 4px 6px;
}

.pa-icon {
  width: 52px;
  height: 52px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 24px;
  background: var(--el-bg-color);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 12px;
  transition: border-color var(--panda-transition), box-shadow var(--panda-transition);
}

.plus-action:active .pa-icon {
  background: var(--el-fill-color);
}

.plus-action.listening .pa-icon {
  border-color: var(--el-color-primary);
  box-shadow: 0 0 0 3px var(--el-color-primary-light-8);
}

.pa-label {
  font-size: 11px;
  color: var(--el-text-color-secondary);
}

/* 识别中：主色描边 + 呼吸光圈反馈（对齐旧 mic-btn.listening 状态反馈） */
.mic-btn.listening {
  color: var(--el-color-primary);
  border-color: var(--el-color-primary);
  animation: mic-pulse 1.2s ease-in-out infinite;
}

@keyframes mic-pulse {
  0%, 100% { box-shadow: 0 0 0 0 var(--el-color-primary-light-5); }
  50% { box-shadow: 0 0 0 6px transparent; }
}

@media (max-width: 768px) {
  .chat-input-wrap {
    margin: 2px 8px 8px;
  }

  .chat-input {
    padding: 6px 8px;
    border-radius: 12px;
  }
}
</style>
