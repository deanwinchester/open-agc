# 调度者模式（Dispatcher Mode）实验方案

分支：`feat/dispatcher-mode`　基线提交：`a3ddc19`
日期：2026-08-10

## 1. 背景与动机

现状：主 agent 单线程执行一切——长任务上下文腐烂（执行多轮后"不知道自己在做什么"）、
子代理上下文不足导致失败、用户输入只能串行排队。

实验目标：验证"调度者模式"是否在不显著增加成本的前提下提升任务成功率与可观测性。

## 2. 方案设计（实验范围 v1）

### 2.1 核心流程

```
用户输入
   │
   ▼
[意图分类] —— 闲聊 / 追加指令 / 新任务（见 §2.3）
   │
   ▼（新任务）
[调度者] 意图澄清 + 上下文装配（交接包 Handoff Packet）
   │
   ▼
[执行 agent]（单执行者，复用 SubAgent 循环：全量工具发现 + 营救 + 空谈守卫）
   │
   ▼
[证据验收] 调度者抽查产出物（文件/日志/测试）→ 呈现结果
```

### 2.2 交接包 Schema（本方案的技术核心）

```json
{
  "intent": "澄清后的用户真实意图（一句话）",
  "background": "任务背景（为什么做）",
  "relevant_history": [{"task_id": 123, "title": "...", "result": "...", "key_steps": ["..."]}],
  "memories": ["相关记忆条目…"],
  "files": ["相关文件/目录路径"],
  "acceptance": ["验收标准1", "验收标准2"]
}
```

装配来源（全部复用现有设施）：`memory_store` 语义检索、`manage_task`/`search_history`
历史任务、`task_steps` 关键步骤、`.checkpoints/` 进度文件。

### 2.3 输入分类（用户新需求的实现）

调度者对每条新输入做轻量 LLM 分类（一次小调用，几百 token）：

| 分类 | 判定要点 | 动作 |
|---|---|---|
| 闲聊 `chat` | 无执行意图（问候/提问/讨论） | 主 agent 直接答，不建任务 |
| 追加指令 `append` | 与进行中任务同主题（"顺便也…""别忘了…""改成…"） | 注入运行中 agent 的消息队列（现有 queue_message 通道） |
| 新任务 `new_task` | 新目标 | 装配交接包，指派新执行 agent |

并发任务：多执行 agent 并存。**会话界面需要配套改造**（v1 先简单）：
- v1：进度卡片按 task_id 分卡（已有），多任务各自更新自己的卡；
- v2（后续）：会话内"任务泳道"视图，正在运行的任务并列展示。

### 2.4 验收与失败回路

- 执行 agent 返回结构化结果（产出文件、步骤、自测）。
- 调度者做轻量验收（产出文件存在性/关键断言），失败 → 补充上下文重派一次 → 再失败接管执行。
- 验收结论与交接包写入任务上下文，界面可查。

## 3. 测评方案（A/B：baseline vs dispatcher）

复用 `eval/` 框架（runner + scenarios + history 对比）。

### 3.1 指标

| 指标 | 来源 | 目标 |
|---|---|---|
| 任务成功率 | eval scenario 断言（output_contains/tool_used/max_steps） | dispatcher ≥ baseline +5pp |
| Token 消耗 | llm_client 统计（prompt/completion/cached） | dispatcher ≤ baseline ×1.3 |
| 执行步骤 | task_steps 数 | dispatcher ≤ baseline |
| 端到端时延 | runner 计时 | 参考值 |
| 用户好评 | 会话内 👍/👎（新增 UI 反馈） | 主观对照 |

### 3.2 方法

1. 选定场景集：从 eval/scenarios 取 20-30 个覆盖 shell/filesystem/python/web/复合任务；
   另加 10 个"长任务"场景（多阶段、需引用历史结果的任务——调度模式的主战场）。
2. 同一场景集跑两遍：`--mode baseline`（现行单线程）与 `--mode dispatcher`。
   runner 增加 `--mode` 参数，dispatcher 模式经环境变量/配置开关启用。
3. 结果落 `eval/results/history.json`，`--report` 出对比表。
4. 每个场景跑 3 次取成功率（LLM 随机性）。

### 3.3 用户好评采集

- 任务完成消息上加 👍/👎 按钮（新字段 `messages.rating`），仅作实验期参考。

### 3.4 判定标准

实验通过（合并 main）需同时满足：成功率不降、token 增幅 ≤30%、长任务场景成功率提升 ≥10pp、
无 Critical 评审问题。

## 4. 里程碑

- M1：交接包装配 + 单执行者派发 + 证据验收（最小闭环，直执兜底）
- M2：输入分类（chat/append/new_task）+ 追加指令注入
- M3：eval 接入（--mode、报告、3 次取均值）+ 用户反馈按钮
- M4：并发任务 UI（泳道雏形）
- M5：A/B 跑分 + 评审 + 合并决策

## 5. 风险与对策

- **装配质量即上限**：交接包在界面对用户可见，可纠偏。
- **成本膨胀**：token 增幅 >30% 即触发回退评审；闲聊/小任务必须走直执。
- **模型方差**：每场景 3 次取成功率；判定标准用 pp（百分点）而非相对值。
