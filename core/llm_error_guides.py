# -*- coding: utf-8 -*-
"""LLM 错误的人话指引表（数据驱动，不写死在 agent 代码里）。

运行时播种到 data/llm_error_guides.json，用户可直接编辑该文件调整文案；
文件缺失/损坏时回退到内置默认表。

每条规则按顺序匹配，先中先得：
  match: 逗号分隔的关键字列表（小写），命中规则 =（任一关键字出现在
         异常类型名小写形式中）或（全部 err: 前缀的关键字出现在错误信息中）
  hint:  展示给用户的指引文案（支持 markdown 链接，前端会渲染成可点击）
"""
import json
import os
import re

from core.paths import get_data_path

# 内置默认表（也是播种模板）。注意 match 全部小写比较。
DEFAULT_GUIDES = [
    {
        "match": "authentication,permissiondenied,err: 401,err:invalid api key,err:invalid_api_key",
        "hint": ("模型服务拒绝了 API Key（未配置、无效或已过期）。"
                 "请到 [「设置 → 模型服务」](/app/settings/models) 检查对应厂商的密钥；"
                 "本地/自部署服务（llamacpp/vLLM 等）一般不校验 Key，随便填个占位值即可"),
    },
    {
        "match": "notfound,err:does not exist",
        "hint": ("模型名不存在或未部署。请到 [「设置 → 模型服务」](/app/settings/models) "
                 "核对模型名是否与服务商/本地服务一致"),
    },
    {
        "match": "ratelimit,err:rate limit,err: 429",
        "hint": ("模型服务限流中（请求过频或配额用尽）。稍等片刻点「继续」重试，"
                 "或临时切换到其它模型"),
    },
    {
        "match": "err:context length,err:context window,err:maximum context",
        "hint": ("对话超出模型上下文窗口。建议「新建对话」重来，"
                 "或在设置里调大上下文/换长上下文模型"),
    },
    {
        "match": "internalserver,err: 500",
        "hint": ("模型服务端内部错误（本地服务多为显存不足或推理崩溃）。"
                 "点「继续」重试；频繁出现请检查模型服务状态或更换模型"),
    },
    {
        "match": "apiconnection",
        "hint": ("连不上模型服务。检查服务地址是否正确、本地模型服务"
                 "（llamacpp/ollama 等）是否已启动"),
    },
    {
        "match": "timeout,err:timed out",
        "hint": ("模型服务响应超时。本地大模型长上下文推理可能较慢，"
                 "稍候点「继续」重试；反复超时请检查模型服务负载"),
    },
    {
        "match": "err:parse tool call,err:unterminated,err:expecting value",
        "hint": "模型连续返回了非法格式的工具调用（已自动重试仍失败）",
    },
]

FALLBACK_HINT = "模型服务调用失败"

_GUIDE_FILE = "llm_error_guides.json"


def _seed_path() -> str:
    return get_data_path(_GUIDE_FILE)


def _seed_if_missing() -> None:
    path = _seed_path()
    if not os.path.exists(path):
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_GUIDES, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def load_guides() -> list:
    """加载指引表（data/llm_error_guides.json，可编辑；损坏回退内置默认）。"""
    _seed_if_missing()
    try:
        with open(_seed_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list) and all("match" in g and "hint" in g for g in data):
            return data
    except Exception as e:
        print(f"[LLMErrorGuides] 指引表读取失败，回退内置默认: {e}")
    return DEFAULT_GUIDES


def render_llm_error_hint(exc_type: str, err_text: str) -> str:
    """按异常类型名 + 错误信息匹配人话指引。无命中返回 FALLBACK_HINT。"""
    et = (exc_type or "").lower()
    em = (err_text or "").lower()
    for guide in load_guides():
        keys = [k.strip() for k in str(guide.get("match", "")).split(",") if k.strip()]
        for key in keys:
            if key.startswith("err:"):
                if key[4:] in em:
                    return guide["hint"]
            elif key in et:
                return guide["hint"]
    return FALLBACK_HINT
