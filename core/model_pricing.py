"""Shared model pricing table — single source of truth for cost estimates.

Historically both ``core/llm_client.py`` (``_calculate_cost``) and
``core/stats_manager.py`` (``record_usage``) kept their own rates, so the
same model call produced different costs depending on where it was recorded.
llm_client's table is the authoritative one; it was extracted here (阶段4
Task5) and both call sites now delegate to :func:`calculate_cost`.

All rates are in CNY (¥).
"""

# DeepSeek rates (¥ per 1M tokens): (cached_prompt, uncached_prompt, completion)
_DEEPSEEK_CHAT_RATES = (0.02, 1.0, 2.0)      # deepseek-chat (Flash)
_DEEPSEEK_PRO_RATES = (0.025, 3.0, 6.0)      # deepseek-reasoner / Pro

# Default flat rate for providers without a specific table (¥ per 1k tokens)
_DEFAULT_FLAT_PER_1K = 0.01


def calculate_cost(provider: str, model: str, prompt_tokens: int,
                   completion_tokens: int, cached_tokens: int = 0) -> float:
    """Calculate cost in CNY with provider-specific pricing.

    ``provider`` is accepted for call-site compatibility; pricing is keyed
    off the model name (same rule as the original llm_client table).
    """
    ml = (model or "").lower()
    # DeepSeek pricing (¥ per 1M tokens)
    if "deepseek" in ml:
        cached_rate, prompt_rate, completion_rate = (
            _DEEPSEEK_CHAT_RATES if "chat" in ml else _DEEPSEEK_PRO_RATES)
        uncached = prompt_tokens - cached_tokens
        return (cached_tokens / 1_000_000 * cached_rate
                + uncached / 1_000_000 * prompt_rate
                + completion_tokens / 1_000_000 * completion_rate)
    # Default flat rate
    tt = prompt_tokens + completion_tokens
    return (tt / 1000.0) * _DEFAULT_FLAT_PER_1K
