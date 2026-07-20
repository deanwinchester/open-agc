# Open-AGC API 契约

> 本文档是 Vue 前端迁移的基准契约，**以代码实际行为为准**（阶段 1 盘点整理）。
> 服务端：FastAPI + SQLite，默认监听 `http://localhost:8000`（`launcher.py`）。
> 所有 API 响应均为 JSON；业务错误通过 `HTTPException` 返回 `{"detail": "..."}`。

## 通用约定（阶段 1 确立）

- **分页**：统一 `page >= 1`、`1 <= page_size <= 200`；越界时 `page` 回落 1、`page_size` 回落默认 50（clamp，不报错）。适用端点：`GET /api/tasks`、`GET /api/tasks/{id}/steps`、`GET /api/model-logs`。SQLite `LIMIT -1` 等于不限制，禁止未校验透传。
- **`POST /api/settings` 为增量语义**：只更新请求体中非 `None` 的字段，未提供的字段保持不变（见 `ConfigUpdate`，api/routes/routes_settings.py:286）。
- **掩码不可回传**：`GET /api/settings` 返回的 `api_keys_masked` 格式为 `{前3字符}...{后3字符}`（长度 ≤6 的 key 为 `***`），`email_password` 为 `***`。POST 时这类掩码值会被拒绝写入（含 `...` 或以 `***` 结尾的值直接忽略），前端回显掩码后原样提交不会破坏真实密钥。
- **时间格式**：DB 中 `created_at/updated_at/next_run_at` 等为 `'%Y-%m-%d %H:%M:%S'` 字符串；调度器按 UTC 比较 `next_run_at`（api/background.py:352）。
- **会话删除级联**：`DELETE /api/sessions/{id}` 在事务内级联删除 `messages/tasks/task_steps/token_usage/model_call_logs` 中该会话的行（SQLite 未开外键，应用层保证），并中断该会话的活动 agent；`POST /api/sessions/{id}/clear` 清理 `messages` + `task_steps`。

---

## 1. REST 端点总表

### 1.1 主服务直接路由（api/server.py）

| 方法 | 路径 | 用途 | 备注 |
|---|---|---|---|
| GET | `/` | 返回 `static/index.html` | SPA 入口 |
| GET | `/api/files/{file_path:path}` | 从 sandbox 目录向 UI 提供文件 | 越界路径 403 |
| GET | `/{full_path:path}` | SPA fallback，返回 index.html | 兜底路由 |
| WS | `/ws?session_id=N` | WebSocket 主通道 | 见第 3 节 |
| — | `/static/*` | 静态资源挂载 | `app.mount` |

### 1.2 会话 / Agent 配置 / 模型（api/routes/routes_sessions.py）

| 方法 | 路径 | 用途 | 请求体关键字段 | 响应关键字段 |
|---|---|---|---|---|
| GET | `/api/sessions` | 列出全部会话（按 updated_at 倒序） | — | `sessions[]`（含 `message_count`，`email_password` 掩码为 `***`） |
| POST | `/api/sessions` | 创建会话 | `name?` + `email_enabled/email_account/email_password/email_imap_server/email_smtp_server/owner_email?` | `session{id,name}` |
| DELETE | `/api/sessions/{session_id}` | 删除会话并级联清理 | — | `ok`；`id=1` → 403 |
| POST | `/api/sessions/{session_id}/clear` | 清空会话数据（保留会话行） | — | `ok` |
| PUT | `/api/sessions/{session_id}` | 更新会话名/邮箱配置 | `name?` + email 字段（增量，仅更新出现的字段） | `ok` |
| GET | `/api/agents` | 列出 agent 配置（config.json 的 `agent_profiles`） | — | `agents[]` |
| POST | `/api/agents` | 新建 agent 配置 | `name`（唯一，必填）, `prompt`, `model`, `temperature`, `max_tokens` | `agents[]`；400 重名/缺名 |
| PUT | `/api/agents/{agent_name}` | 更新 agent 配置 | 除 `name` 外任意字段 | `agents[]`；404 |
| DELETE | `/api/agents/{agent_name}` | 删除 agent 配置 | — | `agents[]` |
| GET | `/api/models/available` | 可用模型（config + 本地 llamacpp） | — | `models[]` |

