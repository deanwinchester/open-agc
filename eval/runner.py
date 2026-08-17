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

    # LLM 调用失败时任何场景都不得判过（生产实证：LLM 全挂时闲聊场景
    # 因「未使用 dispatch_worker + 零错误」假通过）
    if response_text.startswith("[LLM_ERROR]") or response_text.startswith("[ERROR]"):
        details["llm_failed"] = response_text[:120]
        return False, details

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


# ── Mode control (M3: A/B dispatcher evaluation) ──

def _set_dispatcher_mode(enabled):
    """eval 进程内强制 dispatcher_mode 开/关（类方法级 patch，覆盖
    __init__ 组装/工具注册/run_turn 守卫的全部读取点）。"""
    from agent.agent import OpenAGCAgent
    OpenAGCAgent._dispatcher_mode_enabled = lambda self: enabled


def _wait_dispatches(agent, timeout_s=300):
    """dispatcher 模式：run_turn 返回「已开工」后等待后台 worker 完成，
    把 worker 结果并入测评（M2 异步化后 worker 产出不在 run_turn 返回里）。
    读取后 pop 清残留（生产实证：eval 场景共享 (None,None) key，上一个
    场景的完成结果被下一个场景误收）。返回 (dispatch_result or None)。"""
    import time as _t
    from agent import dispatcher
    key = (getattr(agent, "session_id", None), getattr(agent, "task_id", None))
    t0 = _t.time()
    while _t.time() - t0 < timeout_s:
        with dispatcher._running_lock:
            d = dispatcher._running_dispatches.get(key)
        if d and d.get("done"):
            with dispatcher._running_lock:
                dispatcher._running_dispatches.pop(key, None)
            return d.get("result")
        # 从未发起 dispatch（直执/闲聊）→ 立即返回
        if d is None:
            return None
        _t.sleep(1)
    return None


def _clear_stale_dispatch(agent):
    """场景开局清理残留 dispatch 状态（上一场景遗留的 done 结果/线程句柄）。"""
    from agent import dispatcher
    key = (getattr(agent, "session_id", None), getattr(agent, "task_id", None))
    with dispatcher._running_lock:
        dispatcher._running_dispatches.pop(key, None)
    dispatcher._pop_worker_inbox(key[0], key[1])


def _real_token_usage(since_ts):
    """真实 token 统计：model_call_logs 按时间窗聚合（eval 场景无 session，
    过滤 session_id IS NULL；日志未记录时返回 None 退回粗略估算）。"""
    try:
        from api.db import db_connect
        conn = db_connect()
        row = conn.execute(
            "SELECT COALESCE(SUM(prompt_tokens),0) p, COALESCE(SUM(completion_tokens),0) c, "
            "COALESCE(SUM(cached_tokens),0) k, COUNT(*) n FROM model_call_logs "
            "WHERE timestamp >= ? AND session_id IS NULL", (since_ts,)
        ).fetchone()
        conn.close()
        if row and row[3] > 0:
            return {"prompt_tokens": row[0], "completion_tokens": row[1],
                    "cached_tokens": row[2], "total_tokens": row[0] + row[1],
                    # 计费口径：缓存命中部分成本极低（生产实证 k3 缓存率 ~70%，
                    # 名义 ×1.6 的实际成本增幅远小）——判定用 billable
                    "billable_tokens": row[0] - row[2] + row[1],
                    "calls": row[3]}
    except Exception:
        pass
    return None


# ── Scenario runner ──

