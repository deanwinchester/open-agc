# Open-AGC Agent 优化方案

## 现状总览

当前 Agent 的核心瓶颈：

1. **Skill 一次性全量注入**：所有技能全文塞进 system prompt，无论是否用到
2. **无上下文预算管理**：messages 列表无限增长，唯一的保护是单条工具结果 15K 截断
3. **工具结果暴力截断**：长度超限直接砍半，丢失中间信息
4. **无子代理机制**：所有任务在单循环中串行执行
5. **记忆系统未分层利用**：虽然存储了 core/working/episode，但实际使用单一
6. **自学习能力薄弱**：仅有被动 save_learned_skill 和自动记忆提取，无反馈闭环

---

## 实施状态总览

### 已完成（第一阶段 — 核心优化）

| 项目 | 优先级 | 涉及文件 | 状态 |
|------|--------|---------|------|
| 渐进式 Skill 注入 | P0 | `core/skill_store.py`, `agent/agent.py` | ✓ 完成 |
| 工具结果自动摘要压缩 | P0 | `agent/agent.py` | ✓ 完成 |
| 上下文预算管理 | P1 | `core/token_budget.py`, `agent/agent.py` | ✓ 完成 |
| 技能质量评分 + 自动修正 | P1 | `core/skill_store.py`, `agent/agent.py` | ✓ 完成 |
| 反思机制（Reflexion） | P1 | `core/reflection.py`, `agent/agent.py` | ✓ 完成 |
| 多轮工具调用折叠 | P2 | `agent/agent.py` | ✓ 完成 |
| 配置自适应 | P2 | `agent/agent.py` | ✓ 完成 |
| 知识图谱 | 进阶 | `core/knowledge_graph.py`, `agent/agent.py` | ✓ 完成 |
| 自动工具创造 | 进阶 | `tools/auto_tool.py`, `agent/agent.py` | ✓ 完成 |
| 子代理委派 | 进阶 | `agent/sub_agent.py`, `agent/agent.py` | ✓ 完成 |

### 未完成 / 待优化

| 项目 | 说明 | 优先级 |
|------|------|--------|
| Task stats 统计表持久化 | 配置自适应缺乏执行反馈数据持久化 → agent.db task_stats 表 | 低 |
| 自动工具安全提升机制 | 自动工具连续 3 次成功后才升为永久工具的逻辑未实现 | 低 |
| 子代理并行执行 | 当前串行执行，可改为 ThreadPoolExecutor 并行 | 中 |
| 向量检索增强记忆 | 可用 embedding 替代关键词检索 | 中 |
| 子代理结果深度合成 | 当前仅摘要拼接，可让主 Agent 用 LLM 二次加工 | 低 |
| 长期运行统计调优 | 定期分析 task_stats 自动调整 TASK_CATEGORIES 参数 | 低 |

---

## 一、Progressive Skill Injection（渐近式技能注入）

### 目标
从"全量灌注"改为"按需检索注入"，节省上下文空间。

### 具体方案

#### 1.1 技能索引

```
skills/
  index.json            ← 自动生成，结构见下
  web-deploy.md
  data-analysis.md
  ...
```

`index.json` 格式：

```json
{
  "version": 1,
  "skills": [
    {
      "filename": "web-deploy.md",
      "description": "部署 Nginx + FastAPI 应用到 Linux 服务器",
      "keywords": ["deploy", "nginx", "fastapi", "server", "linux", "部署"],
      "usage_count": 12,
      "success_rate": 0.92,
      "last_used": "2026-05-10T15:30:00"
    }
  ]
}
```

#### 1.2 检索与注入

- **初始化时**：仅加载 `index.json`（轻量元数据），不加载全文
- **用户提问时**：对用户输入 + 最近的 tool_call 做关键词/语义匹配，召回 top-2/3 技能
- **在 `_build_system_prompt()` 中动态注入**召回的技能全文
- **Agent 主动请求**：新增 `request_skill` 工具，Agent 认为自己需要额外技能时主动调用

#### 1.3 反馈闭环

- 每次技能被使用后记录 `success/fail`
- 同一技能连续失败 ≥3 次 → 触发 LLM 分析原因 → 自动改写技能文件加注意事项
- 长周期未使用的技能 → 自动降级（减少召回优先级）

### 涉及文件
- `agent/agent.py` — `__init__` 改为只加载索引；`_build_system_prompt` 改为动态检索注入
- `core/skill_store.py` — **新文件**：技能索引生成、检索、统计、自动修正
- `tools/save_skill.py` — 保存技能时同时更新索引

---

## 二、Context Window Budget（上下文预算管理）

### 目标
为每次 LLM 调用设定 Token 预算，避免上下文无限膨胀。

### Token 预算分配

| 区域 | 预算比例 | 说明 |
|------|---------|------|
| System Prompt | 20% | 角色设定、工具 schema、注入的技能 |
| Conversation History | 50% | 用户-Agent 最近对话轮次 |
| Tool Results | 30% | 工具执行结果（已摘要压缩的） |

### 实现策略

```
每次构建 messages 时:
  1. 估算各区域 token 数
  2. 若总 < 预算 → 完整发送
  3. 若超预算 → 按优先级裁剪:
     a. 压缩最早的工具调用-结果对为一行摘要
     b. 裁剪最早的对话轮次 (保留最近 3 轮完整)
     c. 压缩 system prompt 中的工具描述 (仅保留名称)
  4. 保留 system prompt 核心不可裁
```

### 可配置参数

