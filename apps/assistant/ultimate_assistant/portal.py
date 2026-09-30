from __future__ import annotations

from datetime import datetime, timezone
import re
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit


_BLOCKED_ACTION_RE = re.compile(
    r"(?:^|[^a-z])(submit|delete|withdraw|enroll|unenroll|register|send|logout|log out|"
    r"remove|save|post|pay|purchase|drop|edit|update|upload|create|cancel|download|export)"
    r"[a-z]*(?:$|[^a-z])",
    re.IGNORECASE,
)


def _safe_url(url: str) -> str:
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


class PortalSession:
    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._page = None
        self._links: dict[int, str] = {}
        self.ai_consent = False

    @property
    def is_open(self) -> bool:
        return self._page is not None and not self._page.is_closed()

    def open(self, url: str) -> str:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
        ):
            raise ValueError("Use a complete http(s) URL without embedded credentials.")
        self.close()

        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch(channel="msedge", headless=False)
            context = self._browser.new_context()
            self._page = context.new_page()
            self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return self._page.title()
        except Exception:
            self.close()
            raise

    def snapshot(self, *, max_chars: int = 10000, max_links: int = 30) -> dict[str, object]:
        if not self.is_open:
            raise RuntimeError("No portal browser is open. Use /portal open <url> first.")

        page = self._page
        self._links.clear()
        links = []
        for anchor in page.locator("a").all():
            if len(links) >= max_links:
                break
            try:
                if not anchor.is_visible():
                    continue
                label = " ".join((anchor.inner_text(timeout=500) or "").split())
                href = anchor.get_attribute("href")
                if not label or not href:
                    continue
                full_url = urljoin(page.url, href)
                target = urlsplit(full_url)
                current = urlsplit(page.url)
                if (target.scheme, target.netloc) != (current.scheme, current.netloc):
                    continue
                action_text = unquote(f"{label} {target.path} {target.query}")
                if _BLOCKED_ACTION_RE.search(action_text):
                    continue
                link_id = len(links) + 1
                self._links[link_id] = full_url
                links.append({"id": link_id, "label": label[:160], "url": _safe_url(full_url)})
            except Exception:
                continue

        try:
            visible_text = page.locator("body").inner_text(timeout=3000)
        except Exception:
            visible_text = ""
        return {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "title": page.title(),
            "url": _safe_url(page.url),
            "visible_text": visible_text[:max_chars],
            "truncated": len(visible_text) > max_chars,
            "same_origin_links": links,
        }

    def follow_link(self, link_id: int) -> str:
        if not self.is_open:
            raise RuntimeError("No portal browser is open.")
        url = self._links.get(link_id)
        if not url:
            raise ValueError("That link is not in the latest page snapshot.")
        current = urlsplit(self._page.url)
        target = urlsplit(url)
        if (target.scheme, target.netloc) != (current.scheme, current.netloc):
            raise ValueError("Navigation is limited to links on the current site origin.")
        self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
        self._links.clear()
        return self._page.title()

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
        if self._playwright is not None:
            self._playwright.stop()
        self._browser = None
        self._playwright = None
        self._page = None
        self._links.clear()
        self.ai_consent = False