def run_scenario(scenario, dispatcher_mode=None):
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
    total_billable_tokens = 0
    total_completion_tokens = 0
    worker_info = None

    # M3：模式强制（None = 按 config 原样）
    if dispatcher_mode is not None:
        _set_dispatcher_mode(dispatcher_mode)
    from datetime import timezone
    run_start_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    # ── Setup ──
    if setup:
        try:
            if isinstance(setup, str):
                exec(setup)
        except Exception as e:
            print(f"  ⚠️  Setup error: {e}")

    # ── Run ──
    try:
        agent = _make_eval_agent(scenario, dispatcher_mode)
        if dispatcher_mode:
            _clear_stale_dispatch(agent)  # 场景间共享 (None,None) key，清残留
        response = agent.run_turn(prompt, verbose=False)

        # M2 异步：dispatcher 模式下等待后台 worker 完成并合并结果
        if dispatcher_mode:
            wres = _wait_dispatches(agent)
            if wres:
                wsum = str(wres.get("summary", "") or "")
                response = (response + "\n" + wsum).strip()
                wresult = wres.get("result") if isinstance(wres.get("result"), dict) else {}
                worker_info = {
                    "success": bool(wres.get("success")),
                    "tool_calls": wresult.get("tool_calls", 0),
                    "verdict": wres.get("verdict"),
                }
                # worker 的工具调用并入判定口径：tool_used 是系统级能力判定，
                # 不关心是主 agent 还是 worker 调的（生产实证：worker 干完活
                # 但主 agent 只调 dispatch_worker，tool_missing 全 FAIL 假象）
                for st in (wresult.get("steps") or []):
                    if isinstance(st, dict) and st.get("tool"):
                        tool_calls.append({"name": st["tool"],
                                           "args": str(st.get("args", ""))[:200]})
                        if not st.get("success", True):
                            tool_errors += 1

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

        # M3：真实 token（model_call_logs 时间窗），失败退回粗略估算
        _rt = _real_token_usage(run_start_utc)
        if _rt:
            total_prompt_tokens = _rt["total_tokens"]
            total_billable_tokens = _rt["billable_tokens"]
        else:
            total_prompt_tokens = sum(len(str(m.get("content", ""))) // 2 for m in agent.messages)
            total_billable_tokens = total_prompt_tokens

    except Exception as e:
        errors.append(str(e)[:300])
        response = f"[ERROR] {e}"
        print(f"  💥 Exception: {str(e)[:150]}")

    elapsed = time.time() - t0

    metrics = {
        "total_tokens": total_prompt_tokens,
        "billable_tokens": total_billable_tokens,
        "tool_errors": tool_errors,
        "elapsed_s": round(elapsed, 2),
    }
    if worker_info:
        metrics["worker"] = worker_info

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


def generate_ab_report(baseline_results, dispatcher_results, repeat=1, git_commit=None):
    """M3：A/B 对比报告（dispatcher_mode off vs on）。

    判定标准（方案 §3.4）：成功率不降、token 增幅 ≤30%。
    """
    def _summarize(results):
        total = len(results)
        passed = sum(1 for r in results if r["passed"])
        tokens = sum(r["metrics"]["total_tokens"] for r in results)
        billable = sum(r["metrics"].get("billable_tokens", r["metrics"]["total_tokens"]) for r in results)
        steps = sum(r["tool_call_count"] for r in results)
        return {
            "total": total, "passed": passed,
            "pass_rate": round(passed / total * 100, 1) if total else 0,
            "avg_tokens": round(tokens / total, 1) if total else 0,
            "avg_billable": round(billable / total, 1) if total else 0,
            "avg_steps": round(steps / total, 2) if total else 0,
        }

    base = _summarize(baseline_results)
    disp = _summarize(dispatcher_results)
    diff_pp = round(disp["pass_rate"] - base["pass_rate"], 1)
    # token 判定用计费口径（扣缓存命中）：名义 token 把每轮重复发送但几乎
    # 零成本的缓存部分也算了进去，放大派发链路的真实开销
    token_ratio = round(disp["avg_billable"] / base["avg_billable"], 3) if base["avg_billable"] else None
    nominal_ratio = round(disp["avg_tokens"] / base["avg_tokens"], 3) if base["avg_tokens"] else None
    step_ratio = round(disp["avg_steps"] / base["avg_steps"], 3) if base["avg_steps"] else None

    # 逐场景对比（按场景名聚合 repeat 次的成功率）
    def _by_name(results):
        agg = defaultdict(lambda: {"n": 0, "passed": 0, "tokens": 0, "steps": 0})
        for r in results:
            a = agg[r["name"]]
            a["n"] += 1
            a["passed"] += 1 if r["passed"] else 0
            a["tokens"] += r["metrics"]["total_tokens"]
            a["steps"] += r["tool_call_count"]
        return agg

    bmap, dmap = _by_name(baseline_results), _by_name(dispatcher_results)
    per_scenario = []
    for name in sorted(set(bmap) | set(dmap)):
        b, d = bmap.get(name), dmap.get(name)
        per_scenario.append({
            "name": name,
            "baseline_pass": f"{b['passed']}/{b['n']}" if b else "-",
            "dispatcher_pass": f"{d['passed']}/{d['n']}" if d else "-",
            "baseline_avg_tokens": round(b["tokens"] / b["n"], 1) if b else None,
            "dispatcher_avg_tokens": round(d["tokens"] / d["n"], 1) if d else None,
        })

    verdict = {
        "成功率不降": diff_pp >= 0,
        "token增幅≤30%": (token_ratio is not None and token_ratio <= 1.3),
        "pass": diff_pp >= 0 and (token_ratio is not None and token_ratio <= 1.3),
    }
    return {
        "mode": "ab", "repeat": repeat, "git_commit": git_commit,
        "timestamp": datetime.now().isoformat(),
        "baseline": base, "dispatcher": disp,
        "comparison": {
            "pass_rate_diff_pp": diff_pp,
            "token_ratio": token_ratio,
            "token_ratio_nominal": nominal_ratio,
            "step_ratio": step_ratio,
            "verdict": verdict,
            "per_scenario": per_scenario,
        },
        "baseline_scenarios": baseline_results,
        "dispatcher_scenarios": dispatcher_results,
    }


def print_ab_report(report):
    c = report["comparison"]
    print(f"\n  {'='*60}")
    print(f"  📊 A/B 对比（repeat={report['repeat']}）  baseline=dispatcher_off")
    print(f"  {'='*60}")
    b, d = report["baseline"], report["dispatcher"]
    print(f"  成功率:  baseline {b['pass_rate']}% ({b['passed']}/{b['total']})  "
          f"→  dispatcher {d['pass_rate']}% ({d['passed']}/{d['total']})  "
          f"[{c['pass_rate_diff_pp']:+}pp]")
    print(f"  计费token: {b.get('avg_billable', b['avg_tokens'])} → {d.get('avg_billable', d['avg_tokens'])}  [×{c['token_ratio']}]"
          f"  (名义 ×{c.get('token_ratio_nominal')})")
    print(f"  平均步骤:  {b['avg_steps']} → {d['avg_steps']}  [×{c['step_ratio']}]")
    print(f"\n  ── 逐场景 ──")
    for s in c["per_scenario"]:
        print(f"  {s['name'][:34]:<36} {s['baseline_pass']:>6} → {s['dispatcher_pass']:<6}")
    v = c["verdict"]
    print(f"\n  ── 判定（方案 §3.4）──")
    print(f"  成功率不降:      {'✅' if v['成功率不降'] else '❌'}")
    print(f"  token增幅≤30%:   {'✅' if v['token增幅≤30%'] else '❌'}")
    print(f"  总判定:          {'✅ PASS' if v['pass'] else '❌ FAIL'}")


# ── A/B 断点续跑（用户要求：中断后可续，不全部重跑）──
# 每完成一项（场景×模式×轮次）立即落盘 checkpoint；同参数重启时加载并跳过
# 已完成项。全部完成后生成正式报告并删除 checkpoint。

AB_CKPT = os.path.join(RESULTS_DIR, "ab_inprogress.json")


def _ab_signature(args):
    return {
        "mode": args.mode, "repeat": args.repeat,
        "level": sorted(args.level or []),
        "category": sorted(args.category or []),
        "tag": sorted(args.tag or []),
    }


def _ab_load_ckpt(sig):
    if not os.path.exists(AB_CKPT):
        return None
    try:
        with open(AB_CKPT, "r", encoding="utf-8") as f:
            d = json.load(f)
        if d.get("sig") == sig:
            return d
        print("  ⚠️ 存在参数不同的未完成 checkpoint，忽略并重新开始")
    except Exception:
        pass
    return None


def _ab_save_ckpt(sig, base_results, disp_results):
    tmp = AB_CKPT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"sig": sig, "baseline": base_results, "dispatcher": disp_results},
                  f, ensure_ascii=False)
    os.replace(tmp, AB_CKPT)


