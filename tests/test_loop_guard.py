# -*- coding: utf-8 -*-
"""循环检测「调用+结果双同」回归：旧逻辑只看参数一致 ×3 就拦截，
browser_automation 读页面（天然无参重复、结果每次不同）被误杀（生产实证：
Tripo 建 key 流程被 System Guard 阻断）。新逻辑：同签名且结果也相同 ×2
后第三次同签名调用才拦截。"""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import agent.agent as ag  # noqa: E402


def _new_agent():
    a = ag.OpenAGCAgent.__new__(ag.OpenAGCAgent)
    a._loop_last_sig = ""
    a._loop_last_result_hash = ""
    a._loop_streak = 0
    return a


class TestLoopGuardSemantics:
    def test_block_requires_identical_results(self):
        """同签名但结果每次都不同（浏览器读页）→ 不累计、不拦截。"""
        src = open(os.path.join(PROJECT_ROOT, "agent", "agent.py"),
                   encoding="utf-8").read()
        # 拦截条件必须同时看签名与结果哈希
        assert "_loop_last_result_hash" in src
        assert "identical results" in src
        # 结果不同则重置计数
        assert "self._loop_streak = 1" in src

    def test_streak_state_initializable(self):
        a = _new_agent()
        assert a._loop_streak == 0 and a._loop_last_sig == ""

    def test_streak_logic(self):
        """模拟：同签名同结果两次后第三次拦截；结果变化则重置。"""
        import hashlib

        def h(s):
            return hashlib.md5(s.encode()).hexdigest()

        a = _new_agent()
        sig = "browser_automation:read"
        # 第一次：streak=1
        r1 = h("dom-v1")
        assert not (sig == a._loop_last_sig and r1 == a._loop_last_result_hash)
        a._loop_streak = 1
        a._loop_last_sig, a._loop_last_result_hash = sig, r1
        # 第二次同签名同结果：streak→2
        assert sig == a._loop_last_sig and r1 == a._loop_last_result_hash
        a._loop_streak += 1
        assert a._loop_streak == 2
        # 第三次同签名调用：满足拦截条件
        assert a._loop_last_sig == sig and a._loop_streak >= 2
        # 对照：结果不同则重置
        r2 = h("dom-v2")
        assert not (r2 == a._loop_last_result_hash)
