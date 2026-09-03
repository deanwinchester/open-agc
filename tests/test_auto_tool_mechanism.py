"""
Tests for the stage-6 auto-tool mechanism repair:
- usage recording -> graduation chain (3 consecutive successes -> permanent)
- trajectory classification (deterministic vs exploratory)
- pre-generation gate (plan_tool_generation): skip / reinforce / generate
- dedup via reinforce path
- prune_auto_tools archiving + load_all_dynamic_tools skipping _archive
"""
import json
import os
import sys
import time

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tools.auto_tool import (
    ARCHIVE_DIRNAME,
    TRUST_FILE,
    assess_reusability,
    check_graduation,
    classify_trajectory,
    graduate_tool,
    load_all_dynamic_tools,
    parse_tool_names,
    plan_tool_generation,
    prune_auto_tools,
    record_tool_reinforce,
    record_tool_usage,
    save_tool_code,
)


# ── Helpers ──

FAKE_TOOL_TEMPLATE = '''
TOOL_SCHEMA = {{
    "name": "{name}",
    "description": "fake tool {name}",
    "parameters": {{"type": "object", "properties": {{}}, "required": []}},
}}


def execute(**kwargs):
    return "ok"
'''


def _write_tool(tools_dir: str, name: str, age_days: float = 0) -> str:
    os.makedirs(tools_dir, exist_ok=True)
    path = os.path.join(tools_dir, f"{name}.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(FAKE_TOOL_TEMPLATE.format(name=name))
    if age_days > 0:
        old = time.time() - age_days * 86400
        os.utime(path, (old, old))
    return path


def _det_sequence(n_shell: int = 5, n_other: int = 1) -> str:
    lines = [f"→ execute_shell: cmd {i} ✅" for i in range(n_shell)]
    lines += ["→ write_file: out.txt ✅"] * n_other
    return "\n".join(lines)


def _exploratory_sequence() -> str:
    return "\n".join([
        "→ read_file: a.py ✅",
        "→ search_file_content: foo ✅",
        "→ read_file: b.py ✅",
        "→ find_files: *.py ✅",
        "→ list_dir: src ✅",
        "→ execute_shell: ls ✅",
    ])


class _FakeResponse:
    def __init__(self, content: str):
        self.choices = [type("C", (), {"message": type("M", (), {"content": content})()})()]


class FakeLLM:
    """Minimal llm_client stand-in: returns a fixed payload, counts calls."""

    def __init__(self, payload, raise_error: bool = False):
        self.payload = payload
        self.raise_error = raise_error
        self.calls = 0

    def chat(self, messages=None, **kwargs):
        self.calls += 1
        if self.raise_error:
            raise RuntimeError("llm down")
        content = self.payload if isinstance(self.payload, str) else json.dumps(self.payload)
        return _FakeResponse(content), None


# ── Trajectory classification ──

class TestTrajectoryClassification:
    def test_parse_tool_names(self):
        seq = "→ execute_shell: ls ✅\n→ read_file: a.py ❌\nNo tool calls"
        assert parse_tool_names(seq) == ["execute_shell", "read_file"]
        assert parse_tool_names("No tool calls") == []

    def test_deterministic_when_shell_python_dominant(self):
        assert classify_trajectory(_det_sequence(5, 1)) == "deterministic"
        # exactly 50% still counts as deterministic
        seq = "\n".join(["→ execute_shell: a", "→ execute_python: b",
                         "→ read_file: c", "→ write_file: d"])
        assert classify_trajectory(seq) == "deterministic"

    def test_exploratory_when_read_search_dominant(self):
        assert classify_trajectory(_exploratory_sequence()) == "exploratory"

    def test_exploratory_on_empty_or_unparseable(self):
        assert classify_trajectory("") == "exploratory"
        assert classify_trajectory("No tool calls") == "exploratory"


# ── Pre-generation gate ──

class TestGenerationGate:
    def test_blocks_exploratory_without_llm_call(self):
        llm = FakeLLM({"reusable": True, "suggested_name": "x"})
        plan = plan_tool_generation("task", _exploratory_sequence(), {}, llm)
        assert plan["action"] == "skip"
        assert plan["reason"] == "exploratory_trajectory"
        assert llm.calls == 0  # gate fires before any LLM call

    def test_blocks_too_few_calls_without_llm(self):
        llm = FakeLLM({"reusable": True})
        seq = "\n".join(["→ execute_shell: a", "→ execute_shell: b"])
        plan = plan_tool_generation("task", seq, {}, llm)
        assert plan["action"] == "skip"
        assert plan["reason"].startswith("too_few_calls")
        assert llm.calls == 0

    def test_skip_when_llm_says_not_reusable(self):
        llm = FakeLLM({"reusable": False, "reason": "one-off task"})
        plan = plan_tool_generation("task", _det_sequence(), {}, llm)
        assert plan["action"] == "skip"
        assert plan["reason"].startswith("not_reusable")
        assert llm.calls == 1

    def test_generate_when_reusable_and_no_overlap(self):
        llm = FakeLLM({"reusable": True, "reason": "generic",
                       "suggested_name": "new_tool", "overlap_with": None})
        plan = plan_tool_generation("task", _det_sequence(),
                                    {"other_tool": "does other things"}, llm)
        assert plan["action"] == "generate"
        assert plan["suggested_name"] == "new_tool"

    def test_reinforce_on_llm_overlap(self):
        llm = FakeLLM({"reusable": True, "reason": "dup",
                       "suggested_name": "whatever", "overlap_with": "existing_tool"})
        plan = plan_tool_generation("task", _det_sequence(),
                                    {"existing_tool": "already does this"}, llm)
        assert plan["action"] == "reinforce"
        assert plan["overlap_with"] == "existing_tool"

    def test_reinforce_when_suggested_name_exists(self):
        llm = FakeLLM({"reusable": True, "suggested_name": "existing_tool",
                       "overlap_with": None})
        plan = plan_tool_generation("task", _det_sequence(),
                                    {"existing_tool": "already there"}, llm)
        assert plan["action"] == "reinforce"
        assert plan["overlap_with"] == "existing_tool"

    def test_overlap_with_unknown_tool_still_generates(self):
        llm = FakeLLM({"reusable": True, "suggested_name": "fresh",
                       "overlap_with": "ghost_tool"})
        plan = plan_tool_generation("task", _det_sequence(), {}, llm)
        assert plan["action"] == "generate"

    def test_assess_reusability_fail_closed_on_error(self):
        llm = FakeLLM(None, raise_error=True)
        verdict = assess_reusability("task", _det_sequence(), {}, llm)
        assert verdict["reusable"] is False

    def test_assess_reusability_fail_closed_on_non_json(self):
        llm = FakeLLM("I think this is reusable!")
        verdict = assess_reusability("task", _det_sequence(), {}, llm)
        assert verdict["reusable"] is False


# ── Usage recording -> graduation chain ──

@pytest.fixture
def isolated_data_dir(tmp_path, monkeypatch):
    """Point core.paths at a temp base dir so graduation lands in a sandbox."""
    monkeypatch.setenv("OPEN_AGC_DATA_DIR", str(tmp_path))
    # Pre-create data/skills so get_skills_dir skips its bundled-skill seeding
    skills_dir = tmp_path / "data" / "skills"
    skills_dir.mkdir(parents=True)
    (skills_dir / "placeholder.md").write_text("x", encoding="utf-8")
    return tmp_path


class TestUsageToGraduation:
    def test_record_usage_counts_and_streak_reset(self, tmp_path):
        tools_dir = str(tmp_path / "auto_tools" / "1")
        record_tool_usage(tools_dir, "t", True)
        record_tool_usage(tools_dir, "t", True)
        entry = record_tool_usage(tools_dir, "t", False)
        assert entry["total"] == 3
        assert entry["successes"] == 2
        assert entry["failures"] == 1
        assert entry["consecutive"] == 0
        assert "last_used" in entry and "first_used" in entry
        assert os.path.exists(os.path.join(tools_dir, TRUST_FILE))

    def test_three_consecutive_successes_graduate(self, isolated_data_dir):
        tools_dir = str(isolated_data_dir / "auto_tools" / "7")
        _write_tool(tools_dir, "demo")

        for _ in range(2):
            record_tool_usage(tools_dir, "demo", True)
            assert not check_graduation(tools_dir, "demo")
        record_tool_usage(tools_dir, "demo", True)
        assert check_graduation(tools_dir, "demo")

        assert graduate_tool(tools_dir, "demo") is True
        permanent = isolated_data_dir / "data" / "skills" / "permanent"
        assert (permanent / "demo.py").exists()
        assert not os.path.exists(os.path.join(tools_dir, "demo.py"))
        # Graduated entries are not re-graduated
        assert not check_graduation(tools_dir, "demo")
        assert graduate_tool(tools_dir, "demo") is False

    def test_failure_streak_prevents_graduation(self, isolated_data_dir):
        tools_dir = str(isolated_data_dir / "auto_tools" / "8")
        _write_tool(tools_dir, "flaky")
        record_tool_usage(tools_dir, "flaky", True)
        record_tool_usage(tools_dir, "flaky", True)
        record_tool_usage(tools_dir, "flaky", False)
        record_tool_usage(tools_dir, "flaky", True)
        record_tool_usage(tools_dir, "flaky", True)
        assert not check_graduation(tools_dir, "flaky")  # streak is only 2
        record_tool_usage(tools_dir, "flaky", True)
        assert check_graduation(tools_dir, "flaky")


# ── Reinforce path (dedup) ──

class TestReinforce:
    def test_reinforce_records_usage_on_existing_tool(self, tmp_path):
        """The reinforce action bumps counters but never the success streak."""
        tools_dir = str(tmp_path / "auto_tools" / "1")
        _write_tool(tools_dir, "existing_tool")
        llm = FakeLLM({"reusable": True, "overlap_with": "existing_tool",
                       "suggested_name": "new_dup"})
        plan = plan_tool_generation("task", _det_sequence(),
                                    {"existing_tool": "desc"}, llm)
        assert plan["action"] == "reinforce"
        # Agent-side reinforce behavior (agent._reinforce_existing_tool)
        entry = record_tool_reinforce(tools_dir, plan["overlap_with"])
        assert entry["total"] == 1
        assert entry["reinforced"] == 1
        assert entry["consecutive"] == 0  # reinforce is not an execution
        # No new tool file was created
        assert not os.path.exists(os.path.join(tools_dir, "new_dup.py"))

    def test_reinforce_alone_never_graduates(self, tmp_path):
        """3 reinforces must NOT reach the graduation threshold."""
        tools_dir = str(tmp_path / "auto_tools" / "1")
        _write_tool(tools_dir, "overlap_tool")
        for _ in range(3):
            record_tool_reinforce(tools_dir, "overlap_tool")
        entry = record_tool_reinforce(tools_dir, "overlap_tool")
        assert entry["total"] == 4
        assert entry["reinforced"] == 4
        assert entry["consecutive"] == 0
        assert not check_graduation(tools_dir, "overlap_tool")


# ── Pruning ──

class TestPrune:
    def test_prune_archives_old_never_used_keeps_rest(self, tmp_path):
        tools_dir = str(tmp_path / "auto_tools" / "1")
        # old + never used -> archive
        _write_tool(tools_dir, "old_unused", age_days=45)
        # recent + never used -> keep (age gate)
        _write_tool(tools_dir, "recent_unused", age_days=5)
        # old but used (trust) -> keep (call-count gate)
        _write_tool(tools_dir, "old_used", age_days=45)
        record_tool_usage(tools_dir, "old_used", True)

        result = prune_auto_tools(tools_dir, max_age_days=30, min_calls=1)
        assert result["archived"] == ["old_unused"]
        assert sorted(result["kept"]) == ["old_used", "recent_unused"]

        # Archived file moved, not deleted
        archived = os.path.join(tools_dir, ARCHIVE_DIRNAME, "old_unused.py")
        assert os.path.exists(archived)
        assert not os.path.exists(os.path.join(tools_dir, "old_unused.py"))

    def test_prune_uses_trust_last_used_over_mtime(self, tmp_path):
        tools_dir = str(tmp_path / "auto_tools" / "2")
        # File is old, but trust says it was called just now -> keep
        _write_tool(tools_dir, "active", age_days=90)
        record_tool_usage(tools_dir, "active", True)
        result = prune_auto_tools(tools_dir, max_age_days=30, min_calls=1)
        assert result["kept"] == ["active"]

    def test_prune_stale_even_if_used_long_ago(self, tmp_path):
        tools_dir = str(tmp_path / "auto_tools" / "3")
        _write_tool(tools_dir, "once_used", age_days=10)
        # Used once, but 60 days ago (rewrite trust with an old last_used)
        record_tool_usage(tools_dir, "once_used", True)
        trust_path = os.path.join(tools_dir, TRUST_FILE)
        with open(trust_path, encoding="utf-8") as f:
            trust = json.load(f)
        from datetime import datetime, timedelta
        trust["once_used"]["last_used"] = (
            datetime.now() - timedelta(days=60)).astimezone().isoformat()
        with open(trust_path, "w", encoding="utf-8") as f:
            json.dump(trust, f)
        # min_calls=2: single call is below the bar AND stale -> archive
        result = prune_auto_tools(tools_dir, max_age_days=30, min_calls=2)
        assert result["archived"] == ["once_used"]
        # min_calls=1: call count meets the bar -> keep
        _write_tool(tools_dir, "also_once", age_days=10)
        record_tool_usage(tools_dir, "also_once", True)
        with open(trust_path, encoding="utf-8") as f:
            trust = json.load(f)
        trust["also_once"]["last_used"] = (
            datetime.now() - timedelta(days=60)).astimezone().isoformat()
        with open(trust_path, "w", encoding="utf-8") as f:
            json.dump(trust, f)
        result = prune_auto_tools(tools_dir, max_age_days=30, min_calls=1)
        assert result["kept"] == ["also_once"]

    def test_prune_empty_or_missing_dir(self, tmp_path):
        assert prune_auto_tools(str(tmp_path / "nope")) == {"kept": [], "archived": []}

    def test_prune_skips_archive_and_trust_file(self, tmp_path):
        tools_dir = str(tmp_path / "auto_tools" / "4")
        _write_tool(tools_dir, "keepme")
        record_tool_usage(tools_dir, "keepme", True)
        os.makedirs(os.path.join(tools_dir, ARCHIVE_DIRNAME), exist_ok=True)
        _write_tool(os.path.join(tools_dir, ARCHIVE_DIRNAME), "already_archived",
                    age_days=100)
        result = prune_auto_tools(tools_dir, max_age_days=30, min_calls=1)
        assert result["kept"] == ["keepme"]
        assert result["archived"] == []
        # archive content untouched
        assert os.path.exists(os.path.join(tools_dir, ARCHIVE_DIRNAME,
                                           "already_archived.py"))


# ── Loading skips _archive ──

class TestLoadSkipsArchive:
    def test_load_all_dynamic_tools_skips_archive(self, tmp_path):
        tools_dir = str(tmp_path / "auto_tools" / "1")
        _write_tool(tools_dir, "live_tool")
        archive = os.path.join(tools_dir, ARCHIVE_DIRNAME)
        os.makedirs(archive, exist_ok=True)
        _write_tool(archive, "dead_tool")

        loaded = load_all_dynamic_tools(tools_dir)
        assert "live_tool" in loaded
        assert "dead_tool" not in loaded

    def test_prune_then_load_no_longer_picks_up_archived(self, tmp_path):
        tools_dir = str(tmp_path / "auto_tools" / "2")
        _write_tool(tools_dir, "stale", age_days=60)
        _write_tool(tools_dir, "fresh")
        assert set(load_all_dynamic_tools(tools_dir)) == {"stale", "fresh"}

        prune_auto_tools(tools_dir, max_age_days=30, min_calls=1)
        assert set(load_all_dynamic_tools(tools_dir)) == {"fresh"}


# ── Agent-level wiring: generation -> same-session call -> trust record ──

import queue
import types

from agent.agent import OpenAGCAgent
from tools.auto_tool import init_auto_tools


class _ScriptedLLM:
    """Scripted LLM client: pops one item per chat() call."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.default_model = "stub-model"

    def chat(self, messages=None, tools=None, interrupt_check=None):
        self.calls.append({"messages": messages, "tools": tools})
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, "stub-model"


def _resp(content=None, tool_calls=None):
    msg = types.SimpleNamespace(role="assistant", content=content,
                                tool_calls=tool_calls)
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=msg)], usage=None)


def _tool_call(name, arguments):
    return types.SimpleNamespace(
        id="call_1", type="function",
        function=types.SimpleNamespace(name=name, arguments=json.dumps(arguments)))


_GENERATED_CODE = '''
TOOL_SCHEMA = {
    "name": "demo_echo",
    "description": "return a fixed greeting",
    "parameters": {"type": "object", "properties": {}, "required": []},
}


def execute(**kwargs):
    return "echo-ok"
'''


def _bare_agent_for_auto_tool(session_dir: str, llm) -> OpenAGCAgent:
    """Bare OpenAGCAgent (no heavy __init__) with just the auto-tool wiring attrs."""
    agent = OpenAGCAgent.__new__(OpenAGCAgent)
    agent.session_id = 7
    agent.failed_attempts = []
    agent.messages = [{"role": "system", "content": "sys"}]
    agent.logger = None
    agent.llm = llm
    agent.pending_messages = []
    agent._processing_interjection = False
    agent._interjection_stuck_count = 0
    agent._rejected_interjection = None
    agent._in_self_review = False
    agent._max_correction_attempts = 0
    agent.tool_schemas = []
    agent.tool_display_names = {}
    agent.available_tools = {}
    agent.full_available_tools = {}
    agent._auto_tools_dir = session_dir
    agent._dynamic_tool_dirs = {}
    agent._session_sandbox_whitelist = set()
    agent._session_network_whitelist = set()
    agent._session_permission_whitelist = set()
    agent._pending_sudo_password = ""
    agent._session_sudo_password = ""
    agent.reflection_engine = None
    agent.knowledge_graph = types.SimpleNamespace(
        extract_from_messages=lambda msgs: None)
    agent.skill_store = types.SimpleNamespace(refresh=lambda: None)
    agent._save_task_stats = lambda *a, **k: None
    agent._should_delegate = lambda text: False
    agent.user_input_queue = queue.Queue()
    agent.progress_callback = None
    agent._build_system_prompt = lambda **kwargs: "sys"
    return agent


class TestAgentAutoToolWiring:
    def test_generated_tool_callable_same_session_and_records_usage(
            self, tmp_path, monkeypatch):
        """Full chain: _auto_generate_tool -> register -> run_turn executes it
        -> tool_done records usage into the session dir's _trust.json."""
        monkeypatch.setenv("OPEN_AGC_DATA_DIR", str(tmp_path))
        session_dir = str(tmp_path / "auto_tools" / "7")
        os.makedirs(session_dir, exist_ok=True)
        init_auto_tools(session_dir)  # load-path init, mirrors agent startup

        # Phase 1: generation — assess(reusable) then code
        gen_llm = _ScriptedLLM([
            _resp(content=json.dumps({"reusable": True, "reason": "generic",
                                      "suggested_name": "demo_echo",
                                      "overlap_with": None})),
            _resp(content=_GENERATED_CODE),
        ])
        agent = _bare_agent_for_auto_tool(session_dir, gen_llm)

        tool_name = agent._auto_generate_tool(
            "做一件可复用的事", {"tool_sequence": _det_sequence()}, agent.llm)

        assert tool_name == "demo_echo"
        # Critical fix: registered into BOTH tool dicts in the birth session
        assert "demo_echo" in agent.available_tools
        assert "demo_echo" in agent.full_available_tools
        assert agent._dynamic_tool_dirs["demo_echo"] == session_dir
        assert os.path.exists(os.path.join(session_dir, "demo_echo.py"))
        # No usage recorded yet
        assert not os.path.exists(os.path.join(session_dir, TRUST_FILE))

        # Phase 2: same-session execution through the real run_turn loop —
        # LLM asks for the tool once, then answers with text.
        agent.llm = _ScriptedLLM([
            _resp(tool_calls=[_tool_call("demo_echo", {})]),
            _resp(content="完成了"),
        ])
        result = agent.run_turn("调用 demo_echo", verbose=False, skip_rag=True)

        assert "完成了" in result
        # tool_done path resolved the dynamic tool via full_available_tools and
        # recorded usage beside the tool (session dir trust file)
        trust_path = os.path.join(session_dir, TRUST_FILE)
        assert os.path.exists(trust_path), "usage recording never fired"
        with open(trust_path, encoding="utf-8") as f:
            trust = json.load(f)
        assert trust["demo_echo"]["total"] == 1
        assert trust["demo_echo"]["consecutive"] == 1


# ── Trust-file locking: concurrent writers lose no updates ──

import threading


class TestTrustFileLocking:
    def test_concurrent_record_and_reinforce_no_lost_updates(self, tmp_path):
        """Threads racing RMW cycles on the same _trust.json must not drop writes.

        Writers: usage records (main-loop side) + reinforce signals
        (post-process-worker side). Invariant: total == successes + failures
        + reinforced, and every write is accounted for.
        """
        tools_dir = str(tmp_path / "auto_tools" / "1")
        os.makedirs(tools_dir, exist_ok=True)
        ops_per_thread = 50
        n_threads = 4
        errors = []

        def worker(i):
            try:
                for _ in range(ops_per_thread):
                    if i % 2 == 0:
                        record_tool_usage(tools_dir, "shared_tool", True)
                    else:
                        record_tool_reinforce(tools_dir, "shared_tool")
            except Exception as e:  # pragma: no cover - failure path
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,))
                   for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        with open(os.path.join(tools_dir, TRUST_FILE), encoding="utf-8") as f:
            entry = json.load(f)["shared_tool"]
        expected = ops_per_thread * n_threads
        assert entry["total"] == expected
        assert (entry["successes"] + entry["failures"]
                + entry.get("reinforced", 0)) == expected


