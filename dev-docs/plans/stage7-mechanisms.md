# 四机制优化（B1-B4）— 实施计划

> 来源：上下文/任务/目标/巡检四机制深度分析（4 路探索报告）。按 价值/成本 排序执行。
> 全局约束：不执行任何 git 操作（控制者统一提交）；UTF-8；每任务 `python -m pytest tests/ -q` 不回归（当前基线 558）；修复配行为测试；行号以实际代码为准（可能漂移）。

## Task 1（B1）：恢复链路收敛——堵 token 燃烧与双跑

1. **删 CAS 后降级**：`api/background.py` 三处（迟答恢复 ~:391、定时唤醒 ~:578、shell 完成 ~:784）在 `claim_task_for_resume` 成功后不再写回 interrupted，直接 `_run_background_task`（其内部会置 running）；shell 完成路径（~:781-788）补 `claim_task_for_resume(tid, ('backgrounded',))`。
2. **下载直启补 CAS + 原子标志**：`api/routes/routes_settings.py` 的 `_direct_resume_background_task`（~:96-117）起线程前补 `claim_task_for_resume(tid, ('backgrounded',))`；`background_resumed` 标志改为单条 `UPDATE downloads SET background_resumed=1 WHERE id=? AND background_resumed=0` 用 rowcount 判赢（BgMonitor 的下载分支同样改）。
3. **resume_count 收敛到认领点**：`claim_task_for_resume`（api/task_core.py:221）内每次成功认领 `resume_count+1`；Guardian 选任务处（background.py:987-996）的超限检查改为读最新值；wake/shell/下载路径因此全部自然计数。
4. **激活退避**：Guardian 选任务循环接入现成的 `_is_backoff_elapsed(t_updated, t_rc)`（background.py:512），未到期跳过；`max_resume_count` 超限置 `background_failed`（若已有此逻辑保持）。
5. **Scheduler 点火 CAS**：`api/background.py:448` 点火条件收紧为 `status IN ('completed','failed')`，起线程前 `claim_task_for_resume(task_id, ('completed','failed'))`（interrupted 让位 Guardian、backgrounded 让位 BgMonitor）。

验证：CAS 并发双认领测试（已有，需扩展覆盖新路径）；退避到期/未到期；resume_count 各路径递增；Scheduler 对 interrupted/backgrounded 不点火；pytest 不回归。

## Task 2（B2）：上下文防线真正生效

1. **工具结果写入截断恢复**（agent/agent.py:2670 附近，当前被注释）：恢复写入侧截断——按工具类型 cap（read_file/fetch_url 8000、execute_shell/execute_python 12000、其余 4000），超出走 `compress_tool_result`（agent/context_manager.py 现成实现，头+评分中段+尾）；与 _full_cap 注释说明职责分工。
2. **后台 resume 快照全量**：`api/background.py:283` 的 `agent.messages[msg_count_before:]` 改为与 ws 一致的 `agent.messages[1:]`（save_task_context 现有防缩守卫保留）。
3. **microcompact 激活**：`agent.py:2854-2856` 改为无条件 `self.messages = compacted`（时间戳一轮后生效）；恢复路径的 `_timestamp` 剥离逻辑保持。
4. **预算按模型窗口解析**：`core/llm_client.py` 初始化时按模型从 litellm model_cost 解析 `max_input_tokens`（llamacpp 用 `llamacpp_ctx_size`），写入 agent 的 TokenBudget（agent.py:271 处优先配置覆盖）；`llm_client.py:700` 的 `max_tokens=1000000` 硬编码改为解析值 × 0.9。
5. **plan 双注入去重**：删 agent.py:2013-2014 的第一段注入，保留 :2061-2063 带标题段。
6. **reasoning_content 剥离**：`core/llm_client.py` `_build_model_kwargs` 对每条消息 `pop("reasoning_content", None)`（单点覆盖 resume 旧快照）。
7. **failed_attempts 跨任务清空**：`run_turn` 开头随 `_consecutive_failures` 一起 `self.failed_attempts = []`。

验证：截断后单条工具结果不超 cap；后台快照轮次不缩水；microcompact 两轮后冷区消息被清；预算按模型解析（mock model_cost）；reasoning_content 不出现在发送消息中；plan 只出现一次；pytest 不回归。

## Task 3（B3）：目标机制可靠性

