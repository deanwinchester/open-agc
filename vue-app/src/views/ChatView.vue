<script setup>
// 聊天视图（批次 3）：会话侧栏 + 消息流 + Agent 实时进度。
//
// 协议基准：dev-docs/API契约.md §3，以 api/ws.py 实际行为为准。
// 客户端 → 服务端：
//   {query, agent_name?, images?}                发送消息（运行中则由服务端转为插话注入）
//   {type:'interrupt'}                           中断当前 agent（ws.py:462-484 完整处理，无需 REST）
//   {type:'resume', task_id, extra_instruction?} 恢复中断任务
//   {type:'retry', query?}                       重试上一轮（query 缺省时服务端用 last_query）
//   {type:'tool_reply', answer}                  回复前台 ask_user
//   {type:'sandbox_response', session_id, action, path, password?, request_id?}  沙箱授权回复
//     （category='secret' 时附带 secret_name/secret_type/host/username/note 表单字段）
// 服务端 → 客户端：见底部 REPORT 注释与各 on* 处理器。
//
// 已知旧版缺陷的修正：
// - 不拼 HTML 字符串（全部模板渲染 + MarkdownView 消毒），无 XSS 面
// - 快速切换会话：异步历史/任务加载用 sid 快照比对，过期结果直接丢弃
// - WS 订阅集中在 onMounted 注册、onUnmounted 全部退订，不会累积
// - REST /api/tasks/{id}/steps 返回倒序步骤，渲染前反转（旧版历史卡片步骤顺序反了）
import { ref, reactive, computed, onMounted, onUnmounted, watch, nextTick } from 'vue';
import { useRoute, useRouter } from 'vue-router';
import { ElMessage, ElMessageBox } from 'element-plus';
import { useWsStore } from '../stores/ws';
import { request, cachedFetch } from '../api/client';
import zh from '../i18n/zh';
import SessionRail from '../components/chat/SessionRail.vue';
import MessageItem from '../components/chat/MessageItem.vue';
import ProgressCard from '../components/chat/ProgressCard.vue';
import AskUserForm from '../components/chat/AskUserForm.vue';
import SandboxModal from '../components/chat/SandboxModal.vue';
import ChatInput from '../components/chat/ChatInput.vue';

const t = zh.chat;
const JSON_HEADERS = { 'Content-Type': 'application/json' };
const HISTORY_PAGE_SIZE = 100;
// 历史加载完成后的短时间窗内，WS 推来的消息若与历史条目重复则丢弃
//（服务端 _pending_final_responses 重投与 REST 历史存在竞态，见 api/ws.py:63-77）
const DEDUP_WINDOW_MS = 3000;
// history_steps 卡片可显示「继续」按钮的任务状态（对齐旧 renderHistorySteps）
const RESUMABLE_STATUSES = ['interrupted', 'backgrounded', 'background_failed', 'completed'];
// 进入会话时值得用 REST 补一张进度卡片的任务状态（对齐旧 _loadRecentTaskProgress）
const RECENT_CARD_STATUSES = ['running', 'interrupted', 'backgrounded', 'background_failed', 'failed'];

const route = useRoute();
const router = useRouter();
const ws = useWsStore();

const sessions = ref([]);
const currentSessionId = ref(null);
const items = ref([]); // {kind:'msg'|'progress'|'ask', ...} 渲染顺序即对话顺序

// 系统通知折叠分组：连续 ≥2 条 system 消息合并为一个可展开组，避免大量
// 下载/后台通知把执行步骤顶出视野。组展开状态按组 key 记忆。
const expandedNoticeGroups = reactive(new Set());
function toggleNoticeGroup(key) {
  if (expandedNoticeGroups.has(key)) expandedNoticeGroups.delete(key);
  else expandedNoticeGroups.add(key);
}
const displayItems = computed(() => {
  const out = [];
  let run = null;
  const flush = () => {
    if (!run) return;
    if (run.length >= 2) out.push({ kind: 'notice-group', key: `ng-${run[0].key}`, notices: run });
    else out.push(...run);
    run = null;
  };
  for (const it of items.value) {
    if (it.kind === 'msg' && it.role === 'system') (run || (run = [])).push(it);
    else { flush(); out.push(it); }
  }
  flush();
  return out;
});
const liveCard = ref(null); // 当前实时进度卡片（同时也在 items 中）
const thinking = reactive({ visible: false, text: '' });
const running = ref(false);
const currentTaskId = ref(null);
const historyPaging = reactive({ oldestId: 0, hasMore: false, loading: false });
const historyLoadedAt = ref(0);
const retryBar = reactive({ visible: false, originalQuery: '' });
const scrollHint = ref(false);
// 窄屏会话栏抽屉（≤768px 时 SessionRail 隐藏，头部 🗂 按钮滑出）
const railOpen = ref(false);
const downloadBanner = reactive({ visible: false, label: '', pct: 0 });
const sandboxState = reactive({
  visible: false, path: '', toolName: '', blockType: 'path', description: '', category: '',
  requestId: '', // 唯一等待 id：随 sandbox_response 回传，ws.py 据此精确匹配（兼容旧会话键）
  fromSession: 0, // 授权请求来源会话（非当前会话时弹窗标注，授权不分会话展示）
});

