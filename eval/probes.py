"""
Advanced evaluation probes — measure real execution path quality.

These probes run via the REST API (same path as web UI) and measure
aspects the basic runner can't: memory precision/recall, progressive
disclosure activation, long-context retention, and tool choice quality.

Isolation rules (阶段4 Task5):
  - probe_memory_recall seeds its 7 test memories into a TEMPORARY database
    that is deleted on exit — the production data/memory.db is never touched
    unless you explicitly pass db_path= (seeded rows are then deleted again).
    The agent under test is constructed with memory_db_path pointing at that
    same database, so recall is measured against the seeded ground truth.
  - Probes whose tasks mutate the real environment (filesystem writes,
    package installs, memory writes — see SIDE_EFFECT_PROBES) are SKIPPED
    unless explicitly enabled with allow_side_effects=True.
"""
import sys, os, json, time, re, shutil, tempfile, urllib.request, urllib.parse
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Probes that execute agent tasks with real-environment side effects
# (write/edit files, install packages, write memories). Skipped by default.
SIDE_EFFECT_PROBES = ("context_retention", "tool_choice_quality", "response_quality")


def api_query(query, session_id=9999, base_url="http://127.0.0.1:8000"):
    """Send a query via the WebSocket-compatible REST path and get response."""
    # Use the task API endpoint (same backend as web UI)
    data = json.dumps({"query": query, "session_id": session_id}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/api/query",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        resp = urllib.request.urlopen(req, timeout=120)
        return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)}


# ── Probe 1: Memory recall precision/recall ──

def probe_memory_recall(session_id=9998, db_path=None):
    """Measure memory recall quality: precision, recall, F1.

    Measurement semantics: the 7 test memories are seeded into the SAME
    database the agent under test reads — the agent is constructed with
    ``OpenAGCAgent(memory_db_path=db_path)`` so its MemoryStore/MemoryTool
    point at the seeded store. Recall is therefore measured against the
    seeded ground truth, not the production memories.

    Isolation: by default that database is a TEMPORARY file (tempfile)
    deleted on exit — the production data/memory.db is never touched.
    Pass ``db_path`` explicitly (e.g. get_data_path("memory.db")) only for
    a full end-to-end run against the real store; the seeded rows are
    deleted again in the cleanup phase.
    """
    from core.memory_store import MemoryStore
    from agent.agent import OpenAGCAgent

    tmp_dir = None
    store = None
    seeded_ids = []
    if db_path is None:
        tmp_dir = tempfile.mkdtemp(prefix="openagc_probe_mem_")
        db_path = os.path.join(tmp_dir, "memory.db")

    try:
        store = MemoryStore(db_path=db_path, session_id=session_id)

        # Step 1: Seed test memories with known ground truth
        test_memories = [
            ("用户的出生地是北京", "user_pref", "core"),
            ("用户最喜欢的颜色是蓝色", "user_pref", "core"),
            ("用户养了一只橘猫叫小橘", "user_pref", "core"),
            ("用户的工作是软件工程师", "user_pref", "core"),
            ("用户用的是Windows 11系统", "tech", "core"),
            ("用户的显卡是RTX 4090", "tech", "core"),
            ("用户常用IDE是VS Code", "tech", "episode"),
        ]
        for content, cat, mtype in test_memories:
            seeded_ids.append(store.add_memory(content, category=cat, memory_type=mtype))

        # Step 2: Query the agent — it reads the SAME (seeded) db via injection
        agent = OpenAGCAgent(memory_db_path=db_path)
        response = agent.run_turn("你还记得哪些关于我的个人信息？", verbose=False)

        # Step 3: Calculate recall metrics
        expected_facts = {"北京", "蓝色", "橘猫", "小橘", "软件工程师", "Windows", "RTX 4090", "VS Code"}
        found_facts = set()
        for fact in expected_facts:
            if fact in response:
                found_facts.add(fact)

        # Also check for hallucinated facts (things not stored)
        hallucinated = set()
        possible_hallucinations = {"上海", "红色", "小狗", "设计师", "MacOS", "RTX 3080", "PyCharm", "Linux"}
        for h in possible_hallucinations:
            if h in response and h not in expected_facts:
                hallucinated.add(h)

        true_positives = len(found_facts)
        false_positives = len(hallucinated)
        false_negatives = len(expected_facts - found_facts)

        precision = true_positives / (true_positives + false_positives) if (true_positives + false_positives) > 0 else 0
        recall = true_positives / (true_positives + false_negatives) if (true_positives + false_negatives) > 0 else 0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0

        return {
            "probe": "memory_recall",
            "true_positives": true_positives,
            "false_positives": false_positives,
            "false_negatives": false_negatives,
            "precision": round(precision, 3),
            "recall": round(recall, 3),
            "f1": round(f1, 3),
            "found": list(found_facts),
            "hallucinated": list(hallucinated),
            "expected_count": len(expected_facts),
            "isolated_db": tmp_dir is not None,
        }
    finally:
        # Cleanup: never leave probe data behind.
        if tmp_dir is None and store is not None and seeded_ids:
            # Explicit (e.g. production) db_path: remove just the seeded rows.
            try:
                for mid in seeded_ids:
                    store.delete_memory(mid)
            except Exception as _clean_e:
                print(f"[Probe] Seed cleanup error: {_clean_e}")
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)


