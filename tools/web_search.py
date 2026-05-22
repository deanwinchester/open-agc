"""Web search tool with multiple backends and content fetching support."""
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

# Timeout per engine
REQUEST_TIMEOUT = 12


def _get_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
    }


def _text(el):
    """Safely extract text from a BeautifulSoup element."""
    return el.get_text(strip=True) if el else ""


def _decode(resp):
    """Decode response bytes to text with fallback."""
    try:
        return resp.content.decode("utf-8")
    except UnicodeDecodeError:
        try:
            return resp.content.decode(resp.apparent_encoding or "utf-8", errors="replace")
        except Exception:
            return resp.content.decode("utf-8", errors="replace")


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

    # Sogou result selectors (multiple layouts)
    for item in soup.select(".vr-title, .result-title, .rb"):
        a = item if item.name == "a" else item.select_one("a")
        if not a:
            continue
        href = a.get("href", "")
        if href.startswith("/"):
            href = "https://www.sogou.com" + href
        if href in seen_urls:
            continue
        seen_urls.add(href)

        title = a.get_text(strip=True)
        # Find snippet: look for nearby text block
        snippet_el = item.find_next_sibling(class_=lambda c: c and ("str-text" in c or "star-wiki" in c or "str-info" in c))
        snippet = snippet_el.get_text(strip=True) if snippet_el else ""

        results.append({"title": title, "url": href, "snippet": snippet})
        if len(results) >= max_results:
            break

    # Fallback: extract from general h3 > a pattern
    if not results:
        for a in soup.select("h3 a[href]"):
            href = a.get("href", "")
            if href.startswith("/"):
                href = "https://www.sogou.com" + href
            if href in seen_urls:
                continue
            seen_urls.add(href)
            title = a.get_text(strip=True)
            results.append({"title": title, "url": href, "snippet": ""})
            if len(results) >= max_results:
                break

    return results


def _search_baidu(query: str, max_results: int = 5) -> list:
    """Search via Baidu. Uses multiple extraction strategies for the modern SPA layout."""
    url = "https://www.baidu.com/s"
    params = {"wd": query, "rn": max_results}
    resp = requests.get(url, params=params, headers=_get_headers(), timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.content, "html.parser")
    results = []
    seen_urls = set()

    # Strategy 1: h3 > a anywhere in the document
    for a in soup.select("h3 a[href]"):
        href = a.get("href", "")
        if href.startswith("/"):
            href = "https://www.baidu.com" + href
        if href in seen_urls:
            continue
        seen_urls.add(href)
        title = a.get_text(strip=True)
        if title:
            results.append({"title": title, "url": href, "snippet": ""})
            if len(results) >= max_results:
                break

    # Strategy 2: look for links containing titles (wider net)
    if not results:
        for a in soup.select("a[href*='baidu.com/link']"):
            title = a.get("title", "") or a.get_text(strip=True)
            href = a.get("href", "")
            if title and href not in seen_urls and len(title) > 4:
                seen_urls.add(href)
                results.append({"title": title, "url": href, "snippet": ""})
                if len(results) >= max_results:
                    break

    # Strategy 3: extract from JSON-LD / script data
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
    """Fallback: DuckDuckGo HTML (no API key needed, may be blocked in some regions)."""
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


def fetch_page_content(url: str, max_chars: int = 5000) -> str:
    """Fetch a URL and extract its readable text content.

    Useful for getting full article/page content beyond search snippets.
    """
    try:
        resp = requests.get(url, headers=_get_headers(), timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, "html.parser")

        # Remove non-content elements
        for tag in soup.select("script, style, nav, footer, header, aside, iframe, .sidebar, .ad, .menu, .comment"):
            tag.decompose()

        # Try common content selectors
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
        # Clean up excessive whitespace
        text = re.sub(r'\n{3,}', '\n\n', text)
        text = re.sub(r'[ \t]{2,}', ' ', text)

        if len(text) <= max_chars:
            return text
        return text[:max_chars] + f"\n\n...(output truncated, full length: {len(text)} chars)"

    except Exception as e:
        return f"Error fetching page: {type(e).__name__}: {str(e)}"


# Ordered list of search engines to try
SEARCH_ENGINES = [
    ("Bing", _search_bing),
    ("Sogou", _search_sogou),
    ("Baidu", _search_baidu),
    ("DuckDuckGo", _search_duckduckgo),
]


class WebSearchTool(BaseTool):
    """
    Search the web for information using multiple search engines.
    Tries Bing → Sogou → Baidu → DuckDuckGo in order.
    Also supports fetching full page content via fetch_content parameter.
    """

    def __init__(self):
        super().__init__(
            name="search_web",
            description=(
                "Search the web for information, news, or current events. "
                "Tries multiple search engines (Bing, Sogou, Baidu) automatically. "
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

    def execute(self, query: str = "", max_results: int = 5, fetch_content: bool = False,
                fetch_url: str = "", **kwargs) -> str:
        # ── Dedicated URL content fetch ──
        if fetch_url:
            content = fetch_page_content(fetch_url)
            return f"[Fetched Content from {fetch_url}]\n\n{content}"

        if not query:
            return "Error: No query provided."

        # ── Search with multiple engines ──
        all_errors = []

        for engine_name, engine_fn in SEARCH_ENGINES:
            try:
                results = engine_fn(query, max_results)
                if results:
                    body = self._format_results(engine_name, results)

                    # Optionally fetch top result content
                    if fetch_content and results:
                        top_url = results[0]["url"]
                        content = fetch_page_content(top_url)
                        body += f"\n\n[Full content from: {top_url}]\n{content}"

                    return body
                else:
                    all_errors.append(f"{engine_name}: No results")
            except Exception as e:
                all_errors.append(f"{engine_name}: {type(e).__name__}: {str(e)[:80]}")
                continue

        return f"All search engines failed:\n" + "\n".join(all_errors)