// ── 头部栏：agent 选择 / 当前模型 badge / token 用量（对齐旧聊天页头部） ──
const agents = ref([]);              // GET /api/agents → [{name, model, ...}]
const selectedAgent = ref('default'); // 'default' = 不使用自定义角色（ws.py:414 语义）
const defaultModel = ref('');        // GET /api/settings → default_model（cachedFetch TTL 60s）
const tokenUsageText = ref('');      // 最近一次 WS usage 事件的格式化文本，新一轮开始时清零
const currentSessionName = computed(() => {
  const s = sessions.value.find((x) => x.id === currentSessionId.value);
  return (s && s.name) || t.sessionsTitle;
});

async function loadHeaderMeta() {
  try {
    const data = await request('/api/agents');
    agents.value = (data && data.agents) || [];
    // 当前选中的 agent 已被删除时回退到 default，避免发送无效角色名
    if (selectedAgent.value !== 'default'
        && !agents.value.some((a) => a.name === selectedAgent.value)) {
      selectedAgent.value = 'default';
    }
  } catch { /* agent 下拉为可选增强，失败静默 */ }
  try {
    const data = await cachedFetch('/api/settings');
    defaultModel.value = (data && data.default_model) || '';
  } catch { /* 模型 badge 失败静默 */ }
}

const listEl = ref(null);
const chatViewEl = ref(null);
// 移动端输入法遮挡：visualViewport 收缩（键盘弹出）时把聊天区高度钉在可视区，
// 键盘收起后还原；仅窄屏生效，桌面端 visualViewport 变化不影响布局。
function _onImeViewportResize() {
  const el = chatViewEl.value;
  if (!el || !window.visualViewport) return;
  if (!window.matchMedia('(max-width: 768px)').matches) { el.style.height = ''; return; }
  const vv = window.visualViewport;
  const keyboardOpen = window.innerHeight - vv.height > 80;
  el.style.height = keyboardOpen ? `${vv.height}px` : '';
  if (keyboardOpen) scrollToBottom(true, true);
}
let keySeq = 0;
const nextKey = () => ++keySeq;
let downloadBannerTimer = null;
const unsubs = [];

// ── 会话过滤：广播会发到所有连接，非当前会话的事件一律忽略 ──
function isForCurrent(data) {
  const sid = data.session_id != null ? data.session_id
    : (data.task_session_id != null ? data.task_session_id : null);
  return sid == null || sid === currentSessionId.value;
}

// ── 滚动 ──
function scrollToBottom(force = false, instant = false) {
  nextTick(() => {
    const el = listEl.value;
    if (!el) return;
    const atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 200;
    if (force || atBottom || el.scrollHeight <= el.clientHeight) {
      el.scrollTo({ top: el.scrollHeight, behavior: instant ? 'auto' : 'smooth' });
      scrollHint.value = false;
    } else {
      scrollHint.value = true;
    }
  });
}

function onListScroll() {
  const el = listEl.value;
  if (!el) return;
  scrollHint.value = !(el.scrollTop + el.clientHeight >= el.scrollHeight - 200);
}

// ── 消息追加：运行中插入的用户消息排在实时进度卡片之前（对齐旧 appendMessage） ──
function appendItem(item) {
  // 实时消息无 DB id/时间戳：补本地时间用于显示（删除按钮仅对有 DB id 的
  // 历史消息开放，刷新后即可删除）
  if (item.kind === 'msg' && !item.timestamp) {
    item.timestamp = new Date().toISOString();
  }
  if (item.role === 'user' && liveCard.value) {
    const idx = items.value.findIndex((i) => i.kind === 'progress' && i.key === liveCard.value.key);
    if (idx >= 0) {
      items.value.splice(idx, 0, item);
      return;
    }
  }
  items.value.push(item);
}

function isDupFromHistory(role, content) {
  if (Date.now() - historyLoadedAt.value > DEDUP_WINDOW_MS) return false;
  return items.value.some((i) => i.kind === 'msg' && i.role === role && i.content === content);
}

// ── 实时进度卡片 ──
function ensureLiveCard(data) {
  if (liveCard.value) return liveCard.value;
  // 同一任务若已有历史卡片，先移除再建实时卡（对齐旧 ensureProgressContainer）
  if (data && data.task_id) {
    const idx = items.value.findIndex((i) => i.kind === 'progress' && i.taskId === data.task_id);
    if (idx >= 0) items.value.splice(idx, 1);
  }
  thinking.visible = false;
  const card = reactive({
    kind: 'progress', key: nextKey(), taskId: (data && data.task_id) || null,
    live: true, history: false, resumable: false, collapsed: false,
    entries: [], shellLines: [], tokenUsage: '',
  });
  items.value.push(card);
  liveCard.value = card;
  return card;
}

function finishLiveCard() {
  const card = liveCard.value;
  if (!card) return;
  card.live = false;
  card.collapsed = true;
  liveCard.value = null;
}

function pushShellLine(card, text) {
  if (!text) return;
  if (text.includes('\r')) {
    // 回车符：取最后一段并替换最后一行（模拟终端进度条刷新）
    const last = text.split('\r').pop();
    if (!last) return;
    if (card.shellLines.length) card.shellLines[card.shellLines.length - 1] = last;
    else card.shellLines.push(last);
  } else {
    card.shellLines.push(text);
  }
  while (card.shellLines.length > 200) card.shellLines.shift();
}

