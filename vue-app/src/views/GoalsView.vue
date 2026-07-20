<script setup>
// 目标视图（批次 2）：迁移旧 view-goals（static/js/goals.js）。
// 数据契约（api/routes/routes_goals.py，见 dev-docs/API契约.md §1.4）：
// - GET /api/goals → {items: [{id, desc, status, updated, task_ids, resume_count}]}
// - POST /api/goals {desc}（≤100 字）；PUT /api/goals/{id} {desc?, status?}；DELETE /api/goals/{id}
// - status 取值：pending/doing/done/stuck/archived
import { computed, onMounted, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { Plus, Refresh, Search, Delete } from '@element-plus/icons-vue';
import { request } from '../api/client';
import zh from '../i18n/zh';

const t = zh.goals;
const JSON_HEADERS = { 'Content-Type': 'application/json' };

// 排序权重与旧版一致：doing > pending > stuck > done > archived，同级按 id 倒序（新在前）
const STATUS_ORDER = { doing: 0, pending: 1, stuck: 2, done: 3, archived: 4 };
const STATUS_ICONS = { pending: '⬜', doing: '🔄', done: '✅', stuck: '🔴', archived: '📦' };
const QUICK_STATUSES = ['pending', 'doing', 'done', 'stuck'];
// 状态 → 公共 status-pill 变体（进行蓝/完成绿/卡住红/归档橙/待办灰）
const STATUS_PILL_CLS = { doing: 'info', pending: 'default', done: 'success', stuck: 'danger', archived: 'warning' };

const loading = ref(true);
const goals = ref([]);
const searchQuery = ref('');

const visibleGoals = computed(() => {
  const q = searchQuery.value.trim().toLowerCase();
  let items = goals.value;
  if (q) items = items.filter((g) => (g.desc || '').toLowerCase().includes(q));
  return [...items].sort((a, b) => {
    const oa = STATUS_ORDER[a.status] ?? 9;
    const ob = STATUS_ORDER[b.status] ?? 9;
    if (oa !== ob) return oa - ob;
    return (b.id || 0) - (a.id || 0);
  });
});

function statusLabel(status) {
  return t.status[status] || status;
}

function statusIcon(status) {
  return STATUS_ICONS[status] || '⬜';
}

async function loadGoals() {
  loading.value = true;
  try {
    const data = await request('/api/goals');
    goals.value = Array.isArray(data?.items) ? data.items : [];
  } catch (err) {
    ElMessage.error(`${t.loadFailed}: ${err.message}`);
  } finally {
    loading.value = false;
  }
}

// ── 快速状态切换 ──

async function setStatus(goal, status) {
  if (goal.status === status) return;
  try {
    await request(`/api/goals/${goal.id}`, {
      method: 'PUT',
      headers: JSON_HEADERS,
      body: JSON.stringify({ status }),
    });
    goal.status = status;
  } catch (err) {
    ElMessage.error(`${t.statusUpdateFailed}: ${err.message}`);
  }
}

// ── 创建 / 编辑弹窗 ──

const dialogVisible = ref(false);
const saving = ref(false);
const editId = ref(null); // null=创建
const formDesc = ref('');
const formStatus = ref('pending');

const descLen = computed(() => formDesc.value.length);

function openCreate() {
  editId.value = null;
  formDesc.value = '';
  formStatus.value = 'pending';
  dialogVisible.value = true;
}

function openEdit(goal) {
  editId.value = goal.id;
  formDesc.value = goal.desc || '';
  formStatus.value = goal.status || 'pending';
  dialogVisible.value = true;
}

async function saveGoal() {
  const desc = formDesc.value.trim();
  if (!desc) {
    ElMessage.error(t.descRequired);
    return;
  }
  saving.value = true;
  try {
    if (editId.value) {
      await request(`/api/goals/${editId.value}`, {
        method: 'PUT',
        headers: JSON_HEADERS,
        body: JSON.stringify({ desc, status: formStatus.value }),
      });
      ElMessage.success(t.saveSuccess);
    } else {
      await request('/api/goals', {
        method: 'POST',
        headers: JSON_HEADERS,
        body: JSON.stringify({ desc }),
      });
      ElMessage.success(t.createSuccess);
    }
    dialogVisible.value = false;
    loadGoals();
  } catch (err) {
    ElMessage.error(`${t.saveFailed}: ${err.message}`);
  } finally {
    saving.value = false;
  }
}

// ── 删除 ──

async function removeGoal(goal) {
  try {
    await ElMessageBox.confirm(`#${goal.id} ${goal.desc} — ${t.deleteConfirmText}`, t.deleteConfirmTitle, {
      confirmButtonText: t.delete,
      cancelButtonText: t.cancel,
      type: 'warning',
    });
  } catch {
    return; // 用户取消
  }
  try {
    await request(`/api/goals/${goal.id}`, { method: 'DELETE' });
    ElMessage.success(t.deleteSuccess);
    loadGoals();
  } catch (err) {
    ElMessage.error(`${t.deleteFailed}: ${err.message}`);
  }
}

onMounted(loadGoals);
</script>

<template>
  <div class="goals-view">
    <header class="view-header">
      <h1>{{ t.title }}</h1>
      <p class="view-desc">{{ t.desc }}</p>
    </header>

    <el-card class="list-card" shadow="never">
      <div class="toolbar">
        <el-input
          v-model="searchQuery"
          class="search-input"
          size="small"
          clearable
          :prefix-icon="Search"
          :placeholder="t.searchPlaceholder"
        />
        <div class="toolbar-right">
          <el-button size="small" :icon="Refresh" :title="t.refresh" @click="loadGoals" />
          <el-button size="small" type="primary" :icon="Plus" @click="openCreate">
            {{ t.create }}
          </el-button>
        </div>
      </div>

      <div v-if="!visibleGoals.length && !loading" class="empty-state">
        <div class="empty-icon">🎯</div>
        <p>{{ searchQuery ? t.emptySearch : t.empty }}</p>
        <small v-if="!searchQuery">{{ t.emptyHint }}</small>
      </div>

      <div v-loading="loading">
        <div v-for="goal in visibleGoals" :key="goal.id" class="row-card goal-card">
          <div class="row-card-head">
            <span class="goal-icon" :class="goal.status">{{ statusIcon(goal.status) }}</span>
            <span class="row-card-title" :title="goal.desc">{{ goal.desc }}</span>
            <div class="row-card-right">
              <div class="quick-statuses">
                <button
                  v-for="s in QUICK_STATUSES"
                  :key="s"
                  class="quick-status"
                  :class="{ active: goal.status === s }"
                  :title="statusLabel(s)"
                  @click="setStatus(goal, s)"
                >
                  {{ statusIcon(s) }}
                </button>
              </div>
              <span class="status-pill" :class="`status-pill--${STATUS_PILL_CLS[goal.status] || 'default'}`">
                <span class="pill-dot"></span>{{ statusLabel(goal.status) }}
              </span>
              <el-button size="small" text @click="openEdit(goal)">{{ t.edit }}</el-button>
              <el-button
                text
                type="danger"
                class="delete-btn"
                :title="t.delete"
                @click="removeGoal(goal)"
              >
                <el-icon><Delete /></el-icon>
              </el-button>
            </div>
          </div>
          <div
            v-if="goal.updated || (goal.task_ids && goal.task_ids.length)"
            class="row-card-meta"
          >
            <span v-if="goal.updated">{{ goal.updated }}</span>
            <span v-if="goal.task_ids && goal.task_ids.length">
              {{ t.linkedTasksPrefix }}{{ goal.task_ids.length }}{{ t.linkedTasksSuffix }}
            </span>
          </div>
        </div>
      </div>
    </el-card>

    <!-- 创建/编辑弹窗 -->
    <el-dialog v-model="dialogVisible" :title="editId ? t.editTitle : t.createTitle" width="480px">
      <el-form label-position="top" @submit.prevent>
        <el-form-item :label="t.descLabel">
          <el-input
            v-model="formDesc"
            type="textarea"
            :rows="3"
            maxlength="100"
            :placeholder="t.descPlaceholder"
          />
          <div class="char-counter">{{ descLen }}/100</div>
        </el-form-item>
        <el-form-item v-if="editId" :label="t.statusLabel">
          <el-select v-model="formStatus" class="status-select">
            <el-option
              v-for="(label, value) in t.status"
              :key="value"
              :label="label"
              :value="value"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">{{ t.cancel }}</el-button>
        <el-button type="primary" :loading="saving" @click="saveGoal">{{ t.save }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.goals-view {
  padding: 24px 28px 40px;
  max-width: 1080px;
  margin: 0 auto;
}

.view-header h1 {
  margin: 0 0 6px;
  font-size: 20px;
}

.view-desc {
  margin: 0 0 20px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}

.search-input {
  width: 260px;
}

.toolbar-right {
  display: flex;
  align-items: center;
  gap: 8px;
}

.empty-state {
  padding: 32px 0;
  text-align: center;
  color: var(--el-text-color-secondary);
}

.empty-state p {
  margin: 0 0 4px;
}

/* 行卡片结构由全局 .row-card 提供，这里只补目标特有零件 */
.goal-icon {
  font-size: 18px;
  flex-shrink: 0;
}

.delete-btn {
  padding: 4px;
  height: auto;
}

/* 快捷状态切换小圆按钮 */
.quick-statuses {
  display: flex;
  gap: 2px;
}

.quick-status {
  border: 1px solid transparent;
  background: none;
  cursor: pointer;
  font-size: 14px;
  padding: 2px 6px;
  border-radius: 999px;
  opacity: 0.45;
  transition: opacity var(--panda-transition), background-color var(--panda-transition),
    border-color var(--panda-transition);
}

.quick-status:hover {
  background: var(--el-fill-color);
  opacity: 0.8;
}

.quick-status.active {
  opacity: 1;
  border-color: var(--el-color-primary-light-5);
  background: var(--el-color-primary-light-9);
}

.char-counter {
  width: 100%;
  text-align: right;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.status-select {
  width: 200px;
}

@media (max-width: 768px) {
  .goals-view {
    padding: 16px 16px 32px;
  }

  .toolbar {
    flex-direction: column;
    align-items: stretch;
  }

  .toolbar-right {
    justify-content: flex-end;
  }

  .search-input {
    width: 100%;
  }

  /* 行首操作区窄屏允许换行，pill 与删除保持成组 */
  .row-card-right {
    flex-wrap: wrap;
    justify-content: flex-end;
    row-gap: 4px;
  }

  .status-select {
    width: 100%;
  }
}
</style>
