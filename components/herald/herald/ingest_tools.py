"""Herald built-in file/picture ingestion tool -- an MCP server exposing
`ingest_file` to any agentic session. Accepts as many file formats as
reasonably possible and normalizes each into consistent, LLM-usable output:
extracted text, a short summary, and metadata.

Separate process from herald-coding-tools since the underlying extraction
libraries (RapidOCR, PyMuPDF, etc.) are heavier and only needed here.

Read-only: no file modification, no approval-gate integration needed.

Transport: stdio (launched as a subprocess by Herald's bootstrap).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from herald.router.ingestion import ingest_bytes, ingest_path

mcp = FastMCP("herald-ingest")


def _resolve_workspace() -> Path:
    env = os.environ.get("HERALD_WORKSPACE", "").strip()
    if env:
        candidate = Path(env).expanduser().resolve()
        if candidate.exists():
            return candidate
    cwd = Path.cwd().resolve()
    if str(cwd) not in {"/", "C:\\"}:
        return cwd
    return Path.home().resolve()


WORKSPACE = _resolve_workspace()


def _safe_path(raw: str) -> Path:
    expanded = os.path.expanduser(raw) if raw.startswith("~") else raw
    p = Path(expanded)
    if not p.is_absolute():
        p = WORKSPACE / p
    p = p.resolve()
    if WORKSPACE not in (p, *p.parents):
        raise ValueError(
            f"Path '{raw}' resolves outside the workspace root ({WORKSPACE})."
        )
    return p


@mcp.tool()
def ingest_file(path: str) -> str:
    """Extract usable text/structure/description from almost any file type.

    Supports images (OCR + visual description + barcode/QR), PDFs (native
    text, with automatic OCR fallback for scanned pages), Office documents
    (docx/xlsx/pptx), plain text/code/CSV/JSON, and archives (zip/tar,
    recursed). Never fails on an unsupported format -- returns a clear
    "unsupported" result instead.

    Args:
        path: Path to the file, relative to the workspace root.

    Returns:
        JSON with: format, extracted_text (list of page/section/row-group
        strings), summary, metadata, and children (for archives).
    """
    try:
        p = _safe_path(path)
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    if not p.exists():
        return json.dumps({"error": f"'{path}' does not exist"})
    if p.is_dir():
        return json.dumps({"error": f"'{path}' is a directory, not a file"})

    result = ingest_path(p)
    return json.dumps(result.to_dict(), ensure_ascii=False, indent=2)


if __name__ == "__main__":
    mcp.run()
