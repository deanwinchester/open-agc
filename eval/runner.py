"""
Production-grade Agent evaluation runner.

Tracks capability changes across iterations with:
  - Per-category scoring (shell/filesystem/memory/web/reasoning)
  - Token efficiency metrics
  - Tool call sequence analysis
  - Setup/teardown per scenario
  - JSON + human-readable output
  - CI-friendly exit codes

Usage:
    python -m eval.runner                          # Run all
    python -m eval.runner --category shell memory   # By category
    python -m eval.runner --level core              # By level (core/advanced/stress)
    python -m eval.runner --report                  # History comparison
    python -m eval.runner --json                    # Machine-readable JSON output
"""
import os, sys, json, time, glob, argparse
from datetime import datetime
from collections import defaultdict

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

SCENARIOS_DIR = os.path.join(os.path.dirname(__file__), "scenarios")
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")
HISTORY_FILE = os.path.join(RESULTS_DIR, "history.json")
os.makedirs(RESULTS_DIR, exist_ok=True)


# ── Scenario loading ──

def _load_one(entry, fname):
    """Normalize a scenario entry (single dict or array element) with metadata."""
    entry["_file"] = fname
    return entry


def load_scenarios(category_filter=None, level_filter=None, tag_filter=None):
    """Load scenarios with multi-dimensional filtering.

    Each JSON file can be a single scenario object or an array of scenarios.
    """
    scenarios = []
    for fpath in sorted(glob.glob(os.path.join(SCENARIOS_DIR, "*.json"))):
        fname = os.path.basename(fpath)
        with open(fpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        items = data if isinstance(data, list) else [data]
        for item in items:
            entry = _load_one(item, fname)
            if category_filter:
                cats = set(entry.get("category", []))
                if not cats.intersection(category_filter):
                    continue
            if level_filter:
                if entry.get("level") not in level_filter:
                    continue
            if tag_filter:
                tags = set(entry.get("tags", []))
                if not tags.intersection(tag_filter):
                    continue
            scenarios.append(entry)
    return scenarios


# ── Check logic ──

def check_response(response_text, tool_calls, metrics, expected):
    """Check agent response against expected criteria. Returns (pass, details_dict)."""
    details = {}
    passed = True

    used_tools = [tc["name"] for tc in tool_calls]

    if "tool_used" in expected:
        matched = []
        missing = []
        for required in expected["tool_used"]:
            if required in used_tools:
                matched.append(required)
            else:
                missing.append(required)
        if missing:
            details["tool_missing"] = missing
            passed = False
        details["tools_matched"] = matched

    if "tool_not_used" in expected:
        for forbidden in expected["tool_not_used"]:
            if forbidden in used_tools:
                details["tool_forbidden_used"] = forbidden
                passed = False

    if "output_contains" in expected:
        missing_kw = [kw for kw in expected["output_contains"] if kw not in response_text]
        if missing_kw:
            details["output_missing"] = missing_kw
            passed = False

    if "output_not_contains" in expected:
        found_kw = [kw for kw in expected["output_not_contains"] if kw in response_text]
        if found_kw:
            details["output_forbidden_found"] = found_kw
            passed = False

    max_steps = expected.get("max_steps")
    if max_steps and len(tool_calls) > max_steps:
        details["steps_over_limit"] = len(tool_calls)
        passed = False

    max_tokens = expected.get("max_tokens")
    if max_tokens and metrics.get("total_tokens", 0) > max_tokens:
        details["tokens_over_limit"] = metrics["total_tokens"]
        passed = False

    max_errors = expected.get("max_errors", 0)
    if metrics.get("tool_errors", 0) > max_errors:
        details["too_many_errors"] = metrics["tool_errors"]
        passed = False

    # Soft checks (warn only, don't fail)
    details["tool_sequence"] = used_tools
    details["tool_count"] = len(tool_calls)

    return passed, details


# ── Scenario runner ──

def run_scenario(scenario):
    """Execute a single evaluation scenario. Returns detailed result dict."""
    from agent.agent import OpenAGCAgent

    name = scenario["name"]
    prompt = scenario["prompt"]
    setup = scenario.get("setup", "")
    teardown = scenario.get("teardown", "")

    print(f"\n  {'='*55}")
    print(f"  [{scenario.get('level','core').upper():>8}] {name}")
    print(f"  {scenario.get('description','')}")
    print(f"  {'─'*55}")

    errors = []
    tool_calls = []
    tool_errors = 0
    response = ""
    t0 = time.time()
    step_timing = []
    total_prompt_tokens = 0
    total_completion_tokens = 0

    # ── Setup ──
    if setup:
        try:
            if isinstance(setup, str):
                exec(setup)
        except Exception as e:
            print(f"  ⚠️  Setup error: {e}")

    # ── Run ──
    try:
        agent = OpenAGCAgent()
        response = agent.run_turn(prompt, verbose=False)

        for msg in agent.messages:
            if msg.get("role") == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    fn = tc.get("function", {})
                    tool_calls.append({
                        "name": fn.get("name", ""),
                        "args": str(fn.get("arguments", ""))[:200],
                    })

        # Count tool errors from step results
        for msg in agent.messages:
            if msg.get("role") == "tool":
                content = str(msg.get("content", ""))
                if any(kw in content.lower() for kw in ("error", "traceback", "exception")):
                    tool_errors += 1

        # Rough token estimate
        total_prompt_tokens = sum(len(str(m.get("content", ""))) // 2 for m in agent.messages)

    except Exception as e:
        errors.append(str(e)[:300])
        response = f"[ERROR] {e}"
        print(f"  💥 Exception: {str(e)[:150]}")

    elapsed = time.time() - t0

    metrics = {
        "total_tokens": total_prompt_tokens,
        "tool_errors": tool_errors,
        "elapsed_s": round(elapsed, 2),
    }

    passed, details = check_response(response, tool_calls, metrics,
                                     scenario.get("expected", {}))

    result = {
        "name": name,
        "category": scenario.get("category", ["uncategorized"]),
        "level": scenario.get("level", "core"),
        "passed": passed,
        "metrics": metrics,
        "tool_calls": [tc["name"] for tc in tool_calls],
        "tool_call_count": len(tool_calls),
        "tool_error_count": tool_errors,
        "check_details": details,
        "response_snippet": response[:200],
        "errors": errors,
        "timestamp": datetime.now().isoformat(),
    }

    # Print result
    status = "✅ PASS" if passed else "❌ FAIL"
    print(f"  ───────────────────────────────────────")
    print(f"  {status} | {elapsed:.1f}s | {len(tool_calls)} steps | ~{total_prompt_tokens} tok")
    if tool_errors:
        print(f"  ⚠️  {tool_errors} tool error(s)")
    if details.get("tool_missing"):
        print(f"  ❌ 缺少工具: {', '.join(details['tool_missing'])}")
    if details.get("output_missing"):
        print(f"  ❌ 输出缺关键词: {details['output_missing']}")
    if details.get("steps_over_limit"):
        print(f"  ❌ 步骤超限: {details['steps_over_limit']}")
    if errors:
        print(f"  ⚠️  Error: {errors[0][:150]}")
    if details.get("tool_sequence"):
        seq = " → ".join(details["tool_sequence"][:6])
        if len(details["tool_sequence"]) > 6:
            seq += f" ... (+{len(details['tool_sequence'])-6})"
        print(f"  🔧 {seq}")

    # ── Teardown ──
    if teardown:
        try:
            if isinstance(teardown, str):
                exec(teardown)
        except Exception as e:
            print(f"  ⚠️  Teardown error: {e}")

    return result


# ── Reporting ──

def generate_report(all_results, git_commit=None):
    """Generate structured report with per-category breakdowns."""
    total = len(all_results)
    passed = sum(1 for r in all_results if r["passed"])
    failed = total - passed

    # Per-category scoring
    by_category = defaultdict(lambda: {"total": 0, "passed": 0, "tokens": 0, "steps": 0, "errors": 0})
    for r in all_results:
        for cat in r.get("category", ["uncategorized"]):
            c = by_category[cat]
            c["total"] += 1
            c["passed"] += 1 if r["passed"] else 0
            c["tokens"] += r["metrics"]["total_tokens"]
            c["steps"] += r["tool_call_count"]
            c["errors"] += r["tool_error_count"]

    # Per-level scoring
    by_level = defaultdict(lambda: {"total": 0, "passed": 0})
    for r in all_results:
        lvl = r.get("level", "core")
        by_level[lvl]["total"] += 1
        by_level[lvl]["passed"] += 1 if r["passed"] else 0

    report = {
        "timestamp": datetime.now().isoformat(),
        "git_commit": git_commit,
        "summary": {
            "total": total,
            "passed": passed,
            "failed": failed,
            "pass_rate": round(passed / total * 100, 1) if total else 0,
        },
        "by_category": dict(by_category),
        "by_level": dict(by_level),
        "scenarios": all_results,
    }
    return report


def save_report(report):
    """Save run report and update history."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_file = os.path.join(RESULTS_DIR, f"run_{timestamp}.json")
    with open(run_file, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    # Update lightweight history (no scenario details)
    history = []
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            history = json.load(f)
    history.append({
        "timestamp": report["timestamp"],
        "run_file": f"run_{timestamp}.json",
        "summary": report["summary"],
        "by_category": {k: {"total": v["total"], "passed": v["passed"]} for k, v in report["by_category"].items()},
        "by_level": report["by_level"],
    })
    history = history[-100:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

    return run_file


def show_report(history, json_output=False):
    """Display or output historical evaluation results."""
    if not history:
        print("  暂无历史数据")
        return

    last_run = history[-1]
    s = last_run["summary"]

    if json_output:
        print(json.dumps(history, ensure_ascii=False, indent=2))
        return

    print(f"\n  {'='*55}")
    print(f"  📊 最近评估 (#{len(history)})")
    print(f"  {'='*55}")
    print(f"  总通过率: {s['passed']}/{s['total']} ({s['pass_rate']}%)")
    print(f"  失败: {s['failed']}")

    if last_run.get("by_category"):
        print(f"\n  ── 按类别 ──")
        for cat, stats in sorted(last_run["by_category"].items()):
            rate = round(stats["passed"] / stats["total"] * 100, 1) if stats["total"] else 0
            bar = "🟢" if rate >= 80 else "🟡" if rate >= 50 else "🔴"
            print(f"  {bar} {cat:<12} {stats['passed']}/{stats['total']} ({rate}%)")

    if last_run.get("by_level"):
        print(f"\n  ── 按难度 ──")
        for lvl, stats in sorted(last_run["by_level"].items()):
            rate = round(stats["passed"] / stats["total"] * 100, 1) if stats["total"] else 0
            print(f"  {'🟢' if rate>=80 else '🟡' if rate>=50 else '🔴'} {lvl:<10} {stats['passed']}/{stats['total']} ({rate}%)")

    # Trend
    if len(history) >= 5:
        recent = history[-5:]
        rates = [h["summary"]["pass_rate"] for h in recent]
        trend = "📈 上升" if rates[-1] > rates[0] else "📉 下降" if rates[-1] < rates[0] else "➡️ 持平"
        print(f"\n  趋势 (近5次): {' → '.join(f'{r}%' for r in rates)} {trend}")

    if len(history) >= 2:
        prev = history[-2]
        diff = s["passed"] - prev["summary"]["passed"]
        print(f"\n  较上次: {'📈 +' if diff>0 else '📉 ' if diff<0 else '➡️ '}{diff}")


# ── Main ──

def main():
    parser = argparse.ArgumentParser(description="Open-AGC Agent Evaluation")
    parser.add_argument("--category", nargs="*", help="Filter by category (shell filesystem memory web reasoning)")
    parser.add_argument("--level", nargs="*", default=None, help="Filter by level (core advanced stress)")
    parser.add_argument("--tag", nargs="*", help="Filter by tag")
    parser.add_argument("--report", action="store_true", help="Show historical report")
    parser.add_argument("--json", action="store_true", help="JSON output (machine-readable)")
    args = parser.parse_args()

    if args.report:
        history = []
        if os.path.exists(HISTORY_FILE):
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                history = json.load(f)
        show_report(history, json_output=args.json)
        return

    tag_filter = set(args.tag) if args.tag else None
    category_filter = set(args.category) if args.category else None
    level_filter = set(args.level) if args.level else None
    scenarios = load_scenarios(category_filter, level_filter, tag_filter)

    if not scenarios:
        print(f"没有找到匹配的场景")
        return

    print(f"\n  🌟 Open-AGC Agent Evaluation")
    print(f"  {'='*55}")
    if category_filter:
        print(f"  类别过滤: {', '.join(category_filter)}")
    if level_filter:
        print(f"  难度过滤: {', '.join(level_filter)}")
    print(f"  场景数: {len(scenarios)}")
    print(f"  {'='*55}")

    results = []
    for scenario in scenarios:
        result = run_scenario(scenario)
        results.append(result)

    # Commit hash for traceability
    git_commit = None
    try:
        import subprocess
        git_commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except Exception:
        pass

    report = generate_report(results, git_commit=git_commit)
    run_file = save_report(report)
    s = report["summary"]

    print(f"\n  {'='*55}")
    print(f"  📊 总计: {s['passed']}/{s['total']} 通过 ({s['pass_rate']}%)")
    if git_commit:
        print(f"  commit: {git_commit}")
    print(f"  报告: {run_file}")

    show_report(
        [json.load(open(HISTORY_FILE, "r", encoding="utf-8"))[-1]]
        if os.path.exists(HISTORY_FILE) else [],
    )

    # CI-friendly exit code
    exit(0 if s["failed"] == 0 else 1)


if __name__ == "__main__":
    main()
