# 插件开发技能

当用户要求为本系统（Open-AGC）开发插件、扩展系统功能、增加菜单/页面/面板，或要求修改、修复已有插件时使用。关键词场景：开发插件、插件开发、扩展功能、加个页面、加个菜单、plugin。

## 架构红线（必须遵守）

1. **一律使用插件架构**：为本系统增加任何功能/页面/菜单，必须使用 `develop_plugin` 工具（扩展工具，先通过 search_available_tools 搜索「插件」启用）生成脚手架并在其基础上开发。
2. **禁止独立服务**：不得另起 FastAPI/Flask/Express 等服务、不得新开端口（如 8600）、不得另写独立前端页面。插件代码运行在主服务进程内，路由挂载在 `/api/plugin/<插件名>/` 下，视图自动出现在左侧菜单插件区。
3. **LLM 调用必须跟随系统设置**：插件内调用大模型一律使用 `core.llm_client.LLMClient()`（不传参数即为「设置」页配置的默认模型与密钥），禁止自行硬编码 API Key、base_url 或模型名。

## 开发流程

1. `search_available_tools` 搜索「插件」启用 `develop_plugin`。
2. 调用 `develop_plugin(action="scaffold", plugin_name="my-plugin", menu_label="显示名", has_static=True)` 生成脚手架（plugin.json、__init__.py、static/vue-entry.js）。
3. 按需编辑：
   - 后端：在 `__init__.py` 的 router 上加 API 路由（前缀 `/api/plugin/<名>/`），复杂路由放 routes.py。
   - 前端：`static/vue-entry.js` 是原生 ES module，default export `setup(ctx)` 返回 `{views: [{path, title, icon?, component}]}`；component 用 `ctx.Vue.defineComponent` 创建（模板字符串由主应用编译，el-* 组件直接用）。布局参照脚手架示例：整页容器 + 内容区 max-width 居中 + el-card，不要裸写无容器模板（否则右侧样式会坏）。
4. **热更新生效**：改完代码后调用 `POST /api/plugins/scan`（execute_shell 或 fetch_url 调本机 `http://localhost:8000/api/plugins/scan`，或提示用户在设置页点「扫描新插件」）——重新挂载路由与静态目录，**无需重启服务**。
5. 验证：检查 `/api/plugins` 列表含新插件、左侧菜单出现入口、视图路由 `/plugins/<名>/<path>` 可访问、插件 API 返回正常。
6. 出错时可用 `develop_plugin(action="install", plugin_name=..., init_code=...)` 校验代码；删除插件用 `DELETE /api/plugins/<名>`（同样随即调 scan 清理残留路由）。

## LLM 调用示例（插件后端内）

```python
from core.llm_client import LLMClient

llm = LLMClient()  # 跟随系统设置的默认模型与密钥
resp, model_used = llm.chat(messages=[{"role": "user", "content": "你好"}])
text = resp.choices[0].message.content
```

## 涉及工具

- `develop_plugin` — 生成/校验插件（扩展工具，先搜索启用）
- `search_available_tools` — 启用扩展工具
- `write_file` / `edit_file` — 编辑插件代码
- `execute_shell` / `fetch_url` — 调 POST /api/plugins/scan 热更新、验证接口
