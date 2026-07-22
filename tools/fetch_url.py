"""Lightweight URL page-content fetcher with SSRF protection.

Fetches a known URL and extracts readable text (or raw HTML). Hardened
against SSRF: private/loopback/link-local/reserved addresses are rejected
both as URL literals and as DNS resolution results, and every redirect hop
is re-validated before following. Response bodies are capped at 2MB.

`tools.web_search.fetch_page_content` is a thin wrapper over
`fetch_url_text` here, so search-result page fetches get the same guards.
"""
import ipaddress
import json as _json
import random
import re
import socket
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup

from .base import BaseTool

# Rotating User-Agents to avoid blocks (kept in sync with tools.web_search)
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]

FETCH_TIMEOUT = 15
MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 2 * 1024 * 1024  # 2MB hard cap on response body
DEFAULT_MAX_CHARS = 8000

# Hostnames that always mean "this machine" / local services.
_BLOCKED_HOSTNAMES = {
    "localhost", "localhost.localdomain", "ip6-localhost", "ip6-loopback",
    "broadcasthost",
}


class FetchURLError(Exception):
    """Raised when a URL is rejected by the SSRF guard or cannot be fetched."""


def _get_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    }


def _is_public_ip(ip_str: str) -> bool:
    """True only for globally routable unicast addresses."""
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return not (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def validate_public_url(url: str) -> str:
    """Validate that *url* is an http(s) URL pointing at a public host.

    Checks the hostname literally (IP literals, localhost names) and, for
    domain names, every address it resolves to. Raises FetchURLError on
    rejection; returns the normalized URL on success.
    """
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise FetchURLError(
            f"仅支持 http/https URL（当前 scheme: {parsed.scheme or '缺失'}）")
    host = (parsed.hostname or "").strip().rstrip(".").lower()
    if not host:
        raise FetchURLError("URL 缺少主机名")
    if host in _BLOCKED_HOSTNAMES or host.endswith(".localhost"):
        raise FetchURLError(f"SSRF 防护：主机 {host} 指向本机，已拒绝")

    # Literal IP address (IPv4 or bracket-less IPv6 from urlparse.hostname)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        is_ip_literal = False
    else:
        is_ip_literal = True
    if is_ip_literal:
        if not _is_public_ip(host):
            raise FetchURLError(
                f"SSRF 防护：地址 {host} 属于内网/保留地址段，已拒绝")
        return url

    # Domain name: verify every resolved address is public.
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise FetchURLError(f"URL 端口非法: {parsed.netloc}")
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise FetchURLError(f"DNS 解析失败（{host}）: {e}")
    resolved = {info[4][0] for info in infos}
    if not resolved:
        raise FetchURLError(f"DNS 解析无结果（{host}）")
    for ip_str in resolved:
        if not _is_public_ip(ip_str):
            raise FetchURLError(
                f"SSRF 防护：主机 {host} 解析到内网/保留地址 {ip_str}，已拒绝")
    return url


def safe_fetch(url: str, max_bytes: int = MAX_RESPONSE_BYTES):
    """Fetch *url* with per-hop SSRF validation and a response size cap.

    Follows redirects manually (each hop re-validated, max MAX_REDIRECTS).
    Returns (body_bytes, final_url, status_code, content_type, truncated).
    Raises FetchURLError / requests.RequestException on failure.
    """
    current = url
    session = requests.Session()
    try:
        for _ in range(MAX_REDIRECTS + 1):
            validate_public_url(current)
            resp = session.get(current, headers=_get_headers(),
                               timeout=FETCH_TIMEOUT, allow_redirects=False,
                               stream=True)
            try:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("Location")
                    if not location:
                        raise FetchURLError(
                            f"HTTP {resp.status_code} 重定向缺少 Location 头")
                    current = urljoin(current, location)
                    continue
                chunks = []
                size = 0
                truncated = False
                for chunk in resp.iter_content(chunk_size=65536):
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > max_bytes:
                        truncated = True
                        break
                data = b"".join(chunks)[:max_bytes]
                content_type = resp.headers.get("Content-Type", "")
                return data, resp.url, resp.status_code, content_type, truncated
            finally:
                resp.close()
        raise FetchURLError(f"重定向次数超过上限（{MAX_REDIRECTS} 次）")
    finally:
        session.close()


# ---------------------------------------------------------------------------
# HTML → readable text extraction (shared with web_search.fetch_page_content)
# ---------------------------------------------------------------------------

def _extract_spa_embedded_text(soup) -> str:
    """Extract text from SPA pages via embedded JSON data (Next.js, ModelScope, etc.).

    Falls back when the normal DOM extraction yields very little — SPA shells
    render content via JS, but often embed the data in <script> tags.
    """
    parts = []
    seen = set()

    # Pattern 1: <script id="__NEXT_DATA__" type="application/json"> (Next.js)
    for script in soup.select("script#__NEXT_DATA__[type='application/json']"):
        try:
            data = _json.loads(script.string)
            text = _json_text_walk(data)
            if text and text not in seen:
                parts.append(text)
                seen.add(text)
        except Exception:
            pass

    # Pattern 2: <script type="application/ld+json"> (structured data)
    for script in soup.select("script[type='application/ld+json']"):
        try:
            data = _json.loads(script.string)
            text = _json_text_walk(data)
            if text and text not in seen:
                parts.append(text)
                seen.add(text)
        except Exception:
            pass

    # Patterns 3-5: JS-assigned blobs — window.__detail_data__ (ModelScope /
    # Alibaba Cloud), window.__INITIAL_STATE__, window.__NUXT__ (Nuxt.js)
    for script in soup.find_all("script"):
        if not script.string:
            continue
        raw = script.string.strip()
        for pattern, needs_unescape in (
                (r'__detail_data__\s*=\s*"(.+?)"\s*;', True),
                (r'__INITIAL_STATE__\s*=\s*({.+?});', False),
                (r'__NUXT__\s*=\s*({.+?});', False)):
            m = re.search(pattern, raw, re.DOTALL)
            if not m:
                continue
            try:
                inner = m.group(1)
                if needs_unescape:
                    # JS string unescape: first handle \\\\u sequences
                    inner = _js_str_unescape(inner)
                data = _json.loads(inner)
                text = _json_text_walk(data)
                if text and text not in seen:
                    parts.append(text)
                    seen.add(text)
            except Exception:
                pass

    return "\n\n".join(parts)


def _js_str_unescape(s: str) -> str:
    """Unescape a JS string that contains \\escaped quotes and backslashes.

    Handles:
      - \\" -> " (escaped quote)
      - \\\\ -> \\ (escaped backslash)
      - \\uXXXX -> unicode char (limited support)
    """
    result = []
    i = 0
    while i < len(s):
        if s[i:i+2] == '\\\\':  # Escaped backslash
            result.append('\\')
            i += 2
        elif s[i:i+2] == '\\"':  # Escaped quote
            result.append('"')
            i += 2
        elif s[i:i+2] == '\\n':
            result.append('\n')
            i += 2
        elif s[i:i+2] == '\\r':
            result.append('\r')
            i += 2
        elif s[i:i+2] == '\\t':
            result.append('\t')
            i += 2
        elif s[i] == '\\' and i + 1 < len(s) and s[i+1] == 'u':
            # \\uXXXX
            hex_str = s[i+2:i+6]
            if len(hex_str) == 4:
                try:
                    result.append(chr(int(hex_str, 16)))
                    i += 6
                    continue
                except ValueError:
                    pass
            result.append(s[i])
            i += 1
        else:
            result.append(s[i])
            i += 1
    return "".join(result)


def _json_text_walk(obj, depth=0) -> str:
    """Recursively extract all string values from a parsed JSON object.

    Skips short fragments (<40 chars), URLs, timestamps, and null/empty values.
    Favors long text fields like Card, Readme, Description, body, content.
    """
    if depth > 8:
        return ""
    parts = []

    if isinstance(obj, dict):
        # Prioritize long text fields
        for priority_key in ["ReadmeEn", "ReadmeZh", "Card", "Description",
                             "readme", "README", "body", "content", "text",
                             "description", "summary", "overview"]:
            val = obj.get(priority_key)
            if val and isinstance(val, str) and len(val) > 100:
                parts.append(val)
        # Then walk all values
        for v in obj.values():
            child = _json_text_walk(v, depth + 1)
            if child:
                parts.append(child)

    elif isinstance(obj, list):
        for item in obj:
            child = _json_text_walk(item, depth + 1)
            if child:
                parts.append(child)

    elif isinstance(obj, str) and len(obj) >= 40:
        # Skip URLs, timestamps, and JSON-stringified data
        if obj.startswith("http") or obj.startswith("{") or obj.startswith("["):
            return ""
        if re.match(r'^\d{4}-\d{2}-\d{2}', obj):
            return ""
        parts.append(obj)

    return "\n".join(parts)


def extract_page_text(content: bytes, max_chars: int) -> str:
    """Extract readable text from raw HTML bytes (title/meta + main content)."""
    soup = BeautifulSoup(content, "html.parser")

    for tag in soup.select("script, style, nav, footer, header, aside, iframe, .sidebar, .ad, .menu, .comment"):
        tag.decompose()

    content_el = None
    for selector in ["article", "[role='main']", "main", ".content", ".post-content", ".article-content",
                     "#content", ".entry-content", ".post"]:
        el = soup.select_one(selector)
        if el and len(el.get_text(strip=True)) > 200:
            content_el = el
            break

    if not content_el:
        content_el = soup.body or soup

    text = content_el.get_text(separator="\n", strip=True)
    text = re.sub(r'\n{3,}', '\n\n', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)

    # If extracted text is too short, the page is likely an SPA shell.
    # Try to extract embedded JSON data from <script> tags — re-parse from the
    # raw bytes, since the scripts were decomposed from `soup` above.
    if len(text) < 500:
        spa_text = _extract_spa_embedded_text(BeautifulSoup(content, "html.parser"))
        if spa_text:
            text = spa_text

    # Also include <title> and <meta description> as summary
    title = soup.title.string.strip() if soup.title and soup.title.string else ""
    meta_desc = ""
    meta = soup.find("meta", attrs={"name": "description"}) or soup.find("meta", attrs={"property": "og:description"})
    if meta and meta.get("content"):
        meta_desc = meta["content"].strip()

    summary_parts = []
    if title:
        summary_parts.append(f"# {title}")
    if meta_desc:
        summary_parts.append(meta_desc)
    if summary_parts:
        text = "\n\n".join(summary_parts) + "\n\n" + text

    if len(text) <= max_chars:
        return text.strip()
    return text[:max_chars].strip() + f"\n\n...(output truncated, full length: {len(text)} chars)"


def _decode_body(data: bytes, content_type: str) -> str:
    m = re.search(r'charset=([\w\-]+)', content_type or "", re.IGNORECASE)
    encoding = m.group(1) if m else "utf-8"
    try:
        return data.decode(encoding, errors="replace")
    except LookupError:
        return data.decode("utf-8", errors="replace")


def fetch_url_text(url: str, max_chars: int = DEFAULT_MAX_CHARS, raw: bool = False) -> str:
    """Fetch *url* (SSRF-guarded, 2MB-capped) and return text or raw HTML.

    Never raises; failures are returned as "Error ..." strings.
    """
    try:
        max_chars = int(max_chars)
        if max_chars <= 0:
            max_chars = DEFAULT_MAX_CHARS
    except (TypeError, ValueError):
        max_chars = DEFAULT_MAX_CHARS

    try:
        data, final_url, status, content_type, bytes_truncated = safe_fetch(url)
    except FetchURLError as e:
        return f"Error fetching page: {e}"
    except requests.exceptions.Timeout:
        return f"Error fetching page: 请求超时（{FETCH_TIMEOUT}s），目标站点无响应。"
    except requests.exceptions.RequestException as e:
        return f"Error fetching page: {type(e).__name__}: {e}"
    except Exception as e:
        return f"Error fetching page: {type(e).__name__}: {e}"

    if status != 200:
        return f"Error fetching page: HTTP {status}（目标页返回非 200 状态码）。"
    ct = (content_type or "").lower()
    if ct and not any(t in ct for t in ("text/html", "application/xhtml", "text/plain")):
        return (f"Error fetching page: 内容类型非 HTML（{content_type}）。"
                f"如需下载文件请改用 queue_download 工具。")

    byte_note = f"\n\n...(response exceeded {MAX_RESPONSE_BYTES // (1024 * 1024)}MB, truncated)" if bytes_truncated else ""
    if raw:
        text = _decode_body(data, content_type)
        if len(text) > max_chars:
            text = text[:max_chars] + f"\n\n...(output truncated, full length: {len(text)} chars)"
        return text + byte_note
    return extract_page_text(data, max_chars) + byte_note


class FetchURLTool(BaseTool):
    """Fetch readable text content from a known URL (SSRF-guarded)."""

    def __init__(self):
        super().__init__(
            name="fetch_url",
            description=(
                "抓取指定 URL 的网页正文，返回纯文本。已知目标 URL 时直接用本工具，不必先搜索；"
                "raw=true 返回原始 HTML。内网/本机地址会被 SSRF 防护拒绝。"
                "不知道 URL 时用 search_web 搜索；页面需 JS 渲染或登录交互时用 browser_automation；"
                "已有大段 HTML 要转 Markdown 用 parse_html。"
            ),
        )

    def get_openai_schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "要抓取的完整 URL（仅支持 http/https）。",
                        },
                        "max_chars": {
                            "type": "integer",
                            "description": "返回内容字符数上限（默认 8000）。",
                        },
                        "raw": {
                            "type": "boolean",
                            "description": "true 时返回原始 HTML 源码；默认 false 返回提取后的正文纯文本。",
                        },
                    },
                    "required": ["url"],
                },
            },
        }

    def execute(self, url: str = "", max_chars: int = DEFAULT_MAX_CHARS,
                raw: bool = False, **kwargs) -> str:
        if not url or not str(url).strip():
            return "Error: 缺少必填参数 url。"
        # Tolerate string-typed booleans from loosely-typed model output
        raw = str(raw).strip().lower() in ("true", "1", "yes")
        return fetch_url_text(str(url).strip(), max_chars=max_chars, raw=raw)
