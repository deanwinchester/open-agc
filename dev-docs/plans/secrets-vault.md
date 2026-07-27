# 会话内凭证闭环 — 实施计划

> 已确认设计 v2：凭证库 + 引用注入 + 输出脱敏 + 两条会话内入口（agent 弹窗收集 / 用户粘贴检测确认）。
> 全局约束：不执行任何 git 操作（控制者统一提交）；UTF-8；每任务 `python -m pytest tests/ -q` 不回归（当前基线 691）；`data/secrets.json` 必须进 .gitignore；**任何路径不得把密码明文交给 LLM/前端回显/日志**。

## Task 1：凭证库后端 + 执行替换 + 输出脱敏

1. **`core/secrets.py`**（新建）：
   - 存储：`data/secrets.json`（不存在则空 dict；模块级 RLock；原子写 tmp+replace；UTF-8）
   - CRUD：`list_secrets()`（**不含 password 字段**，只有 name/type/host/username_masked/note/created_at）、`get_secret(name)`（含明文，仅服务端内部用）、`upsert_secret(name, ...)`、`delete_secret(name)`
   - 拼串：`build_uri(name)` → `mongodb://user:pass@host:port/`（按 type）
   - 替换：`substitute_refs(text) -> text`：把 `{{secret:name.field}}`（field ∈ username/password/host/uri/note）替换为真实值；未知名称/字段原样保留
   - 检测引用：`has_secret_ref(text) -> bool`
   - 脱敏：`mask_secrets(text) -> text`：把库内所有 password 值和 build_uri 值替换为 `***`（空值跳过）
2. **API（`api/routes/routes_secrets.py`，新建并注册）**：
   - `GET /api/secrets`（掩码视图）、`POST /api/secrets`（upsert，name 校验 `^[A-Za-z0-9_-]+$`）、`DELETE /api/secrets/{name}`
   - `GET /api/secrets/for-llm`（给 agent 的 list_secrets 视图）
   - **任何端点不得返回 password**
3. **执行替换**：`tools/shell.py` 与 `tools/python_repl.py` 在命令/代码执行**前**调 `substitute_refs`（imports 用局部导入避免循环）。
4. **输出脱敏**：`tools/shell.py`、`tools/python_repl.py` 的结果返回**前**调 `mask_secrets`；`agent/agent.py` 写 tool result 入 messages 前统一再过一次 `mask_secrets`（单点兜底，覆盖所有工具）。
5. **`.gitignore`** 加 `data/secrets.json`。

验证：存储/掩码视图不含密码；替换各字段与 uri；脱敏（输出含密码/连接串变 ***）；shell 执行占位符命令时上下文只见占位符；pytest 不回归。

## Task 2：入口 A——agent 弹窗收集自动入库

1. **`tools/request_secret.py`**（新建 `RequestSecretTool`）：参数 `purpose`（用途说明，必填）、`name`（建议名称，可选）、`secret_type`（mongodb/mysql/api_key/generic，默认 generic）、`host`（可选）。执行时 **raise SandboxBlocked(category='secret', description=purpose, path=name)**（复用授权链路；先读 agent.py 的 category 处理，扩展 'secret' 类别）。
2. **弹窗（前端）**：SandboxModal 增加 `secret` 类型表单——名称/类型/host/用户名/密码/备注（密码必填，名称缺省自动生成 `secret_<timestamp>`）。提交走现有 `sandbox_response` 通道（带 request_id），payload 含表单字段。
3. **后端接通**：ws.py 的 sandbox_response 处理中，`category='secret'` 时调 `core.secrets.upsert_secret` 入库（复用 stage 6 的共享会话存储思路做名称冲突处理：同名覆盖前确认）；agent 侧授权结果返回文本 `"已保存为 {{secret:<name>}}（类型/宿主），请用引用名 {{secret:<name>.password}} 或 {{secret:<name>.uri}} 继续，不要在上下文中包含明文。"`——**返回值不含明文**。
4. **系统提示**：加规则——需要凭据时先查 `list_secrets`（走 `/api/secrets/for-llm` 或由工具提供），无则调用 `request_secret`；**禁止直接向用户索要明文凭据**；对话中出现明文凭据应提醒用户改用凭证库。
5. **agent 侧工具清单**：`list_secrets` 以只读工具或 prompt 注入方式提供（选实现成本低的：系统提示注入 for-llm 视图）。

验证：弹窗表单提交后库内有记录（无明文回给 agent）；request_secret 返回文本不含密码；占位符可被执行替换；pytest 不回归。

## Task 3：入口 B——粘贴检测 + 倒计时确认窗 + 设置页管理

1. **检测（前端，`vue-app/src/components/chat/ChatInput.vue`）**：发送前对文本做模式检测——`mongodb://`、`mysql://`、`postgres://`、`password\s*[=:]\s*\S+`、`sk-[A-Za-z0-9]{16,}`、"密码[:：是]\s*\S+"（先保守，避免误报）。命中弹出确认窗。
2. **确认弹窗（新组件 `SecretConfirmModal.vue`）**：显示命中的凭据片段（打码显示，如 `mongoadm/Neu***ft`）、名称输入框（自动建议）、类型选择、10 秒倒计时进度条；按钮：立即保存（默认）/ 丢弃并打码 / 保留明文（警示样式）。倒计时到点自动执行"立即保存"。
3. **保存动作**：调 `POST /api/secrets` 入库；然后把输入框文本中的凭据片段替换为引用占位符（password → `{{secret:<name>.password}}`，完整 uri → `{{secret:<name>.uri}}`）后自动发送；UI 气泡显示打码版（`mongoadm/********`），LLM 上下文为占位符。"丢弃并打码"：凭据片段替换为 `***` 发送。"保留明文"：按原文发送（警示确认）。
4. **设置页凭证管理区块**（`ModelsView` 或独立子页）：列表（掩码）/删除/新增（表单）/编辑说明；与 Task 1 的 API 对接。

验证：粘贴 `mongodb://user:pass@host:50000` 触发弹窗；倒计时自动入库；发送内容无明文（占位符/打码）；库内可查（掩码）；pytest/构建不回归。

## 顺序与验收

Task 1 → Task 2 → Task 3，各自独立评审；最后整体验收：
- 让 agent 用凭证库连 mongo：模型调用日志全文无明文密码，任务完成
- 粘贴凭据到输入框：弹窗出现，倒计时自动入库，发送内容无明文
- 弹窗收集：agent 收到的是引用名，库内可查
