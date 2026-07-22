# -*- coding: utf-8 -*-
"""阶段5 Task1: 工具 schema 盘点脚本。

实例化 OpenAGCAgent，导出每个静态工具的 get_openai_schema() 字节数。
用法:
    python scratch/inventory_tool_schemas.py dump <out.json>   # 导出 schema 快照
    python scratch/inventory_tool_schemas.py sizes <in.json>   # 打印每工具字节数表
"""
import io
import json
import sys
import os

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# 静态工具名(agent.py full_available_tools 初始 dict + search_available_tools)
STATIC_TOOL_NAMES = {
    "execute_shell", "read_file", "write_file", "edit_file", "search_file_content",
    "find_files", "execute_python", "computer_control", "manage_memory", "search_web",
    "mac_system_action", "save_learned_skill", "browser_automation", "search_emails",
    "send_email", "queue_download", "ask_user_question", "user_interjection_response",
    "search_history", "pause_and_wait", "enter_sandbox_mode", "exit_sandbox_mode",
    "self_review", "configure_system", "develop_plugin", "shell_send",
    "manage_task_plan", "manage_task", "parse_html", "compact_context",
    "search_available_tools",
}


def collect_schemas():
    """实例化 Agent, 返回 {tool_name: schema_dict} (仅静态工具, 排除动态/MCP)。"""
    from agent.agent import OpenAGCAgent
    a = OpenAGCAgent(model="deepseek")
    schemas = {}
    for name in sorted(STATIC_TOOL_NAMES):
        tool = a.full_available_tools.get(name)
        if tool is None:
            continue
        schemas[name] = tool.get_openai_schema()
    return schemas, a


def schema_size(schema: dict) -> int:
    return len(json.dumps(schema, ensure_ascii=False).encode("utf-8"))


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "dump"
    if cmd == "dump":
        out = sys.argv[2]
        schemas, a = collect_schemas()
        active_names = [s["function"]["name"] for s in a.tool_schemas]
        payload = {
            "schemas": schemas,
            "active_tool_names": active_names,
            "active_total_bytes": len(json.dumps(a.tool_schemas, ensure_ascii=False).encode("utf-8")),
        }
        with open(out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=1)
        total = sum(schema_size(s) for s in schemas.values())
        print(f"dumped {len(schemas)} static tools -> {out}")
        print(f"static total: {total} bytes; active(tool_schemas) total: {payload['active_total_bytes']} bytes")
    elif cmd == "sizes":
        with open(sys.argv[2], encoding="utf-8") as f:
            payload = json.load(f)
        schemas = payload["schemas"]
        total = 0
        for name in sorted(schemas, key=lambda n: -schema_size(schemas[n])):
            sz = schema_size(schemas[name])
            total += sz
            mark = " *active*" if name in payload.get("active_tool_names", []) else ""
            print(f"{sz:6d}  {name}{mark}")
        print(f"{total:6d}  TOTAL ({len(schemas)} tools)")
        print(f"active tool_schemas total: {payload.get('active_total_bytes')} bytes")


if __name__ == "__main__":
    main()
