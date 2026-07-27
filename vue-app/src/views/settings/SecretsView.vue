<script setup>
// 设置 · 凭证库（入口 B 管理面）：列表（掩码视图）/ 删除 / 新增 / 编辑。
// 数据契约（api/routes/routes_secrets.py + core/secrets.py）：
// - GET /api/secrets 返回掩码列表（name/type/host/username_masked/note，绝不含密码）
// - POST /api/secrets 为 upsert：name 校验 ^[A-Za-z0-9_-]+$；
//   password 缺省（不传）= 保留旧值，显式 "" = 清空 —— 编辑时留空即不修改
// - DELETE /api/secrets/{name} 删除
import { onMounted, reactive, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { request } from '../../api/client';
import zh from '../../i18n/zh';

const t = zh.settings.secrets;

const SECRET_TYPES = ['mongodb', 'mysql', 'postgres', 'redis', 'api_key', 'generic'];
const SECRET_NAME_RE = /^[A-Za-z0-9_-]+$/;

const loading = ref(false);
const saving = ref(false);
const secrets = ref([]);

async function loadSecrets() {
  loading.value = true;
  try {
    const data = await request('/api/secrets');
    secrets.value = Array.isArray(data?.secrets) ? data.secrets : [];
  } catch (err) {
    ElMessage.error(`${t.loadFailed}: ${err.message}`);
  } finally {
    loading.value = false;
  }
}

// ── 新增 / 编辑弹窗 ──
const dialogOpen = ref(false);
const dialogMode = ref('add'); // 'add' | 'edit'
const form = reactive({
  name: '', type: 'generic', host: '', port: '', database: '', username: '', password: '', note: '',
});

function openAdd() {
  dialogMode.value = 'add';
  form.name = '';
  form.type = 'generic';
  form.host = '';
  form.port = '';
  form.database = '';
  form.username = '';
  form.password = '';
  form.note = '';
  dialogOpen.value = true;
}

function openEdit(row) {
  dialogMode.value = 'edit';
  form.name = row.name; // 名称即主键，编辑态只读
  form.password = ''; // 留空 = 保留旧值（后端 password=None 语义）
  form.note = row.note || '';
  dialogOpen.value = true;
}

async function save() {
  if (saving.value) return;
  const name = form.name.trim();
  if (!name) { ElMessage.warning(t.nameRequired); return; }
  if (!SECRET_NAME_RE.test(name)) { ElMessage.warning(t.nameInvalid); return; }
  if (dialogMode.value === 'add' && !form.password) {
    ElMessage.warning(t.passwordRequired);
    return;
  }
  const body = { name, note: form.note };
  if (dialogMode.value === 'add') {
    body.type = form.type;
    body.host = form.host.trim();
    body.port = form.port.trim();
    body.database = form.database.trim();
    body.username = form.username.trim();
  }
  // 编辑态只提交 note（+可选密码）：type/host/username 不传 = 后端保留旧值。
  // 密码留空 → 不传该字段；新增已在上方校验必填，不会走到这
  if (form.password) body.password = form.password;
  saving.value = true;
  try {
    await request('/api/secrets', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    ElMessage.success(t.saveSuccess);
    dialogOpen.value = false;
    await loadSecrets();
  } catch (err) {
    ElMessage.error(`${t.saveFailed}: ${err.message}`);
  } finally {
    saving.value = false;
  }
}

async function remove(row) {
  try {
    await ElMessageBox.confirm(t.deleteConfirmText, t.deleteConfirmTitle, {
      confirmButtonText: t.remove,
      cancelButtonText: t.cancel,
      type: 'warning',
    });
  } catch { return; } // 用户取消
  try {
    await request(`/api/secrets/${encodeURIComponent(row.name)}`, { method: 'DELETE' });
    ElMessage.success(t.deleteSuccess);
    await loadSecrets();
  } catch (err) {
    ElMessage.error(`${t.deleteFailed}: ${err.message}`);
  }
}

onMounted(loadSecrets);
</script>

<template>
  <div class="secrets-view" v-loading="loading">
    <header class="view-header">
      <h1>{{ t.title }}</h1>
      <p class="view-desc">{{ t.desc }}</p>
    </header>

    <el-card class="settings-card" shadow="never">
      <template #header>
        <div class="card-header">
          <el-button type="primary" @click="openAdd">{{ t.add }}</el-button>
          <el-button @click="loadSecrets">{{ t.refresh }}</el-button>
        </div>
      </template>

      <el-table :data="secrets" class="secrets-table">
        <el-table-column prop="name" :label="t.colName" min-width="140" />
        <el-table-column prop="type" :label="t.colType" width="110" />
        <el-table-column prop="host" :label="t.colHost" min-width="140">
          <template #default="{ row }">{{ row.host || '—' }}</template>
        </el-table-column>
        <el-table-column prop="database" :label="t.colDatabase" min-width="100">
          <template #default="{ row }">{{ row.database || '—' }}</template>
        </el-table-column>
        <el-table-column prop="username_masked" :label="t.colUsername" width="110">
          <template #default="{ row }">{{ row.username_masked || '—' }}</template>
        </el-table-column>
        <el-table-column prop="note" :label="t.colNote" min-width="140">
          <template #default="{ row }">{{ row.note || '—' }}</template>
        </el-table-column>
        <el-table-column :label="t.colActions" width="150" fixed="right">
          <template #default="{ row }">
            <el-button size="small" @click="openEdit(row)">{{ t.edit }}</el-button>
            <el-button size="small" type="danger" @click="remove(row)">{{ t.remove }}</el-button>
          </template>
        </el-table-column>
        <template #empty>{{ t.empty }}</template>
      </el-table>
    </el-card>

    <el-dialog
      v-model="dialogOpen"
      :title="dialogMode === 'add' ? t.addTitle : t.editTitle"
      width="480px"
    >
      <el-form label-position="top">
        <el-form-item :label="t.nameLabel">
          <el-input
            v-model="form.name"
            :placeholder="t.namePlaceholder"
            :disabled="dialogMode === 'edit'"
            autocomplete="off"
          />
        </el-form-item>
        <template v-if="dialogMode === 'add'">
          <el-form-item :label="t.typeLabel">
            <el-select v-model="form.type" class="field-full">
              <el-option v-for="st in SECRET_TYPES" :key="st" :label="st" :value="st" />
            </el-select>
          </el-form-item>
          <el-form-item :label="t.hostLabel">
            <el-input v-model="form.host" autocomplete="off" />
          </el-form-item>
          <el-form-item :label="t.portLabel">
            <el-input v-model="form.port" autocomplete="off" />
          </el-form-item>
          <el-form-item :label="t.databaseLabel">
            <el-input v-model="form.database" autocomplete="off" />
          </el-form-item>
          <el-form-item :label="t.usernameLabel">
            <el-input v-model="form.username" autocomplete="off" />
          </el-form-item>
        </template>
        <el-form-item :label="t.passwordLabel">
          <el-input
            v-model="form.password"
            type="password"
            show-password
            autocomplete="new-password"
          />
          <div v-if="dialogMode === 'edit'" class="field-hint">{{ t.passwordKeepHint }}</div>
        </el-form-item>
        <el-form-item :label="t.noteLabel">
          <el-input v-model="form.note" type="textarea" :rows="2" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogOpen = false">{{ t.cancel }}</el-button>
        <el-button type="primary" :loading="saving" @click="save">{{ t.save }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.secrets-view {
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

.card-header {
  display: flex;
  gap: 8px;
}

.secrets-table {
  width: 100%;
}

.field-full {
  width: 100%;
}

.field-hint {
  width: 100%;
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}

@media (max-width: 768px) {
  .secrets-view {
    padding: 16px 16px 32px;
  }
}
</style>
