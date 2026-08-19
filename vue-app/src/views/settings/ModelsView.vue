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

// ── 自定义厂商（custom_providers）──
// GET /api/settings 读入 ref；POST 为整体替换语义：保存时按当前列表全量回传。
// api_key 为掩码（含 "..."）时原样保留回传，后端按原样存储。
const customProviders = ref([]);           // 编辑中的列表
const initialCustomProviders = ref([]);    // 初始快照，用于 dirty 判断
const newProvider = reactive({ name: '', base_url: '', api_key: '', models: '' });
// 预置厂商名：自定义 name 不允许冲突（其模型 id 形如 <name>/<model>）
const PRESET_PROVIDER_NAMES = ['kimi', 'deepseek', 'openai', 'anthropic', 'gemini', 'glm', 'minimax', 'llamacpp', 'kimi_code'];

// 默认模型厂商下拉：静态 providers + 动态追加的自定义厂商（值 custom:<name>）
const providerOptions = computed(() => [
  ...providers.map((p) => ({ key: p.key, label: p.label })),
  ...customProviders.value.map((p) => ({
    key: `custom:${p.name}`,
    label: `${t.customProviders.optionPrefix}: ${p.name}`,
  })),
]);

function addCustomProvider() {
  const name = newProvider.name.trim().toLowerCase();
  const baseUrl = newProvider.base_url.trim();
  if (!name || !baseUrl) { ElMessage.warning(t.customProviders.missingFields); return; }
  if (!/^[a-z][a-z0-9_-]*$/.test(name)) { ElMessage.warning(t.customProviders.invalidName); return; }
  if (PRESET_PROVIDER_NAMES.includes(name)) { ElMessage.warning(t.customProviders.nameConflict); return; }
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
}