```json
{
  "context_budget": {
    "max_total_tokens": 64000,
    "min_keep_rounds": 3,
    "system_ratio": 0.20,
    "history_ratio": 0.50,
    "tool_ratio": 0.30
  }
}
```

### 涉及文件
- `agent/agent.py` — `run_turn` 中新增预算检查与裁剪逻辑
- `core/token_budget.py` — **新文件**：token 估算、预算分配、裁剪策略
- `core/paths.py` / config — 配置参数

---

## 三、Tool Result 自动摘要压缩

### 目标
替代当前暴力截断，对超长工具结果做智能摘要，保留关键信息。

### 实现

```
工具结果长度 > 3000 tokens?
  → 调用 LLM（用快模型，如 gpt-4o-mini 或本地模型）
  → prompt: "请用中文摘要以下命令执行结果，保留关键数字、路径和结论：\n{result}"
  → 摘要替换原始结果存入 messages
```

预算充足时保留完整结果，仅在接近预算上限时触发摘要。

```python
# 伪代码
def compress_tool_result(result: str) -> str:
    if estimate_tokens(result) < COMPRESS_THRESHOLD:
        return result
    summary = self.llm.chat(
        messages=[{"role": "user", "content": f"摘要以下内容保留关键信息:\n{result}"}],
        model="gpt-4o-mini"  # 快模型
    )
    return f"[Compressed] {summary}\n[Original: {len(result)} chars → {len(summary)} chars]"
```

### 涉及文件
- `agent/agent.py` — 工具结果后处理环节

---

## 四、多轮工具调用折叠

### 目标
连续工具调用-结果对占用大量上下文，将其压缩为执行轨迹摘要。

### 实现

```
检测到连续 N 轮 tool_call + tool_result:
  前 N-2 轮 → 折叠为一行摘要:
    "📋 已完成: ls (列出文件) → grep (搜索关键字) → cat (读取结果)"
  保留最后 2 轮完整
```

折叠阈值 N 默认为 6，可在配置中调整。

### 涉及文件
- `agent/agent.py` — `run_turn` 循环体末尾

---

## 五、Memory 层级优化

### 目标
从"单一检索"升级为分层利用，让不同层级的记忆发挥不同作用。

| 层级 | 生命周期 | 存储 | 使用方式 | 优化内容 |
|------|---------|------|---------|---------|
| Working | 当前会话 | `messages` | 每次 LLM 调用 | 加入滑动窗口 + 预算管理 |
| Episodic | 跨会话 | `memory_store.conversations` | run_turn 开头检索 | 自动会话摘要 + 失败反思 |
| Semantic | 长期 | `memory_store.memories` | run_turn 开头检索 | 增加向量检索（可选） |
| Procedural | 长期 | `skills/` | 动态检索注入（第一章） | 反馈闭环优化 |

### 新增：Episodic 反思机制

```
任务结束时（无论成功/失败）:
  → 调用 LLM 分析: 这个任务的关键步骤、关键决策、失败原因
  → 以 memory_type=episode 存入 memory_store
下次相似任务开始时:
  → 自动检索相关 episode
  → 注入 system prompt 作为参考
  → "[历史经验] 上次部署时遇到依赖冲突，解决方案是先运行 pip check"
```

### 涉及文件
- `agent/agent.py` — `run_turn` 末尾新增反思提取
- `core/memory_store.py` — 可能需扩展检索接口

---

## 六、自学习与进化体系

> 实施状态：核心闭环（6.1 技能反馈 + 6.2 Reflexion）已实现，知识图谱/自动工具/配置自适应已实现

### 6.1 技能自进化闭环 ✓（已完成）

```
Agent 完成任务
  → 评估成功率（工具调用次数、错误率、用户反馈）
  → 如果成功且步骤有参考价值 → 自动提取核心步骤序列
  → 对比已有技能（检索去重），新模式则生成草案
  → 如果技能执行失败 → 记录失败模式 → 自动修正
```

**技能质量评分**：
- 每次使用技能记录 `success/fail`、`iterations_used`、`error_type`
- 存入 `skill_usage_stats` 表（SQLite，共用 `chat_history.db`）
- 查询技能时参考评分排序

**自动修正**：
- 同一技能连续失败 ≥3 次 → 触发 LLM 分析失败原因
- 重新读取技能文件 → 分析操作步骤与实际结果的偏差
- 生成修正建议 → 写入技能文件末尾的「⚠️ 已知问题」区

### 6.2 经验回放（Reflexion 模式）✓（已完成）

参考 Reflexion 论文 + Stanford Generative Agents：

- **Trajectory 存储**：成功任务的完整 tool_call 序列存入 `task_trajectories` 表
- **Reflection 生成**：任务失败后，LLM 分析原因 → 生成反思文本 → 存入 episodic 记忆
- **Few-shot 注入**：新任务开始时，检索相似的成功轨迹作为示例注入 system prompt
- **反思召回**：同时检索相关失败反思，提前规避已知陷阱

```python
# 伪代码：反思生成
def generate_reflection(task_input, messages, success):
    if success:
        return  # 成功不需要反思
    reflection = llm.chat(
        f"任务 '{task_input}' 失败了。分析以下执行过程，找出失败原因和改进建议。\n"
        f"执行记录: {summarize_tool_calls(messages)}"
    )
    memory_store.add_memory(
        content=reflection,
        category="tech",
        memory_type="episode",
        importance=3
    )
```

### 6.3 知识图谱构建 ✓（已完成）

