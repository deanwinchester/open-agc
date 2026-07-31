// 插件 UI 契约注册表（新 SPA 侧）。
//
// 契约（与 dev-docs/API契约.md「插件 Vue 视图契约」一节保持一致）：
//   1. 插件 manifest（plugin.json）声明可选字段 "vue_entry"，如 "vue_entry": "vue-entry.js"，
//      入口文件经插件静态目录暴露：/static/plugins/<name>/<vue_entry>。
//   2. GET /api/plugins 返回的每个插件对象带有 vue_entry 字段（core/plugin_manager.py 透传）。
//      仅当插件 loaded && enabled && vue_entry 非空时才加载其前端。
//   3. 入口模块是一个原生 ES module，default export 为函数 setup(ctx)，
//      同步或异步返回 { views: [{ path, title, icon?, component }] }。
//      component 是用 ctx.Vue 创建的组件定义（模板字符串由主应用的
//      vue.esm-bundler 运行时编译器编译；Element Plus 已在主应用全局注册，
//      插件模板可直接使用 el-* 组件，主题变量共享）。
//   4. 每个 view 注册为路由 /plugins/<name>/<path>，并出现在侧边栏插件区。
//
// ctx（setup 的唯一参数）：
//   - pluginName: 插件名
//   - Vue:        主应用的 Vue 命名空间（defineComponent/ref/computed/...），
//                 插件必须用它创建组件，保证与主应用同一 Vue 实例
//   - apiFetch:   主应用 api client 的 request(url, options)（JSON 解析 + 错误规范化，
//                 与 fetch 参数一致；插件自行拼接自己的 API 前缀）
//   - ElMessage / ElMessageBox: Element Plus 反馈组件（插件无法自行 import element-plus）
//   - wsOn(type, fn): 订阅主应用 WebSocket 事件（返回退订函数；未连接时自动建立连接）
//   - navigate(path): router.push 封装（插件内跳转，如创建训练后跳到监控页）
//
// 热更新：后端 POST /api/plugins/scan 重新加载插件代码后，前端调用
// refreshPluginViews()（PluginsView 的扫描动作已接线）：import URL 带 ?t=
// 时间戳破缓存重新拉取 vue-entry.js，旧路由经 router.removeRoute 移除后
// 重新注册，旧导航项同步替换 —— 无需刷新页面、无需重启服务。

import * as Vue from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { request } from '../api/client';
import { useWsStore } from '../stores/ws';
import { buildPluginEntryUrl, createRegistrationTracker, removeNavByName } from './registry-utils';

// 侧边栏插件导航（响应式，App.vue 直接渲染）。
// 元素形状：{ name, label, icon, views: [{ path, title, icon? }] }
export const pluginNav = Vue.reactive([]);
// 插件前端加载错误（响应式）：name -> 错误信息。插件管理页据此展示缺陷，
// 不再静默 console.error（生产实证：插件 vue-entry 报错时菜单无任何提示）。
export const pluginErrors = Vue.reactive({});

function setPluginError(name, err) {
  const msg = (err && (err.message || String(err))) || '未知错误';
  pluginErrors[name] = msg;
  console.error(`[plugins] 加载插件视图失败: ${name}`, err);
}

function makeCtx(router, plugin) {
  return {
    pluginName: plugin.name,
    Vue,
    apiFetch: request,
    ElMessage,
    ElMessageBox,
    wsOn(type, fn) {
      const ws = useWsStore();
      if (!ws.connected) ws.connect();
      return ws.on(type, fn); // 返回退订函数，组件卸载时调用
    },
    navigate(path) {
      router.push(path);
    },
  };
}

// 已注册插件视图的记录：name -> { routeNames: [] }。
// 作用一：保证 initPluginRegistry 可重入（重复调用不产生重复路由/导航）；
// 作用二：重注册/刷新时按记录 removeRoute 移除旧路由。
const _tracker = createRegistrationTracker();
// 首次 init 时保存的 router，供 refreshPluginViews() 使用。
let _router = null;

// 移除某插件已注册的视图（路由 + 侧边栏导航项）；未注册过则为无操作。
function removePluginRegistration(router, name) {
  const rec = _tracker.take(name);
  if (rec) {
    for (const routeName of rec.routeNames) {
      if (router.hasRoute(routeName)) router.removeRoute(routeName);
    }
  }
  removeNavByName(pluginNav, name);
}

