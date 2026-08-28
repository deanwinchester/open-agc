# -*- coding: utf-8 -*-
"""Tests for _annotate_short_answer disambiguation hint."""
from agent.agent import _annotate_short_answer


def _assistant(content):
    return {"role": "assistant", "content": content}


def _user(content):
    return {"role": "user", "content": content}


class TestAnnotateShortAnswer:
    def test_letter_answer_after_question_gets_hint(self):
        msgs = [_user("问题1？"), _assistant("要不要记录经验？（A=要/B=不要）")]
        out = _annotate_short_answer("B", msgs)
        assert out == "（针对你上一条提问的回答）B"

    def test_chinese_answer_after_question_gets_hint(self):
        msgs = [_assistant("确认执行吗？")]
        out = _annotate_short_answer("要", msgs)
        assert out == "（针对你上一条提问的回答）要"

    def test_long_message_not_annotated(self):
        msgs = [_assistant("确认执行吗？")]
        out = _annotate_short_answer("我觉得这个方案可以再优化一下", msgs)
        assert out == "我觉得这个方案可以再优化一下"

    def test_no_question_no_annotation(self):
        msgs = [_assistant("任务已完成。")]
        out = _annotate_short_answer("B", msgs)
        assert out == "B"

    def test_no_assistant_message_no_annotation(self):
        msgs = [_user("你好")]
        out = _annotate_short_answer("B", msgs)
        assert out == "B"

    def test_question_in_older_message_not_annotated(self):
        msgs = [_assistant("旧问题？"), _user("回答"), _assistant("任务已完成。")]
        out = _annotate_short_answer("B", msgs)
        assert out == "B"

    def test_empty_input_returns_empty(self):
        msgs = [_assistant("确认吗？")]
        assert _annotate_short_answer("", msgs) == ""
        assert _annotate_short_answer(None, msgs) is None
