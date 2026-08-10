<script setup>
// 设置 · 技能管理（批次 1b）：迁移旧 view-settings-skills。
// 数据契约（dev-docs/API契约.md + api/routes/routes_skills.py）：
// - GET /api/skills → {skills: [{filename, title, size, modified, lines, enabled}]}
// - 启停：POST /api/settings 增量提交 {disabled_skills: [...]}（只提交该字段，不动其他配置）
// - 编辑保存/导入：POST /api/skills/import {filename, content, force?}
//   （旧前端编辑保存走 POST /api/skills，后端无此路由恒 405；import 才是实际写入路径。
//    import 对已存在文件直接覆盖，force 仅用于跳过 danger 级安全校验拦截）
// - 删除：DELETE /api/skills/{filename}（后端 resolve_under 校验路径穿越）
import { computed, onMounted, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { Plus, Refresh, Link } from '@element-plus/icons-vue';
import { cachedFetch, invalidateCache, request } from '../../api/client';
import zh from '../../i18n/zh';

const t = zh.settings.skills;

const loading = ref(true);
const skills = ref([]);
const search = ref('');

const filteredSkills = computed(() => {
  const q = search.value.trim().toLowerCase();
  if (!q) return skills.value;
  return skills.value.filter((s) =>
    (s.filename || '').toLowerCase().includes(q) ||
    (s.title || '').toLowerCase().includes(q));
});

async function loadSkills() {
  loading.value = true;
  try {
    const data = await cachedFetch('/api/skills', 10000);
    skills.value = Array.isArray(data?.skills) ? data.skills : [];
  } catch (err) {
    ElMessage.error(`${t.loadFailed}: ${err.message}`);
  } finally {
    loading.value = false;
  }
}

function refresh() {
  invalidateCache('/api/skills');
  loadSkills();
}

// 启停：以当前列表的 enabled 状态重建 disabled_skills（翻转目标项），增量提交。
// 失败时不动本地状态，el-switch 因 :model-value 未变而自动回弹。
async function toggleSkill(skill, enabled) {
  const disabled = skills.value
    .filter((s) => (s.filename === skill.filename ? !enabled : !s.enabled))
    .map((s) => s.filename);
  skill.toggling = true;
  try {
    await request('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ disabled_skills: disabled }),
    });
    skill.enabled = enabled;
    invalidateCache('/api/settings');
    invalidateCache('/api/skills');
  } catch (err) {
    ElMessage.error(`${t.toggleFailed}: ${err.message}`);
  } finally {
    skill.toggling = false;
  }
}

// ── 编辑 / 导入（共用 import 提交，含 danger 级校验的强制导入确认）──

function isValidFilename(name) {
  return !!name && !name.includes('..') && !/[\\/]/.test(name);
}

// 返回后端结果；用户在 danger 确认框中取消时返回 null。
async function submitSkill(filename, content, { force = false } = {}) {
  const res = await request('/api/skills/import', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filename, content, force }),
  });
  if (res && res.success === false && !force && res.validation?.level === 'danger') {
    const issues = (res.validation.issues || [])
      .map((i) => i.description || i.pattern)
      .join('\n');
    try {
      await ElMessageBox.confirm(`${res.message}\n${issues}`, t.dangerConfirmTitle, {
        confirmButtonText: t.forceImport,
        cancelButtonText: t.cancel,
        type: 'warning',
      });
    } catch {
      return null; // 用户取消
    }
    return submitSkill(filename, content, { force: true });
  }
  return res;
}

const editVisible = ref(false);
const editFilename = ref('');
const editContent = ref('');
const editLoading = ref(false);
const editSaving = ref(false);

async function openEdit(skill) {
  editFilename.value = skill.filename;
  editContent.value = '';
  editVisible.value = true;
  editLoading.value = true;
  try {
    const data = await request(`/api/skills/${encodeURIComponent(skill.filename)}`);
    editContent.value = data?.content ?? '';
  } catch (err) {
    ElMessage.error(`${t.loadContentFailed}: ${err.message}`);
    editVisible.value = false;
  } finally {
    editLoading.value = false;
  }
}

