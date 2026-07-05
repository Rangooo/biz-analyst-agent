"""Run-level quality and observability metrics.

The orchestrator keeps the business workflow; this module keeps deterministic
scoring and JSON export logic that can be tested without LLM calls.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from schemas import AnalysisRun, Evidence


DATA_DIR = Path(__file__).resolve().parent / "data"


def _as_list(evidences) -> list[Evidence]:
    if isinstance(evidences, dict):
        return list(evidences.values())
    return list(evidences or [])


def _domain(url: str) -> str:
    host = urlparse(url or "").netloc.lower()
    return host[4:] if host.startswith("www.") else host


def _ratio(count: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(count / total, 3)


def _section_terms(section: str) -> list[str]:
    section = str(section or "").strip()
    if not section:
        return []

    terms = {section.lower()}
    for part in re.split(r"[\s,，/、|&]+|与|和|及|以及|vs|VS", section):
        part = part.strip().lower()
        if len(part) >= 2:
            terms.add(part)
            for suffix in ("质量", "趋势", "能力", "真实性", "走向", "表现", "指标", "格局"):
                if part.endswith(suffix) and len(part) > len(suffix) + 1:
                    terms.add(part[: -len(suffix)])

    if len(section) >= 4:
        terms.add(section[:4].lower())
    if len(section) >= 2:
        terms.add(section[:2].lower())

    return sorted(terms, key=len, reverse=True)


def _section_coverage(sections: list[str], evidences: list[Evidence]) -> dict:
    result = {}
    texts = [
        f"{ev.source_title or ''}\n{ev.content or ''}".lower()
        for ev in evidences
    ]
    for section in sections or []:
        terms = _section_terms(section)
        hit_count = 0
        for text in texts:
            if any(term and term in text for term in terms):
                hit_count += 1
        result[section] = {
            "covered": hit_count >= 1,
            "hits": hit_count,
            "terms": terms[:5],
        }
    return result


def _parse_dt(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _elapsed_ms(start: datetime | None, end: datetime | None) -> int | None:
    if not start or not end or end < start:
        return None
    return int((end - start).total_seconds() * 1000)


def _run_duration_ms(run: AnalysisRun) -> int | None:
    start = _parse_dt(run.created_at)
    end = _parse_dt(run.updated_at)
    if run.trace:
        trace_times = [_parse_dt(ev.ts) for ev in run.trace]
        trace_times = [ts for ts in trace_times if ts is not None]
        if trace_times:
            start = min([start] + trace_times) if start else min(trace_times)
            end = max([end] + trace_times) if end else max(trace_times)
    return _elapsed_ms(start, end)


def _stage_durations_ms(run: AnalysisRun) -> dict[str, int]:
    stage_times: dict[str, list[datetime]] = {}
    for ev in run.trace:
        ts = _parse_dt(ev.ts)
        if not ts:
            continue
        stage_times.setdefault(ev.stage or "unknown", []).append(ts)
    result = {}
    for stage, times in stage_times.items():
        if len(times) < 2:
            result[stage] = 0
        else:
            result[stage] = int((max(times) - min(times)).total_seconds() * 1000)
    return result


def evaluate_collect_quality(run: AnalysisRun, evidences) -> dict:
    """Score the collect stage evidence pool on a deterministic 0-100 scale."""
    evs = _as_list(evidences)
    total = len(evs)
    urls = [ev.source_url for ev in evs if ev.source_url]
    titles = [ev.source_title for ev in evs if ev.source_title]
    dates = [ev.as_of or ev.published_at for ev in evs if (ev.as_of or ev.published_at)]
    source_types = sorted({ev.source_type for ev in evs if ev.source_type})

    source_ids = {_domain(ev.source_url) for ev in evs if _domain(ev.source_url)}
    if evs:
        source_ids.update(run.data_sources_used or [])
    source_count = len(source_ids)

    tier_counts: dict[str, int] = {}
    authoritative_count = 0
    for ev in evs:
        tier = str(int(ev.tier))
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        if int(ev.tier) <= 3:
            authoritative_count += 1

    sections = run.profile.sections if run.profile else []
    section_detail = _section_coverage(sections, evs)
    covered_sections = sum(1 for item in section_detail.values() if item["covered"])
    section_ratio = _ratio(covered_sections, len(section_detail))

    url_coverage = _ratio(len(urls), total)
    title_coverage = _ratio(len(titles), total)
    date_coverage = _ratio(len(dates), total)

    volume_score = min(25, total * 2)
    diversity_score = min(20, source_count * 5)
    metadata_score = round(20 * (url_coverage * 0.4 + title_coverage * 0.3 + date_coverage * 0.3))
    authority_score = min(20, authoritative_count * 5)
    section_score = round(15 * section_ratio) if sections else 10
    score = int(min(100, volume_score + diversity_score + metadata_score + authority_score + section_score))

    issues = []
    if total < 8:
        issues.append("证据量不足，建议补充至少 8 条可溯源证据")
    if source_count < 2:
        issues.append("来源多样性不足，建议引入第二类独立数据源")
    if authoritative_count == 0:
        issues.append("缺少财报、公告或监管等高权威信源")
    if url_coverage < 0.8:
        issues.append("部分证据缺少来源 URL")
    if title_coverage < 0.8:
        issues.append("部分证据缺少来源标题")
    if date_coverage < 0.6:
        issues.append("证据时点覆盖不足，建议补充 published_at/as_of")
    if sections and section_ratio < 0.7:
        issues.append("分析维度覆盖不足，建议针对弱覆盖维度补搜")

    status = "ok" if score >= 70 else "weak" if score >= 45 else "poor"
    return {
        "score": score,
        "status": status,
        "issues": issues,
        "evidence_count": total,
        "source_count": source_count,
        "source_types": source_types,
        "tier_counts": tier_counts,
        "authoritative_evidence_count": authoritative_count,
        "fresh_evidence_count": len(dates),
        "url_coverage": url_coverage,
        "title_coverage": title_coverage,
        "date_coverage": date_coverage,
        "section_coverage_ratio": section_ratio,
        "sections": section_detail,
        "score_components": {
            "volume": volume_score,
            "diversity": diversity_score,
            "metadata": metadata_score,
            "authority": authority_score,
            "section": section_score,
        },
    }


def build_run_metrics(run: AnalysisRun) -> dict:
    diagnostics = [
        {
            "stage": ev.stage,
            "type": ev.type,
            "title": ev.title,
            "detail": ev.detail[:240],
            "ts": ev.ts,
        }
        for ev in run.trace
        if ev.type == "diagnosis" or any(k in ev.title for k in ("降级", "出错", "失败", "问题", "⚠"))
    ]
    return {
        "run_id": run.id,
        "query": run.query,
        "status": run.status,
        "created_at": run.created_at,
        "updated_at": run.updated_at,
        "duration_ms": _run_duration_ms(run),
        "stage_durations_ms": _stage_durations_ms(run),
        "data_as_of": run.data_as_of,
        "data_sources_used": run.data_sources_used,
        "evidence_count": len(run.evidence_pool),
        "insight_count": len(run.insights),
        "trace_count": len(run.trace),
        "collect_quality": run.collect_quality,
        "quality_eval": run.quality_eval,
        "applied_strategy_ids": run.applied_strategy_ids,
        "strategy_effect": run.strategy_effect,
        "token_summary": run.token_summary,
        "coherence_check": run.coherence_check,
        "structure_repair_applied": any("结构不变量自动修复通过" in ev.title for ev in run.trace),
        "diagnostics": diagnostics,
    }


def write_run_metrics(run: AnalysisRun, base_dir: Path | str | None = None) -> Path:
    root = Path(base_dir) if base_dir is not None else DATA_DIR / "run_metrics"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{run.id}.json"
    metrics = run.run_metrics or build_run_metrics(run)
    path.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
