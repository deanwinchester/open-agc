# -*- coding: utf-8 -*-
"""委派上下文隔离修复测试（zxsai 事故）：

- _build_context_brief：目标 / 近期用户消息 / 绝对路径（含中文路径）提取，≤500 字
- _should_delegate 调试延续闸门：连续调试场景（报错粘贴 + 同主题）不委派；
  新独立复杂任务仍委派
- _decompose_task：prompt 注入会话上下文与"禁止寻仓"指示
- SubAgent：context_brief 注入 system prompt；dispatch_subagent 工具透传简报
"""
import inspect
import json
import os
import sys
import types

import pytest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from agent.agent import OpenAGCAgent, _session_paths, _topic_tokens  # noqa: E402
from agent.sub_agent import SubAgent, TOOL_SETS  # noqa: E402
from tools.subagent_dispatch import DispatchSubagentTool  # noqa: E402

ZXSAI = "D:\\中新社\\gitlab\\zxsai"


def _bare_agent(messages=None, llm=None):
    """绕过 __init__ 的最小 agent：只带 messages / llm 两个属性。"""
    a = object.__new__(OpenAGCAgent)
    a.messages = messages or []
    a.llm = llm
    return a


# zxsai 事故复刻：长报错粘贴，且含足以触发旧规则委派的复杂度词
ERR_INPUT = (
    "Spring Boot 启动失败，所有服务都起不来，先检查数据库再部署。\n"
    "Caused by: java.sql.SQLSyntaxErrorException: "
    "Table 'zxsai.user' doesn't exist\n"
    "ERROR 8612 --- [main] o.s.b.SpringApplication: Application run failed"
)

DEBUG_MESSAGES = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": f"帮我修复 {ZXSAI} 的 launch.json"},
    {"role": "assistant", "content": f"已修改 {ZXSAI}\\.vscode\\launch.json，端口改为 8080。"},
    {"role": "user", "content": "zxsai 项目启动还是有问题"},
    {"role": "assistant", "content": "我检查了 zxsai 的配置，数据库连接指向 localhost。"},
]


# ────────────────────────── _build_context_brief ──────────────────────────

class TestBuildContextBrief:
    def test_extracts_goal_recent_messages_and_chinese_path(self):
        brief = _bare_agent(DEBUG_MESSAGES)._build_context_brief()
        assert ZXSAI in brief                      # 中文路径提取
        assert "当前任务目标：zxsai 项目启动还是有问题" in brief
        assert f"帮我修复 {ZXSAI} 的 launch.json" in brief  # 近期消息摘要
        assert len(brief) <= 500

    def test_paths_deduped_and_capped_at_five(self):
        msgs = [
            {"role": "user", "content": "看看这些: " +
             " ".join(f"D:\\proj\\p{i}" for i in range(8)) + " 再说 D:\\proj\\p0"},
            {"role": "user", "content": "接下来怎么做"},
        ]
        brief = _bare_agent(msgs)._build_context_brief()
        path_line = [l for l in brief.split("\n") if l.startswith("会话涉及路径：")][0]
        listed = path_line.replace("会话涉及路径：", "").split("；")
        assert len(listed) == 5                    # 最多 5 个
        assert len(set(listed)) == 5               # 去重

    def test_posix_path_extracted(self):
        msgs = [{"role": "user", "content": "部署 /home/ubuntu/myapp 这个服务"}]
        brief = _bare_agent(msgs)._build_context_brief()
        assert "/home/ubuntu/myapp" in brief

    def test_user_message_truncated_to_100_chars(self):
        msgs = [{"role": "user", "content": "长" * 300}]
        brief = _bare_agent(msgs)._build_context_brief()
        for line in brief.split("\n"):
            body = line.split("：", 1)[-1].lstrip("- ")
            assert len(body) <= 100

    def test_empty_messages_returns_empty(self):
        assert _bare_agent([])._build_context_brief() == ""

    def test_multimodal_content_handled(self):
        msgs = [{"role": "user", "content": [
            {"type": "text", "text": f"看下 {ZXSAI} 的问题"},
            {"type": "image_url", "image_url": {"url": "data:..."}},
        ]}]
        brief = _bare_agent(msgs)._build_context_brief()
        assert ZXSAI in brief


# ────────────────────────── 调试延续闸门 ──────────────────────────

