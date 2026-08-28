# -*- coding: utf-8 -*-
"""Tests for llama.cpp manager download resume logic.

Reproduces the bug where resuming a ModelScope/HF download with a partial
file could silently overwrite the partial when the server ignored the
Range header (returned 200 instead of 206), or report success even though
no bytes were received.
"""
import os
import pytest
from unittest.mock import patch, MagicMock

from core.llamacpp_manager import LlamaCppManager


@pytest.fixture
def manager(tmp_path, monkeypatch):
    """LlamaCppManager using a temp data directory."""
    monkeypatch.setenv("OPEN_AGC_DATA_DIR", str(tmp_path))
    return LlamaCppManager()


@pytest.fixture
def model_file(manager):
    """Return a deterministic model filename for tests."""
    return "test-model.gguf"


def _mock_get(status_code, content_chunks, headers=None):
    """Build a mocked requests.get return value."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = headers or {}
    resp.iter_content.return_value = content_chunks
    resp.raise_for_status.return_value = None
    return resp


def test_resume_with_206_appends_to_partial(manager, model_file):
    """A proper 206 Partial Content response should append to the partial file."""
    partial_path = os.path.join(manager.models_dir, model_file + ".partial")
    target_path = os.path.join(manager.models_dir, model_file)
    os.makedirs(manager.models_dir, exist_ok=True)
    with open(partial_path, "wb") as f:
        f.write(b"partial")  # 7 bytes already downloaded

    with patch("core.llamacpp_manager.requests.get") as mock_get:
        # Server accepts Range and sends remaining 8 bytes (total 15)
        mock_get.return_value = _mock_get(
            206,
            [b"12345678"],
            {"Content-Range": "bytes 7-14/15"},
        )
        result = manager.download_model(
            "https://example.com/model.gguf", model_file, resume=True
        )

    assert result is True
    assert os.path.exists(target_path)
    with open(target_path, "rb") as f:
        assert f.read() == b"partial12345678"
    assert not os.path.exists(partial_path)


def test_resume_with_200_does_not_overwrite_partial(manager, model_file):
    """If the server ignores Range and returns 200, we must NOT overwrite partial."""
    partial_path = os.path.join(manager.models_dir, model_file + ".partial")
    os.makedirs(manager.models_dir, exist_ok=True)
    original = b"already-downloaded-data"
    with open(partial_path, "wb") as f:
        f.write(original)

    with patch("core.llamacpp_manager.requests.get") as mock_get:
        # Server returns full content, ignoring Range header
        mock_get.return_value = _mock_get(
            200,
            [b"full-content-from-start"],
            {"Content-Length": "23"},
        )
        result = manager.download_model(
            "https://example.com/model.gguf", model_file, resume=True
        )

    assert result is False, "resume without 206 should fail to protect partial data"
    assert os.path.exists(partial_path)
    with open(partial_path, "rb") as f:
        assert f.read() == original


def test_resume_with_empty_response_fails(manager, model_file):
    """A 206 response with no body and no total-size info must not be treated as success."""
    partial_path = os.path.join(manager.models_dir, model_file + ".partial")
    target_path = os.path.join(manager.models_dir, model_file)
    os.makedirs(manager.models_dir, exist_ok=True)
    original = b"some-data"
    with open(partial_path, "wb") as f:
        f.write(original)

    with patch("core.llamacpp_manager.requests.get") as mock_get:
        # Server claims total is 19 bytes but sends nothing -> size mismatch
        mock_get.return_value = _mock_get(
            206, [], {"Content-Range": "bytes 9-9/19"}
        )
        result = manager.download_model(
            "https://example.com/model.gguf", model_file, resume=True
        )

    assert result is False
    assert not os.path.exists(target_path)
    assert os.path.exists(partial_path)
    with open(partial_path, "rb") as f:
        assert f.read() == original


def test_fresh_download_validates_final_size(manager, model_file):
    """A fresh download that receives no bytes should not be marked completed."""
    target_path = os.path.join(manager.models_dir, model_file)
    os.makedirs(manager.models_dir, exist_ok=True)

    with patch("core.llamacpp_manager.requests.get") as mock_get:
        mock_get.return_value = _mock_get(200, [], {"Content-Length": "100"})
        result = manager.download_model(
            "https://example.com/model.gguf", model_file, resume=False
        )

    assert result is False
    assert not os.path.exists(target_path)