# ── Probe 2: Progressive disclosure activation ──

def probe_tool_discovery(session_id=9997):
    """Verify progressive disclosure: core tools available, deferred tools need search."""
    from agent.agent import OpenAGCAgent
    import json

    agent = OpenAGCAgent()

    # Check which tools are loaded by default (core)
    core_tools = set(agent.active_tool_names)
    deferred_tools = set(agent.full_available_tools.keys()) - core_tools

    result = {
        "probe": "tool_discovery",
        "core_tool_count": len(core_tools),
        "deferred_tool_count": len(deferred_tools),
        "core_tools": sorted(core_tools)[:20],  # sample
        "deferred_tools_sample": sorted(deferred_tools)[:10],
        "search_available_tools_loaded": "search_available_tools" in core_tools,
    }

    # Verify critical core tools are always present
    essentials = {"execute_shell", "read_file", "write_file", "edit_file",
                  "search_file_content", "find_files", "manage_memory",
                  "search_web", "execute_python"}
    missing_essentials = essentials - core_tools
    result["essential_tools_missing"] = list(missing_essentials) if missing_essentials else []
    result["essential_tools_all_present"] = len(missing_essentials) == 0

    return result


# ── Probe 3: Long-context retention ──

def probe_context_retention(session_id=9996, allow_side_effects=False):
    """Measure how well the agent retains information across a long conversation.

    Simulates a multi-turn interaction by feeding sequential messages
    and checking if earlier information is retained in later turns.

    Side effects: the injected "请记住：…" turns make the real agent write
    memories into the production memory.db. Skipped unless
    ``allow_side_effects=True``.
    """
    if not allow_side_effects:
        return {
            "probe": "context_retention",
            "skipped": True,
            "reason": "real-environment side effects (memory writes); "
                      "pass allow_side_effects=True to enable",
        }
    from agent.agent import OpenAGCAgent

    agent = OpenAGCAgent()
    facts = [
        "会议时间定在周三下午3点",
        "项目代号是 Project Phoenix",
        "预算限制在5万元以内",
        "客户联系人姓张",
        "交付截止日期是下个月15号",
    ]
    injected = []

    # Phase 1: Inject facts in separate turns
    for fact in facts:
        resp = agent.run_turn(f"请记住：{fact}")
        injected.append(fact)

    # Phase 2: Query retention of early facts after more turns
    resp = agent.run_turn("之前让你记的那些信息，还记得多少？")

    # Check recall
    early_facts = {"周三", "3点", "Phoenix", "5万", "张", "15号"}
    found = {f for f in early_facts if f in resp}
    missed = early_facts - found

    # Phase 3: Test for hallucinated details
    distractors = {"周四", "2点", "Alpha", "10万", "李", "20号"}
    hallucinated = {f for f in distractors if f in resp and f not in early_facts}

    recall_rate = len(found) / len(early_facts)
    precision = len(found) / (len(found) + len(hallucinated)) if (len(found) + len(hallucinated)) > 0 else 1.0

    return {
        "probe": "context_retention",
        "facts_injected": len(facts),
        "facts_recalled": len(found),
        "facts_missed": len(missed),
        "hallucinated_facts": len(hallucinated),
        "recall_rate": round(recall_rate, 2),
        "precision": round(precision, 2),
        "missed_details": list(missed),
        "hallucinated_details": list(hallucinated),
    }


# ── Probe 4: Tool choice quality ──

