# -*- coding: utf-8 -*-
"""阶段5 Task2: tiered tool exposure verification.

Part 1 (static, no LLM):
  - AFTER: first-turn tool_schemas bytes with tool_tiered_exposure=true (default)
  - BEFORE: reconstructed full-resident bytes (legacy 20-tool core + adaptive)
  - flag wiring: subprocess with OPEN_AGC_DATA_DIR=tmp and
    config.json {"tool_tiered_exposure": false} -> full residency restored
  - lazy injection: a non-resident tool is discovered+enabled via
    search_available_tools without any LLM call

Part 2 (live LLM, kimi_code/k3):
  (a) non-resident task: "查看系统配置" needs configure_system (lazy) ->
      must be discovered via search_available_tools and then called
  (b) resident task: execute_python arithmetic -> normal direct call

Run: venv/Scripts/python.exe scratch/verify_tiered_tools.py [--skip-live]
"""
import json
import os
import subprocess
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

SKIP_LIVE = "--skip-live" in sys.argv
results = []  # (name, ok, detail)


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def schema_bytes(schemas):
    return len(json.dumps(schemas, ensure_ascii=False).encode("utf-8"))


# ---------------------------------------------------------------- Part 1
print("=== Part 1: static checks (no LLM) ===")
tmp_mem = tempfile.mkdtemp(prefix="tiered_mem_")
mem_db = os.path.join(tmp_mem, "memory.db")

from agent.agent import OpenAGCAgent  # noqa: E402

agent = OpenAGCAgent(model="kimi_code/k3", session_id=990201,
                     memory_db_path=mem_db)

TIERED_CORE_TOOL_NAMES = {"read_file", "write_file", "edit_file", "apply_patch", "execute_shell",
                          "execute_python", "search_file_content", "find_files",
                          "search_web", "ask_user_question", "self_review",
                          "user_interjection_response", "manage_memory",
                          "search_history", "search_available_tools"}

tiered_names = set(agent.available_tools.keys())
after_bytes = schema_bytes(agent.tool_schemas)
print(f"Tiered resident tools ({len(tiered_names)}): {sorted(tiered_names)}")
print(f"AFTER  first-turn schema bytes (this machine, incl. adaptive): {after_bytes}")

check("tiered flag read (default true)", agent.tool_tiered_exposure is True)

# Split resident set into designed core vs adaptive promotions
from core.paths import get_data_path  # noqa: E402
from tools.adaptive import get_adaptive_tools  # noqa: E402

adapt_dir = os.path.dirname(get_data_path("config.json"))
adaptive_now = get_adaptive_tools(adapt_dir,
                                  set(agent.full_available_tools.keys()),
                                  TIERED_CORE_TOOL_NAMES)
core_only_names = tiered_names - adaptive_now
core_only_bytes = schema_bytes(
    [agent.full_available_tools[n].get_openai_schema()
     for n in core_only_names if agent.full_available_tools.get(n) is not None])
print(f"Designed core ({len(core_only_names)}): {sorted(core_only_names)}")
print(f"Core-only schema bytes (fresh-profile first turn): {core_only_bytes}; "
      f"adaptive promotions here: {sorted(adaptive_now)}")

check("resident set == designed core (+ adaptive only)",
      core_only_names == TIERED_CORE_TOOL_NAMES,
      f"extra={sorted(core_only_names - TIERED_CORE_TOOL_NAMES)}, "
      f"missing={sorted(TIERED_CORE_TOOL_NAMES - core_only_names)}")

# Representative lazy tools must not be in the DESIGNED core (adaptive may
# legitimately promote a few of them on machines with usage history)
expected_lazy = {"queue_download", "pause_and_wait", "shell_send",
                 "configure_system", "manage_task_plan", "browser_automation",
                 "computer_control", "send_email", "search_emails",
                 "compact_context", "manage_task", "develop_plugin",
                 "enter_sandbox_mode", "exit_sandbox_mode", "mac_system_action",
                 "save_learned_skill"}
