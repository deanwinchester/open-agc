<script setup>
// 设置 · 模型与服务（批次 1a）：迁移旧 view-settings-models 的字段。
// 数据契约（dev-docs/API契约.md + api/routes/routes_settings.py）：
// - GET /api/settings 返回 api_keys_masked（xxx...xxx 掩码）与各配置字段
// - POST /api/settings 为增量语义：只提交用户实际修改的字段；掩码值（含 "..." 或以 "***" 结尾）
//   会被后端拒绝，所以 API key 输入留空 = 不修改，绝不回传 placeholder 里的掩码
// - GET /api/provider-models?provider=<p> 返回 {models: [...]}
import { computed, onMounted, reactive, ref } from 'vue';
import { ElMessage } from 'element-plus';
import { Refresh } from '@element-plus/icons-vue';
import { cachedFetch, invalidateCache, request } from '../../api/client';
import ApiKeyRow from '../../components/ApiKeyRow.vue';
import zh from '../../i18n/zh';

const t = zh.settings.models;
const providers = zh.settings.providers;

const loading = ref(true);        // 初始配置加载中（期间禁止保存，避免用默认值覆盖真实配置）
const saving = ref(false);
const modelsLoading = ref(false);

const maskedKeys = ref({});       // provider -> 掩码值（仅用于 placeholder）
const apiKeyInputs = ref({});     // provider -> 用户新输入的 key（空 = 不修改）
const modelOptions = ref([]);

const form = reactive({
  provider: '',
  model: '',
  fallbackModels: '',             // 逗号分隔字符串，保存时切回数组
  sandboxMode: true,
  sandboxDir: '',
  llamacppCtxSize: 32768,
  browserHeadless: false,
  httpProxy: '',
  heartbeatEnabled: false,
  heartbeatInterval: 60,
  searxngUrl: '',
  maxCorrectionAttempts: 5,
  coldCacheTtl: 3600,
  maxResumeCount: 10,
  maxTotalTokens: 128000,
});

// 初始值快照（来自 GET 响应），保存时逐字段对比得出 dirty 集合
const initial = ref(null);

// 巡视间隔下拉：预设档位 + 当前值（若不在预设中，动态补一项，避免回显丢失）
const intervalOptions = computed(() => {
  const base = t.heartbeat.intervalOptions;
  const v = form.heartbeatInterval;
  if (v != null && !base.some((o) => o.value === v)) {
    return [...base, { value: v, label: `${v}${t.heartbeat.secondsSuffix}` }]
      .sort((a, b) => a.value - b.value);
  }
  return base;
});

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
  if (ml.startsWith('openai/')) return 'openai';
  if (ml.startsWith('anthropic/')) return 'anthropic';
  if (ml.includes('claude')) return 'anthropic';
  if (ml.includes('gpt')) return 'openai';
  return '';
}