### 1.3 任务 / 调度 / 进程（api/routes/routes_tasks.py）

| 方法 | 路径 | 用途 | 请求体/参数关键字段 | 响应关键字段 |
|---|---|---|---|---|
| GET | `/api/tasks` | 任务列表（分页 clamp） | query: `status?`（含 `scheduled`）, `q?`, `session_id?`, `page=1`, `page_size=50` | `tasks[]`（含 `step_count/session_name/total_tokens/total_cost` 等）, `total_count`, `page`, `page_size` |
| GET | `/api/tasks/{task_id}` | 任务详情（含全部 steps） | — | `task{...,steps[],output_files[]}`；404 |
| GET | `/api/tasks/{task_id}/steps` | 任务步骤分页（clamp） | `page=1`, `page_size=50` | `steps[]`, `total`, `page`, `page_size`, `total_pages` |
| POST | `/api/tasks/{task_id}/interrupt` | 中断任务（只中断匹配 task_id 的 agent） | — | `status`, `message` |
| DELETE | `/api/tasks/{task_id}` | 删除任务 + 步骤 + 临时文件，清理 goal 关联 | — | `status`, `message` |
| POST | `/api/tasks/{task_id}/reset-resume` | resume_count 归零 | — | `status` |
| POST | `/api/tasks/{task_id}/complete` | 手动标记完成并停止 agent | — | `status`, `message` |
| POST | `/api/tasks/schedule` | 创建定时任务 | `title`, `query`, `cron`, `session_id=1` | `status`, `task_id`；400 cron 非法 |
| POST | `/api/tasks/{task_id}/toggle-schedule` | 启用/停用定时任务（启用时重算 next_run_at） | — | `status`, `enabled`；404；500 重算失败（不改状态） |
| PUT | `/api/tasks/{task_id}/schedule` | 更新定时任务配置（同步重算 next_run_at） | `title`, `query`, `cron`, `session_id` | `status`；400 cron 非法 |
| GET | `/api/processes` | 后台 shell 进程列表（含孤儿进程） | — | `processes{tid:{pid,command,alive,uptime,...}}` |
| GET | `/api/tasks/{task_id}/process` | 任务进程信息（自动认领孤儿进程） | — | `process{pid,command,alive,uptime,output_file} \| null` |
| GET | `/api/tasks/{task_id}/logs` | 任务进程输出尾部 | `lines=50` | `logs`, `lines[]` |
| POST | `/api/tasks/{task_id}/kill` | 杀掉任务后台进程 | — | `status`, `message` |
| POST | `/api/tasks/{task_id}/reset-resume-count` | resume_count 归零（guardian 重试用） | — | `status` |
| POST | `/api/tasks/{task_id}/reply` | 回复后台任务的 ask_user 提问 | `answer`（必填） | `status`, `message`；400/404/500 |
| GET | `/api/agent/effectiveness` | Agent 效果聚合指标（纯 SELECT 聚合） | — | `status_counts{}`, `tasks_total`, `tasks_last_7d`, `tasks_last_30d`, `avg_steps_per_task`, `tool_calls_total`, `tool_success_rate`, `top_tools[]` |

### 1.4 大目标（api/routes/routes_goals.py）

| 方法 | 路径 | 用途 | 请求体关键字段 | 响应关键字段 |
|---|---|---|---|---|
| GET | `/api/goals` | 列出全部目标 | — | `items[]{id,desc,status,updated,task_ids,resume_count}` |
| POST | `/api/goals` | 创建目标 | `desc`（≤100 字） | `status`, `goal`；400 超限/空描述 |
| PUT | `/api/goals/{goal_id}` | 更新目标描述/状态 | `desc?`, `status?`（pending/doing/done/stuck/archived） | `status`, `goal`；400/404 |
| DELETE | `/api/goals/{goal_id}` | 删除目标 | — | `status`, `message`；404 |

### 1.5 记忆 / 历史（api/routes/routes_memories.py）

