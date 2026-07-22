# -*- coding: utf-8 -*-
"""阶段5 Task4: edit_file 增强 + apply_patch 实测验证。

内容：
1. litellm 接受性：带全量 active tool_schemas（含 apply_patch）发一次真实请求（kimi_code/k3）
2. 实测多文件编辑任务：让 agent 用 apply_patch 一次调用修改两个文件，核对文件内容
3. 实测 edit_file 冲突反馈：让 agent 对有多处匹配的文件做修改，观察其利用行号错误信息消歧
4. active 载荷字节统计（16KB 预算核对）

用法: python scratch/verify_s5t4.py
结果写 scratch/verify_s5t4_result.json
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

FILE_A = os.path.abspath("workspace/s5t4_alpha.py")
FILE_B = os.path.abspath("workspace/s5t4_beta.py")
FILE_C = os.path.abspath("workspace/s5t4_gamma.py")

A_BEFORE = '"""模块 alpha。"""\n\nMARK_ALPHA = "old_alpha"\n\n\ndef alpha():\n    return MARK_ALPHA\n'
B_BEFORE = '"""模块 beta。"""\n\nMARK_BETA = "old_beta"\n\n\ndef beta():\n    return MARK_BETA\n'
C_BEFORE = "# 配置\n阈值 = 10\n# 分隔\n阈值 = 10\n# 结束\n"


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def read(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def make_agent(model):
    from agent.agent import OpenAGCAgent
    agent = OpenAGCAgent(model=model)
    agent._user_input_timeout = 5.0
    return agent


def run_task(agent, label, user_input, expect_tools, timeout_s=300):
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
    tool_results = {e["tool"]: (e.get("result_preview") or "")[:400]
                    for e in events if e["event"] == "tool_done"}
    hit = [t for t in expect_tools if t in called]
    result = {
        "label": label,
        "duration_s": round(dur, 1),
        "error": error,
        "tools_called": called,
        "tool_results": tool_results,
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

    write(FILE_A, A_BEFORE)
    write(FILE_B, B_BEFORE)
    write(FILE_C, C_BEFORE)

    cfg = json.load(open("data/config.json", encoding="utf-8"))
    fallback = cfg.get("default_model", "deepseek/deepseek-v4-flash")
    report["fallback_model"] = fallback

    model_used = MODEL
    try:
        agent = make_agent(MODEL)
        n_schemas = len(agent.tool_schemas)
        payload_bytes = len(json.dumps(agent.tool_schemas, ensure_ascii=False).encode("utf-8"))
        names = [s["function"]["name"] for s in agent.tool_schemas]
        resp, actual = agent.llm.chat(
            messages=[{"role": "user", "content": "回复 ok 两个字母即可，不要调用任何工具。"}],
            model=MODEL,
            tools=agent.tool_schemas,
        )
        report["litellm_accept"] = {
            "ok": True, "model": actual, "schemas": n_schemas,
            "payload_bytes": payload_bytes,
            "apply_patch_resident": "apply_patch" in names,
            "reply": (resp.choices[0].message.content or "")[:50],
        }
        print(f"[1] litellm 接受: OK schemas={n_schemas} payload={payload_bytes}B "
              f"apply_patch_resident={'apply_patch' in names}")
    except Exception as e:
        print(f"[1] kimi_code 不可用: {e} -> 回退 {fallback}")
        report["litellm_accept"] = {"ok": False, "error": str(e), "fell_back": True}
        model_used = fallback
        agent = make_agent(model_used)
    report["model_used"] = model_used

    results = []

    # ── 任务1: apply_patch 一次调用修改两个文件 ──
    r1 = run_task(
        agent, "apply_patch 多文件编辑",
        f"请用 apply_patch 工具在一次调用中完成以下两处修改：\n"
        f"1. 文件 {FILE_A}：把 \"old_alpha\" 改成 \"new_alpha\"\n"
        f"2. 文件 {FILE_B}：把 \"old_beta\" 改成 \"new_beta\"\n"
        f"其他内容不要动。",
        ["apply_patch"])
    r1["file_a_ok"] = '"new_alpha"' in read(FILE_A) and '"old_alpha"' not in read(FILE_A)
    r1["file_b_ok"] = '"new_beta"' in read(FILE_B) and '"old_beta"' not in read(FILE_B)
    r1["ok"] = r1["ok"] and r1["file_a_ok"] and r1["file_b_ok"]
    print(f"    file_a_ok={r1['file_a_ok']} file_b_ok={r1['file_b_ok']}")
    results.append(r1)

    # ── 任务2: edit_file 多处匹配 → 错误给行号，模型应能消歧后成功 ──
    r2 = run_task(
        agent, "edit_file 冲突消歧",
        f"文件 {FILE_C} 中有两处 \"阈值 = 10\"（一处在注释\"# 分隔\"之前，一处在之后）。"
        f"请只把第二处（\"# 分隔\"之后的那个）\"阈值 = 10\" 改成 \"阈值 = 20\"，第一处保持不变。",
        ["edit_file"])
    c_after = read(FILE_C)
    r2["file_c_after"] = c_after
    r2["file_c_ok"] = c_after == "# 配置\n阈值 = 10\n# 分隔\n阈值 = 20\n# 结束\n"
    r2["ok"] = r2["ok"] and r2["file_c_ok"]
    print(f"    file_c_ok={r2['file_c_ok']}")
    results.append(r2)

    report["tasks"] = results
    report["all_ok"] = all(r["ok"] for r in results)

    with open("scratch/verify_s5t4_result.json", "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print(f"\n汇总: all_ok={report['all_ok']} model_used={model_used}")
    print("结果已写入 scratch/verify_s5t4_result.json")


if __name__ == "__main__":
    main()
