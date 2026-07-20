// API client: fetch 封装（JSON 解析 + 错误规范化），带 TTL 缓存与 in-flight 请求去重。
//
// 设计参考旧 static/js/cache.js 的意图，但修正其接口错位：
// 旧实现 TTL 表以资源名（'settings'）为 key，却用 URL 做 startsWith 匹配，永远不命中。
// 这里 TTL 表直接以 URL 前缀为 key，缓存逻辑通过 createApiClient 注入 fetch/now，
// 保持纯函数可测（见 vue-app/scripts/smoke.mjs，Node 可直接运行，无浏览器 API）。

export const TTL_TABLE = {
  '/api/tasks': 5000,
  '/api/settings': 60000,
  '/api/plugins': 30000,
  '/api/downloads': 10000,
};

// 规范化错误：HTTP 非 2xx 与网络层异常统一为 ApiError，附带 status/url/响应体。
export class ApiError extends Error {
  constructor(message, { status = 0, url = '', data = null } = {}) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.url = url;
    this.data = data;
  }
}

// 按 URL 前缀查 TTL，取最长匹配前缀；无匹配返回 0（不缓存）。
export function lookupTtl(url, ttlTable = TTL_TABLE) {
  const path = String(url).split('?')[0];
  let best = 0;
  let bestLen = -1;
  for (const [prefix, ttl] of Object.entries(ttlTable)) {
    if (path.startsWith(prefix) && prefix.length > bestLen) {
      best = ttl;
      bestLen = prefix.length;
    }
  }
  return best;
}

export function createApiClient({ fetchImpl, ttlTable = TTL_TABLE, now = () => Date.now() } = {}) {
  const doFetch = fetchImpl || ((...args) => fetch(...args));
  const cache = new Map();   // url -> { data, timestamp }
  const pending = new Map(); // url -> Promise（in-flight 去重）

  // 基础请求：JSON 解析 + 错误规范化。写操作（POST/DELETE 等）走这里，不进缓存。
  async function request(url, options = {}) {
    let res;
    try {
      res = await doFetch(url, options);
    } catch (err) {
      throw new ApiError(`网络请求失败: ${err.message}`, { url });
    }
    const text = await res.text();
    let data = null;
    if (text) {
      try { data = JSON.parse(text); } catch { data = text; }
    }
    if (!res.ok) {
      const detail = data && typeof data === 'object' && (data.detail || data.error || data.message);
      // FastAPI 422 的 detail 是对象数组，直接作 message 会变 [object Object]
      const msg = typeof detail === 'string' ? detail : detail ? JSON.stringify(detail) : null;
      throw new ApiError(msg || `HTTP ${res.status}`, { status: res.status, url, data });
    }
    return data;
  }

  // 带缓存的 GET：ttl 显式传入时优先，否则查 URL 前缀表；ttl<=0 直接透传。
  // 缓存命中（未过期）或在途请求直接复用；请求失败时回退到过期缓存（stale-if-error）。
  function cachedFetch(url, ttl) {
    const effectiveTtl = ttl ?? lookupTtl(url, ttlTable);
    if (!(effectiveTtl > 0)) return request(url);

    const entry = cache.get(url);
    if (entry && now() - entry.timestamp < effectiveTtl) {
      return Promise.resolve(entry.data);
    }

    if (pending.has(url)) return pending.get(url);

    const promise = request(url)
      .then((data) => {
        cache.set(url, { data, timestamp: now() });
        pending.delete(url);
        return data;
      })
      .catch((err) => {
        pending.delete(url);
        const stale = cache.get(url);
        if (stale) return stale.data;
        throw err;
      });

    pending.set(url, promise);
    return promise;
  }

  // 失效：无参数清空；否则按子串匹配删除（如 invalidate('/api/settings')）。
  function invalidate(pattern) {
    if (!pattern) {
      cache.clear();
      return;
    }
    for (const key of cache.keys()) {
      if (key.includes(pattern)) cache.delete(key);
    }
  }

  return { request, cachedFetch, invalidate, _cache: cache, _pending: pending };
}

// 默认实例：浏览器环境直接绑定全局 fetch。
const defaultClient = createApiClient();

export const request = defaultClient.request;
export const cachedFetch = defaultClient.cachedFetch;
export const invalidateCache = defaultClient.invalidate;
