<script setup>
// 设置 · 模型与服务（批次 1a + 本地模型管理恢复）：API 密钥 / 默认模型 / 自定义厂商 / 本地模型 (Llama.cpp)。
// 数据契约（dev-docs/API契约.md + api/routes/routes_settings.py）：
// - GET /api/settings 返回 api_keys_masked（xxx...xxx 掩码）与各配置字段
// - POST /api/settings 为增量语义：只提交用户实际修改的字段；掩码值（含 "..." 或以 "***" 结尾）
//   会被后端拒绝，所以 API key 输入留空 = 不修改，绝不回传 placeholder 里的掩码
// - GET /api/provider-models?provider=<p> 返回 {models: [...]}
// - 本地模型管理：GET /api/llamacpp/status（状态+下载进度）、POST /api/llamacpp/setup（装二进制）、
//   POST /api/llamacpp/search-models、/api/llamacpp/model-files、/api/llamacpp/download-from-hf、
//   /api/llamacpp/control（start/stop）；下载进度另经 WebSocket `llamacpp_download` 事件推送
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue';
import { ElMessage } from 'element-plus';
import { Refresh } from '@element-plus/icons-vue';
import { cachedFetch, invalidateCache, request } from '../../api/client';
import { useWsStore } from '../../stores/ws';
import ApiKeyRow from '../../components/ApiKeyRow.vue';
import zh from '../../i18n/zh';

const t = zh.settings.models;
const providers = zh.settings.providers;
const ws = useWsStore();

const loading = ref(true);        // 初始配置加载中（期间禁止保存，避免用默认值覆盖真实配置）
const saving = ref(false);
const modelsLoading = ref(false);

const maskedKeys = ref({});       // provider -> 掩码值（仅用于 placeholder）
const apiKeyInputs = ref({});     // provider -> 用户新输入的 key（空 = 不修改）
const modelOptions = ref([]);

// ── 自定义厂商（custom_providers）──
// GET /api/settings 读入 ref；POST 为整体替换语义。
// 添加/删除操作立即自动保存（不等页面底部统一保存），成功后清缓存重载刷新。
// api_key 为掩码（含 "..."）时原样保留回传，后端按原样存储。
const customProviders = ref([]);           // 编辑中的列表
const initialCustomProviders = ref([]);    // 初始快照，用于 dirty 判断
const cpSaving = ref(false);               // 添加/删除触发的立即保存中
const newProvider = reactive({ name: '', base_url: '', api_key: '', models: '' });
// 预置厂商名：自定义 name 不允许冲突（其模型 id 形如 <name>/<model>）
const PRESET_PROVIDER_NAMES = ['kimi', 'deepseek', 'openai', 'anthropic', 'gemini', 'glm', 'minimax', 'llamacpp', 'kimi_code', 'xiaomi'];

// 默认模型厂商下拉：静态 providers + 动态追加的自定义厂商（值 custom:<name>）
const providerOptions = computed(() => [
  ...providers.map((p) => ({ key: p.key, label: p.label })),
  ...customProviders.value.map((p) => ({
    key: `custom:${p.name}`,
    label: `${t.customProviders.optionPrefix}: ${p.name}`,
  })),
]);

// 立即把当前列表全量 POST /api/settings（整体替换），成功后清缓存重载
async function saveCustomProviders() {
  if (cpSaving.value) return;
  cpSaving.value = true;
  try {
    const res = await request('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        custom_providers: customProviders.value.map((p) => ({
          name: p.name,
          base_url: p.base_url,
          api_key: p.api_key,
          models: p.models,
        })),
      }),
    });
    if (res && res.status === 'success') {
      ElMessage.success(t.saveSuccess);
      invalidateCache('/api/settings');
      await loadSettings();
    } else {
      ElMessage.error(`${t.saveFailed}: ${(res && (res.detail || res.message)) || t.unknownError}`);
    }
  } catch (err) {
    ElMessage.error(`${t.saveFailed}: ${err.message}`);
  } finally {
    cpSaving.value = false;
  }
}

