# 阶段 4：持续优化 — 实施计划

> 来源：总方案阶段 4。目标：Agent 可靠性 + 任务系统健壮性 + 观测一致性 + 死代码清理。
> 全局约束：不执行任何 git 操作（控制者统一提交）；UTF-8；改动最小；每任务完成后 `python -m pytest tests/ -q` 与 `python -m pytest plugins/open-agc-train/tests/ -q` 不回归；新行为配最小测试。
> 证据来源：全量代码审查报告（行号以实际代码为准）。

## Task 1：Agent 主循环可靠性（agent/agent.py）

1. **主循环 LLM 调用无异常保护**（约 :1720）：`response, actual_model = self.llm.chat(...)` 与 `response.choices[0]` 不在任何 try 中，网络错误/空 choices 直接逃出 `run_turn`，跳过 `_save_task_stats`、skill feedback、KG extraction 等收尾。修复：包 try/except，失败时走与 max_iterations 相同的清理路径（收尾必须执行）；`choices` 为空时按失败处理而非 IndexError。
2. **委派静默丢任务 + KeyError**（约 :1580-1615）：循环依赖/依赖失败时 `batch` 为空直接 break，`remaining` 子任务被丢弃且最终报告不提；`plan["task"]`/`plan["id"]` 无 `.get()` 防护。修复：`plan.get("id", i)`、`plan.get("task", "...")` 等默认值；结束时把未执行的子任务列入综合报告（明确标注"未执行"）。
3. **interjection 索引 off-by-one**（约 :2133/:2140）：追加顺序为 user interjection(-3) → assistant tool_call(-2) → tool result(-1)，accept/reject 分支读写的 `self.messages[-2]` 指向 assistant 消息而非用户插话。修复：索引改 `-3`（先读代码验证当前追加顺序再改）。
4. **插话超时静默丢消息**（约 :670-676）：`_interjection_stuck_count > 12` 时注释写 "Auto-pop and accept"，实际 `pop(0)` 后 `return ""`——用户消息既未注入上下文也未创建新任务。修复：超时后把该消息作为普通 user 消息注入当前 loop（或走 reject 流程），并在回复中告知用户该消息的处理方式。
5. **wait_for_user_input 永久阻塞**（约 :2400）：`self.user_input_queue.get(block=True)` 无超时不查中断，用户停止任务后 agent 线程永远泄漏。修复：`get(timeout=1)` 循环 + `is_interrupted` 检查 + 总超时（如 300s，超时按中断处理）。
6. **post-process 线程竞态**（约 :2338-2342/:2359-2364）：每回合裸建 daemon 线程跑 `_background_post_process`，读 `self.messages`、写两个 SQLite 库，与下一回合并发。修复：改为单 worker 串行队列（模块级或实例级 `queue.Queue` + 常驻 worker 线程），传入 `list(self.messages)` 快照。

验证：每项给 before/after 与理由；能测的配测试（interjection 索引、委派缺字段、post-process 串行性），放 tests/。

## Task 2：子代理与沙箱等待（agent/sub_agent.py、agent/agent.py）

1. **sub_agent 锁创建竞态**（sub_agent.py 约 :204-205）：`if func_name not in self._tool_locks: self._tool_locks[func_name] = Lock()` 是 check-then-act，并行子代理可各自创建 Lock 互相覆盖，browser/computer 互斥失效。修复：类级预初始化两把锁（browser_automation/computer_control），或 setdefault + 元锁。
2. **子代理吞 SandboxBlocked**（sub_agent.py 约 :226）：裸 `except Exception` 把 SandboxBlocked 当普通错误返回字符串，子代理无授权通道。修复：显式 `except SandboxBlocked` 并透传到主 agent 的授权流程（先读主 agent `_handle_sandbox_blocked` 与 ThreadPoolExecutor 调用处的结构，选最小透传方案：重新抛出并在主循环捕获接入 `_handle_sandbox_blocked`）。
3. **_sandbox_waits 键冲突 + 等待不可中断**（agent.py 约 :738-740）：以 session_id 为键，同会话并发两次阻塞后者覆盖前者，前一个 wait_event 永远无人 set 只能等 120s；`wait_event.wait(timeout=120)` 期间不查 `is_interrupted`。修复：改用唯一 request id 作键（消息里带给前端，ws.py 侧响应也按此 id 匹配——先读 api/ws.py 的 sandbox_response 处理确认匹配字段，改动需两侧一致）；等待改分段循环（每次 wait(1)，检查中断与超时）。

验证：锁互斥测试（多线程同一 func 串行）；_sandbox_waits 并发两次不互相覆盖（单测）；ws.py 同步改动过 ast.parse。

## Task 3：任务系统健壮性（api/background.py、api/ws.py、api/task_core.py）

