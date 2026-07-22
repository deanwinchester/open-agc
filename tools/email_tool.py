from typing import Any, Dict
from tools.base import BaseTool
from core.email_service import fetch_emails, send_email
from core.paths import get_data_path
import os
import json

def load_email_config():
    config_path = get_data_path("config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                c = json.load(f)
                return {
                    "account": c.get("email_account", ""),
                    "password": c.get("email_password", ""),
                    "imap": c.get("email_imap_server", ""),
                    "smtp": c.get("email_smtp_server", ""),
                    "owner": c.get("owner_email", "")
                }
        except:
            pass
    return None

class SearchEmailTool(BaseTool):
    name: str = "search_emails"
    description: str = "从已配置的 IMAP 邮箱抓取并阅读邮件。查看、总结邮件时用它；发邮件用 send_email。"

    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "criteria": {
                            "type": "string",
                            "description": "IMAP 搜索条件（如 UNSEEN、ALL、FROM a@b.com），默认 ALL。"
                        },
                        "limit": {
                            "type": "integer",
                            "description": "返回邮件数上限。",
                            "default": 10
                        }
                    }
                }
            }
        }

    def execute(self, **kwargs) -> str:
        config = load_email_config()
        if not config or not config["account"] or not config["password"] or not config["imap"]:
            return "Email account is not fully configured in settings."
            
        criteria = kwargs.get("criteria", "ALL")
        if "owner" in criteria.lower() and config["owner"]:
            criteria = criteria.replace("owner_email", config["owner"])
            criteria = criteria.replace("master_email", config["owner"])
            
        limit = kwargs.get("limit", 10)
        
        emails = fetch_emails(
            config["imap"], 
            config["account"], 
            config["password"], 
            criteria=criteria, 
            limit=limit,
            mark_seen=False
        )
        
        if not emails:
            return "No emails found matching criteria."
            
        res = []
        for e in emails:
            res.append(f"Subject: {e['subject']}\nFrom: {e['from']}\nDate: {e['date']}\nBody:\n{e['body']}\n---")
            
        return "\n\n".join(res)

class SendEmailTool(BaseTool):
    name: str = "send_email"
    description: str = "从已配置的 SMTP 账户发送邮件。to_addr 填 owner 可直接发给主人。"

    def get_openai_schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "to_addr": {
                            "type": "string",
                            "description": "收件人邮箱地址；填 owner 发给主人。"
                        },
                        "subject": {
                            "type": "string",
                            "description": "邮件主题。"
                        },
                        "body": {
                            "type": "string",
                            "description": "邮件正文内容。"
                        }
                    },
                    "required": ["to_addr", "subject", "body"]
                }
            }
        }

    def execute(self, **kwargs) -> str:
        config = load_email_config()
        if not config or not config["account"] or not config["password"] or not config["smtp"]:
            return "Email account is not fully configured in settings."
            
        to = kwargs.get("to_addr", "")
        sub = kwargs.get("subject", "")
        body = kwargs.get("body", "")
        
        if to.lower() in ("owner", "master", "owner_email", "master_email"):
            to = config["owner"]
            if not to:
                 return "Owner email not configured."
                 
        success = send_email(config["smtp"], config["account"], config["password"], to, sub, body)
        if success:
            return f"Successfully sent email to {to}"
        else:
            return "Failed to send email. Check SMTP settings and credentials."