```
实体提取（文件路径、项目名、API、命令、依赖）
  → 关系抽取（依赖、冲突、顺序、替代）
  → 轻量图结构（用 SQLite 关系表模拟）
  → 任务规划时检索相关实体关系
```

例如 Agent 学过：
- 部署 open-agc → 需要 Python 3.10+
- 安装 Python 3.10 → 需要 build-essential（Linux）或 Visual Studio（Windows）
- 下次被问"部署"时能推理出完整前置链

### 6.4 自动工具创造 ✓（已完成）

当前 `save_learned_skill` 保存 Markdown → 模型读文本后自行发挥。

进化目标：

| | 当前 Skill | 未来可执行 Tool |
|--|-----------|----------------|
| 格式 | Markdown 步骤 | Python 脚本 + schema |
| 使用方式 | 注入 prompt，模型阅读 | 作为工具注册调用 |
| 可靠性 | 取决于模型执行准确度 | 确定性执行 |
| 维护 | 纯手工 | 可自动测试 + 修正 |

实现路径：
- 先以 `skills/*.py` 格式保存，约定函数签名
- Agent 完成任务后自动提取核心步骤 → 生成 Python 函数 + 参数 schema
- 注册为临时工具（会话级）→ 多次验证 → 提升为永久工具

### 6.5 配置自适应 ✓（已完成）

基于任务特征自动调整 Agent 参数：

| 参数 | 自适应策略 |
|------|-----------|
| `max_iterations` | 文件读写类 → 15；部署类 → 50；探索未知代码库 → 30 |
| `temperature` | 代码/命令生成 → 0.1；分析/搜索 → 0.3；创意/写作 → 0.7 |
| 模型选择 | 文件操作用本地模型；复杂推理用云端强模型 |
| `context_budget` | 长对话逐步收紧，短对话放宽 |

通过 `manage_memory` 保存任务类型 → 参数映射。

### 涉及文件
- `core/skill_store.py` — **新文件**：技能索引、评分、自动修正、去重
- `core/reflection.py` — **新文件**：反思生成、轨迹存储
- `core/knowledge_graph.py` — **新文件**：实体关系抽取与查询
- `agent/agent.py` — 集成上述模块
- `tools/auto_tool.py` — **新文件**：自动工具生成与注册
- DB 迁移：`skill_usage_stats`、`task_trajectories`、`reflections` 表

---

## 七、子代理/任务委派（进阶）

### 目标
对复杂任务，主 Agent 将子任务委派给子 Agent，防止单上下文爆炸。

### 方案

```
主 Agent 收到复杂任务 "部署 Web 应用"
  → 拆分子任务: 1. 安装依赖 2. 配置 Nginx 3. 启动服务
  → 为每个子任务启动 SubAgent(sub_task, max_iterations=10)
  → SubAgent 独立运行，拥有独立 messages
  → SubAgent 返回结构化的结果摘要 {success, output, artifacts}
  → 主 Agent 汇总结果回复用户
```

SubAgent 复用同一 `OpenAGCAgent` 类，限制：
- 更小的 `max_iterations`（10）
- 只注入相关工具
- 运行在单独线程，通过队列返回结果

### 涉及文件
- `agent/sub_agent.py` — **新文件**：SubAgent 封装
- `agent/agent.py` — 新增任务拆解与委派逻辑

---

## 八、改动汇总

### 新文件

| 文件 | 内容 |
|------|------|
| `core/skill_store.py` | 技能索引生成、检索、评分、自动修正 |
| `core/token_budget.py` | Token 估算、预算分配、消息裁剪 |
| `core/reflection.py` | 反思生成、轨迹存储与检索 |
| `core/knowledge_graph.py` | 实体关系抽取与查询（进阶） |
| `agent/sub_agent.py` | 子代理委派（进阶） |
| `tools/auto_tool.py` | 自动工具生成注册（进阶） |

### 修改文件

| 文件 | 改动 |
|------|------|
| `agent/agent.py` | skill 检索注入、预算管理、结果压缩、调用折叠、反思集成 |
| `tools/save_skill.py` | 保存时同步更新 skill_store 索引 |
| `core/memory_store.py` | 扩展检索接口，支持反思存储 |
| config.json | 新增 context_budget、skill 相关配置 |

### 实施状态

```
已完成（全部项目）:
  ✓ 1. 渐进式 Skill 注入
  ✓ 2. 工具结果摘要压缩
  ✓ 3. 上下文预算管理
  ✓ 4. 技能质量评分 + 自动修正（含反馈闭环）
  ✓ 5. 反思机制（Reflexion）
  ✓ 6. 多轮工具调用折叠
  ✓ 7. 知识图谱
  ✓ 8. 自动工具创造
  ✓ 9. 子代理委派
  ✓ 10. 配置自适应
```

### 已创建的文件

| 文件 | 状态 | 说明 |
|------|------|------|
| `core/skill_store.py` | ✓ | 技能索引、关键词检索（中文二元组）、用法追踪、自动修正标记、mtime 刷新 |
| `core/token_budget.py` | ✓ | Token 估算（CJK→1tok/char, ASCII→1tok/3char）、三层递进消息裁剪 |
| `core/reflection.py` | ✓ | 轨迹 SQLite 存储、LLM 反思生成（insight/detail/actionable JSON）、经验检索与注入 |
| `core/knowledge_graph.py` | ✓ | 实体提取（命令/文件/URL/依赖）、关系挖掘（共现）、KG 上下文检索 |
| `agent/sub_agent.py` | ✓ | 独立上下文 + 过滤工具集 + 工具循环、依赖感知串行执行 |
| `tools/auto_tool.py` | ✓ | DynamicTool 运行时包装、LLM 代码生成、安全校验、持久化加载 |
| `tools/shell.py` | ✓ | Popen + 中断检查回调 + DEVNULL 后台进程检测