1. **goals.json 并发安全**：`tools/task_plan.py` 加模块级 `threading.RLock` 与 `update_goals(mutator_fn)` 助手（锁内 load-modify-save）；所有写路径（goal_add/goal_update/goal_archive、routes_goals.py PUT/DELETE、task_core `_check_goal_completeness`、background 巡检）改走它；`_atomic_json_write` tmp 名加 pid+线程 id；`_check_goal_completeness` 改 LLM 调用后重新 load 再改单条。
2. **删 `_generate_task_goal_background`**（task_core.py:96-121）及其调用点——每个任务白烧一次 LLM 且无人读取。
3. **判 NO 补救**：巡检判目标未完成时启用 goal.resume_count（<3 则创建补救任务：query 带目标 desc + 已有任务摘要 + 判 NO 理由，resume_count+1；超限置 `stuck` 并写 reason）；`failed` 从"已完结"集合剔除。
4. **尊重用户中断**：巡检创建续跑任务前查该目标最新任务 `interruption_reason == 'user'` 则跳过；`_resumable` 查询加 `resume_count < max_resume_count`。
5. **空 task_ids 回写**：WS 在 `ws_task_id` 确定后，若 `_resolved_goal > 0` 且不在 goal.task_ids 中则锁内追加；巡检对 doing/pending 但空 task_ids 的目标可创建首个任务并回链。
6. **goals 上限只统计 active**（pending/doing/stuck）；prompt 注入跳过 done；重叠检测改字符 bigram 或 difflib ratio ≥0.6。

验证：并发写不丢更新（两线程交替 update_goals）；判 NO 创建补救/超限 stuck；用户中断目标不被巡检复活；空 task_ids 目标被接管；pytest 不回归（新测试文件 tests/test_goals_mechanism.py）。

## Task 4（B4）：巡检可用性与误判治理

1. **stale rescue 移出 heartbeat 门控**：Guardian 的陈腐 running 复位块（background.py:961-975 附近）改为独立小循环（60s，纯 SQL），`heartbeat_enabled` 只控制 interrupted 自动恢复与 goal patrol。
2. **陈腐复位先查活**：复位前查 `_background_agents`/`_active_agents` 是否有该 task 活句柄，有则跳过（线程活着就不是孤尸）。
3. **停滞判定保守化**：`background.py:731-735` 的"活着但无输出"快路径（30s 判完成、删输出文件）改为统一长任务保守语义——停滞阈值 ~15min，解除追踪但如实告知"仍在运行、无输出 N 分钟"，不删输出文件；pid 死亡分支保留。
4. **无寄托 backgrounded 兜底**：解除追踪时写兜底 `wake_at`（+30min），让任务被自动收回询问用户；BgMonitor 无 pinfo 分支对超 6h 无寄托任务置 `background_failed`。
5. **邮件监听**（小）：mark_seen 移到任务落库成功后；回信文案按真实终态区分（completed/failed/interrupted 不再一律 "Task completed"）。

验证：heartbeat 关闭时 stale 复位仍工作；活 agent 句柄存在时不误判；静默进程 15min 内不被判完成；无寄托任务被兜底唤醒/终结；pytest 不回归。

## Task 5（B5）：体验与一致性收尾

1. **协议串泄漏清理**：`api/ws.py` 完成路径统一 `_user_facing(response)`——`[TASK_BACKGROUNDED]`/`[MAX_ITERATIONS_REACHED]` 不存聊天消息、不广播原文（task_backgrounded 事件已单独提示；max_iterations 剥前缀+可继续提示）。
2. **归属启发式修正**（task_core.py:312-324）：窗口改 `updated_at`；命中 `_CONTINUATION_PREFIXES` 无视长度续接；>10 字盲续仅限 interrupted/backgrounded；running 复用前先查 `_background_agents` 存活则改 `queue_message`。
3. **回放 sub_task 补列**：`api/state.py:112`、`api/ws.py:196`、`:1020` 三处 history_steps 查询补 sub_task 列；三处 `ORDER BY created_at` 改 `ORDER BY id`（task_core.py:533 同）。
4. **死代码**：删 ws.py:868-872 不可达分支（is_heartbeat 未定义）；`detached` 状态前端死样式（TasksView.vue:25-27）标注或清理。
5. **WS resume 合成提示不落库**：resume_task_id 非空时跳过 `save_message("user", query)`（api/ws.py:487 附近）。

验证：backgrounded 响应不出现在聊天消息/广播；归属矩阵（继续/新话题/长消息）；回放含 sub_task 且顺序稳定；pytest 不回归。
