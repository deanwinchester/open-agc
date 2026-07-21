"""阶段4 Task5: single pricing table + get_task_usage cost-key fallback.

  1. llm_client._calculate_cost delegates to core/model_pricing.calculate_cost
  2. stats_manager.record_usage uses the SAME table (both paths agree)
  3. get_task_usage always returns a "cost" key (0 fallback on empty SUM)

All tests run without API keys or external services.
"""
import os
import sys
import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.model_pricing import calculate_cost
from core.stats_manager import StatsManager

_HAS_LITELLM = False
try:
    import litellm  # noqa: F401
    _HAS_LITELLM = True
except ImportError:
    pass

_litellm = pytest.mark.skipif(not _HAS_LITELLM, reason="requires litellm")

CASES = [
    # (provider, model, prompt_tokens, completion_tokens, cached_tokens)
    ("deepseek", "deepseek-chat", 1000, 500, 100),
    ("deepseek", "deepseek-chat", 0, 0, 0),
    ("deepseek", "deepseek-reasoner", 2000, 800, 500),
    ("openai", "gpt-4o", 2000, 1000, 0),
    ("kimi", "moonshot/kimi-k2", 1500, 300, 0),
    ("local", "llamacpp/qwen", 999, 1, 0),
]


@_litellm
def test_llm_client_delegates_to_shared_table():
    """Same model+tokens → same cost from the llm_client path."""
    from core.llm_client import _calculate_cost
    for provider, model, pt, ct, cached in CASES:
        assert _calculate_cost(provider, model, pt, ct, cached) == pytest.approx(
            calculate_cost(provider, model, pt, ct, cached))


def test_stats_manager_uses_shared_table(tmp_path):
    """Same model+tokens → same cost from the stats_manager path."""
    sm = StatsManager(str(tmp_path / "stats.db"))
    for i, (provider, model, pt, ct, cached) in enumerate(CASES, start=1):
        sm.record_usage(provider=provider, model=model, prompt_tokens=pt,
                        completion_tokens=ct, task_id=i, cached_tokens=cached)
        usage = sm.get_task_usage(i)
        assert usage["cost"] == pytest.approx(
            calculate_cost(provider, model, pt, ct, cached))


def test_record_usage_deepseek_not_flat_rate(tmp_path):
    """Regression: stats_manager used a flat 0.01/1k rate for every provider,
    so deepseek rows disagreed with llm_client's model_call_logs."""
    sm = StatsManager(str(tmp_path / "stats.db"))
    sm.record_usage(provider="deepseek", model="deepseek-chat",
                    prompt_tokens=1000, completion_tokens=500,
                    task_id=1, cached_tokens=100)
    usage = sm.get_task_usage(1)
    # Shared table: 100*0.02/1M + 900*1.0/1M + 500*2.0/1M = 0.001902
    assert usage["cost"] == pytest.approx(0.001902)
    # Old flat rate would have been (1500/1000)*0.01 = 0.015
    assert usage["cost"] != pytest.approx(0.015)


def test_get_task_usage_empty_returns_cost_key(tmp_path):
    """Zero matching rows: SUM() yields NULL — must fall back to 0 and
    still include every key, especially "cost"."""
    sm = StatsManager(str(tmp_path / "stats.db"))
    usage = sm.get_task_usage(99999)
    assert usage == {"prompt": 0, "completion": 0, "total": 0,
                     "cached": 0, "cost": 0}
    assert all(v is not None for v in usage.values())


def test_get_task_usage_sums_multiple_rows(tmp_path):
    sm = StatsManager(str(tmp_path / "stats.db"))
    sm.record_usage(provider="openai", model="gpt-4o", prompt_tokens=1000,
                    completion_tokens=500, task_id=7)
    sm.record_usage(provider="openai", model="gpt-4o", prompt_tokens=2000,
                    completion_tokens=1000, task_id=7)
    usage = sm.get_task_usage(7)
    assert usage["prompt"] == 3000
    assert usage["completion"] == 1500
    assert usage["total"] == 4500
    assert usage["cost"] == pytest.approx(
        calculate_cost("openai", "gpt-4o", 1000, 500, 0)
        + calculate_cost("openai", "gpt-4o", 2000, 1000, 0))
