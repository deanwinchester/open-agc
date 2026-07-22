import os
from typing import Any, Dict
from tools.base import BaseTool, SandboxBlocked

class ReadFileTool(BaseTool):
    name: str = "read_file"
    description: str = "读取文件内容（带行号），大文件用 offset/limit 分页。已知路径时用；找文件用 find_files，搜内容用 search_file_content。受沙箱限制。"

    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "文件路径（绝对或相对）。"
                        },
                        "offset": {
                            "type": "integer",
                            "description": "起始行号，默认 1。"
                        },
                        "limit": {
                            "type": "integer",
                            "description": "最大行数，默认读全文件。"
                        }
                    },
                    "required": ["path"]
                }
            }
        }

    def execute(self, **kwargs) -> str:
        import json
        from core.paths import get_data_path
        
        path = kwargs.get("path")
        if not path:
            return "Error: No file path provided."
            
        # Sandbox Mode Enforcement
        config_path = get_data_path("config.json")
        if os.path.exists(config_path):
            config = {}
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
            except Exception:
                pass
            if config.get("sandbox_mode", True):
                whitelist = kwargs.get("_session_whitelist", None)
                self.check_sandbox(path, config=config, session_whitelist=whitelist)
                
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
                
            try:
                offset = int(kwargs.get("offset", 1) or 1)
            except (TypeError, ValueError):
                return "Error: offset must be an integer."
            limit = kwargs.get("limit")
            if limit is not None:
                try:
                    limit = int(limit)
                except (TypeError, ValueError):
                    return "Error: limit must be an integer."
                if limit < 1:
                    return "Error: limit must be >= 1."
            offset = max(1, offset)
            
            total = len(lines)
            if total == 0:
                return f"--- Content of {path} (empty file, 0 lines) ---"
            
            # 1-indexed to 0-indexed
            start_idx = offset - 1
            if start_idx >= total:
                return (f"--- Content of {path} (file has {total} lines) ---\n"
                        f"offset={offset} 超出文件末尾（共 {total} 行），请用 1 <= offset <= {total} 读取。")
            end_idx = min(total, start_idx + limit) if limit is not None else total
            
            subset = lines[start_idx:end_idx]
            
            # Format with line numbers (cat -n style)
            formatted_lines = []
            for i, line in enumerate(subset, start=start_idx + 1):
                clean_line = line.rstrip('\n')
                formatted_lines.append(f"{i:4d} | {clean_line}")
                
            content = "\n".join(formatted_lines)
            header = f"--- Content of {path} (Lines {start_idx + 1} to {end_idx} of {total}) ---"
            parts = [header]
            # Huge file without explicit pagination: still return everything
            # (unchanged behavior), but suggest paging.
            if limit is None and offset == 1 and total > 2000:
                parts.append(f"--- 提示：文件较大（共 {total} 行），建议用 offset/limit 分页读取，如 offset=1, limit=200 ---")
            parts.append(content)
            if end_idx < total:
                parts.append(f"--- 已显示到第 {end_idx} 行，剩余 {total - end_idx} 行；用 offset={end_idx + 1}（可加 limit）继续读取 ---")
            return "\n".join(parts)
        except Exception as e:
            return f"Error reading file {path}: {str(e)}"

