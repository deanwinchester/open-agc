# -*- coding: utf-8 -*-
"""S6 Task4 实测：kimi_code/k3 通过 dispatch_subagent 拆分两子任务。

链路：真实 Agent（k3）→ search_available_tools 惰性启用 dispatch_subagent →
k3 两次调用 dispatch_subagent → 每次构造 SubAgent（复用父 agent 的 llm/tools
通道）→ 子代理在 workspace 写文件 → 结构化结果（success/summary）返回主循环。

另验证 _should_delegate 怪癖修复：提到 read_file 不再触发自动委派；
且本脚本的实测 prompt 本身不触发自动委派（否则主循环被委派路径截胡）。

运行：python scratch/verify_s6t4.py
"""
import json
import os
import sys

# Windows 控制台默认 GBK，强制 UTF-8 输出
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

FILE_A = os.path.join(PROJECT_ROOT, "workspace", "s6t4_a.txt")
FILE_B = os.path.join(PROJECT_ROOT, "workspace", "s6t4_b.txt")
CONTENT_A = "S6T4-A-OK"
CONTENT_B = "S6T4-B-OK"


def main():
    from agent.agent import OpenAGCAgent

    for p in (FILE_A, FILE_B):
        if os.path.exists(p):
            os.remove(p)

    agent = OpenAGCAgent(model="kimi_code/k3")

    # 1) 注册检查：惰性集（在 full_available_tools，不在常驻 core）
    assert "dispatch_subagent" in agent.full_available_tools, "dispatch_subagent 未注册"
    assert "dispatch_subagent" not in agent.active_tool_names, "dispatch_subagent 不应常驻 core"
    print("[check] dispatch_subagent 已注册（惰性集，未常驻 core）[OK]")

    # 2) _should_delegate 怪癖修复检查
    assert agent._should_delegate("用 read_file 读一下 test/case.txt") is False
    assert agent._should_delegate("部署并监控这个服务") is True
    print("[check] _should_delegate：read_file 字面量不委派 / 部署+监控委派 [OK]")

    # 3) 惰性发现：通过 search_available_tools 检索启用（真实惰性路径）
    discovery = agent.full_available_tools["search_available_tools"]
    out = discovery.execute(query="分派子代理 dispatch_subagent")
    print(f"[check] discovery output:\n{out}")
    assert "dispatch_subagent" in agent.active_tool_names, "discovery 未启用 dispatch_subagent"
    print("[check] search_available_tools 已惰性启用 dispatch_subagent [OK]")

    # 4) 实测：让 k3 用 dispatch_subagent 拆两个独立子任务
    #    注意措辞避开 _should_delegate 复杂度关键词（分别/同时/多个/所有/先.*再 等），
    #    否则会走自动委派路径而非显式 dispatch。
    prompt = (
        "这个工作包含两个相互独立的子任务。请调用 dispatch_subagent 工具两次，"
        "每次分派一个子代理完成一个子任务（你自己不要直接写文件）：\n"
        f"子任务一：在 workspace 目录下创建文件 s6t4_a.txt，内容为一行 {CONTENT_A}\n"
        f"子任务二：在 workspace 目录下创建文件 s6t4_b.txt，内容为一行 {CONTENT_B}\n"
        "两个子代理都返回之后，汇报它们各自的执行结果。"
    )
    assert agent._should_delegate(prompt) is False, "实测 prompt 意外触发自动委派"
    print(f"[run] prompt: {prompt}")
    reply = agent.run_turn(prompt, verbose=True)
    print(f"\n[reply]\n{reply}\n")

    # 5) dispatch_subagent 确实被调用了两次
    dispatch_msgs = [
        m for m in agent.messages
        if m["role"] == "tool" and m.get("name") == "dispatch_subagent"
    ]
    assert len(dispatch_msgs) >= 2, \
        f"dispatch_subagent 调用次数不足: {len(dispatch_msgs)}"
    for m in dispatch_msgs:
        payload = json.loads(m["content"])
        assert payload["success"] is True, f"子代理执行失败: {payload}"
        assert payload.get("summary"), f"子代理缺少 summary: {payload}"
    print(f"[check] dispatch_subagent 被调用 {len(dispatch_msgs)} 次，"
          f"结构化结果均 success [OK]")

    # 6) 子代理真实产出文件
    for path, expected in ((FILE_A, CONTENT_A), (FILE_B, CONTENT_B)):
        assert os.path.exists(path), f"子任务产物缺失: {path}"
        content = open(path, encoding="utf-8").read()
        assert expected in content, f"{path} 内容异常: {content[:80]}"
        print(f"[check] {os.path.basename(path)} 内容正确 [OK]")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
