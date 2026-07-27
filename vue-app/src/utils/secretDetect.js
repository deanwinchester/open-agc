// 凭据检测与文本变换的纯函数模块（无浏览器依赖，Node 可直接测，
// 见 vue-app/scripts/smoke-secrets.mjs）。
//
// 设计原则：保守，宁缺勿滥。
// - URI 类（mongodb/mysql/postgres/redis）只在 userinfo 含密码（user:pass@ 或 :pass@）时命中，
//   纯协议名（如 "请用 mongodb:// 连接"）不弹窗；
// - password=/密码是 等键值型只截取右侧非空白值；
// - sk- 要求 ≥16 位字母数字，短串不命中。
//
// 三类文本产物（由 buildOutTexts 一次产出）：
//   placeholderText — 发给后端：命中片段替换为 {{secret:<name>.uri|.password}}
//   displayText     — 聊天气泡：秘密值部分替换为 ********（URI 仅密码段，其余保留）
//   discardText     — 丢弃并打码：命中片段整体替换为 ***（不入库时发送与显示同用）

// ── 检测模式 ──
// 每条：{ kind, type, re }。re 必须带 g 标志；命名组：
//   full  — 整个凭据片段（省略时为整 match）
//   value — 片段中的秘密值部分（URI 的密码段 / 键值右侧值；省略时同 full）
const URI_SCHEME_TYPE = {
  mongodb: 'mongodb',
  'mongodb+srv': 'mongodb',
  mysql: 'mysql',
  postgres: 'postgres',
  postgresql: 'postgres',
  redis: 'redis',
};