class WriteFileTool(BaseTool):
    name: str = "write_file"
    description: str = "写入文件内容（存在覆盖、不存在创建）。局部小改用 edit_file。受沙箱限制。"

    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "文件路径（绝对或相对）。"
                        },
                        "content": {
                            "type": "string",
                            "description": "要写入的完整文本内容。"
                        }
                    },
                    "required": ["path", "content"]
                }
            }
        }

    def execute(self, **kwargs) -> str:
        import json
        from core.paths import get_data_path
        
        path = kwargs.get("path")
        content = kwargs.get("content", "")
        if not path:
            return "Error: No file path provided."
            
        # Sandbox Mode Enforcement
        config_path = get_data_path("config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                
                if config.get("sandbox_mode", True):
                    whitelist = kwargs.get("_session_whitelist", None)
                    self.check_sandbox(path, config=config, session_whitelist=whitelist)
            except SandboxBlocked:
                raise
            except Exception:
                pass
                
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
            return f"Successfully written to {path}."
        except Exception as e:
            return f"Error writing file {path}: {str(e)}"

def _as_bool(value) -> bool:
    """Normalize booleans that may arrive as strings from the model."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def _match_locations(content: str, old_string: str, limit: int = 10):
    """Return up to `limit` matches as (start_line, [(line_no, text, is_match), ...]).

    Each block covers the line before, the full line range of the match
    (multi-line old_string included), and the line after; out-of-range
    lines are omitted. Identical matches are disambiguated by context.
    """
    lines = content.split("\n")
    total = len(lines)
    matches = []
    start = 0
    while len(matches) < limit:
        idx = content.find(old_string, start)
        if idx == -1:
            break
        first = content.count("\n", 0, idx) + 1
        # Line holding the last char of old_string (a trailing '\n' ends the
        # previous line, so it does not extend the range).
        last = content.count("\n", 0, idx + len(old_string) - 1) + 1
        block = []
        for ln in range(max(1, first - 1), min(total, last + 1) + 1):
            text = lines[ln - 1]
            if len(text) > 80:
                text = text[:77] + "..."
            block.append((ln, text, first <= ln <= last))
        matches.append((first, block))
        start = idx + 1
    return matches


def _near_miss_lines(content: str, old_string: str, max_scan: int = 5000, top: int = 5):
    """Find file lines similar to old_string's first non-empty line (for hints)."""
    import difflib
    probe = ""
    for line in old_string.splitlines():
        if line.strip():
            probe = line.strip()
            break
    if not probe:
        return []
    scored = []
    for i, line in enumerate(content.splitlines()[:max_scan], start=1):
        s = line.strip()
        if not s:
            continue
        if probe in s or (len(s) >= 8 and s in probe):
            scored.append((1.0, i, s))
            continue
        ratio = difflib.SequenceMatcher(None, probe, s).ratio()
        if ratio >= 0.6:
            scored.append((ratio, i, s))
    scored.sort(key=lambda t: -t[0])
    out = []
    for _, line_no, text in scored[:top]:
        if len(text) > 80:
            text = text[:77] + "..."
        out.append((line_no, text))
    return out


class EditFileTool(BaseTool):
    name: str = "edit_file"
    description: str = "精确字符串替换，适合局部修改。改前需先 read_file 读过该文件；多文件批量修改用 apply_patch。受沙箱限制。"

    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "文件路径（绝对或相对）。"
                        },
                        "old_string": {
                            "type": "string",
                            "description": "被替换的原文，须与文件完全一致，不含行号前缀。"
                        },
                        "new_string": {
                            "type": "string",
                            "description": "新文本（保持缩进）。"
                        },
                        "replace_all": {
                            "type": "boolean",
                            "description": "true 替换全部；默认 false 要求唯一。"
                        }
                    },
                    "required": ["path", "old_string", "new_string"]
                }
            }
        }

    def execute(self, **kwargs) -> str:
        import json
        from core.paths import get_data_path

        path = kwargs.get("path")
        old_string = kwargs.get("old_string")
        new_string = kwargs.get("new_string")
        replace_all = _as_bool(kwargs.get("replace_all", False))

        if not path:
            return "Error: Missing required argument 'path'."
        if new_string is None:
            return "Error: Missing required argument 'new_string'."
        if not old_string:
            return ("Error: old_string 不能为空（精确替换需要非空原文）。"
                    "创建/整体覆盖文件请用 write_file；插入内容时，把插入点周围的原文作为 old_string，"
                    "new_string 写原文加新内容。")

        # Sandbox Mode Enforcement
        config_path = get_data_path("config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)

                if config.get("sandbox_mode", True):
                    whitelist = kwargs.get("_session_whitelist", None)
                    self.check_sandbox(path, config=config, session_whitelist=whitelist)
            except SandboxBlocked:
                raise
            except Exception:
                pass
        if not os.path.exists(path):
            return f"Error: File '{path}' does not exist."

        try:
            with open(path, "r", encoding="utf-8") as f:
                content = f.read()

            occurrences = content.count(old_string)
            if occurrences == 0:
                msg = (f"Error: old_string 在 '{path}' 中未找到。"
                       "请先用 read_file 确认文件实际内容（输出含行号前缀，复制时不要带上）。")
                near = _near_miss_lines(content, old_string)
                if near:
                    lines = ["相近行（可对照修正 old_string）:"]
                    lines += [f"  L{n}: {text}" for n, text in near]
                    msg += "\n" + "\n".join(lines)
                return msg

            if occurrences > 1 and not replace_all:
                locs = _match_locations(content, old_string)
                lines = []
                for mi, (first, block) in enumerate(locs, start=1):
                    lines.append(f"  匹配 {mi}（L{first} 起）:")
                    for ln, text, is_match in block:
                        lines.append(f"  {'>' if is_match else ' '} L{ln}: {text}")
                if occurrences > len(locs):
                    lines.append(f"  … 以及另外 {occurrences - len(locs)} 处")
                return (f"Error: old_string 在 '{path}' 中出现 {occurrences} 次"
                        f"（replace_all=false 要求唯一匹配）。匹配位置（> 为匹配行，含前后各 1 行上下文）:\n"
                        + "\n".join(lines)
                        + "\n请扩大 old_string 的上下文使其唯一，或确认全部替换时设 replace_all=true。")

            new_content = content.replace(old_string, new_string)

            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)

            msg = f"Successfully edited {path}."
            if replace_all and occurrences > 1:
                msg += f" Replaced {occurrences} occurrences."
            return msg
        except Exception as e:
            return f"Error editing file {path}: {str(e)}"