async function addCustomProvider() {
  const name = newProvider.name.trim().toLowerCase();
  const baseUrl = newProvider.base_url.trim();
  if (!name || !baseUrl) { ElMessage.warning(t.customProviders.missingFields); return; }
  if (!/^[a-z][a-z0-9_-]*$/.test(name)) { ElMessage.warning(t.customProviders.invalidName); return; }
  if (PRESET_PROVIDER_NAMES.includes(name)) {
    // xiaomi 已是预置厂商：若要用订阅 Token Plan 端点（token-plan-cn.xiaomimimo.com），换个名字如 xiaomi_plan
    ElMessage.warning(name === 'xiaomi' ? t.customProviders.xiaomiConflict : t.customProviders.nameConflict);
    return;
  }
  if (customProviders.value.some((p) => p.name === name)) { ElMessage.warning(t.customProviders.duplicate); return; }
  customProviders.value.push({
    name,
    base_url: baseUrl,
    api_key: newProvider.api_key.trim(),
    models: newProvider.models.split(',').map((s) => s.trim()).filter(Boolean),
  });
  newProvider.name = '';
  newProvider.base_url = '';
  newProvider.api_key = '';
  newProvider.models = '';
  await saveCustomProviders();
}

async function removeCustomProvider(idx) {
  customProviders.value.splice(idx, 1);
  await saveCustomProviders();
}

const form = reactive({
  provider: '',
  model: '',
  fallbackModels: '',             // 逗号分隔字符串，保存时切回数组
  llamacppCtxSize: 32768,
});

// 初始值快照（来自 GET 响应），保存时逐字段对比得出 dirty 集合
const initial = ref(null);

// 从 default_model 反推 provider（model 字符串带 litellm 前缀）：
// moonshot/xxx -> kimi，zai/xxx -> glm；无前缀的 claude*/gpt* 按关键字归属。
function providerFromModel(model) {
  if (!model) return '';
  const ml = String(model).toLowerCase();
  if (ml.startsWith('moonshot/')) return 'kimi';
  if (ml.startsWith('kimi_code/')) return 'kimi_code';
  if (ml.startsWith('deepseek/')) return 'deepseek';
  if (ml.startsWith('llamacpp/')) return 'llamacpp';
  if (ml.startsWith('gemini/')) return 'gemini';
  if (ml.startsWith('zai/')) return 'glm';
  if (ml.startsWith('minimax/')) return 'minimax';
  if (ml.startsWith('xiaomi/')) return 'xiaomi';
  if (ml.startsWith('openai/')) return 'openai';
  if (ml.startsWith('anthropic/')) return 'anthropic';
  // 自定义厂商：model 前缀（xxx/ 前段）命中已加载自定义厂商 name 时归为 custom:<name>
  const slashIdx = ml.indexOf('/');
  if (slashIdx > 0) {
    const prefix = ml.slice(0, slashIdx);
    if (customProviders.value.some((p) => p.name === prefix)) return `custom:${prefix}`;
  }
  if (ml.includes('claude')) return 'anthropic';
  if (ml.includes('gpt')) return 'openai';
  return '';
}

function applySettings(data) {
  maskedKeys.value = data.api_keys_masked || {};
  const inputs = {};
  for (const p of providers) inputs[p.key] = '';
  apiKeyInputs.value = inputs;

  // 自定义厂商先读入：下方 providerFromModel 反推依赖该列表
  const cps = Array.isArray(data.custom_providers) ? data.custom_providers : [];
  customProviders.value = cps.map((p) => ({
    name: p.name || '',
    base_url: p.base_url || '',
    api_key: p.api_key || '',
    models: Array.isArray(p.models) ? [...p.models] : [],
  }));
  initialCustomProviders.value = JSON.parse(JSON.stringify(customProviders.value));

  const init = {
    default_model: data.default_model || '',
    fallback_models: Array.isArray(data.fallback_models) ? [...data.fallback_models] : [],
    llamacpp_ctx_size: data.llamacpp_ctx_size ?? 32768,
  };
  initial.value = init;

  form.fallbackModels = init.fallback_models.join(', ');
  form.llamacppCtxSize = init.llamacpp_ctx_size;

  form.provider = providerFromModel(init.default_model);
  form.model = init.default_model;
  loadProviderModels(form.provider, { selectModel: init.default_model });
}