async function loadPluginViews(router, plugin) {
  // 同一插件重新注册（热更新）前，先移除其旧路由与旧导航项
  removePluginRegistration(router, plugin.name);
  delete pluginErrors[plugin.name];
  try {
    // import URL 加时间戳破浏览器缓存，保证拿到最新 vue-entry.js
    const url = buildPluginEntryUrl(plugin.name, plugin.vue_entry, Date.now());
    const mod = await import(/* @vite-ignore */ url);
    const setup = mod.default;
    if (typeof setup !== 'function') {
      setPluginError(plugin.name, 'vue_entry default export 不是函数');
      return [];
    }
    const result = await setup(makeCtx(router, plugin));
    const views = (result && result.views) || [];
    const navViews = [];
    const routeNames = [];
    for (const v of views) {
      if (!v || !v.path || !v.component) continue;
    const fullPath = `/plugins/${plugin.name}/${v.path}`;
    const routeName = `plugin-${plugin.name}-${v.path}`;
    router.addRoute({
      path: fullPath,
      name: routeName,
      component: v.component,
      meta: { title: v.title || v.path, plugin: plugin.name },
    });
    routeNames.push(routeName);
    navViews.push({ path: fullPath, title: v.title || v.path, icon: v.icon || '' });
  }
    if (navViews.length) {
      pluginNav.push({
        name: plugin.name,
        label: (plugin.menu && plugin.menu.label) || plugin.name,
        icon: (plugin.menu && plugin.menu.icon) || '🧩',
        views: navViews,
      });
    }
    return routeNames;
  } catch (err) {
    setPluginError(plugin.name, err);
    return [];
  }
}

// 应用启动时调用一次（main.js，mount 之后，保证 pinia 已激活）。
// 拉取失败（如服务器正忙/扫描阻塞窗口）时重试——否则本次会话插件菜单
// 一直空白，只能整页刷新（生产实证：scan 阻塞期间重载页面即复现）。
const _INIT_RETRY_MAX = 3;
const _INIT_RETRY_MS = 3000;
let _initRetryTimer = null;

export async function initPluginRegistry(router, attempt = 1) {
  _router = router;
  let data;
  try {
    data = await request('/api/plugins');
  } catch (err) {
    console.warn(`[plugins] /api/plugins 请求失败(第${attempt}次):`, err);
    if (attempt < _INIT_RETRY_MAX && _tracker.size === 0) {
      clearTimeout(_initRetryTimer);
      _initRetryTimer = setTimeout(() => initPluginRegistry(router, attempt + 1), _INIT_RETRY_MS);
    }
    return;
  }
  // 成功拉到数据后清理可能存在的重试计时器
  clearTimeout(_initRetryTimer);
  _initRetryTimer = null;
  // /api/plugins 会合并多个插件目录的扫描结果，已加载插件可能出现重复条目
  // （且重复条目可能带陈旧的 enabled 状态）——按 name 去重，first-wins：
  // 先出现的条目来自已加载列表，状态最准确。
  const seen = new Set();
  const plugins = [];
  for (const p of data.plugins || []) {
    if (!(p.loaded && p.enabled && p.vue_entry)) continue;
    if (seen.has(p.name)) continue;
    seen.add(p.name);
    plugins.push(p);
  }
  for (const p of plugins) {
    if (_tracker.has(p.name)) continue;
    try {
      const routeNames = await loadPluginViews(router, p);
      _tracker.set(p.name, routeNames); // 成功后才标记，失败允许下次重试
    } catch (err) {
      console.error(`[plugins] 加载插件视图失败: ${p.name}`, err);
    }
  }
  // 插件路由是 mount 后异步 addRoute 的，直接打开/刷新 /app/plugins/... 时
  // catch-all 路由已先匹配并重定向到 /chat。注册完成后 replace 当前路径触发
  // 重解析，让新注册的插件路由接管；对相同路径的 replace 是安全无副作用的。
  router.replace(router.currentRoute.value.fullPath);
}

// 后端扫描（POST /api/plugins/scan）成功后调用：清掉全部已注册插件视图
// （removeRoute + 移除导航项），再重新拉取 /api/plugins 并注册。
// vue-entry.js 经 ?t= 时间戳重新加载，前端热更新无需刷新页面。
export async function refreshPluginViews() {
  if (!_router) return;
  for (const name of _tracker.names()) {
    removePluginRegistration(_router, name);
  }
  await initPluginRegistry(_router);
}
