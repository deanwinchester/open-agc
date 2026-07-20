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

import * as Vue from 'vue';
import { ElMessage, ElMessageBox } from 'element-plus';
import { request } from '../api/client';
import { useWsStore } from '../stores/ws';

// 侧边栏插件导航（响应式，App.vue 直接渲染）。
// 元素形状：{ name, label, icon, views: [{ path, title, icon? }] }
export const pluginNav = Vue.reactive([]);

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

async function loadPluginViews(router, plugin) {
  const url = `/static/plugins/${plugin.name}/${plugin.vue_entry}`;
  const mod = await import(/* @vite-ignore */ url);
  const setup = mod.default;
  if (typeof setup !== 'function') {
    console.warn(`[plugins] ${plugin.name}: vue_entry default export 不是函数，跳过`);
    return;
  }
  const result = await setup(makeCtx(router, plugin));
  const views = (result && result.views) || [];
  const navViews = [];
  for (const v of views) {
    if (!v || !v.path || !v.component) continue;
    const fullPath = `/plugins/${plugin.name}/${v.path}`;
    router.addRoute({
      path: fullPath,
      name: `plugin-${plugin.name}-${v.path}`,
      component: v.component,
      meta: { title: v.title || v.path, plugin: plugin.name },
    });
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
}

// 已完成视图注册的插件名（保证 initPluginRegistry 可重入：重复调用不产生重复路由/导航）。
const _registered = new Set();

// 应用启动时调用一次（main.js，mount 之后，保证 pinia 已激活）。
export async function initPluginRegistry(router) {
  let data;
  try {
    data = await request('/api/plugins');
  } catch (err) {
    console.warn('[plugins] /api/plugins 请求失败，跳过插件视图注册:', err);
    return;
  }
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
    if (_registered.has(p.name)) continue;
    try {
      await loadPluginViews(router, p);
      _registered.add(p.name); // 成功后才标记，失败允许下次重试
    } catch (err) {
      console.error(`[plugins] 加载插件视图失败: ${p.name}`, err);
    }
  }
  // 插件路由是 mount 后异步 addRoute 的，直接打开/刷新 /app/plugins/... 时
  // catch-all 路由已先匹配并重定向到 /chat。注册完成后 replace 当前路径触发
  // 重解析，让新注册的插件路由接管；对相同路径的 replace 是安全无副作用的。
  router.replace(router.currentRoute.value.fullPath);
}
