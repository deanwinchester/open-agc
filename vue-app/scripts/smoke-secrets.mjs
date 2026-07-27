// Node 冒烟脚本：验证 src/utils/secretDetect.js 的检测正则矩阵与占位符/打码替换纯函数。
// 每个模式覆盖一条命中 + 一条不误报；另测重叠去重、多处替换、打码与名称建议。
// 运行：node vue-app/scripts/smoke-secrets.mjs

import assert from 'node:assert/strict';
import {
  detectSecrets,
  maskSnippet,
  describeHit,
  suggestName,
  buildOutTexts,
  placeholderFor,
} from '../src/utils/secretDetect.js';

let passed = 0;
function ok(name) {
  passed += 1;
  console.log(`  ok - ${name}`);
}

function hitsOf(text) {
  return detectSecrets(text);
}

// ── 检测矩阵：每个模式一条命中 + 一条不误报 ──
console.log('检测矩阵');

// mongodb://（含凭据才命中）
{
  const h = hitsOf('连这个库 mongodb://mongoadm:Neu4ft9x@db.internal:50000/admin 快');
  assert.equal(h.length, 1);
  assert.equal(h[0].kind, 'uri');
  assert.equal(h[0].type, 'mongodb');
  assert.equal(h[0].username, 'mongoadm');
  assert.equal(h[0].password, 'Neu4ft9x');
  assert.equal(h[0].host, 'db.internal:50000');
  assert.equal(hitsOf('连接格式以 mongodb:// 开头，后面跟主机名').length, 0, '裸协议名无凭据不命中');
  ok('mongodb:// 命中 + 裸协议不误报');
}

// mysql://
{
  const h = hitsOf('mysql://root:S3cret!@10.0.0.8:3306/app');
  assert.equal(h.length, 1);
  assert.equal(h[0].type, 'mysql');
  assert.equal(h[0].password, 'S3cret!');
  assert.equal(hitsOf('mysql:// 只是协议说明').length, 0);
  ok('mysql:// 命中 + 不误报');
}

// postgres://（postgresql:// 同型）
{
  const h = hitsOf('postgres://pguser:pgpass@db:5432/x');
  assert.equal(h.length, 1);
  assert.equal(h[0].type, 'postgres');
  assert.equal(hitsOf('postgres://db:5432/x 没有用户名密码').length, 0, '无凭据 URI 不命中');
  ok('postgres:// 命中 + 无凭据 URI 不误报');
}

// redis://（:pass@ 无用户名形态）
{
  const h = hitsOf('redis://:Auth123@cache.internal:6379/0');
  assert.equal(h.length, 1);
  assert.equal(h[0].type, 'redis');
  assert.equal(h[0].username, '');
  assert.equal(h[0].password, 'Auth123');
  assert.equal(hitsOf('redis://localhost:6379 本地无密码').length, 0);
  ok('redis:// 命中 + 无密码本地串不误报');
}

// password= / password:
{
  const h = hitsOf('配置里 password=Sup3rSecret 别外发');
  assert.equal(h.length, 1);
  assert.equal(h[0].kind, 'kv');
  assert.equal(h[0].value, 'Sup3rSecret');
  assert.equal(hitsOf('password 复杂度要求至少 12 位').length, 0, 'password 后无 = : 不命中');
  ok('password[=:] 命中 + 散文不误报');
}

// sk- API key（≥16 位）
{
  const h = hitsOf('key 是 sk-abcdef0123456789ABCD 拿好');
  assert.equal(h.length, 1);
  assert.equal(h[0].kind, 'api_key');
  assert.equal(h[0].type, 'api_key');
  assert.equal(h[0].value, 'sk-abcdef0123456789ABCD');
  assert.equal(hitsOf('sk-short 太短不算').length, 0, '短于 16 位不命中');
  ok('sk- 命中 + 短串不误报');
}

// 密码是 / 密码：/ 密码:
{
  const h = hitsOf('数据库密码是 Abc12345，记一下');
  assert.equal(h.length, 1);
  assert.equal(h[0].kind, 'kv_zh');
  assert.equal(h[0].value.startsWith('Abc12345'), true);
  assert.equal(hitsOf('密码请妥善保管').length, 0, '「密码」单独出现不命中');
  ok('密码[:：是] 命中 + 散文不误报');
}

// ── 重叠去重与多处命中 ──
console.log('重叠与多处');

{
  // password=sk-xxxx 同时中 kv 与 api_key，保留先匹配的 kv，只弹一处
  const h = hitsOf('password=sk-abcdef0123456789ABCD');
  assert.equal(h.length, 1);
  assert.equal(h[0].kind, 'kv');
  ok('重叠命中去重（kv 先于 api_key）');
}

