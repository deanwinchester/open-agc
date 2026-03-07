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
    description: str = "Fetch and read emails from the connected IMAP account. Useful for summarizing emails. IMAP criteria examples: 'UNSEEN', 'ALL', 'FROM person@example.com'."

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
                            "description": "IMAP search criteria (e.g., 'UNSEEN', 'ALL', 'FROM owner_email'). Defaults to 'ALL'."
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of emails to return.",
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
    description: str = "Send an email from the connected SMTP account to a recipient. You can use 'owner' as to_addr to send to the owner."

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
                            "description": "Recipient email address."
                        },
                        "subject": {
                            "type": "string",
                            "description": "Email subject."
                        },
                        "body": {
                            "type": "string",
                            "description": "Email body content."
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
