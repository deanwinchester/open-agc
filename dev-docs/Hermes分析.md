# Hermes 模型分析与借鉴

## 概述

Hermes 是由 NousResearch 开发的开源大语言模型系列，以其在**函数调用（Function Calling）**、**工具使用（Tool Use）** 和**多轮对话**方面的卓越表现而闻名。Hermes 系列覆盖从 14B 到 405B 的参数规模，是开源社区中 Agent 能力最强的模型之一。

## 核心特性

### 1. 函数调用能力

Hermes 最突出的特性是其原生的函数调用支持：

- **Json Mode / Functionary Mode**：Hermes 在训练阶段专门针对函数调用格式进行了微调，支持直接输出符合 OpenAI 函数调用规范的 JSON。
- **多工具并行调用**：支持在单次回复中调用多个工具（Parallel Tool Calls），这与 OpenAI 的 `tools` 接口兼容。
- **嵌套函数调用**：支持函数返回结果再传入另一函数的链式调用场景。
- **函数调用数据集**：NousResearch 发布了专门的函数调用数据集（Hermes Function Calling Dataset），用于训练模型的工具使用能力。

### 2. 系统提示遵循能力

- Hermes 在训练中强调对系统提示（System Prompt）的严格遵循，能够准确理解复杂的指令约束。
- 支持角色扮演、格式约束、输出格式限定等高级系统提示用法。

### 3. 长上下文支持

- Hermes 4 系列支持 128K+ tokens 的上下文窗口。
- 通过 YaRN （YaRN: Yet another RoPE extensioN）扩展技术实现位置编码的外推，使模型能够处理超长序列。

### 4. 多轮对话一致性

- 在多轮对话中保持对话历史和工具调用状态的一致性。
- 不会在后续轮次中"忘记"之前调用的工具及其结果。

## 模型架构

| 模型 | 参数量 | 基础模型 | 上下文长度 | 特点 |
|------|--------|----------|-----------|------|
| Hermes 4.3 | 36B | 混合专家（MoE） | 128K | 最新版本，MoE架构，高效推理 |
| Hermes 4 | 14B / 70B / 405B | Llama-3 | 128K | 全系列覆盖，405B为最强 |
| Hermes 3 | 8B / 70B / 405B | Llama-3.1 | 128K | 稳定版本，广泛部署 |

## 训练方法

### 1. 后训练（Post-Training）管线

Hermes 的训练管线包含多个阶段：

1. **基础预训练**：基于 Llama 或其他基础模型
2. **监督微调（SFT）**：使用高质量的指令数据、函数调用数据、对话数据进行微调
3. **直接偏好优化（DPO）**：通过偏好对齐提升输出质量和安全性

### 2. 函数调用数据合成

- NousResearch 使用 **数据合成管线** 自动生成大量的函数调用训练数据
- 通过将 API 文档、函数签名和自然语言指令配对，构造多样化的工具使用场景
- 数据覆盖：单工具调用、多工具并行、条件分支工具调用、错误恢复等

### 3. 评估体系

Hermes 使用多种基准评估工具使用能力：

- **BFCL（Berkeley Function Calling Leaderboard）**：函数调用准确率
- **API-Bank**：复杂 API 调用场景
- **Gorilla APIBench**：API 调用能力
- **MT-Bench**：多轮对话质量

## 对 Open-AGC 的借鉴意义

### 1. 函数调用数据构造

Hermes 的函数调用训练数据构造方法值得借鉴：

- 目前 Open-AGC 的 tool 调用依赖 LLM 的固有函数调用能力，未针对特定工具集做微调
- **借鉴思路**：可以基于 Open-AGC 的工具集（shell、filesystem、browser 等）构造函数调用训练数据，用于微调或 few-shot 示例

### 2. 并行工具调用

Hermes 支持并行工具调用，而 Open-AGC 当前是串行调用：

- 当多个工具调用无依赖关系时，可以并行执行
- 需要修改 agent loop 支持多 tool_call 的并发执行和结果聚合

### 3. 上下文管理

Hermes 的长上下文处理能力提示：

- Open-AGC 的上下文压缩策略（15K 截断）可以更精细化
- 参考 Hermes 的对话状态保持机制，优化多轮 tool 调用的上下文管理

### 4. System Prompt 结构化

Hermes 对系统提示的严格遵循能力值得学习：

- 当前 Open-AGC 使用单一的长系统提示，可以拆分为多个模块化的指令块
- 参考 Hermes 的格式约束方式，统一 tool 输出格式

### 5. 开源生态

Hermes 的开源策略带来的优势：

- 模型权重完全开源，可自托管部署
- 可通过 `llamacpp` 或 `sglang` 本地运行，与 Open-AGC 的本地模型架构兼容
- NousResearch 的 Hermes Function Calling Dataset 可用于改进 Open-AGC 的 tool 调用

## 总结

Hermes 代表了开源大模型在工具使用和函数调用领域的最高水平之一。其函数调用数据合成管线、系统提示遵循能力、以及多轮对话一致性是 Open-AGC 可以重点借鉴的方向。结合 Open-AGC 现有的本地模型部署能力（LlamaCppManager / SGLangManager），可以直接部署 Hermes 模型作为 agent 的底层 LLM，从而获得更好的工具使用表现。
