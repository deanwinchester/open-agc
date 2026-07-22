# -*- coding: utf-8 -*-
"""阶段6 Task2: fetch_url 工具 live 验证（kimi_code/k3）。

Part 1 (static): fetch_url 已注册进 full_available_tools / 分层常驻核心集 /
  tool_schemas；schema 含 url/max_chars/raw；SSRF 直接调用拦截（无网络）；
  真实抓取 example.com 冒烟（有网络、无 LLM）。
Part 2 (live, kimi_code/k3):
  (a) 抓取公开页面 https://example.com -> 期望调用 fetch_url 且答出 Example Domain
  (b) 抓取 http://127.0.0.1:11434/ -> 期望 fetch_url 返回 SSRF 拒绝错误

Run: python scratch/verify_s6t2.py [--skip-live]
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


# ---------------------------------------------------------------- Part 1
print("=== Part 1: static checks (no LLM) ===")
tmp_mem = tempfile.mkdtemp(prefix="s6t2_mem_")
mem_db = os.path.join(tmp_mem, "memory.db")

from agent.agent import OpenAGCAgent  # noqa: E402
from tools.fetch_url import FetchURLTool, fetch_url_text  # noqa: E402

agent = OpenAGCAgent(model="kimi_code/k3", session_id=990401, memory_db_path=mem_db)

check("fetch_url registered in full_available_tools",
      "fetch_url" in agent.full_available_tools)
check("fetch_url resident (tiered core set, where search_web is)",
      "fetch_url" in agent.active_tool_names and "search_web" in agent.active_tool_names,
      f"active={sorted(agent.active_tool_names)}")
check("fetch_url in tool_schemas",
      any(s["function"]["name"] == "fetch_url" for s in agent.tool_schemas))
check("fetch_url display name", agent.tool_display_names.get("fetch_url") == "抓取网页正文")

schema = agent.full_available_tools["fetch_url"].get_openai_schema()
props = schema["function"]["parameters"]["properties"]
check("fetch_url schema params (url/max_chars/raw, required=url)",
      set(props) == {"url", "max_chars", "raw"}
      and schema["function"]["parameters"]["required"] == ["url"],
      f"props={sorted(props)}")

search_web_desc = agent.full_available_tools["search_web"].description
check("search_web description points to fetch_url tool",
      "fetch_url 工具" in search_web_desc and "本工具的 fetch_url 参数" not in search_web_desc,
      f"desc={search_web_desc!r}")

# SSRF: blocked before any network I/O
out = FetchURLTool().execute(url="http://127.0.0.1:8080/v1/models")
check("SSRF: 127.0.0.1 blocked via execute()",
      out.startswith("Error") and "SSRF" in out, f"out={out[:100]!r}")
out = FetchURLTool().execute(url="http://192.168.1.1/admin")
check("SSRF: 192.168.x blocked via execute()",
      out.startswith("Error") and "SSRF" in out, f"out={out[:100]!r}")

# Real fetch smoke (network, no LLM)
try:
    out = fetch_url_text("https://example.com", max_chars=2000)
    check("direct fetch https://example.com (network smoke)",
          "Example Domain" in out and not out.startswith("Error"),
          f"out={out[:120]!r}")
except Exception as e:
    check("direct fetch https://example.com (network smoke)", False, f"exc={e}")

# ---------------------------------------------------------------- Part 2
if SKIP_LIVE:
    print("\n=== Part 2: SKIPPED (--skip-live) ===")
else:
    print("\n=== Part 2: live agent tests (kimi_code/k3) ===")
    live = OpenAGCAgent(model="kimi_code/k3", session_id=990402, memory_db_path=mem_db)
    calls = []
    fetch_results = []  # full_result strings from fetch_url tool_done events

    def cb(ev):
        if ev.get("event") == "tool_done":
            calls.append((ev.get("tool"), ev.get("success")))
            print(f"  tool_done: {ev.get('tool')} success={ev.get('success')}")
            if ev.get("tool") == "fetch_url":
                fetch_results.append(
                    (ev.get("full_result") or "") + (ev.get("result_preview") or ""))

    # (a) fetch a public page
    calls.clear()
    fetch_results.clear()
    try:
        reply = live.run_turn(
            "请用 fetch_url 工具抓取 https://example.com 的网页正文，"
            "告诉我这个页面的标题（h1）写的是什么。",
            verbose=True, progress_callback=cb)
    except Exception as e:
        reply = f"<exception: {e}>"
    called = [t for t, _ in calls]
    reply_s = str(reply)
    check("live (a): fetch_url called & Example Domain reported",
          "fetch_url" in called and "Example Domain" in reply_s,
          f"calls={called}; reply={reply_s[:150]!r}")
    check("live (a): tool result contained page content",
          any("Example Domain" in r for r in fetch_results),
          f"results={[r[:80] for r in fetch_results]}")

    # (b) SSRF surfaced through the agent
    calls.clear()
    fetch_results.clear()
    try:
        reply = live.run_turn(
            "用 fetch_url 工具抓取 http://127.0.0.1:11434/ 的内容。"
            "如果工具返回错误，请把错误信息原样告诉我，不要换别的工具重试。",
            verbose=True, progress_callback=cb)
    except Exception as e:
        reply = f"<exception: {e}>"
    called = [t for t, _ in calls]
    check("live (b): fetch_url SSRF rejection surfaced",
          "fetch_url" in called and any("SSRF" in r for r in fetch_results),
          f"calls={called}; results={[r[:100] for r in fetch_results]}")

# ---------------------------------------------------------------- summary
print("\n=== SUMMARY ===")
failed = [r for r in results if not r[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