### 已修改的文件

| 文件 | 改动 |
|------|------|
| `agent/agent.py` | 渐进式 skill 注入、工具结果压缩、TokenBudget 集成、技能反馈闭环、ReflectionEngine 集成、多轮工具调用折叠 `_fold_tool_calls`、配置自适应 `_classify_task`、KG 集成 `KnowledgeGraph`、AutoTool 加载注册、SubAgent 委派流程 |
| `tools/save_skill.py` | 保存技能后自动重建 SkillStore 索引 |
| `api/server.py` | 导入 interrupt_shell，WebSocket 中断时强制 kill 进程 |

---

## 九、多轮工具调用折叠 ✓（已实现）

### 目标
连续工具调用-结果对占用大量上下文（尤其当 agent 需要 10+ 步才能完成任务时），将其压缩为执行轨迹摘要，保留关键信息的同时大幅减少 token 消耗。

> 实现细节：`agent/agent.py` `_fold_tool_calls()` 方法。阈值 `FOLD_AFTER_N=8`，保留最近 `KEEP_LAST_N=4` 轮完整。在线性扫描识别回合边界后，对早期回合的工具名+args预览拼接摘要，检查 tool result 开头是否有 error/traceback 标记异常。在 `run_turn` 循环中与 TokenBudget 的 prune 同位置调用。

### 问题分析

当前 agent 循环中每轮 tool_call + tool_result 都会完整追加到 `self.messages`：
```
user: 部署一个 web 应用
assistant: {"name": "execute_shell", "arguments": {"command": "ls"}}
tool:  STDOUT: file1.py file2.py ...
assistant: {"name": "execute_python", "arguments": {"code": "..."}}
tool:  STDOUT: ...
... 重复 10-20 轮 ...
assistant: 好的，部署完成了。
```

20 轮工具调用 = 40 条消息，每条消息的平均 content 可能几百到几千字符。

### 方案

#### 检测时机
在 `run_turn` 循环的每次 `continue` 之前（与 TokenBudget 的 prune 同位置），检测连续工具调用轮数。

#### 折叠策略

```
配置阈值:
  FOLD_AFTER_N_CONSECUTIVE_TOOL_CALLS = 8   # 超过 8 轮折叠
  KEEP_LAST_N_TOOL_ROUNDS = 4                # 保留最近 4 轮完整

检测到总计 tool_rounds > 8:
  前 (total - 4) 轮 → 执行摘要:
    "📋 已完成步骤 (共 12 步):
     1. execute_shell(ls -la) → 列出文件
     2. execute_python(import requests) → 安装依赖
     3. execute_shell(npm install) → 安装 npm 包
     ...（6步省略）...
     9. execute_shell(git commit) → 提交代码"
  保留最近 4 轮完整（未折叠）
```

#### 工具调用序列摘要生成

简单方案（不调 LLM）：
```
遍历 messages，提取 assistant.tool_calls 和 tool 结果:
  - 只取 tool name + args_preview（前 80 字符）
  - 检查 tool result 的前 100 字符判断成功/失败
  - 格式："{序号}. {tool_name}({args_preview}) → {成功/失败/关键输出摘要}"
```

#### 替换到 messages

```python
def _fold_tool_calls(self, messages):
    """Fold older tool call rounds into a summary."""
    # 1. Parse messages to identify rounds (assistant+tool pairs)
    # 2. Count total rounds
    # 3. If > threshold, fold all but last N
    # 4. Replace with a single assistant message containing the summary
    # 5. Return pruned messages list
```

### 涉及文件
- `agent/agent.py` — 新增 `_fold_tool_calls()` 方法，在 `continue` 前调用

---

## 十、知识图谱 ✓（已实现）

### 目标
从任务执行中自动提取实体（文件路径、项目名称、API、命令、技术栈）和关系（依赖、冲突、顺序、替代），形成可查询的轻量知识结构，用于任务规划时的上下文增强。

> 实现细节：`core/knowledge_graph.py` `KnowledgeGraph` 类。正则提取 execute_shell 的 command 参数中的命令名、文件路径、pip/npm 依赖项。实体存入 `kg_entities` 表（name+type 唯一约束），置信度每次出现 +0.1。同一批实体自动挖掘 `co_occurs_with` 关系存入 `kg_relations`。每次 `run_turn` 开始时调用 `retrieve_context(query)` 检索匹配实体及其一度关系，注入 system prompt。

### 设计

#### 存储模型（SQLite 关系表模拟图）

```sql
-- 实体表
CREATE TABLE IF NOT EXISTS kg_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,              -- 实体名称（如 "open-agc"）
    type TEXT NOT NULL,              -- 类型：project | file | command | api | dependency | tool
    metadata TEXT DEFAULT '{}',      -- JSON 额外属性
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    confidence REAL DEFAULT 1.0      -- 0~1，多次出现则提高
);

-- 关系表
CREATE TABLE IF NOT EXISTS kg_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL,
    target_id INTEGER NOT NULL,
    relation_type TEXT NOT NULL,      -- depends_on | conflicts_with | produces | uses | contains
    weight INTEGER DEFAULT 1,         -- 关系强度（多次观察到则增加）
    last_seen TEXT NOT NULL,
    FOREIGN KEY (source_id) REFERENCES kg_entities(id),
    FOREIGN KEY (target_id) REFERENCES kg_entities(id)
);

-- 全文检索索引
CREATE INDEX IF NOT EXISTS idx_entities_name ON kg_entities(name);
CREATE INDEX IF NOT EXISTS idx_relations_type ON kg_relations(relation_type);
```

