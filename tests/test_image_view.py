# -*- coding: utf-8 -*-
"""Tests for tools/image_view.py + the agent-side image injection chain.

Covers:
- sandbox denial / allowance (same pattern as read_file)
- PIL long-edge scaling (default 1024, custom max_size, no-op for small images)
- PIL-missing fallback (raw read + note)
- non-vision model error message (heuristic + capability flag + config switch)
- is_vision_model heuristic matrix
- extract_image_data marker round-trip
- injection chain: after image_view runs, the next loop iteration's messages
  contain a user message with an image_url block (stub LLM, no network)
"""
import base64
import io
import json
import os
import queue
import sys
import types

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

pytest.importorskip("PIL")
from PIL import Image  # noqa: E402

from tools.base import SandboxBlocked  # noqa: E402
from tools.image_view import ImageViewTool, is_vision_model  # noqa: E402
from core.llm_client import IMAGE_MARKER, extract_image_data  # noqa: E402


# ------------------------------------------------------------------ helpers

def _make_image(path, size=(64, 48), color=(200, 30, 30), fmt="PNG"):
    img = Image.new("RGB", size, color)
    img.save(str(path), format=fmt)
    return str(path)


def _write_config(tmp_path, monkeypatch, **overrides):
    """Write an isolated config.json and point core.paths.get_data_path at it."""
    config = {"sandbox_mode": True, "sandbox_dir": str(tmp_path)}
    config.update(overrides)
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr("core.paths.get_data_path", lambda *a, **k: str(cfg_path))
    return config


def _vision_agent(model="gpt-4o", flag=None):
    agent = types.SimpleNamespace(model=model,
                                  llm=types.SimpleNamespace(default_model=model))
    if flag is not None:
        agent.vision_capable = flag
    return agent


def _decode_data_url(result):
    url = extract_image_data(result)
    assert url, f"no {IMAGE_MARKER} marker in result: {result[:200]}"
    assert url.startswith("data:image/")
    b64 = url.split(",", 1)[1]
    return Image.open(io.BytesIO(base64.b64decode(b64)))


# ------------------------------------------------------------------ sandbox

class TestSandbox:
    def test_outside_sandbox_blocked(self, tmp_path, monkeypatch):
        sandbox = tmp_path / "ws"
        sandbox.mkdir()
        outside = tmp_path / "secret.png"
        _make_image(outside)
        _write_config(sandbox, monkeypatch)

        tool = ImageViewTool()
        with pytest.raises(SandboxBlocked):
            tool.execute(path=str(outside), _agent_context=_vision_agent())

    def test_inside_sandbox_allowed(self, tmp_path, monkeypatch):
        img = _make_image(tmp_path / "ok.png")
        _write_config(tmp_path, monkeypatch)

        result = ImageViewTool().execute(path=img, _agent_context=_vision_agent())
        assert IMAGE_MARKER in result
        assert "图片已加载" in result

    def test_session_whitelist_allows_outside_path(self, tmp_path, monkeypatch):
        sandbox = tmp_path / "ws"
        sandbox.mkdir()
        outside_dir = tmp_path / "shared"
        outside_dir.mkdir()
        img = _make_image(outside_dir / "pic.png")
        _write_config(sandbox, monkeypatch)

        result = ImageViewTool().execute(
            path=img,
            _agent_context=_vision_agent(),
            _session_whitelist={str(outside_dir)},
        )
        assert IMAGE_MARKER in result


# ------------------------------------------------------------------ scaling

