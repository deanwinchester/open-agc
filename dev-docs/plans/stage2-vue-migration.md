# 阶段 2：Vue3 前端迁移 — 实施计划

> 来源：总方案（界面升级与功能优化）阶段 2。目标：用 Vue3 SPA 逐步替换现有原生 JS 前端，新旧并存、逐视图替换、每步可验证。
> 全局约束：
> - 旧前端（static/index.html + static/js/* + static/dist/* IIFE 包）在新 SPA 达到同等功能前**必须保持可用**，不得删改其行为
> - 新 SPA 路径前缀 `/app`，静态产物输出到 `static/vue/`（该目录已在 gitignore 或应加入）
> - 后端 API 契约以 `dev-docs/API契约.md` 为准；/api/settings 为增量语义（只提交非 None 字段）
> - 不执行任何 git 操作（由控制者统一提交）；文件一律 UTF-8
> - 视觉延续熊猫主题：色值以 `static/css/variables.css` 为准，通过 Element Plus CSS 变量覆盖
> - 验证方式：每个任务完成后 `npm run build:vue` 成功 + 既有 `python -m pytest tests/ -q` 不回归

## Task 1：Vue3 脚手架与构建管线

- 新建 `vue-app/` 目录：`vue-app/vite.config.js`（插件 `@vitejs/plugin-vue`，root 为 vue-app/，build.outDir 为 `../static/vue`，`emptyOutDir: true`，`base: '/static/vue/'`）、`vue-app/index.html`（挂载点 `<div id="app">`）、`vue-app/src/main.js`
- 根 `package.json`：dependencies 增加 `vue@^3`、`vue-router@^4`、`pinia@^2`、`element-plus@^2`、`@element-plus/icons-vue@^2`；devDependencies 增加 `@vitejs/plugin-vue@^5`；scripts 增加 `"build:vue": "vite build --config vue-app/vite.config.js"`
- `vue-app/src/App.vue`：左侧导航壳（熊猫 logo 区块 + 菜单项：对话/任务/目标/下载/设置/调试，暂为占位 router-view）+ 主内容区
- `vue-app/src/router/index.js`：history 模式，base `/app`，路由 `/chat`、`/tasks`、`/goals`、`/downloads`、`/settings`、`/debug` 各指向占位组件（`vue-app/src/views/PlaceholderView.vue`，显示视图名）
- `api/server.py`：新增 `/app` 与 `/app/{path:path}` 路由，返回 `static/vue/index.html`（SPA fallback）；并将 `static/vue` 挂载为静态目录。注意不能影响现有 `/` 与旧 SPA fallback 的行为
- `.gitignore`：确认 `static/vue/` 被忽略（产物不入库）
- 验证：`npm install`、`npm run build:vue` 成功；`python -c "import api.server"` 正常；pytest 不回归

## Task 2：地基（API client / WS store / 主题 / Markdown 组件）

- `vue-app/src/api/client.js`：fetch 封装（JSON、错误规范化）；带 TTL 缓存 + in-flight 请求去重（参考旧 `static/js/cache.js` 的设计意图但修正其接口错位：TTL 按 URL 前缀匹配表 `{'/api/tasks':5000,'/api/settings':60000,'/api/plugins':30000,'/api/downloads':10000}`，调用签名 `cachedFetch(url, ttl?)`）
- `vue-app/src/stores/ws.js`（Pinia）：WebSocket 连接管理（`/ws?session_id=`）、指数退避重连（1s→30s 上限，参考旧 app.js）、事件按 `type` 分发给订阅者（`on(type, fn)`）；事件 type 清单以 `dev-docs/API契约.md` 的 WebSocket 目录为准
- `vue-app/src/components/MarkdownView.vue`：`marked` 解析 + `dompurify` 消毒（两个包均已在 dependencies），props 为 `content: String`
- `vue-app/src/theme/element-panda.css`：读 `static/css/variables.css` 的熊猫色板，映射为 Element Plus CSS 变量（`--el-color-primary` 等），在 main.js 中于 element-plus 样式之后引入
- `vue-app/src/i18n/zh.js`：中文文案常量文件（先集中新 SPA 用到的字符串，不引入 vue-i18n 依赖）
- `api/client.js` 与 `stores/ws.js` 配最小 vitest 或 node 冒烟脚本（放 `vue-app/scripts/smoke.mjs`，验证 TTL 去重逻辑纯函数部分）
- 验证：`npm run build:vue` 成功；冒烟脚本通过

## Task 3：批次 1a — 设置·模型视图

- `vue-app/src/views/settings/ModelsView.vue`：迁移旧 `view-settings-models` 功能——API keys 网格（13 个 provider 含 kimi_code，数据来自 GET /api/settings 的 api_keys_masked，占位显示、留空不改）、默认厂商/模型选择（/api/provider-models）、回退模型、sandbox/代理/心跳/上下文预算等基础字段
- 保存走增量语义：仅提交用户实际修改的字段（对照明细见 API契约.md）；保存后刷新显示
- API key 输入框 `autocomplete="new-password"`（防浏览器自动填充事故，旧前端已修过同类问题）
- 验证：构建成功；与旧页面对照字段无遗漏（列出对照表）

## Task 4：批次 1b — 设置·技能/MCP/插件 + 调试视图

- `SkillsView.vue`：技能列表/启停/导入/删除（/api/skills 系列，注意删除走 DELETE 且 filename 需合法）
- `McpView.vue`：MCP 配置编辑器（JSON 校验，保存只提交 `{ mcp_servers, session_id }`，与旧 saveMcpConfig 行为一致）+ Agents 列表（数据来自 /api/agents 与 /api/models/available——旧前端读错端点导致永远空白，契约文档已记录正确端点）
- `PluginsView.vue`：插件列表/启停/扫描/git 安装（安装输入校验）
- `DebugView.vue`：日志查看（/api/logs 系列，tail 参数）
- 验证：构建成功；对照旧视图功能清单

## Task 5：批次 2 — 任务/目标/下载视图

- `TasksView.vue` + `TaskDetailView.vue`：任务列表（/api/tasks 分页 clamp）、状态筛选、任务详情（步骤回放 /api/tasks/{id}/steps、进程日志 tail、中断/删除/完成按钮）、计划任务创建弹窗（cron）
- `GoalsView.vue`：目标 CRUD（/api/goals）
- `DownloadsView.vue`：下载记录（/api/downloads，含进度轮询或 WS download 事件）
- 修复旧前端已知问题：步骤分页闭包错位（Vue 响应式天然规避）、日志 interval 泄漏（组件 unmount 时清理）
- 验证：构建成功；对照功能清单

## Task 6：批次 3 — 聊天视图（最重）

- `ChatView.vue`：会话列表侧栏（/api/sessions）、消息流（历史分页加载 /api/sessions/{id}/history）、发送/追加消息（WS user_message）、agent 进度事件渲染（tool_start/tool_done/thinking 等 17 种事件按契约文档）、插话（interjection）UI、沙箱授权弹窗（四按钮流程，契约文档有事件定义）、停止按钮、MarkdownView 渲染 agent 消息
- WS 由 stores/ws.js 驱动；会话切换走 router（/app/chat/:sessionId?）
- 验证：构建成功；与旧聊天页人工对照消息流与进度卡片

## Task 7：批次 4 — 插件 UI 契约 + 切换与清理

- 设计并实现插件注册 Vue 视图的机制：/api/plugins 返回的 manifest 含 `menu`/`views`；主 SPA 按 manifest 动态注册路由并懒加载插件前端包（插件包约定输出到 `plugins/<name>/static/vue/`）
- 训练插件 plugin.js 的 4 个视图按此契约迁移（保持插件形态，重依赖不进主体）
- 修复阶段 1 遗留：定时任务 next_run_at 本地时间 vs 调度器 UTC 比较不一致（统一 UTC 存储与比较）
- 切换：`/` 重定向到 `/app`（或 server 根路径直接服务新 SPA），旧 `static/js/*.js`、`static/app.js`、旧 `static/index.html` 视图区删除，旧 IIFE 构建从 vite.config.mjs 移除
- 验证：构建成功；pytest 不回归；手动全流程走查（聊天/设置/任务/训练插件）
