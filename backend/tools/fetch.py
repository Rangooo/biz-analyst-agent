"""网页正文抓取 —— 把搜索命中的页面取回干净文本，供 LLM 引用核对。

策略：先用 httpx 直取（快）；失败/内容太短时调用 Playwright Scraper 技能的
playwright-simple.js（处理动态页）。HTML→文本用轻量正则清洗。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import httpx

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


def fetch_text(url: str, max_chars: int = 6000) -> str:
    """返回页面正文文本（已清洗、截断）。失败返回空串。"""
    text = _httpx_get(url)
    if len(text) < 400 and _PW_SIMPLE.exists():
        pw = _playwright_get(url)
        if len(pw) > len(text):
            text = pw
    return text[:max_chars]


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
