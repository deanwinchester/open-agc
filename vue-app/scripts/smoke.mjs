// Node 冒烟脚本：验证 api/client.js 的 TTL 缓存 / in-flight 去重 / 错误规范化，
// 以及 stores/ws.js 的纯逻辑（退避表、事件分发）。
// 仅覆盖无浏览器依赖的纯函数部分（fetch/now 均为注入实现）。
// 运行：node vue-app/scripts/smoke.mjs

import assert from 'node:assert/strict';
import { createApiClient, lookupTtl, ApiError } from '../src/api/client.js';
import { reconnectDelay, createDispatcher } from '../src/stores/ws.js';

let passed = 0;
function ok(name) {
  passed += 1;
  console.log(`  ok - ${name}`);
}

// ---- 测试工具 ----
function jsonResponse(data, { ok = true, status = 200 } = {}) {
  return { ok, status, text: async () => JSON.stringify(data) };
}

function countingFetch(handler) {
  const fn = async (url, options) => {
    fn.calls.push(url);
    return handler(url, options);
  };
  fn.calls = [];
  return fn;
}

// ---- api/client.js ----
console.log('api/client.js');

// TTL 前缀表匹配（修正旧 cache.js 的接口错位：key 是 URL 前缀而非资源名）
assert.equal(lookupTtl('/api/tasks'), 5000);
assert.equal(lookupTtl('/api/tasks/123'), 5000);
assert.equal(lookupTtl('/api/tasks?status=running'), 5000, '查询串不影响匹配');
assert.equal(lookupTtl('/api/settings'), 60000);
assert.equal(lookupTtl('/api/plugins'), 30000);
assert.equal(lookupTtl('/api/downloads'), 10000);
assert.equal(lookupTtl('/api/downloads/history'), 10000, '子路径继承前缀 TTL');
assert.equal(lookupTtl('/api/unknown'), 0, '未匹配前缀不缓存');
ok('lookupTtl 前缀匹配');

// 缓存命中：TTL 内第二次调用不再发请求
{
  let nowMs = 1000;
  const fetchImpl = countingFetch(async () => jsonResponse({ list: [1] }));
  const client = createApiClient({ fetchImpl, now: () => nowMs });

  const a = await client.cachedFetch('/api/tasks');
  const b = await client.cachedFetch('/api/tasks');
  assert.deepEqual(a, { list: [1] });
  assert.deepEqual(b, { list: [1] });
  assert.equal(fetchImpl.calls.length, 1, 'TTL 内应命中缓存');

  nowMs += 4999;
  await client.cachedFetch('/api/tasks');
  assert.equal(fetchImpl.calls.length, 1, 'TTL 边界内仍命中');

  nowMs += 2; // 超过 5000ms
  await client.cachedFetch('/api/tasks');
  assert.equal(fetchImpl.calls.length, 2, 'TTL 过期后重新请求');
  ok('TTL 缓存命中与过期');
}

// 显式 ttl 参数优先于前缀表；表外 URL 也可缓存
{
  let nowMs = 0;
  const fetchImpl = countingFetch(async () => jsonResponse({ v: 1 }));
  const client = createApiClient({ fetchImpl, now: () => nowMs });

  await client.cachedFetch('/api/other', 10000);
  await client.cachedFetch('/api/other', 10000);
  assert.equal(fetchImpl.calls.length, 1, '显式 ttl 使表外 URL 也走缓存');

  await client.cachedFetch('/api/settings', 0);
  await client.cachedFetch('/api/settings', 0);
  assert.equal(fetchImpl.calls.length, 3, 'ttl=0 绕过缓存（覆盖表内 60000）');
  ok('显式 ttl 覆盖前缀表');
}

// in-flight 去重：并发同一 URL 只发一次请求，调用方共享同一 Promise 结果
{
  let nowMs = 0;
  let release;
  const gate = new Promise((r) => { release = r; });
  const fetchImpl = countingFetch(async () => {
    await gate;
    return jsonResponse({ n: 42 });
  });
  const client = createApiClient({ fetchImpl, now: () => nowMs });

  const p1 = client.cachedFetch('/api/plugins');
  const p2 = client.cachedFetch('/api/plugins');
  const p3 = client.cachedFetch('/api/plugins');
  release();
  const [r1, r2, r3] = await Promise.all([p1, p2, p3]);
  assert.equal(fetchImpl.calls.length, 1, '并发请求应去重为 1 次');
  assert.deepEqual(r1, { n: 42 });
  assert.deepEqual(r2, r1);
  assert.deepEqual(r3, r1);
  ok('in-flight 请求去重');

  // 去重窗口结束后，缓存接管：后续调用仍不发请求
  await client.cachedFetch('/api/plugins');
  assert.equal(fetchImpl.calls.length, 1);
  ok('去重完成后结果进入缓存');
}