async function loadSettings() {
  loading.value = true;
  try {
    const data = await cachedFetch('/api/settings');
    applySettings(data || {});
  } catch (err) {
    ElMessage.error(`${t.loadFailed}: ${err.message}`);
  } finally {
    loading.value = false;
  }
}

// selectModel 不在返回列表里时前置插入，保证当前默认模型始终可见
async function loadProviderModels(provider, { selectModel = '', force = false } = {}) {
  if (!provider) {
    modelOptions.value = selectModel ? [selectModel] : [];
    return;
  }
  modelsLoading.value = true;
  try {
    const url = `/api/provider-models?provider=${encodeURIComponent(provider)}`;
    if (force) invalidateCache(url);
    const data = await cachedFetch(url, 60000);
    const models = Array.isArray(data?.models) ? [...data.models] : [];
    if (selectModel && !models.includes(selectModel)) models.unshift(selectModel);
    modelOptions.value = models;
  } catch (err) {
    modelOptions.value = selectModel ? [selectModel] : [];
    ElMessage.error(`${t.fetchModelsFailed}: ${err.message}`);
  } finally {
    modelsLoading.value = false;
  }
}

function onProviderChange(provider) {
  form.model = '';
  loadProviderModels(provider);
}

function refreshModels() {
  loadProviderModels(form.provider, { selectModel: form.model, force: true });
}

// 增量 payload：只收集与初始快照不同的字段；数字为 null（被清空）时不提交
function buildPayload() {
  const init = initial.value;
  const payload = {};

  const keys = {};
  for (const p of providers) {
    const v = (apiKeyInputs.value[p.key] || '').trim();
    if (v) keys[p.key] = v;
  }
  if (Object.keys(keys).length > 0) payload.api_keys = keys;

  // 清空默认模型选择视为「不修改」，不发送空 default_model
  if (form.model && form.model !== init.default_model) payload.default_model = form.model;

  // 自定义厂商：整体替换语义，仅在与初始快照不同时全量回传（掩码 api_key 原样保留）
  if (JSON.stringify(customProviders.value) !== JSON.stringify(initialCustomProviders.value)) {
    payload.custom_providers = customProviders.value.map((p) => ({
      name: p.name,
      base_url: p.base_url,
      api_key: p.api_key,
      models: p.models,
    }));
  }

  const fallback = form.fallbackModels.split(',').map((s) => s.trim()).filter(Boolean);
  if (JSON.stringify(fallback) !== JSON.stringify(init.fallback_models)) {
    payload.fallback_models = fallback;
  }

  if (form.llamacppCtxSize != null && form.llamacppCtxSize !== init.llamacpp_ctx_size) {
    payload.llamacpp_ctx_size = form.llamacppCtxSize;
  }

  return payload;
}

// ── 本地模型管理（Llama.cpp）──
// 状态走 GET /api/llamacpp/status（不走缓存，每次取最新）；
// 下载进行中的进度由 WebSocket `llamacpp_download` 事件实时推送，落进 llamaStatus.download。
const llamaStatus = reactive({
  installed: false,
  running: false,
  models: [],
  port: null,
  download: null,   // {active, task, label, progress, stage, error}
});
const llamaStatusLoading = ref(false);
const llamaSetupLoading = ref(false);
const llamaControlLoading = ref(false);
const llamaSelectedModel = ref('');

const llamaDownloadActive = computed(() => !!(llamaStatus.download && llamaStatus.download.active));
const llamaDownloadPercent = computed(() =>
  Math.round(((llamaStatus.download?.progress) || 0) * 100));
const llamaDownloadStatus = computed(() => {
  const stage = llamaStatus.download?.stage;
  if (stage === 'complete') return 'success';
  if (stage === 'error') return 'exception';
  return '';
});

