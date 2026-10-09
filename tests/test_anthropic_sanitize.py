# -*- coding: utf-8 -*-
"""_sanitize_for_anthropic：Anthropic 渠道空文本清洗的回归测试。

实证：Guardian 恢复 #591 时 kimi_code/k3 报 400 "text content is empty"——
快照/中断轮次里混入空 content 消息，Anthropic Messages API 直接拒绝。"""
from core.llm_client import LLMClient


def _s(msgs):
    return LLMClient._sanitize_for_anthropic(msgs)


def test_empty_string_replaced():
    out = _s([{"role": "assistant", "content": ""}])
    assert out[0]["content"] == "（空）"


def test_whitespace_only_replaced():
    out = _s([{"role": "assistant", "content": "  \n "}])
    assert out[0]["content"] == "（空）"


def test_none_without_tool_calls_replaced():
    out = _s([{"role": "assistant", "content": None}])
    assert out[0]["content"] == "（空）"


def test_none_with_tool_calls_kept():
    msg = {"role": "assistant", "content": None,
           "tool_calls": [{"id": "1", "function": {"name": "x", "arguments": "{}"}}]}
    out = _s([msg])
    assert out[0]["content"] is None
    assert out[0]["tool_calls"]


def test_empty_text_block_filtered_from_list():
    msgs = [{"role": "user", "content": [
        {"type": "text", "text": ""},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,xx"}},
    ]}]
    out = _s(msgs)
    assert len(out[0]["content"]) == 1
    assert out[0]["content"][0]["type"] == "image_url"


def test_all_empty_blocks_become_placeholder():
    out = _s([{"role": "user", "content": [{"type": "text", "text": "  "}]}])
    assert out[0]["content"] == "（空）"


def test_normal_messages_untouched():
    msgs = [{"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": [{"type": "text", "text": "ok"}]}]
    assert _s(msgs) == msgs
