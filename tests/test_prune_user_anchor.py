"""回归测试：预算剪枝必须保住首个 user 消息（任务锚点）。

生产实证：长会话越界剪枝把首问剪掉后，恢复出的上下文没有任何 user
消息，服务端报 "No user query found in messages"，恢复链全灭。
"""
from core.token_budget import TokenBudget


def _mk_msgs(n_rounds: int):
    msgs = [{"role": "system", "content": "sys" * 100}]
    msgs.append({"role": "user", "content": "给我微信上的郭路明发消息：老乡您好"})
    for i in range(n_rounds):
        msgs.append({"role": "assistant", "content": None,
                     "tool_calls": [{"id": f"tc{i}", "type": "function",
                                     "function": {"name": "x", "arguments": "{}"}}]})
        msgs.append({"role": "tool", "tool_call_id": f"tc{i}", "name": "x",
                     "content": "out" * 5000})
        msgs.append({"role": "user", "content": f"[截图{i}]"})
    msgs.append({"role": "assistant", "content": "最终答复"})
    return msgs


class TestPruneKeepsUserAnchor:
    def test_first_user_message_always_kept(self):
        tb = TokenBudget(config={"max_total_tokens": 2000, "min_keep_rounds": 1})
        out = tb.prune_messages(_mk_msgs(10))
        users = [m for m in out if m.get("role") == "user"]
        assert users, "剪枝后必须仍有 user 消息"
        assert any("郭路明" in str(m.get("content", "")) for m in users), \
            "首问（任务锚点）必须保留"

    def test_over_budget_still_has_user(self):
        tb = TokenBudget(config={"max_total_tokens": 500, "min_keep_rounds": 1})
        out = tb.prune_messages(_mk_msgs(20))
        assert any(m.get("role") == "user" for m in out)
