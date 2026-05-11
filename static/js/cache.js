// Simple API response cache with TTL support
// Prevents redundant fetches when switching between views

const cache = new Map();
const pending = new Map(); // In-flight request dedup

const DEFAULTS = {
  settings: { ttl: 60000 },     // 1 min
  tasks: { ttl: 5000 },         // 5 sec
  downloads: { ttl: 10000 },    // 10 sec
  plugins: { ttl: 30000 },      // 30 sec
  sessions: { ttl: 30000 },     // 30 sec
  training: { ttl: 10000 },     // 10 sec
  llama: { ttl: 10000 },        // 10 sec
};

let stats = { hits: 0, misses: 0, sets: 0 };

export function getCacheTTL(key) {
  for (const [prefix, config] of Object.entries(DEFAULTS)) {
    if (key.startsWith(prefix)) return config.ttl;
  }
  return 0; // no cache
}

export async function cachedFetch(url, options = {}) {
  const ttl = options.ttl ?? getCacheTTL(url);
  if (ttl <= 0) return fetch(url).then(r => r.json());

  const now = Date.now();
  const entry = cache.get(url);

  if (entry && now - entry.timestamp < ttl) {
    stats.hits++;
    return entry.data;
  }

  // Dedup in-flight requests
  if (pending.has(url)) {
    stats.hits++;
    return pending.get(url);
  }

  stats.misses++;
  const promise = fetch(url)
    .then(r => {
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return r.json();
    })
    .then(data => {
      cache.set(url, { data, timestamp: Date.now() });
      stats.sets++;
      pending.delete(url);
      return data;
    })
    .catch(err => {
      pending.delete(url);
      // On error, return stale cache if available
      const stale = cache.get(url);
      if (stale) return stale.data;
      throw err;
    });

  pending.set(url, promise);
  return promise;
}

export function invalidateCache(pattern) {
  if (!pattern) {
    cache.clear();
    return;
  }
  for (const key of cache.keys()) {
    if (key.includes(pattern)) cache.delete(key);
  }
}

export function getCacheStats() {
  return { ...stats, size: cache.size };
}

// Auto-invalidate on certain events
export function initCacheAutoInvalidate() {
  // Settings save invalidates settings cache
  const origFetch = window.fetch;
  const patchedFetch = async (url, options) => {
    const res = await origFetch(url, options);
    if (options?.method === 'POST' && typeof url === 'string') {
      if (url === '/api/settings') invalidateCache('/api/settings');
      if (url.startsWith('/api/tasks/') && (options.body?.includes('DELETE') || url.includes('/interrupt'))) {
        invalidateCache('/api/tasks');
      }
      if (url.startsWith('/api/plugins/')) invalidateCache('/api/plugins');
    }
    return res;
  };
  // Only override if we can
  try { window.fetch = patchedFetch; } catch (e) { /* non-critical */ }
}