class TestScaling:
    def test_long_edge_scaled_to_default_1024(self, tmp_path, monkeypatch):
        img = _make_image(tmp_path / "big.png", size=(2000, 1000))
        _write_config(tmp_path, monkeypatch)

        result = ImageViewTool().execute(path=img, _agent_context=_vision_agent())
        decoded = _decode_data_url(result)
        assert decoded.size == (1024, 512)
        assert "2000x1000" in result
        assert "已缩放" in result

    def test_custom_max_size(self, tmp_path, monkeypatch):
        img = _make_image(tmp_path / "big.png", size=(2000, 1000))
        _write_config(tmp_path, monkeypatch)

        result = ImageViewTool().execute(path=img, max_size=256,
                                         _agent_context=_vision_agent())
        decoded = _decode_data_url(result)
        assert decoded.size == (256, 128)

    def test_small_image_not_scaled(self, tmp_path, monkeypatch):
        img = _make_image(tmp_path / "small.png", size=(100, 50))
        _write_config(tmp_path, monkeypatch)

        result = ImageViewTool().execute(path=img, _agent_context=_vision_agent())
        decoded = _decode_data_url(result)
        assert decoded.size == (100, 50)
        assert "未缩放" in result

    def test_max_size_zero_disables_scaling(self, tmp_path, monkeypatch):
        img = _make_image(tmp_path / "big.png", size=(1500, 900))
        _write_config(tmp_path, monkeypatch)

        result = ImageViewTool().execute(path=img, max_size=0,
                                         _agent_context=_vision_agent())
        decoded = _decode_data_url(result)
        assert decoded.size == (1500, 900)

    def test_jpeg_stays_jpeg(self, tmp_path, monkeypatch):
        img = _make_image(tmp_path / "photo.jpg", size=(300, 200), fmt="JPEG")
        _write_config(tmp_path, monkeypatch)

        result = ImageViewTool().execute(path=img, _agent_context=_vision_agent())
        url = extract_image_data(result)
        assert url.startswith("data:image/jpeg")

    def test_pil_missing_falls_back_to_raw_read(self, tmp_path, monkeypatch):
        img = _make_image(tmp_path / "raw.png", size=(120, 80))
        _write_config(tmp_path, monkeypatch)
        monkeypatch.setitem(sys.modules, "PIL", None)

        result = ImageViewTool().execute(path=img, _agent_context=_vision_agent())
        assert "PIL 不可用" in result
        decoded = _decode_data_url(result)
        assert decoded.size == (120, 80)  # original bytes, unscaled

    def test_invalid_max_size(self, tmp_path, monkeypatch):
        img = _make_image(tmp_path / "a.png")
        _write_config(tmp_path, monkeypatch)
        result = ImageViewTool().execute(path=img, max_size="abc",
                                         _agent_context=_vision_agent())
        assert result.startswith("Error")


# ------------------------------------------------------------------ vision gating

