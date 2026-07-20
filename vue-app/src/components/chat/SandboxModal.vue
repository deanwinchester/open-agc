<script setup>
// 沙箱授权弹窗：sandbox_blocked 事件触发（progress 子事件，见 dev-docs/API契约.md §3.2）。
// 按钮动作名与 api/ws.py 的 sandbox_response 处理严格对应：
// approve_once / approve_dir / approve_always / approve_session / deny_once / deny_always。
// block_type=path 多一个「授权整个目录」；permission 多一个「本次会话全部同类」；
// category=sudo 时授权类动作要求输入密码（ws.py password 字段）。
import { computed, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';
import zh from '../../i18n/zh';

const t = zh.chat.sandbox;

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  data: { type: Object, required: true }, // { path, toolName, blockType, description, category }
});
const emit = defineEmits(['update:modelValue', 'respond']);

const sudoPassword = ref('');

watch(
  () => props.modelValue,
  (v) => {
    if (v) sudoPassword.value = '';
  }
);

const isSudo = computed(() => props.data.category === 'sudo');

const title = computed(() => {
  if (props.data.blockType === 'network') return t.networkTitle;
  if (props.data.blockType === 'permission') return t.permissionTitle;
  return t.pathTitle;
});

const descText = computed(() => {
  if (props.data.blockType === 'network') return t.networkDesc;
  if (props.data.blockType === 'permission') return t.permissionDesc;
  return t.pathDesc;
});

// 按钮集按 block_type 区分，与旧 static/app.js showSandboxBlockedModal 一致
const buttons = computed(() => {
  const deny = [
    { action: 'deny_once', label: t.denyOnce, type: 'danger' },
    { action: 'deny_always', label: t.denyAlways, type: 'danger' },
  ];
  if (props.data.blockType === 'network') {
    return [
      { action: 'approve_once', label: t.approveOnce, type: 'default' },
      { action: 'approve_always', label: t.approveAlways, type: 'primary' },
      ...deny,
    ];
  }
  if (props.data.blockType === 'permission') {
    return [
      { action: 'approve_once', label: t.approveThis, type: 'default' },
      { action: 'approve_session', label: t.approveSession, type: 'default' },
      { action: 'approve_always', label: t.approveAlways, type: 'primary' },
      ...deny,
    ];
  }
  return [
    { action: 'approve_dir', label: t.approveDir, type: 'primary' },
    { action: 'approve_once', label: t.approveOnce, type: 'default' },
    { action: 'approve_always', label: t.approveAlways, type: 'primary' },
    ...deny,
  ];
});

function respond(action) {
  const isApprove = action.startsWith('approve');
  if (isSudo.value && isApprove && !sudoPassword.value) {
    ElMessage.warning(t.sudoRequired);
    return;
  }
  // 仅授权类动作携带密码；拒绝类动作不传，避免不必要的密码传输
  emit('respond', { action, password: isApprove ? sudoPassword.value || '' : '' });
}

function onUpdate(v) {
  emit('update:modelValue', v);
}
</script>

<template>
  <el-dialog
    :model-value="modelValue"
    :title="title"
    width="480px"
    :show-close="false"
    :close-on-click-modal="false"
    :close-on-press-escape="false"
    @update:model-value="onUpdate"
  >
    <p class="sb-line">
      {{ t.toolPrefix }} <b>{{ data.toolName }}</b> {{ descText }}
    </p>
    <p v-if="data.blockType === 'permission' && data.description" class="sb-desc">
      ⚠️ {{ data.description }}
    </p>
    <div class="sb-path">{{ data.path }}</div>

    <div v-if="isSudo" class="sb-sudo">
      <label class="sb-sudo-label">{{ t.sudoLabel }}</label>
      <el-input
        v-model="sudoPassword"
        type="password"
        :placeholder="t.sudoPlaceholder"
        autocomplete="off"
        show-password
      />
    </div>

    <div class="sb-actions">
      <el-button
        v-for="btn in buttons"
        :key="btn.action"
        :type="btn.type"
        @click="respond(btn.action)"
      >{{ btn.label }}</el-button>
    </div>
  </el-dialog>
</template>

<style scoped>
.sb-line {
  margin: 0 0 8px;
  font-size: 14px;
}

.sb-desc {
  color: var(--el-color-warning);
  font-size: 13px;
  margin: 0 0 8px;
}

.sb-path {
  background: var(--el-fill-color);
  padding: 8px 12px;
  border-radius: 6px;
  font-family: Consolas, Monaco, monospace;
  font-size: 12px;
  word-break: break-all;
  margin-bottom: 12px;
}

.sb-sudo {
  margin-bottom: 12px;
}

.sb-sudo-label {
  display: block;
  font-size: 13px;
  color: var(--el-text-color-secondary);
  margin-bottom: 6px;
}

.sb-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.sb-actions .el-button {
  margin-left: 0;
  flex: 1 1 45%;
}
</style>