// stale-if-error：缓存过期后请求失败，回退到过期数据
{
  let nowMs = 0;
  let shouldFail = false;
  const fetchImpl = countingFetch(async () => {
    if (shouldFail) throw new Error('boom');
    return jsonResponse({ ok: true });
  });
  const client = createApiClient({ fetchImpl, now: () => nowMs });

  await client.cachedFetch('/api/downloads');
  nowMs += 20000; // 过期
  shouldFail = true;
  const data = await client.cachedFetch('/api/downloads');
  assert.deepEqual(data, { ok: true }, '请求失败应回退到 stale 缓存');
  ok('stale-if-error 回退');
}

// 错误规范化：非 2xx → ApiError（带 status 与响应体里的 detail）；无缓存时失败直接抛出
{
  const fetchImpl = countingFetch(async () =>
    jsonResponse({ detail: '任务不存在' }, { ok: false, status: 404 })
  );
  const client = createApiClient({ fetchImpl });

  await assert.rejects(
    client.request('/api/tasks/999'),
    (err) => {
      assert.ok(err instanceof ApiError);
      assert.equal(err.status, 404);
      assert.equal(err.message, '任务不存在');
      return true;
    }
  );

  await assert.rejects(client.cachedFetch('/api/tasks/999'), (err) => err instanceof ApiError);
  ok('错误规范化（ApiError）');
}

// 网络层异常同样规范化为 ApiError
{
  const fetchImpl = countingFetch(async () => { throw new TypeError('fetch failed'); });
  const client = createApiClient({ fetchImpl });
  await assert.rejects(client.request('/api/settings'), (err) => {
    assert.ok(err instanceof ApiError);
    assert.equal(err.status, 0);
    assert.match(err.message, /网络请求失败/);
    return true;
  });
  ok('网络异常规范化');
}

// invalidate：按子串失效 / 全清
{
  let nowMs = 0;
  const fetchImpl = countingFetch(async () => jsonResponse({}));
  const client = createApiClient({ fetchImpl, now: () => nowMs });

  await client.cachedFetch('/api/tasks');
  await client.cachedFetch('/api/settings');
  client.invalidate('/api/tasks');
  await client.cachedFetch('/api/tasks');
  await client.cachedFetch('/api/settings');
  assert.equal(fetchImpl.calls.length, 3, '仅 /api/tasks 被失效');
  client.invalidate();
  await client.cachedFetch('/api/settings');
  assert.equal(fetchImpl.calls.length, 4, '无参 invalidate 清空全部');
  ok('invalidate 缓存失效');
}

// ---- stores/ws.js（纯逻辑部分） ----
console.log('stores/ws.js');

// 退避表：1s 起步 ×2 递增，30s 封顶（对齐旧 app.js）
assert.equal(reconnectDelay(0), 1000);
assert.equal(reconnectDelay(1), 2000);
assert.equal(reconnectDelay(2), 4000);
assert.equal(reconnectDelay(3), 8000);
assert.equal(reconnectDelay(4), 16000);
assert.equal(reconnectDelay(5), 30000, '第 5 次起封顶 30s');
assert.equal(reconnectDelay(10), 30000, '高次重连仍封顶');
ok('reconnectDelay 指数退避 1s→30s 封顶');

// 事件按 type 分发 / 退订
{
  const d = createDispatcher();
  const seen = [];
  const onProgress = (e) => seen.push(['progress', e.step]);
  const onMessage = (e) => seen.push(['message', e.role]);

  d.on('progress', onProgress);
  d.on('message', onMessage);
  d.dispatch({ type: 'progress', step: 1 });
  d.dispatch({ type: 'message', role: 'agent' });
  d.dispatch({ type: 'status', message: 'thinking' }); // 无订阅者，不报错
  d.dispatch({}); // 无 type，忽略
  assert.deepEqual(seen, [['progress', 1], ['message', 'agent']], '按 type 精确分发');

  d.off('progress', onProgress);
  d.dispatch({ type: 'progress', step: 2 });
  assert.deepEqual(seen, [['progress', 1], ['message', 'agent']], 'off 后不再收到');

  // on 返回的退订函数
  const offMessage = d.on('message', (e) => seen.push(['message2', e.role]));
  offMessage();
  d.dispatch({ type: 'message', role: 'system' });
  assert.deepEqual(seen.at(-1), ['message', 'system'], '退订函数生效');
  ok('dispatcher on/off/dispatch');
}

// 订阅者抛错不影响其他订阅者（静默 console.error，避免干扰输出）
{
  const d = createDispatcher();
  const seen = [];
  const origError = console.error;
  console.error = () => {};
  try {
    d.on('error', () => { throw new Error('subscriber bug'); });
    d.on('error', (e) => seen.push(e.content));
    d.dispatch({ type: 'error', content: 'x' });
  } finally {
    console.error = origError;
  }
  assert.deepEqual(seen, ['x']);
  ok('订阅者异常隔离');
}

console.log(`\nAll smoke checks passed (${passed} groups).`);