class TestVisionGating:
    def test_non_vision_model_gets_clear_error(self, tmp_path, monkeypatch):
        img = _make_image(tmp_path / "a.png")
        _write_config(tmp_path, monkeypatch)

        result = ImageViewTool().execute(
            path=img, _agent_context=_vision_agent("deepseek/deepseek-chat"))
        assert result.startswith("Error")
        assert "视觉" in result
        assert "deepseek/deepseek-chat" in result
        assert IMAGE_MARKER not in result

    def test_capability_flag_overrides_heuristic(self, tmp_path, monkeypatch):
        img = _make_image(tmp_path / "a.png")
        _write_config(tmp_path, monkeypatch)

        # flag=True lets a text model through; flag=False blocks a vision model
        ok = ImageViewTool().execute(
            path=img, _agent_context=_vision_agent("deepseek/deepseek-chat", flag=True))
        assert IMAGE_MARKER in ok
        denied = ImageViewTool().execute(
            path=img, _agent_context=_vision_agent("gpt-4o", flag=False))
        assert denied.startswith("Error")
        assert IMAGE_MARKER not in denied

    def test_disabled_by_config(self, tmp_path, monkeypatch):
        img = _make_image(tmp_path / "a.png")
        _write_config(tmp_path, monkeypatch, image_view_enabled=False)

        result = ImageViewTool().execute(path=img, _agent_context=_vision_agent())
        assert result.startswith("Error")
        assert "禁用" in result
        assert IMAGE_MARKER not in result

    def test_unknown_model_defaults_to_non_vision(self, tmp_path, monkeypatch):
        img = _make_image(tmp_path / "a.png")
        _write_config(tmp_path, monkeypatch)
        result = ImageViewTool().execute(
            path=img, _agent_context=_vision_agent("some-random-text-model-9000"))
        assert result.startswith("Error")
        assert IMAGE_MARKER not in result

    def test_config_vision_models_allows_custom_model(self, tmp_path, monkeypatch):
        img = _make_image(tmp_path / "a.png")
        _write_config(tmp_path, monkeypatch, vision_models=["qwen3.8"])
        result = ImageViewTool().execute(
            path=img, _agent_context=_vision_agent("qwen38/Qwen3.8-27B"))
        assert IMAGE_MARKER in result

    def test_config_vision_capable_overrides_heuristic(self, tmp_path, monkeypatch):
        img = _make_image(tmp_path / "a.png")
        _write_config(tmp_path, monkeypatch, vision_capable=True)
        result = ImageViewTool().execute(
            path=img, _agent_context=_vision_agent("some-random-text-model-9000"))
        assert IMAGE_MARKER in result

    def test_config_vision_capable_false_blocks_heuristic(self, tmp_path, monkeypatch):
        img = _make_image(tmp_path / "a.png")
        _write_config(tmp_path, monkeypatch, vision_capable=False)
        result = ImageViewTool().execute(
            path=img, _agent_context=_vision_agent("gpt-4o"))
        assert result.startswith("Error")
        assert IMAGE_MARKER not in result


class TestVisionProbe:
    """端点探测兜底：名称启发式识别不了的自定义模型，探测端点是否接受图片。"""

    def _agent_with_custom_provider(self, model, base_url, api_key="sk-x"):
        import types
        llm = types.SimpleNamespace(
            default_model=model,
            _custom_providers={model.split('/')[0]: {"base_url": base_url, "api_key": api_key}},
            llamacpp_api_base="http://localhost:8080/v1",
        )
        return types.SimpleNamespace(model=model, llm=llm)

    def _stub_post(self, monkeypatch, status_code=200, text=""):
        import requests
        class _Resp:
            def __init__(self):
                self.status_code = status_code
                self.text = text
        monkeypatch.setattr(requests, "post", lambda *a, **k: _Resp())

    def test_probe_true_when_endpoint_accepts_image(self, monkeypatch):
        from tools.image_view import _probe_vision_capability, _VISION_PROBE_CACHE
        _VISION_PROBE_CACHE.clear()
        self._stub_post(monkeypatch, status_code=200)
        agent = self._agent_with_custom_provider("qwen38/Qwen3.8-27B", "http://x/v1")
        assert _probe_vision_capability("qwen38/Qwen3.8-27B", agent) is True

    def test_probe_false_when_endpoint_rejects_image(self, monkeypatch):
        from tools.image_view import _probe_vision_capability, _VISION_PROBE_CACHE
        _VISION_PROBE_CACHE.clear()
        self._stub_post(monkeypatch, status_code=400,
                        text='{"error": "image_url is not supported by this model"}')
        agent = self._agent_with_custom_provider("qwen38/Qwen3.8-27B", "http://x/v1")
        assert _probe_vision_capability("qwen38/Qwen3.8-27B", agent) is False

    def test_probe_none_when_no_endpoint(self):
        from tools.image_view import _probe_vision_capability
        assert _probe_vision_capability("some-model", None) is None

    def test_probe_result_cached(self, monkeypatch):
        from tools.image_view import _probe_vision_capability, _VISION_PROBE_CACHE
        _VISION_PROBE_CACHE.clear()
        calls = {"n": 0}
        import requests
        class _Resp:
            status_code = 200
            text = ""
        def _post(*a, **k):
            calls["n"] += 1
            return _Resp()
        monkeypatch.setattr(requests, "post", _post)
        agent = self._agent_with_custom_provider("qwen38/Qwen3.8-27B", "http://x/v1")
        _probe_vision_capability("qwen38/Qwen3.8-27B", agent)
        _probe_vision_capability("qwen38/Qwen3.8-27B", agent)
        assert calls["n"] == 1

    def test_execute_uses_probe_for_unknown_custom_model(self, tmp_path, monkeypatch):
        from tools.image_view import _VISION_PROBE_CACHE
        _VISION_PROBE_CACHE.clear()
        img = _make_image(tmp_path / "a.png")
        _write_config(tmp_path, monkeypatch)
        self._stub_post(monkeypatch, status_code=200)
        agent = self._agent_with_custom_provider("qwen38/Qwen3.8-27B", "http://x/v1")
        result = ImageViewTool().execute(path=img, _agent_context=agent)
        assert IMAGE_MARKER in result


