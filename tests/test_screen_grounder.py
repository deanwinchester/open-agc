# -*- coding: utf-8 -*-
"""tools.screen_grounder 的坐标解析与配置检测测试。"""
from tools.screen_grounder import parse_point


class TestParsePoint:
    """解析模型回复中的坐标点：默认绝对像素（UI-TARS 惯例），
    归一化制式按 mode 显式指定。"""

    W, H = 1920, 1080

    def test_absolute(self):
        assert parse_point("(878, 1052)", self.W, self.H) == (878, 1052)

    def test_uitars_style(self):
        assert parse_point("click(start_box='(1345, 692)')", self.W, self.H) == (1345, 692)

    def test_norm1_autodetect_in_abs_mode(self):
        # 两值都 ≤1.0 且带小数——不可能是像素坐标，按 [0,1] 归一化
        assert parse_point("(0.457, 0.974)", self.W, self.H) == (877, 1052)

    def test_norm1_explicit(self):
        assert parse_point("(0.5, 0.5)", self.W, self.H, mode="norm1") == (960, 540)

    def test_norm1000_explicit(self):
        assert parse_point("(457, 974)", self.W, self.H, mode="norm1000") == (877, 1052)

    def test_small_absolute_not_rescaled(self):
        # (100,200) 这种绝对坐标绝不能被 [0,1000] 猜测误放大（实证 bug）
        assert parse_point("(100, 200)", self.W, self.H) == (100, 200)

    def test_out_of_bounds_rejected(self):
        assert parse_point("(5000, 300)", self.W, self.H) is None

    def test_no_point(self):
        assert parse_point("找不到目标", self.W, self.H) is None
        assert parse_point("", self.W, self.H) is None
        assert parse_point(None, self.W, self.H) is None

    def test_first_point_wins(self):
        # UI-TARS 有时输出多组坐标（如 start_box + end_box），取第一组
        assert parse_point("(100, 200) to (300, 400)", self.W, self.H) == (100, 200)


class TestGrounderGating:
    """未配置 grounder 时 locate 不注入（schema/定位规程都不出现）——
    用户明确要求：没配置就不生效、不注入，避免模型调到「未配置」错误。"""

    def _set_ready(self, monkeypatch, ready):
        import tools.screen_grounder as sg
        monkeypatch.setattr(sg, "grounder_ready", lambda: ready)
        # computer.py 在函数内 from import，patch 模块属性即可生效
        return sg

    def test_schema_omits_locate_when_unconfigured(self, monkeypatch):
        self._set_ready(monkeypatch, False)
        from tools.computer import ComputerTool
        schema = ComputerTool().get_openai_schema()["function"]
        assert "locate" not in schema["parameters"]["properties"]["action"]["description"]
        assert "target" not in schema["parameters"]["properties"]
        assert "click" not in schema["parameters"]["properties"]

    def test_schema_includes_locate_when_configured(self, monkeypatch):
        self._set_ready(monkeypatch, True)
        from tools.computer import ComputerTool
        schema = ComputerTool().get_openai_schema()["function"]
        assert "locate" in schema["parameters"]["properties"]["action"]["description"]
        assert "target" in schema["parameters"]["properties"]

    def test_guide_omits_locate_when_unconfigured(self, monkeypatch):
        self._set_ready(monkeypatch, False)
        from tools.computer import get_grounding_guide
        assert "locate" not in get_grounding_guide()

    def test_guide_includes_locate_when_configured(self, monkeypatch):
        self._set_ready(monkeypatch, True)
        from tools.computer import get_grounding_guide
        assert "locate" in get_grounding_guide()
