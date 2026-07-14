"""网页正文抓取 —— 把搜索命中的页面取回干净文本，供 LLM 引用核对。

策略（三级 fallback）：
1. httpx 直取（快、免费）
2. 内容太短 → Firecrawl scrape（JS 渲染，5 credits，有预算管理）
3. Firecrawl 也失败/预算不足 → Playwright 本地（慢但无限）
HTML→文本用轻量正则清洗。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

# Playwright scraper 脚本路径：优先环境变量，默认用 ~/.workbuddy/skills/ 标准位置
_SKILLS_DIR = os.path.expanduser("~/.workbuddy/skills")
_PW_SIMPLE = Path(os.getenv(
    "PW_SCRAPER_SCRIPT",
    os.path.join(_SKILLS_DIR, "skill_2053082580726448129", "scripts", "playwright-simple.js"),
))
# Node 解释器：优先环境变量，其次 PATH 中的 node
_NODE = os.getenv("NODEBIN", shutil.which("node") or "node")

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

# 内容长度阈值：低于此值认为 httpx 抓取不充分，需要 JS 渲染
_MIN_CONTENT_LEN = 400


def fetch_text(url: str, max_chars: int = 6000) -> str:
    """返回页面正文文本（已清洗、截断）。失败返回空串。

    Fallback 链：httpx → Firecrawl(JS渲染) → Playwright(本地)
    """
    # 第一层：httpx 直取（免费、最快）
    text = _httpx_get(url)
    if len(text) >= _MIN_CONTENT_LEN:
        return text[:max_chars]

    # 第二层：Firecrawl 深度抓取（JS 渲染，有预算管理）
    fc_text = _firecrawl_get(url, max_chars)
    if len(fc_text) > len(text):
        text = fc_text
    if len(text) >= _MIN_CONTENT_LEN:
        return text[:max_chars]

    # 第三层：Playwright 本地（无限但慢）
    if _PW_SIMPLE.exists():
        pw = _playwright_get(url)
        if len(pw) > len(text):
            text = pw

    return text[:max_chars]


def _firecrawl_get(url: str, max_chars: int = 6000) -> str:
    """通过 Firecrawl 深度抓取（含 JS 渲染）。预算不足时返回空串。"""
    try:
        from tools.firecrawl_adapter import scrape_url
        return scrape_url(url, max_chars=max_chars)
    except ImportError:
        return ""
    except Exception as e:  # noqa: BLE001
        logger.debug("Firecrawl fetch 异常: %s", e)
        return ""


def _httpx_get(url: str) -> str:
    try:
        with httpx.Client(timeout=25, headers={"User-Agent": _UA}, follow_redirects=True) as c:
            r = c.get(url)
            r.raise_for_status()
            return _html_to_text(r.text)
    except Exception:  # noqa: BLE001
        return ""


def _playwright_get(url: str) -> str:
    try:
        proc = subprocess.run(
            [_NODE, str(_PW_SIMPLE), url],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode == 0 and proc.stdout:
            data = json.loads(proc.stdout)
            return data.get("content", "") or ""
    except Exception:  # noqa: BLE001
        pass
    return ""


def _html_to_text(html: str) -> str:
    html = re.sub(r"<script[\s\S]*?</script>", " ", html, flags=re.I)
    html = re.sub(r"<style[\s\S]*?</style>", " ", html, flags=re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    html = re.sub(r"&nbsp;", " ", html)
    html = re.sub(r"&amp;", "&", html)
    html = re.sub(r"\s+", " ", html)
    return html.strip()
