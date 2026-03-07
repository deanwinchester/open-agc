import imaplib
import smtplib
import email
from email.header import decode_header
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from bs4 import BeautifulSoup

def clean_html(html_text):
    if not html_text:
        return ""
    try:
        soup = BeautifulSoup(html_text, "html.parser")
        return soup.get_text(separator="\n", strip=True)
    except:
        return html_text

def decode_str(s):
    if not s:
        return ""
    try:
        decoded, charset = decode_header(s)[0]
        if charset:
            return decoded.decode(charset)
        elif isinstance(decoded, bytes):
            return decoded.decode("utf-8", "ignore")
        return decoded
    except:
        return str(s)

def fetch_emails(imap_server, username, password, criteria='ALL', limit=10, mark_seen=False):
    """Fetch emails from the server matching criteria."""
    try:
        mail = imaplib.IMAP4_SSL(imap_server)
        mail.login(username, password)
        mail.select("inbox")
        
        status, messages = mail.search(None, criteria)
        if status != "OK":
            mail.logout()
            return []
            
        mail_ids = messages[0].split()
        if not mail_ids:
            mail.logout()
            return []
            
        # Get newest first
        mail_ids = mail_ids[::-1][:limit]
        
        results = []
        for i in mail_ids:
            fetch_mode = '(RFC822)' if mark_seen else '(BODY.PEEK[])'
            res, msg_data = mail.fetch(i, fetch_mode)
            for response_part in msg_data:
                if isinstance(response_part, tuple):
                    msg = email.message_from_bytes(response_part[1])
                    subject = decode_str(msg.get("Subject", ""))
                    from_ = decode_str(msg.get("From", ""))
                    date_ = msg.get("Date", "")
                    
                    body = ""
                    if msg.is_multipart():
                        for part in msg.walk():
                            content_type = part.get_content_type()
                            if content_type == "text/plain":
                                try:
                                    body += part.get_payload(decode=True).decode("utf-8", "ignore")
                                except:
                                    pass
                            elif content_type == "text/html":
                                try:
                                    html = part.get_payload(decode=True).decode("utf-8", "ignore")
                                    body += clean_html(html)
                                except:
                                    pass
                    else:
                        content_type = msg.get_content_type()
                        try:
                            content = msg.get_payload(decode=True).decode("utf-8", "ignore")
                            if content_type == "text/html":
                                body = clean_html(content)
                            else:
                                body = content
                        except:
                            pass
                            
                    results.append({
                        "id": i.decode(),
                        "subject": subject,
                        "from": from_,
                        "date": date_,
                        "body": body[:5000] # truncate very long emails
                    })
                    
        mail.logout()
        return results
    except Exception as e:
        print(f"Error fetching emails: {e}")
        return []

def send_email(smtp_server, username, password, to_addr, subject, body):
    """Send an email."""
    try:
        msg = MIMEMultipart()
        msg['From'] = username
        msg['To'] = to_addr
        msg['Subject'] = subject
        
        msg.attach(MIMEText(body, 'plain'))
        
        # typically port 465 for SSL, or 587 for TLS
        # we try 465 SSL first, if fails try 587 TLS
        try:
            server = smtplib.SMTP_SSL(smtp_server, 465, timeout=10)
            server.login(username, password)
            server.send_message(msg)
            server.quit()
            return True
        except:
            server = smtplib.SMTP(smtp_server, 587, timeout=10)
            server.starttls()
            server.login(username, password)
            server.send_message(msg)
            server.quit()
            return True
            
    except Exception as e:
        print(f"Error sending email: {e}")
        return False