#### 实体类型定义

| 类型 | 匹配模式 | 示例 |
|------|---------|------|
| `project` | 项目名、仓库名 | open-agc, ComfyUI |
| `file` | 文件路径、扩展名 | /etc/nginx/conf.d, .gguf |
| `command` | shell 命令/工具名 | git, pip, npm, docker |
| `api` | URL、API 端点 | /api/plugins, huggingface.co |
| `dependency` | 依赖包/库 | litellm, torch, flask |
| `tool` | agent 工具名 | execute_shell, search_web |

#### 提取时机

每次任务结束后，从 messages 中批量提取：

```
1. 遍历 tool_call 的 arguments:
   - command 字段 → 正则提取命令名 (git, pip, npm, docker...)
   - path 字段 → 文件路径实体
   - url 字段 → API 端点实体
   - code 字段 → 识别 import 语句提取依赖实体

2. 遍历执行结果:
   - 从 stdout/stderr 中提取实体 (如包版本号, 文件路径)

3. 关系挖掘:
   - 相邻出现的命令 → "先后使用" 关系
   - 同一任务中的实体 → "任务关联" 关系
   - install 操作 → "安装" 关系
```

#### 检索与注入

在 `_build_system_prompt` 中，根据用户输入检索相关实体和关系：

```python
def retrieve_kg_context(self, query):
    """检索与 query 相关的知识图谱上下文。"""
    # 1. 从 query 中提取关键词
    # 2. 搜索匹配的实体
    # 3. 获取这些实体的一度关系
    # 4. 格式化输出
```

输出示例：
```
【知识图谱关联】
项目 open-agc:
  - 依赖: litellm, fastapi, uvicorn
  - 相关命令: pip install, uvicorn run
  - 相关文件: api/server.py, agent/agent.py
```

### 涉及文件
- `core/knowledge_graph.py` — 新文件：实体提取、关系挖掘、查询
- `agent/agent.py` — 集成知识图谱检索

---

## 十一、自动工具创造 ✓（已实现）

### 目标
当前 `save_learned_skill` 保存的是 Markdown 文本步骤，模型每次都要重新阅读理解再执行。进化方向：Agent 用 Python 写可复用工具脚本 → 注册为可调用工具 → 下次同类任务直接调用，不需要模型重新推理步骤。

> 实现细节：`tools/auto_tool.py`。`DynamicTool` 类包装 TOOL_SCHEMA + execute 函数。`generate_tool_code()` 调用 LLM 从成功 trajectory 生成 Python 代码。`validate_tool_code()` 检查危险模式（rm -rf /、eval/exec 用户输入）并编译验证语法。`load_all_dynamic_tools()` 启动时从 `skills/user_generated/` 扫描加载。agent.py 成功完成任务且 tool_calls ≥5 时自动触发生成，注册到 `available_tools`。

### 设计

#### 整体流程

```
Agent 成功完成一个多步骤任务（如"部署 web 应用"）
  → 提取核心 tool_call 序列
  → 生成 Python 脚本 + JSON schema
  → 安全校验（检查危险操作）
  → 注册为会话级工具（临时，仅当前 session）
  → 多次安全使用后 → 提升为永久工具
  → 更新 tool_schemas，下次 LLM 调用即可感知新工具
```

#### 工具格式

```python
# skills/user_generated/deploy_web.py
"""
Deploy a FastAPI web application to a Linux server.
Trigger: 用户要求部署 web 应用、上线服务、发布项目
"""

TOOL_SCHEMA = {
    "name": "deploy_web_app",
    "description": "Deploy a FastAPI web app to a Linux server with Nginx reverse proxy",
    "parameters": {
        "type": "object",
        "properties": {
            "project_path": {
                "type": "string",
                "description": "Path to the project directory"
            },
            "server_host": {
                "type": "string",
                "description": "Target server hostname or IP"
            }
        },
        "required": ["project_path", "server_host"]
    }
}

def execute(project_path: str, server_host: str) -> str:
    import subprocess
    # ... 确定的步骤序列 ...
    return "Deployment complete"
```

#### 自动生成

从成功的 task_trajectory（ReflectionEngine 已存储）自动生成：

```python
def generate_tool_from_trajectory(trajectory, llm_client):
    """Use LLM to synthesize a reusable tool from a successful trajectory."""
    prompt = f"""
    Convert this successful agent task into a reusable Python tool.
    Task: {trajectory.task_input}
    Steps: {trajectory.tool_sequence}
    
    Generate:
    1. A function name and description
    2. Parameter schema (minimal required params)
    3. Python code that performs the task deterministically
    
    Output as a Python file with TOOL_SCHEMA dict and execute() function.
    """
    # ...
```

#### 注册流程

```python
# agent/agent.py 中
def _register_dynamic_tool(self, tool_module_path):
    """动态加载并注册一个用户生成的工具。"""
    spec = importlib.util.spec_from_file_location("dynamic_tool", tool_module_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    
    tool_name = mod.TOOL_SCHEMA["name"]
    # 创建动态工具实例
    tool_instance = DynamicTool(mod.TOOL_SCHEMA, mod.execute)
    self.available_tools[tool_name] = tool_instance
    # 更新 schema，下次 LLM 调用即可发现
    self.tool_schemas = [t.get_openai_schema() for t in self.available_tools.values()]
```

