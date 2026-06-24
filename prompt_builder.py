"""
System prompt building mixin for OpenAGCAgent.

Extracted from agent/agent.py to reduce the 2649-line monolith.
"""
import os
import platform
import shutil
from datetime import datetime
from core.paths import get_data_path


def detect_system_env() -> str:
    """Detect the current system environment and return a description string."""
    system = platform.system()
    os_name = system
    if system == "Darwin":
        mac_ver = platform.mac_ver()[0]
        os_name = f"macOS {mac_ver}" if mac_ver else "macOS"
    elif system == "Windows":
        win_ver = platform.version()
        os_name = f"Windows {win_ver}" if win_ver else "Windows"
    elif system == "Linux":
        try:
            import subprocess
            distro = subprocess.run(["lsb_release", "-ds"], capture_output=True, text=True, timeout=3).stdout.strip()
            os_name = distro or "Linux"
        except Exception:
            os_name = "Linux"

    arch = platform.machine()
    default_shell = os.environ.get("SHELL", "unknown").split("/")[-1]
    py_ver = platform.python_version()

    pkg_managers = []
    if shutil.which("brew"): pkg_managers.append("brew")
    if shutil.which("apt"): pkg_managers.append("apt")
    if shutil.which("pip3"): pkg_managers.append("pip3")
    elif shutil.which("pip"): pkg_managers.append("pip")
    pkg_hint = ", ".join(pkg_managers) if pkg_managers else "未检测到常见包管理器"

    home = os.path.expanduser("~")

    sudo_available = "sudo" if shutil.which("sudo") else ""
    sudo_hint = ""
    if sudo_available and system != "Windows":
        sudo_hint = ("sudo 可用。注意：在子进程中运行时，sudo 没有 TTY 无法交互式输入密码。"
                     "需要使用 -S 从 stdin 读密码，或用 -n 跳过密码（需 NOPASSWD 配置），"
                     "或使用 `echo password | sudo -S command`")

    parts = [
        f"# 系统环境",
        f"- 操作系统：{os_name}",
        f"- 架构：{arch}",
        f"- 默认 Shell：{default_shell}",
        f"- Python 版本：{py_ver}",
        f"- 包管理器：{pkg_hint}",
        f"- 用户主目录：{home}",
    ]

    if system == "Windows":
        parts.append(
            f"- **Windows 注意事项**：PowerShell 中的 curl 命令是 Invoke-WebRequest 别名，"
            f"需使用 curl.exe。中文乱码可在命令前添加 "
            f"`$OutputEncoding = [Console]::OutputEncoding = [Text.UTF8Encoding]::new()`。")
    elif system == "Darwin":
        parts.append(
            f"- **macOS 注意事项**：使用 brew 安装软件。Shell 工具使用 zsh/bash，"
            f"不支持 PowerShell 语法。系统偏好中文界面。")
    elif system == "Linux":
        parts.append(f"- **Linux 注意事项**：使用 apt/yum 安装软件，标准 POSIX shell 环境。")

    if sudo_hint:
        parts.append(f"- **sudo 注意事项**：{sudo_hint}")

    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        from core.paths import get_data_dir, get_skills_dir, get_bin_dir
        parts.append(
            f"- **Docker 持久化指导**：\n"
            f"  1. 下载的文件和生成的脚本必须放在 workspace/ 或 data/ 下——"
            f"仅这两个目录是持久化卷（VOLUME），其它目录在容器重启后会丢失\n"
            f"  2. 学到的技能(save_learned_skill)自动持久化到 data/skills/\n"
            f"  3. 安装的系统包(apt/pip)重启后会丢失，用完后即刻完成任务，"
            f"不要依赖重启后仍存在\n"
            f"  4. 持久数据目录: {get_data_dir()}\n"
            f"  5. 技能目录: {get_skills_dir()}\n"
            f"  6. 二进制目录: {get_bin_dir()}")

    return "\n".join(parts)


class PromptBuilderMixin:
    """Mixin providing _build_tool_list_section and _build_system_prompt."""

    def _build_tool_list_section(self) -> str:
        """Build a markdown section listing all available tools."""
        lines = ["\n## 全量工具列表\n",
                 "以下是你可用的所有工具"
                 "（核心工具已加载完整用法，其余工具通过 search_available_tools 加载完整用法）：\n"]
        CORE_NAMES = {"execute_shell", "read_file", "write_file", "edit_file",
                       "search_file_content", "find_files", "execute_python",
                       "search_web", "manage_memory", "ask_user_question",
                       "search_history", "queue_download", "pause_and_wait",
                       "self_review", "search_available_tools", "configure_system", "compact_context"}
        core_items = []
        ext_items = []
        for name, tool in sorted(self.full_available_tools.items()):
            is_core = name in CORE_NAMES
            if is_core:
                desc = getattr(tool, 'description', '')[:60]
                item = f"  ✅ `{name}`"
                if desc:
                    item += f" — {desc}"
                core_items.append(item)
            else:
                ext_items.append(f"  🔧 `{name}`")
        if core_items:
            lines.append("核心工具（已就绪）：")
            lines.extend(core_items)
        if ext_items:
            lines.append("扩展工具（需通过 search_available_tools 唤醒）：")
            lines.extend(ext_items)
        return "\n".join(lines)

    def _build_system_prompt(self, memory_context: str = "", skill_context: str = "",
                             experience_context: str = "", kg_context: str = "") -> str:
        """Build the system prompt with all dynamic context injections."""
        current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        current_date = datetime.now().strftime("%Y年%m月%d日")

        prompt = self.system_prompt_base.replace("{current_time}", current_time)
        prompt = prompt.replace("{current_date}", current_date)
        prompt = prompt.replace("{cwd_dir}", self.sandbox_dir or os.getcwd())
        prompt = prompt.replace("{system_env}", detect_system_env())

        if hasattr(self, 'full_available_tools'):
            prompt += self._build_tool_list_section()

        if memory_context:
            prompt += f"\n--- 历史记忆回溯 (Episodic Memory) ---\n{memory_context}\n"

        # ── MEMORY.md: persistent facts (discovered paths, configs) ──
        memory_file_path = get_data_path("MEMORY.md")
        if os.path.exists(memory_file_path):
            try:
                with open(memory_file_path, "r", encoding="utf-8") as f:
                    content = f.read().strip()
                    if content:
                        prompt += f"\n--- 全局核心设定与事实库 (MEMORY.md) ---\n{content}\n"
            except Exception as e:
                print(f"Failed to read MEMORY.md: {e}")
        else:
            prompt += f"\n持久化事实文件位于: {memory_file_path}（尚不存在，发现重要路径/配置后可创建）\n"

        # ── soul.md: agent personality / style config ──
        soul_path = get_data_path("soul.md")
        if os.path.exists(soul_path):
            try:
                with open(soul_path, "r", encoding="utf-8") as f:
                    soul = f.read().strip()
                    if soul:
                        prompt += f"\n--- 人格设定 (soul.md) ---\n{soul}\n"
            except Exception as e:
                print(f"Failed to read soul.md: {e}")

        if skill_context:
            prompt += f"\n{skill_context}"
        if experience_context:
            prompt += f"\n\n{experience_context}"
        if kg_context:
            prompt += f"\n\n{kg_context}"

        return prompt