| 方法 | 路径 | 用途 | 参数 | 响应关键字段 |
|---|---|---|---|---|
| GET | `/api/memories` | 搜索或列出记忆 | `category?`, `query?` | `memories[]`, `type`（`search`/`all`） |
| GET | `/api/memories/categories` | 记忆分类汇总 | — | `categories` |
| GET | `/api/history` | 聊天历史（向上翻页） | `session_id?`, `before_id=0`, `limit=100` | `history[]{id,role,content}`（正序）, `oldest_id`, `has_more`（基于 `SELECT EXISTS` 判断是否存在更早消息） |

### 1.6 技能（api/routes/routes_skills.py）

| 方法 | 路径 | 用途 | 请求体关键字段 | 响应关键字段 |
|---|---|---|---|---|
| GET | `/api/skills` | 列出技能（含 enabled 标记） | — | `skills[]` |
| GET | `/api/skills/stats` | 技能使用统计（读 skills/index.json，不存在返回空数组） | — | `skills[]{filename,title,usage_count,success_rate,last_used}`（按使用次数降序） |
| POST | `/api/skills/import` | 导入技能（安全校验） | `filename`, `content`, `force?` | 导入结果；400 |
| POST | `/api/skills/validate` | 仅校验不导入 | `content` | 校验结果 |
| GET | `/api/skills/{filename}` | 读取技能内容 | — | `filename`, `content`；400/404 |
| DELETE | `/api/skills/{filename}` | 删除技能并重建索引 | — | `success`, `message`；400/404 |

### 1.7 文件上传（api/routes/uploads.py）

| 方法 | 路径 | 用途 | 请求 | 响应关键字段 |
|---|---|---|---|---|
| POST | `/api/upload` | 上传文件到 `sandbox/uploads/`（MD5 去重，≤500MB） | multipart `file` | `status`, `filename`, `size`, `path`；400/403/413/500 |
| GET | `/api/uploads` | 列出已上传文件 | — | `files[]{name,size,modified,modified_iso}` |
| GET | `/api/upload/{filename:path}` | 下载已上传文件 | — | FileResponse；403/404 |
| DELETE | `/api/upload/{filename:path}` | 删除已上传文件 | — | `status`, `filename`；403/404 |

### 1.8 设置 / Provider / llama.cpp / 下载（api/routes/routes_settings.py）

| 方法 | 路径 | 用途 | 请求体/参数关键字段 | 响应关键字段 |
|---|---|---|---|---|
| GET | `/api/settings` | 读取配置（key 已掩码） | `session_id?`（带会话邮箱配置） | `api_keys_masked`, `default_model`, `fallback_models`, `sandbox_mode`, `sandbox_dir`, `tool_permissions`, `mcp_servers`…（全量见代码） |
| POST | `/api/settings` | **增量**更新配置并写 .env | `ConfigUpdate` 全可选：`api_keys`, `default_model`, `fallback_models`, `sandbox_mode`, `sandbox_dir`, `tool_permissions`, `mcp_servers`, `session_id`（会话级邮箱）等 | `status`, `message`；500 |
| GET | `/api/provider-models` | 查询 provider 可用模型（无 key 回落默认列表） | `provider` | `models[]` |
| GET | `/api/stats/token_usage` | provider 历史 token 用量 | `provider`, `days=30` | `status`, `data` |
| GET | `/api/llamacpp/status` | llama-server 状态 | — | `installed`, `running`, `models[]`, `port`, `download` |
| POST | `/api/llamacpp/setup` | 后台下载安装 llama-server 二进制 | — | 安装状态 |
| POST | `/api/llamacpp/download-model` | 按 URL 下载 GGUF | `url`, `filename` | 下载状态 |
| POST | `/api/llamacpp/search-models` | 搜索 HF/ModelScope 模型 | `query`, `source="huggingface"` | 模型列表 |
| POST | `/api/llamacpp/model-files` | 列出仓库内 GGUF 文件 | `repo_id`, `source="huggingface"` | 文件列表 |
| POST | `/api/llamacpp/download-from-hf` | 从 HF 下载指定文件 | `repo_id`, `filename`, `source="huggingface"` | 下载状态 |
| POST | `/api/llamacpp/control` | 启停 llama-server | `action`（start/stop/restart）, `model?`（start 必填） | `status`, `message`；400/500 |
| GET | `/api/downloads` | 下载任务列表 | `status?` | 下载记录数组 |
| GET | `/api/downloads/{download_id}/events` | 下载事件流水 | — | 事件数组 |
| POST | `/api/downloads/{download_id}/resume` | 断点续传 | — | 续传状态 |
| DELETE | `/api/downloads/{download_id}` | 删除下载任务 | — | 删除状态 |
| POST | `/api/agent-design` | 用 agent 按自然语言需求生成模型设计参数 | `agent_name?="default"`, `requirements`（必填） | 设计参数；400 |

