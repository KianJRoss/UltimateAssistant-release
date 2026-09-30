"""Live Web and API Context Ingestion Engine for Herald.

Allows fetching real-time data from REST APIs, web servers, metrics endpoints,
JSON feeds, or static web pages, formatting the response for prompt context injection.
"""
from __future__ import annotations

import json
from typing import Any

import httpx


def fetch(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
    method: str = "GET",
    json_body: dict[str, Any] | None = None,
    timeout: float = 10.0,
    max_chars: int = 12000,
) -> str:
    """Fetch live data from any API endpoint or web server and format for AI context.

    Usage:
        metrics = herald.fetch("http://localhost:8080/metrics")
        herald.chat("Identify bottlenecks", context=metrics)
    """
    req_headers = {"User-Agent": "Herald/1.0 (AI Context Fetcher)"}
    if headers:
        req_headers.update(headers)

    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.request(
                method=method.upper(),
                url=url,
                headers=req_headers,
                params=params,
                json=json_body,
            )
            resp.raise_for_status()

            # Attempt JSON formatting
            content_type = resp.headers.get("content-type", "")
            if "json" in content_type:
                try:
                    data = resp.json()
                    formatted = json.dumps(data, indent=2)
                    if len(formatted) > max_chars:
                        formatted = formatted[:max_chars] + f"\n... [Truncated {len(formatted) - max_chars} characters]"
                    return f"[API Response from {url}]\n```json\n{formatted}\n```"
                except Exception:
                    pass

            text = resp.text
            if len(text) > max_chars:
                text = text[:max_chars] + f"\n... [Truncated {len(text) - max_chars} characters]"
            return f"[Context fetched from {url}]\n{text}"
    except Exception as exc:
        return f"[Error fetching context from {url}: {exc}]"


def fetch_json(url: str, **kwargs: Any) -> Any:
    """Fetch and return parsed JSON from an API endpoint directly."""
    try:
        with httpx.Client(timeout=kwargs.get("timeout", 10.0), follow_redirects=True) as client:
            resp = client.get(url, headers=kwargs.get("headers"))
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:
        return {"error": str(exc), "url": url}