function removeCustomProvider(idx) {
  customProviders.value.splice(idx, 1);
}

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
  // 沙箱自动清理（sandbox_janitor 节）
  janitorEnabled: true,
  janitorTtlDays: 7,
  janitorIntervalHours: 1,
  janitorSoftGb: 20,
  janitorHardGb: 50,
  // 调度者（分身）模式
  dispatcherMode: false,
  agentWorkerName: '分身',
  // 访问控制：局域网访问密码（空 = 不修改；勾选清除 = 恢复仅本机）
  accessPassword: '',
  accessPasswordClear: false,
  // 邮件监听与助手（重构时丢失的区块，恢复）
  emailListenerEnabled: false,
  ownerEmail: '',
  emailAccount: '',
  emailPassword: '',      // 仅保存用户新输入；GET 返回 *** 不回填
  emailImap: '',
  emailSmtp: '',
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
    sandbox_janitor: {
      enabled: data.sandbox_janitor?.enabled ?? true,
      tmp_ttl_days: data.sandbox_janitor?.tmp_ttl_days ?? 7,
      interval_hours: data.sandbox_janitor?.interval_hours ?? 1,
      soft_gb: data.sandbox_janitor?.soft_gb ?? 20,
      hard_gb: data.sandbox_janitor?.hard_gb ?? 50,
    },
    access_password_set: data.access_password_set ?? false,
    dispatcher_mode: data.dispatcher_mode ?? false,
    agent_worker_name: data.agent_worker_name || '分身',
    email_listener_enabled: data.email_listener_enabled ?? false,
    owner_email: data.owner_email || '',
    email_account: data.email_account || '',
    email_imap_server: data.email_imap_server || '',
    email_smtp_server: data.email_smtp_server || '',
    email_password_set: data.email_password === '***',
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
  form.janitorEnabled = init.sandbox_janitor.enabled;
  form.janitorTtlDays = init.sandbox_janitor.tmp_ttl_days;
  form.janitorIntervalHours = init.sandbox_janitor.interval_hours;
  form.janitorSoftGb = init.sandbox_janitor.soft_gb;
  form.janitorHardGb = init.sandbox_janitor.hard_gb;
  form.accessPassword = '';          // 已设置显示占位，不回填真实值
  form.accessPasswordClear = false;
  form.dispatcherMode = init.dispatcher_mode;
  form.agentWorkerName = init.agent_worker_name;
  form.emailListenerEnabled = init.email_listener_enabled;
  form.ownerEmail = init.owner_email;
  form.emailAccount = init.email_account;
  form.emailImap = init.email_imap_server;
  form.emailSmtp = init.email_smtp_server;
  form.emailPassword = '';   // 已设置显示占位，不回填真实值

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

  // sandbox_janitor：逐键对比，只发变化键（后端白名单合并进配置节）
  const jInit = init.sandbox_janitor;
  const jCur = {
    enabled: form.janitorEnabled,
    tmp_ttl_days: form.janitorTtlDays,
    interval_hours: form.janitorIntervalHours,
    soft_gb: form.janitorSoftGb,
    hard_gb: form.janitorHardGb,
  };
  const jDiff = {};
  for (const [k, v] of Object.entries(jCur)) {
    if (v != null && v !== jInit[k]) jDiff[k] = v;
  }
  if (Object.keys(jDiff).length > 0) payload.sandbox_janitor = jDiff;

  // 访问控制密码：输入新值 = 设置/覆盖；未输入且勾选清除 = 恢复仅本机
  const ap = form.accessPassword.trim();
  if (ap) payload.access_password = ap;
  else if (form.accessPasswordClear && init.access_password_set) payload.access_password = '';

  // 调度者（分身）模式：仅在与初始值不同时提交；叫法留空视为「不修改」
  if (form.dispatcherMode !== init.dispatcher_mode) payload.dispatcher_mode = form.dispatcherMode;
  const wn = form.agentWorkerName.trim();
  if (wn && wn !== init.agent_worker_name) payload.agent_worker_name = wn;

  // 邮件监听与助手：密码仅用户新输入时才发送（*** 掩码不回传）
  if (form.emailListenerEnabled !== init.email_listener_enabled) payload.email_listener_enabled = form.emailListenerEnabled;
  if (form.ownerEmail.trim() !== init.owner_email) payload.owner_email = form.ownerEmail.trim();
  if (form.emailAccount.trim() !== init.email_account) payload.email_account = form.emailAccount.trim();
  if (form.emailImap.trim() !== init.email_imap_server) payload.email_imap_server = form.emailImap.trim();
  if (form.emailSmtp.trim() !== init.email_smtp_server) payload.email_smtp_server = form.emailSmtp.trim();
  if (form.emailPassword.trim()) payload.email_password = form.emailPassword.trim();

  return payload;
}

// ── 界面主题：导出/导入/主题市场 ──
const themeInfo = ref({});
const themeMarket = ref([]);
const themeMarketLoading = ref(false);
const themeImportVisible = ref(false);
const themeImportText = ref('');
const themeImporting = ref(false);

const themeSummary = computed(() => {
  const th = themeInfo.value || {};
  const parts = [];
  if (!th.primary_color && !th.sidebar_color && !th.logo_url && !th.chat_bg_url
      && !th.glass && !th.bordered && !th.animations && (th.decor || 'none') === 'none'
      && !th.custom_css) return t.theme.summaryDefault;
  if (th.primary_color) parts.push(`${t.theme.summaryColor} ${th.primary_color}`);
  if (th.sidebar_color) parts.push(`${t.theme.summarySidebar} ${th.sidebar_color}`);
  if (th.logo_url) parts.push(t.theme.summaryLogo);
  if (th.chat_bg_url) parts.push(t.theme.summaryBg);
  for (const [k, label] of [['glass', t.theme.fx.glass], ['bordered', t.theme.fx.bordered],
                            ['animations', t.theme.fx.animations]]) {
    if (th[k]) parts.push(label);
  }
  if (th.decor && th.decor !== 'none') parts.push(t.theme.fx[th.decor] || th.decor);
  if (th.custom_css) parts.push(t.theme.summaryCustomCss);
  return parts.join(' · ');
});

