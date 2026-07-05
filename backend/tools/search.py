"""搜索工具 —— agent 联网采集公开信息的核心入口。

搜索源架构（2026-06-27 更新）：
- **Exa（主搜索源）**：通过 mcporter 调用 Exa MCP，免费、语义搜索、中英混合友好。
  在 orchestrator._collect() 中优先调用。本文件不直接封装 Exa（见 finance_sources.py 的 ExaSearchAdapter）。
- **Tavily（补充源）**：结构化好、有免费额度。在本文件中封装，供需要结构化搜索结果的场景使用。
- **Serper（补充源备用）**：Tavily 无 key 或耗尽时回退。

额度韧性：
- 查询缓存去重：相同 (query, max_results, days) 命中缓存，省 Tavily 调用。
- Tavily 耗尽检测：连续 N 次空/错响应判定为耗尽，search_status() 暴露 degraded，
  健康检查据此告警（替代"key 还在就显示可用"的误导）。
- **Exa 可用时不标记 degraded**：Exa 是主搜索源，免费且语义质量高，有 Exa 即搜索功能正常。
"""
from __future__ import annotations

import os
import shutil
import time

import httpx

# ---------- 查询缓存（进程内，去重省额度）----------
_CACHE: dict[tuple, list[dict]] = {}
_CACHE_TTL = 6 * 3600  # 6 小时内同查询复用

# ---------- Tavily 耗尽检测 ----------
_TAVILY_FAIL = 0          # 连续失败计数
_TAVILY_EXHAUST_LIMIT = 5  # 连续 5 次空/错 → 判定耗尽
_TAVILY_EXHAUSTED = False  # 是否已判定耗尽


def _cache_get(key):
    item = _CACHE.get(key)
    if not item:
        return None
    ts, rows = item
    if time.time() - ts > _CACHE_TTL:
        return None
    return rows


def _cache_set(key, rows):
    _CACHE[key] = (time.time(), rows)


def search(query: str, max_results: int = 6, days: int | None = None) -> list[dict]:
    """返回 [{title, url, content, score}]。days 限定时效。带缓存去重。"""
    global _TAVILY_FAIL, _TAVILY_EXHAUSTED
    key = (query.strip().lower(), max_results, days)
    hit = _cache_get(key)
    if hit is not None:
        return hit

    tavily = os.getenv("TAVILY_API_KEY")
    rows: list[dict] = []
    if tavily and not _TAVILY_EXHAUSTED:
        rows = _tavily(query, max_results, days, tavily)
        if rows:
            _TAVILY_FAIL = 0  # 成功，重置计数
        else:
            _TAVILY_FAIL += 1
            if _TAVILY_FAIL >= _TAVILY_EXHAUST_LIMIT:
                _TAVILY_EXHAUSTED = True
    if not rows:
        serper = os.getenv("SERPER_API_KEY")
        if serper:
            rows = _serper(query, max_results, serper)
    _cache_set(key, rows)
    return rows


def has_search_backend() -> bool:
    """是否配置了任何搜索源（Exa/Tavily/Serper 任一可用即 True）。"""
    exa = bool(os.getenv("EXA_API_KEY") or shutil.which("mcporter"))
    return exa or bool(os.getenv("TAVILY_API_KEY") or os.getenv("SERPER_API_KEY"))


def search_status() -> dict:
    """搜索后端健康：综合 Exa(主) + Tavily/Serper(补充) 判断。

    status ∈ ok|degraded|unavailable：
    - ok: 至少有 Exa 主搜索源（免费且语义好），或 Tavily 正常可用
    - degraded: 无 Exa，且 Tavily 耗尽但 Serper 可用（降级但不中断）
    - unavailable: 所有搜索源都不可用
    """
    tavily = bool(os.getenv("TAVILY_API_KEY"))
    serper = bool(os.getenv("SERPER_API_KEY"))
    exa = bool(os.getenv("EXA_API_KEY") or shutil.which("mcporter"))
    exhausted = _TAVILY_EXHAUSTED

    if not exa and not tavily and not serper:
        st = "unavailable"
    elif exa:
        # 有 Exa 主搜索源 → 搜索功能正常，Tavily/Serper 只是补充
        st = "ok"
    else:
        # 无 Exa，靠 Tavily/Serper
        if exhausted and serper:
            st = "degraded"  # Tavily 挂了，Serper 顶上
        elif exhausted and not serper:
            st = "unavailable"  # Tavily 挂了，无 Serper，无 Exa
        else:
            st = "ok"
    return {"status": st, "tavily": tavily, "serper": serper, "exa": exa,
            "exhausted": exhausted,
            "cache_size": len(_CACHE)}


def _tavily(query, max_results, days, key) -> list[dict]:
    payload = {
        "api_key": key,
        "query": query,
        "max_results": max_results,
        "search_depth": "advanced",
        "include_answer": False,
    }
    if days:
        payload["days"] = days
    try:
        with httpx.Client(timeout=40) as c:
            r = c.post("https://api.tavily.com/search", json=payload)
            r.raise_for_status()
            data = r.json()
        return [
            {
                "title": it.get("title", ""),
                "url": it.get("url", ""),
                "content": it.get("content", ""),
                "score": it.get("score", 0.0),
            }
            for it in data.get("results", [])
        ]
    except Exception:  # noqa: BLE001
        return []


def _serper(query, max_results, key) -> list[dict]:
    try:
        with httpx.Client(timeout=40) as c:
            r = c.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": key, "Content-Type": "application/json"},
                json={"q": query, "num": max_results},
            )
            r.raise_for_status()
            data = r.json()
        out = []
        for it in data.get("organic", [])[:max_results]:
            out.append({
                "title": it.get("title", ""),
                "url": it.get("link", ""),
                "content": it.get("snippet", ""),
                "score": 0.5,
            })
        return out
    except Exception:  # noqa: BLE001
        return []
