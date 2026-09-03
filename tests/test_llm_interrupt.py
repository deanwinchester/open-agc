# -*- coding: utf-8 -*-
"""LLM 调用中断测试：chat/chat_stream_collect/chat_with_drift_retry 支持
interrupt_check——置位即抛 LLMInterrupted，不再等整轮生成/重试跑完
（生产实证：中断按钮要按几次、反应慢的主因）。"""
import time
import types

import pytest

from core.llm_client import LLMInterrupted, chat_with_drift_retry
from core import llm_client as llm_mod


def _bare_client(stream_chunks=None):
    """绕过 __init__ 构造最小 LLMClient（chat 只依赖这三个属性）。"""
    c = llm_mod.LLMClient.__new__(llm_mod.LLMClient)
    c.default_model = "test/model"
    c.fallback_models = []
    c._custom_providers = {}
    c._log_session_id = None
    c._log_task_id = None
    return c


class TestChatInterrupt:
    def test_interrupt_during_retry_sleep(self, monkeypatch):
        """可重试错误后的退避 sleep 期间置位 → 立即抛 LLMInterrupted。"""
        calls = []

        def fake_completion(**kwargs):
            calls.append(1)
            raise ConnectionError("connection reset by peer")

        monkeypatch.setattr(llm_mod.litellm, "completion", fake_completion)
        c = _bare_client()
        flag = {"on": False}

        def flip():
            time.sleep(0.3)
            flag["on"] = True

        import threading
        threading.Thread(target=flip, daemon=True).start()
        t0 = time.time()
        with pytest.raises(LLMInterrupted):
            c.chat(messages=[{"role": "user", "content": "hi"}],
                   interrupt_check=lambda: flag["on"])
        assert time.time() - t0 < 2.5  # 不分段检查的话要等满 2s+4s 退避
        assert len(calls) == 1  # 中断后不再重试

    def test_no_interrupt_check_still_works(self, monkeypatch):
        """不传 interrupt_check 时行为不变。"""
        resp = types.SimpleNamespace(
            usage=types.SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            choices=[types.SimpleNamespace(
                message=types.SimpleNamespace(content="ok", tool_calls=None))],
        )
        monkeypatch.setattr(llm_mod.litellm, "completion", lambda **kw: resp)
        c = _bare_client()
        c._log_session_id = None
        c._log_task_id = None
        monkeypatch.setattr(llm_mod, "_log_model_call", lambda **kw: None)
        out, model = c.chat(messages=[{"role": "user", "content": "hi"}])
        assert out is resp and model == "test/model"

    def test_interrupt_never_retried(self, monkeypatch):
        """LLMInterrupted 不被归类为可重试错误（不落 retry/fallback）。"""
        def fake_completion(**kwargs):
            raise LLMInterrupted("interrupted by user")

        monkeypatch.setattr(llm_mod.litellm, "completion", fake_completion)
        c = _bare_client()
        c.fallback_models = ["test/fallback"]
        t0 = time.time()
        with pytest.raises(LLMInterrupted):
            c.chat(messages=[{"role": "user", "content": "hi"}])
        assert time.time() - t0 < 1.0  # 无退避等待


class TestStreamInterrupt:
    def test_interrupt_mid_stream_closes_and_raises(self, monkeypatch):
        """流式生成中置位：抛 LLMInterrupted 且底层流被关闭。"""
        closed = {"v": False}

        def fake_stream(messages, model=None, tools=None):
            for i in range(100):
                yield types.SimpleNamespace(
                    choices=[types.SimpleNamespace(
                        delta=types.SimpleNamespace(content=f"c{i}",
                                                    reasoning_content=None))],
                    usage=None)
            # 不应走到这里（中断提前终止）
            raise AssertionError("stream not aborted")

        c = _bare_client()
        monkeypatch.setattr(c, "chat_stream", fake_stream)
        seen = []

        with pytest.raises(LLMInterrupted):
            c.chat_stream_collect(
                messages=[{"role": "user", "content": "hi"}],
                on_delta=lambda k, t: seen.append(t),
                interrupt_check=lambda: len(seen) >= 3)
        assert seen == ["c0", "c1", "c2"]  # 第 4 个 chunk 到达前已中止

    def test_stream_without_interrupt_unchanged(self, monkeypatch):
        chunks = [
            types.SimpleNamespace(
                choices=[types.SimpleNamespace(
                    delta=types.SimpleNamespace(content="a", reasoning_content=None))],
                usage=None),
            types.SimpleNamespace(
                choices=[types.SimpleNamespace(
                    delta=types.SimpleNamespace(content="b", reasoning_content=None))],
                usage=None),
        ]
        c = _bare_client()
        monkeypatch.setattr(c, "chat_stream", lambda *a, **k: iter(chunks))
        _msg = types.SimpleNamespace(content="ab", tool_calls=None)
        monkeypatch.setattr(llm_mod.litellm, "stream_chunk_builder",
                            lambda ch: types.SimpleNamespace(
                                usage=None, chunks=ch,
                                choices=[types.SimpleNamespace(message=_msg)]))
        monkeypatch.setattr(llm_mod, "_log_model_call", lambda **kw: None)
        resp, _ = c.chat_stream_collect(messages=[{"role": "user", "content": "hi"}])
        assert len(resp.chunks) == 2


class TestDriftRetryInterrupt:
    def test_interrupt_propagates_through_drift_retry(self, monkeypatch):
        c = _bare_client()
        monkeypatch.setattr(c, "chat", lambda **kw: (_ for _ in ()).throw(
            LLMInterrupted("interrupted by user")))
        with pytest.raises(LLMInterrupted):
            chat_with_drift_retry(c, [{"role": "user", "content": "hi"}],
                                  interrupt_check=lambda: False)

    def test_interrupt_check_before_call(self, monkeypatch):
        called = []
        c = _bare_client()
        monkeypatch.setattr(c, "chat", lambda **kw: called.append(1) or None)
        with pytest.raises(LLMInterrupted):
            chat_with_drift_retry(c, [{"role": "user", "content": "hi"}],
                                  interrupt_check=lambda: True)
        assert called == []  # 置位时根本不发请求