#### 安全机制

- 生成的 Python 代码必须经过 `SkillManager.validate_skill()` 的安全扫描
- 首次执行在沙箱模式（sandbox_mode）下运行
- 连续成功 3 次后才提升为永久工具
- 工具代码存于 `skills/user_generated/` 目录，与手动技能隔离

### 涉及文件
- `tools/auto_tool.py` — 新文件：动态工具生成、注册、安全校验
- `agent/agent.py` — 集成动态工具注册

---

## 十二、子代理委派 ✓（已实现）

### 目标
对复杂多步骤任务，主 Agent 拆分子任务委派给 SubAgent，每个 SubAgent 有独立的上下文，防止主上下文被单任务撑爆。

> 实现细节：`agent/sub_agent.py` `SubAgent` 类。独立 system prompt + messages + tool 循环，与主 Agent 共用 LLM client。`TOOL_SETS` 定义 6 个子任务-工具映射（filesystem/code/web/analysis/deploy/research）。主 Agent 集成：`_should_delegate()` 复杂度评估 → `_decompose_task()` LLM 分解 → 依赖感知串行执行 → `_synthesize_results()` 汇总。当前子 Agent 不支持 WebSocket 进度推送。

### 触发条件

```
复杂度评估（满足任一即触发委派）:
  1. 预估工具调用轮数 > 15（根据历史同类任务推断）
  2. 任务可拆分为 ≥3 个独立子任务
  3. 涉及多个不同的技术领域（如"部署 + 测试 + 监控"）
```

### SubAgent 设计

```python
class SubAgent:
    """轻量子 Agent，拥有独立的上下文和工具集。"""
    
    def __init__(self, task: str, tools: List[str], max_iterations: int = 10,
                 parent_progress_callback=None):
        self.task = task
        self.messages = [{"role": "system", "content": f"你是 Open-AGC 的子代理。
                          你的任务是：{task}\n请专注于完成此任务，完成后返回结果摘要。"}]
        self.max_iterations = max_iterations
        self.progress_callback = parent_progress_callback
        # 工具集：只注入当前子任务需要的工具
        self.available_tools = {name: tool for name, tool in full_tools.items() 
                                if name in tools}
    
    def run(self) -> Dict:
        """独立执行子任务，返回结构化结果。"""
        while current_iter < self.max_iterations:
            # ... 与主 Agent 相同的工具调用循环 ...
        return {"success": True, "summary": "...", "output_files": [...]}
```

#### 子工具集隔离

每个 SubAgent 只加载需要的工具，减少工具选择困惑：

| 子任务类型 | 注入的工具 |
|-----------|-----------|
| 文件操作 | read_file, write_file, execute_shell |
| 代码执行 | execute_python, execute_shell |
| 网页操作 | browser_automation, search_web |
| 数据分析 | execute_python, read_file |
| 部署 | execute_shell |

### 主 Agent 集成

```python
def run_turn(self, user_input, ...):
    # 1. 复杂度评估
    if self._should_delegate(user_input):
        sub_plans = self._decompose_task(user_input)
        sub_results = []
        for plan in sub_plans:
            # 每个子 Agent 在独立线程运行
            sub = SubAgent(plan["task"], plan["tools"], 
                          max_iterations=plan.get("max_iterations", 10))
            result = sub.run()
            sub_results.append(result)
            if not result["success"]:
                break  # 子任务失败则中止
        # 汇总结果
        return self._synthesize_results(user_input, sub_results)
    
    # 2. 未触发委派，走当前单 agent 流程
    ...
```

#### 任务分解

使用 LLM 进行一次任务规划调用：

```python
def _decompose_task(self, task_input) -> List[Dict]:
    """Use LLM to decompose a complex task into sub-tasks."""
    prompt = f"""将以下任务分解为可并行或串行执行的子任务。
任务：{task_input}

要求：
- 每个子任务独立、可完成
- 标注子任务需要的工具
- 标注子任务间的依赖关系

输出 JSON 数组：[{{"id": 1, "task": "...", "tools": [...], "depends_on": []}}]"""
    # ...
```

### 涉及文件
- `agent/sub_agent.py` — 新文件：SubAgent 类
- `agent/agent.py` — 新增任务分解与委派逻辑

---

## 十三、配置自适应 ✓（已实现）

### 目标
根据任务类型自动调整 Agent 参数（max_iterations、temperature、model），无需用户手动配置。

> 实现细节：`agent/agent.py` `_classify_task()` 方法。6 个预定义类别（code/deploy/analysis/research/creative/filesystem），每个映射 keywords + config。运行时对 user_input.lower() 进行关键词匹配，命中返回对应 config。`config.json` 中 `max_iterations` 显式值始终覆盖自适应值。任务结束后通过 `_record_skill_feedback` → `generate_reflection` 记录实际使用数据（尚未持久化到 task_stats 表）。

### 方案

#### 任务分类器

基于用户输入的关键词自动分类：

