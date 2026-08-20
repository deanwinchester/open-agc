<script setup>
// 凭据检测确认窗（入口 B）：ChatInput 发送前检测到凭据片段时弹出。
// - 命中片段打码显示（前 3 后 3，中间 ••••，由 secretDetect.describeHit 生成）
// - 10 秒倒计时进度条（50ms 步进平滑递减），到点自动执行「立即保存」；
//   鼠标悬停内容区暂停倒计时，移出继续
// - 三个动作：立即保存（默认高亮）/ 丢弃并打码 / 保留明文（warning + 二次确认）
// resolve 事件载荷：{ action: 'save'|'discard'|'keep', form: { name, type, username, host } }
// 倒计时/监听全部在 onUnmounted 清理。
import { computed, reactive, ref, watch, onUnmounted } from 'vue';
import { ElMessage } from 'element-plus';
import zh from '../../i18n/zh';
import { SECRET_NAME_RE } from '../../utils/secretDetect';

const t = zh.chat.secretConfirm;

const COUNTDOWN_MS = 10000;
const TICK_MS = 50;
const SECRET_TYPES = ['mongodb', 'mysql', 'postgres', 'redis', 'api_key', 'generic'];

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  // 打码后的命中描述串数组（secretDetect.describeHit 产物），不含明文
  hitLabels: { type: Array, default: () => [] },
  // 预填表单 { name, type, username, host }
  suggested: { type: Object, default: () => ({}) },
});
const emit = defineEmits(['update:modelValue', 'resolve']);

const form = reactive({ name: '', type: 'generic', username: '', host: '' });

// ── 倒计时 ──
const remaining = ref(COUNTDOWN_MS);
const paused = ref(false);
let timer = null;

const pct = computed(() => Math.max(0, (remaining.value / COUNTDOWN_MS) * 100));
const secondsLeft = computed(() => Math.ceil(remaining.value / 1000));

function stopTimer() {
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
}

function startTimer() {
  stopTimer();
  timer = setInterval(() => {
    if (paused.value) return;
    remaining.value -= TICK_MS;
    if (remaining.value <= 0) {
      remaining.value = 0;
      // 先停表再保存：表单非法时 doSave 内部 return，
      // 否则每 50ms 重复触发警告刷屏（失败后由用户手动点击重试）
      stopTimer();
      doSave(); // 到点自动「立即保存」
    }
  }, TICK_MS);
}

watch(
  () => props.modelValue,
  (v) => {
    if (!v) {
      stopTimer();
      return;
    }
    // 每次打开重置表单与倒计时
    form.name = props.suggested.name || '';
    form.type = props.suggested.type || 'generic';
    form.username = props.suggested.username || '';
    form.host = props.suggested.host || '';
    remaining.value = COUNTDOWN_MS;
    paused.value = false;
    keepArmed.value = false;
    startTimer();
  }
);

onUnmounted(stopTimer);

function close() {
  emit('update:modelValue', false);
}

function validForm() {
  const name = form.name.trim();
  if (!name) {
    ElMessage.warning(t.nameRequired);
    return null;
  }
  if (!SECRET_NAME_RE.test(name)) {
    ElMessage.warning(t.nameInvalid);
    return null;
  }
  return {
    name,
    type: form.type,
    username: form.username.trim(),
    host: form.host.trim(),
  };
}

function doSave() {
  const f = validForm();
  if (!f) return;
  stopTimer();
  emit('resolve', { action: 'save', form: f });
  close();
}

function doDiscard() {
  stopTimer();
  emit('resolve', { action: 'discard', form: null });
  close();
}

// 保留明文：warning 样式 + 二次确认（第一次点击进入待确认态，再点确认）
const keepArmed = ref(false);
function doKeep() {
  if (!keepArmed.value) {
    keepArmed.value = true;
    return;
  }
  stopTimer();
  emit('resolve', { action: 'keep', form: null });
  close();
}
</script>

<template>
  <el-dialog :append-to-body="true"
    :model-value="modelValue"
    :title="t.title"
    width="480px"
    :show-close="false"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    @update:model-value="emit('update:modelValue', $event)"
  >
    <div class="sc-body" @mouseenter="paused = true" @mouseleave="paused = false">
      <p class="sc-desc">{{ t.desc }}</p>
      <div class="sc-hits">
        <div v-for="(h, i) in hitLabels" :key="i" class="sc-hit">{{ h }}</div>
      </div>

      <label class="sc-field-label">{{ t.nameLabel }}</label>
      <el-input v-model="form.name" autocomplete="off" />
      <label class="sc-field-label">{{ t.typeLabel }}</label>
      <el-select v-model="form.type" class="sc-field-full">
        <el-option v-for="st in SECRET_TYPES" :key="st" :label="st" :value="st" />
      </el-select>
      <label class="sc-field-label">{{ t.usernameLabel }}</label>
      <el-input v-model="form.username" autocomplete="off" />
      <label class="sc-field-label">{{ t.hostLabel }}</label>
      <el-input v-model="form.host" autocomplete="off" />

      <div class="sc-countdown">
        <el-progress
          :percentage="pct"
          :stroke-width="6"
          :show-text="false"
          :duration="0"
          color="var(--el-color-primary)"
        />
        <span class="sc-countdown-text">
          {{ paused ? t.countdownPaused : secondsLeft + t.countdownSuffix }}
        </span>
      </div>
    </div>

    <div class="sc-actions">
      <el-button type="primary" @click="doSave">{{ t.save }}</el-button>
      <el-button @click="doDiscard">{{ t.discard }}</el-button>
      <el-button type="warning" :plain="!keepArmed" @click="doKeep">
        {{ keepArmed ? t.keepConfirm : t.keep }}
      </el-button>
    </div>
  </el-dialog>
</template>

<style scoped>
.sc-desc {
  margin: 0 0 10px;
  font-size: 13px;
  color: var(--el-text-color-regular);
}

.sc-hits {
  background: var(--el-fill-color);
  border-radius: 6px;
  padding: 8px 12px;
  margin-bottom: 6px;
}

.sc-hit {
  font-family: Consolas, Monaco, monospace;
  font-size: 12px;
  color: var(--el-color-warning);
  word-break: break-all;
  line-height: 1.7;
}

.sc-field-label {
  display: block;
  font-size: 13px;
  color: var(--el-text-color-secondary);
  margin: 8px 0 4px;
}

.sc-field-full {
  width: 100%;
}

.sc-countdown {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-top: 14px;
}

.sc-countdown .el-progress {
  flex: 1;
}

.sc-countdown-text {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  white-space: nowrap;
}

.sc-actions {
  display: flex;
  gap: 8px;
  margin-top: 16px;
}

.sc-actions .el-button {
  margin-left: 0;
  flex: 1 1 30%;
}
</style>
