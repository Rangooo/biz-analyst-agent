"""SEC EDGAR 官方接口 —— 上市公司（美股）结构化财务数据。

复用 earning-explore 经验：EDGAR 全免费、无需 key，但要求请求头带 User-Agent。
两个核心端点：
1. company_tickers.json —— ticker → CIK 映射
2. companyconcept / companyfacts —— 标准化 XBRL 财务事实（营收、净利润等）
"""
from __future__ import annotations

import os

import httpx

def _headers() -> dict[str, str]:
    return {
        "User-Agent": os.getenv("SEC_USER_AGENT", "biz-analyst-agent contact@example.com"),
        "Accept-Encoding": "gzip, deflate",
    }

_TICKER_CACHE: dict[str, str] = {}


def lookup_cik(ticker: str) -> str | None:
    """ticker -> 10 位 CIK（带前导零）。"""
    ticker = ticker.upper().strip()
    if not _TICKER_CACHE:
        try:
            with httpx.Client(timeout=30, headers=_headers()) as c:
                r = c.get("https://www.sec.gov/files/company_tickers.json")
                r.raise_for_status()
                data = r.json()
            for item in data.values():
                _TICKER_CACHE[item["ticker"].upper()] = str(item["cik_str"]).zfill(10)
        except Exception:  # noqa: BLE001
            return None
    return _TICKER_CACHE.get(ticker)


def get_key_financials(ticker: str, n_annual: int = 5, n_quarter: int = 4) -> dict:
    """拉取关键财务概念，按报告期对齐。返回 {concept: [{fy, fp, val, end, form, period_type}]}。

    两个关键保证：
    1. 年度/季度分离：每行带 period_type（annual/quarter），年度行在前、季度行在后，
       避免季度值(如单季 8B)与年度值(如全年 33B)混排造成误读。
    2. 各指标期数一致：以营收(缺则净利润)为锚确定统一的报告期集合，所有指标对齐到同一组
       报告期；某指标某期缺披露时补 None 占位，保证营收/净利润/毛利等行数严格一致。
    """
    cik = lookup_cik(ticker)
    if not cik:
        return {"error": f"未找到 {ticker} 的 CIK（可能非美股上市）"}
    # 每个 alias 对应一组候选 XBRL 概念（按优先级排列）。
    # 营收尤其重要：公司在 ASC 606 准则切换前后会更换标签
    # （旧 Revenues / SalesRevenueNet → 新 RevenueFromContractWithCustomer...），
    # 必须把同一逻辑指标的多个标签合并、按报告期去重，否则时间线会断裂/重叠。
    concept_groups = {
        "Revenue": [
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "RevenueFromContractWithCustomerIncludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
        ],
        "NetIncome": ["NetIncomeLoss"],
        "GrossProfit": ["GrossProfit"],
        "OperatingIncome": ["OperatingIncomeLoss"],
    }

    def _is_annual(r: dict) -> bool:
        return r.get("fp") == "FY" or r.get("form") in ("10-K", "20-F")

    # 1. 各 alias 跨标签按报告期(end)去重合并
    raw: dict = {}  # alias -> {end: row}
    for alias, candidates in concept_groups.items():
        merged: dict = {}
        for concept in candidates:
            for r in _company_concept(cik, concept):
                end = r.get("end")
                if not end:
                    continue
                existing = merged.get(end)
                # 候选列表靠前的标签优先；同期 10-K 可覆盖非 10-K
                if existing is None or (r.get("form") == "10-K" and existing.get("form") != "10-K"):
                    merged[end] = r
        raw[alias] = merged

    # 2. 锚定统一报告期：取所有指标年度/季度 end 的并集，按时间倒序取最近 N 期。
    #    用并集而非单指标——某指标某年缺标签时仍保留该报告期（值补 None），
    #    避免营收因 ASC606 标签缺口把整条时间线打散（如 PayPal 跳过 2020/2021）。
    all_annual_ends = sorted(
        {e for m in raw.values() for e, r in m.items() if _is_annual(r)}, reverse=True
    )[:n_annual]
    all_quarter_ends = sorted(
        {e for m in raw.values() for e, r in m.items() if not _is_annual(r)}, reverse=True
    )[:n_quarter]
    annual_ends, quarter_ends = all_annual_ends, all_quarter_ends

    # 3. 各指标对齐到锚定报告期，缺失补 None 占位 → 各指标期数严格一致
    def _aligned(merged: dict, ends: list, ptype: str) -> list:
        rows = []
        for e in ends:
            r = merged.get(e)
            if r:
                rows.append({**r, "period_type": ptype})
            else:
                rows.append({"fy": None, "fp": "FY" if ptype == "annual" else "",
                             "val": None, "end": e, "form": "", "period_type": ptype})
        return rows

    concepts: dict = {}
    for alias, merged in raw.items():
        rows = _aligned(merged, annual_ends, "annual") + _aligned(merged, quarter_ends, "quarter")
        if any(r.get("val") is not None for r in rows):  # 全空指标不收录
            concepts[alias] = rows

    return {"cik": cik, "ticker": ticker.upper(), "concepts": concepts,
            "annual_ends": annual_ends, "quarter_ends": quarter_ends}


def _company_concept(cik: str, concept: str) -> list[dict]:
    """拉取单个 XBRL 概念的所有 10-K/10-Q/20-F 报告行。

    不做 limit 截断——截断会导致中间年份丢失（如 NetIncomeLoss 有 249 条，
    前 40 条按 end 倒序全是近年季度行，旧年报行触达不到）。
    行数控制由上层 get_key_financials 的 n_annual/n_quarter 切片完成。
    """
    url = (
        f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/"
        f"us-gaap/{concept}.json"
    )
    try:
        with httpx.Client(timeout=30, headers=_headers()) as c:
            r = c.get(url)
            if r.status_code != 200:
                return []
            data = r.json()
        units = data.get("units", {})
        usd = units.get("USD", [])
        # 只取年度/季度报告，按结束日期倒序；不截断
        rows = [
            {
                "fy": it.get("fy"),
                "fp": it.get("fp"),
                "val": it.get("val"),
                "end": it.get("end"),
                "form": it.get("form"),
            }
            for it in usd
            if it.get("form") in ("10-K", "10-Q", "20-F")
        ]
        rows.sort(key=lambda x: x.get("end") or "", reverse=True)
        return rows
    except Exception:  # noqa: BLE001
        return []
