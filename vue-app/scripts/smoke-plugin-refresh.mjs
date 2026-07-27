// Node 冒烟脚本：验证插件注册表热更新的纯逻辑部分
// （vue-app/src/plugins/registry-utils.js —— registry.js 的破缓存 URL、
//   重注册记录、导航项替换所依赖的全部纯函数）。
// 运行：node vue-app/scripts/smoke-plugin-refresh.mjs

import assert from 'node:assert/strict';
import {
  buildPluginEntryUrl,
  createRegistrationTracker,
  removeNavByName,
} from '../src/plugins/registry-utils.js';

let passed = 0;
function ok(name) {
  passed += 1;
  console.log(`  ok - ${name}`);
}

console.log('plugins/registry-utils.js');

// ---- buildPluginEntryUrl：import URL 带时间戳破缓存 ----
assert.equal(
  buildPluginEntryUrl('my-plugin', 'vue-entry.js', 123),
  '/static/plugins/my-plugin/vue-entry.js?t=123'
);
{
  const u1 = buildPluginEntryUrl('p', 'vue-entry.js', Date.now());
  const u2 = buildPluginEntryUrl('p', 'vue-entry.js', Date.now() + 1);
  assert.notEqual(u1, u2, '不同时间戳应产生不同 URL（破缓存）');
  assert.ok(u1.startsWith('/static/plugins/p/vue-entry.js?t='));
}
ok('buildPluginEntryUrl 破缓存 URL');

// ---- createRegistrationTracker：注册记录 / 重注册覆盖 / take 移除 ----
{
  const tr = createRegistrationTracker();
  assert.equal(tr.has('a'), false);
  assert.equal(tr.take('a'), null, '未注册时 take 返回 null');

  tr.set('a', ['plugin-a-main']);
  assert.equal(tr.has('a'), true);

  // 重注册（热更新）时覆盖旧记录
  tr.set('a', ['plugin-a-main', 'plugin-a-extra']);
  const rec = tr.take('a');
  assert.deepEqual(rec.routeNames, ['plugin-a-main', 'plugin-a-extra'], 'take 返回最新记录');
  assert.equal(tr.has('a'), false, 'take 后记录已移除');

  // set 时复制数组，外部修改不影响记录
  const names = ['plugin-b-main'];
  tr.set('b', names);
  names.push('mutated');
  assert.deepEqual(tr.take('b').routeNames, ['plugin-b-main']);

  tr.set('c', ['plugin-c-main']);
  tr.set('d', []);
  assert.deepEqual(tr.names().sort(), ['c', 'd']);
}
ok('createRegistrationTracker set/has/take/names');

// ---- removeNavByName：导航项原地替换 ----
{
  const nav = [{ name: 'a', views: [] }, { name: 'b', views: [] }];
  assert.equal(removeNavByName(nav, 'a'), true);
  assert.deepEqual(nav.map((n) => n.name), ['b']);
  assert.equal(removeNavByName(nav, 'a'), false, '重复移除返回 false');
  assert.equal(removeNavByName(nav, 'zzz'), false);
  assert.equal(nav.length, 1);
}
ok('removeNavByName 移除/幂等');

// ---- 模拟 registry.js 的热更新重注册流：removeRoute 旧路由 → 重新注册 ----
{
  // 伪 router，行为对齐 vue-router 的 addRoute/removeRoute/hasRoute
  const routes = new Map();
  const router = {
    addRoute(r) { routes.set(r.name, r); },
    removeRoute(name) { routes.delete(name); },
    hasRoute(name) { return routes.has(name); },
  };
  const nav = [];
  const tracker = createRegistrationTracker();

  // 与 registry.js loadPluginViews 相同的注册/重注册序列
  function register(plugin, views) {
    const old = tracker.take(plugin.name);
    if (old) {
      for (const rn of old.routeNames) {
        if (router.hasRoute(rn)) router.removeRoute(rn);
      }
    }
    removeNavByName(nav, plugin.name);
    const routeNames = [];
    for (const v of views) {
      const routeName = `plugin-${plugin.name}-${v.path}`;
      router.addRoute({ path: `/plugins/${plugin.name}/${v.path}`, name: routeName });
      routeNames.push(routeName);
    }
    nav.push({ name: plugin.name, views });
    tracker.set(plugin.name, routeNames);
  }

  register({ name: 'demo' }, [{ path: 'main' }]);
  register({ name: 'demo' }, [{ path: 'main' }, { path: 'extra' }]); // 热更新重注册

  assert.deepEqual([...routes.keys()].sort(), ['plugin-demo-extra', 'plugin-demo-main'],
    '重注册后无重复路由，且新视图已加入');
  assert.equal(nav.filter((n) => n.name === 'demo').length, 1, '导航项不重复');
  assert.equal(nav[0].views.length, 2, '导航项已替换为最新视图');

  // 全量刷新（refreshPluginViews 的移除阶段）
  for (const name of tracker.names()) {
    const rec = tracker.take(name);
    for (const rn of rec.routeNames) {
      if (router.hasRoute(rn)) router.removeRoute(rn);
    }
    removeNavByName(nav, name);
  }
  assert.equal(routes.size, 0);
  assert.equal(nav.length, 0);
}
ok('热更新重注册流（removeRoute 替换 + 导航去重 + 全量清除）');

console.log(`\nAll plugin-refresh smoke checks passed (${passed} groups).`);
