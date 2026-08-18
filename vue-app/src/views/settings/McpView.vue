<script setup>
// 设置 · Agents & MCP（批次 1b）：迁移旧 view-settings-mcp。
// 数据契约（api/routes/routes_sessions.py + routes_settings.py，见 dev-docs/API契约.md）：
// - Agents：GET/POST /api/agents，PUT/DELETE /api/agents/{name}
//   （旧前端读 /api/settings 的 agent_profiles/available_models —— 两个字段都不存在，恒为空白）
// - 模型选项：GET /api/models/available → {models: [...]}
// - MCP 保存：POST /api/settings 仅提交 {mcp_servers, session_id: 1}（增量语义，
//   绝不提交全量设置 —— 旧版全量提交曾清空其他配置）；提交前本地 JSON.parse 校验。
//   注意：GET /api/settings 已返回 mcp_servers（后端已补齐，可直接回显）；
//   下方加载逻辑仍保留字段存在性防御，字段缺失/为空时安全降级为空编辑器。
import { onMounted, reactive, ref } from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { cachedFetch, invalidateCache, request } from '../../api/client';
import zh from '../../i18n/zh';

const t = zh.settings.mcp;
const JSON_HEADERS = { 'Content-Type': 'application/json' };

// ═══════════════ Agents ═══════════════

const agentsLoading = ref(true);
const agents = ref([]);
const modelOptions = ref([]);

async function loadAgents() {
  agentsLoading.value = true;
  try {
    const data = await cachedFetch('/api/agents', 10000);
    agents.value = Array.isArray(data?.agents) ? data.agents : [];
  } catch (err) {
    ElMessage.error(`${t.agents.loadFailed}: ${err.message}`);
  } finally {
    agentsLoading.value = false;
  }
}

async function loadModels() {
  try {
    const data = await cachedFetch('/api/models/available', 60000);
    modelOptions.value = Array.isArray(data?.models) ? data.models : [];
  } catch {
    modelOptions.value = []; // 模型列表失败不阻塞编辑，仍可手填
  }
}

const agentDialog = reactive({
  visible: false,
  mode: 'create',       // 'create' | 'edit'
  originalName: '',
  saving: false,
});

const agentForm = reactive({
  name: '',
  prompt: '',
  model: '',
  temperature: 0.7,
  maxTokens: 4096,
});

function openCreateAgent() {
  agentDialog.mode = 'create';
  agentDialog.originalName = '';
  Object.assign(agentForm, { name: '', prompt: '', model: '', temperature: 0.7, maxTokens: 4096 });
  agentDialog.visible = true;
}

function openEditAgent(agent) {
  agentDialog.mode = 'edit';
  agentDialog.originalName = agent.name;
  Object.assign(agentForm, {
    name: agent.name || '',
    prompt: agent.prompt || '',
    model: agent.model || '',
    temperature: agent.temperature ?? 0.7,
    maxTokens: agent.max_tokens ?? 4096,
  });
  agentDialog.visible = true;
}

async function saveAgent() {
  const name = agentForm.name.trim();
  if (!name) {
    ElMessage.error(t.agents.nameRequired);
    return;
  }
  agentDialog.saving = true;
  try {
    let res;
    if (agentDialog.mode === 'create') {
      res = await request('/api/agents', {
        method: 'POST',
        headers: JSON_HEADERS,
        body: JSON.stringify({
          name,
          prompt: agentForm.prompt,
          model: agentForm.model,
          temperature: agentForm.temperature,
          max_tokens: agentForm.maxTokens,
        }),
      });
    } else {
      // PUT 语义：name 不可改，仅提交其余字段
      res = await request(`/api/agents/${encodeURIComponent(agentDialog.originalName)}`, {
        method: 'PUT',
        headers: JSON_HEADERS,
        body: JSON.stringify({
          prompt: agentForm.prompt,
          model: agentForm.model,
          temperature: agentForm.temperature,
          max_tokens: agentForm.maxTokens,
        }),
      });
    }
    if (Array.isArray(res?.agents)) agents.value = res.agents;
    invalidateCache('/api/agents');
    ElMessage.success(t.agents.saveSuccess);
    agentDialog.visible = false;
  } catch (err) {
    ElMessage.error(`${t.agents.saveFailed}: ${err.message}`);
  } finally {
    agentDialog.saving = false;
  }
}

