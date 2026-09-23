# -*- coding: utf-8 -*-
"""litellm 懒加载测试：import core.llm_client 不得触发 litellm 顶层导入。

背景：litellm 顶层导入实测 ~4.4s（冻结包更重），曾压在启动 splash
关键路径。懒加载后由预热线程并发触发，首次调用走 _llm() 缓存。
"""
import subprocess
import sys


def _run_fresh(code: str) -> str:
    import os
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       text=True, cwd=repo, timeout=120)
    return r.stdout + r.stderr


def test_llm_client_does_not_import_litellm_at_module_level():
    out = _run_fresh(
        "import sys\n"
        "sys.path.insert(0, '.')\n"
        "import core.llm_client\n"
        "assert 'litellm' not in sys.modules, 'litellm 仍在模块顶层被导入'\n"
        "print('NO_TOP_IMPORT_OK')\n"
    )
    assert "NO_TOP_IMPORT_OK" in out, out


def test_llm_lazy_getter_caches_and_configures():
    out = _run_fresh(
        "import sys\n"
        "sys.path.insert(0, '.')\n"
        "import core.llm_client as c\n"
        "lt = c._llm()\n"
        "assert lt is c._llm(), '缓存失效'\n"
        "assert lt.num_tokens_logging is False\n"
        "assert lt.supports_token_counter is False\n"
        "assert c.litellm.completion == lt.completion, '代理属性转发失效'\n"
        "print('LAZY_OK')\n"
    )
    assert "LAZY_OK" in out, out


def test_llm_client_module_getattr_works():
    """代理对象转发属性；except 占位在加载后替换为真实异常类。"""
    out = _run_fresh(
        "import sys\n"
        "sys.path.insert(0, '.')\n"
        "import core.llm_client as c\n"
        "c._llm()\n"
        "try:\n"
        "    raise c.ContextWindowExceededError('x', 'm', 'p')\n"
        "except c.ContextWindowExceededError:\n"
        "    print('EXCEPT_OK')\n"
        "assert c.ContextWindowExceededError is not Exception, '占位未被替换'\n"
    )
    assert "EXCEPT_OK" in out, out