// ── 历史步骤卡片（history_steps / REST 补卡共用） ──
function renderHistorySteps(data) {
  if (!data || !data.task_id) return;
  if (liveCard.value && liveCard.value.taskId === data.task_id) return; // 已是实时卡，跳过
  const steps = Array.isArray(data.steps) ? data.steps : [];
  // 同任务旧卡片先移除（去重）
  const existingIdx = items.value.findIndex((i) => i.kind === 'progress' && i.taskId === data.task_id);
  if (existingIdx >= 0) items.value.splice(existingIdx, 1);
  if (!steps.length) return;

  const isLive = data.task_status === 'running' || data.task_status === 'resuming';
  const card = reactive({
    kind: 'progress', key: nextKey(), taskId: data.task_id,
    live: isLive, history: !isLive,
    resumable: RESUMABLE_STATUSES.includes(data.task_status),
    collapsed: !isLive,
    entries: steps.flatMap((s) => {
      const ok = s.success === 1 || s.success === true;
      const failed = s.success === 0 || s.success === false;
      const toolEntry = {
        kind: 'tool', step: s.step_number, tool: s.tool_name,
        toolLabel: s.tool_label || s.tool_name,
        argsPreview: s.args_preview || '',
        // 行内只显示短概览；完整结果点击后在右侧抽屉查看（对齐旧版交互）
        resultPreview: String(s.result_preview || '').substring(0, 120),
        fullResult: s.full_result || '',
        subTask: s.sub_task || '',
        success: ok ? true : failed ? false : null,
        status: ok ? 'done' : failed ? 'failed' : 'running',
      };
      // 该步骤前缓存的思考内容（reasoning_content 落库于 task_steps.thinking_content）
      // 作为 thinking 条目插在工具步骤之前——刷新后思考过程仍可见（用户反馈）
      const thinkingEntry = s.thinking_content
        ? { kind: 'thinking', ekey: `h-th-${s.step_number}`, content: s.thinking_content }
        : null;
      return thinkingEntry ? [thinkingEntry, toolEntry] : [toolEntry];
    }),
    shellLines: [], tokenUsage: '',
  });

  // 插入到最后一条用户消息之后（对齐旧 renderHistorySteps）
  let insertAt = -1;
  for (let i = items.value.length - 1; i >= 0; i--) {
    if (items.value[i].kind === 'msg' && items.value[i].role === 'user') { insertAt = i; break; }
  }
  if (insertAt >= 0) items.value.splice(insertAt + 1, 0, card);
  else items.value.push(card);

  if (isLive) {
    // 复用为实时卡片，后续 tool_start/tool_done 追加到这张卡上
    liveCard.value = card;
    running.value = true;
    currentTaskId.value = data.task_id;
    thinking.text = t.resuming;
    thinking.visible = true;
  }
  scrollToBottom();
}

// ── WS 事件处理 ──
function onStatus(data) {
  if (!isForCurrent(data) || data.background) return;
  thinking.text = t.thinking;
  thinking.visible = true;
  scrollToBottom();
}

function onProgress(data) {
  const ev = data.event;

  // 沙箱授权不分会话：单用户部署下任何会话产生的权限请求都必须可见
  // （此前被 isForCurrent 过滤，其他会话的请求静默超时）。弹窗标注来源会话。
  if (ev === 'sandbox_blocked') {
    sandboxState.path = data.path || '';
    sandboxState.toolName = data.tool_name || data.tool || '?';
    sandboxState.blockType = data.block_type || 'path';
    sandboxState.description = data.description || '';
    sandboxState.category = data.category || '';
    sandboxState.requestId = data.request_id || '';
    sandboxState.fromSession = data.session_id && data.session_id !== currentSessionId.value
      ? data.session_id : 0;
    sandboxState.visible = true;
    return;
  }

  if (!isForCurrent(data)) return;

  if (ev === 'ask_user') { handleAskUser(data); return; }
  if (ev === 'task_backgrounded') return; // 由顶层 task_backgrounded 事件统一提示

  const card = ensureLiveCard(data);
  if (data.task_id && card.taskId == null) card.taskId = data.task_id;

  switch (ev) {
    case 'thinking':
      if (data.content) {
        const ekey = `thought-${data.iteration || 0}`;
        const existing = card.entries.find((e) => e.kind === 'thinking' && e.ekey === ekey);
        if (existing) existing.content = data.content;
        else card.entries.push({ kind: 'thinking', ekey, content: data.content });
      } else {
        thinking.text = t.thinking;
        thinking.visible = true;
      }
      break;
    case 'shell_output':
      pushShellLine(card, data.text || '');
      break;
    case 'model_switched':
      card.entries.push({ kind: 'note', text: `${t.modelSwitched}: ${data.from} → ${data.to}` });
      break;
    case 'response':
      if (data.content) card.entries.push({ kind: 'response', content: data.content });
      break;
    case 'usage': {
      // 服务端字段：total_tokens/prompt_tokens/completion_tokens/cached_tokens（无 cost，
      // 见 agent/agent.py usage 事件构造）；格式对齐旧 static/app.js:830-836
      const usageText = `${t.tokens}: ${data.total_tokens} (P:${data.prompt_tokens} / C:${data.completion_tokens}${data.cached_tokens ? `${t.cachedPrefix}${data.cached_tokens}` : ''})`;
      card.tokenUsage = usageText;
      tokenUsageText.value = usageText; // 头部栏同步显示
      break;
    }
    case 'tool_start':
      running.value = true;
      if (data.task_id) currentTaskId.value = data.task_id;
      card.entries.push({
        kind: 'tool', step: data.step, tool: data.tool,
        toolLabel: data.tool_label || data.tool,
        argsPreview: data.args_preview || '',
        resultPreview: '', success: null, status: 'running',
        subTask: data.sub_task || '',
      });
      break;
    case 'tool_done': {
      // 按 step + subTask 匹配：并行子代理各自从 1 编号，仅按 step 会串号
      const entry = card.entries.find((e) => e.kind === 'tool' && e.step === data.step
        && (e.subTask || '') === (data.sub_task || ''));
      if (entry) {
        entry.status = data.success ? 'done' : 'failed';
        entry.success = !!data.success;
        if (data.result_preview) entry.resultPreview = data.result_preview;
        if (data.full_result) entry.fullResult = data.full_result;
      }
      break;
    }
    default:
      break; // 未识别的 progress 子事件忽略
  }
  scrollToBottom();
}

