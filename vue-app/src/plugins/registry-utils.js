// registry.js 的纯逻辑部分（无 Vue/DOM/网络依赖），单独抽出以便 node 冒烟测试
// （vue-app/scripts/smoke-plugin-refresh.mjs 直接 import 本文件）。

// 插件前端入口 URL：?t= 时间戳破除浏览器缓存，热更新（重新扫描）时强制
// 重新加载最新 vue-entry.js，而不是沿用首次 import 的旧代码。
export function buildPluginEntryUrl(name, entry, ts) {
  return `/static/plugins/${name}/${entry}?t=${ts}`;
}

// 已注册插件视图的记录表：name -> { routeNames: [] }。
// 重注册前先 take() 取出旧记录，用于 removeRoute 移除旧路由。
export function createRegistrationTracker() {
  const records = new Map();
  return {
    has: (name) => records.has(name),
    set(name, routeNames) {
      records.set(name, { routeNames: [...(routeNames || [])] });
    },
    // 取出并删除记录；未注册过时返回 null
    take(name) {
      const rec = records.get(name) || null;
      records.delete(name);
      return rec;
    },
    names: () => [...records.keys()],
  };
}

// 从导航数组中移除某插件的导航项（原地 splice，与响应式 pluginNav 语义一致）。
// 返回是否实际移除。
export function removeNavByName(nav, name) {
  const idx = nav.findIndex((n) => n && n.name === name);
  if (idx === -1) return false;
  nav.splice(idx, 1);
  return true;
}