1. **Guardian 被陈腐 running 卡死**（background.py 约 :844-848）：发现任何 status='running' 的非心跳任务就无限 sleep——agent 线程崩溃前没重置状态则 Guardian 永久停摆。修复：增加"running 但 updated_at 超过 N 分钟（建议 15）"的陈腐判定，陈腐任务复位为 interrupted 并允许 Guardian 继续（先读现有代码结构再接入）。
2. **running 任务无条件复用**（task_core.py 约 :229-231）：`_resolve_task_for_query` 遇到该会话最新任务是 running 就复用，用户新消息全部追加进一个死任务。修复：复用前做同样的新鲜度检查（updated_at 超阈值则不复用，另建新任务）。
3. **step_offset 两处不一致**：后台 resume 的 offset 用 `COALESCE(MAX(step_number),0)`（background.py 约 :152-155），ws.py 用 `MAX+1`（约 :177-181）——后台 resume 后新步骤号与旧记录撞号，`tool_done` 的 UPDATE 按 (task_id, step_number) 匹配会误改旧步骤。修复：统一为 `MAX(step_number)+1`。
4. **ws 恢复路径 step 双重偏移**（ws.py 约 :218-219 vs :352-353）：218 行已加一次 offset 写回 event["step"]，352 行又加一次——推给前端的步骤号 = 原始值 + 2×offset，与落库值（正确）不一致。修复：删 352-353 的重复偏移（先读代码确认两处语义，保留落库正确的那条路径）。
5. **会话历史把 system 消息塞进 LLM 上下文**（ws.py 约 :97,105-109）：`role != 'tool_step'` 过滤带出 `save_message("system", ...)` 的系统通知（下载通知等），role 不映射直接进 `agent.messages`——严格的 provider 会报错。修复：过滤条件改 `role IN ('user','agent')`。

验证：陈腐判定与复用新鲜度的单测（临时 DB）；step_offset 统一性检查脚本或单测；ast.parse。

## Task 4：llm_client 修复族（core/llm_client.py、core/db_maintenance.py）

1. **_infer_provider 顺序**（llm_client.py 约 :66-67）：`"llama" in ml` 在 `"llamacpp"` 之前，`llamacpp/xxx` 永远命中 "llama" 返回 provider="llama"，"local" 分支死代码——统计按 provider 分组时错归类。修复：llamacpp/sglang 判断提前。
2. **_sanitize_for_llamacpp 丢 system 消息**（约 :521-542）：遍历中 `system_content` 被每条 system 消息覆盖，只有最后一条被合并进首个 user 消息，其余（主 agent 提示词）静默丢弃。修复：收集所有 system 内容拼接后合并。
3. **上下文压缩原地篡改调用方列表**（约 :658）：`messages[:] = truncated` 写回调用方持有的列表（隐蔽副作用）；且该重试对 llamacpp 绕过 `_sanitize_for_llamacpp`。修复：不原地改（用副本），重试前重新走 `_build_model_kwargs`。
4. **model_call_logs 建表缺 cached_tokens 列**（约 :32-49 vs :189-198）：INSERT 依赖 api/db.py 的 ALTER 补列才不出错；先于 init_db 的调用会静默失败。修复：本模块建表语句补列 + 幂等 ALTER；`_init_model_logs_table` 加初始化标志不再每次执行。
5. **清理永假式**（db_maintenance.py 约 :38-42）：`cleanup_model_logs` 默认 `min_cost=0.0` → SQL 条件 `cost_estimate < 0.0` 无任何行满足，retention 从未生效；`freed_bytes` 永远返回 0。修复：min_cost<=0 时去掉该谓词；删除文件时累计实际字节数返回。另：删除的 `reflections` 表不存在（真实表名 task_trajectories，约 :122-126），更正。

验证：_infer_provider 参数化测试；sanitize 多 system 测试；cleanup 用临时 DB 测试删除与字节数；ast.parse。

## Task 5：eval 隔离 + 观测一致性 + 死代码清理

1. **eval/probes.py 污染生产库**（eval/probes.py 约 :42-55）：`probe_memory_recall` 往生产 `data/memory.db` 写 7 条测试记忆不清理；`probe_tool_choice_quality` 的部分任务在真实环境产生副作用。修复：改用临时 db_path（tmp 目录）并在结束清理；有副作用的 probe 默认跳过需显式开启。
2. **成本费率统一**（core/stats_manager.py vs core/llm_client.py）：两套不同费率导致同一调用两处成本不一致；`get_task_usage` 错误路径缺 `"cost"` 键、零行 SUM 得 None。修复：抽公共费率表到一处（llm_client 的 `_calculate_cost` 为准），stats_manager 复用；get_task_usage 返回值补齐 cost 键与 0 兜底。
3. **死代码清理**（逐项核实无引用后删除）：
   - `agent/context_manager.py` 的 `compact_messages`（全项目无调用）及其内部 `bounds` 死变量、:243 调试 print
   - `api/task_core.py` 的 `record_tool_step`（定义了无人调用，ws/background 各自内联）
   - `agent/agent.py:2159-2162` 的 `_fake_ctx` 死代码；:84/89 与 :85/334 重复初始化（合并）
   - `prompt_builder.py:131` mixin 版 `_build_system_prompt` 的 `prompt` 未定义 NameError 地雷（agent.py 有同名方法遮蔽，删除 mixin 版重复方法或修复——先确认遮蔽关系选删除）
   - `core/llm_client.py:19-26` 线程本地 SQLite 连接进程生命周期不关闭（改 db_connect 或 closing）
4. **dev-docs/Agent优化方案.md 与实际状态同步**：文档声称反思/KG 等功能"已完成"，但阶段 0 才发现反思功能 100% 失效已久——在该文档加一节"已知失效修复记录"注明这些功能曾因吞异常长期失效、阶段 0/3 修复，提醒后续勿以文档完成度为准。

验证：probes 跑一遍确认生产库无新增（测试断言 memory.db 行数不变）；费率一致性测试（同一 model/tokens 两处结果相等）；删除后 ast.parse + pytest 不回归。