### 1.9 SearXNG / 版本 / 日志 / 工具统计（api/routes/routes_searxng.py）

| 方法 | 路径 | 用途 | 请求体/参数关键字段 | 响应关键字段 |
|---|---|---|---|---|
| GET | `/api/searxng/status` | SearXNG 状态 | — | manager.get_status() |
| POST | `/api/searxng/install` | 安装 SearXNG | — | `status`, `message`；500 |
| POST | `/api/searxng/control` | 启停 SearXNG | `action`（start/stop） | `status`；400 |
| GET | `/api/version` | 当前/最新版本 | — | `current`, `latest`, `update_available` |
| POST | `/api/upgrade` | 执行自动升级 | — | `status`, `message`；500 |
| GET | `/api/logs` | 服务端日志尾部 | `lines=200` | `lines[]`, `total` |
| GET | `/api/model-logs/status` | 模型调用日志开关状态 | — | `enabled` |
| POST | `/api/model-logs/toggle` | 开关模型调用日志 | `enabled?` | `enabled` |
| POST | `/api/model-logs/clear` | 清空调用日志 | — | `status` |
| GET | `/api/model-logs/filters` | provider/model 去重列表 | — | `providers[]`, `models[]` |
| GET | `/api/model-logs` | 调用日志分页（clamp） | `page=1`, `page_size=50`, `provider?`, `model?`, `session_id?` | `logs[]`, `total`, `page`, `page_size` |
| GET | `/api/model-logs/{log_id}` | 调用日志详情（含文件中的完整请求/响应） | — | 日志行 + `request_data/response_data`；404 |
| GET | `/api/tools/stats` | 工具调用频率统计 | — | `tools[]`, `summary{total_calls,total_tools}` |
| GET | `/api/tools/auto-tools` | 自动生成的工具列表 | — | `tools[]{name,session,calls,...}` |

### 1.10 沙箱 / 插件 / 市场（api/routes/routes_plugins.py）

| 方法 | 路径 | 用途 | 请求体关键字段 | 响应关键字段 |
|---|---|---|---|---|
| POST | `/api/sandbox/approve` | 批准沙箱路径/权限等待 | `session_id?`, `action?`（默认 deny_once）, `path?`, `password?` | `status` |
| POST | `/api/sandbox/remove-path` | 移除会话级沙箱白名单路径 | `path`, `session_id?` | `status` |
| POST | `/api/sandbox/remove-permission` | 移除会话级工具授权 | `tool`, `session_id?` | `status` |
| GET | `/api/plugins` | 列出内置+用户插件 | — | `plugins[]`, `plugins_dir` |
| POST | `/api/plugins/scan` | 重新扫描并挂载插件 | — | `status`, `count`, `plugins` |
| POST | `/api/plugins/{name}/toggle` | 启用/停用插件 | — | `status`, `enabled` |
| POST | `/api/plugins/install` | 从 git URL 安装插件 | `name`, `url` | `status`, `message`；400/500 |
| DELETE | `/api/plugins/{name}` | 卸载并删除插件目录 | — | `status`, `message`；400/404 |
| GET | `/api/marketplace` | 获取插件市场数据 | — | `marketplace` |

### 1.11 Benchmark（api/routes/benchmark.py，router 前缀 `/api/training`）