async function loadThemeInfo() {
  try { themeInfo.value = await request('/api/theme'); } catch { /* 忽略 */ }
}

async function loadThemeMarket() {
  themeMarketLoading.value = true;
  try {
    const data = await request('/api/theme/market');
    themeMarket.value = (data?.themes || []).map((x) => ({ ...x, applying: false }));
  } catch (err) {
    ElMessage.error(`${t.theme.marketFailed}: ${err.message}`);
  } finally {
    themeMarketLoading.value = false;
  }
}

async function exportTheme() {
  // 走后端导出：主题包内嵌 Logo/背景图（base64）与自定义 CSS，好友导入即完整还原
  try {
    const pkg = await request('/api/theme/export');
    const blob = new Blob([JSON.stringify(pkg, null, 2)], { type: 'application/json' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `open-agc-theme-${Date.now()}.json`;
    a.click();
    URL.revokeObjectURL(a.href);
    ElMessage.success(t.theme.exportSuccess);
  } catch (err) {
    ElMessage.error(`${t.theme.exportFailed}: ${err.message}`);
  }
}

async function importTheme() {
  if (!themeImportText.value.trim() || themeImporting.value) return;
  themeImporting.value = true;
  try {
    const parsed = JSON.parse(themeImportText.value);
    const theme = parsed.theme || parsed;
    const res = await request('/api/theme', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ theme }),
    });
    if (res && res.status === 'success') {
      ElMessage.success(t.theme.importSuccess);
      themeImportVisible.value = false;
      themeImportText.value = '';
      // 导入后整页刷新，保证全部样式面生效（用户反馈：局部热更新不完整）
      setTimeout(() => location.reload(), 400);
    } else {
      ElMessage.error(`${t.theme.importFailed}: ${(res && res.detail) || t.unknownError}`);
    }
  } catch (err) {
    ElMessage.error(`${t.theme.importFailed}: ${err.message}`);
  } finally {
    themeImporting.value = false;
  }
}

async function applyMarketTheme(th) {
  th.applying = true;
  try {
    const res = await request('/api/theme', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ theme: th.theme }),
    });
    if (res && res.status === 'success') {
      ElMessage.success(`${t.theme.applySuccess}: ${th.name}`);
      setTimeout(() => location.reload(), 400);
    } else {
      ElMessage.error(`${t.theme.applyFailed}: ${(res && res.detail) || t.unknownError}`);
    }
  } catch (err) {
    ElMessage.error(`${t.theme.applyFailed}: ${err.message}`);
  } finally {
    th.applying = false;
  }
}

async function resetTheme() {
  try {
    await ElMessageBox.confirm(t.theme.resetConfirmText, t.theme.resetConfirmTitle,
      { confirmButtonText: t.theme.reset, cancelButtonText: zh.goals.cancel, type: 'warning' });
  } catch { return; }
  try {
    await request('/api/theme', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: 'replace', theme: {} }),
    });
    ElMessage.success(t.theme.resetSuccess);
    setTimeout(() => location.reload(), 400);
  } catch (err) {
    ElMessage.error(`${t.theme.resetFailed}: ${err.message}`);
  }
}

// ── 界面主题（ui_theme）结束 ──

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

