"""Firecrawl v2 integration for search and JavaScript-rendered page scraping."""
from __future__ import annotations

import logging
import os

import httpx

from schemas import Evidence, SourceTier
from tools.finance_sources import BaseAdapter, SourceResult

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.firecrawl.dev/v2"


def _api_key() -> str:
    return os.getenv("FIRECRAWL_API_KEY", "").strip()


def _base_url() -> str:
    return os.getenv("FIRECRAWL_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _post(path: str, payload: dict) -> dict:
    key = _api_key()
    if not key:
        return {}
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        with httpx.Client(timeout=60, headers=headers) as client:
            response = client.post(f"{_base_url()}{path}", json=payload)
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Firecrawl request failed for %s: %s", path, exc)
        return {}
    return body if body.get("success", True) else {}


def scrape_url(url: str, *, only_main: bool = True, wait_for: int = 0,
               max_chars: int = 8000) -> str:
    """Scrape one URL and return markdown, or an empty string on failure."""
    payload: dict = {"url": url, "formats": ["markdown"], "onlyMainContent": only_main}
    if wait_for > 0:
        payload["waitFor"] = wait_for
    markdown = (_post("/scrape", payload).get("data") or {}).get("markdown", "")
    return markdown[:max_chars] if isinstance(markdown, str) else ""


def search_web(query: str, *, max_results: int = 5,
               days: int | None = None) -> list[dict]:
    """Search the web through Firecrawl v2."""
    payload: dict = {"query": query, "limit": max_results, "sources": ["web"]}
    if days:
        payload["tbs"] = f"qdr:d{days}"
    data = _post("/search", payload).get("data") or {}
    rows = data.get("web", []) if isinstance(data, dict) else []
    return [row for row in rows[:max_results] if isinstance(row, dict)]


class FirecrawlAdapter(BaseAdapter):
    """Search adapter used when a Firecrawl API key is configured."""

    name = "firecrawl"

    def __init__(self):
        self.available = firecrawl_available()

    def search(self, query, kind="general", max_results=5, days=None):
        if not self.available:
            return SourceResult(meta={"error": "FIRECRAWL_API_KEY is not configured"})
        rows = search_web(query, max_results=max_results, days=days)
        evidences = [Evidence(
            content=(row.get("description") or row.get("markdown") or "")[:400],
            source_url=row.get("url", ""),
            source_title=(row.get("title") or "Firecrawl search")[:80],
            source_type="web",
            tier=SourceTier.MEDIA,
        ) for row in rows if row.get("url") or row.get("title")]
        return SourceResult(evidences=evidences)

    def deep_scrape(self, url: str, max_chars: int = 8000) -> str:
        return scrape_url(url, max_chars=max_chars)


def firecrawl_available() -> bool:
    return bool(_api_key())


def firecrawl_budget_status() -> dict:
    """Expose configuration state; Firecrawl owns authoritative usage limits."""
    return {"configured": firecrawl_available(), "api_version": "v2"}