async function refreshLlamaStatus() {
  llamaStatusLoading.value = true;
  try {
    const data = await request('/api/llamacpp/status');
    llamaStatus.installed = !!data?.installed;
    llamaStatus.running = !!data?.running;
    llamaStatus.models = Array.isArray(data?.models) ? data.models : [];
    llamaStatus.port = data?.port ?? null;
    llamaStatus.download = data?.download || null;
    // 已安装列表变化后，当前选中项失效时清空
    if (llamaSelectedModel.value && !llamaStatus.models.includes(llamaSelectedModel.value)) {
      llamaSelectedModel.value = '';
    }
  } catch (err) {
    ElMessage.error(`${t.llama.statusLoadFailed}: ${err.message}`);
  } finally {
    llamaStatusLoading.value = false;
  }
}

// WS 推送的下载进度；结束（complete/error）后补一次状态刷新（模型列表/二进制状态可能变化）
function onLlamaDownload(msg) {
  llamaStatus.download = {
    active: msg.stage === 'downloading' || msg.stage === 'extracting',
    task: msg.task || '',
    label: msg.label || '',
    progress: msg.progress ?? 0,
    stage: msg.stage || '',
    error: msg.error || '',
  };
  if (msg.stage === 'complete' || msg.stage === 'error') {
    refreshLlamaStatus();
  }
}

async function setupLlama() {
  if (llamaSetupLoading.value || llamaDownloadActive.value) return;
  llamaSetupLoading.value = true;
  try {
    await request('/api/llamacpp/setup', { method: 'POST' });
    ElMessage.success(t.llama.installStarted);
    await refreshLlamaStatus();
  } catch (err) {
    // 409 = 已有下载任务进行中
    ElMessage.error(`${t.llama.installFailed}: ${err.status === 409 ? t.llama.downloadBusy : err.message}`);
  } finally {
    llamaSetupLoading.value = false;
  }
}

async function controlLlama(action) {
  if (llamaControlLoading.value) return;
  if (action === 'start' && !llamaSelectedModel.value) {
    ElMessage.warning(t.llama.needModel);
    return;
  }
  llamaControlLoading.value = true;
  try {
    await request('/api/llamacpp/control', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(action === 'start'
        ? { action: 'start', model: llamaSelectedModel.value }
        : { action: 'stop' }),
    });
    ElMessage.success(action === 'start' ? t.llama.startSuccess : t.llama.stopSuccess);
    // 进程起停有延迟，稍等再拉状态
    setTimeout(refreshLlamaStatus, 800);
  } catch (err) {
    ElMessage.error(`${t.llama.controlFailed}: ${err.message}`);
  } finally {
    llamaControlLoading.value = false;
  }
}

// ── 模型搜索与下载 ──
const searchQuery = ref('');
const searchSource = ref('huggingface');
const searchResults = ref([]);
const searchLoading = ref(false);
const searchDone = ref(false);          // 区分「未搜索」与「搜索无结果」
const expandedRepo = ref('');           // 当前展开文件列表的 repo_id
const repoFiles = reactive({});         // repo_id -> files[]
const repoFilesLoading = ref('');       // 正在加载文件列表的 repo_id

async function searchLlamaModels() {
  const query = searchQuery.value.trim();
  if (!query || searchLoading.value) return;
  searchLoading.value = true;
  searchDone.value = false;
  expandedRepo.value = '';
  try {
    const data = await request('/api/llamacpp/search-models', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ query, source: searchSource.value }),
    });
    searchResults.value = Array.isArray(data?.models) ? data.models : [];
    searchDone.value = true;
  } catch (err) {
    ElMessage.error(`${t.llama.searchFailed}: ${err.message}`);
  } finally {
    searchLoading.value = false;
  }
}

async function toggleRepoFiles(model) {
  const repoId = model.repo_id;
  if (expandedRepo.value === repoId) {
    expandedRepo.value = '';
    return;
  }
  expandedRepo.value = repoId;
  if (repoFiles[repoId]) return;   // 已加载过，直接展开
  repoFilesLoading.value = repoId;
  try {
    const data = await request('/api/llamacpp/model-files', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repo_id: repoId, source: searchSource.value }),
    });
    repoFiles[repoId] = Array.isArray(data?.files) ? data.files : [];
  } catch (err) {
    repoFiles[repoId] = [];
    ElMessage.error(`${t.llama.filesFailed}: ${err.message}`);
  } finally {
    repoFilesLoading.value = '';
  }
}