def probe_tool_choice_quality(session_id=9995, allow_side_effects=False):
    """Evaluate whether the agent chooses the right tool for each task.

    Measures: correct tool first attempt, unnecessary tool switches,
    and task completion efficiency.

    Side effects: several tasks ("修改文件内容", "安装Python包", …) make the
    real agent edit files / install packages in the real environment.
    Skipped unless ``allow_side_effects=True``.
    """
    if not allow_side_effects:
        return {
            "probe": "tool_choice_quality",
            "skipped": True,
            "reason": "real-environment side effects (file edits, package installs); "
                      "pass allow_side_effects=True to enable",
        }
    from agent.agent import OpenAGCAgent

    tasks = [
        ("列出文件", ["execute_shell", "find_files"]),
        ("读取config.json", ["read_file"]),
        ("修改文件内容", ["edit_file", "write_file"]),
        ("搜索Python文件中的class定义", ["search_file_content", "execute_shell"]),
        ("安装Python包", ["execute_shell"]),
    ]

    results = []
    correct_first = 0
    total_steps = 0

    for desc, expected_tools in tasks:
        agent = OpenAGCAgent()
        response = agent.run_turn(desc, verbose=False)

        try:
            # Find first tool call
            first_tool = None
            all_tools = []
            for msg in agent.messages:
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    for tc in msg["tool_calls"]:
                        fn = tc.get("function", {})
                        name = fn.get("name", "")
                        if not first_tool:
                            first_tool = name
                        all_tools.append(name)

            correct = first_tool in expected_tools if first_tool else False
            if correct:
                correct_first += 1
            total_steps += len(set(all_tools))

            results.append({
                "task": desc,
                "expected": expected_tools,
                "first_tool": first_tool,
                "all_tools": all_tools[:10],
                "correct_first_choice": correct,
            })
        except Exception as e:
            results.append({"task": desc, "error": str(e)})

    accuracy = correct_first / len(tasks)

    return {
        "probe": "tool_choice_quality",
        "total_tasks": len(tasks),
        "correct_first_choice": correct_first,
        "accuracy": round(accuracy, 3),
        "details": results,
    }


# ── Probe 5: Response quality (conciseness, helpfulness) ──

def probe_response_quality(session_id=9994, allow_side_effects=False):
    """Measure response quality metrics: length, structure, code inclusion.

    Side effects: tasks like "帮我创建一个简单的HTML页面" make the real
    agent write files into the workspace. Skipped unless
    ``allow_side_effects=True``.
    """
    if not allow_side_effects:
        return {
            "probe": "response_quality",
            "skipped": True,
            "reason": "real-environment side effects (workspace file writes); "
                      "pass allow_side_effects=True to enable",
        }
    from agent.agent import OpenAGCAgent

    queries = [
        "当前目录下有哪些文件？",
        "用Python生成一个斐波那契数列的前20个数字",
        "帮我创建一个简单的HTML页面",
    ]

    results = []
    total_response_length = 0
    code_block_count = 0

    for q in queries:
        agent = OpenAGCAgent()
        response = agent.run_turn(q, verbose=False)

        # Count markdown code blocks
        code_blocks = len(re.findall(r'```', response)) // 2
        tool_call_count = sum(
            1 for msg in agent.messages
            if msg.get("role") == "assistant" and msg.get("tool_calls")
        )

        results.append({
            "query": q[:30],
            "response_length": len(response),
            "code_blocks": code_blocks,
            "tool_calls": tool_call_count,
            "has_error_response": "error" in response.lower(),
        })
        total_response_length += len(response)
        code_block_count += code_blocks

    return {
        "probe": "response_quality",
        "queries": len(queries),
        "avg_response_length": round(total_response_length / len(queries)),
        "total_code_blocks": code_block_count,
        "details": results,
    }


# ── Probe 6: REST API execution path ──

def probe_rest_api_execution(base_url="http://127.0.0.1:8000"):
    """Test the real web execution path through the REST API.

    Requires the server to be running (python -m uvicorn api.server:app).
    Tests that the WebSocket-compatible API endpoint processes queries correctly.
    """
    import urllib.request, urllib.error

    health_check = f"{base_url}/"
    try:
        resp = urllib.request.urlopen(health_check, timeout=5)
        html = resp.read().decode("utf-8")
        server_running = resp.status == 200 and "Open-AGC" in html
    except Exception as e:
        return {
            "probe": "rest_api",
            "server_running": False,
            "error": f"Server not reachable: {e}",
            "message": "Start server with: python -m uvicorn api.server:app",
        }

    # If server is running, test a query
    result = api_query("列出当前目录文件", base_url=base_url)
    success = "error" not in result

    return {
        "probe": "rest_api",
        "server_running": True,
        "query_success": success,
        "response_preview": str(result)[:200] if success else result.get("error"),
    }


# ── Run all probes ──

