# 阶段 6（P3）：新能力工具 + auto_tool 机制修复 — 实施计划

> 来源：工具集优化方案 P3 + 用户提出的 auto_tool 复用率问题。
> 诊断结论（数据在案）：132 个自动工具仅 1 个有真实复用；根因四点——
> ① 生成门槛形同虚设（成功任务 ≥5 调用即生成，大量一次性任务专用工具）；
> ② `record_tool_usage` 无调用方，`_trust.json` 从不存在，毕业机制从未运行；
> ③ 无去重（功能重叠严重）；④ 会话级存储 + 无清理（只增不减）。
> 全局约束：不执行任何 git 操作（控制者统一提交）；UTF-8；每任务 `python -m pytest tests/ -q` 不回归；新工具配行为测试与 k3 实测。

## Task 1：auto_tool 机制修复

1. **生成前复用性判定**（agent/agent.py 触发处 + tools/auto_tool.py）：
   - 触发条件收紧：成功 + ≥5 调用 + **轨迹以确定性命令/脚本为主**（execute_shell/execute_python 占比高的才适合固化；以 read_file/search 为主的探索型轨迹不生成）。
   - 生成前 LLM 复用性判定（一次轻量调用）：输入轨迹摘要 + 已有自动工具清单，输出 {reusable: bool, reason, suggested_name}；reusable=false 则跳过；与已有工具语义重叠（名称/描述关键词重合）则改为记录该既有工具（强化而非新建）。
2. **usage 记录接通**：在 agent 工具执行完成处（agent.py tool_done 路径，仅当工具来自 auto_tools 时）调用 `record_tool_usage(tools_dir, name, success)`——先读 auto_tool.py 的 DynamicTool 与现有调用链确认接入点；成功毕业（≥3 连续成功）走现有 graduate_tool 移到 skills/permanent。
3. **会话作用域调整**：新生成工具仍存 `auto_tools/{session_id}`；**已毕业工具（skills/permanent）在所有会话加载**（检查当前加载路径是否覆盖 permanent，没有则补）。
4. **清理机制**：新增 `prune_auto_tools(tools_dir, max_age_days=30, min_calls=1)`——调用次数 < min_calls 且超过 max_age_days 未使用的工具移入 `_archive/` 子目录（不硬删）；启动加载时跳过 `_archive`。在 auto_tool.py 实现 + 在 agent 启动加载处调用一次。
5. **存量治理**：对现有 132 个工具跑一次 prune（作为实现的一部分或提供脚本 scratch/prune_auto_tools.py 让用户可重跑）；报告给出"保留/归档"分类统计。

验证：usage 记录→毕业链路测试（临时目录模拟 3 次成功→移入 permanent）；复用性判定的轨迹分类测试（确定性 vs 探索型）；prune 归档测试；agent 实测（构造一个成功任务，检查不再随意生成）。

## Task 2：fetch_url 工具

1. 新增 `fetch_url` 工具（tools/web_search.py 同文件或 tools/fetch_url.py）：轻量抓取 URL 正文（requests + 简单 HTML→text 提取，复用现有 fetch_page_content 逻辑如有——先读 tools/web_search.py:487 附近实现），参数 `url`（必填）、`max_chars`（默认 8000）、`raw`（bool 返回原始 HTML）。
2. 防 SSRF：拒绝内网地址（127.0.0.0/8、10.0.0.0/8、172.16.0.0/12、192.168.0.0/16、169.254.0.0/16、localhost 域名）——llamacpp 等本地服务本来就靠专用渠道；响应大小上限（如 2MB 截断）。
3. 注册进分层核心集（与 search_web 同位置）；描述遵循规范（与 search_web/parse_html 的场景区分写清）。

验证：SSRF 拒绝矩阵；大小截断；agent 实测抓取一个公开页面。

## Task 3：image_view 工具

1. 新增 `image_view` 工具：读取本地图片返回给视觉模型（与现有 `:image` 附件同通道——先读 agent 对 images 的处理 build_user_message，工具的返回如何携带图片：若工具结果只能是文本，实现为"把图片注入下一轮 user 消息 images 数组"的机制，先读 agent.py 工具结果处理确认可行路径；选最简单可靠的一种并说明）。
2. 参数：`path`（必填，沙箱限制）、`max_size`（默认 1024px 缩放，减少 token）。非视觉模型调用时返回明确错误（检查 llm 能力标记或模型名启发式 + 配置开关）。
3. 注册（惰性集即可）；描述遵循规范。

验证：沙箱拒绝；缩放；agent 实测让 k3/deepseek（视觉能力模型）看图说话；非视觉模型的错误提示。

## Task 4（可选，视 1-3 完成情况）：显式子代理 dispatch 工具

- 新增 `dispatch_subagent` 工具：显式发起子代理（参数 task、tool_set、max_iterations），复用 SubAgent 现有实现；让模型在长任务中自主选择分派而非仅依赖 _should_delegate 启发式（同时修 _should_delegate 对 "read_file" 字面量必委派的怪癖：关键词与工具名分离）。
