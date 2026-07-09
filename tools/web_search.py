"""Web search tool with multiple backends and content fetching support.

Engine selection by query language:
  - Chinese queries → Sogou → Baidu → Bing → DuckDuckGo
    (Bing China / cn.bing.com fails on multi-word CJK queries for
    less-known entities, returning character-dictionary results instead.
    Sogou handles Chinese queries much more reliably.)
  - English queries → Bing → DuckDuckGo → Sogou → Baidu
"""
import json as _json
import os
import random
import re
import traceback
import requests
from bs4 import BeautifulSoup
from .base import BaseTool

# Rotating User-Agents to avoid blocks
USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]

REQUEST_TIMEOUT = 12


def _get_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    }


def _search_bing(query: str, max_results: int = 5) -> list:
    """Search via Bing."""
    url = "https://www.bing.com/search"
    params = {"q": query, "count": max_results}
    resp = requests.get(url, params=params, headers=_get_headers(), timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.content, "html.parser")
    results = []

    for item in soup.select("li.b_algo"):
        title_el = item.select_one("h2 a")
        snippet_el = item.select_one(".b_caption p") or item.select_one("p")
        if title_el:
            title = title_el.get_text(strip=True)
            href = title_el.get("href", "")
            snippet = snippet_el.get_text(strip=True) if snippet_el else ""
            results.append({"title": title, "url": href, "snippet": snippet})
            if len(results) >= max_results:
                break

    return results


def _search_sogou(query: str, max_results: int = 5) -> list:
    """Search via Sogou (reliable in China, good for Chinese queries)."""
    url = "https://www.sogou.com/web"
    params = {"query": query}
    resp = requests.get(url, params=params, headers=_get_headers(), timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.content, "html.parser")
    results = []
    seen_urls = set()

    for vr in soup.select(".vrwrap"):
        # Skip hint/related-search boxes
        if any("hint" in c for c in (vr.get("class") or [])):
            continue
        # Extract title and url from the first suitable link
        a = vr.select_one("h3 a[href], .vr-title a[href], a[href]")
        if not a:
            continue
        href = a.get("href", "")
        if href.startswith("/"):
            href = "https://www.sogou.com" + href
        if href in seen_urls:
            continue
        seen_urls.add(href)
        title = a.get_text(strip=True)
        if not title:
            continue

        # Extract snippet from the result block
        snippet = ""
        for sel in ["div[class*='text']", ".space-txt", "div[class*='content']"]:
            el = vr.select_one(sel)
            if el:
                txt = el.get_text(strip=True)
                if txt and len(txt) > 10:
                    snippet = txt
                    break
        if not snippet:
            # Fallback: full text minus title prefix
            full = vr.get_text(strip=True)
            snippet = full.replace(title, "", 1).strip()

        results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= max_results:
            break

    return results


