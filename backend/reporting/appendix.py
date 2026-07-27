"""Report appendix helpers: data gaps and compact references."""
from __future__ import annotations

from collections.abc import Callable
import re

from reporting.structure import _extract_cited_ids
from schemas import AnalysisRun, DataGap, Evidence, Verdict


def _safe_str(v, max_len: int = 800) -> str:
    if isinstance(v, str):
        return v[:max_len]
    if v is None:
        return ""
    return str(v)[:max_len]


def format_apa_reference(ev: Evidence, idx: int) -> str:
    """Format one evidence item as a compact APA-like markdown row."""
    title = (ev.source_title or "").strip() or "（未标注来源）"
    date_str = ev.as_of or ev.published_at or ""
    year = date_str[:4] if date_str and len(date_str) >= 4 else ""
    year_str = f"({year})" if year else ""
    stance = "支撑" if ev.supports else "反证"
    tier_str = ev.tier_label
    url_part = ev.source_url or ""
    # 截断过长 URL（超过 120 字符的 URL 会撑破前端布局）
    if len(url_part) > 120:
        url_part = url_part[:120] + "..."
    parts = [f"- **[^{idx}]**: {title}"]
    if year_str:
        parts.append(year_str)
    parts.append(f"[{tier_str}·{stance}]")
    if url_part:
        parts.append(url_part)
    return " ".join(parts)


def _gap_key(gap: DataGap) -> tuple[str, str, str]:
    return (
        (gap.topic or "").strip()[:40],
        (gap.gap_type or "").strip(),
        (gap.detail or "").strip()[:60],
    )


def _has_financial_unit_conflict(text: str) -> bool:
    """Detect likely scale/period risks before the report treats numbers as facts."""
    if not text:
        return False
    has_unit_mix = "百万元" in text and "亿元" in text
    has_financial_metric = bool(re.search(r"营收|收入|净利润|经营利润|EBITA|毛利|利润率", text))
    has_time_series = bool(re.search(r"FY\d{2,4}|20\d{2}|Q[1-4]|季度|财年", text))
    return has_unit_mix and has_financial_metric and has_time_series


def _has_strong_causal_gap(text: str) -> bool:
    """Flag strong attribution language when the same report admits missing direct evidence."""
    if not text:
        return False
    strong_terms = r"主因|核心驱动|直接导致|不是一次性|非一次性|足以证明|确认|替代.*核心利润|利润.*结构性"
    gap_terms = r"未披露|未拆分|无法精确|不能精确|数据缺口|披露不足|缺乏直接|缺少直接"
    attribution_topics = r"AI|云智能|分部|利润|毛利率|经营利润|一次性|资产处置"
    return (
        bool(re.search(strong_terms, text))
        and bool(re.search(gap_terms, text))
        and bool(re.search(attribution_topics, text))
    )


def collect_data_gaps(run: AnalysisRun, body: str = "") -> list[DataGap]:
    """Build report-level data gap appendix from deterministic runtime signals.

    精简原则：只保留读者不易察觉的结构性缺口（单位冲突、financials/行业总量缺失）。
    其他缺口（[UNSOURCED] 标记、"未披露"计数、强归因闸门、collect_quality 问题、
    被推翻洞察）应该在正文里直接处理，不应在附录重复。
    """
    gaps: list[DataGap] = []
    seen: set[tuple[str, str, str]] = set()

    def add(topic: str, gap_type: str, detail: str, impact: str,
            suggested_source: str, priority: str = "medium") -> None:
        topic = _safe_str(topic, 80).strip() or "未命名议题"
        detail = _safe_str(detail, 180).strip()
        if not detail:
            return
        gap = DataGap(
            topic=topic,
            gap_type=gap_type,
            detail=detail,
            impact=_safe_str(impact, 120).strip(),
            suggested_source=_safe_str(suggested_source, 120).strip(),
            priority=priority if priority in ("high", "medium", "low") else "medium",
        )
        key = _gap_key(gap)
        if key not in seen:
            gaps.append(gap)
            seen.add(key)

    profile = run.profile
    is_industry = bool(profile and profile.kind == "industry")
    is_public_company = bool(profile and profile.kind == "company" and profile.is_public)

    if body:
        # 1. 财务单位/口径冲突——读者不易察觉的硬问题，必须提醒
        if _has_financial_unit_conflict(body):
            add(
                "财务口径一致性",
                "conflict",
                "正文同时使用百万元与亿元等财务单位，且涉及营收/利润与年度或季度时序，存在单位、期间或换算口径混用风险。",
                "财务表格和趋势判断可能出现量级错误，必须先统一单位和期间口径再下结论。",
                "补查同一期官方财报、业绩公告、交易所公告或 IR PDF，并在表格中统一单位。",
                "high",
            )

    # 2. 上市公司无 financials——结构性数据缺口
    if is_public_company and not run.financials:
        add(
            "财务时序",
            "missing_data",
            "上市公司未抽取到可用的多年/最新季度财务时序。",
            "财务概览和增长质量判断可能只能依赖零散文本证据。",
            "补查年报、季报、业绩公告、交易所公告或官方 IR PDF。",
            "high",
        )

    # 3. 行业报告无总量/价格——结构性数据缺口
    if is_industry:
        metrics = run.industry_metrics or {}
        totals = metrics.get("totals") if isinstance(metrics, dict) else []
        prices = metrics.get("prices") if isinstance(metrics, dict) else []
        section_text = " ".join(profile.sections or []) if profile else ""
        if not totals:
            add(
                "行业总量口径",
                "missing_data",
                "未抽取到行业总量/规模/产量等核心时序指标。",
                "市场空间、周期定位和增长驱动判断缺少量化锚点。",
                "补查监管统计、行业协会、国家统计局、龙头公司年报或权威咨询报告。",
                "high",
            )
        if ("价格" in section_text or "供需" in section_text) and not prices:
            add(
                "行业价格时序",
                "missing_data",
                "行业维度包含供需/价格判断，但未抽取到代表性产品价格时序。",
                "量价关系和周期拐点判断可能偏定性。",
                "补查行业协会、交易所报价、专业价格数据库或权威研报。",
                "medium",
            )

    return gaps[:6]  # 最多 6 条（原来 12 条）


def build_appendix_md(
    run: AnalysisRun,
    evidence_pool: dict[int, Evidence],
    idx_of: Callable[[Evidence], int],
    extra_text: str = "",
) -> str:
    """Build data-gap and reference appendix markdown."""
    data_gaps = collect_data_gaps(run, extra_text)
    run.data_gaps = data_gaps
    cited: set[int] = set()
    cited |= _extract_cited_ids(extra_text)
    if not cited:
        for ins in run.insights:
            for e in ins.evidence:
                idx = idx_of(e)
                if idx:
                    cited.add(idx)
        # 无引用时的兜底：取前 20 条高质量证据
        cited = set(sorted(cited)[:20])

    cited_list = sorted(cited)
    # 按 tier 排序（高质量优先），不限条数
    cited_list.sort(key=lambda i: (evidence_pool[i].tier if i in evidence_pool else 99, i))

    total = len(evidence_pool)
    lines = ["---", ""]

    lines += [f"#### 参考文献（核心引用 {len(cited_list)} 条 / 共采集 {total} 条）", ""]
    for i in cited_list:
        if i not in evidence_pool:
            continue
        lines.append(format_apa_reference(evidence_pool[i], i))
    return "\n".join(lines)