function handleAskUser(data) {
  if (data.background && data.task_id) {
    // 后台任务提问：作为系统通知 + 回复表单，走 REST /api/tasks/{id}/reply
    appendItem({
      kind: 'ask', key: nextKey(), question: data.question,
      options: data.options || null, taskId: data.task_id,
      background: true, answered: false, answer: '', error: '',
    });
  } else {
    // 前台提问：挂到实时进度卡片内，走 WS tool_reply
    thinking.visible = false;
    const card = ensureLiveCard(data);
    card.entries.push({
      kind: 'ask', question: data.question,
      options: data.options || null, taskId: data.task_id || null,
      background: false, answered: false, answer: '', error: '',
    });
  }
  scrollToBottom();
}

function onMessage(data) {
  if (!isForCurrent(data)) return;
  const role = data.role || 'agent';
  if (role === 'tool_step') return; // 步骤消息由进度卡片呈现
  if (isDupFromHistory(role, data.content)) return;
  thinking.visible = false;
  finishLiveCard();
  running.value = false;
  currentTaskId.value = null;
  retryBar.visible = false;
  // 中断等场景下 response 可能为空 —— 状态已重置，空内容不再渲染空气泡
  if (data.content && String(data.content).trim()) {
    appendItem({ kind: 'msg', key: nextKey(), role, content: data.content, taskId: data.task_id || null });
  }
  scrollToBottom();
}

function onError(data) {
  if (data.background) {
    if (isForCurrent(data)) {
      appendItem({ kind: 'msg', key: nextKey(), role: 'system', content: `${t.bgTaskError}: ${data.content}` });
      scrollToBottom();
    }
    return;
  }
  if (!isForCurrent(data)) return;
  thinking.visible = false;
  finishLiveCard();
  running.value = false;
  currentTaskId.value = null;
  // 旧版只显示重试条、错误内容仅落库；这里直接渲染为系统通知（含 API Key 配置引导）
  if (data.content) {
    appendItem({ kind: 'msg', key: nextKey(), role: 'system', content: data.content });
  }
  retryBar.originalQuery = data.original_query || '';
  retryBar.visible = true;
  scrollToBottom();
}

function onHistorySteps(data) {
  if (!isForCurrent(data)) return;
  renderHistorySteps(data);
}

function onTaskBackgrounded(data) {
  if (!isForCurrent(data)) return;
  thinking.visible = false;
  finishLiveCard();
  running.value = false;
  currentTaskId.value = null;
  appendItem({
    kind: 'msg', key: nextKey(), role: 'system',
    content: `${t.taskBackgrounded}\n\n${data.message || ''}\n\n${t.taskBackgroundedHint}`,
    taskId: data.task_id || null,
  });
  scrollToBottom();
}

function onSystemMessage(data) {
  if (!isForCurrent(data)) return;
  if (!data.message) return;
  if (isDupFromHistory('system', data.message)) return;
  appendItem({ kind: 'msg', key: nextKey(), role: 'system', content: data.message });
  scrollToBottom();
}

function onLlamaDownload(data) {
  if (data.stage === 'complete') {
    clearTimeout(downloadBannerTimer);
    downloadBanner.visible = false;
    ElMessage.success(`${t.downloadComplete}: ${data.label || ''}`);
  } else if (data.stage === 'error') {
    clearTimeout(downloadBannerTimer);
    downloadBanner.visible = false;
    ElMessage.error(`${t.downloadFailed}: ${data.error || data.label || t.unknownError}`);
  } else {
    downloadBanner.visible = true;
    downloadBanner.label = data.label || '';
    downloadBanner.pct = Math.round((data.progress || 0) * 100);
    clearTimeout(downloadBannerTimer);
    downloadBannerTimer = setTimeout(() => { downloadBanner.visible = false; }, 5000);
  }
}

function onDownloadSuccess(data) {
  // 系统消息按会话过滤（事件带 session_id 时），toast 全局提示
  if (isForCurrent(data)) {
    appendItem({ kind: 'msg', key: nextKey(), role: 'system', content: `✅ **${t.downloadComplete}: ${data.label || ''}**` });
    scrollToBottom();
  }
  ElMessage.success(`${t.downloadComplete}: ${data.label || ''}`);
}

function onDownloadFailed(data) {
  if (isForCurrent(data)) {
    appendItem({
      kind: 'msg', key: nextKey(), role: 'system',
      content: `❌ **${t.downloadFailed}: ${data.label || ''}**\n\n${data.error || t.unknownError}`,
    });
    scrollToBottom();
  }
  ElMessage.error(`${t.downloadFailed}: ${data.error || t.unknownError}`);
}

