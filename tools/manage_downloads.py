# -*- coding: utf-8 -*-
"""下载管理工具（agent 侧）：查看/续传/删除下载记录。

背景：agent 此前只能用 queue_download 新建下载，看不到下载列表与状态——
中断后想续传只能靠用户去下载页点按钮（生产实证：下载中断残留记录，
agent 不知道现状又重复下载）。本工具补齐管理面。
"""
import asyncio
from typing import Any, Dict

from tools.base import BaseTool


class ManageDownloadsTool(BaseTool):
    name: str = "manage_downloads"
    description: str = (
        "查看和管理下载管理器的记录：list 列出全部下载（状态/进度/来源/大小），"
        "resume 续传某个已暂停/失败的下载，delete 删除记录（连带删除 partial 残留文件）。"
        "用户提到下载中断/下载失败/续传/清理下载记录时使用。"
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
                        "action": {
                            "type": "string",
                            "enum": ["list", "resume", "delete"],
                            "description": "list 列出记录；resume 续传；delete 删除记录",
                        },
                        "download_id": {
                            "type": "integer",
                            "description": "resume/delete 时的记录 ID（从 list 获取）",
                        },
                    },
                    "required": ["action"],
                },
            },
        }

    def execute(self, action: str = "", download_id: int = None, **kwargs) -> str:
        try:
            from api.routes.routes_settings import (
                list_download_records, resume_download, delete_download,
            )
        except ImportError as e:
            return f"Error: 下载系统不可用: {e}"

        if action == "list":
            rows = list_download_records()
            if not rows:
                return "下载管理器中没有记录。"
            lines = []
            for r in rows:
                size_mb = (r.get("total_size") or 0) / 1048576
                done_mb = (r.get("downloaded_bytes") or 0) / 1048576
                pct = round((r.get("progress") or 0) * 100)
                err = f"，错误: {r['error_message']}" if r.get("error_message") else ""
                lines.append(
                    f"- #{r['id']} [{r['status']}] {r.get('label') or r.get('filename')} "
                    f"— {done_mb:.0f}/{size_mb:.0f}MB ({pct}%)，来源 {r.get('source') or '?'}，"
                    f"创建于 {r.get('created_at')}{err}")
            return "下载记录（按创建倒序）：\n" + "\n".join(lines)

        if action == "resume":
            if not download_id:
                return "Error: resume 需要 download_id"
            try:
                asyncio.run(resume_download(int(download_id)))
                return f"已恢复下载 #{download_id}（断点续传，进度见下载管理面板）。"
            except Exception as e:
                # FastAPI HTTPException 的 detail 是真实原因
                return f"Error: 续传失败: {getattr(e, 'detail', str(e))}"

        if action == "delete":
            if not download_id:
                return "Error: delete 需要 download_id"
            try:
                asyncio.run(delete_download(int(download_id)))
                return f"已删除下载记录 #{download_id}（含 partial 残留文件）。"
            except Exception as e:
                return f"Error: 删除失败: {getattr(e, 'detail', str(e))}"

        return f"Error: 未知 action '{action}'（list/resume/delete）"