function applySettings(data) {
  maskedKeys.value = data.api_keys_masked || {};
  const inputs = {};
  for (const p of providers) inputs[p.key] = '';
  apiKeyInputs.value = inputs;

  const init = {
    default_model: data.default_model || '',
    fallback_models: Array.isArray(data.fallback_models) ? [...data.fallback_models] : [],
    sandbox_mode: data.sandbox_mode ?? true,
    sandbox_dir: data.sandbox_dir || '',
    llamacpp_ctx_size: data.llamacpp_ctx_size ?? 32768,
    browser_headless: data.browser_headless ?? false,
    http_proxy: data.http_proxy || '',
    heartbeat_enabled: data.heartbeat_enabled ?? false,
    heartbeat_interval: data.heartbeat_interval ?? 60,
    searxng_url: data.searxng_url || '',
    max_correction_attempts: data.max_correction_attempts ?? 5,
    cold_cache_ttl: data.cold_cache_ttl ?? 3600,
    max_resume_count: data.max_resume_count ?? 10,
    max_total_tokens: data.context_budget?.max_total_tokens ?? 128000,
  };
  initial.value = init;

  form.fallbackModels = init.fallback_models.join(', ');
  form.sandboxMode = init.sandbox_mode;
  form.sandboxDir = init.sandbox_dir;
  form.llamacppCtxSize = init.llamacpp_ctx_size;
  form.browserHeadless = init.browser_headless;
  form.httpProxy = init.http_proxy;
  form.heartbeatEnabled = init.heartbeat_enabled;
  form.heartbeatInterval = init.heartbeat_interval;
  form.searxngUrl = init.searxng_url;
  form.maxCorrectionAttempts = init.max_correction_attempts;
  form.coldCacheTtl = init.cold_cache_ttl;
  form.maxResumeCount = init.max_resume_count;
  form.maxTotalTokens = init.max_total_tokens;

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

  const fallback = form.fallbackModels.split(',').map((s) => s.trim()).filter(Boolean);
  if (JSON.stringify(fallback) !== JSON.stringify(init.fallback_models)) {
    payload.fallback_models = fallback;
  }

  if (form.sandboxMode !== init.sandbox_mode) payload.sandbox_mode = form.sandboxMode;
  if (form.sandboxDir.trim() !== init.sandbox_dir) payload.sandbox_dir = form.sandboxDir.trim();
  if (form.llamacppCtxSize != null && form.llamacppCtxSize !== init.llamacpp_ctx_size) {
    payload.llamacpp_ctx_size = form.llamacppCtxSize;
  }
  if (form.browserHeadless !== init.browser_headless) payload.browser_headless = form.browserHeadless;
  if (form.httpProxy.trim() !== init.http_proxy) payload.http_proxy = form.httpProxy.trim();
  if (form.heartbeatEnabled !== init.heartbeat_enabled) payload.heartbeat_enabled = form.heartbeatEnabled;
  if (form.heartbeatInterval != null && form.heartbeatInterval !== init.heartbeat_interval) {
    payload.heartbeat_interval = form.heartbeatInterval;
  }
  if (form.searxngUrl.trim() !== init.searxng_url) payload.searxng_url = form.searxngUrl.trim();
  if (form.maxCorrectionAttempts != null && form.maxCorrectionAttempts !== init.max_correction_attempts) {
    payload.max_correction_attempts = form.maxCorrectionAttempts;
  }
  if (form.coldCacheTtl != null && form.coldCacheTtl !== init.cold_cache_ttl) {
    payload.cold_cache_ttl = form.coldCacheTtl;
  }
  if (form.maxResumeCount != null && form.maxResumeCount !== init.max_resume_count) {
    payload.max_resume_count = form.maxResumeCount;
  }
  if (form.maxTotalTokens != null && form.maxTotalTokens !== init.max_total_tokens) {
    payload.max_total_tokens = form.maxTotalTokens;
  }

  return payload;
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

onMounted(loadSettings);
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
              <el-option v-for="p in providers" :key="p.key" :label="p.label" :value="p.key" />
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
          <p class="card-desc">{{ t.llama.desc }}</p>
        </div>
      </template>
      <el-form label-position="top">
        <el-form-item :label="t.llama.ctxSize">
          <el-input-number v-model="form.llamacppCtxSize" :min="512" :max="262144" :step="1024" />
          <div class="field-hint">{{ t.llama.ctxHint }}</div>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- 后台巡视 -->
    <el-card class="settings-card" shadow="never">
      <template #header>
        <div class="card-header">
          <span class="card-title">{{ t.heartbeat.title }}</span>
          <p class="card-desc">{{ t.heartbeat.desc }}</p>
        </div>
      </template>
      <el-form label-position="top">
        <el-form-item :label="t.heartbeat.enable">
          <el-switch v-model="form.heartbeatEnabled" />
        </el-form-item>
        <el-form-item :label="t.heartbeat.interval">
          <el-select v-model="form.heartbeatInterval" class="interval-select">
            <el-option v-for="o in intervalOptions" :key="o.value" :label="o.label" :value="o.value" />
          </el-select>
        </el-form-item>
        <el-form-item :label="t.heartbeat.correction">
          <el-input-number v-model="form.maxCorrectionAttempts" :min="0" :max="100" />
          <div class="field-hint">{{ t.heartbeat.correctionHint }}</div>
        </el-form-item>
        <el-form-item :label="t.heartbeat.coldTtl">
          <el-input-number v-model="form.coldCacheTtl" :min="60" :max="86400" :step="60" />
          <div class="field-hint">{{ t.heartbeat.coldTtlHint }}</div>
        </el-form-item>
        <el-form-item :label="t.heartbeat.budget">
          <el-input-number v-model="form.maxTotalTokens" :min="16000" :max="1048576" :step="16000" />
          <div class="field-hint">{{ t.heartbeat.budgetHint }}</div>
        </el-form-item>
        <el-form-item :label="t.heartbeat.resume">
          <el-input-number v-model="form.maxResumeCount" :min="0" :max="100" />
          <div class="field-hint">{{ t.heartbeat.resumeHint }}</div>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- 沙箱与网络 -->
    <el-card class="settings-card" shadow="never">
      <template #header>
        <div class="card-header">
          <span class="card-title">{{ t.sandbox.title }}</span>
          <p class="card-desc">{{ t.sandbox.desc }}</p>
        </div>
      </template>
      <el-form label-position="top">
        <el-form-item :label="t.sandbox.mode">
          <el-switch v-model="form.sandboxMode" />
        </el-form-item>
        <el-form-item :label="t.sandbox.dir">
          <el-input v-model="form.sandboxDir" :placeholder="t.sandbox.dirPlaceholder" />
          <div class="field-hint">{{ t.sandbox.dirHint }}</div>
        </el-form-item>
        <el-form-item :label="t.sandbox.proxy">
          <el-input v-model="form.httpProxy" :placeholder="t.sandbox.proxyPlaceholder" />
          <div class="field-hint">{{ t.sandbox.proxyHint }}</div>
        </el-form-item>
        <el-form-item :label="t.sandbox.headless">
          <el-switch v-model="form.browserHeadless" />
          <div class="field-hint">{{ t.sandbox.headlessHint }}</div>
        </el-form-item>
        <el-form-item :label="t.sandbox.searxng">
          <el-input v-model="form.searxngUrl" :placeholder="t.sandbox.searxngPlaceholder" />
          <div class="field-hint">{{ t.sandbox.searxngHint }}</div>
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

.interval-select {
  width: 180px;
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
  .model-item,
  .interval-select {
    width: 100%;
    min-width: 0;
  }
}
</style>
