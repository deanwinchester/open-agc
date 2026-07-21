"""阶段4 Task5: eval probes must be isolated from the production environment.

  1. probe_memory_recall seeds into a TEMP db by default — production
     data/memory.db row count is unchanged, and the temp dir is deleted.
  2. probe_memory_recall with an explicit db_path deletes the seeded rows.
  3. Side-effect probes (context_retention / tool_choice_quality /
     response_quality) are skipped unless allow_side_effects=True.
  4. run_all_probes reports skipped probes and excludes them from the
     passed count.

OpenAGCAgent is mocked — no LLM calls, no API keys needed.
"""
import os
import sqlite3
import sys
import tempfile
from unittest import mock

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

_HAS_LITELLM = False
try:
    import litellm  # noqa: F401
    _HAS_LITELLM = True
except ImportError:
    pass

pytestmark = pytest.mark.skipif(not _HAS_LITELLM,
                                reason="requires litellm (agent.agent import)")


def _count_memories(db_path):
    if not os.path.exists(db_path):
        return 0
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    except sqlite3.OperationalError:  # no memories table yet
        return 0
    finally:
        conn.close()


def _fake_agent(response="我记得你住在北京，最喜欢蓝色。"):
    agent = mock.MagicMock()
    agent.run_turn.return_value = response
    agent.active_tool_names = {"read_file", "execute_shell"}
    agent.full_available_tools = {"read_file": object(), "execute_shell": object()}
    return agent


def test_probe_memory_recall_leaves_production_db_untouched(monkeypatch):
    from core.paths import get_data_path
    from eval import probes

    prod_db = get_data_path("memory.db")
    before = _count_memories(prod_db)

    created_dirs = []
    real_mkdtemp = tempfile.mkdtemp

    def spy_mkdtemp(*args, **kwargs):
        d = real_mkdtemp(*args, **kwargs)
        created_dirs.append(d)
        return d

    monkeypatch.setattr(tempfile, "mkdtemp", spy_mkdtemp)

    with mock.patch("agent.agent.OpenAGCAgent", return_value=_fake_agent()):
        result = probes.probe_memory_recall()

    # Production row count unchanged (7 test memories went to the temp db)
    assert _count_memories(prod_db) == before
    # Temp db was used and deleted afterwards
    assert created_dirs, "probe should have created a temp dir"
    assert not os.path.exists(created_dirs[0])
    # Probe still produced metrics from the (mocked) agent response
    assert result["probe"] == "memory_recall"
    assert result["isolated_db"] is True
    assert set(result["found"]) == {"北京", "蓝色"}


def test_probe_memory_recall_explicit_db_path_cleans_seeded_rows(tmp_path):
    from eval import probes

    db_path = str(tmp_path / "memory.db")
    with mock.patch("agent.agent.OpenAGCAgent", return_value=_fake_agent()):
        result = probes.probe_memory_recall(db_path=db_path)

    assert result["isolated_db"] is False
    # The 7 seeded rows were deleted again — nothing left behind
    assert _count_memories(db_path) == 0


def test_side_effect_probes_skipped_by_default():
    from eval import probes

    for fn, name in [
        (probes.probe_context_retention, "context_retention"),
        (probes.probe_tool_choice_quality, "tool_choice_quality"),
        (probes.probe_response_quality, "response_quality"),
    ]:
        result = fn()
        assert result["probe"] == name
        assert result["skipped"] is True
        assert "allow_side_effects" in result["reason"]
    # And the constant documents exactly which probes are gated
    assert set(probes.SIDE_EFFECT_PROBES) == {
        "context_retention", "tool_choice_quality", "response_quality"}


def test_side_effect_probe_runs_when_explicitly_enabled():
    from eval import probes

    with mock.patch("agent.agent.OpenAGCAgent", return_value=_fake_agent("")):
        result = probes.probe_tool_choice_quality(allow_side_effects=True)
    assert "skipped" not in result
    assert result["probe"] == "tool_choice_quality"
    assert result["total_tasks"] > 0


def test_run_all_probes_marks_and_counts_skips():
    from eval import probes

    with mock.patch("agent.agent.OpenAGCAgent", return_value=_fake_agent()):
        out = probes.run_all_probes()

    by_name = {p["probe"]: p for p in out["results"]}
    # Side-effect probes skipped, isolation-safe probes ran
    assert by_name["tool_choice_quality"]["skipped"] is True
    assert by_name["context_retention"]["skipped"] is True
    assert by_name["response_quality"]["skipped"] is True
    assert "error" not in by_name["memory_recall"]
    assert "error" not in by_name["tool_discovery"]
    # Skipped probes count as neither passed nor failed
    assert out["skipped_probes"] == 3
    assert out["passed_probes"] == 2


# ── Review fix: the isolated probe must actually MEASURE recall ──

EXPECTED_SEED_FACTS = {"北京", "蓝色", "橘猫", "小橘", "软件工程师",
                       "Windows", "RTX 4090", "VS Code"}


def test_probe_recall_reads_seeded_facts_from_isolated_db():
    """Regression (review): seeds went to the temp db but the agent under
    test read the production db — recall was a guaranteed false negative.
    The probe must inject the SAME db path into the agent."""
    from core.memory_store import MemoryStore
    from core.paths import get_data_path
    from eval import probes

    captured = {}

    def make_fake_agent(*args, **kwargs):
        captured.update(kwargs)
        agent = mock.MagicMock()

        def run_turn(query, verbose=False):
            # Read the injected store the way the real agent's memory
            # retrieval would — seeds are found only if injection worked.
            store = MemoryStore(db_path=kwargs.get("memory_db_path")
                                or get_data_path("memory.db"))
            return "；".join(m["content"] for m in store.get_all_memories())

        agent.run_turn.side_effect = run_turn
        return agent

    with mock.patch("agent.agent.OpenAGCAgent", side_effect=make_fake_agent):
        result = probes.probe_memory_recall()

    # The probe injected the isolated temp db into the agent under test
    assert captured.get("memory_db_path")
    assert captured["memory_db_path"] != get_data_path("memory.db")
    # All 8 seeded facts genuinely recalled from that isolated db
    assert set(result["found"]) == EXPECTED_SEED_FACTS
    assert result["recall"] == 1.0
    assert result["precision"] == 1.0


def test_agent_memory_db_path_injection_and_default(tmp_path):
    """OpenAGCAgent(memory_db_path=...) wires the injected db into BOTH the
    agent's memory_store and the manage_memory tool; omitting it keeps the
    production default (get_data_path('memory.db'))."""
    from agent.agent import OpenAGCAgent
    from core.paths import get_data_path

    injected_path = str(tmp_path / "memory.db")
    agent = OpenAGCAgent(memory_db_path=injected_path)
    assert agent.memory_store.db_path == injected_path
    tool_store = agent.full_available_tools["manage_memory"]._store
    assert tool_store.db_path == injected_path

    default_agent = OpenAGCAgent()
    assert default_agent.memory_store.db_path == get_data_path("memory.db")
    default_tool_store = default_agent.full_available_tools["manage_memory"]._store
    assert default_tool_store.db_path == get_data_path("memory.db")
