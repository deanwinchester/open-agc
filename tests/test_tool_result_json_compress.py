# -*- coding: utf-8 -*-
"""MCP/JSON 工具结果的结构化压缩与溢出指引的回归测试。

背景：MCP 工具（如 zxs_es_es_search）返回 JSON，但 compress_tool_result 按
工具名里的 "search" 路由到 web 搜索文本压缩器——JSON 没有「1. Title」条目行，
整个结果被当成单条裁到 400 字符，模型看到残片误判「工具输出被截断」，
转而用 execute_python 直接请求接口（生产实证）。修复：JSON 优先路由到
结构化压缩，并在截断时溢出全文到 sandbox 文件 + 附明确指引。
"""
import json
import os

from agent.agent import OpenAGCAgent
from agent.context_manager import compress_json_result, compress_tool_result


def _es_like_json(hits=50, field_len=800):
    return json.dumps({
        "query": "国庆",
        "indices": ["mcp_ncbdocument", "mcp_tcbdocument"],
        "total": 1144,
        "tookMs": 33,
        "hits": [{"_index": "mcp_tcb", "_id": str(i),
                  "title": f"标题{i}", "body": "正文" * field_len}
                 for i in range(hits)],
    }, ensure_ascii=False)


def test_non_json_returns_none():
    assert compress_json_result("plain text output\nline2") is None
    assert compress_json_result("") is None
    assert compress_json_result("{broken json") is None


def test_json_result_keeps_meta_and_shrinks_arrays():
    raw = _es_like_json()
    out = compress_json_result(raw, 4000)
    assert out is not None
    assert len(out) <= 4000
    # 标量元信息完整保留
    assert '"total": 1144' in out
    assert '"tookMs": 33' in out
    # 命中数组被裁条数，且说明不是工具故障
    assert "省略" in out
    assert "这不是工具故障" in out
    # 保留的条目仍是合法 JSON 片段（note 之后可解析）
    body = out.split("\n", 1)[1]
    parsed = json.loads(body)
    assert parsed["total"] == 1144
    assert 1 <= len(parsed["hits"]) <= 10


def test_json_routing_beats_name_routing():
    """工具名含 search 但返回 JSON 时必须走 JSON 压缩，而非 web 搜索压缩器。"""
    raw = _es_like_json()
    out = compress_tool_result(raw, "zxs_es_es_search", 4000)
    assert "这不是工具故障" in out
    assert '"hits"' in out
    # web 搜索压缩器会把它裁成约 400 字符的单条目，这里必须显著更完整
    assert len(out) > 1000


def test_truncate_spills_full_result_and_guides(tmp_path):
    agent = OpenAGCAgent.__new__(OpenAGCAgent)
    agent.sandbox_dir = str(tmp_path)
    raw = _es_like_json()
    out = agent._truncate_tool_result_for_context(raw, "zxs_es_es_search")
    assert len(out) <= 4000
    # 指引：不要绕过工具直接请求接口 + 溢出文件路径
    assert "不要改用 execute_python" in out
    assert "tool_results/" in out
    # 溢出文件存在且内容完整
    spill_dir = tmp_path / "tool_results"
    files = list(spill_dir.glob("zxs_es_es_search_*.txt"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8") == raw


def test_truncate_without_sandbox_attr_still_caps():
    """无 sandbox_dir 属性（如裸 __new__）时溢出静默跳过，仍守住 cap。"""
    agent = OpenAGCAgent.__new__(OpenAGCAgent)
    import tempfile
    cwd = tempfile.mkdtemp()
    old = os.getcwd()
    os.chdir(cwd)
    try:
        raw = _es_like_json()
        out = agent._truncate_tool_result_for_context(raw, "zxs_es_es_search")
        assert len(out) <= 4000
    finally:
        os.chdir(old)