async function downloadModelFile(model, file) {
  if (llamaDownloadActive.value) {
    ElMessage.warning(t.llama.downloadBusy);
    return;
  }
  try {
    await request('/api/llamacpp/download-from-hf', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        repo_id: model.repo_id,
        filename: file.filename,
        source: searchSource.value,
      }),
    });
    ElMessage.success(`${t.llama.downloadStarted}: ${file.filename}`);
    await refreshLlamaStatus();
  } catch (err) {
    ElMessage.error(`${t.llama.downloadFailed}: ${err.status === 409 ? t.llama.downloadBusy : err.message}`);
  }
}

async function save() {
  if (!initial.value || saving.value) return;
  const payload = buildPayload();
  if (Object.keys(payload).length === 0) {
    ElMessage.info(t.noChanges);
    return;
  }
  saving.value = true;
  try {
    const res = await request('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    if (res && res.status === 'success') {
      ElMessage.success(t.saveSuccess);
      // 保存后刷新显示：清缓存重载，掩码占位与初始快照同步更新
      invalidateCache('/api/settings');
      await loadSettings();
    } else {
      ElMessage.error(`${t.saveFailed}: ${(res && (res.detail || res.message)) || t.unknownError}`);
    }
  } catch (err) {
    ElMessage.error(`${t.saveFailed}: ${err.message}`);
  } finally {
    saving.value = false;
  }
}

let unsubLlama = null;

onMounted(() => {
  loadSettings();
  refreshLlamaStatus();
  unsubLlama = ws.on('llamacpp_download', onLlamaDownload);
});

onUnmounted(() => {
  unsubLlama?.();
});
</script>