class TestIsVisionModel:
    @pytest.mark.parametrize("model", [
        "kimi_code/k3", "gpt-4o", "gpt-4o-mini", "gpt-4.1",
        "claude-sonnet-4-5", "claude-3-opus", "gemini/gemini-2.5-flash",
        "qwen2.5-vl-7b", "glm-4v", "llava-13b", "moonshot-v1-8k-vision-preview",
    ])
    def test_vision_models(self, model):
        assert is_vision_model(model) is True

    @pytest.mark.parametrize("model", [
        "deepseek/deepseek-chat", "deepseek/deepseek-reasoner",
        "moonshot/moonshot-v1-8k", "kimi-k2", "gpt-3.5-turbo", "", None,
    ])
    def test_non_vision_models(self, model):
        assert is_vision_model(model) is False

    def test_flag_takes_precedence(self):
        assert is_vision_model("gpt-3.5-turbo", capability_flag=True) is True
        assert is_vision_model("gpt-4o", capability_flag=False) is False


# ------------------------------------------------------------------ misc errors

class TestMiscErrors:
    def test_missing_file(self, tmp_path, monkeypatch):
        _write_config(tmp_path, monkeypatch)
        result = ImageViewTool().execute(path=str(tmp_path / "nope.png"),
                                         _agent_context=_vision_agent())
        assert result.startswith("Error")
        assert "not found" in result.lower() or "File not found" in result

    def test_unsupported_extension(self, tmp_path, monkeypatch):
        txt = tmp_path / "notes.txt"
        txt.write_text("hello", encoding="utf-8")
        _write_config(tmp_path, monkeypatch)
        result = ImageViewTool().execute(path=str(txt), _agent_context=_vision_agent())
        assert result.startswith("Error")
        assert "不支持" in result

    def test_no_path(self):
        result = ImageViewTool().execute()
        assert result.startswith("Error")

    def test_extract_image_data_roundtrip(self):
        assert extract_image_data(f"prefix {IMAGE_MARKER}data:image/png;base64,abc] suffix") \
            == "data:image/png;base64,abc"
        assert extract_image_data("no marker here") is None


# ------------------------------------------------------------------ injection chain

class _StubMessage:
    def __init__(self, content=None, tool_calls=None):
        self.role = "assistant"
        self.content = content
        self.tool_calls = tool_calls


class _StubResponse:
    def __init__(self, message):
        self.choices = [types.SimpleNamespace(message=message)]
        self.usage = None


class _StubLLM:
    """Scripted LLM client: pops one response per chat() call."""

    def __init__(self, script):
        self.script = list(script)
        self.default_model = "gpt-4o"

    def chat(self, messages=None, tools=None, interrupt_check=None):
        return self.script.pop(0), self.default_model