| 方法 | 路径 | 用途 | 请求体/参数关键字段 |
|---|---|---|---|
| POST | `/api/training/benchmark/pre-download` | 预下载 benchmark 数据集 | `benchmark_type`, `sample_size?` |
| GET | `/api/training/benchmark/cache-status` | 数据集缓存状态 | — |
| GET | `/api/training/benchmark/preview/{benchmark_type}` | 预览缓存题目 | `n=10` |
| GET | `/api/training/benchmark/checkpoint-status` | 可恢复的 checkpoint 列表 | — |
| GET | `/api/training/benchmarks` | benchmark 结果列表 | — |
| GET | `/api/training/benchmarks/{bench_id}` | 结果详情 | — |
| DELETE | `/api/training/benchmarks/{bench_id}` | 删除结果 | — |
| POST | `/api/training/benchmark/cancel` | 中止运行中的 benchmark | — |
| POST | `/api/training/benchmark` | 后台线程运行 benchmark | `model_id`, `model_source="online"`, `benchmark_types=["mmlu","latency"]`, `resume=false` |
| GET | `/api/training/all-models` | 全部可用模型（含 litellm 前缀） | — |

### 1.12 数据集下载 / 训练依赖（api/routes/downloads.py）

| 方法 | 路径 | 用途 | 请求体/参数关键字段 |
|---|---|---|---|
| GET | `/api/training/recommended-datasets` | 推荐数据集清单 | — |
| GET | `/api/downloads/dataset-files/{repo_id:path}` | 列出 HF 数据集仓库文件 | `split?`, `config?` |
| GET | `/api/downloads/dataset-configs/{repo_id:path}` | 列出数据集 config/subset | — |
| POST | `/api/downloads/dataset` | 下载 HF 数据集（带进度） | `repo_id`, `name=""`, `split="train"`, `config?`, `target_file?` |
| POST | `/api/training/install-deps` | pip 安装训练依赖（实时进度） | — |
| GET | `/api/training/status` | 训练依赖可用性 | — |

---

## 2. 训练插件端点（plugins/open-agc-train）

插件路由统一挂载在前缀 **`/api/plugin/open-agc-train`** 下（`plugins/open-agc-train/__init__.py:90`，经 `api/server.py:162` 挂载），数据存于独立 `data/training.db`。静态页面挂载于 `/static/plugins/open-agc-train`。

| 方法 | 路径（省略前缀） | 用途 | 请求体关键字段 |
|---|---|---|---|
| GET | `/status` | 训练引擎可用性（torch/transformers/peft） | — |
| POST | `/runs` | 创建训练任务 | `name`, `model_config_id?`, `dataset_id?`, `base_model_id?`, `base_model_source="huggingface"`, `training_params_json="{}"`, `val_ratio=0.1` |
| GET | `/runs` | 训练任务列表（最近 50） | — |
| GET | `/runs/{run_id}` | 训练任务详情 | — |
| DELETE | `/runs/{run_id}` | 删除训练任务 | — |
| POST | `/runs/{run_id}/{action}` | 控制运行（pause/resume/abort 等，按 action_map） | 路径参数 `action` |
| POST | `/runs/{run_id}/layer-stats-toggle` | 开关逐层统计 | — |
| POST | `/runs/{run_id}/test-chat` | 用训练产物试聊 | `prompt`, `max_length=200`, `temperature=0.7` |
| POST | `/runs/{run_id}/eval-ppl` | 启动困惑度评估（后台 job） | `dataset_path=""`, `max_samples=500`, `stride=512`, `max_length=1024`, `dataset_id?` |
| GET | `/runs/{run_id}/eval-ppl` | 查询 PPL 评估结果 | — |
| POST | `/eval-metrics` | 生成质量指标评估 | `model_path`（支持 `run_<id>`）, `dataset_path`, `dataset_id?`, `max_samples=100` |
| GET | `/model-configs` | 模型配置列表 | — |
| POST | `/model-configs` | 新建模型配置 | `name`, `architecture`, `config_json`, `param_count_estimate=0` |
| GET | `/model-configs/{config_id}` | 配置详情 | — |
| DELETE | `/model-configs/{config_id}` | 删除配置 | — |
| POST | `/model-configs/estimate` | 估算参数量/显存 | `architecture`, `config_json` |
| GET | `/datasets` | 数据集列表 | — |
| POST | `/datasets/scan-import` | 扫描 datasets 目录自动导入 | — |
| GET | `/datasets/{ds_id}` | 数据集详情 | — |
| GET | `/datasets/{ds_id}/preview` | 预览样本 | `n=5` |
| DELETE | `/datasets/{ds_id}` | 删除数据集 | — |
| POST | `/datasets/upload` | 上传数据集文件 | multipart `file`, form `name?` |
| PUT | `/datasets/{ds_id}` | 更新数据集 | `name`, `samples`（JSONL 内容）, `format="jsonl"` |
| POST | `/datasets/create` | 从编辑器内容创建数据集 | 同上 `CreateDatasetRequest` |
| GET | `/recommended-datasets` | 推荐数据集 | — |
| GET | `/base-models` | 可微调的基座模型（GGUF + 已训练 + HF 预设） | — |