<template>
  <div class="models-view" v-loading="loading">
    <header class="view-header">
      <h1>⚙ {{ zh.settings.title }}</h1>
      <p class="view-desc">{{ zh.settings.desc }}</p>
    </header>

    <!-- API 密钥 -->
    <el-card class="settings-card" shadow="never">
      <template #header>
        <div class="card-header">
          <span class="card-title">{{ t.apiKeys.title }}</span>
          <p class="card-desc">{{ t.apiKeys.desc }}</p>
        </div>
      </template>
      <div class="api-keys-grid">
        <ApiKeyRow
          v-for="p in providers"
          :key="p.key"
          v-model="apiKeyInputs[p.key]"
          :label="p.label"
          :masked="maskedKeys[p.key] || ''"
        />
      </div>
    </el-card>

    <!-- 自定义厂商 -->
    <el-card class="settings-card" shadow="never">
      <template #header>
        <div class="card-header">
          <span class="card-title">{{ t.customProviders.title }}</span>
          <p class="card-desc">{{ t.customProviders.desc }}</p>
        </div>
      </template>
      <div v-if="customProviders.length" class="cp-list">
        <div v-for="(p, idx) in customProviders" :key="p.name" class="cp-row">
          <div class="cp-info">
            <strong>{{ p.name }}</strong>
            <span class="cp-detail">{{ p.base_url }}</span>
            <span class="cp-detail">
              {{ t.customProviders.apiKey }}: {{ p.api_key || '—' }}
              <em v-if="String(p.api_key || '').includes('...')" class="cp-masked-hint">
                （{{ t.customProviders.maskedKeyKept }}）
              </em>
            </span>
            <span class="cp-detail">{{ t.customProviders.models }}: {{ (p.models || []).join(', ') || '—' }}</span>
          </div>
          <el-button size="small" type="danger" plain :loading="cpSaving" @click="removeCustomProvider(idx)">
            {{ t.customProviders.remove }}
          </el-button>
        </div>
      </div>
      <div v-else class="empty-state"><p>{{ t.customProviders.listEmpty }}</p></div>
      <el-divider content-position="left">{{ t.customProviders.add }}</el-divider>
      <el-form label-position="top">
        <div class="cp-form-grid">
          <el-form-item :label="t.customProviders.name">
            <el-input v-model="newProvider.name" :placeholder="t.customProviders.namePlaceholder" />
          </el-form-item>
          <el-form-item :label="t.customProviders.baseUrl">
            <el-input v-model="newProvider.base_url" :placeholder="t.customProviders.baseUrlPlaceholder" />
          </el-form-item>
          <el-form-item :label="t.customProviders.apiKey">
            <el-input v-model="newProvider.api_key" :placeholder="t.customProviders.apiKeyPlaceholder" />
          </el-form-item>
          <el-form-item :label="t.customProviders.models">
            <el-input v-model="newProvider.models" :placeholder="t.customProviders.modelsPlaceholder" />
          </el-form-item>
        </div>
        <el-button type="primary" plain :loading="cpSaving" @click="addCustomProvider">{{ t.customProviders.add }}</el-button>
        <div class="field-hint">{{ t.customProviders.saveHint }}</div>
      </el-form>
    </el-card>

    <!-- 默认模型 -->
    <el-card class="settings-card" shadow="never">
      <template #header>
        <div class="card-header">
          <span class="card-title">{{ t.defaultModel.title }}</span>
          <p class="card-desc">{{ t.defaultModel.desc }}</p>
        </div>
      </template>
      <el-form label-position="top">
        <div class="model-row">
          <el-form-item :label="t.defaultModel.provider" class="provider-item">
            <el-select v-model="form.provider" @change="onProviderChange">
              <el-option v-for="p in providerOptions" :key="p.key" :label="p.label" :value="p.key" />
            </el-select>
          </el-form-item>
          <el-form-item :label="t.defaultModel.model" class="model-item">
            <div class="model-select-row">
              <el-select
                v-model="form.model"
                filterable
                :loading="modelsLoading"
                :placeholder="t.defaultModel.selectModel"
                class="model-select"
              >
                <el-option v-for="m in modelOptions" :key="m" :label="m" :value="m" />
              </el-select>
              <el-button
                :icon="Refresh"
                :title="t.defaultModel.refresh"
                :loading="modelsLoading"
                @click="refreshModels"
              />
            </div>
          </el-form-item>
        </div>
        <el-form-item :label="t.defaultModel.fallback">
          <el-input v-model="form.fallbackModels" :placeholder="t.defaultModel.fallbackPlaceholder" />
          <div class="field-hint">{{ t.defaultModel.fallbackHint }}</div>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- 本地模型 (Llama.cpp) -->
    <el-card class="settings-card" shadow="never">
      <template #header>
        <div class="card-header">
          <span class="card-title">{{ t.llama.title }}</span>
          <p class="card-desc">{{ t.llama.manageDesc }}</p>
        </div>
      </template>

      <!-- 状态与操作 -->
      <div class="llama-status" v-loading="llamaStatusLoading">
        <div class="llama-status-row">
          <span class="llama-dot" :class="llamaStatus.installed ? 'ok' : 'err'"></span>
          <span>{{ llamaStatus.installed ? t.llama.binInstalled : t.llama.binNotInstalled }}</span>
          <span class="llama-dot" :class="llamaStatus.running ? 'ok' : 'idle'"></span>
          <span>
            {{ llamaStatus.running ? t.llama.serviceRunning.replace('{port}', llamaStatus.port) : t.llama.serviceStopped }}
          </span>
        </div>
        <div class="llama-actions">
          <el-button
            size="small"
            type="primary"
            plain
            :loading="llamaSetupLoading"
            :disabled="llamaDownloadActive"
            @click="setupLlama"
          >
            {{ t.llama.install }}
          </el-button>
          <el-button
            size="small"
            type="success"
            plain
            :loading="llamaControlLoading"
            :disabled="!llamaStatus.installed || llamaStatus.running"
            @click="controlLlama('start')"
          >
            {{ t.llama.start }}
          </el-button>
          <el-button
            size="small"
            type="danger"
            plain
            :loading="llamaControlLoading"
            :disabled="!llamaStatus.running"
            @click="controlLlama('stop')"
          >
            {{ t.llama.stop }}
          </el-button>
          <el-button size="small" :icon="Refresh" :loading="llamaStatusLoading" @click="refreshLlamaStatus">
            {{ t.llama.refresh }}
          </el-button>
        </div>
        <el-form label-position="top" class="llama-model-form">
          <el-form-item :label="t.llama.installedModels">
            <el-select
              v-model="llamaSelectedModel"
              filterable
              class="llama-model-select"
              :placeholder="t.llama.selectModel"
              :no-data-text="t.llama.noModels"
            >
              <el-option v-for="m in llamaStatus.models" :key="m" :label="m" :value="m" />
            </el-select>
          </el-form-item>
        </el-form>
      </div>

      <!-- 下载进度 -->
      <div v-if="llamaStatus.download && (llamaStatus.download.active || llamaStatus.download.stage === 'error')" class="llama-download">
        <div class="llama-download-label">{{ llamaStatus.download.label }}</div>
        <el-progress
          :percentage="llamaDownloadPercent"
          :status="llamaDownloadStatus"
        />
        <div v-if="llamaStatus.download.error" class="llama-download-error">
          {{ llamaStatus.download.error }}
        </div>
      </div>

      <el-divider content-position="left">{{ t.llama.search }}</el-divider>

      <!-- 模型搜索 -->
      <div class="llama-search-row">
        <el-input
          v-model="searchQuery"
          class="llama-search-input"
          :placeholder="t.llama.searchPlaceholder"
          clearable
          @keyup.enter="searchLlamaModels"
        />
        <el-select v-model="searchSource" class="llama-source-select">
          <el-option value="huggingface" :label="t.llama.sourceHf" />
          <el-option value="modelscope" :label="t.llama.sourceMs" />
        </el-select>
        <el-button
          type="primary"
          :loading="searchLoading"
          :disabled="!searchQuery.trim()"
          @click="searchLlamaModels"
        >
          {{ t.llama.search }}
        </el-button>
      </div>

      <!-- 搜索结果 -->
      <div v-if="searchResults.length" class="llama-results">
        <div v-for="m in searchResults" :key="m.repo_id" class="llama-result">
          <div class="llama-result-head" @click="toggleRepoFiles(m)">
            <div class="llama-result-info">
              <strong>{{ m.repo_id }}</strong>
              <span v-if="m.author" class="llama-result-meta">{{ m.author }}</span>
              <span class="llama-result-meta">
                ⬇ {{ (m.downloads || 0).toLocaleString() }} · ♥ {{ (m.likes || 0).toLocaleString() }}
              </span>
              <span v-if="m.description" class="llama-result-desc">{{ m.description }}</span>
            </div>
            <el-button size="small" text>
              {{ expandedRepo === m.repo_id ? t.llama.hideFiles : t.llama.showFiles }}
            </el-button>
          </div>
          <div v-if="expandedRepo === m.repo_id" class="llama-files">
            <div v-if="repoFilesLoading === m.repo_id" class="llama-files-hint">{{ t.llama.filesLoading }}</div>
            <template v-else-if="repoFiles[m.repo_id]?.length">
              <div v-for="f in repoFiles[m.repo_id]" :key="f.filename" class="llama-file-row">
                <span class="llama-file-name" :title="f.filename">{{ f.filename }}</span>
                <span class="llama-file-size">{{ f.size }}</span>
                <el-button
                  size="small"
                  type="primary"
                  plain
                  :disabled="llamaDownloadActive"
                  @click="downloadModelFile(m, f)"
                >
                  {{ t.llama.download }}
                </el-button>
              </div>
            </template>
            <div v-else class="llama-files-hint">{{ t.llama.filesEmpty }}</div>
          </div>
        </div>
      </div>
      <div v-else-if="searchDone && !searchLoading" class="empty-state">
        <p>{{ t.llama.searchEmpty }}</p>
      </div>

      <el-divider />

      <!-- 运行参数 -->
      <el-form label-position="top">
        <el-form-item :label="t.llama.ctxSize">
          <el-input-number v-model="form.llamacppCtxSize" :min="512" :max="262144" :step="1024" />
          <div class="field-hint">{{ t.llama.ctxHint }}</div>
        </el-form-item>
      </el-form>
    </el-card>

    <div class="save-bar">
      <el-button
        type="primary"
        size="large"
        :loading="saving"
        :disabled="loading || saving || !initial"
        @click="save"
      >
        {{ t.save }}
      </el-button>
    </div>
  </div>
