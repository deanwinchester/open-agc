"""
TokenBudget — Context window budget management.

Estimates token usage of messages and prunes conversation history
when the budget is exceeded, keeping the most important content.
"""
import re
from typing import List, Dict, Optional, Tuple


# Default budget allocation
DEFAULT_MAX_TOKENS = 128000
DEFAULT_SYSTEM_RATIO = 0.20
DEFAULT_HISTORY_RATIO = 0.50
DEFAULT_TOOL_RATIO = 0.30
DEFAULT_MIN_KEEP_ROUNDS = 3


def estimate_tokens(text: str) -> int:
    """Rough token estimation without external dependencies.

    For mixed Chinese/English text, ~1 token per 1.5-2 characters.
    We use len//2 as a conservative overestimate.
    """
    if not text:
        return 0
    # Count Chinese chars (denser, ~1 token per char)
    cjk = len(re.findall(r'[一-鿿]', text))
    # Non-CJK chars (English, numbers, symbols: ~1 token per 3-4 chars)
    ascii_chars = len(text) - cjk
    # Rough estimate: CJK ~1 tok/char, ASCII ~1 tok/3 chars
    return cjk + ascii_chars // 3 + 1


def estimate_messages_tokens(messages: List[Dict]) -> int:
    """Estimate total tokens used by a list of messages."""
    total = 0
    for msg in messages:
        total += 4  # role + metadata overhead
        content = msg.get("content", "")
        if content:
            total += estimate_tokens(str(content))
        # Tool call content
        tool_calls = msg.get("tool_calls")
        if tool_calls:
            for tc in tool_calls:
                if isinstance(tc, dict):
                    total += estimate_tokens(str(tc.get("function", {}).get("arguments", "")))
        # Tool call id
        if msg.get("tool_call_id"):
            total += 2
    return total


class TokenBudget:
    """Manages context window budget and message pruning."""

    def __init__(self, config: Optional[Dict] = None):
        cfg = config or {}
        self.max_tokens = cfg.get("max_total_tokens", DEFAULT_MAX_TOKENS)
        self.system_ratio = cfg.get("system_ratio", DEFAULT_SYSTEM_RATIO)
        self.history_ratio = cfg.get("history_ratio", DEFAULT_HISTORY_RATIO)
        self.tool_ratio = cfg.get("tool_ratio", DEFAULT_TOOL_RATIO)
        self.min_keep_rounds = cfg.get("min_keep_rounds", DEFAULT_MIN_KEEP_ROUNDS)

        # Derived budgets
        self.system_budget = int(self.max_tokens * self.system_ratio)
        self.history_budget = int(self.max_tokens * self.history_ratio)
        self.tool_budget = int(self.max_tokens * self.tool_ratio)

    def budget_summary(self) -> Dict:
        return {
            "max_tokens": self.max_tokens,
            "system_budget": self.system_budget,
            "history_budget": self.history_budget,
            "tool_budget": self.tool_budget,
        }

    def prune_messages(self, messages: List[Dict]) -> List[Dict]:
        """Prune messages to fit within the token budget.

        Strategy:
          1. System prompt is always kept intact.
          2. Keep last N complete rounds (user+assistant tool calls).
          3. Prune older tool results first — replace with short markers.
          4. If still over, prune oldest conversation turns (head of list).
        """
        if not messages or messages[0].get("role") != "system":
            return messages

        system = [messages[0]]
        rest = messages[1:]

        total = estimate_messages_tokens(system)
        if total > self.max_tokens:
            # System prompt alone is over budget — still keep it, nothing we can do
            return messages

        # Work on a copy
        pruned = list(rest)

        # Pass 1: compress verbose tool results
        pruned = self._compress_tool_results(pruned)

        # Pass 2: remove oldest rounds if still over budget
        total = self._total_with_system(system, pruned)
        if total <= self.max_tokens:
            return system + pruned

        pruned = self._remove_oldest_rounds(pruned)

        total = self._total_with_system(system, pruned)
        if total <= self.max_tokens:
            return system + pruned

        # Pass 3: emergency — drop tool results entirely except the last 2
        pruned = self._emergency_prune(pruned)

        return system + pruned

    def _total_with_system(self, system: List[Dict], rest: List[Dict]) -> int:
        return estimate_messages_tokens(system) + estimate_messages_tokens(rest)

    def _compress_tool_results(self, messages: List[Dict]) -> List[Dict]:
        """Replace verbose tool results with short summaries."""
        compressed = []
        for msg in messages:
            if msg.get("role") == "tool":
                content = msg.get("content", "")
                if estimate_tokens(str(content)) > self.tool_budget // max(len([m for m in messages if m.get("role") == "tool"]), 1):
                    # Shorten the content
                    text = str(content)
                    lines = text.split("\n")
                    if len(lines) > 20:
                        summary = "\n".join(lines[:10] + [f"...({len(lines)-20} lines omitted)..."] + lines[-10:])
                    else:
                        summary = text[:500] + "..." if len(text) > 500 else text
                    msg = dict(msg)
                    msg["content"] = summary + f"\n[Compressed: was {len(text)} chars]"
                compressed.append(msg)
            else:
                compressed.append(msg)
        return compressed

    def _remove_oldest_rounds(self, messages: List[Dict]) -> List[Dict]:
        """Remove oldest conversation turns until we're under budget or at min_keep_rounds."""
        # Group messages into rounds: [user, (tool_call+tool_result)*, assistant]
        rounds = []
        current = []
        for msg in messages:
            current.append(msg)
            role = msg.get("role", "")
            if role == "assistant":
                # Only finalize as a round if it's a text response (not a tool call)
                if not msg.get("tool_calls"):
                    rounds.append(current)
                    current = []
                else:
                    # This assistant message has tool_calls — the round continues
                    pass
            elif role == "tool":
                # Next message after tool result is usually another tool_call or the final answer
                # Check if the round should end here
                pass

        # If current is non-empty, add it as the last round
        if current:
            rounds.append(current)

        # Always keep last min_keep_rounds
        if len(rounds) <= self.min_keep_rounds:
            return messages

        keep = rounds[-self.min_keep_rounds:]
        kept = [m for r in keep for m in r]

        # Flatten the kept rounds back to a message list
        return kept

    def _emergency_prune(self, messages: List[Dict]) -> List[Dict]:
        """Emergency: keep only the last 2 tool results, drop the rest."""
        tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
        if len(tool_indices) <= 2:
            return messages

        # Keep the last 2 tool results
        keep_indices = set(tool_indices[-2:])
        pruned = []
        for i, m in enumerate(messages):
            if m.get("role") == "tool" and i not in keep_indices:
                pruned.append({
                    "role": "tool",
                    "content": "[Pruned by token budget — tool result removed to save context]",
                    "tool_call_id": m.get("tool_call_id", ""),
                    "name": m.get("name", ""),
                })
            else:
                pruned.append(m)
        return pruned
