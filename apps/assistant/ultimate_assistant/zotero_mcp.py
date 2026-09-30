from __future__ import annotations

import os
import re

import httpx
from mcp.server.fastmcp import FastMCP
from herald.router.secret_vault import SecretVault


mcp = FastMCP("Ultimate Assistant Zotero")
USER_ID = os.environ["ULTIMATE_ASSISTANT_ZOTERO_USER_ID"]
SECRET_NAME = os.environ["ULTIMATE_ASSISTANT_ZOTERO_SECRET"]
API_ROOT = f"https://api.zotero.org/users/{USER_ID}"


def _get(path: str, **params):
    key = SecretVault().get(SECRET_NAME).decode()
    response = httpx.get(
        f"{API_ROOT}/{path.lstrip('/')}", params=params,
        headers={"Zotero-API-Key": key, "Zotero-API-Version": "3"}, timeout=30,
    )
    response.raise_for_status()
    return response.json()


@mcp.tool()
def search_library(query: str, limit: int = 10) -> list[dict]:
    """Search the connected Zotero library by title, creator, or keyword."""
    rows = _get("items", q=query.strip(), limit=max(1, min(int(limit), 50)))
    return [
        {
            "key": row.get("key"),
            "title": row.get("data", {}).get("title", ""),
            "type": row.get("data", {}).get("itemType", ""),
            "creators": row.get("data", {}).get("creators", []),
            "date": row.get("data", {}).get("date", ""),
        }
        for row in rows if isinstance(row, dict)
    ]


@mcp.tool()
def list_collections(limit: int = 100) -> list[dict]:
    """List collections in the connected Zotero library."""
    rows = _get("collections", limit=max(1, min(int(limit), 100)))
    return [
        {"key": row.get("key"), "name": row.get("data", {}).get("name", ""),
         "parent": row.get("data", {}).get("parentCollection")}
        for row in rows if isinstance(row, dict)
    ]


@mcp.tool()
def get_item(item_key: str) -> dict:
    """Read bibliographic metadata for one Zotero item by key."""
    key = item_key.strip()
    if not re.fullmatch(r"[A-Za-z0-9]{1,32}", key):
        raise ValueError("Provide a valid Zotero item key.")
    return _get(f"items/{key}")


if __name__ == "__main__":
    mcp.run()
