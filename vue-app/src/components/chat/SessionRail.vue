<script setup>
// 会话侧栏：列表 / 新建 / 重命名 / 删除（id=1 为清空，对齐旧 static/js/sessions.js 规则）。
// 确认弹窗在本组件内完成，REST 调用由父组件（ChatView）执行；搜索直接调 REST。
import { ref, watch } from 'vue';
import { ElMessageBox } from 'element-plus';
import { request } from '../../api/client';
import zh from '../../i18n/zh';

const t = zh.chat;

defineProps({
  sessions: { type: Array, default: () => [] },
  currentId: { type: Number, default: null },
  unread: { type: Object, default: () => ({}) },
});
const emit = defineEmits(['select', 'create', 'rename', 'remove', 'clear']);

// ── 会话搜索（全文检索聊天记录）──
const query = ref('');
const results = ref(null); // null = 未搜索；数组 = 搜索结果
let searchTimer = null;

watch(query, (q) => {
  clearTimeout(searchTimer);
  const kw = (q || '').trim();
  if (!kw) { results.value = null; return; }
  searchTimer = setTimeout(async () => {
    try {
      const data = await request(`/api/sessions/search?q=${encodeURIComponent(kw)}`);
      results.value = (data && data.results) || [];
    } catch {
      results.value = [];
    }
  }, 300);
});

function onPickResult(r) {
  emit('select', r.id);
}

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
    <div class="rail-search">
      <input
        v-model="query"
        class="search-input"
        type="text"
        :placeholder="t.searchSessions"
        clearable
      />
    </div>
    <div v-if="results !== null" class="rail-list">
      <div v-if="!results.length" class="search-empty">{{ t.searchNoResult }}</div>
      <div
        v-for="r in results"
        :key="r.id"
        class="session-item search-hit"
        :class="{ active: r.id === currentId }"
        @click="onPickResult(r)"
      >
        <div class="hit-body">
          <span class="session-name" :title="r.name">{{ r.name }}</span>
          <span v-if="r.snippet" class="hit-snippet" :title="r.snippet">{{ r.snippet }}</span>
        </div>
        <span v-if="unread[r.id]" class="unread-badge">{{ unread[r.id] > 99 ? '99+' : unread[r.id] }}</span>
      </div>
    </div>
    <div v-else class="rail-list">
      <div
        v-for="s in sessions"
        :key="s.id"
        class="session-item"
        :class="{ active: s.id === currentId }"
        @click="emit('select', s.id)"
      >
        <span class="session-name" :title="s.name">{{ s.name }}</span>
        <span v-if="unread[s.id]" class="unread-badge">{{ unread[s.id] > 99 ? '99+' : unread[s.id] }}</span>
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

.rail-search {
  padding: 8px 12px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.search-input {
  width: 100%;
  box-sizing: border-box;
  padding: 6px 10px;
  font-size: 12px;
  border: 1px solid var(--el-border-color);
  border-radius: 6px;
  background: var(--el-fill-color-light);
  color: var(--el-text-color-primary);
  outline: none;
  transition: border-color var(--panda-transition);
}

.search-input:focus {
  border-color: var(--el-color-primary);
  background: var(--el-bg-color);
}

.search-empty {
  padding: 16px 8px;
  text-align: center;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.hit-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.hit-snippet {
  font-size: 11px;
  font-weight: 400;
  color: var(--el-text-color-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.unread-badge {
  flex-shrink: 0;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  border-radius: 8px;
  background: var(--el-color-danger);
  color: #fff;
  font-size: 10px;
  line-height: 16px;
  text-align: center;
  font-weight: 600;
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