// ── 用户操作 ──
function onSend(payload) {
  // ChatInput 上抛 {text, images, files}；onRetryContinue 等处仍传纯字符串，兼容两种形态
  const p = typeof payload === 'string' ? { text: payload } : (payload || {});
  const text = (p.text || '').trim();
  const images = Array.isArray(p.images) ? p.images : [];
  const files = Array.isArray(p.files) ? p.files : [];
  if (!text && !images.length && !files.length) return;
  if (!ws.connected) { ElMessage.error(t.sendFailed); return; }
  // 附件不进 WS 独立字段，把文件列表拼进 query 文本——字符串格式与旧
  // static/app.js:1686-1692 完全一致（Agent 靠该文本定位 uploads/ 下的文件）
  let query = text;
  if (files.length > 0) {
    const fileList = files.map((f) => 'uploads/' + f.name).join(', ');
    query = query ? `${query}\n\n[已上传文件: ${fileList}]` : `请处理我上传的文件: ${fileList}`;
  }
  // 气泡显示原始输入（无文字时按旧版显示 [图片]/[文件] 占位，app.js:1694）；
  // displayText 为凭据打码版（入口 B，ChatInput 可选上抛），优先于原文
  appendItem({
    kind: 'msg', key: nextKey(), role: 'user',
    content: p.displayText || text || (images.length > 0 ? t.imageOnlyContent : t.fileOnlyContent),
    images: images.length > 0 ? images : null,
    files: files.length > 0 ? files : null,
  });
  scrollToBottom(true);
  // agent_name 语义见 api/ws.py:963 + 414：'default'/缺省 = 不使用自定义角色；
  // images 由 ws.py:964 读取后传给 agent.run_turn
  const msg = { query, agent_name: selectedAgent.value };
  if (images.length > 0) msg.images = images;
  if (!ws.send(msg)) { ElMessage.error(t.sendFailed); return; }
  retryBar.visible = false;
  if (running.value) {
    // 服务端会把消息注入正在运行的 agent（插话），不重置进度状态
    ElMessage.info(t.queuedNotice);
    return;
  }
  // 新一轮：旧实时卡片标记完成，token 用量清零，等待新的进度事件
  finishLiveCard();
  tokenUsageText.value = '';
  running.value = true;
}

function onStop() {
  if (!running.value) return;
  // ws.py:462-484 完整处理 interrupt（agent/后台 agent/shell/下载），无需再调 REST
  if (!ws.send({ type: 'interrupt' })) ElMessage.error(t.sendFailed);
  else ElMessage.info(t.interrupted);
}

function onResume(taskId) {
  if (running.value) { ElMessage.warning(t.agentBusy); return; }
  if (!ws.connected) { ElMessage.error(t.sendFailed); return; }
  // 先把卡片本地标记为运行中（服务端随后会推 task_status=resuming 的 history_steps）
  const card = items.value.find((i) => i.kind === 'progress' && i.taskId === taskId);
  if (card) {
    card.live = true;
    card.history = false;
    card.collapsed = false;
    card.resumable = false;
    liveCard.value = card;
  }
  running.value = true;
  currentTaskId.value = taskId;
  ws.send({ type: 'resume', task_id: taskId });
}

function onRetry() {
  const msg = { type: 'retry' };
  if (retryBar.originalQuery) msg.query = retryBar.originalQuery;
  retryBar.visible = false;
  if (ws.send(msg)) running.value = true;
  else ElMessage.error(t.sendFailed);
}

function onRetryContinue() {
  retryBar.visible = false;
  onSend(t.continueMessage);
}

async function onSubmitAsk(entry, answer) {
  if (!answer || entry.answered) return;
  if (entry.background && entry.taskId) {
    try {
      const d = await request(`/api/tasks/${entry.taskId}/reply`, {
        method: 'POST', headers: JSON_HEADERS, body: JSON.stringify({ answer }),
      });
      if (d && d.status === 'success') {
        entry.answered = true; entry.answer = answer; entry.error = '';
      } else {
        entry.error = (d && (d.detail || d.message)) || t.replyFailed;
      }
    } catch (e) {
      entry.error = e.message;
    }
  } else if (ws.send({ type: 'tool_reply', answer })) {
    entry.answered = true; entry.answer = answer; entry.error = '';
  } else {
    entry.error = t.sendFailed;
  }
}

function onSandboxRespond(payload) {
  const { action, password } = payload;
  const msg = {
    type: 'sandbox_response',
    session_id: currentSessionId.value,
    action,
    path: sandboxState.path,
  };
  if (sandboxState.requestId) msg.request_id = sandboxState.requestId;
  if (password) msg.password = password;
  // 凭据收集表单（category='secret'）：全部字段透传到 agent 的 result_holder，
  // ws.py 仅放内存，由 agent 写入本机凭证库；拒绝时弹窗侧本就不带这些字段
  if (sandboxState.category === 'secret') {
    msg.secret_name = payload.secretName || '';
    msg.secret_type = payload.secretType || 'generic';
    msg.host = payload.host || '';
    msg.username = payload.username || '';
    msg.note = payload.note || '';
  }
  if (!ws.send(msg)) ElMessage.error(t.sandbox.sendFailed);
  sandboxState.visible = false;
}

// ── 删除单条消息（有 DB id 的历史消息） ──
async function onDeleteMessage(item) {
  if (!item.id) return;
  try {
    await ElMessageBox.confirm(
      t.deleteConfirmText,
      t.deleteConfirmTitle,
      { confirmButtonText: t.deleteMessage, cancelButtonText: zh.goals.cancel, type: 'warning' }
    );
  } catch {
    return; // 用户取消
  }
  try {
    await request(`/api/history/${item.id}`, { method: 'DELETE' });
    items.value = items.value.filter((i) => i !== item);
    ElMessage.success(t.deleteSuccess);
  } catch (e) {
    ElMessage.error(`${t.deleteFailed}: ${e.message}`);
  }
}