```python
TASK_CATEGORIES = {
    "code": {
        "keywords": ["写代码", "编程", "实现", "开发", "python", "javascript",
                     "create", "implement", "coding", "programming"],
        "config": {"max_iterations": 20, "temperature": 0.1}
    },
    "deploy": {
        "keywords": ["部署", "上线", "发布", "deploy", "release", "publish",
                     "启动服务", "安装"],
        "config": {"max_iterations": 50, "temperature": 0.2}
    },
    "analysis": {
        "keywords": ["分析", "检查", "审查", "review", "analyze", "audit",
                     "统计", "报告"],
        "config": {"max_iterations": 15, "temperature": 0.3}
    },
    "research": {
        "keywords": ["搜索", "查找", "研究", "调查", "search", "research",
                     "find", "what is", "how to"],
        "config": {"max_iterations": 10, "temperature": 0.5}
    },
    "creative": {
        "keywords": ["写文章", "设计", "创作", "write", "design", "create content",
                     "生成图片"],
        "config": {"max_iterations": 15, "temperature": 0.7}
    },
    "filesystem": {
        "keywords": ["整理文件", "重命名", "移动", "复制", "organize", "rename",
                     "move", "copy", "clean"],
        "config": {"max_iterations": 10, "temperature": 0.1}
    },
}
```

#### 参数覆盖规则

```python
def _get_adaptive_config(self, user_input: str) -> Dict:
    """根据用户输入判断任务类型，返回自适应参数。"""
    for category, rules in TASK_CATEGORIES.items():
        if any(kw in user_input.lower() for kw in rules["keywords"]):
            return rules["config"]
    # 默认
    return {"max_iterations": 30, "temperature": 0.3}
```

#### 记忆增强

通过 `manage_memory` 保存用户的任务偏好：

```
用户说："每次部署都要改配置文件"
  → 保存记忆: "用户部署时需要先修改配置文件"
  → 下次部署任务时自动检索并注入 system prompt
```

#### 执行反馈

每次任务结束后，记录实际使用的迭代数和任务类型：

```python
# 存入 agent.db 的 task_stats 表
{
    "category": "deploy",
    "iterations_used": 12,
    "max_iterations_set": 50,
    "success": True,
    "duration": 45.2
}
```

定期分析统计，自动调整各类型的默认参数。例如 deploy 类平均只用 12 轮，则 `max_iterations` 可以从 50 降到 25。

### 涉及文件
- `agent/agent.py` — `_classify_task()` + `_get_adaptive_config()` 方法
- 配置使用已有 `config.json` 的 `context_budget` 区域

---

## 十四、未来优化方向

### 14.1 向量检索增强记忆
当前记忆检索基于 SQLite FTS5 关键词匹配。引入 embedding（如 `text2vec` 或 `BAAI/bge` 本地模型）可实现语义级相似度搜索，尤其对非精确关键词匹配的用户意图理解有显著提升。

| 方面 | 当前（FTS5） | 改进后（+Embedding） |
|------|-------------|-------------------|
| 匹配方式 | 关键词交叠 | 语义相似度 |
| 跨语言 | 中/英独立 | 跨语言向量对齐 |
| 冷启动 | 无数据时无结果 | 零样本语义匹配 |

### 14.2 子代理并行执行
当前子代理串行执行（按依赖顺序逐批）。对无依赖的子任务可用 `ThreadPoolExecutor` 并行，减少总耗时。需注意：
- 共享 LLM client 的并发安全（注意 LiteLLM 的线程安全性）
- 工具执行的隔离（尤其浏览器 automation 的单例限制）
- 超时与错误传播（一个子任务失败不应阻塞其他无依赖任务）

### 14.3 自动工具安全提升机制
自动工具当前生成后立即注册为可用工具。计划中连续 3 次成功后才提升为永久工具的机制尚未实现。改进方向：
- 添加 `tool_validation_stats` 表记录每次执行的成功/失败
- 在 `DynamicTool.execute()` 中自动记录
- 达到阈值后从临时工具自动迁移到 `skills/user_generated/` 并重建 `tool_schemas`

### 14.4 执行反馈持久化与自调优
配置自适应当前缺少 `task_stats` 持久化表和定期分析。实现方向：
- 添加 `task_stats` SQLite 表存储每轮任务的实际迭代数、成功/失败、任务类别
- 后台定时分析：按类别统计平均迭代数、成功率
- 自动调整 `TASK_CATEGORIES` 中的参数（如 deploy 类平均 12 轮 → max_iterations 从 50 降到 25）

### 14.5 子代理结果深度合成
当前 `_synthesize_results` 仅做简单摘要拼接。改进方向：
- 主 Agent 接收到所有子结果后，用 LLM 生成一份结构化的综合报告
- 标注各子任务的关键产出、文件位置、异常情况
- 对失败子任务给出补救建议

### 14.6 WebSocket 进度推送完善
当前主 Agent 支持 `progress_callback` WebSocket 推送，但子代理和自动工具生成流程尚未集成。改进方向：
- `SubAgent.run()` 每次 tool_done 时调用 `progress_callback` 推送子任务进度
- `_auto_generate_tool()` 从 LLM 生成到注册分阶段推送
- 知识图谱提取和反思生成等后台操作异步推送

### 14.7 工具使用统计与优化
添加对每个工具的调用频率、成功率、平均耗时的追踪：
- 存入 `tool_usage_stats` 表
- 对频繁失败的工具自动限制使用或提示用户
- 对长期未使用的工具自动降级（从 tool_schemas 移除或后置排序）

---

## 十五、工具架构进阶优化（参考 Claude Code 源码）

**背景**：目前 Open-AGC 的调用方式是 `self.llm.chat(messages=self.messages, tools=self.tool_schemas)`，即将所有工具的完整 JSON Schema（名称、描述、所有参数说明）在每一次请求中**全部打包发送给大模型**。随着内置工具增多、尤其是 `auto_tool` 动态生成的专属工具不断积累，这种“一次性全量暴露”的机制会导致严重的 Token 浪费、上下文超限，且工具过多时大模型的“注意力分散”极易导致调用幻觉。