{
  const h = hitsOf('A 库 password=pw11111 ；B key sk-zzzzzzzzzzzzzzzz1');
  assert.equal(h.length, 2);
  ok('多处命中全部收集');
}

// ── 打码与名称建议 ──
console.log('打码与建议名');

assert.equal(maskSnippet('Neu4ft9x'), 'Neu••••t9x');
assert.equal(maskSnippet('abc'), '••••', '短串全打码');
const NOW = 1764000000000; // 固定时间戳，建议名可断言
assert.match(suggestName('mongodb', NOW), /^mongo_[a-z0-9]{6}$/);
assert.match(suggestName('api_key', NOW), /^key_[a-z0-9]{6}$/);
assert.match(suggestName('generic', NOW), /^secret_[a-z0-9]{6}$/);
ok('maskSnippet / suggestName');

{
  const h = hitsOf('mongodb://mongoadm:Neu4ft9x@db.internal:50000')[0];
  assert.equal(describeHit(h), 'mongodb: mongoadm/Neu••••t9x');
  ok('describeHit 打码展示（user/前3••••后3）');
}

// ── 占位符与三种产物 ──
console.log('占位符替换');

{
  const text = '用这个 mongodb://mongoadm:Neu4ft9x@db.internal:50000 导数据';
  const h = hitsOf(text);
  const out = buildOutTexts(text, h, 'mongo_abc123');
  assert.equal(placeholderFor(h[0], 'mongo_abc123'), '{{secret:mongo_abc123.uri}}');
  assert.equal(out.placeholderText, '用这个 {{secret:mongo_abc123.uri}} 导数据');
  assert.equal(out.displayText, '用这个 mongodb://mongoadm:********@db.internal:50000 导数据');
  assert.equal(out.discardText, '用这个 *** 导数据');
  assert.ok(!out.placeholderText.includes('Neu4ft9x'), '发送版无明文');
  assert.ok(!out.displayText.includes('Neu4ft9x'), '气泡版无明文');
  ok('URI → .uri 占位符；气泡仅密码段 ********；丢弃 ***');
}

{
  const text = '配置 password=Sup3rSecret 这样';
  const h = hitsOf(text);
  const out = buildOutTexts(text, h, 'secret_x1');
  assert.equal(out.placeholderText, '配置 password={{secret:secret_x1.password}} 这样');
  assert.equal(out.displayText, '配置 password=******** 这样');
  assert.equal(out.discardText, '配置 *** 这样');
  ok('kv → .password 占位符，键名保留');
}

{
  // 同一凭据出现两次，两处都替换
  const text = 'password=pw12345 再说一遍 password=pw12345';
  const h = hitsOf(text);
  assert.equal(h.length, 2);
  const out = buildOutTexts(text, h, 's2');
  assert.equal(
    out.placeholderText,
    'password={{secret:s2.password}} 再说一遍 password={{secret:s2.password}}'
  );
  ok('同一凭据多处出现全部替换');
}

// ── 值是前缀子串时的定位（Critical 回归：indexOf 错位导致明文外泄） ──
console.log('前缀子串定位');

{
  const text = 'password=password';
  const h = hitsOf(text);
  assert.equal(h.length, 1);
  const out = buildOutTexts(text, h, 'x1');
  assert.equal(out.placeholderText, 'password={{secret:x1.password}}', '占位符必须落在值位');
  assert.ok(!out.placeholderText.includes('=password'), '无残留明文');
  assert.equal(out.displayText, 'password=********');
  ok('password=password 值定位不外泄');
}

{
  const text = 'password=sword';
  const h = hitsOf(text);
  const out = buildOutTexts(text, h, 'x2');
  assert.equal(out.placeholderText, 'password={{secret:x2.password}}');
  assert.equal(out.displayText, 'password=********');
  assert.equal(out.discardText, '***');
  ok('password=sword 前缀内子串不错位');
}

// ── URI 路径段 → database ──
console.log('database 解析');

{
  const h = hitsOf('mongodb://mongoadm:Neu4ft9x@db.internal:50000/admin')[0];
  assert.equal(h.host, 'db.internal:50000');
  assert.equal(h.database, 'admin');
  const h2 = hitsOf('mysql://root:S3cret!@10.0.0.8:3306/app')[0];
  assert.equal(h2.database, 'app');
  const h3 = hitsOf('redis://:Auth123@cache.internal:6379/0')[0];
  assert.equal(h3.database, '0');
  const h4 = hitsOf('mongodb://u:p@host:50000')[0];
  assert.equal(h4.database, '', '无路径段时为空');
  ok('URI 路径段解析为 database');
}

console.log(`\n全部通过（${passed} 组断言）`);