// ── 历史加载（含分页与过期守卫） ──
async function loadHistory(sid, { beforeId = 0 } = {}) {
  if (historyPaging.loading) return;
  historyPaging.loading = true;
  try {
    const params = `session_id=${sid}&limit=${HISTORY_PAGE_SIZE}${beforeId ? `&before_id=${beforeId}` : ''}`;
    const data = await request(`/api/history?${params}`);
    if (sid !== currentSessionId.value) return; // 会话已切换，丢弃过期结果
    historyPaging.oldestId = data.oldest_id || 0;
    historyPaging.hasMore = !!data.has_more;
    const msgs = (data.history || []).map((m) => ({
      kind: 'msg', key: nextKey(), role: m.role, content: m.content,
      id: m.id, timestamp: m.timestamp || null,
      // 附件（粘贴图片落盘路径 uploads/xxx）→ 缩略图 URL，经 /api/upload 提供
      images: (Array.isArray(m.attachments) && m.attachments.length)
        ? m.attachments.map((a) => '/api/upload/' + encodeURIComponent(String(a).split('/').pop()))
        : (m.images || null),
      files: m.files || null,
    }));
    if (beforeId) {
      // 向前翻页：顶部插入并保持滚动位置
      const el = listEl.value;
      const prevHeight = el ? el.scrollHeight : 0;
      items.value = [...msgs, ...items.value];
      await nextTick();
      if (el && el.scrollHeight > prevHeight) el.scrollTop = el.scrollHeight - prevHeight;
    } else {
      items.value = msgs.length
        ? msgs
        : [{ kind: 'msg', key: nextKey(), role: 'system', content: t.welcome }];
      historyLoadedAt.value = Date.now();
      scrollToBottom(true, true);
    }
  } catch (e) {
    if (sid === currentSessionId.value) ElMessage.error(`${t.historyLoadFailed}: ${e.message}`);
  } finally {
    historyPaging.loading = false;
  }
}

// 进入会话时用 REST 补一张最近任务的进度卡片（WS 连接后也会推 history_steps，按 task_id 去重）
async function loadRecentTaskCard(sid) {
  try {
    const data = await request(`/api/tasks?session_id=${sid}&page=1&page_size=1`);
    if (sid !== currentSessionId.value) return;
    const task = (data.tasks || [])[0];
    if (!task || !RECENT_CARD_STATUSES.includes(task.status)) return;
    const stepData = await request(`/api/tasks/${task.id}/steps?page=1&page_size=100`);
    if (sid !== currentSessionId.value) return;
    // 该接口按 created_at DESC 返回，渲染前反转为正序（修正旧版历史卡片顺序 bug）
    const steps = (stepData.steps || []).slice().reverse();
    if (!steps.length) return;
    renderHistorySteps({ task_id: task.id, task_status: task.status, steps, session_id: sid });
  } catch {
    /* 进度卡片是可选增强，失败静默 */
  }
}

// ── 会话管理 ──
async function loadSessions() {
  try {
    const data = await request('/api/sessions');
    sessions.value = data.sessions || [];
  } catch (e) {
    ElMessage.error(`${t.loadSessionsFailed}: ${e.message}`);
  }
}

async function enterSession(sid) {
  sid = parseInt(sid, 10);
  if (!Number.isFinite(sid) || sid <= 0) sid = 1;
  if (sid === currentSessionId.value) return; // 同步赋值在 await 之前，重入安全
  currentSessionId.value = sid;
  localStorage.setItem('lastSessionId', String(sid));
  // 重置上一会话的全部状态
  items.value = [];
  liveCard.value = null;
  thinking.visible = false;
  running.value = false;
  currentTaskId.value = null;
  retryBar.visible = false;
  scrollHint.value = false;
  sandboxState.visible = false;
  tokenUsageText.value = '';
  historyPaging.oldestId = 0;
  historyPaging.hasMore = false;
  historyPaging.loading = false;
  // 先加载历史再切换 WS：连接后服务端会立即推 history_steps / 未送达的最终回复
  //（api/ws.py:38-77），若先连 WS 再加载历史，推送的消息会被历史覆盖丢失。
  await loadHistory(sid);
  await loadRecentTaskCard(sid);
  if (sid !== currentSessionId.value) return; // 加载期间又切换了会话，放弃旧连接
  ws.switchSession(sid); // store 内部关旧连新
}

async function onCreateSession() {
  try {
    const data = await request('/api/sessions', { method: 'POST', headers: JSON_HEADERS, body: '{}' });
    await loadSessions();
    const id = data.session && data.session.id;
    if (id) router.push(`/chat/${id}`);
  } catch (e) {
    ElMessage.error(`${t.createFailed}: ${e.message}`);
  }
}

function onSelectSession(id) {
  railOpen.value = false; // 窄屏抽屉内选择会话后自动收起
  if (id !== currentSessionId.value) router.push(`/chat/${id}`);
}

async function onRenameSession({ id, name }) {
  try {
    await request(`/api/sessions/${id}`, {
      method: 'PUT', headers: JSON_HEADERS, body: JSON.stringify({ name }),
    });
    await loadSessions();
  } catch (e) {
    ElMessage.error(`${t.renameFailed}: ${e.message}`);
  }
}

async function onDeleteSession(id) {
  if (sessions.value.length <= 1) { ElMessage.warning(t.lastSession); return; }
  try {
    await request(`/api/sessions/${id}`, { method: 'DELETE' });
    if (currentSessionId.value === id) {
      const next = sessions.value.find((s) => s.id !== id);
      await loadSessions();
      if (next) router.push(`/chat/${next.id}`);
    } else {
      await loadSessions();
    }
  } catch (e) {
    ElMessage.error(`${t.deleteFailed}: ${e.message}`);
  }
}