onMounted(() => {
  loadSettings();
  loadThemeInfo();
  loadThemeMarket();
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
          <el-button size="small" type="danger" plain @click="removeCustomProvider(idx)">
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
        <el-button type="primary" plain @click="addCustomProvider">{{ t.customProviders.add }}</el-button>
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

    <!-- 调度者（分身）模式 -->
    <el-card class="settings-card" shadow="never">
      <template #header>
        <div class="card-header">
          <span class="card-title">{{ t.dispatcher.title }}</span>
          <p class="card-desc">{{ t.dispatcher.desc }}</p>
        </div>
      </template>
      <el-form label-position="top">
        <el-form-item :label="t.dispatcher.enable">
          <el-switch v-model="form.dispatcherMode" />
          <div class="field-hint">{{ t.dispatcher.enableHint }}</div>
        </el-form-item>
        <el-form-item :label="t.dispatcher.workerName">
          <el-input
            v-model="form.agentWorkerName"
            class="worker-name-input"
            :placeholder="t.dispatcher.workerNamePlaceholder"
          />
          <div class="field-hint">{{ t.dispatcher.workerNameHint }}</div>
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
        <el-divider content-position="left">{{ t.sandbox.janitorTitle }}</el-divider>
        <el-form-item :label="t.sandbox.janitorEnabled">
          <el-switch v-model="form.janitorEnabled" />
          <div class="field-hint">{{ t.sandbox.janitorEnabledHint }}</div>
        </el-form-item>
        <template v-if="form.janitorEnabled">
          <el-form-item :label="t.sandbox.janitorTtl">
            <el-input-number v-model="form.janitorTtlDays" :min="0" :max="365" />
            <div class="field-hint">{{ t.sandbox.janitorTtlHint }}</div>
          </el-form-item>
          <el-form-item :label="t.sandbox.janitorInterval">
            <el-input-number v-model="form.janitorIntervalHours" :min="0.1" :max="168" :step="0.5" />
            <div class="field-hint">{{ t.sandbox.janitorIntervalHint }}</div>
          </el-form-item>
          <el-form-item :label="t.sandbox.janitorSoft">
            <el-input-number v-model="form.janitorSoftGb" :min="0" :max="100000" />
            <div class="field-hint">{{ t.sandbox.janitorSoftHint }}</div>
          </el-form-item>
          <el-form-item :label="t.sandbox.janitorHard">
            <el-input-number v-model="form.janitorHardGb" :min="0" :max="100000" />
            <div class="field-hint">{{ t.sandbox.janitorHardHint }}</div>
          </el-form-item>
        </template>
      </el-form>
    </el-card>

    <!-- 访问控制 -->
    <el-card class="settings-card" shadow="never">
      <template #header>
        <div class="card-header">
          <span class="card-title">{{ t.access.title }}</span>
          <p class="card-desc">{{ t.access.desc }}</p>
        </div>
      </template>
      <el-form label-position="top">
        <el-form-item :label="t.access.password">
          <el-input
            v-model="form.accessPassword"
            type="password"
            show-password
            :placeholder="initial?.access_password_set ? t.access.passwordSetPlaceholder : t.access.passwordPlaceholder"
          />
          <div class="field-hint">{{ t.access.passwordHint }}</div>
        </el-form-item>
        <el-form-item v-if="initial?.access_password_set">
          <el-checkbox v-model="form.accessPasswordClear" :disabled="!!form.accessPassword.trim()">
            {{ t.access.clear }}
          </el-checkbox>
        </el-form-item>
        <div class="field-hint">{{ t.access.policyHint }}</div>
      </el-form>
    </el-card>

    <!-- 界面主题：导出/导入/主题市场 -->
    <el-card class="settings-card" shadow="never">
      <template #header>
        <div class="card-header">
          <span class="card-title">{{ t.theme.title }}</span>
          <p class="card-desc">{{ t.theme.desc }}</p>
        </div>
      </template>
      <div class="theme-current">
        <span class="theme-label">{{ t.theme.current }}:</span>
        <span v-if="themeInfo.primary_color" class="color-chip" :style="{ background: themeInfo.primary_color }" :title="themeInfo.primary_color"></span>
        <span v-if="themeInfo.sidebar_color" class="color-chip" :style="{ background: themeInfo.sidebar_color }" :title="themeInfo.sidebar_color"></span>
        <span class="theme-summary">{{ themeSummary }}</span>
      </div>
      <div class="theme-actions">
        <el-button size="small" @click="exportTheme">{{ t.theme.export }}</el-button>
        <el-button size="small" @click="themeImportVisible = true">{{ t.theme.import }}</el-button>
        <el-button size="small" type="danger" plain @click="resetTheme">{{ t.theme.reset }}</el-button>
      </div>
      <el-divider content-position="left">{{ t.theme.marketTitle }}</el-divider>
      <div v-loading="themeMarketLoading" class="theme-market">
        <div v-for="th in themeMarket" :key="th.name + th.source" class="theme-row">
          <div class="theme-row-info">
            <strong>{{ th.name }}</strong>
            <el-tag size="small" :type="th.source === 'preset' ? 'info' : 'success'" disable-transitions>
              {{ th.source === 'preset' ? t.theme.sourcePreset : t.theme.sourceMarket }}
            </el-tag>
            <span v-if="th.author" class="theme-author">{{ th.author }}</span>
            <div class="theme-desc">{{ th.desc }}</div>
          </div>
          <el-button size="small" type="primary" plain :loading="th.applying" @click="applyMarketTheme(th)">
            {{ t.theme.apply }}
          </el-button>
        </div>
        <div v-if="!themeMarket.length && !themeMarketLoading" class="empty-state">
          <p>{{ t.theme.marketEmpty }}</p>
        </div>
      </div>

      <el-dialog :append-to-body="true" v-model="themeImportVisible" :title="t.theme.importTitle" width="480px">
        <el-input
          v-model="themeImportText"
          type="textarea"
          :rows="8"
          :placeholder="t.theme.importPlaceholder"
        />
        <div class="field-hint">{{ t.theme.importHint }}</div>
        <template #footer>
          <el-button @click="themeImportVisible = false">{{ zh.goals.cancel }}</el-button>
          <el-button type="primary" :loading="themeImporting" @click="importTheme">{{ t.theme.importDo }}</el-button>
        </template>
      </el-dialog>
    </el-card>

    <!-- 邮件监听与助手 -->
    <el-card class="settings-card" shadow="never">
      <template #header>
        <div class="card-header">
          <span class="card-title">{{ t.email.title }}</span>
          <p class="card-desc">{{ t.email.desc }}</p>
        </div>
      </template>
      <el-form label-position="top">
        <el-form-item :label="t.email.listenerEnabled">
          <el-switch v-model="form.emailListenerEnabled" />
          <div class="field-hint">{{ t.email.listenerHint }}</div>
        </el-form-item>
        <el-form-item :label="t.email.ownerEmail">
          <el-input v-model="form.ownerEmail" :placeholder="t.email.ownerEmailPlaceholder" />
        </el-form-item>
        <el-form-item :label="t.email.account">
          <el-input v-model="form.emailAccount" :placeholder="t.email.accountPlaceholder" />
        </el-form-item>
        <el-form-item :label="t.email.password">
          <el-input
            v-model="form.emailPassword"
            type="password"
            show-password
            :placeholder="initial?.email_password_set ? t.email.passwordSetPlaceholder : t.email.passwordPlaceholder"
          />
        </el-form-item>
        <el-form-item :label="t.email.imap">
          <el-input v-model="form.emailImap" :placeholder="t.email.imapPlaceholder" />
        </el-form-item>
        <el-form-item :label="t.email.smtp">
          <el-input v-model="form.emailSmtp" :placeholder="t.email.smtpPlaceholder" />
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

/* 界面主题卡 */
.theme-current {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.theme-label {
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.color-chip {
  display: inline-block;
  width: 18px;
  height: 18px;
  border-radius: 5px;
  border: 1px solid var(--el-border-color);
}

.theme-summary {
  font-size: 13px;
  color: var(--el-text-color-regular);
}

.theme-actions {
  display: flex;
  gap: 8px;
  margin-bottom: 4px;
}

.theme-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 4px;
  border-bottom: 1px solid var(--el-border-color-lighter);
}

.theme-row:last-child {
  border-bottom: none;
}

.theme-row-info {
  flex: 1;
  min-width: 0;
}

.theme-author {
  margin-left: 6px;
  font-size: 12px;
  color: var(--el-text-color-placeholder);
}

.theme-desc {
  margin-top: 2px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
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

.worker-name-input {
  max-width: 320px;
}
</style>
