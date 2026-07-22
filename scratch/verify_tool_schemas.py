# -*- coding: utf-8 -*-
"""阶段5 Task1: 工具 schema 实测验证。

内容：
1. 结构等价校验（before/after：工具名、参数名、required、enum 完全一致）
2. litellm 接受性：用 LLMClient 带全量 active tool_schemas 发一次真实请求（kimi_code/k3）
3. 实测 3 个典型任务（读文件并总结 / 搜索代码并修改 / 运行命令），
   用 kimi_code/k3 跑 OpenAGCAgent，记录工具选择与调用事件
4. 尺寸统计（静态全量 + active 载荷）

用法: venv/Scripts/python.exe scratch/verify_tool_schemas.py
结果写 scratch/verify_tool_schemas_result.json
"""
import io
import json
import os
import sys
import time
import traceback

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

MODEL = "kimi_code/k3"
FALLBACK = None  # 从 config.default_model 读

SAMPLE = os.path.abspath("workspace/s5t1_sample.txt")
SAMPLE_CONTENT = (
    "Open-AGC 是一个本地智能体项目。\n"
    "它支持工具调用、记忆管理和沙箱执行。\n"
    "阶段五的任务是优化工具描述。\n"
    "标记词: BANANA_MARK_42\n"
)


def strip_descriptions(node):
    if isinstance(node, dict):
        return {k: strip_descriptions(v) for k, v in node.items() if k != "description"}
    if isinstance(node, list):
        return [strip_descriptions(x) for x in node]
    return node


def structural_check():
    with open("scratch/schemas_before.json", encoding="utf-8") as f:
        before = json.load(f)["schemas"]
    with open("scratch/schemas_after.json", encoding="utf-8") as f:
        after = json.load(f)["schemas"]
    problems = []
    if set(before) != set(after):
        problems.append(f"工具集合不一致: {set(before) ^ set(after)}")
    for name in sorted(set(before) & set(after)):
        jb = json.dumps(strip_descriptions(before[name]), ensure_ascii=False, sort_keys=True)
        ja = json.dumps(strip_descriptions(after[name]), ensure_ascii=False, sort_keys=True)
        if jb != ja:
            problems.append(f"{name}: 结构不一致")
    return problems


def size_stats():
    def sz(x):
        return len(json.dumps(x, ensure_ascii=False).encode("utf-8"))
    with open("scratch/schemas_before.json", encoding="utf-8") as f:
        b = json.load(f)
    with open("scratch/schemas_after.json", encoding="utf-8") as f:
        a = json.load(f)
    return {
        "static_before": sum(sz(v) for v in b["schemas"].values()),
        "static_after": sum(sz(v) for v in a["schemas"].values()),
        "active_before": b["active_total_bytes"],
        "active_after": a["active_total_bytes"],
    }


def make_agent(model):
    from agent.agent import OpenAGCAgent
    agent = OpenAGCAgent(model=model)
    agent._user_input_timeout = 5.0  # 实测中不允许阻塞提问
    return agent


def run_task(agent, label, user_input, expect_tools, timeout_s=240):
    """跑一个任务，收集 tool_start/tool_done 事件。"""
    events = []

    def cb(evt):
        if evt.get("event") in ("tool_start", "tool_done", "ask_user"):
            events.append(evt)

    t0 = time.time()
    error = None
    reply = ""
    try:
        reply = agent.run_turn(user_input, progress_callback=cb)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        traceback.print_exc()
    dur = time.time() - t0

    called = [e["tool"] for e in events if e["event"] == "tool_start"]
    done = {e["tool"]: e.get("success") for e in events if e["event"] == "tool_done"}
    hit = [t for t in expect_tools if t in called]
    result = {
        "label": label,
        "duration_s": round(dur, 1),
        "error": error,
        "tools_called": called,
        "tools_done_success": done,
        "expected_tools_hit": hit,
        "expected_tools_missing": [t for t in expect_tools if t not in called],
        "asked_user": any(e["event"] == "ask_user" for e in events),
        "reply_preview": (reply or "")[:300],
        "ok": bool(hit) and error is None,
    }
    print(f"[{label}] {dur:.0f}s tools={called} ok={result['ok']}")
    return result


def main():
    report = {"model": MODEL, "started": time.strftime("%Y-%m-%d %H:%M:%S")}

    # ── 1. 结构等价 ──
    problems = structural_check()
    report["structural_problems"] = problems
    print(f"[1] 结构等价: {'OK' if not problems else problems}")

    # ── 尺寸 ──
    report["sizes"] = size_stats()
    print(f"[尺寸] {report['sizes']}")

    # ── 准备样例文件 ──
    os.makedirs(os.path.dirname(SAMPLE), exist_ok=True)
    with open(SAMPLE, "w", encoding="utf-8") as f:
        f.write(SAMPLE_CONTENT)

    cfg = json.load(open("data/config.json", encoding="utf-8"))
    fallback = cfg.get("default_model", "deepseek/deepseek-v4-flash")
    report["fallback_model"] = fallback

    agent = None
    model_used = MODEL
    try:
        agent = make_agent(MODEL)
        # ── 2. litellm 接受性：带全量 active schemas 发一个最小请求 ──
        n_schemas = len(agent.tool_schemas)
        payload_bytes = len(json.dumps(agent.tool_schemas, ensure_ascii=False).encode("utf-8"))
        resp, actual = agent.llm.chat(
            messages=[{"role": "user", "content": "回复 ok 两个字母即可，不要调用任何工具。"}],
            model=MODEL,
            tools=agent.tool_schemas,
        )
        txt = resp.choices[0].message.content or ""
        report["litellm_accept"] = {
            "ok": True, "model": actual, "schemas": n_schemas,
            "payload_bytes": payload_bytes, "reply": txt[:50],
        }
        print(f"[2] litellm 接受: OK model={actual} schemas={n_schemas} payload={payload_bytes}B")
    except Exception as e:
        print(f"[2] kimi_code 不可用: {e} -> 回退 {fallback}")
        report["litellm_accept"] = {"ok": False, "error": str(e), "fell_back": True}
        model_used = fallback
        agent = make_agent(fallback)
    report["model_used"] = model_used

    # ── 3. 三个典型任务（同一 agent 顺序执行，模拟真实会话） ──
    tasks = [
        ("读文件并总结",
         f"请读取文件 {SAMPLE} 并用一句话总结其内容。",
         ["read_file"]),
        ("搜索代码并修改",
         f"在文件 {SAMPLE} 中搜索 BANANA_MARK_42 这个标记词，把它所在行的 BANANA_MARK_42 改成 APPLE_MARK_42，其他内容不动。",
         ["search_file_content", "edit_file"]),
        ("运行命令",
         "请运行 shell 命令: echo S5T1_SHELL_OK，然后告诉我输出。",
         ["execute_shell"]),
    ]
    results = []
    for label, prompt, expect in tasks:
        results.append(run_task(agent, label, prompt, expect))
    report["tasks"] = results
    report["all_ok"] = all(r["ok"] for r in results) and not problems

    with open("scratch/verify_tool_schemas_result.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"\n汇总: all_ok={report['all_ok']} model_used={model_used}")
    print("结果已写入 scratch/verify_tool_schemas_result.json")


if __name__ == "__main__":
    main()
