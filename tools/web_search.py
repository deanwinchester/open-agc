import traceback
import random
import re
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


def _get_headers():
    return {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
    }


def _search_bing(query: str, max_results: int = 5) -> list:
    """Search via Bing (international, usually accessible)."""
    url = "https://www.bing.com/search"
    params = {"q": query, "count": max_results}
    resp = requests.get(url, params=params, headers=_get_headers(), timeout=10)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
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


def _search_baidu(query: str, max_results: int = 5) -> list:
    """Search via Baidu (reliable in China)."""
    url = "https://www.baidu.com/s"
    params = {"wd": query, "rn": max_results}
    resp = requests.get(url, params=params, headers=_get_headers(), timeout=10)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    for item in soup.select("div.result, div.c-container"):
        title_el = item.select_one("h3 a")
        snippet_el = (
            item.select_one(".c-abstract")
            or item.select_one(".content-right_2s-H4")
            or item.select_one("span.content-right_2s-H4")
        )
        if not snippet_el:
            # Try generic paragraph
            snippet_el = item.select_one("p") or item.select_one("span")

        if title_el:
            title = title_el.get_text(strip=True)
            href = title_el.get("href", "")
            snippet = snippet_el.get_text(strip=True) if snippet_el else ""
            results.append({"title": title, "url": href, "snippet": snippet})
            if len(results) >= max_results:
                break

    return results


def _search_google(query: str, max_results: int = 5) -> list:
    """Search via Google (may be blocked in some regions)."""
    url = "https://www.google.com/search"
    params = {"q": query, "num": max_results, "hl": "zh-CN"}
    resp = requests.get(url, params=params, headers=_get_headers(), timeout=10)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []

    for g in soup.select("div.g"):
        title_el = g.select_one("h3")
        link_el = g.select_one("a")
        snippet_el = g.select_one("div.VwiC3b") or g.select_one("span.st")

        if title_el and link_el:
            title = title_el.get_text(strip=True)
            href = link_el.get("href", "")
            # Google wraps URLs, extract actual URL
            if href.startswith("/url?q="):
                href = href.split("/url?q=")[1].split("&")[0]
            snippet = snippet_el.get_text(strip=True) if snippet_el else ""
            results.append({"title": title, "url": href, "snippet": snippet})
            if len(results) >= max_results:
                break

    return results


def _search_duckduckgo(query: str, max_results: int = 5) -> list:
    """Fallback: DuckDuckGo HTML (no API key needed)."""
    url = "https://html.duckduckgo.com/html/"
    data = {"q": query}
    resp = requests.post(url, data=data, headers=_get_headers(), timeout=10)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
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


# Ordered list of search engines to try
SEARCH_ENGINES = [
    ("Bing", _search_bing),
    ("Baidu", _search_baidu),
    ("Google", _search_google),
    ("DuckDuckGo", _search_duckduckgo),
]


class WebSearchTool(BaseTool):
    """
    Search the web for information using multiple search engines.
    Tries Bing → Baidu → Google → DuckDuckGo in order.
    No paid API required — uses direct HTTP scraping.
    """

    def __init__(self):
        super().__init__(
            name="search_web",
            description=(
                "Search the web for information, news, or current events. "
                "Tries multiple search engines (Bing, Baidu, Google) automatically. "
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
                    },
                    "required": ["query"],
                },
            },
        }

    def execute(self, query: str, max_results: int = 5) -> str:
        errors = []

        for engine_name, engine_fn in SEARCH_ENGINES:
            try:
                results = engine_fn(query, max_results)
                if results:
                    formatted = []
                    for r in results:
                        formatted.append(
                            f"Title: {r['title']}\nURL: {r['url']}\nSnippet: {r['snippet']}"
                        )
                    header = f"[Source: {engine_name}]\n"
                    return header + "\n\n".join(formatted)
                else:
                    errors.append(f"{engine_name}: No results returned")
            except Exception as e:
                errors.append(f"{engine_name}: {str(e)}")
                continue

        # All engines failed
        error_details = "\n".join(errors)
        return f"All search engines failed:\n{error_details}"
