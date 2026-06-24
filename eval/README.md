# Agent 评估系统

用于测评 Open-AGC Agent 的核心能力变化，每次迭代后运行，检测回归。

## 快速开始

```bash
# 运行全部评估
python -m eval.runner

# 运行指定场景
python -m eval.runner --scenarios shell filesystem

# 生成历史对比报告
python -m eval.runner --report
```

## 场景定义

每个场景是一个 JSON 文件，放在 `eval/scenarios/` 下：

```json
{
  "name": "shell_list_files",
  "description": "Agent can list files in a directory",
  "prompt": "列出当前目录下的文件",
  "expected": {
    "tool_used": ["execute_shell"],
    "output_contains": ["agent.py", "main.py"],
    "max_steps": 3,
    "max_tokens": 5000
  },
  "timeout": 30,
  "tags": ["core", "shell"]
}
```

## 输出格式

每次运行结果保存在 `eval/results/` 下：

```
eval/results/
  run_20260624_153000.json      ← 单次运行结果
  history.json                   ← 历史汇总
```

每次运行记录：pass/fail、耗时、tokens、工具调用序列、错误信息。
