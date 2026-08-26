<script setup>
// 设置 · 系统设置：后台巡视 / 调度者（分身）模式 / 沙箱与网络 / 访问控制 / 邮件监听与助手。
// 数据契约与 ModelsView 一致：GET /api/settings 读入，POST /api/settings 增量提交
// （只回传与初始快照不同的字段；访问密码留空 = 不修改，勾选清除 = 恢复仅本机）。
import { computed, onMounted, reactive, ref } from 'vue';
import { ElMessage } from 'element-plus';
import { cachedFetch, invalidateCache, request } from '../../api/client';
import zh from '../../i18n/zh';

const t = zh.settings.models; // 复用 models 节下 heartbeat/dispatcher/sandbox/access/email 文案

const loading = ref(true);   // 初始配置加载中（期间禁止保存，避免用默认值覆盖真实配置）
const saving = ref(false);

const form = reactive({
  heartbeatEnabled: false,
  heartbeatInterval: 60,
  maxCorrectionAttempts: 5,
  coldCacheTtl: 3600,
  maxTotalTokens: 128000,
  maxResumeCount: 10,
  dispatcherMode: false,
  agentWorkerName: '分身',
  sandboxMode: true,
  sandboxDir: '',
  httpProxy: '',
  browserHeadless: false,
  searxngUrl: '',
  // 沙箱自动清理（sandbox_janitor 节）
  janitorEnabled: true,
  janitorTtlDays: 7,
  janitorIntervalHours: 1,
  janitorSoftGb: 20,
  janitorHardGb: 50,
  // 访问控制：局域网访问密码（空 = 不修改；勾选清除 = 恢复仅本机）
  accessPassword: '',
  accessPasswordClear: false,
  // 邮件监听与助手
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

function applySettings(data) {
  const init = {
    heartbeat_enabled: data.heartbeat_enabled ?? false,
    heartbeat_interval: data.heartbeat_interval ?? 60,
    max_correction_attempts: data.max_correction_attempts ?? 5,
    cold_cache_ttl: data.cold_cache_ttl ?? 3600,
    max_total_tokens: data.context_budget?.max_total_tokens ?? 128000,
    max_resume_count: data.max_resume_count ?? 10,
    dispatcher_mode: data.dispatcher_mode ?? false,
    agent_worker_name: data.agent_worker_name || '分身',
    sandbox_mode: data.sandbox_mode ?? true,
    sandbox_dir: data.sandbox_dir || '',
    http_proxy: data.http_proxy || '',
    browser_headless: data.browser_headless ?? false,
    searxng_url: data.searxng_url || '',
    sandbox_janitor: {
      enabled: data.sandbox_janitor?.enabled ?? true,
      tmp_ttl_days: data.sandbox_janitor?.tmp_ttl_days ?? 7,
      interval_hours: data.sandbox_janitor?.interval_hours ?? 1,
      soft_gb: data.sandbox_janitor?.soft_gb ?? 20,
      hard_gb: data.sandbox_janitor?.hard_gb ?? 50,
    },
    access_password_set: data.access_password_set ?? false,
    email_listener_enabled: data.email_listener_enabled ?? false,
    owner_email: data.owner_email || '',
    email_account: data.email_account || '',
    email_imap_server: data.email_imap_server || '',
    email_smtp_server: data.email_smtp_server || '',
    email_password_set: data.email_password === '***',
  };
  initial.value = init;

  form.heartbeatEnabled = init.heartbeat_enabled;
  form.heartbeatInterval = init.heartbeat_interval;
  form.maxCorrectionAttempts = init.max_correction_attempts;
  form.coldCacheTtl = init.cold_cache_ttl;
  form.maxTotalTokens = init.max_total_tokens;
  form.maxResumeCount = init.max_resume_count;
  form.dispatcherMode = init.dispatcher_mode;
  form.agentWorkerName = init.agent_worker_name;
  form.sandboxMode = init.sandbox_mode;
  form.sandboxDir = init.sandbox_dir;
  form.httpProxy = init.http_proxy;
  form.browserHeadless = init.browser_headless;
  form.searxngUrl = init.searxng_url;
  form.janitorEnabled = init.sandbox_janitor.enabled;
  form.janitorTtlDays = init.sandbox_janitor.tmp_ttl_days;
  form.janitorIntervalHours = init.sandbox_janitor.interval_hours;
  form.janitorSoftGb = init.sandbox_janitor.soft_gb;
  form.janitorHardGb = init.sandbox_janitor.hard_gb;
  form.accessPassword = '';          // 已设置显示占位，不回填真实值
  form.accessPasswordClear = false;
  form.emailListenerEnabled = init.email_listener_enabled;
  form.ownerEmail = init.owner_email;
  form.emailAccount = init.email_account;
  form.emailImap = init.email_imap_server;
  form.emailSmtp = init.email_smtp_server;
  form.emailPassword = '';   // 已设置显示占位，不回填真实值
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

// 增量 payload：只收集与初始快照不同的字段；数字为 null（被清空）时不提交
function buildPayload() {
  const init = initial.value;
  const payload = {};

  if (form.heartbeatEnabled !== init.heartbeat_enabled) payload.heartbeat_enabled = form.heartbeatEnabled;
  if (form.heartbeatInterval != null && form.heartbeatInterval !== init.heartbeat_interval) {
    payload.heartbeat_interval = form.heartbeatInterval;
  }
  if (form.maxCorrectionAttempts != null && form.maxCorrectionAttempts !== init.max_correction_attempts) {
    payload.max_correction_attempts = form.maxCorrectionAttempts;
  }
  if (form.coldCacheTtl != null && form.coldCacheTtl !== init.cold_cache_ttl) {
    payload.cold_cache_ttl = form.coldCacheTtl;
  }
  if (form.maxTotalTokens != null && form.maxTotalTokens !== init.max_total_tokens) {
    payload.max_total_tokens = form.maxTotalTokens;
  }
  if (form.maxResumeCount != null && form.maxResumeCount !== init.max_resume_count) {
    payload.max_resume_count = form.maxResumeCount;
  }

  // 调度者（分身）模式：仅在与初始值不同时提交；叫法留空视为「不修改」
  if (form.dispatcherMode !== init.dispatcher_mode) payload.dispatcher_mode = form.dispatcherMode;
  const wn = form.agentWorkerName.trim();
  if (wn && wn !== init.agent_worker_name) payload.agent_worker_name = wn;

  if (form.sandboxMode !== init.sandbox_mode) payload.sandbox_mode = form.sandboxMode;
  if (form.sandboxDir.trim() !== init.sandbox_dir) payload.sandbox_dir = form.sandboxDir.trim();
  if (form.httpProxy.trim() !== init.http_proxy) payload.http_proxy = form.httpProxy.trim();
  if (form.browserHeadless !== init.browser_headless) payload.browser_headless = form.browserHeadless;
  if (form.searxngUrl.trim() !== init.searxng_url) payload.searxng_url = form.searxngUrl.trim();

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

  // 邮件监听与助手：密码仅用户新输入时才发送（*** 掩码不回传）
  if (form.emailListenerEnabled !== init.email_listener_enabled) payload.email_listener_enabled = form.emailListenerEnabled;
  if (form.ownerEmail.trim() !== init.owner_email) payload.owner_email = form.ownerEmail.trim();
  if (form.emailAccount.trim() !== init.email_account) payload.email_account = form.emailAccount.trim();
  if (form.emailImap.trim() !== init.email_imap_server) payload.email_imap_server = form.emailImap.trim();
  if (form.emailSmtp.trim() !== init.email_smtp_server) payload.email_smtp_server = form.emailSmtp.trim();
  if (form.emailPassword.trim()) payload.email_password = form.emailPassword.trim();

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
  <div class="system-view" v-loading="loading">
    <header class="view-header">
      <h1>🛠 {{ zh.settings.nav.system }}</h1>
      <p class="view-desc">{{ zh.settings.desc }}</p>
    </header>

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
.system-view {
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

.worker-name-input {
  max-width: 320px;
}

@media (max-width: 768px) {
  .system-view {
    padding: 16px 16px 32px;
  }

  .interval-select,
  .worker-name-input {
    width: 100%;
    max-width: none;
  }
}
</style>