class ApplyPatchTool(BaseTool):
    name: str = "apply_patch"
    description: str = "一次调用对多个文件应用多处精确替换。批量修改时用；单处修改用 edit_file。按顺序应用并逐块报告成败。受沙箱限制。"

    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "patches": {
                            "type": "array",
                            "description": "编辑块数组：[{path, edits: [...]}]，按顺序应用。",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "path": {
                                        "type": "string",
                                        "description": "目标文件路径（绝对或相对，须已存在）。"
                                    },
                                    "edits": {
                                        "type": "array",
                                        "description": "该文件的编辑数组，按顺序应用。",
                                        "items": {
                                            "type": "object",
                                            "properties": {
                                                "old_string": {
                                                    "type": "string",
                                                    "description": "被替换的原文，须与文件完全一致。"
                                                },
                                                "new_string": {
                                                    "type": "string",
                                                    "description": "新文本（保持缩进）。"
                                                },
                                                "replace_all": {
                                                    "type": "boolean",
                                                    "description": "true 替换全部；默认 false 要求唯一。"
                                                }
                                            },
                                            "required": ["old_string", "new_string"]
                                        }
                                    }
                                },
                                "required": ["path", "edits"]
                            }
                        }
                    },
                    "required": ["patches"]
                }
            }
        }

    def execute(self, **kwargs) -> str:
        import json
        from core.paths import get_data_path

        patches = kwargs.get("patches")
        if isinstance(patches, str):
            try:
                patches = json.loads(patches)
            except Exception:
                return "Error: patches 不是合法 JSON，应为数组：[{path, edits: [{old_string, new_string, replace_all?}]}]。"
        if not isinstance(patches, list) or not patches:
            return "Error: patches 必须是非空数组：[{path, edits: [{old_string, new_string, replace_all?}]}]。"

        # Sandbox Mode Enforcement —— 全部路径先预检（与 edit_file 同一模式）。
        # 任一路径被拒则抛 SandboxBlocked：一处都未应用，授权后整个调用可安全重试。
        config_path = get_data_path("config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
                if config.get("sandbox_mode", True):
                    whitelist = kwargs.get("_session_whitelist", None)
                    for block in patches:
                        p = block.get("path") if isinstance(block, dict) else None
                        if p:
                            self.check_sandbox(p, config=config, session_whitelist=whitelist)
            except SandboxBlocked:
                raise
            except Exception:
                pass

        # 顺序应用语义：逐块逐条应用，成功即落盘；单条失败不中断后续，
        # 逐块报告 + 末尾汇总，绝不存在静默的部分应用。
        reports = []
        total = 0
        applied = 0
        failed = []

        for bi, block in enumerate(patches, start=1):
            if not isinstance(block, dict):
                reports.append(f"[{bi}] ERROR: 块格式错误（需要 {{path, edits}} 对象），已跳过。")
                failed.append(f"块{bi}")
                continue
            path = block.get("path")
            edits = block.get("edits")
            if not path or not isinstance(edits, list) or not edits:
                reports.append(f"[{bi}] {path or '?'}: ERROR: 块需要非空 path 与 edits 数组，已跳过。")
                failed.append(f"{path or ('块' + str(bi))}")
                continue
            if not os.path.exists(path):
                total += len(edits)
                reports.append(f"[{bi}] {path}: ERROR: 文件不存在，{len(edits)} 处编辑未应用。")
                failed.extend(f"{path}#{ei}" for ei in range(1, len(edits) + 1))
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception as e:
                total += len(edits)
                reports.append(f"[{bi}] {path}: ERROR: 读取失败（{e}），{len(edits)} 处编辑未应用。")
                failed.extend(f"{path}#{ei}" for ei in range(1, len(edits) + 1))
                continue

            for ei, edit in enumerate(edits, start=1):
                total += 1
                label = f"[{bi}.{ei}] {path}"
                if not isinstance(edit, dict):
                    reports.append(f"{label}: ERROR: 编辑项格式错误（需要 old_string/new_string）。")
                    failed.append(f"{path}#{ei}")
                    continue
                old_string = edit.get("old_string")
                new_string = edit.get("new_string")
                replace_all = _as_bool(edit.get("replace_all", False))
                if not old_string or new_string is None:
                    reports.append(f"{label}: ERROR: old_string 为空或 new_string 缺失。")
                    failed.append(f"{path}#{ei}")
                    continue
                occurrences = content.count(old_string)
                if occurrences == 0:
                    reports.append(f"{label}: ERROR: old_string 未找到（前面编辑可能已改动上下文，请 read_file 确认）。")
                    failed.append(f"{path}#{ei}")
                    continue
                if occurrences > 1 and not replace_all:
                    reports.append(f"{label}: ERROR: old_string 出现 {occurrences} 次，需唯一或设 replace_all=true。")
                    failed.append(f"{path}#{ei}")
                    continue
                new_content = content.replace(old_string, new_string)
                try:
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(new_content)
                    content = new_content
                except Exception as e:
                    reports.append(f"{label}: ERROR: 写入失败（{e}）。")
                    failed.append(f"{path}#{ei}")
                    continue
                applied += 1
                suffix = f"（replace_all 共 {occurrences} 处）" if occurrences > 1 else ""
                reports.append(f"{label}: OK{suffix}")

        summary = f"Summary: {applied}/{total} 处编辑已应用，{len(failed)} 处失败。"
        if failed:
            summary += " 失败项: " + ", ".join(failed) + "。"
        if applied and failed:
            summary += "（顺序应用：成功的编辑已落盘，请修正失败项后仅重试这些编辑。）"
        elif not failed:
            summary += " 全部成功。"
        return "\n".join(reports) + "\n" + summary


_LIST_DIR_MAX_ENTRIES = 500

class ListDirTool(BaseTool):
    name: str = "list_dir"
    description: str = "列出目录内容（名称/大小/修改时间，可递归 1-3 层）。看目录结构时用；按名字找文件用 find_files。受沙箱限制。"

    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "目录路径（绝对或相对）。"
                        },
                        "depth": {
                            "type": "integer",
                            "description": "递归深度 1-3，默认 1（仅直接子项）。"
                        },
                        "sort": {
                            "type": "string",
                            "enum": ["name", "mtime"],
                            "description": "排序：name 按名称（默认）；mtime 按修改时间，新在前。"
                        },
                        "show_size": {
                            "type": "boolean",
                            "description": "是否显示文件大小，默认 true。"
                        }
                    },
                    "required": ["path"]
                }
            }
        }

    def execute(self, **kwargs) -> str:
        import json
        import time as _time
        from core.paths import get_data_path

        path = kwargs.get("path")
        if not path:
            return "Error: No directory path provided."

        # Sandbox Mode Enforcement (same pattern as ReadFileTool)
        config_path = get_data_path("config.json")
        if os.path.exists(config_path):
            config = {}
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    config = json.load(f)
            except Exception:
                pass
            if config.get("sandbox_mode", True):
                whitelist = kwargs.get("_session_whitelist", None)
                self.check_sandbox(path, config=config, session_whitelist=whitelist)

        if not os.path.isdir(path):
            return f"Error: '{path}' is not a directory (or does not exist)."

        sort = kwargs.get("sort", "name")
        if sort not in ("name", "mtime"):
            return f"Error: Invalid sort '{sort}'. Use name | mtime."
        try:
            depth = int(kwargs.get("depth", 1) or 1)
        except (TypeError, ValueError):
            return "Error: depth must be an integer."
        depth = min(3, max(1, depth))
        show_size = kwargs.get("show_size", True)
        if isinstance(show_size, str):
            show_size = show_size.strip().lower() not in ("false", "0", "no")

        rows = []  # (level, is_dir, name, size, mtime)
        truncated = False

        def _scan(dir_path, level):
            nonlocal truncated
            if truncated:
                return
            try:
                items = list(os.scandir(dir_path))
            except OSError as e:
                rows.append((level, None, f"[无法读取: {e}]", None, 0.0))
                return
            enriched = []
            for item in items:
                try:
                    is_dir = item.is_dir(follow_symlinks=False)
                except OSError:
                    is_dir = False
                try:
                    st = item.stat(follow_symlinks=False)
                    size, mtime = (None if is_dir else st.st_size), st.st_mtime
                except OSError:
                    size, mtime = None, 0.0
                enriched.append((item, is_dir, size, mtime))
            if sort == "mtime":
                enriched.sort(key=lambda t: (0 if t[1] else 1, -t[3], t[0].name.casefold()))
            else:
                enriched.sort(key=lambda t: (0 if t[1] else 1, t[0].name.casefold()))
            for item, is_dir, size, mtime in enriched:
                if len(rows) >= _LIST_DIR_MAX_ENTRIES:
                    truncated = True
                    return
                rows.append((level, is_dir, item.name, size, mtime))
                if is_dir and level + 1 < depth:
                    _scan(item.path, level + 1)

        _scan(path, 0)

        def _fmt_size(n):
            if n is None:
                return "-"
            n = float(n)
            for unit in ("B", "KB", "MB", "GB", "TB"):
                if n < 1024 or unit == "TB":
                    return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
                n /= 1024
            return f"{n} B"

        out = [f"--- Directory listing of {path} (depth={depth}, sort={sort}) ---"]
        for level, is_dir, name, size, mtime in rows:
            indent = "  " * level
            mtime_str = _time.strftime("%Y-%m-%d %H:%M", _time.localtime(mtime)) if mtime else "-"
            if is_dir is None:
                out.append(f"{indent}{name}")
            elif is_dir:
                out.append(f"{indent}{name}/  <DIR>  {mtime_str}")
            else:
                size_str = _fmt_size(size) if show_size else ""
                out.append(f"{indent}{name}  {size_str}  {mtime_str}".rstrip())
        out.append(f"--- {len(rows)} entries ---")
        if truncated:
            out.append(f"... [Truncated: only first {_LIST_DIR_MAX_ENTRIES} entries shown; narrow path or depth]")
        return "\n".join(out)
