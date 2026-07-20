<script setup>
// 会话侧栏：列表 / 新建 / 重命名 / 删除（id=1 为清空，对齐旧 static/js/sessions.js 规则）。
// 确认弹窗在本组件内完成，REST 调用由父组件（ChatView）执行。
import { ElMessageBox } from 'element-plus';
import zh from '../../i18n/zh';

const t = zh.chat;

defineProps({
  sessions: { type: Array, default: () => [] },
  currentId: { type: Number, default: null },
});
const emit = defineEmits(['select', 'create', 'rename', 'remove', 'clear']);

async function onRename(session) {
  try {
    const { value } = await ElMessageBox.prompt(t.renamePrompt, t.rename, {
      inputValue: session.name,
      confirmButtonText: t.rename,
      cancelButtonText: zh.goals.cancel,
    });
    const name = (value || '').trim();
    if (name && name !== session.name) emit('rename', { id: session.id, name });
  } catch {
    /* 用户取消 */
  }
}

async function onRemove(session) {
  try {
    await ElMessageBox.confirm(t.deleteConfirmText, t.deleteConfirmTitle, {
      type: 'warning',
      confirmButtonText: t.delete,
      cancelButtonText: zh.goals.cancel,
    });
    emit('remove', session.id);
  } catch {
    /* 用户取消 */
  }
}

async function onClear(session) {
  try {
    await ElMessageBox.confirm(t.clearConfirmText, t.clearConfirmTitle, {
      type: 'warning',
      confirmButtonText: t.clear,
      cancelButtonText: zh.goals.cancel,
    });
    emit('clear', session.id);
  } catch {
    /* 用户取消 */
  }
}
</script>

<template>
  <aside class="session-rail">
    <div class="rail-header">
      <span class="rail-title">{{ t.sessionsTitle }}</span>
      <el-button size="small" type="primary" plain @click="emit('create')">+ {{ t.newSession }}</el-button>
    </div>
    <div class="rail-list">
      <div
        v-for="s in sessions"
        :key="s.id"
        class="session-item"
        :class="{ active: s.id === currentId }"
        @click="emit('select', s.id)"
      >
        <span class="session-name" :title="s.name">{{ s.name }}</span>
        <span class="session-actions" @click.stop>
          <button class="icon-btn" :title="t.rename" @click="onRename(s)">✎</button>
          <button
            v-if="s.id === 1"
            class="icon-btn"
            :title="t.clear"
            @click="onClear(s)"
          >⟳</button>
          <button
            v-else
            class="icon-btn danger"
            :title="t.delete"
            @click="onRemove(s)"
          >×</button>
        </span>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.session-rail {
  width: 220px;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  border-right: 1px solid var(--el-border-color-lighter);
  background: var(--el-bg-color);
  min-height: 0;
}

.rail-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 12px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.rail-title {
  font-weight: 600;
  font-size: 14px;
  letter-spacing: 0.02em;
}

.rail-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}

.session-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 9px 12px;
  margin-bottom: 2px;
  border-radius: 8px;
  cursor: pointer;
  font-size: 13px;
  gap: 4px;
  transition: background-color var(--panda-transition), color var(--panda-transition),
    box-shadow var(--panda-transition);
}

.session-item:hover {
  background: var(--el-fill-color-light);
}

/* 激活会话：浅绿底 + 左侧竹绿条（inset 阴影实现，不挤压布局） */
.session-item.active {
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary-dark-2);
  font-weight: 600;
  box-shadow: inset 3px 0 0 var(--el-color-primary);
}

.session-name {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
  min-width: 0;
}

.session-actions {
  display: none;
  flex-shrink: 0;
}

.session-item:hover .session-actions,
.session-item.active .session-actions {
  display: inline-flex;
}

.icon-btn {
  border: none;
  background: transparent;
  cursor: pointer;
  color: var(--el-text-color-secondary);
  font-size: 13px;
  padding: 2px 4px;
  border-radius: 4px;
  transition: background-color var(--panda-transition), color var(--panda-transition);
}

.icon-btn:hover {
  background: var(--el-fill-color);
  color: var(--el-text-color-primary);
}

.icon-btn.danger:hover {
  color: var(--el-color-danger);
}

@media (max-width: 768px) {
  /* 抽屉模式下触控目标 ≥40px */
  .session-item {
    padding: 11px 12px;
  }

  .icon-btn {
    padding: 6px;
  }
}
</style>
