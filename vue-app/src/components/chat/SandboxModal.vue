<script setup>
// 沙箱授权弹窗：sandbox_blocked 事件触发（progress 子事件，见 dev-docs/API契约.md §3.2）。
// 按钮动作名与 api/ws.py 的 sandbox_response 处理严格对应：
// approve_once / approve_dir / approve_always / approve_session / deny_once / deny_always。
// block_type=path 多一个「授权整个目录」；permission 多一个「本次会话全部同类」；
// category=sudo 时授权类动作要求输入密码（ws.py password 字段）；
// category=secret 时切换为凭据收集表单（request_secret 工具触发），
// 表单字段随 sandbox_response 透传到 agent 的 result_holder（仅内存），由 agent 写入本地凭证库。
import { computed, reactive, ref, watch } from 'vue';
import { ElMessage } from 'element-plus';
import zh from '../../i18n/zh';

const t = zh.chat.sandbox;

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  data: { type: Object, required: true }, // { path, toolName, blockType, description, category }
});
const emit = defineEmits(['update:modelValue', 'respond']);

const sudoPassword = ref('');

// 凭据收集表单（category='secret'）。名称留空时后端自动生成 secret_<timestamp>；
// LLM 建议的名称经 sandbox_blocked 的 path 字段传入，作为预填值。
const secretForm = reactive({
  name: '', type: 'generic', host: '', username: '', password: '', note: '',
});

const SECRET_TYPES = ['generic', 'api_key', 'mongodb', 'mysql'];
const SECRET_NAME_RE = /^[A-Za-z0-9_-]+$/;

watch(
  () => props.modelValue,
  (v) => {
    if (!v) return;
    sudoPassword.value = '';
    secretForm.name = props.data.category === 'secret' ? (props.data.path || '') : '';
    secretForm.type = 'generic';
    secretForm.host = '';
    secretForm.username = '';
    secretForm.password = '';
    secretForm.note = '';
  }
);

const isSudo = computed(() => props.data.category === 'sudo');
const isSecret = computed(() => props.data.category === 'secret');

const title = computed(() => {
  if (isSecret.value) return t.secretTitle;
  if (props.data.blockType === 'network') return t.networkTitle;
  if (props.data.blockType === 'permission') return t.permissionTitle;
  return t.pathTitle;
});

const descText = computed(() => {
  if (isSecret.value) return t.secretDesc;
  if (props.data.blockType === 'network') return t.networkDesc;
  if (props.data.blockType === 'permission') return t.permissionDesc;
  return t.pathDesc;
});

// 按钮集按 block_type/category 区分，与旧 static/app.js showSandboxBlockedModal 一致
const buttons = computed(() => {
  const deny = [
    { action: 'deny_once', label: t.denyOnce, type: 'danger' },
    { action: 'deny_always', label: t.denyAlways, type: 'danger' },
  ];
  if (isSecret.value) {
    return [
      { action: 'approve_once', label: t.secretSave, type: 'primary' },
      { action: 'deny_once', label: t.secretCancel, type: 'danger' },
    ];
  }
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
  if (isSecret.value) {
    // 拒绝类动作不携带任何表单字段，避免不必要的凭据传输
    if (!isApprove) {
      emit('respond', { action, password: '' });
      return;
    }
    if (!secretForm.password) {
      ElMessage.warning(t.secretPasswordRequired);
      return;
    }
    if (secretForm.name && !SECRET_NAME_RE.test(secretForm.name)) {
      ElMessage.warning(t.secretNameInvalid);
      return;
    }
    emit('respond', {
      action,
      password: secretForm.password,
      secretName: secretForm.name.trim(),
      secretType: secretForm.type,
      host: secretForm.host.trim(),
      username: secretForm.username.trim(),
      note: secretForm.note,
    });
    return;
  }
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
    <p v-if="(data.blockType === 'permission' || isSecret) && data.description" class="sb-desc">
      ⚠️ {{ data.description }}
    </p>
    <div v-if="!isSecret" class="sb-path">{{ data.path }}</div>

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

    <div v-if="isSecret" class="sb-secret">
      <p class="sb-secret-hint">{{ t.secretHint }}</p>
      <label class="sb-field-label">{{ t.secretNameLabel }}</label>
      <el-input v-model="secretForm.name" :placeholder="t.secretNamePlaceholder" autocomplete="off" />
      <label class="sb-field-label">{{ t.secretTypeLabel }}</label>
      <el-select v-model="secretForm.type" class="sb-field-full">
        <el-option v-for="st in SECRET_TYPES" :key="st" :label="st" :value="st" />
      </el-select>
      <label class="sb-field-label">{{ t.secretHostLabel }}</label>
      <el-input v-model="secretForm.host" :placeholder="t.secretHostPlaceholder" autocomplete="off" />
      <label class="sb-field-label">{{ t.secretUsernameLabel }}</label>
      <el-input v-model="secretForm.username" autocomplete="off" />
      <label class="sb-field-label">{{ t.secretPasswordLabel }}</label>
      <el-input
        v-model="secretForm.password"
        type="password"
        :placeholder="t.secretPasswordPlaceholder"
        autocomplete="new-password"
        show-password
      />
      <label class="sb-field-label">{{ t.secretNoteLabel }}</label>
      <el-input v-model="secretForm.note" type="textarea" :rows="2" />
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

.sb-secret {
  margin-bottom: 12px;
}

.sb-secret-hint {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin: 0 0 10px;
}

.sb-field-label {
  display: block;
  font-size: 13px;
  color: var(--el-text-color-secondary);
  margin: 8px 0 4px;
}

.sb-field-full {
  width: 100%;
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