async function saveEdit() {
  if (!editContent.value.trim()) {
    ElMessage.error(t.contentRequired);
    return;
  }
  editSaving.value = true;
  try {
    const res = await submitSkill(editFilename.value, editContent.value);
    if (res === null) return;
    if (res?.success) {
      ElMessage.success(t.saveSuccess);
      editVisible.value = false;
      refresh();
    } else {
      ElMessage.error(res?.message || t.saveFailed);
    }
  } catch (err) {
    ElMessage.error(`${t.saveFailed}: ${err.message}`);
  } finally {
    editSaving.value = false;
  }
}

const importVisible = ref(false);
const importFilename = ref('');
const importContent = ref('');
const importSaving = ref(false);

function openImport() {
  importFilename.value = '';
  importContent.value = '';
  importVisible.value = true;
}

async function saveImport() {
  const filename = importFilename.value.trim();
  if (!isValidFilename(filename)) {
    ElMessage.error(t.filenameInvalid);
    return;
  }
  if (!importContent.value.trim()) {
    ElMessage.error(t.contentRequired);
    return;
  }
  importSaving.value = true;
  try {
    const res = await submitSkill(filename, importContent.value);
    if (res === null) return;
    if (res?.success) {
      ElMessage.success(t.importSuccess);
      importVisible.value = false;
      refresh();
    } else {
      ElMessage.error(res?.message || t.saveFailed);
    }
  } catch (err) {
    ElMessage.error(`${t.saveFailed}: ${err.message}`);
  } finally {
    importSaving.value = false;
  }
}

// ── 从 GitHub 安装（目录式技能包，POST /api/skills/install）──

const installVisible = ref(false);
const installUrl = ref('');
const installLoading = ref(false);

function openInstall() {
  installUrl.value = '';
  installVisible.value = true;
}

async function saveInstall() {
  const url = installUrl.value.trim();
  if (!url) {
    ElMessage.error(t.installUrlRequired);
    return;
  }
  installLoading.value = true;
  try {
    const res = await request('/api/skills/install', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ url }),
    });
    ElMessage.success(`${t.installSuccess}: ${res?.title || res?.name || ''}`);
    installVisible.value = false;
    refresh();
  } catch (err) {
    ElMessage.error(`${t.installFailed}: ${err.message}`);
  } finally {
    installLoading.value = false;
  }
}

// ── 删除 ──

async function removeSkill(skill) {
  try {
    await ElMessageBox.confirm(`${skill.filename} — ${t.deleteConfirmText}`, t.deleteConfirmTitle, {
      confirmButtonText: t.remove,
      cancelButtonText: t.cancel,
      type: 'warning',
    });
  } catch {
    return; // 用户取消
  }
  try {
    await request(`/api/skills/${encodeURIComponent(skill.filename)}`, { method: 'DELETE' });
    ElMessage.success(t.deleteSuccess);
    refresh();
  } catch (err) {
    ElMessage.error(`${t.deleteFailed}: ${err.message}`);
  }
}