> 注：`plugins/open-agc-train/routes_benchmark.py`、`routes_datasets.py` 是主应用 `api/routes/benchmark.py`、`downloads.py` 的副本，**未单独挂载**；对应端点以主应用的 `/api/training/*`、`/api/downloads/*` 为准（见 1.11/1.12）。

---

## 3. WebSocket 协议（`/ws`）

- 连接：`ws://<host>/ws?session_id=N`（默认 1）。断线由前端指数退避重连（1s→30s 封顶）。
- 连接建立时服务端主动推送：进行中的 `llamacpp_download` 状态、未送达的最终回复（`message`）、该会话最近任务的 `history_steps`（api/ws.py:37-68）。
- 服务端广播统一走 `api/state.py:_broadcast_to_websockets`（发给所有连接，线程安全、尽力投递）；前端按 `session_id`/`task_session_id` 过滤是否属于当前会话（static/app.js:295）。

### 3.1 客户端 → 服务端消息

| type | 关键字段 | 服务端行为 |
|---|---|---|
| `query`（缺省） | `query`, `agent_name?`, `images?` | 启动一轮 agent 执行；该会话已有活动 agent 时改为 `queue_message` 排队（ws.py:959-966） |
| `switch_session` | `session_id` | 不重连切换会话，重载历史并推送该会话最近任务的 `history_steps` |
| `sandbox_response` | `session_id?`, `action`, `path?`, `password?` | 解除沙箱授权等待；迟到的授权会记录并恢复被中断任务（ws.py:837-886） |
| `resume` | `task_id`, `extra_instruction?` | 恢复中断任务：先推 `history_steps`（task_status=resuming），再带上下文续跑 |
| `retry` | `query?`, `model?`, `agent_name?`, `images?` | 用指定模型/agent 重试上一轮 |
| `interrupt` | — | 中断当前运行中的 agent 与 shell（ws.py:462-472）。前端同时也会调 REST `POST /api/tasks/{id}/interrupt` |

### 3.2 服务端 → 客户端事件

