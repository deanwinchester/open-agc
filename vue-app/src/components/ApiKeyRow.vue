<script setup>
// 单个 provider 的 API Key 输入行。
// - placeholder 显示后端返回的掩码值（xxx...xxx），未配置时显示「未配置」
// - 输入留空 = 保存时不提交该 provider（绝不回传掩码，后端也会拒绝掩码值）
// - autocomplete="new-password" 防浏览器把保存过的密码自动填进来
// - 明文/密文切换用 el-input 原生 show-password（内置眼睛图标与状态管理），
//   替代旧版自定义 el-button 方案（该实现点击无响应，已按验收要求移除）
import { computed } from 'vue';
import zh from '../i18n/zh';

const props = defineProps({
  modelValue: { type: String, default: '' },
  label: { type: String, required: true },
  masked: { type: String, default: '' },
});
const emit = defineEmits(['update:modelValue']);

const value = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
});
</script>

<template>
  <div class="api-key-row">
    <span class="api-key-label" :title="label">{{ label }}</span>
    <el-input
      v-model="value"
      type="password"
      show-password
      :placeholder="masked || zh.settings.models.apiKeys.notSet"
      autocomplete="new-password"
      clearable
      class="api-key-input"
    />
  </div>
</template>

<style scoped>
.api-key-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.api-key-label {
  width: 160px;
  flex-shrink: 0;
  font-size: 13px;
  color: var(--el-text-color-secondary);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.api-key-input {
  flex: 1;
}

@media (max-width: 768px) {
  /* 窄屏：标签在上、输入框在下 */
  .api-key-row {
    flex-direction: column;
    align-items: stretch;
    gap: 4px;
  }

  .api-key-label {
    width: auto;
  }
}
</style>