async function removeAgent(agent) {
  try {
    await ElMessageBox.confirm(`${agent.name} — ${t.agents.deleteConfirmText}`, t.agents.deleteConfirmTitle, {
      confirmButtonText: t.agents.remove,
      cancelButtonText: t.agents.cancel,
      type: 'warning',
    });
  } catch {
    return; // 用户取消
  }
  try {
    const res = await request(`/api/agents/${encodeURIComponent(agent.name)}`, { method: 'DELETE' });
    if (Array.isArray(res?.agents)) agents.value = res.agents;
    invalidateCache('/api/agents');
    ElMessage.success(t.agents.deleteSuccess);
  } catch (err) {
    ElMessage.error(`${t.agents.deleteFailed}: ${err.message}`);
  }
}

// ═══════════════ MCP Servers ═══════════════

const mcpText = ref('');
const mcpError = ref('');
const mcpSaving = ref(false);
const mcpLoadFailed = ref(false); // 初始读取失败时禁用保存，杜绝"空白编辑器覆盖已有配置"

async function loadMcp() {
  try {
    const data = await cachedFetch('/api/settings');
    const servers = data?.mcp_servers;
    mcpText.value = servers && Object.keys(servers).length > 0
      ? JSON.stringify(servers, null, 2)
      : '';
    mcpLoadFailed.value = false;
  } catch (err) {
    mcpLoadFailed.value = true;
    ElMessage.error(`${t.servers.loadFailed}: ${err.message}`);
  }
}

async function saveMcp() {
  const raw = mcpText.value.trim();
  let parsed;
  try {
    parsed = raw ? JSON.parse(raw) : {};
  } catch (e) {
    mcpError.value = `${t.servers.jsonError}: ${e.message}`;
    return;
  }
  mcpError.value = '';
  mcpSaving.value = true;
  try {
    const res = await request('/api/settings', {
      method: 'POST',
      headers: JSON_HEADERS,
      body: JSON.stringify({ mcp_servers: parsed, session_id: 1 }),
    });
    if (res && res.status === 'success') {
      ElMessage.success(t.servers.saveSuccess);
      invalidateCache('/api/settings');
    } else {
      ElMessage.error(`${t.servers.saveFailed}: ${(res && (res.detail || res.message)) || ''}`);
    }
  } catch (err) {
    ElMessage.error(`${t.servers.saveFailed}: ${err.message}`);
  } finally {
    mcpSaving.value = false;
  }
}

onMounted(() => {
  loadAgents();
  loadModels();
  loadMcp();
});
</script>