def _tool_call(name, arguments):
    return types.SimpleNamespace(
        id="call_1", type="function",
        function=types.SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def _bare_agent(tool):
    """Minimal OpenAGCAgent (no heavy __init__) wired with a single tool."""
    from agent.agent import OpenAGCAgent
    agent = OpenAGCAgent.__new__(OpenAGCAgent)
    agent.session_id = None
    agent.failed_attempts = []
    agent.messages = [{"role": "system", "content": "sys"}]
    agent.logger = None
    agent.model = "gpt-4o"
    agent.llm = None  # set by caller
    agent.pending_messages = []
    agent._processing_interjection = False
    agent._interjection_stuck_count = 0
    agent._rejected_interjection = None
    agent._in_self_review = False
    agent._max_correction_attempts = 0
    agent.tool_schemas = []
    agent.tool_display_names = {"image_view": "查看图片"}
    agent.available_tools = {"image_view": tool}
    agent.full_available_tools = {"image_view": tool}
    agent._session_sandbox_whitelist = set()
    agent._session_network_whitelist = set()
    agent._session_permission_whitelist = set()
    agent._pending_sudo_password = ""
    agent._session_sudo_password = ""
    agent.reflection_engine = None
    agent.knowledge_graph = types.SimpleNamespace(extract_from_messages=lambda msgs: None)
    agent._save_task_stats = lambda *a, **k: None
    agent.user_input_queue = queue.Queue()
    agent.progress_callback = None
    agent._build_system_prompt = lambda **kwargs: "sys"
    agent._should_delegate = lambda text: False
    return agent


class TestInjectionChain:
    def test_image_injected_as_user_message_next_round(self, tmp_path, monkeypatch):
        img = _make_image(tmp_path / "scene.png", size=(300, 200))
        _write_config(tmp_path, monkeypatch)
        monkeypatch.setattr("tools.adaptive.record_tool_call", lambda *a, **k: None)

        agent = _bare_agent(ImageViewTool())
        agent.llm = _StubLLM([
            _StubResponse(_StubMessage(tool_calls=[_tool_call("image_view", {"path": img})])),
            _StubResponse(_StubMessage(content="图片里是一块红色区域。")),
        ])

        result = agent.run_turn("看看这张图", verbose=False, skip_rag=True)

        assert result == "图片里是一块红色区域。"
        # Find the injected multimodal user message
        injected = [
            m for m in agent.messages
            if m["role"] == "user" and isinstance(m["content"], list)
            and any(p.get("type") == "image_url" for p in m["content"])
        ]
        assert len(injected) == 1, f"expected 1 injected image message, got {len(injected)}"
        parts = injected[0]["content"]
        text_part = next(p for p in parts if p["type"] == "text")
        img_part = next(p for p in parts if p["type"] == "image_url")
        assert "image_view" in text_part["text"]
        assert img_part["image_url"]["url"].startswith("data:image/png;base64,")
        # Tool result itself stayed on the text channel; the base64 marker is
        # replaced by a short placeholder after extraction so the blob is not
        # retained twice (image lives in the injected user message).
        tool_msgs = [m for m in agent.messages if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        assert IMAGE_MARKER not in tool_msgs[0]["content"]
        assert "base64," not in tool_msgs[0]["content"]
        assert "[图片已注入]" in tool_msgs[0]["content"]

    def test_non_vision_result_not_injected(self, tmp_path, monkeypatch):
        img = _make_image(tmp_path / "scene.png")
        _write_config(tmp_path, monkeypatch)
        monkeypatch.setattr("tools.adaptive.record_tool_call", lambda *a, **k: None)

        agent = _bare_agent(ImageViewTool())
        agent.model = "deepseek/deepseek-chat"
        agent.llm = _StubLLM([
            _StubResponse(_StubMessage(tool_calls=[_tool_call("image_view", {"path": img})])),
            _StubResponse(_StubMessage(content="模型不是视觉模型，换用文本方式。")),
        ])
        agent.llm.default_model = "deepseek/deepseek-chat"

        result = agent.run_turn("看看这张图", verbose=False, skip_rag=True)

        assert result == "模型不是视觉模型，换用文本方式。"
        injected = [
            m for m in agent.messages
            if m["role"] == "user" and isinstance(m["content"], list)
        ]
        assert injected == []