function formatSize(bytes) {
  if (!bytes && bytes !== 0) return '';
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(1)} KB`;
}

onMounted(loadSkills);
</script>

<template>
  <div class="skills-view" v-loading="loading">
    <header class="view-header">
      <h1>{{ t.title }}</h1>
      <p class="view-desc">{{ t.desc }}</p>
    </header>

    <el-card class="settings-card" shadow="never">
      <div class="toolbar">
        <el-input
          v-model="search"
          :placeholder="t.searchPlaceholder"
          clearable
          class="search-input"
        />
        <span class="skills-count">{{ filteredSkills.length }}</span>
        <div class="toolbar-actions">
          <el-button :icon="Refresh" :title="t.refresh" @click="refresh" />
          <el-button :icon="Link" @click="openInstall">{{ t.installFromGithub }}</el-button>
          <el-button type="primary" :icon="Plus" @click="openImport">{{ t.import }}</el-button>
        </div>
      </div>

      <div v-if="!filteredSkills.length && !loading" class="empty-state">
        <div class="empty-icon">✨</div>
        <p>{{ t.empty }}</p>
      </div>

      <div v-for="s in filteredSkills" :key="s.filename" class="skill-row">
        <el-switch
          :model-value="s.enabled"
          :loading="s.toggling"
          class="skill-switch"
          @change="(v) => toggleSkill(s, v)"
        />
        <div class="skill-info">
          <div class="skill-name">
            <strong>{{ s.filename }}</strong>
            <span v-if="s.title && s.title !== s.filename" class="skill-title">{{ s.title }}</span>
          </div>
          <div class="skill-meta" v-if="s.lines != null">
            {{ s.lines }}{{ t.linesSuffix }} · {{ formatSize(s.size) }} · {{ (s.modified || '').slice(0, 10) }}
          </div>
          <div class="skill-meta" v-else-if="s.error">{{ s.error }}</div>
        </div>
        <div class="skill-actions">
          <el-button size="small" @click="openEdit(s)">{{ t.edit }}</el-button>
          <el-button size="small" type="danger" plain @click="removeSkill(s)">{{ t.remove }}</el-button>
        </div>
      </div>
    </el-card>

    <!-- 编辑技能 -->
    <el-dialog v-model="editVisible" :title="`${t.editTitle}: ${editFilename}`" width="720px">
      <div v-loading="editLoading">
        <el-input
          v-model="editContent"
          type="textarea"
          :rows="16"
          spellcheck="false"
          class="mono-textarea"
        />
      </div>
      <template #footer>
        <el-button @click="editVisible = false">{{ t.cancel }}</el-button>
        <el-button type="primary" :loading="editSaving" :disabled="editLoading" @click="saveEdit">
          {{ t.save }}
        </el-button>
      </template>
    </el-dialog>

    <!-- 从 GitHub 安装技能 -->
    <el-dialog v-model="installVisible" :title="t.installTitle" width="560px">
      <el-form label-position="top">
        <el-form-item :label="t.installUrlLabel">
          <el-input v-model="installUrl" :placeholder="t.installUrlPlaceholder" spellcheck="false" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="installVisible = false">{{ t.cancel }}</el-button>
        <el-button type="primary" :loading="installLoading" @click="saveInstall">{{ t.installConfirm }}</el-button>
      </template>
    </el-dialog>

    <!-- 导入技能 -->
    <el-dialog v-model="importVisible" :title="t.importTitle" width="720px">
      <el-form label-position="top">
        <el-form-item :label="t.filenameLabel">
          <el-input v-model="importFilename" :placeholder="t.filenamePlaceholder" />
        </el-form-item>
        <el-form-item :label="t.contentLabel">
          <el-input
            v-model="importContent"
            type="textarea"
            :rows="14"
            spellcheck="false"
            class="mono-textarea"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="importVisible = false">{{ t.cancel }}</el-button>
        <el-button type="primary" :loading="importSaving" @click="saveImport">{{ t.save }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.skills-view {
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

.settings-card {
  margin-bottom: 20px;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}

.search-input {
  flex: 1;
  min-width: 200px;
}

.skills-count {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.toolbar-actions {
  display: flex;
  gap: 8px;
}

.empty-state {
  padding: 32px 0;
  text-align: center;
  color: var(--el-text-color-secondary);
}

.skill-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 8px;
  border-top: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  transition: background-color var(--panda-transition);
}

.skill-row:hover {
  background: var(--el-color-primary-light-9);
}

.skill-switch {
  flex-shrink: 0;
}

.skill-info {
  flex: 1;
  min-width: 0;
}

.skill-name {
  display: flex;
  align-items: baseline;
  gap: 8px;
  flex-wrap: wrap;
}

.skill-title {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.skill-meta {
  margin-top: 2px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.skill-actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}

.mono-textarea :deep(textarea) {
  font-family: 'Cascadia Code', Consolas, monospace;
  font-size: 13px;
}

@media (max-width: 768px) {
  .skills-view {
    padding: 16px 16px 32px;
  }

  .toolbar {
    flex-wrap: wrap;
  }

  .search-input {
    min-width: 0;
  }

  /* 行内容 wrap：操作按钮挤到下一行右对齐 */
  .skill-row {
    flex-wrap: wrap;
  }

  .skill-actions {
    margin-left: auto;
    flex-wrap: wrap;
  }
}
</style>
