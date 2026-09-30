from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any


_WORD_RE = re.compile(r"[\w'-]+", re.UNICODE)


def _fts_query(query: str) -> str:
    words = list(dict.fromkeys(_WORD_RE.findall(query)))
    return " OR ".join(f'"{word.replace(chr(34), chr(34) * 2)}"' for word in words)


def search_local_knowledge(database: Path, query: str, *, limit: int = 5) -> list[dict[str, Any]]:
    """Search the existing College Assistant FTS index without modifying it."""
    fts_query = _fts_query(query)
    if not fts_query or not database.is_file():
        return []

    uri = database.resolve().as_uri() + "?mode=ro"
    try:
        with sqlite3.connect(uri, uri=True, timeout=3) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                """
                SELECT d.path, d.kind, d.title, d.course_name, d.tags,
                       snippet(search_fts, 4, '[', ']', ' … ', 18) AS excerpt
                  FROM search_fts
                  JOIN search_doc d ON d.path = search_fts.path
                 WHERE search_fts MATCH ?
                 ORDER BY bm25(search_fts)
                 LIMIT ?
                """,
                (fts_query, max(1, min(int(limit), 10))),
            ).fetchall()
    except sqlite3.Error:
        return []

    return [dict(row) for row in rows]

