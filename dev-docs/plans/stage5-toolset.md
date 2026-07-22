# 阶段 5：工具集优化 — 实施计划

> 来源：用户批准的工具集优化方案 P0-P2（P3 新能力按需后续）。
> 全局约束：不执行任何 git 操作（控制者统一提交）；UTF-8；每任务完成后 `python -m pytest tests/ -q` 与插件套件不回归；**每个任务必须用真实模型（kimi_code/k3 或 deepseek）实测一次 agent 工具调用链路正常**（脚本放 scratch/，跑完即弃或保留验证脚本）。
> 背景数据：当前 22 个工具，schema 总量约 30KB；工具描述中英混杂。

## Task 1：工具描述规范化 + schema 瘦身（P0 语义层）

1. 盘点 22 个工具的 description 与参数描述（`tools/*.py` 的 `get_openai_schema`），统一为规范格式：
   - 第一句：工具是干什么的（≤30 字）
   - 第二句：什么时候该用它（含与相近工具的区分，如 read_file vs search_file_content vs parse_html）
   - 参数描述：每个参数一行说清（含格式与默认行为）
   - 语言统一中文（现有中文工具保持，英文工具翻译，专有名词保留英文）
2. 压缩冗余：单个工具 description ≤200 字、参数描述每项 ≤60 字；删除重复说教（如在多个工具里重复的沙箱说明——沙箱拦截由系统层保证，描述只需一句"受沙箱限制"）。
3. 目标：全量 tool_schemas JSON ≤ 16KB（从 ~30KB 降 ~50%），在报告里给出前后对比表（每个工具的字节数变化）。
4. **不得改变任何工具的行为与参数结构**（名字、字段、required 全部不变，只动描述文本）。
5. 实测：用 kimi_code/k3 跑 3 个典型任务（读文件并总结、搜索代码并修改、运行命令），确认工具选择与调用正常（scratch/verify_tool_schemas.py 记录结果）。

验证：ast.parse 全部改动文件；schema 每工具的 JSON 仍可被 litellm 接受（用 LLMClient 实测）；pytest 不回归。

## Task 2：分层暴露接通（P0）

1. 先读 `tools/adaptive.py`（auto-resident 机制）与 `tools/discovery.py`（search_available_tools）和 `agent/agent.py` 的工具装载路径，弄清现状：哪些工具常驻、auto-resident 如何触发、惰性工具如何被发现。
2. 定义核心常驻集（建议 ≤10 个）：read_file、write_file、edit_file、execute_shell、execute_python、search_file_content、find_files、search_web、ask_user_question、self_review（最终清单以实现者评估为准并说明理由）。
3. 非常驻工具不在初始 tool_schemas 中，改为：首次用户消息匹配/模型主动 search_available_tools 时按需注入（复用现有机制；若机制只支持 auto-resident 两类，把"惰性"类接入同一注入路径）。
4. 目标：常规对话首轮 schema ≤ 10KB；需要时可通过发现机制调用任意工具（实测：让 agent 用 browser_automation 或 manage_memory，验证其能被发现并调用）。
5. 回退开关：config.json 加 `tool_tiered_exposure`（默认 true），false 时恢复全量常驻。

验证：首轮 schema 字节数对比（报告给数据）；agent 实测惰性工具可发现可调用；pytest 不回归。

## Task 3：检索强化（P1）

1. `tools/search.py` 的 `search_file_content`：新增可选参数 `context_lines`（前后 N 行，ripgrep -C N）、`output_mode`（`content`|`files_with_matches`|`count`，默认 content）、`head_limit`（结果条数上限，默认 50，超出标注截断）。Python re 回退路径保持同等语义。
2. `tools/filesystem.py` 的 `read_file`：新增可选 `offset`/`limit`（按行分页），返回时标注总行数与本次范围；超限提示用分页继续读。
3. 新增 `list_dir` 工具（tools/filesystem.py 或新文件）：列目录（可选递归深度 1-3、显示大小/修改时间、按时间或名称排序），受沙箱限制；注册进工具集。
4. 每个新参数/工具都更新 description（遵循 Task 1 的规范）。

验证：参数级测试（临时目录构造文件树，验证 context_lines/output_mode/分页边界）；agent 实测大文件分页读取任务；pytest 不回归。

## Task 4：编辑强化（P1）

1. `edit_file` 增强：新增 `replace_all: bool`（全部替换）；old_string 不唯一且未指定 replace_all 时返回明确错误（列出匹配行号与上下文各 1 行）；old_string 为空/未找到时错误信息包含建议（相近行提示，可选）。
2. 新增 `apply_patch` 工具：接受统一 diff 或简化的多文件编辑格式（二选一，实现者评估 LLM 生成可靠性后决定并说明理由），支持多文件多处在一次调用中应用，逐块报告成功/失败，任一失败不部分提交（或明确标注哪些已应用）。
3. 沙箱检查与新工具对齐现有 filesystem 工具的模式。

验证：edit_file 矩阵测试（唯一/多处/replace_all/未找到/空串）；apply_patch 混合成功失败场景；agent 实测一次多文件修改任务；pytest 不回归。

## Task 5：工具可靠性清偿（P2）

1. `tools/shell.py`：`is_background` 把 `npm start` 这类命令误判为后台（`start` 关键词正则过宽，:240 附近）——收窄匹配（Windows 的 `start` 命令需独立词首且非引号内；`npm start`/`start.py` 不算）；后台分支输出丢失问题（报告里说明取舍）。
2. `tools/shell.py:432-451` 交互式误判：`b'ress: '` 和 `b' :'` 误伤面广——收窄为整行匹配（如行尾 `mysql>`/`sqlite>`/`>>>`/`:~$` 等提示符模式）。
3. `tools/download.py:181-193`：直连下载缺 `raise_for_status`，404 错误页会被当成下载完成——补上并校验最终大小。
4. `tools/auto_tool.py:115-138`：生成代码安全校验从 6 条正则改为 AST 分析（禁止 import os/subprocess/sys 的非白名单成员、禁止 eval/exec/__import__/open 网络写入等——定义白名单 API 面，文档化）。
5. `tools/mcp_tool.py:128`：`future.result()` 无超时（MCP 卡死则线程永久阻塞）——加 `timeout=120` 并对超时做 session 重建；`load_servers` 的 `if name in self._sessions: continue` 改为断线重连（检查 session 活性）。

验证：每项配测试（误判矩阵：npm start/start 命令/普通输出含 "Progress: "；下载 404；AST 拦截用例；MCP 超时 mock）；pytest 不回归。