check("lazy tools excluded from designed core",
      not (expected_lazy & core_only_names),
      f"in core: {sorted(expected_lazy & core_only_names)}")

# BEFORE: reconstruct the legacy full-resident set from the same tool instances
FULL_CORE_TOOL_NAMES = {"execute_shell", "manage_memory", "read_file", "write_file", "edit_file",
                        "search_file_content", "find_files", "search_available_tools",
                        "ask_user_question", "user_interjection_response", "search_history",
                        "queue_download", "pause_and_wait", "execute_python", "search_web",
                        "self_review", "configure_system", "manage_task_plan", "parse_html",
                        "shell_send"}
legacy_adaptive = get_adaptive_tools(adapt_dir,
                                     set(agent.full_available_tools.keys()),
                                     FULL_CORE_TOOL_NAMES)
before_names = (set(FULL_CORE_TOOL_NAMES) | legacy_adaptive) & set(agent.full_available_tools.keys())
before_schemas = [agent.full_available_tools[n].get_openai_schema()
                  for n in before_names
                  if agent.full_available_tools[n] is not None]
before_bytes = schema_bytes(before_schemas)
print(f"BEFORE first-turn schema bytes: {before_bytes} ({len(before_names)} tools, "
      f"adaptive: {sorted(legacy_adaptive)})")

check("core set first-turn schema <= 10KB",
      core_only_bytes <= 10 * 1024, f"{core_only_bytes} bytes")
check("tiered exposure shrinks first-turn schema",
      after_bytes < before_bytes,
      f"{before_bytes} -> {after_bytes} "
      f"({100 * (before_bytes - after_bytes) / before_bytes:.0f}% smaller)")

# Subprocess checks with an isolated OPEN_AGC_DATA_DIR (no usage history ->
# no adaptive promotions; no MCP/auto tools) = fresh-profile first turn.
child_code = r'''
import json, os, sys
sys.path.insert(0, r"%s")
from agent.agent import OpenAGCAgent
a = OpenAGCAgent(model="kimi_code/k3", session_id=990202,
                 memory_db_path=os.path.join(os.environ["OPEN_AGC_DATA_DIR"], "data", "m.db"))
names = sorted(a.available_tools.keys())
b = len(json.dumps(a.tool_schemas, ensure_ascii=False).encode("utf-8"))
print("RESULT_JSON=" + json.dumps({"flag": a.tool_tiered_exposure, "names": names, "bytes": b}))
''' % PROJECT_ROOT


def run_isolated(config):
    tmp_data = tempfile.mkdtemp(prefix="tiered_cfg_")
    os.makedirs(os.path.join(tmp_data, "data"), exist_ok=True)
    with open(os.path.join(tmp_data, "data", "config.json"), "w", encoding="utf-8") as f:
        json.dump(config, f)
    env = dict(os.environ, OPEN_AGC_DATA_DIR=tmp_data)
    out = subprocess.run([sys.executable, "-c", child_code], env=env,
                         capture_output=True, text=True, timeout=240, cwd=PROJECT_ROOT)
    line = next((l for l in out.stdout.splitlines() if l.startswith("RESULT_JSON=")), None)
    if line is None:
        raise RuntimeError(f"child failed: {out.stderr[-500:]}")
    return json.loads(line[len("RESULT_JSON="):])


try:
    fresh = run_isolated({"sandbox_mode": False})  # flag defaults to true
    print(f"fresh profile (flag default): {fresh['bytes']} bytes, {len(fresh['names'])} tools")
    check("fresh profile: exactly the designed core is resident",
          set(fresh["names"]) == TIERED_CORE_TOOL_NAMES
          and fresh["flag"] is True,
          f"{len(fresh['names'])} tools, {fresh['bytes']} bytes")
    check("fresh profile first-turn schema <= 10KB",
          fresh["bytes"] <= 10 * 1024, f"{fresh['bytes']} bytes")
