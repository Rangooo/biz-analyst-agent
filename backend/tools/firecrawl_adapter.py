"""Firecrawl v2 integration for search and JavaScript-rendered page scraping."""
from __future__ import annotations

import logging
import os
import json
from datetime import datetime
from pathlib import Path

import httpx

from schemas import Evidence, SourceTier
from tools.finance_sources import BaseAdapter, SourceResult

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.firecrawl.dev/v2"
KEYLESS_MONTHLY_CREDITS = 1000
SCRAPE_CREDITS = 5
SEARCH_CREDITS = 2
_USAGE_FILE = Path(os.getenv(
    "FIRECRAWL_USAGE_FILE",
    str(Path.home() / ".workbuddy" / "cache" / "firecrawl_usage.json"),
))


class _KeylessBudget:
    """Conservative local guard for Firecrawl's monthly keyless allowance."""

    def __init__(self, path: Path | None = None):
        self.path = path or _USAGE_FILE

    @staticmethod
    def month() -> str:
        return datetime.now().strftime("%Y-%m")

    def load(self) -> dict:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if data.get("month") == self.month():
                return data
        except (OSError, ValueError):
            pass
        return {"month": self.month(), "used": 0}

    def used(self) -> int:
        return int(self.load().get("used", 0))

    def allows(self, cost: int) -> bool:
        return self.used() + cost <= KEYLESS_MONTHLY_CREDITS

    def record(self, cost: int) -> None:
        data = self.load()
        data["used"] = int(data.get("used", 0)) + cost
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2), encoding="utf-8")


_budget = _KeylessBudget()


def _api_key() -> str:
    return os.getenv("FIRECRAWL_API_KEY", "").strip()


def _base_url() -> str:
    return os.getenv("FIRECRAWL_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def _post(path: str, payload: dict, *, keyless_cost: int) -> dict:
    key = _api_key()
    if not key and not _budget.allows(keyless_cost):
        logger.info("Firecrawl keyless monthly budget exhausted")
        return {}
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        with httpx.Client(timeout=60, headers=headers) as client:
            response = client.post(f"{_base_url()}{path}", json=payload)
            response.raise_for_status()
            body = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Firecrawl request failed for %s: %s", path, exc)
        return {}
    if not body.get("success", True):
        return {}
    if not key:
        _budget.record(keyless_cost)
    return body


def scrape_url(url: str, *, only_main: bool = True, wait_for: int = 0,
               max_chars: int = 8000) -> str:
    """Scrape one URL and return markdown, or an empty string on failure."""
    payload: dict = {"url": url, "formats": ["markdown"], "onlyMainContent": only_main}
    if wait_for > 0:
        payload["waitFor"] = wait_for
    markdown = (_post("/scrape", payload, keyless_cost=SCRAPE_CREDITS).get("data") or {}).get("markdown", "")
    return markdown[:max_chars] if isinstance(markdown, str) else ""


def search_web(query: str, *, max_results: int = 5,
               days: int | None = None) -> list[dict]:
    """Search the web through Firecrawl v2."""
    payload: dict = {"query": query, "limit": max_results, "sources": ["web"]}
    if days:
        payload["tbs"] = f"qdr:d{days}"
    data = _post("/search", payload, keyless_cost=SEARCH_CREDITS).get("data") or {}
    rows = data.get("web", []) if isinstance(data, dict) else []
    return [row for row in rows[:max_results] if isinstance(row, dict)]


class FirecrawlAdapter(BaseAdapter):
    """Search adapter used when a Firecrawl API key is configured."""

    name = "firecrawl"

    def __init__(self):
        self.available = firecrawl_available()

    def search(self, query, kind="general", max_results=5, days=None):
        if not self.available:
            return SourceResult(meta={"error": "Firecrawl keyless monthly budget is exhausted"})
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
    return bool(_api_key()) or _budget.allows(SEARCH_CREDITS)


def firecrawl_budget_status() -> dict:
    """Expose API-key mode or the conservative local keyless usage counter."""
    key = bool(_api_key())
    used = _budget.used() if not key else None
    return {
        "available": firecrawl_available(),
        "mode": "api_key" if key else "keyless",
        "api_version": "v2",
        "used": used,
        "remaining": None if key else max(0, KEYLESS_MONTHLY_CREDITS - used),
        "monthly_budget": None if key else KEYLESS_MONTHLY_CREDITS,
    }
