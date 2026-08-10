# -*- coding: utf-8 -*-
"""LLM 兜底混合检索回归：bigram 零命中但语义相关的查询应命中技能
（生产实证：「outputs里那个太阳雨，你往后续写5章」字面零交集，
human-writing 不可见，agent 转而从 GitHub 克隆同名仓库）。
向量方案依赖 sentence-transformers（本环境未装），改用 LLM 一次性判定。"""
import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from core.skill_store import SkillStore  # noqa: E402


def _store():
    ss = SkillStore.__new__(SkillStore)
    ss.skills_dir = "/nonexist"
    ss.index = {"skills": [
        {"filename": "human-writing", "title": "活人感写作",
         "description": "小说故事创作与改稿", "keywords": [],
         "success_rate": 1.0, "usage_count": 0},
        {"filename": "api_tester.md", "title": "API 测试技能",
         "description": "测试接口验证端点", "keywords": ["api", "测试"],
         "success_rate": 1.0, "usage_count": 0},
    ]}
    return ss


def _mock_llm(monkeypatch, answer):
    class _Msg:
        content = answer

    class _Choice:
        message = _Msg()

    class _Resp:
        choices = [_Choice()]

    class _Client:
        def chat(self, messages=None, **kw):
            return _Resp(), "m"

    monkeypatch.setattr("core.llm_client.LLMClient", lambda: _Client())


class TestSemanticRetrieve:
    def test_llm_fallback_hits(self, monkeypatch):
        """字面零命中 → LLM 兜底挑出 human-writing。"""
        ss = _store()
        _mock_llm(monkeypatch, '["human-writing"]')
        hits = ss.retrieve_semantic("outputs里那个太阳雨，你往后续写5章")
        assert hits and hits[0]["filename"] == "human-writing"

    def test_llm_says_none(self, monkeypatch):
        """LLM 判不相关 → 不注入（宁缺毋滥）。"""
        ss = _store()
        _mock_llm(monkeypatch, "[]")
        assert ss.retrieve_semantic("今天天气怎么样") == []

    def test_llm_error_degrades_gracefully(self, monkeypatch):
        """LLM 调用失败 → 静默退化（空结果，无异常）。"""
        ss = _store()

        class _Bad:
            def chat(self, **kw):
                raise RuntimeError("no key")
        monkeypatch.setattr("core.llm_client.LLMClient", lambda: _Bad())
        assert ss.retrieve_semantic("续写5章") == []

    def test_literal_hit_skips_llm(self, monkeypatch):
        """字面已命中 → 不再花 LLM 调用（控制预载成本）。"""
        ss = _store()
        called = {"n": 0}

        class _C:
            def chat(self, **kw):
                called["n"] += 1
                raise AssertionError("不应调用 LLM")
        monkeypatch.setattr("core.llm_client.LLMClient", lambda: _C())
        hits = ss.retrieve_semantic("帮我测试 api 接口")
        assert hits and hits[0]["filename"] == "api_tester.md"
        assert called["n"] == 0

    def test_llm_output_filtered_to_known(self, monkeypatch):
        """LLM 幻觉出的不存在 filename 被过滤。"""
        ss = _store()
        _mock_llm(monkeypatch, '["human-writing", "not-a-skill"]')
        hits = ss.retrieve_semantic("写小说")
        assert [h["filename"] for h in hits] == ["human-writing"]