<template>
  <div class="mcp-view">
    <header class="view-header">
      <h1>{{ t.title }}</h1>
      <p class="view-desc">{{ t.desc }}</p>
    </header>

    <!-- 智能体管理 -->
    <el-card class="settings-card" shadow="never" v-loading="agentsLoading">
      <template #header>
        <div class="card-header">
          <span class="card-title">{{ t.agents.title }}</span>
          <p class="card-desc">{{ t.agents.desc }}</p>
        </div>
      </template>

      <el-button type="primary" class="new-agent-btn" @click="openCreateAgent">{{ t.agents.new }}</el-button>

      <div v-if="!agents.length && !agentsLoading" class="empty-state">
        <div class="empty-icon">🤖</div>
        <p>{{ t.agents.empty }}</p>
      </div>

      <div v-for="a in agents" :key="a.name" class="agent-row">
        <div class="agent-info">
          <strong>{{ a.name }}</strong>
          <p v-if="a.prompt" class="agent-prompt">{{ a.prompt.substring(0, 100) }}</p>
          <span class="agent-meta">{{ t.agents.modelPrefix }}{{ a.model || t.agents.defaultModel }}</span>
        </div>
        <div class="agent-actions">
          <el-button size="small" @click="openEditAgent(a)">{{ t.agents.edit }}</el-button>
          <el-button size="small" type="danger" plain @click="removeAgent(a)">{{ t.agents.remove }}</el-button>
        </div>
      </div>
    </el-card>

    <!-- MCP Servers 配置 -->
    <el-card class="settings-card" shadow="never">
      <template #header>
        <div class="card-header">
          <span class="card-title">{{ t.servers.title }}</span>
          <p class="card-desc">{{ t.servers.desc }}</p>
        </div>
      </template>
      <el-form label-position="top">
        <el-form-item :label="t.servers.label">
          <el-input
            v-model="mcpText"
            type="textarea"
            :rows="10"
            :placeholder="t.servers.placeholder"
            spellcheck="false"
            class="mono-textarea"
          />
        </el-form-item>
      </el-form>
      <div v-if="mcpError" class="mcp-error">{{ mcpError }}</div>
      <div class="save-bar">
        <el-button type="primary" :loading="mcpSaving" :disabled="mcpLoadFailed" @click="saveMcp">{{ t.servers.save }}</el-button>
      </div>
    </el-card>

    <!-- 新建 / 编辑 Agent -->
    <el-dialog :append-to-body="true"
      v-model="agentDialog.visible"
      :title="agentDialog.mode === 'create' ? t.agents.createTitle : t.agents.editTitle"
      width="560px"
    >
      <el-form label-position="top">
        <el-form-item :label="t.agents.name">
          <el-input
            v-model="agentForm.name"
            :placeholder="t.agents.namePlaceholder"
            :disabled="agentDialog.mode === 'edit'"
          />
        </el-form-item>
        <el-form-item :label="t.agents.prompt">
          <el-input v-model="agentForm.prompt" type="textarea" :rows="5" :placeholder="t.agents.promptPlaceholder" />
        </el-form-item>
        <el-form-item :label="t.agents.model">
          <el-select v-model="agentForm.model" filterable allow-create clearable class="model-select">
            <el-option v-for="m in modelOptions" :key="m" :label="m" :value="m" />
          </el-select>
        </el-form-item>
        <div class="number-row">
          <el-form-item :label="t.agents.temperature">
            <el-input-number v-model="agentForm.temperature" :min="0" :max="2" :step="0.1" />
          </el-form-item>
          <el-form-item :label="t.agents.maxTokens">
            <el-input-number v-model="agentForm.maxTokens" :min="1" :max="200000" :step="256" />
          </el-form-item>
        </div>
      </el-form>
      <template #footer>
        <el-button @click="agentDialog.visible = false">{{ t.agents.cancel }}</el-button>
        <el-button type="primary" :loading="agentDialog.saving" @click="saveAgent">{{ t.agents.save }}</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.mcp-view {
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

.card-header .card-title {
  font-size: 15px;
  font-weight: 600;
}

.card-desc {
  margin: 6px 0 0;
  font-size: 12px;
  font-weight: 400;
  color: var(--el-text-color-secondary);
}

.new-agent-btn {
  margin-bottom: 12px;
}

.empty-state {
  padding: 24px 0;
  text-align: center;
  color: var(--el-text-color-secondary);
}

.agent-row {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 8px;
  border-top: 1px solid var(--el-border-color-lighter);
  border-radius: 8px;
  transition: background-color var(--panda-transition);
}

.agent-row:hover {
  background: var(--el-color-primary-light-9);
}

.agent-info {
  flex: 1;
  min-width: 0;
}

.agent-prompt {
  margin: 4px 0 0;
  font-size: 13px;
  color: var(--el-text-color-regular);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.agent-meta {
  display: inline-block;
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.agent-actions {
  display: flex;
  gap: 8px;
  flex-shrink: 0;
}

.model-select {
  width: 100%;
}

.number-row {
  display: flex;
  gap: 24px;
  flex-wrap: wrap;
}

.mcp-error {
  margin: 4px 0 8px;
  font-size: 13px;
  color: var(--el-color-error);
  white-space: pre-wrap;
}

.save-bar {
  display: flex;
  justify-content: flex-end;
}

.mono-textarea :deep(textarea) {
  font-family: 'Cascadia Code', Consolas, monospace;
  font-size: 13px;
}

@media (max-width: 768px) {
  .mcp-view {
    padding: 16px 16px 32px;
  }

  .agent-row {
    flex-wrap: wrap;
  }

  .agent-actions {
    margin-left: auto;
    flex-wrap: wrap;
  }

  /* 数字输入行纵向堆叠 */
  .number-row {
    flex-direction: column;
    gap: 12px;
  }
}
</style>
