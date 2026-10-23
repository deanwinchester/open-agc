# -*- coding: utf-8 -*-
"""pip_install 工具：agent 自助安装 Python 包到用户扩展库。

背景：打包版 execute_python 走嵌入解释器（--pyrun），spec 排除了 pip 时
agent 缺包只能绕路写替代脚本（生产实证：写 xlsx 炸出致命弹窗、编辑用户
被骚扰）。本工具打通自助装包链路：嵌入解释器经 gui_app 的
``--pyrun -m pip`` 模块通道运行 pip，包安装到数据目录 pylibs/（持久），
execute_python 子进程环境自动带 PYTHONPATH=该目录（tools/python_repl.py），
装一次后续所有执行可用。

安全边界：package 必须是合法包名（字母数字 . _ - 与可选版本约束），
防注入；安装源默认清华镜像（内网实测可达，aliyun 在多台构建机断流实证）。
"""
import os
import re
import subprocess
import sys
from typing import Any, Dict

from tools.base import BaseTool

_PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*(\[[A-Za-z0-9,._-]+\])?"
                     r"(==|>=|<=|~=|!=|>|<)?[A-Za-z0-9.*+-]*$")
_DEFAULT_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"


def pylibs_dir() -> str:
    """用户扩展库目录（execute_python 的 PYTHONPATH 同源）。"""
    from core.paths import get_data_path
    d = get_data_path("pylibs")
    os.makedirs(d, exist_ok=True)
    return d


class PipInstallTool(BaseTool):
    name: str = "pip_install"
    description: str = (
        "安装 Python 包到用户扩展库（execute_python 缺包时用，装一次持久可用；"
        "装完直接在后续代码里 import）。仅装纯 Python/预编译 wheel 包；"
        "需要编译的包可能失败。"
    )

    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "package": {
                            "type": "string",
                            "description": "包名（可带版本约束，如 requests 或 openpyxl>=3.1）。"
                        },
                        "index_url": {
                            "type": "string",
                            "description": "可选：pip 源（默认清华镜像）。"
                        },
                    },
                    "required": ["package"],
                },
            },
        }

    def _pip_cmd_prefix(self) -> list:
        """pip 调用前缀：冻结包走 --pyrun -m pip 模块通道；源码环境直接 -m pip。"""
        if getattr(sys, "frozen", False):
            return [sys.executable, "--pyrun", "-m", "pip"]
        return [sys.executable, "-m", "pip"]

    def execute(self, package: str = "", index_url: str = "", **kwargs) -> str:
        package = str(package or "").strip()
        if not package or not _PKG_RE.match(package):
            return (f"Error: 非法包名 '{package}'。只允许字母数字 . _ - 与"
                    f"可选版本约束（如 requests、openpyxl>=3.1）。")
        target = pylibs_dir()
        index = str(index_url or "").strip() or _DEFAULT_INDEX
        cmd = self._pip_cmd_prefix() + [
            "install", "--target", target, "--retries", "3", "--timeout", "30",
            "-i", index, package,
        ]
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            from core.process import NO_WINDOW_KW
        except Exception:
            NO_WINDOW_KW = {}
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace",
                                  timeout=300, env=env, **NO_WINDOW_KW)
        except subprocess.TimeoutExpired:
            return f"Error: pip 安装 {package} 超时（300s）——网络过慢或源不可达，可换 index_url 重试。"
        except Exception as e:
            return f"Error: pip 安装 {package} 执行失败: {e}"
        tail = ((proc.stdout or "") + (proc.stderr or "")).strip()[-800:]
        if proc.returncode != 0:
            return (f"Error: pip 安装 {package} 失败（exit {proc.returncode}）。\n"
                    f"{tail}\n可换 index_url 或检查包名/网络后重试。")
        return (f"安装成功：{package} → 用户扩展库（持久可用，后续 "
                f"execute_python 直接 import 即可）。\npip 输出尾部:\n{tail}")