async function onClearSession(id) {
  try {
    await request(`/api/sessions/${id}/clear`, { method: 'POST' });
    ElMessage.success(t.cleared);
    if (currentSessionId.value === id) {
      items.value = [];
      liveCard.value = null;
      historyPaging.oldestId = 0;
      historyPaging.hasMore = false;
      await loadHistory(id); // 空历史会显示欢迎语
    }
    await loadSessions();
  } catch (e) {
    ElMessage.error(`${t.clearFailed}: ${e.message}`);
  }
}

// ── 生命周期 ──
watch(
  () => route.params.sessionId,
  (val) => {
    if (route.name !== 'chat') return;
    const sid = parseInt(val, 10);
    if (Number.isFinite(sid) && sid !== currentSessionId.value) enterSession(sid);
  }
);

onMounted(async () => {
  window.visualViewport?.addEventListener('resize', _onImeViewportResize);
  unsubs.push(
    ws.on('status', onStatus),
    ws.on('progress', onProgress),
    ws.on('message', onMessage),
    ws.on('error', onError),
    ws.on('history_steps', onHistorySteps),
    ws.on('task_backgrounded', onTaskBackgrounded),
    ws.on('system_message', onSystemMessage),
    ws.on('llamacpp_download', onLlamaDownload),
    ws.on('download_success', onDownloadSuccess),
    ws.on('download_failed', onDownloadFailed),
  );
  await loadSessions();
  loadHeaderMeta(); // 不阻塞会话进入：agent 下拉 / 模型 badge 异步到位
  let sid = parseInt(route.params.sessionId, 10);
  if (!Number.isFinite(sid)) {
    const last = parseInt(localStorage.getItem('lastSessionId'), 10);
    sid = sessions.value.some((s) => s.id === last) ? last : (sessions.value[0] ? sessions.value[0].id : 1);
    router.replace(`/chat/${sid}`);
  }
  await enterSession(sid);
});

onUnmounted(() => {
  window.visualViewport?.removeEventListener('resize', _onImeViewportResize);
  unsubs.forEach((fn) => { try { fn(); } catch { /* noop */ } });
  unsubs.length = 0;
  clearTimeout(downloadBannerTimer);
  ws.disconnect();
});
</script>

<template>
  <div ref="chatViewEl" class="chat-view" :class="{ 'rail-open': railOpen }">
    <SessionRail
      :sessions="sessions"
      :current-id="currentSessionId"
      @select="onSelectSession"
      @create="onCreateSession"
      @rename="onRenameSession"
      @remove="onDeleteSession"
      @clear="onClearSession"
    />

    <div v-if="railOpen" class="rail-overlay" @click="railOpen = false"></div>

    <div class="chat-main">
      <header class="chat-header">
        <button
          class="rail-toggle"
          type="button"
          :title="t.sessionsTitle"
          @click="railOpen = !railOpen"
        >🗂️</button>
        <span class="session-title" :title="currentSessionName">{{ currentSessionName }}</span>
        <div class="header-right">
          <el-select v-model="selectedAgent" size="small" class="agent-select" :title="t.agentSelectTitle">
            <el-option value="default" :label="t.defaultAgent" />
            <el-option v-for="a in agents" :key="a.name" :value="a.name" :label="a.name" />
          </el-select>
          <span v-if="defaultModel" class="model-badge">{{ defaultModel }}</span>
          <span v-if="tokenUsageText" class="token-usage">{{ tokenUsageText }}</span>
        </div>
      </header>

      <div v-if="downloadBanner.visible" class="download-banner">
        <span class="dl-label">📥 {{ downloadBanner.label }}</span>
        <span class="dl-pct">{{ downloadBanner.pct }}%</span>
      </div>

      <div ref="listEl" class="msg-list" @scroll="onListScroll">
        <div class="msg-list-inner">
          <div v-if="historyPaging.hasMore" class="load-more">
            <el-button
              size="small"
              text
              :loading="historyPaging.loading"
              @click="loadHistory(currentSessionId, { beforeId: historyPaging.oldestId })"
            >{{ t.loadMore }}</el-button>
          </div>

          <template v-for="item in displayItems" :key="item.key">
            <MessageItem v-if="item.kind === 'msg'" :item="item" @delete="onDeleteMessage" />
            <ProgressCard
              v-else-if="item.kind === 'progress'"
              :card="item"
              @resume="onResume"
              @submit-ask="onSubmitAsk"
            />
            <div v-else-if="item.kind === 'ask'" class="ask-row">
              <AskUserForm :entry="item" @submit="onSubmitAsk" />
            </div>
            <div v-else-if="item.kind === 'notice-group'" class="notice-group">
              <button type="button" class="ng-header" @click="toggleNoticeGroup(item.key)">
                📢 {{ t.systemNotices }} · {{ item.notices.length }} {{ t.noticesSuffix }}
                <span class="ng-arrow">{{ expandedNoticeGroups.has(item.key) ? '▾' : '▸' }}</span>
              </button>
              <div v-if="expandedNoticeGroups.has(item.key)" class="ng-body">
                <MessageItem v-for="n in item.notices" :key="n.key" :item="n" />
              </div>
            </div>
          </template>

          <div v-if="thinking.visible" class="thinking-bubble">
            <span class="thinking-dot"></span>
            <span>{{ thinking.text }}</span>
          </div>

          <div v-if="retryBar.visible" class="retry-bar">
            <el-button size="small" @click="onRetry">↻ {{ t.retry }}</el-button>
            <el-button size="small" @click="onRetryContinue">▶ {{ t.continueRest }}</el-button>
          </div>
        </div>
      </div>

      <div v-if="scrollHint" class="scroll-hint" @click="scrollToBottom(true)">
        <span>{{ t.newMessages }}</span>
      </div>

      <ChatInput :running="running" :connected="ws.connected" @send="onSend" @stop="onStop" />
    </div>

    <SandboxModal v-model="sandboxState.visible" :data="sandboxState" @respond="onSandboxRespond" />
  </div>