class TestDebuggingContinuationGate:
    def test_continuous_debugging_not_delegated(self):
        """事故复刻：同主题报错粘贴，即使命中复杂度词也不委派。"""
        agent = _bare_agent(DEBUG_MESSAGES)
        # 前置：该输入按旧三支规则本应委派（所有 + 先.*再 + 部署）
        assert OpenAGCAgent._should_delegate(object(), ERR_INPUT) is True
        # 有调试延续上下文 → 闸门拦截
        assert agent._should_delegate(ERR_INPUT) is False

    def test_new_independent_task_still_delegated(self):
        """无会话上下文（新会话）→ 闸门不拦，照委派。"""
        assert _bare_agent([])._should_delegate(ERR_INPUT) is True

    def test_unrelated_topic_still_delegated(self):
        """最近在聊别的项目 → 主题不重叠，不视为调试延续。"""
        msgs = [
            {"role": "user", "content": "帮我整理 D:\\电影库 的文件夹"},
            {"role": "assistant", "content": "已把 D:\\电影库 按年份分类完成。"},
            {"role": "assistant", "content": "电影库整理全部完成，共 120 部。"},
        ]
        assert _bare_agent(msgs)._should_delegate(ERR_INPUT) is True

    def test_non_error_input_not_gated(self):
        """非报错类输入不触发闸门，走原有三支规则。"""
        agent = _bare_agent(DEBUG_MESSAGES)
        assert agent._should_delegate("把 zxsai 的所有模块分别做全面分析并部署") is True
        assert agent._should_delegate("zxsai 的 readme 帮我看下") is False

    def test_bare_object_self_still_works(self):
        """兼容旧测试：self 无 messages 属性时闸门安全跳过。"""
        assert OpenAGCAgent._should_delegate(object(), "部署并监控这个服务") is True
        assert OpenAGCAgent._should_delegate(object(), "看下这个报错") is False


# ────────────────────────── _decompose_task 注入上下文 ──────────────────────────

class _StubLLM:
    def __init__(self, payload):
        self.prompts = []
        self._payload = payload

    def chat(self, messages=None, tools=None):
        self.prompts.append(messages[-1]["content"])
        msg = types.SimpleNamespace(content=self._payload)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)]), None


class TestDecomposeTaskContext:
    PAYLOAD = ('[{"id": 1, "task": "创建缺失的数据表", "tools": ["code"],'
               ' "depends_on": [], "max_iterations": 10}]')

    def test_prompt_contains_brief_and_no_repo_hunting_rule(self):
        llm = _StubLLM(self.PAYLOAD)
        agent = _bare_agent(DEBUG_MESSAGES, llm)
        plans = agent._decompose_task(ERR_INPUT)
        prompt = llm.prompts[0]
        assert "会话上下文" in prompt
        assert ZXSAI in prompt                       # 上下文路径进入 prompt
        assert "寻找/定位代码仓库" in prompt            # 禁止寻仓指示
        assert plans and plans[0]["task"] == "创建缺失的数据表"
        assert set(plans[0]["tools"]) == set(TOOL_SETS["code"]["tools"])

    def test_prompt_without_context_still_has_rule(self):
        """无上下文时无简报段落，但禁止寻仓指示仍在。"""
        llm = _StubLLM(self.PAYLOAD)
        agent = _bare_agent([], llm)
        agent._decompose_task("分别部署并监控所有服务")
        prompt = llm.prompts[0]
        assert "分解时必须以此为依据" not in prompt   # 简报段落未注入
        assert "寻找/定位代码仓库" in prompt


# ────────────────────────── SubAgent 注入 brief ──────────────────────────

class TestSubAgentContextBrief:
    def test_brief_injected_into_system_prompt(self):
        sub = SubAgent(task="修复建表", tools=[], parent_tools={},
                       llm_client=None,
                       context_brief=f"会话涉及路径：{ZXSAI}")
        sys_msg = sub.messages[0]["content"]
        assert "## 会话上下文" in sys_msg
        assert ZXSAI in sys_msg

    def test_no_brief_keeps_prompt_unchanged(self):
        sub = SubAgent(task="t", tools=[], parent_tools={}, llm_client=None)
        assert "## 会话上下文" not in sub.messages[0]["content"]


class _BriefStubAgent:
    """带 _build_context_brief 的最小 agent 上下文。"""

    def __init__(self):
        self.llm = object()
        self.full_available_tools = {"execute_shell": object()}
        self._session_sandbox_whitelist = None
        self._session_network_whitelist = None
        self._session_permission_whitelist = None
        self.session_id = 1

    def _build_context_brief(self):
        return "BRIEF-CTX"


class _CaptureSubAgent:
    last_init = None

    def __init__(self, **kwargs):
        _CaptureSubAgent.last_init = kwargs

    def run(self):
        return {"success": True, "summary": "ok", "output_files": [],
                "iterations_used": 1, "tool_calls": 0, "duration": 0.1,
                "steps": []}


@pytest.fixture
def capture_sub_agent(monkeypatch):
    _CaptureSubAgent.last_init = None
    monkeypatch.setattr("agent.sub_agent.SubAgent", _CaptureSubAgent)
    return _CaptureSubAgent


class TestDispatchPassesBrief:
    def test_dispatch_forwards_context_brief(self, capture_sub_agent):
        out = DispatchSubagentTool().execute(task="部署这个服务",
                                             _agent_context=_BriefStubAgent())
        assert json.loads(out)["success"] is True
        assert capture_sub_agent.last_init["context_brief"] == "BRIEF-CTX"

    def test_dispatch_without_brief_fn_falls_back_empty(self, capture_sub_agent):
        agent = types.SimpleNamespace(          # 无 _build_context_brief 的上下文
            llm=object(),
            full_available_tools={"execute_shell": object()},
        )
        DispatchSubagentTool().execute(task="部署这个服务", _agent_context=agent)
        assert capture_sub_agent.last_init["context_brief"] == ""


