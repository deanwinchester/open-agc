# -*- coding: utf-8 -*-
"""_ws_chunk_frames 分片测试：libsoup2 ≤2.6x（UOS/deepin WebKitGTK 2.38 栈）
处理 >16KB 的 WS 帧会把网络进程主循环卡死（100% CPU 空转、全部请求饿死，
生产实证阈值 16~32KB）。大消息拆 8KB 帧，前端重组。"""
import json

from api.state import _ws_chunk_frames, WS_CHUNK_SIZE


def test_small_message_passthrough():
    frames = _ws_chunk_frames({"type": "message", "content": "你好"})
    assert len(frames) == 1
    assert json.loads(frames[0])["content"] == "你好"


def test_big_message_splits_and_roundtrips():
    big = {"type": "history_steps", "steps": [{"full_result": "x" * 50000}]}
    frames = _ws_chunk_frames(big)
    assert len(frames) > 1
    # 每帧都不超限
    for f in frames:
        assert len(f.encode("utf-8")) <= WS_CHUNK_SIZE + 200  # 帧头开销
        obj = json.loads(f)
        assert obj["type"] == "_chunk"
        assert obj["n"] == len(frames)
    # 乱序重组还原（模拟前端 createChunkReassembler）
    parts = {}
    for f in frames:
        obj = json.loads(f)
        parts[obj["i"]] = obj["data"]
    joined = "".join(parts[i] for i in range(len(frames)))
    assert json.loads(joined) == big


def test_exact_boundary_message():
    """接近阈值的边界消息：8KB 以内不拆，超过才拆。"""
    small = {"d": "y" * (WS_CHUNK_SIZE - 100)}
    assert len(_ws_chunk_frames(small)) == 1
    big = {"d": "y" * (WS_CHUNK_SIZE * 3)}
    assert len(_ws_chunk_frames(big)) >= 3
