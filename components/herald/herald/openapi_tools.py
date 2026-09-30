"""Universal OpenAPI / REST API to AI Tool Generator for Herald.

Automatically parses OpenAPI / Swagger 2.0 / 3.0 / 3.1 JSON schemas and generates
AI-callable tools with typed signatures and documentation.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Callable

import httpx
from herald.dev import _CUSTOM_TOOLS

logger = logging.getLogger("herald.openapi_tools")


def _sanitize_tool_name(path: str, method: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", f"{method}_{path}").strip("_")
    return cleaned.lower()


def tools_from_openapi(
    spec_url_or_dict: str | dict[str, Any],
    *,
    base_url: str | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> list[str]:
    """Generate and register AI-executable tools from an OpenAPI/Swagger spec URL or dict.

    Usage:
        herald.tools_from_openapi("http://localhost:8000/openapi.json")
    """
    if isinstance(spec_url_or_dict, str):
        try:
            resp = httpx.get(spec_url_or_dict, timeout=timeout)
            resp.raise_for_status()
            spec = resp.json()
            if not base_url:
                # Infer base_url from spec URL
                from urllib.parse import urlparse
                parsed = urlparse(spec_url_or_dict)
                base_url = f"{parsed.scheme}://{parsed.netloc}"
        except Exception as exc:
            logger.error(f"Failed to fetch OpenAPI spec from {spec_url_or_dict}: {exc}")
            return []
    else:
        spec = spec_url_or_dict

    api_base = base_url or "http://localhost:8000"
    paths = spec.get("paths", {})
    registered_tools = []

    for path, methods in paths.items():
        for method, op in methods.items():
            if method.lower() not in ("get", "post", "put", "delete", "patch"):
                continue

            op_id = op.get("operationId")
            tool_name = op_id if op_id else _sanitize_tool_name(path, method)
            summary = op.get("summary") or op.get("description") or f"Execute {method.upper()} {path}"

            # Extract parameters
            properties: dict[str, Any] = {}
            required: list[str] = []

            for p in op.get("parameters", []):
                p_name = p.get("name")
                p_schema = p.get("schema", {})
                p_type = p_schema.get("type", "string")
                properties[p_name] = {
                    "type": p_type,
                    "description": p.get("description") or f"Parameter {p_name} ({p.get('in', 'query')})",
                }
                if p.get("required"):
                    required.append(p_name)

            # Request Body parameters
            req_body = op.get("requestBody", {})
            content = req_body.get("content", {})
            json_schema = content.get("application/json", {}).get("schema", {})
            if json_schema:
                body_props = json_schema.get("properties", {})
                for b_name, b_def in body_props.items():
                    properties[b_name] = {
                        "type": b_def.get("type", "string"),
                        "description": b_def.get("description") or f"Body parameter {b_name}",
                    }
                required.extend(json_schema.get("required", []))

            schema = {
                "name": tool_name,
                "description": f"{summary} ({method.upper()} {path})",
                "inputSchema": {
                    "type": "object",
                    "properties": properties,
                    "required": list(set(required)),
                },
            }

            # Create closure for execution
            def make_executor(p_method: str, p_path: str, p_base: str, p_headers: dict[str, str] | None):
                def executor(**kwargs: Any) -> Any:
                    target_url = p_base.rstrip("/") + p_path
                    query_params = {}
                    body_json = {}

                    for k, v in kwargs.items():
                        if f"{{{k}}}" in target_url:
                            target_url = target_url.replace(f"{{{k}}}", str(v))
                        elif p_method.upper() in ("POST", "PUT", "PATCH"):
                            body_json[k] = v
                        else:
                            query_params[k] = v

                    req_headers = {"User-Agent": "Herald/1.0"}
                    if p_headers:
                        req_headers.update(p_headers)

                    with httpx.Client(timeout=timeout) as client:
                        r = client.request(
                            method=p_method.upper(),
                            url=target_url,
                            params=query_params or None,
                            json=body_json or None,
                            headers=req_headers,
                        )
                        try:
                            return r.json()
                        except Exception:
                            return r.text
                return executor

            _CUSTOM_TOOLS[tool_name] = {
                "name": tool_name,
                "schema": schema,
                "func": make_executor(method, path, api_base, headers),
            }
            registered_tools.append(tool_name)

    return registered_tools