# ── Sequence runner（M3+：长会话上下文污染量化）──
# 单轮孤立场景 ≠ 真实使用（长会话连续作战）。sequence 场景在同一 agent 实例
# 上连续跑一串任务（长任务穿插固定探针问答），测量：
#   - 主上下文膨胀曲线（每步后 messages 字符量）
#   - 探针步正确率（基准校验，随污染退化即污染实锤）
#   - 每步 billable token（baseline 随历史线性涨，dispatcher 应平稳）

def _context_chars(agent):
    return sum(len(str(m.get("content", ""))) for m in getattr(agent, "messages", []))


def _preload_session_messages(agent, session_id, limit=40):
    """预载真实会话历史（只读 DB，不写）——模拟「长会话进行中」的线上
    上下文起点（用户要求：测评要含真实使用场景）。"""
    from api.db import db_connect
    conn = db_connect()
    rows = conn.execute(
        "SELECT role, content FROM messages WHERE session_id=? "
        "AND role IN ('user','agent') ORDER BY id DESC LIMIT ?",
        (session_id, limit)).fetchall()
    conn.close()
    msgs = [{"role": ("assistant" if r[0] == "agent" else "user"), "content": r[1]}
            for r in reversed(rows) if r[1]]
    agent.messages = [agent.messages[0]] + msgs
    return len(msgs)