# ── save_tool_code: explicit tools_dir, never the module global ──

class TestSaveToolCodeExplicitDir:
    def test_alternating_sessions_save_into_their_own_dirs(self, tmp_path):
        """Two 'sessions' interleaving init_auto_tools must still each save into
        the directory they explicitly pass — the last init must not win."""
        dir_a = str(tmp_path / "auto_tools" / "A")
        dir_b = str(tmp_path / "auto_tools" / "B")
        code_a = FAKE_TOOL_TEMPLATE.format(name="tool_for_a")
        code_b = FAKE_TOOL_TEMPLATE.format(name="tool_for_b")

        init_auto_tools(dir_a)
        init_auto_tools(dir_b)  # global now points at B
        path_a = save_tool_code(code_a, "tool_for_a", dir_a)
        init_auto_tools(dir_a)  # global flips back to A
        path_b = save_tool_code(code_b, "tool_for_b", dir_b)

        assert path_a == os.path.join(dir_a, "tool_for_a.py")
        assert path_b == os.path.join(dir_b, "tool_for_b.py")
        assert os.path.exists(os.path.join(dir_a, "tool_for_a.py"))
        assert os.path.exists(os.path.join(dir_b, "tool_for_b.py"))
        # No cross-contamination via the stale global
        assert not os.path.exists(os.path.join(dir_b, "tool_for_a.py"))
        assert not os.path.exists(os.path.join(dir_a, "tool_for_b.py"))

    def test_save_tool_code_empty_dir_returns_none(self):
        assert save_tool_code("TOOL_SCHEMA = {}\n", "x", "") is None
