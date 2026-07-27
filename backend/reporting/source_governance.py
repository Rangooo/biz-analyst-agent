"""Pure source-URL governance utilities."""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urlparse


_TRACKING_QUERY_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "from", "froms", "source", "ref", "share_token",
}
_SUSPICIOUS_QUERY_RE = re.compile(
    r"联系\s*tg|telegram|撞库|数据渗透|海外支付通道|群发微信",
    re.IGNORECASE,
)


def sanitize_source_url(url: str) -> str:
    """Remove tracking/spam query payloads without changing the source page."""
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw)
        clean_pairs = []
        for key, value in parse_qsl(parsed.query, keep_blank_values=False):
            if key.lower() in _TRACKING_QUERY_KEYS:
                continue
            if _SUSPICIOUS_QUERY_RE.search(f"{key}={value}"):
                continue
            clean_pairs.append((key, value))
        return parsed._replace(query=urlencode(clean_pairs, doseq=True), fragment="").geturl()
    except Exception:  # noqa: BLE001
        return raw