def _isolate_eval_side_effects(agent, results_subdir="."):
    """eval 副作用隔离：记忆库指向临时文件；KG 提取与反思后处理 no-op——
    评测产生的「苹果123」之类不应污染真实记忆/知识图谱/反思库。"""
    import types as _t
    try:
        from core.memory_store import MemoryStore
        from core.paths import get_data_path as _gdp
        _mdir = os.path.join(RESULTS_DIR, "sandbox")
        os.makedirs(_mdir, exist_ok=True)
        agent.memory_store = MemoryStore(db_path=os.path.join(_mdir, "eval_memory.db"))
    except Exception:
        pass
    agent.knowledge_graph = _t.SimpleNamespace(
        extract_from_messages=lambda msgs: None,
        retrieve_context=lambda *a, **k: None)
    agent._enqueue_post_process = lambda *a, **k: None


def _make_eval_agent(scenario=None, dispatcher_mode=None):
    """eval 专用 agent 构造：model=None（跟随 config）+ 副作用隔离 +
    可选真实会话历史预载（preload_session 场景字段）。"""
    from agent.agent import OpenAGCAgent
    if dispatcher_mode is not None:
        _set_dispatcher_mode(dispatcher_mode)
    agent = OpenAGCAgent(model=None)
    _isolate_eval_side_effects(agent)
    if scenario:
        preload = scenario.get("preload_session")
        if preload:
            n = _preload_session_messages(agent, int(preload))
            print(f"  📚 预载会话 #{preload} 历史 {n} 条（真实上下文起点）")
    return agent