def _search_baidu(query: str, max_results: int = 5) -> list:
    """Search via Baidu. Uses multiple extraction strategies for the modern SPA layout."""
    url = "https://www.baidu.com/s"
    params = {"wd": query, "rn": max_results}
    resp = requests.get(url, params=params, headers=_get_headers(), timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    # Bail out if Baidu returned a security/challenge page
    if "百度安全验证" in resp.text or len(resp.content) < 5000:
        return []

    soup = BeautifulSoup(resp.content, "html.parser")
    results = []
    seen_urls = set()

    # Strategy 1: result container divs (modern Baidu layout)
    for container in soup.select(".result, .result-op, .c-container"):
        a = container.select_one("h3 a[href]")
        if not a:
            continue
        href = a.get("href", "")
        if href.startswith("/"):
            href = "https://www.baidu.com" + href
        if href in seen_urls:
            continue
        seen_urls.add(href)
        title = a.get_text(strip=True)
        if not title:
            continue

        # Extract snippet
        snippet = ""
        for sel in [".c-abstract", ".c-span-last", ".c-color-gray",
                     ".c-line-clamp2", ".content-right_8Zs40", "p"]:
            el = container.select_one(sel)
            if el:
                txt = el.get_text(strip=True)
                if txt and len(txt) > 10:
                    snippet = txt
                    break
        if not snippet:
            full = container.get_text(strip=True)
            snippet = full.replace(title, "", 1).strip()

        results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= max_results:
            break

    # Strategy 2: h3 > a anywhere in the document (fallback)
    if not results:
        for a in soup.select("h3 a[href]"):
            href = a.get("href", "")
            if href.startswith("/"):
                href = "https://www.baidu.com" + href
            if href in seen_urls:
                continue
            seen_urls.add(href)
            title = a.get_text(strip=True)
            snippet = ""
            # Try to get text from parent div
            parent = a.find_parent("div")
            if parent:
                for sel in [".c-abstract", ".c-span-last", ".c-color-gray",
                             ".c-line-clamp2", "p"]:
                    el = parent.select_one(sel)
                    if el:
                        txt = el.get_text(strip=True)
                        if txt and len(txt) > 10:
                            snippet = txt
                            break
            if title:
                results.append({"title": title, "url": href, "snippet": snippet})
                if len(results) >= max_results:
                    break

    # Strategy 3: JSON-LD script data
    if not results:
        for script in soup.select("script[type='application/ld+json']"):
            try:
                import json
                data = json.loads(script.string or "{}")
                items = data if isinstance(data, list) else [data]
                for item in items:
                    title = item.get("name", "") or item.get("headline", "")
                    href = item.get("url", "")
                    snippet = item.get("description", "")
                    if title and href not in seen_urls:
                        seen_urls.add(href)
                        results.append({"title": title, "url": href, "snippet": snippet})
                        if len(results) >= max_results:
                            break
            except Exception:
                pass

    return results


def _search_duckduckgo(query: str, max_results: int = 5) -> list:
    """Fallback: DuckDuckGo HTML."""
    url = "https://html.duckduckgo.com/html/"
    data = {"q": query}
    resp = requests.post(url, data=data, headers=_get_headers(), timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.content, "html.parser")
    results = []

    for item in soup.select("div.result"):
        title_el = item.select_one("a.result__a")
        snippet_el = item.select_one("a.result__snippet")
        if title_el:
            title = title_el.get_text(strip=True)
            href = title_el.get("href", "")
            snippet = snippet_el.get_text(strip=True) if snippet_el else ""
            results.append({"title": title, "url": href, "snippet": snippet})
            if len(results) >= max_results:
                break

    return results


def _search_tavily(query: str, max_results: int = 5) -> list:
    """Search via Tavily API (requires TAVILY_API_KEY env var)."""
    api_key = os.environ.get("TAVILY_API_KEY", "")
    if not api_key:
        return []
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={"query": query, "search_depth": "basic", "max_results": max_results, "api_key": api_key},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for item in data.get("results", []):
            title = item.get("title", "")
            url = item.get("url", "")
            snippet = item.get("content", "")
            if title:
                results.append({"title": title, "url": url, "snippet": snippet})
        return results[:max_results]
    except Exception:
        return []


def _search_searxng(query: str, max_results: int = 5) -> list:
    """Search via SearXNG instance (requires SEARXNG_URL env var)."""
    base_url = os.environ.get("SEARXNG_URL", "").rstrip("/")
    if not base_url:
        return []
    api_key = os.environ.get("SEARXNG_API_KEY", "")
    try:
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        resp = requests.get(
            f"{base_url}/search",
            params={"q": query, "format": "json", "language": "all", "categories": "general", "pageno": 1},
            headers=headers,
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for item in data.get("results", []):
            title = item.get("title", "")
            url = item.get("url", "")
            snippet = item.get("content", "")
            if title:
                results.append({"title": title, "url": url, "snippet": snippet})
        return results[:max_results]
    except Exception:
        return []


def _search_brave(query: str, max_results: int = 5) -> list:
    """Search via Brave Search API (requires BRAVE_SEARCH_API_KEY env var)."""
    api_key = os.environ.get("BRAVE_SEARCH_API_KEY", "")
    if not api_key:
        return []
    try:
        resp = requests.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": max_results},
            headers={"X-Subscription-Token": api_key, "Accept": "application/json"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        results = []
        for item in data.get("web", {}).get("results", []):
            title = item.get("title", "")
            url = item.get("url", "")
            snippet = item.get("description", "")
            if title:
                results.append({"title": title, "url": url, "snippet": snippet})
        return results[:max_results]
    except Exception:
        return []


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

    # Pattern 3: window.__detail_data__ (ModelScope / Alibaba Cloud)
    for script in soup.find_all("script"):
        if not script.string:
            continue
        raw = script.string.strip()
        # Match: __detail_data__ = "...json...";
        m = re.search(r'__detail_data__\s*=\s*"(.+?)"\s*;', raw, re.DOTALL)
        if m:
            try:
                inner = m.group(1)
                # JS string unescape: first handle \\\\u sequences
                inner = _js_str_unescape(inner)
                data = _json.loads(inner)
                text = _json_text_walk(data)
                if text and text not in seen:
                    parts.append(text)
                    seen.add(text)
            except Exception:
                pass

    # Pattern 4: window.__INITIAL_STATE__ (common SPA pattern)
    for script in soup.find_all("script"):
        if not script.string:
            continue
        raw = script.string.strip()
        m = re.search(r'__INITIAL_STATE__\s*=\s*({.+?});', raw, re.DOTALL)
        if m:
            try:
                data = _json.loads(m.group(1))
                text = _json_text_walk(data)
                if text and text not in seen:
                    parts.append(text)
                    seen.add(text)
            except Exception:
                pass

    # Pattern 5: window.__NUXT__ (Nuxt.js)
    for script in soup.find_all("script"):
        if not script.string:
            continue
        raw = script.string.strip()
        m = re.search(r'__NUXT__\s*=\s*({.+?});', raw, re.DOTALL)
        if m:
            try:
                data = _json.loads(m.group(1))
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


def fetch_page_content(url: str, max_chars: int = 5000) -> str:
    """Fetch a URL and extract its readable text content."""
    try:
        resp = requests.get(url, headers=_get_headers(), timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "html.parser")

        for tag in soup.select("script, style, nav, footer, header, aside, iframe, .sidebar, .ad, .menu, .comment"):
            tag.decompose()

        content = None
        for selector in ["article", "[role='main']", "main", ".content", ".post-content", ".article-content",
                         "#content", ".entry-content", ".post"]:
            el = soup.select_one(selector)
            if el and len(el.get_text(strip=True)) > 200:
                content = el
                break

        if not content:
            content = soup.body or soup

        text = content.get_text(separator="\n", strip=True)
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r'[ \t]{2,}', ' ', text)

        # If extracted text is too short, the page is likely an SPA shell.
        # Try to extract embedded JSON data from <script> tags.
        if len(text) < 500:
            spa_text = _extract_spa_embedded_text(soup)
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

    except Exception as e:
        return f"Error fetching page: {type(e).__name__}: {str(e)}"


# Engine order varies by query language
_ENGLISH_ENGINES = [
    ("Tavily", _search_tavily),
    ("Bing", _search_bing),
    ("DuckDuckGo", _search_duckduckgo),
    ("Sogou", _search_sogou),
    ("Baidu", _search_baidu),
    ("SearXNG", _search_searxng),
    ("Brave", _search_brave),
]
_CHINESE_ENGINES = [
    ("Sogou", _search_sogou),
    ("Baidu", _search_baidu),
    ("Tavily", _search_tavily),
    ("Bing", _search_bing),
    ("DuckDuckGo", _search_duckduckgo),
    ("SearXNG", _search_searxng),
    ("Brave", _search_brave),
]


class WebSearchTool(BaseTool):
    """
    Search the web for information using multiple search engines.
    For Chinese queries: Sogou → Baidu → Tavily → Bing → DuckDuckGo → SearXNG → Brave
    For English queries: Tavily → Bing → DuckDuckGo → Sogou → Baidu → SearXNG → Brave
    Also supports fetching full page content via fetch_content parameter.
    """

    def __init__(self):
        super().__init__(
            name="search_web",
            description=(
                "Search the web for information, news, or current events. "
                "Tries multiple search engines (Tavily, Bing, Sogou, Baidu, DuckDuckGo, SearXNG, Brave) automatically. "
                "Use fetch_content=True to get full page content from a result URL. "
                "Use this tool for any question about recent events or real-time information."
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
                        "query": {
                            "type": "string",
                            "description": "The search query. Use the language most relevant to the topic.",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of results to return (default 5).",
                        },
                        "fetch_content": {
                            "type": "boolean",
                            "description": "If True, also fetch the full text content from the top result URL. Useful when snippets are insufficient. Default: False.",
                        },
                        "fetch_url": {
                            "type": "string",
                            "description": "Fetch full text content from a specific URL instead of searching. Use this when you already have a URL and want to read its content.",
                        },
                    },
                    "required": ["query"],
                },
            },
        }

    @staticmethod
    def _format_results(engine_name: str, results: list) -> str:
        formatted = []
        for i, r in enumerate(results, 1):
            formatted.append(
                f"{i}. {r['title']}\n   URL: {r['url']}\n   {r['snippet']}"
            )
        header = f"[From {engine_name}]\n"
        return header + "\n\n".join(formatted)

    @staticmethod
    def _extract_key_terms(query: str) -> set:
        """Extract meaningful key terms from query for relevance checking."""
        terms = set()
        cjk_blocks = re.findall(r'[一-鿿豈-﫿㐀-䶿]{2,}', query)
        for block in cjk_blocks:
            if len(block) >= 2:
                terms.add(block)
            for i in range(len(block) - 1):
                terms.add(block[i:i+2])
        for w in re.findall(r'[a-zA-Z]{3,}', query):
            terms.add(w.lower())
        return terms

    @staticmethod
    def _results_relevant(query: str, results: list, min_match_ratio: float = 0.3) -> bool:
        """Lightweight relevance check for CJK queries."""
        has_cjk = bool(re.search(r'[一-鿿]', query))
        if not has_cjk or not results:
            return True

        key_terms = WebSearchTool._extract_key_terms(query)
        if not key_terms:
            return True

        match_count = 0
        for r in results:
            combined = (r["title"] + " " + r["url"]).lower()
            if any(t.lower() in combined for t in key_terms):
                match_count += 1

        return match_count / len(results) >= min_match_ratio

    @staticmethod
    def _is_cjk_query(query: str) -> bool:
        """Check if query contains CJK characters."""
        return bool(re.search(r'[一-鿿㐀-䶿]', query))

    def execute(self, query: str = "", max_results: int = 5, fetch_content: bool = False,
                fetch_url: str = "", **kwargs) -> str:
        # ── Dedicated URL content fetch ──
        if fetch_url:
            content = fetch_page_content(fetch_url)
            return f"[Fetched Content from {fetch_url}]\n\n{content}"

        if not query:
            return "Error: No query provided."

        # ── Select engine order by query language ──
        engines = _CHINESE_ENGINES if self._is_cjk_query(query) else _ENGLISH_ENGINES

        # ── Search with multiple engines, with relevance check ──
        all_errors = []
        all_results = []

        for engine_name, engine_fn in engines:
            try:
                results = engine_fn(query, max_results)
                if results:
                    if self._results_relevant(query, results):
                        body = self._format_results(engine_name, results)
                        if fetch_content and results:
                            top_url = results[0]["url"]
                            content = fetch_page_content(top_url)
                            body += f"\n\n[Full content from: {top_url}]\n{content}"
                        return body
                    else:
                        all_results.append((engine_name, results))
                        all_errors.append(f"{engine_name}: Results low relevance for query terms")
                else:
                    all_errors.append(f"{engine_name}: No results")
            except Exception as e:
                all_errors.append(f"{engine_name}: {type(e).__name__}: {str(e)[:80]}")
                continue

        # ── Fallback ──
        if all_results:
            best_name, best_results = all_results[0]
            body = self._format_results(best_name, best_results)
            body += ("\n\n⚠️ 搜索结果质量可能不佳，相关查询词未在结果标题中出现。"
                     "建议使用 browser_automation 直接访问目标网站获取更准确信息。")
            return body

        return f"All search engines failed:\n" + "\n".join(all_errors)
