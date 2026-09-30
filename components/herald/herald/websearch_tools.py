"""Herald built-in web search tool -- gives any agentic session (native CLI
or Herald's own harness) a real way to search outward, not just introspect
the local workspace. No API key required: uses DuckDuckGo's HTML endpoint
(https://html.duckduckgo.com/html/), the standard zero-config approach for
this since it needs no signup or credentials.

Intended for the "search broad, then condense" research pattern: call this
(directly, or via several parallel consult_models delegations each with a
different angle/query) to gather a pool of candidates, then synthesize.

Transport: stdio (launched as a subprocess by Herald's bootstrap).
"""
from __future__ import annotations

import re
from html import unescape
from typing import Any
from urllib.parse import quote_plus, unquote, urlparse, parse_qs

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("herald-websearch")

_RESULT_RE = re.compile(
    r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_SNIPPET_RE = re.compile(
    r'<a[^>]+class="result__snippet"[^>]*>(?P<snippet>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _clean(html_fragment: str) -> str:
    return unescape(_TAG_RE.sub("", html_fragment)).strip()


def _resolve_href(href: str) -> str:
    # DuckDuckGo's HTML results wrap the real URL in a redirect
    # (/l/?uddg=<encoded target>) -- unwrap it so callers get the actual
    # destination, not a DDG-internal link.
    if href.startswith("//duckduckgo.com/l/") or href.startswith("/l/"):
        parsed = urlparse(href if href.startswith("http") else f"https:{href}")
        target = parse_qs(parsed.query).get("uddg")
        if target:
            return unquote(target[0])
    return href


@mcp.tool()
def web_search(query: str, num_results: int = 8) -> str:
    """Search the web and return a list of {title, url, snippet} results as
    text, most relevant first. No API key needed. Use this to gather real,
    current outside information -- library/SDK conventions, security
    guidance, what other projects do -- rather than guessing from training
    data alone. For broad research, call this (or delegate several angles
    via consult_models) with a few different phrasings of the query and
    condense the combined results, rather than trusting one narrow search.
    """
    query = query.strip()
    if not query:
        return "[error] web_search requires a non-empty query"
    num_results = max(1, min(int(num_results), 20))

    try:
        response = httpx.post(
            "https://html.duckduckgo.com/html/",
            data={"q": query},
            headers={"User-Agent": "Mozilla/5.0 (compatible; HeraldWebSearch/1.0)"},
            timeout=15,
            follow_redirects=True,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        return f"[error] web search request failed: {exc}"

    body = response.text
    titles = _RESULT_RE.findall(body)
    snippets = _SNIPPET_RE.findall(body)

    if not titles:
        return f"No results found for: {query}"

    results: list[dict[str, str]] = []
    for i, (href, title_html) in enumerate(titles[:num_results]):
        snippet_html = snippets[i] if i < len(snippets) else ""
        results.append({
            "title": _clean(title_html),
            "url": _resolve_href(href),
            "snippet": _clean(snippet_html),
        })

    lines = [f"Results for: {query}\n"]
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