def run_sequence(scenario, dispatcher_mode=None):
    """同一 agent 实例连续执行 steps。返回聚合结果 dict。"""
    from agent.agent import OpenAGCAgent
    from datetime import timezone

    name = scenario["name"]
    steps = scenario.get("steps", [])
    if dispatcher_mode is not None:
        _set_dispatcher_mode(dispatcher_mode)

    print(f"\n  {'='*55}")
    print(f"  [ SEQUENCE] {name} ({'dispatcher' if dispatcher_mode else 'baseline'})")
    print(f"  {scenario.get('description','')} — {len(steps)} 步连续执行")
    print(f"  {'─'*55}")

    setup = scenario.get("setup", "")
    if setup:
        try:
            exec(setup)
        except Exception as e:
            print(f"  ⚠️  Setup error: {e}")

    agent = _make_eval_agent(scenario, dispatcher_mode)
    if dispatcher_mode:
        _clear_stale_dispatch(agent)

    step_results = []
    context_curve = []
    total_billable = 0
    t0 = time.time()
    for i, st in enumerate(steps, 1):
        from datetime import timezone as _tz
        step_start = datetime.now(_tz.utc).strftime("%Y-%m-%d %H:%M:%S")
        prompt = st.get("prompt", "")
        expected = st.get("expected", {})
        is_probe = bool(st.get("probe"))
        try:
            worker_steps = []
            response = agent.run_turn(prompt, verbose=False) or ""
            worker_info = None
            if dispatcher_mode:
                wres = _wait_dispatches(agent)
                if wres:
                    # 与线上通道一致（用户要求）：worker 完成 = 【分身返回】注入
                    # messages + 主 agent 跑呈现 turn（线上走 resume_task_manual
                    # 唤起，同语义）。此前只在 run_turn 外拼字符串，主 agent 的
                    # messages 永远不含 worker 结果——下一步它只会对着「已开工」
                    # 的承诺空转（R3/R4 终检连环错位根因）。
                    try:
                        agent.pending_messages = [
                            m for m in (agent.pending_messages or [])
                            if f"【{getattr(agent, '_worker_name', '分身')}返回】" not in str(m)]
                    except Exception:
                        pass
                    ok = bool(wres.get("success"))
                    wsum = str(wres.get("summary", "") or "")[:1500]
                    verdict = wres.get("verdict") or {}
                    note = (f"【执行者返回】验收{'通过 ✅' if ok else '未通过 ❌'}\n"
                            f"摘要：{wsum or '（空）'}")
                    if not ok and verdict.get("failures"):
                        note += ("\n失败点：" + "; ".join(str(f)[:120]
                                 for f in verdict["failures"][:3])
                                 + "\n请按调度者职责：补充信息重派或亲自接管，并如实告知用户。")
                    else:
                        note += "\n请验收证据并呈现交付给用户。"
                    agent.messages.append({"role": "user", "content": note})
                    present = agent.run_turn(None, verbose=False, skip_rag=True) or ""
                    response = (response + "\n" + str(present)).strip()
                    wr = wres.get("result") if isinstance(wres.get("result"), dict) else {}
                    worker_info = {"success": ok,
                                   "tool_calls": wr.get("tool_calls", 0)}
                    # worker 步骤并入判定（与单任务 run_scenario 口径一致）
                    for wst in (wr.get("steps") or []):
                        if isinstance(wst, dict) and wst.get("tool"):
                            worker_steps.append(wst["tool"])
            tcs = []
            for msg in agent.messages:
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        tcs.append({"name": tc.get("function", {}).get("name", "")})
            for wtn in worker_steps:
                tcs.append({"name": wtn})
            rt = _real_token_usage(step_start) or {}
            billable = rt.get("billable_tokens", 0)
            passed, details = check_response(response, tcs, {"total_tokens": billable}, expected)
        except Exception as e:
            response = f"[ERROR] {e}"
            passed, details, billable, worker_info = False, {"exception": str(e)[:200]}, 0, None
        ctx = _context_chars(agent)
        context_curve.append(ctx)
        total_billable += billable
        step_results.append({
            "step": i, "probe": is_probe, "passed": passed,
            "billable_tokens": billable, "context_chars": ctx,
            "check_details": details,
            "response_snippet": str(response)[:200],
        })
        print(f"  {'🧪' if is_probe else '  '} 步骤{i}/{len(steps)}: "
              f"{'✅' if passed else '❌'} | billable {billable} | 上下文 {ctx} 字符")

    teardown = scenario.get("teardown", "")
    if teardown:
        try:
            exec(teardown)
        except Exception as e:
            print(f"  ⚠️  Teardown error: {e}")

    probe_steps = [s for s in step_results if s["probe"]]
    probe_passed = sum(1 for s in probe_steps if s["passed"])
    return {
        "name": name, "type": "sequence", "category": scenario.get("category", ["sequence"]),
        "level": scenario.get("level", "advanced"),
        "passed": all(s["passed"] for s in step_results),
        "metrics": {"total_tokens": total_billable, "billable_tokens": total_billable,
                    "tool_errors": 0, "elapsed_s": round(time.time() - t0, 2)},
        "tool_call_count": 0,
        "tool_error_count": 0,
        "check_details": {},
        "response_snippet": "",
        "errors": [],
        "timestamp": datetime.now().isoformat(),
        "sequence": {
            "steps": step_results,
            "context_curve": context_curve,
            "context_final": context_curve[-1] if context_curve else 0,
            "probe_total": len(probe_steps),
            "probe_passed": probe_passed,
            "probe_pass_rate": round(probe_passed / len(probe_steps) * 100, 1) if probe_steps else None,
        },
    }


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