| type | 发送方 | 关键字段 | 前端消费（static/app.js） |
|---|---|---|---|
| `status` | api/ws.py | `message`, `session_id` | 显示"Agent 思考中"状态（`showThinkingStatus`） |
| `progress` | api/ws.py、api/background.py（agent 内 `progress_callback`） | `event`（子类型见下）, `step`, `tool`, `tool_label`, `args_preview`, `result_preview`, `success`, `task_id`, `session_id`, `background?` | `handleProgressEvent` 实时渲染步骤卡片；跨会话仅更新任务角标（`updateTaskBadge`） |
| └ `event=thinking` / `usage` / `model_switched` / `response` | agent/agent.py | `iteration`, token 用量等 | 思考状态/用量展示 |
| └ `event=tool_start` / `tool_done` | agent/agent.py、agent/sub_agent.py | `step`, `tool`, `tool_label`, `args_preview` / `result_preview`, `success` | 步骤卡片开始/完成渲染 |
| └ `event=ask_user` | agent/agent.py:2395 | `question`, `task_id`, `background` | 后台提问：渲染回复输入框，提交走 `POST /api/tasks/{id}/reply`；自动切到聊天视图 |
| └ `event=sandbox_blocked` / `sandbox_approved` | agent/agent.py | `block_type`（path/network/permission）, `path`, `category?`, `description?` | `showSandboxBlockedModal` 授权弹窗，结果经 WS `sandbox_response` 或 REST `/api/sandbox/approve` 回传 |
| └ `event=task_backgrounded` | agent/agent.py | `task_id` | 步骤卡片标记转入后台 |
| `message` | api/ws.py、api/background.py | `role`（agent/system/user；`tool_step` 前端跳过）, `content`, `session_id`, `task_id?`, `background?` | `appendMessage` 渲染最终回复；语音播报（如开启） |
| `error` | api/ws.py、api/background.py | `content`, `original_query?`, `session_id` | 前台：重试条 `showRetryBar`；后台：系统消息 |
| `history_steps` | api/state.py（`_broadcast_task_history`）、api/ws.py | `task_id`, `session_id`, `steps[]`, `task_status` | `renderHistorySteps` 渲染历史步骤汇总卡 |
| `task_backgrounded` | api/ws.py、api/background.py | `task_id`, `message`, `session_id` | 系统消息提示任务转入后台 |
| `system_message` | api/ws.py | `message`, `session_id` | `appendMessage(..., 'system')` |
| `llamacpp_download` | api/ws.py、api/routes/routes_settings.py、api/routes/downloads.py | `task`（binary/model/dataset）, `label`, `download_id?`, `progress`, 阶段/状态字段, `error?` | `handleLlamaDownloadProgress` 下载进度 UI |
| `download_success` | api/routes/routes_settings.py | `label` | 系统消息 + 成功 toast |
| `download_failed` | api/ws.py、api/routes/routes_settings.py | `label`, `error` | 系统消息 + 错误 toast |
| `benchmark_progress` / `benchmark_complete` | api/routes/benchmark.py | `task`（benchmark 类型）, `stage`, `benchmark_id` 等 | 插件监听器（`window._pluginWsListeners`，app.js:242 先行分发） |
| `training_install_progress` | api/routes/downloads.py | `stage`（installing/error/complete）, `message`/`error` | 插件监听器 |
| `training_progress` / `training_complete` / `training_error` / `training_step_paused` / `eval_progress` | plugins/open-agc-train（engine.py、routes.py） | `run_id`, `stage`, loss/进度字段, `best_loss?` 等 | 训练插件前端 JS 监听器 |

---

## 4. 阶段 1 修复的契约决定

1. **中断/删除不误伤前台聊天**（routes_tasks.py）：`POST /api/tasks/{id}/interrupt` 与 `DELETE /api/tasks/{id}` 原先同时中断 `_active_agents` 中 key 为 `0` 的 agent（即该会话的前台聊天 agent，ws.py:407 以 `[ws_task_id or 0]` 注册），导致中断任意任务都会误杀正在进行的对话。已改为只匹配 `_aid == task_id`（与 `complete_task` 一致）。
2. **分页统一 clamp**（routes_tasks.py `get_task_steps`、routes_searxng.py `get_model_logs`）：`page >= 1`、`1 <= page_size <= 200`，越界回落默认值（与 `get_tasks` 一致），杜绝 `LIMIT -1` 全表导出。
3. **更新 cron 同步重算 next_run_at**（routes_tasks.py `update_schedule`）：用 croniter 以本地时间重算（`'%Y-%m-%d %H:%M:%S'`），与 `toggle_schedule` 同一实现，避免调度器仍按旧时间触发。
4. **toggle_schedule 不再吞错**（routes_tasks.py）：cron 重算失败返回 HTTP 500 + `detail`，不再静默把任务置为 `status='paused'`。
5. **会话删除级联**（routes_sessions.py）：`DELETE /api/sessions/{id}` 事务内删除 `messages/tasks/task_steps/token_usage/model_call_logs` 中该会话行（先查 `PRAGMA table_info` 确认 `session_id` 列存在），删除前将该会话 `_active_agents` 中的 agent 置 `is_interrupted`；`POST /api/sessions/{id}/clear` 补充清理 `task_steps`。
6. **history has_more 基于存在性判断**（routes_memories.py）：改用 `SELECT EXISTS(SELECT 1 FROM messages WHERE session_id=? AND id < ?)`，消息被删除后不再恒为 true。
7. **/api/settings 增量语义 + 掩码约定**：见"通用约定"，本次仅文档化确认既有行为。
8. **next_run_at 统一 UTC（批次 4，取代第 3 条的时区口径）**：`routes_tasks.py` 的 create/update/toggle 三个写入点原先以本地时间计算 `next_run_at`（且 create 时根本未写入，新建定时任务永不触发），而调度器 `api/background.py` 以 UTC 比较。现统一为 `croniter(cron, datetime.now(timezone.utc))`（辅助函数 `_next_run_utc`），存储格式仍为 `'YYYY-MM-DD HH:MM:SS'`（UTC）。存量本地时间行不做时区猜测，由调度器启动时 `_normalize_next_run_at_utc()` 按 cron 表达式一次性重算。