# ────────────────────────── system prompt 验证指引 ──────────────────────────

class TestSystemPromptVerificationRule:
    def test_failure_section_requires_actual_verification(self):
        import agent.agent as agent_mod
        src = inspect.getsource(agent_mod)
        assert "必须用 execute_shell 或 execute_python 实际验证" in src
        assert "未经验证的参数调整" in src


# ────────────────────────── 评审缺陷回归（5 处） ──────────────────────────

class TestReviewDefect1PathSurvivesCap:
    """缺陷1：路径行曾被 500 字截断——目标行+5 条长摘要把路径挤出预算。"""

    def test_paths_survive_500_cap_with_long_messages(self):
        long_err = "ERROR 启动失败 " + "堆栈信息" * 40   # >100 字的长报错
        msgs = [{"role": "user", "content": f"帮我看下 {ZXSAI} 项目"}]
        msgs += [{"role": "user", "content": f"第{i}次尝试 {long_err}"}
                 for i in range(5)]
        brief = _bare_agent(msgs)._build_context_brief()
        assert len(brief) <= 500
        assert ZXSAI in brief                       # 中文路径必须保留
        path_pos = brief.find("会话涉及路径")
        assert path_pos != -1
        # 路径行排在摘要之前（目标行 → 路径行 → 摘要）
        assert brief.find("当前任务目标") < path_pos
        summary_pos = brief.find("\n- ")
        assert summary_pos == -1 or path_pos < summary_pos


class TestReviewDefect2UrlNotPath:
    """缺陷2：URL 被盘符分支误判为路径（https:// → s://cdn...）。"""

    def test_url_not_extracted_as_path(self):
        assert _session_paths("见 https://cdn.example.com/x.png") == []
        assert _session_paths("x=https://cdn.example.com/a.png") == []
        assert _session_paths("镜像 http://a.com/home/u/f.png 失效") == []

    def test_real_paths_still_extracted_alongside_urls(self):
        paths = _session_paths(
            f"配置在 {ZXSAI}\\application.yml，参考 https://example.com/doc")
        assert paths == [ZXSAI + "\\application.yml"]


class TestReviewDefect3DriveLetterToken:
    """缺陷3：盘符组件 'd:' 成为话题 token，无关 D: 路径间假重叠。"""

    def test_drive_letter_not_topic_token(self):
        tokens = _topic_tokens("D:\\proj\\app")
        assert "d:" not in tokens
        assert "proj" in tokens and "app" in tokens

    def test_unrelated_drive_paths_do_not_fake_overlap(self):
        """输入带 D:\\proj\\app，会话在整理 D:\\电影库——仅有 'd:' 相同不算同主题。"""
        msgs = [
            {"role": "user", "content": "整理 D:\\电影库"},
            {"role": "assistant", "content": "已把 D:\\电影库 整理完毕。"},
            {"role": "assistant", "content": "D:\\电影库 全部完成。"},
        ]
        err = ("D:\\proj\\app 启动失败 ERROR Table 'app.user' doesn't exist，"
               "所有模块先排查再部署")
        assert _bare_agent(msgs)._should_delegate(err) is True


class TestReviewDefect4PathWithSpaces:
    """缺陷4：带空格路径 D:\\My Documents\\proj 曾被截成 D:\\My。"""

    def test_space_path_extracted_whole(self):
        assert _session_paths("打开 D:\\My Documents\\proj 然后看看") == \
            ["D:\\My Documents\\proj"]

    def test_quoted_space_path(self):
        assert _session_paths('日志在 "D:\\My Documents\\proj" 里') == \
            ["D:\\My Documents\\proj"]

    def test_space_separated_path_list_stays_split(self):
        """多个空格分隔的独立路径不得被空格续段规则合并。"""
        assert _session_paths("D:\\proj\\p0 D:\\proj\\p1") == \
            ["D:\\proj\\p0", "D:\\proj\\p1"]

    def test_trailing_latin_word_not_swallowed(self):
        """路径后紧跟无分隔符的英文单词不是路径一部分。"""
        assert _session_paths("D:\\proj\\p0 backup 已完成") == ["D:\\proj\\p0"]


class TestReviewDefect5PureChineseError:
    """缺陷5：纯中文报错提取不到话题词，旧闸门直接放行导致过度委派。"""

    def test_pure_chinese_error_conservative_no_delegate(self):
        msgs = [
            {"role": "user", "content": "帮我把项目跑起来"},
            {"role": "assistant", "content": "已经在本地启动了，你看下日志。"},
        ]
        err = "又报错了：系统表不存在，所有服务都起不来，先分别排查再处理"
        # 前置：该输入按三支规则本应委派（所有 + 分别 + 先.*再）
        assert OpenAGCAgent._should_delegate(object(), err) is True
        # 会话进行中 + 无可用 token → 保守不委派
        assert _bare_agent(msgs)._should_delegate(err) is False

    def test_pure_chinese_error_empty_session_still_delegated(self):
        """新会话（无 assistant 历史）不适用保守分支，照规则委派。"""
        err = "又报错了：系统表不存在，所有服务都起不来，先分别排查再处理"
        assert _bare_agent([])._should_delegate(err) is True