def save_report(report, probes=None):
    """Save run report and update history. Optionally includes probe results."""
    if probes:
        report["probes"] = probes

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
    parser.add_argument("--probes", action="store_true", help="Run advanced probes (memory recall, tool discovery, context retention)")
    parser.add_argument("--probes-allow-side-effects", action="store_true",
                        help="Also run probes that mutate the real environment "
                             "(context retention / tool choice / response quality)")
    parser.add_argument("--json", action="store_true", help="JSON output (machine-readable)")
    parser.add_argument("--mode", choices=["baseline", "dispatcher", "ab"], default=None,
                        help="M3: 强制 dispatcher_mode 开/关测评；ab=双模式对比")
    parser.add_argument("--repeat", type=int, default=1,
                        help="每场景重复次数（模型方差大，ab 建议 3）")
    args = parser.parse_args()

    if args.probes:
        from eval.probes import run_all_probes
        probe_results = run_all_probes(allow_side_effects=args.probes_allow_side_effects)
        if args.json:
            print(json.dumps(probe_results, ensure_ascii=False, indent=2))
        return

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
    if args.mode:
        print(f"  模式: {args.mode} (repeat={args.repeat})")
    print(f"  {'='*55}")

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

    # ── M3: A/B 双模式对比（断点续跑）──
    if args.mode == "ab":
        sig = _ab_signature(args)
        base_results, disp_results = [], []
        done_keys = set()
        ckpt = _ab_load_ckpt(sig)
        if ckpt:
            base_results = ckpt.get("baseline", [])
            disp_results = ckpt.get("dispatcher", [])
            for r in base_results:
                done_keys.add((r["name"], "b", r.get("_round")))
            for r in disp_results:
                done_keys.add((r["name"], "d", r.get("_round")))
            if done_keys:
                print(f"  ♻️ 断点恢复：已完成 {len(done_keys)} 项，直接跳过")
        total_items = len(scenarios) * max(1, args.repeat) * 2
        for rnd in range(max(1, args.repeat)):
            if args.repeat > 1:
                print(f"\n  ── 第 {rnd+1}/{args.repeat} 轮 ──")
            for scenario in scenarios:
                for mode_flag, bucket, mtag in ((False, base_results, "b"),
                                                (True, disp_results, "d")):
                    if (scenario["name"], mtag, rnd) in done_keys:
                        continue
                    if scenario.get("type") == "sequence":
                        r = run_sequence(scenario, dispatcher_mode=mode_flag)
                    else:
                        r = run_scenario(scenario, dispatcher_mode=mode_flag)
                    r["_round"] = rnd
                    bucket.append(r)
                    _ab_save_ckpt(sig, base_results, disp_results)  # 每项落盘
                    done = len(base_results) + len(disp_results)
                    print(f"  [进度 {done}/{total_items}]")
        report = generate_ab_report(base_results, disp_results,
                                    repeat=args.repeat, git_commit=git_commit)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        ab_file = os.path.join(RESULTS_DIR, f"ab_{ts}.json")
        with open(ab_file, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        try:
            os.remove(AB_CKPT)  # 全部完成，清 checkpoint
        except OSError:
            pass
        if args.json:
            print(json.dumps(report["comparison"], ensure_ascii=False, indent=2))
        else:
            print_ab_report(report)
        print(f"\n  报告: {ab_file}")
        exit(0 if report["comparison"]["verdict"]["pass"] else 1)

    # ── 单模式（可指定 baseline/dispatcher，可 repeat）──
    dm_flag = {"baseline": False, "dispatcher": True}.get(args.mode)
    results = []
    for rnd in range(max(1, args.repeat)):
        if args.repeat > 1:
            print(f"\n  ── 第 {rnd+1}/{args.repeat} 轮 ──")
        for scenario in scenarios:
            if scenario.get("type") == "sequence":
                results.append(run_sequence(scenario, dispatcher_mode=dm_flag))
            else:
                results.append(run_scenario(scenario, dispatcher_mode=dm_flag))

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