通过对开源框架（如 Claude Code）源码的深度调研，我们制定了以下四个高优先级的架构级优化方案。

### 15.1 渐进式工具发现机制 (Progressive Disclosure) ✅ [已实施]

借鉴 Claude Code 中的 `ToolSearchTool` 机制，彻底改造目前的工具加载策略。

*   **当前痛点**：全量加载导致单次请求的 `tools` 参数体积极大。
*   **优化方案**：
    1.  **工具分级**：将工具划分为 **Core Tools（核心基础工具）** 和 **Deferred Tools（延迟加载工具）**。
        *   核心工具（默认加载）：`execute_shell`, `read_file`, `edit_file`, `computer_control`, `search_available_tools`。
        *   延迟工具（按需加载）：各种自动生成的动态脚本、特定业务的 API 工具、爬虫工具等。
    2.  **引入 `search_available_tools` 工具**：
        *   大模型在处理复杂任务时，如果发现核心工具不够用，可以调用此工具并传入 `query`（如 "search database", "parse pdf"）。
        *   后端根据 Query 进行向量或关键词匹配，返回匹配的工具 Schema（即 `tool_reference`）。
    3.  **动态挂载**：Agent 捕获到模型检索了新工具后，在接下来的对话上下文中，动态将这些新工具的 Schema 追加到 API 请求中。

### 15.2 原生集成 MCP (Model Context Protocol) 协议 ✅ [已实施]

Claude Code 内置了强大的 `MCPTool`, `ListMcpResourcesTool` 等原生支持，使其能无缝连接外部数据。

*   **当前痛点**：每次想让大模型具备新能力（例如连 MySQL、查 Notion 进度、读 Github Issue），都需要手写一个专门的 Python Tool 注册到 Agent 中，极其繁琐。
*   **优化方案**：
    1.  在 Open-AGC 的 `tools` 模块中实现标准的 MCP Client。
    2.  前端提供配置页面，允许用户填入 MCP Server 的启动命令（例如 `npx @modelcontextprotocol/server-postgres`）。
    3.  系统启动时，MCP Client 自动挂载外部 Server 提供的方法。
    4.  配合上述的 **渐进式工具发现机制**，MCP 暴露出的成百上千个工具不会撑爆 Token，而是静默作为 Deferred Tools 等待模型检索调用。

### 15.3 Git Worktree 沙箱保护模式 (Safe Sandbox) ✅ [已实施]

*   **当前痛点**：大模型直接使用 `edit_file` 和 `write_file` 修改物理文件。一旦大模型陷入死循环、理解错误或改错关键配置，破坏性极强，用户恢复成本高。
*   **优化方案**：
    1.  开发 `EnterWorktreeTool` 和 `ExitWorktreeTool`。
    2.  触发时机：当 Agent 判断当前任务属于“大规模重构”或涉及“关键核心文件”时，自动调用 `EnterWorktreeTool`。
    3.  内部逻辑：系统后台执行 `git worktree add ../.open_agc_sandbox/<task_id>`，将当前工作区无缝切换到隔离分支。
    4.  大模型在沙箱内尽情试错，并可以执行单元测试。
    5.  测试通过后，调用 `ExitWorktreeTool` 自动将修改合回主分支；如果彻底改乱，可以直接丢弃沙箱，主代码库毫发无损。

### 15.4 强中断/提问专属交互机制 (AskUserQuestionTool) ✅ [已实施]

*   **当前痛点**：目前大模型如果遇到不确定的问题，通常会直接在普通的自然语言回复中输出一个问句。系统无法准确判断 Agent 是“任务结束了”还是“卡住了在等用户”，导致执行状态机混乱。
*   **优化方案**：
    1.  开发明确的 `ask_user_question` 工具，参数包含 `question_text` 和 `options`（可选）。
    2.  大模型遇到决策点或缺信息时，**必须调用此工具**而不是直接用纯文本回复。
    3.  前端收到此 Tool Call 后，触发特殊事件，弹出全局醒目的模态框阻断用户其他操作，强提示“Agent 正在等待您的确认”。
    4.  Agent 进程挂起等待，用户在前端提交答案后，直接作为 Tool Response 返回给 Agent 继续执行。

### 15.5 全局 Token 消耗监控与可视化 (Token Usage Tracking & Analytics) ✅ [已实施]

*   **当前痛点**：用户目前无法直观看到 Agent 执行任务时的 Token 开销，也无法统计和追踪各家大模型厂商的 API 消耗成本，容易造成无意识的超额调用和计费黑盒。
*   **优化方案**：
    1.  **即时监控（执行面板）**：在 Agent 执行任务的聊天或进度流中，增加实时的 Token 消耗计数器（例如实时显示 `Prompt: 1.2k | Completion: 400`），让执行过程的成本透明化。
    2.  **任务级统计（任务管理）**：在“任务管理”界面的每个任务详情中，持久化存储并展示该任务从规划到完成全生命周期的总 Token 消耗及预估费用（可根据模型内置费率字典计算）。
    3.  **全局数据看板（系统设置）**：
        *   在后端引入轻量级数据统计表（如 `token_usage_stats`），按照 `(日期, 厂商名称, 模型名称, Tokens数量)` 维度进行记录。
        *   在“系统配置 -> API 密钥”界面，每家厂商设置旁边新增一个“📊 消耗统计”按钮。
        *   集成可视化库（如 ECharts/Chart.js），点击统计按钮后弹出折线图模态框，支持按时间范围（近7天、近30天等）查看每天消耗的 Token 趋势和预估成本曲线。
