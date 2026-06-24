"""
Agent evaluation runner — measures agent capability changes across iterations.

Usage:
    python -m eval.runner                          # Run all scenarios
    python -m eval.runner --scenarios shell memory  # Run specific tags
    python -m eval.runner --report                  # Show history comparison
"""
import os, sys, json, time, glob, re, argparse, textwrap
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

SCENARIOS_DIR = os.path.join(os.path.dirname(__file__), "scenarios")
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")
HISTORY_FILE = os.path.join(RESULTS_DIR, "history.json")
os.makedirs(RESULTS_DIR, exist_ok=True)


def load_scenarios(tag_filter=None):
    """Load all scenario JSON files, optionally filtered by tag."""
    scenarios = []
    for fpath in sorted(glob.glob(os.path.join(SCENARIOS_DIR, "*.json"))):
        with open(fpath, "r", encoding="utf-8") as f:
            scenario = json.load(f)
        scenario["_file"] = os.path.basename(fpath)
        if tag_filter:
            tags = set(scenario.get("tags", []))
            if not tags.intersection(tag_filter):
                continue
        scenarios.append(scenario)
    return scenarios


def check_expected(response_text, tool_calls, usage, expected):
    """Check if agent response meets expected criteria. Returns (pass, details)."""
    details = []
    passed = True

    # Check tool usage
    if "tool_used" in expected:
        used_tools = {tc.get("function", {}).get("name", "") for tc in tool_calls}
        for required in expected["tool_used"]:
            if required in used_tools:
                details.append(f"  ✅ 使用工具: {required}")
            else:
                details.append(f"  ❌ 未使用工具: {required}")
                passed = False

    # Check output content
    if "output_contains" in expected:
        for keyword in expected["output_contains"]:
            if keyword in response_text:
                details.append(f"  ✅ 输出包含: {keyword}")
            else:
                details.append(f"  ❌ 输出缺少: {keyword}")
                passed = False

    # Check max steps
    max_steps = expected.get("max_steps")
    if max_steps and len(tool_calls) > max_steps:
        details.append(f"  ❌ 步骤超限: {len(tool_calls)} > {max_steps}")
        passed = False
    elif max_steps:
        details.append(f"  ✅ 步骤数: {len(tool_calls)} ≤ {max_steps}")

    # Token budget
    max_tokens = expected.get("max_tokens")
    total_tokens = (usage or {}).get("total_tokens", 0)
    if max_tokens and total_tokens > max_tokens:
        details.append(f"  ❌ Token超限: {total_tokens} > {max_tokens}")
        passed = False
    elif max_tokens:
        details.append(f"  ✅ Token: {total_tokens} ≤ {max_tokens}")

    return passed, "\n".join(details) if details else "  ✅ 无检查项"


def run_scenario(scenario):
    """Run a single evaluation scenario and return results."""
    from agent.agent import OpenAGCAgent
    from core.llm_client import _log_model_call, _infer_provider

    name = scenario["name"]
    prompt = scenario["prompt"]
    timeout = scenario.get("timeout", 60)

    print(f"\n  {'='*50}")
    print(f"  场景: {name}")
    print(f"  描述: {scenario.get('description', '')}")
    print(f"  提示: {prompt[:80]}{'...' if len(prompt) > 80 else ''}")
    print(f"  {'='*50}")

    t0 = time.time()
    errors = []
    tool_calls = []
    response = ""
    token_usage = {}

    try:
        # Create fresh agent for each scenario
        agent = OpenAGCAgent()
        response = agent.run_turn(prompt, verbose=False)

        # Extract tool calls from agent.messages
        for msg in agent.messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    tool_calls.append({
                        "name": fn.get("name", ""),
                        "args": str(fn.get("arguments", ""))[:200],
                    })

        # Rough token estimate
        token_usage = {"total_tokens": sum(len(str(m.get("content", ""))) // 2 for m in agent.messages)}

    except Exception as e:
        errors.append(str(e)[:200])
        response = f"[ERROR] {e}"

    elapsed = time.time() - t0
    passed, details = check_expected(response, tool_calls, token_usage, scenario.get("expected", {}))

    result = {
        "name": name,
        "passed": passed,
        "elapsed_s": round(elapsed, 2),
        "tool_calls": len(tool_calls),
        "total_tokens": token_usage.get("total_tokens", 0),
        "details": details,
        "errors": errors,
        "timestamp": datetime.now().isoformat(),
    }

    status = "✅" if passed else "❌"
    print(f"  结果: {status} ({elapsed:.1f}s, {tool_calls} steps)")
    if errors:
        print(f"  ⚠️  Error: {errors[0][:150]}")
    return result


def save_run(results):
    """Save run results and update history."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_file = os.path.join(RESULTS_DIR, f"run_{timestamp}.json")
    summary = {
        "timestamp": timestamp,
        "total": len(results),
        "passed": sum(1 for r in results if r["passed"]),
        "failed": sum(1 for r in results if not r["passed"]),
        "scenarios": results,
    }
    with open(run_file, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)

    # Update history
    history = []
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            history = json.load(f)
    history.append({
        "timestamp": timestamp,
        "total": summary["total"],
        "passed": summary["passed"],
        "failed": summary["failed"],
    })
    # Keep last 50 runs
    history = history[-50:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

    return summary


def show_report():
    """Show historical evaluation results."""
    if not os.path.exists(HISTORY_FILE):
        print("  暂无历史数据")
        return

    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        history = json.load(f)

    print(f"\n  {'='*50}")
    print(f"  评估历史 ({len(history)} 次运行)")
    print(f"  {'='*50}")
    for h in history:
        trend = "🟢" if h["passed"] >= h["total"] * 0.8 else "🟡" if h["passed"] >= h["total"] * 0.5 else "🔴"
        print(f"  {trend} {h['timestamp'][:16]}  {h['passed']}/{h['total']} 通过 ({h['failed']} 失败)")

    if len(history) >= 2:
        last = history[-1]
        prev = history[-2]
        diff = last["passed"] - prev["passed"]
        arrow = "📈" if diff > 0 else "📉" if diff < 0 else "➡️"
        print(f"\n  最近变化: {arrow} {prev['passed']}/{prev['total']} → {last['passed']}/{last['total']} ({diff:+d})")


def main():
    parser = argparse.ArgumentParser(description="Open-AGC Agent Evaluation")
    parser.add_argument("--scenarios", nargs="*", help="Filter by tags (e.g. shell memory)")
    parser.add_argument("--report", action="store_true", help="Show historical report")
    args = parser.parse_args()

    if args.report:
        show_report()
        return

    tag_filter = set(args.scenarios) if args.scenarios else None
    scenarios = load_scenarios(tag_filter)

    if not scenarios:
        print(f"没有找到场景 (filter={tag_filter})")
        print(f"场景目录: {SCENARIOS_DIR}")
        return

    print(f"\n  加载了 {len(scenarios)} 个场景")
    results = []
    for scenario in scenarios:
        result = run_scenario(scenario)
        results.append(result)

    summary = save_run(results)
    passed = summary["passed"]
    total = summary["total"]
    print(f"\n  {'='*50}")
    print(f"  📊 总计: {passed}/{total} 通过 ({(passed/total*100):.0f}%)")
    print(f"  {'='*50}")

    show_report()


if __name__ == "__main__":
    main()