except Exception as e:
    check("fresh profile subprocess", False, str(e))

try:
    full = run_isolated({"tool_tiered_exposure": False, "sandbox_mode": False})
    print(f"flag=false subprocess: {len(full['names'])} tools, {full['bytes']} bytes")
    check("tool_tiered_exposure=false restores full residency",
          full["flag"] is False
          and "configure_system" in full["names"]
          and "queue_download" in full["names"]
          and len(full["names"]) >= 20,
          f"{len(full['names'])} tools resident, {full['bytes']} bytes")
except Exception as e:
    check("tool_tiered_exposure=false restores full residency", False, str(e))

# Lazy injection machinery (no LLM): discover + enable a non-resident tool
target = next((t for t in ["queue_download", "configure_system", "send_email",
                           "manage_task_plan", "compact_context"]
               if t not in agent.available_tools), None)
if target is None:
    check("lazy injection via search_available_tools", False,
          "no lazy tool available to test (all promoted by adaptive?)")
else:
    before_enable = schema_bytes(agent.tool_schemas)
    discovery = agent.full_available_tools["search_available_tools"]
    result = discovery.execute(query=target.replace("_", " "))
    after_enable = schema_bytes(agent.tool_schemas)
    enabled = target in agent.available_tools
    in_schema = any(s["function"]["name"] == target for s in agent.tool_schemas)
    check("lazy injection via search_available_tools",
          enabled and in_schema,
          f"{target} enabled; schema {before_enable} -> {after_enable} bytes")
    print("discovery result:", result.splitlines()[0])

# ---------------------------------------------------------------- Part 2
if SKIP_LIVE:
    print("\n=== Part 2: SKIPPED (--skip-live) ===")
else:
    print("\n=== Part 2: live agent tests (kimi_code/k3) ===")
    live = OpenAGCAgent(model="kimi_code/k3", session_id=990203,
                        memory_db_path=mem_db)

    calls = []  # (tool, success)

    def cb(ev):
        if ev.get("event") == "tool_done":
            calls.append((ev.get("tool"), ev.get("success")))
            print(f"  tool_done: {ev.get('tool')} success={ev.get('success')}")

    # (a) non-resident tool: configure_system (read-only get_settings)
    lazy_target = "configure_system" if "configure_system" not in live.available_tools else None
    if lazy_target is None:
        check("live: configure_system is lazy at start", False,
              "configure_system unexpectedly resident (adaptive?) — pick another target")
    else:
        calls.clear()
        try:
            reply = live.run_turn(
                "帮我查看一下当前的系统配置，告诉我默认模型是什么。"
                "这需要用到系统配置管理工具。",
                verbose=True, progress_callback=cb)
        except Exception as e:
            reply = f"<exception: {e}>"
        called = [t for t, _ in calls]
        discovered = "search_available_tools" in called
        used = "configure_system" in called
        ok = used and "configure_system" in live.available_tools
        check("live (a): lazy tool discovered & called", ok,
              f"calls={called}; discovered={discovered}; reply={str(reply)[:120]!r}")

    # (b) resident tool: execute_python
    calls.clear()
    try:
        reply = live.run_turn(
            "用 execute_python 计算 123*456，然后直接告诉我计算结果数字。",
            verbose=True, progress_callback=cb)
    except Exception as e:
        reply = f"<exception: {e}>"
    called = [t for t, _ in calls]
    check("live (b): resident tool called directly",
          "execute_python" in called and "56088" in str(reply),
          f"calls={called}; reply={str(reply)[:120]!r}")

# ---------------------------------------------------------------- summary
print("\n=== SUMMARY ===")
failed = [r for r in results if not r[1]]
print(f"{len(results) - len(failed)}/{len(results)} checks passed")
print(f"METRIC before={before_bytes}B after={after_bytes}B "
      f"resident_tools={len(tiered_names)}")
sys.exit(1 if failed else 0)