---

## 5. 插件 Vue 视图契约（新 SPA `/app`）

新 SPA 通过 manifest 的可选字段 **`vue_entry`** 发现并挂载插件前端（实现：`vue-app/src/plugins/registry.js`，参照实现：`plugins/open-agc-train/static/vue-entry.js`）。

### 5.1 manifest 与发现

- `plugin.json` 声明 `"vue_entry": "vue-entry.js"`（入口文件相对插件静态目录，经 `/static/plugins/<name>/<vue_entry>` 暴露）。
- `GET /api/plugins` 返回的每个插件对象带 `vue_entry` 字段（`core/plugin_manager.py` 的 `list_plugins` / `list_all_plugins` 透传）。
- 主 SPA 仅为 **`loaded && enabled && vue_entry` 非空** 的插件加载入口模块（原生 `import()`，不进 Vite 构建）。

### 5.2 入口模块

default export 必须是函数 `setup(ctx)`，可同步或异步返回：

```js
export default function setup(ctx) {
  return {
    views: [
      { path: 'designer', title: '模型设计器', icon: '🎓', component: /* 组件定义 */ },
    ],
  };
}
```

- 每个 view 注册为路由 **`/plugins/<name>/<path>`**（`router.addRoute`），并渲染在主 SPA 侧边栏的插件区（插件 label 来自 manifest `menu.label` / `menu.icon`）。
- `component` 必须用 `ctx.Vue` 创建（`ctx.Vue.defineComponent`）。主 SPA 的 Vite 构建将 `vue` 别名到 `vue/dist/vue.esm-bundler.js`（含模板编译器），因此插件组件可用**模板字符串**；**Element Plus 已全局注册**，插件模板可直接使用 `el-*` 组件，主题（Element Plus CSS 变量）共享。
- 插件代码**不能 `import 'vue'` / `import 'element-plus'`**（插件以浏览器原生 ES module 加载，裸导入无法解析），一律经由 `ctx` 获取。

### 5.3 ctx（setup 的唯一参数）

| 字段 | 说明 |
|---|---|
| `pluginName` | 插件名（如 `open-agc-train`） |
| `Vue` | 主应用的 Vue 命名空间（`defineComponent`/`ref`/`reactive`/`computed`/`watch`/`onMounted` 等），保证插件与主应用同一 Vue 实例 |
| `apiFetch(url, options)` | 主应用 api client 的 `request`（JSON 解析 + 错误规范化，参数同 `fetch`）；插件自行拼接 API 前缀（如 `/api/plugin/<name>` 或主应用 `/api/...`），**API 请求统一走它，不用裸 fetch** |
| `ElMessage` / `ElMessageBox` | Element Plus 反馈组件（toast / 确认框 / 输入框） |
| `wsOn(type, fn)` | 订阅主应用 WebSocket 事件（§3.2），返回退订函数（组件卸载时必须退订）；未连接时自动建立连接 |
| `navigate(path)` | `router.push` 封装（插件内跳转，如创建训练后跳监控页） |

### 5.4 旧前端并存

旧版插件前端（如 `plugins/open-agc-train/static/plugin.js`，IIFE + window 钩子）服务旧 SPA（`static/index.html`），与 `vue_entry` 互不干扰；两者通过同一套 `/api/plugin/<name>` REST 端点与 WS 事件通信。