</template>

<style scoped>
.chat-view {
  display: flex;
  height: 100%;
  min-height: 0;
  overflow: hidden;
}

.chat-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  position: relative;
}

/* 头部栏：会话标题 + agent 下拉 + 模型 badge + token 用量（对齐旧聊天页头部） */
.chat-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 20px;
  border-bottom: 1px solid var(--el-border-color-lighter);
  background: var(--el-bg-color);
}

.session-title {
  font-size: 14px;
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.header-right {
  display: flex;
  align-items: center;
  gap: 12px;
  flex-shrink: 0;
}

.agent-select {
  width: 140px;
}

.model-badge {
  font-size: 12px;
  color: var(--el-color-primary);
  background: var(--el-color-primary-light-9);
  border-radius: 10px;
  padding: 2px 10px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  max-width: 260px;
}

.token-usage {
  font-size: 11px;
  font-family: monospace;
  color: var(--el-text-color-secondary);
  white-space: nowrap;
}

.download-banner {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 16px;
  background: var(--el-color-primary-light-9);
  color: var(--el-color-primary);
  font-size: 12px;
}

.msg-list {
  flex: 1;
  overflow-y: auto;
  min-height: 0;
}

.msg-list-inner {
  max-width: 860px;
  margin: 0 auto;
  padding: 16px;
}

/* 系统通知折叠组：紧凑居中 chip，避免刷屏 */
.notice-group {
  display: flex;
  flex-direction: column;
  align-items: center;
  margin-bottom: 16px;
}

.ng-header {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 5px 14px;
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 999px;
  background: var(--el-fill-color-light);
  color: var(--el-text-color-secondary);
  font-size: 12px;
  cursor: pointer;
  transition: color var(--panda-transition), border-color var(--panda-transition);
}

.ng-header:hover {
  color: var(--el-color-primary);
  border-color: var(--el-color-primary-light-5);
}

.ng-body {
  margin-top: 8px;
  width: 100%;
}

.load-more {
  text-align: center;
  margin-bottom: 12px;
}

.ask-row {
  max-width: 85%;
  margin: 0 auto 14px;
}

.thinking-bubble {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
  padding: 6px 4px 14px;
}

.thinking-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--el-color-primary);
  animation: thinking-pulse 1s ease-in-out infinite;
}

@keyframes thinking-pulse {
  0%, 100% { opacity: 0.3; transform: scale(0.8); }
  50% { opacity: 1; transform: scale(1.2); }
}

.retry-bar {
  display: flex;
  gap: 8px;
  padding: 4px 0 14px;
}

.scroll-hint {
  position: absolute;
  bottom: 96px;
  left: 50%;
  transform: translateX(-50%);
  background: var(--el-color-primary);
  color: var(--panda-on-accent);
  font-size: 12px;
  padding: 5px 14px;
  border-radius: 999px;
  cursor: pointer;
  box-shadow: var(--panda-shadow-float);
  transition: opacity var(--panda-transition), transform var(--panda-transition);
}

.scroll-hint:hover {
  transform: translateX(-50%) translateY(-1px);
}

/* ── 中间档（≤1024px）：头部允许换行，隐藏 token 用量（次要信息） ── */
@media (max-width: 1024px) {
  .chat-header {
    flex-wrap: wrap;
    row-gap: 6px;
  }

  .token-usage {
    display: none;
  }

  .model-badge {
    max-width: 200px;
  }
}

/* ── 窄屏（≤768px）：会话栏抽屉化 + 头部换行收缩 ── */
.rail-toggle,
.rail-overlay {
  display: none;
}

@media (max-width: 768px) {
  /* .session-rail 是子组件根节点，父级 scoped 选择器可直接命中 */
  .session-rail {
    position: fixed;
    left: 0;
    top: 0;
    bottom: 0;
    z-index: 1002;
    width: 240px;
    transform: translateX(-105%);
    transition: transform 0.22s ease;
    box-shadow: var(--panda-shadow-float);
  }

  .chat-view.rail-open .session-rail {
    transform: translateX(0);
  }

  .rail-overlay {
    display: block;
    position: fixed;
    inset: 0;
    z-index: 1001;
    background: rgba(15, 23, 20, 0.45);
  }

  .rail-toggle {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    flex-shrink: 0;
    border: 1px solid var(--el-border-color-light);
    border-radius: 8px;
    background: var(--el-bg-color);
    font-size: 15px;
    cursor: pointer;
  }

  .chat-header {
    flex-wrap: wrap;
    row-gap: 6px;
    padding: 8px 12px;
  }

  .session-title {
    flex: 1;
    min-width: 0;
  }

  .header-right {
    flex-wrap: wrap;
    gap: 8px;
  }

  /* 次要元素窄屏隐藏 / 收缩 */
  .token-usage {
    display: none;
  }

  .model-badge {
    max-width: 140px;
  }

  .agent-select {
    width: 120px;
  }

  .msg-list-inner {
    padding: 12px;
  }

  .scroll-hint {
    bottom: 84px;
  }
}
</style>