export const SECRET_PATTERNS = [
  {
    kind: 'uri',
    // userinfo 必须含冒号（即有密码）：user:pass@ 或 redis 的 :pass@
    re: /\b(?<scheme>mongodb(?:\+srv)?|mysql|postgres(?:ql)?|redis):\/\/(?<userinfo>[^\s/@]*:[^\s/@]+)@(?<hostpart>[^\s"'<>]+)/gi,
  },
  {
    kind: 'kv',
    type: 'generic',
    re: /\b(?<prefix>password\s*[=:]\s*)(?<value>\S+)/gi,
  },
  {
    kind: 'api_key',
    type: 'api_key',
    re: /\b(?<value>sk-[A-Za-z0-9]{16,})\b/g,
  },
  {
    kind: 'kv_zh',
    type: 'generic',
    re: /(?<prefix>密码\s*[:：是]\s*)(?<value>\S+)/g,
  },
];

// 名称自动建议的类型前缀（suggestName）
const NAME_PREFIX = {
  mongodb: 'mongo',
  mysql: 'mysql',
  postgres: 'pg',
  redis: 'redis',
  api_key: 'key',
  generic: 'secret',
};

export const SECRET_NAME_RE = /^[A-Za-z0-9_-]+$/;

// ── 检测 ──
// 返回按出现位置排序、去重叠的命中数组：
//   { kind, type, match, value, start, end, valueStart, valueEnd, username, host, password }
// start/end 针对整片段；valueStart/valueEnd 针对秘密值部分（无独立值时同 start/end）。
export function detectSecrets(text) {
  if (!text) return [];
  const hits = [];
  for (const p of SECRET_PATTERNS) {
    p.re.lastIndex = 0;
    let m;
    while ((m = p.re.exec(text)) !== null) {
      const g = m.groups || {};
      let start = m.index;
      let end = m.index + m[0].length;
      let match = m[0];
      let valueStart = start;
      let valueEnd = end;
      if (g.value != null) {
        // value 恒为匹配的尾部组，用长度差确定性定位；
        // 不能用 indexOf（值是前缀子串时会错位导致明文外泄，如 password=password）
        valueStart = start + (m[0].length - g.value.length);
        valueEnd = valueStart + g.value.length;
      }
      const hit = {
        kind: p.kind,
        type: p.type || URI_SCHEME_TYPE[(g.scheme || '').toLowerCase()] || 'generic',
        match,
        value: g.value != null ? g.value : match,
        start,
        end,
        valueStart,
        valueEnd,
        username: '',
        host: '',
        password: '',
        database: '',
      };
      if (p.kind === 'uri') {
        // userinfo 形如 user:pass 或 :pass（redis）；hostpart 形如 host:port/db?...
        const ui = g.userinfo || '';
        const ci = ui.indexOf(':');
        hit.username = ui.slice(0, ci);
        hit.password = ui.slice(ci + 1);
        const hp = g.hostpart || '';
        hit.host = hp.replace(/\/.*$/, '');
        // 路径段作为 database 入库（如 mongodb://u:p@h:50000/admin → admin），
        // 去查询串/锚点；无路径则为 ''
        const slash = hp.indexOf('/');
        if (slash >= 0) {
          hit.database = hp.slice(slash + 1).split(/[?#]/)[0].replace(/\/+$/, '');
        }
        // URI 的秘密值部分 = 密码段；位置由 userinfo 内冒号确定性推导，
        // 不能用 indexOf 搜（username 与 password 同串时会错位）
        if (hit.password) {
          const uiStart = match.indexOf('://') + 3;
          hit.value = hit.password;
          hit.valueStart = start + uiStart + ci + 1;
          hit.valueEnd = hit.valueStart + hit.password.length;
        }
      } else {
        hit.password = hit.value;
      }
      hits.push(hit);
      if (m[0].length === 0) p.re.lastIndex += 1; // 防零宽死循环
    }
  }
  // 按位置排序并丢弃与前一命中重叠的（如 password=sk-xxxx 同时中 kv 与 api_key，保留先匹配的 kv）
  hits.sort((a, b) => a.start - b.start || b.end - a.end);
  const out = [];
  let lastEnd = -1;
  for (const h of hits) {
    if (h.start < lastEnd) continue;
    out.push(h);
    lastEnd = h.end;
  }
  return out;
}

// ── 片段打码（弹窗内展示）：前 3 后 3，中间 ••••；短串全打码 ──
export function maskSnippet(s) {
  const v = String(s == null ? '' : s);
  if (v.length <= 6) return '••••';
  return v.slice(0, 3) + '••••' + v.slice(-3);
}

// 命中片段的弹窗展示文本：URI 显示 <username>/<打码密码>，其余显示打码值
export function describeHit(hit) {
  if (hit.kind === 'uri') {
    const user = hit.username ? hit.username + '/' : '';
    return `${hit.type}: ${user}${maskSnippet(hit.password)}`;
  }
  return `${hit.type}: ${maskSnippet(hit.value)}`;
}

// ── 名称自动建议：类型前缀 + 时间戳 base36 后 6 位（如 mongo_8f2a1c） ──
export function suggestName(type, now = Date.now()) {
  const prefix = NAME_PREFIX[type] || 'secret';
  return `${prefix}_${now.toString(36).slice(-6)}`;
}

// ── 文本变换（从后往前替换，索引不失效） ──
function replaceSpans(text, spans) {
  let out = text;
  const sorted = [...spans].sort((a, b) => b.start - a.start);
  for (const s of sorted) {
    out = out.slice(0, s.start) + s.replacement + out.slice(s.end);
  }
  return out;
}

export function placeholderFor(hit, name) {
  // 完整 uri → .uri；键值/密钥 → .password
  return hit.kind === 'uri' ? `{{secret:${name}.uri}}` : `{{secret:${name}.password}}`;
}

// 三种产物一次算清。name 仅用于 placeholderText。
export function buildOutTexts(text, hits, name) {
  const placeholderSpans = [];
  const displaySpans = [];
  const discardSpans = [];
  for (const h of hits) {
    // URI 整段 → .uri；键值型只替换秘密值部分，保留键名前缀（password= / 密码是），
    // 让模型知道该位置是什么字段
    if (h.kind === 'uri') {
      placeholderSpans.push({ start: h.start, end: h.end, replacement: placeholderFor(h, name) });
    } else {
      placeholderSpans.push({ start: h.valueStart, end: h.valueEnd, replacement: placeholderFor(h, name) });
    }
    // 气泡：仅秘密值部分 → ********，键值前缀（password= / 密码是）与 URI 结构保留
    displaySpans.push({ start: h.valueStart, end: h.valueEnd, replacement: '********' });
    // 丢弃：整片段 → ***
    discardSpans.push({ start: h.start, end: h.end, replacement: '***' });
  }
  return {
    placeholderText: replaceSpans(text, placeholderSpans),
    displayText: replaceSpans(text, displaySpans),
    discardText: replaceSpans(text, discardSpans),
  };
}