</template>

<style scoped>
.models-view {
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

.api-keys-grid {
  display: grid;
  /* min() 兜底：容器窄于 460px 时单列收缩，避免中间宽度档横向溢出 */
  grid-template-columns: repeat(auto-fill, minmax(min(460px, 100%), 1fr));
  gap: 12px 24px;
}

.model-row {
  display: flex;
  gap: 16px;
  flex-wrap: wrap;
  width: 100%;
}

.provider-item {
  width: 240px;
  flex-shrink: 0;
}

.model-item {
  flex: 1;
  min-width: 320px;
}

.model-select-row {
  display: flex;
  gap: 8px;
  width: 100%;
}

.model-select {
  flex: 1;
}

.field-hint {
  width: 100%;
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--el-text-color-secondary);
}

.save-bar {
  display: flex;
  justify-content: flex-end;
  padding-bottom: 8px;
}

@media (max-width: 768px) {
  .models-view {
    padding: 16px 16px 32px;
  }

  /* 卡片网格与表单项窄屏单列堆叠 */
  .api-keys-grid {
    grid-template-columns: 1fr;
  }

  .model-row {
    flex-direction: column;
    gap: 12px;
  }

  .provider-item,
  .model-item {
    width: 100%;
    min-width: 0;
  }

  .llama-search-row {
    flex-direction: column;
    align-items: stretch;
  }

  .llama-source-select {
    width: 100%;
  }

  .llama-result-head {
    flex-direction: column;
    align-items: flex-start;
  }

  .llama-file-row {
    flex-wrap: wrap;
  }
}