def run_all_probes(include_rest_api=False, allow_side_effects=False):
    """Run all advanced probes and return aggregated results.

    Probes with real-environment side effects (SIDE_EFFECT_PROBES) are
    skipped unless ``allow_side_effects=True``.
    """
    print(f"  {'='*55}")
    print(f"  🔬 Advanced Probes")
    print(f"  {'='*55}")

    probes = []

    def _skipped(r):
        if r.get("skipped"):
            print(f"  ⏭️  Skipped (side effects) — enable with allow_side_effects=True")
            return True
        return False

    print(f"\n  ── Probe 1: Memory Recall (precision/recall/F1) ──")
    try:
        r = probe_memory_recall()
        probes.append(r)
        print(f"  Precision={r['precision']:.1%} Recall={r['recall']:.1%} F1={r['f1']:.1%}")
        print(f"  TP={r['true_positives']} FP={r['false_positives']} FN={r['false_negatives']}")
        if r.get("isolated_db"):
            print(f"  🛡️  Isolated temp DB (production memory.db untouched)")
        if r['hallucinated']:
            print(f"  ⚠️  Hallucinated: {r['hallucinated']}")
    except Exception as e:
        probes.append({"probe": "memory_recall", "error": str(e)})
        print(f"  ❌ Error: {e}")

    print(f"\n  ── Probe 2: Progressive Disclosure ──")
    try:
        r = probe_tool_discovery()
        probes.append(r)
        print(f"  Core tools: {r['core_tool_count']}, Deferred: {r['deferred_tool_count']}")
        print(f"  Essentials all present: {r['essential_tools_all_present']}")
        if not r['essential_tools_all_present']:
            print(f"  ⚠️  Missing: {r['essential_tools_missing']}")
    except Exception as e:
        probes.append({"probe": "tool_discovery", "error": str(e)})
        print(f"  ❌ Error: {e}")

    print(f"\n  ── Probe 3: Long-context Retention ──")
    try:
        r = probe_context_retention(allow_side_effects=allow_side_effects)
        probes.append(r)
        if not _skipped(r):
            print(f"  Recall rate: {r['recall_rate']:.0%} ({r['facts_recalled']}/{r['facts_injected']})")
            print(f"  Hallucinated: {r['hallucinated_facts']}")
            if r['missed_details']:
                print(f"  Missed: {r['missed_details']}")
    except Exception as e:
        probes.append({"probe": "context_retention", "error": str(e)})
        print(f"  ❌ Error: {e}")

    print(f"\n  ── Probe 4: Tool Choice Quality ──")
    try:
        r = probe_tool_choice_quality(allow_side_effects=allow_side_effects)
        probes.append(r)
        if not _skipped(r):
            print(f"  Correct first choice: {r['correct_first_choice']}/{r['total_tasks']} ({r['accuracy']:.0%})")
            for d in r['details']:
                mark = "✅" if d.get('correct_first_choice') else "❌"
                print(f"  {mark} {d['task']}: first={d.get('first_tool')} expected={d.get('expected')}")
    except Exception as e:
        probes.append({"probe": "tool_choice_quality", "error": str(e)})
        print(f"  ❌ Error: {e}")

    print(f"\n  ── Probe 5: Response Quality ──")
    try:
        r = probe_response_quality(allow_side_effects=allow_side_effects)
        probes.append(r)
        if not _skipped(r):
            print(f"  Avg response length: {r['avg_response_length']} chars")
            print(f"  Code blocks: {r['total_code_blocks']}")
    except Exception as e:
        probes.append({"probe": "response_quality", "error": str(e)})
        print(f"  ❌ Error: {e}")

    if include_rest_api:
        print(f"\n  ── Probe 6: REST API (requires running server) ──")
        try:
            r = probe_rest_api_execution()
            probes.append(r)
            if r.get("server_running"):
                print(f"  Server: ✅ | Query: {'✅' if r.get('query_success') else '❌'}")
            else:
                print(f"  Server: ❌ ({r.get('error', '')})")
        except Exception as e:
            probes.append({"probe": "rest_api", "error": str(e)})
            print(f"  ❌ Error: {e}")

    # Aggregate (skipped probes count as neither passed nor failed)
    passed = sum(1 for p in probes if "error" not in p and not p.get("skipped"))
    skipped = sum(1 for p in probes if p.get("skipped"))
    total = len(probes)

    print(f"\n  {'='*55}")
    print(f"  Probes: {passed}/{total - skipped} passed" +
          (f" ({skipped} skipped)" if skipped else ""))
    print(f"  {'='*55}")

    return {
        "timestamp": datetime.now().isoformat(),
        "total_probes": total,
        "passed_probes": passed,
        "skipped_probes": skipped,
        "results": probes,
    }
