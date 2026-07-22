# -*- coding: utf-8 -*-
"""阶段5 Task3: 检索强化 live 验证（kimi_code/k3）。

Part 1 (static): list_dir 已注册进 full_available_tools / 常驻核心集 /
  tool_schemas；search_file_content schema 含三个新参数。
Part 2 (live):
  (a) 大文件分页任务: workspace/s5t3_big.txt (600 行, marker 在第 487 行)
      -> 期望 agent 用 read_file offset/limit 分页定位并答出 marker 内容
  (b) 目录列举任务: workspace/s5t3_dir -> 期望 agent 调用 list_dir 并说出其中文件

Run: python scratch/verify_s5t3.py [--skip-live]
"""
import json
import os
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# GBK 控制台安全（模型回复可能含非 GBK 字符）
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

SKIP_LIVE = "--skip-live" in sys.argv
results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


# ---------------------------------------------------------------- fixtures
WS = os.path.join(PROJECT_ROOT, "workspace")
os.makedirs(WS, exist_ok=True)

big_path = os.path.join(WS, "s5t3_big.txt")
MARKER = "S5T3_MARKER_LINE_487_CONTENT"
with open(big_path, "w", encoding="utf-8") as f:
    for i in range(1, 601):
        f.write(f"{MARKER}\n" if i == 487 else f"ordinary log line {i}\n")

dir_path = os.path.join(WS, "s5t3_dir")
os.makedirs(os.path.join(dir_path, "inner"), exist_ok=True)
with open(os.path.join(dir_path, "alpha_report.txt"), "w", encoding="utf-8") as f:
    f.write("alpha\n")
with open(os.path.join(dir_path, "beta_notes.md"), "w", encoding="utf-8") as f:
    f.write("beta\n")
with open(os.path.join(dir_path, "inner", "gamma_data.csv"), "w", encoding="utf-8") as f:
    f.write("g1,g2\n")

# ---------------------------------------------------------------- Part 1
print("=== Part 1: static checks (no LLM) ===")
tmp_mem = tempfile.mkdtemp(prefix="s5t3_mem_")
mem_db = os.path.join(tmp_mem, "memory.db")

from agent.agent import OpenAGCAgent  # noqa: E402

agent = OpenAGCAgent(model="kimi_code/k3", session_id=990301, memory_db_path=mem_db)

check("list_dir registered in full_available_tools",
      "list_dir" in agent.full_available_tools)
check("list_dir resident (active core set, where find_files is)",
      "list_dir" in agent.active_tool_names and "find_files" in agent.active_tool_names,
      f"active={sorted(agent.active_tool_names)}")
check("list_dir in tool_schemas",
      any(s["function"]["name"] == "list_dir" for s in agent.tool_schemas))
check("list_dir display name", agent.tool_display_names.get("list_dir") == "列出目录结构")

grep_schema = agent.full_available_tools["search_file_content"].get_openai_schema()
props = grep_schema["function"]["parameters"]["properties"]
check("search_file_content schema has new params",
      {"context_lines", "output_mode", "head_limit"} <= set(props),
      f"props={sorted(props)}")

# ---------------------------------------------------------------- Part 2
if SKIP_LIVE:
    print("\n=== Part 2: SKIPPED (--skip-live) ===")
else:
    print("\n=== Part 2: live agent tests (kimi_code/k3) ===")
    live = OpenAGCAgent(model="kimi_code/k3", session_id=990302, memory_db_path=mem_db)
    calls = []
    tool_args_log = []  # (tool, args_dict)

    def cb(ev):
        if ev.get("event") == "tool_done":
            calls.append((ev.get("tool"), ev.get("success")))
            print(f"  tool_done: {ev.get('tool')} success={ev.get('success')}")
        elif ev.get("event") == "tool_start":
            try:
                tool_args_log.append((ev.get("tool"), json.loads(ev.get("tool_args") or "{}")))
            except Exception:
                pass

    # (a) big file paging: single direct page read via offset/limit.
    # NOTE: prompt must not mention tool names nor complexity keywords —
    # _should_delegate treats "read_file" as 5 TOOL_SETS area hits and would
    # hijack the turn into sub-agent delegation.
    calls.clear()
    tool_args_log.clear()
    try:
        reply = live.run_turn(
            "workspace/s5t3_big.txt 是一个 600 行的日志文件，文件太大不要一次读完。"
            "请用带行号、支持起始行和行数分页的文件读取方式，只取第 480 到 495 行这一页，"
            "看看里面有没有 S5T3_MARKER 标记，有的话把那一行的完整内容和行号告诉我。",
            verbose=True, progress_callback=cb)
    except Exception as e:
        reply = f"<exception: {e}>"
    called = [t for t, _ in calls]
    paged = any(t == "read_file" and ("offset" in a or "limit" in a)
                for t, a in tool_args_log)
    check("live (a): read_file paged & marker found",
          "read_file" in called and paged and MARKER in str(reply) and "487" in str(reply),
          f"calls={called}; paged_args={[(t, a) for t, a in tool_args_log if t == 'read_file']}; "
          f"reply={str(reply)[:150]!r}")

    # (b) list directory
    calls.clear()
    try:
        reply = live.run_turn(
            "用 list_dir 工具查看 workspace/s5t3_dir 目录（递归到第 2 层），"
            "告诉我里面有哪些文件。",
            verbose=True, progress_callback=cb)
    except Exception as e:
        reply = f"<exception: {e}>"
    called = [t for t, _ in calls]
    reply_s = str(reply)
    check("live (b): list_dir called & files reported",
          "list_dir" in called and "alpha_report" in reply_s and "beta_notes" in reply_s,
          f"calls={called}; reply={reply_s[:150]!r}")

# ---------------------------------------------------------------- summary
print("\n=== SUMMARY ===")
failed = [r for r in results if not r[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