/* 自定义厂商卡 */
.cp-list {
  margin-bottom: 4px;
}

.cp-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 4px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.cp-row:last-child {
  border-bottom: none;
}

.cp-info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.cp-detail {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  word-break: break-all;
}

.cp-masked-hint {
  font-style: normal;
  color: var(--el-text-color-placeholder);
}

.cp-form-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(300px, 100%), 1fr));
  gap: 0 24px;
  width: 100%;
}

/* 本地模型管理 */
.llama-status-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 12px;
  font-size: 13px;
}

.llama-dot {
  display: inline-block;
  width: 9px;
  height: 9px;
  border-radius: 50%;
  flex-shrink: 0;
}

.llama-dot.ok {
  background: var(--el-color-success);
}

.llama-dot.err {
  background: var(--el-color-danger);
}

.llama-dot.idle {
  background: var(--el-text-color-secondary);
}

.llama-actions {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}

.llama-actions .el-button + .el-button {
  margin-left: 0;
}

.llama-model-form {
  max-width: 480px;
}

.llama-model-select {
  width: 100%;
}

.llama-download {
  margin: 8px 0 4px;
  padding: 10px 12px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
}

.llama-download-label {
  font-size: 13px;
  margin-bottom: 6px;
}

.llama-download-error {
  margin-top: 6px;
  font-size: 12px;
  color: var(--el-color-danger);
}

.llama-search-row {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
}

.llama-search-input {
  flex: 1;
}

.llama-source-select {
  width: 150px;
  flex-shrink: 0;
}

.llama-result {
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  margin-bottom: 8px;
  overflow: hidden;
}

.llama-result-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  cursor: pointer;
}

.llama-result-head:hover {
  background: var(--el-fill-color-light);
}

.llama-result-info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.llama-result-meta {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.llama-result-desc {
  font-size: 12px;
  color: var(--el-text-color-placeholder);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.llama-files {
  border-top: 1px solid var(--el-border-color-lighter);
  padding: 6px 12px 10px;
}

.llama-file-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 0;
}

.llama-file-name {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.llama-file-size {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  flex-shrink: 0;
}

.llama-files-hint {
  padding: 8px 0;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}
</style>
