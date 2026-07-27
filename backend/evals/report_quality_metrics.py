"""Deterministic report quality metrics for low-variance A/B evaluation."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from reporting.writing_contract import (
    audit_report_grounding,
    propagate_derived_table_citations,
)
from schemas import AnalysisRun, Verdict


_CITATION_RE = re.compile(r"\[\^(\d{1,3})\]")
_REFERENCE_RE = re.compile(r"^\s*-\s+\*\*\[\^(\d{1,3})\]\*\*", re.MULTILINE)
_H2_RE = re.compile(r"^##\s+(.+?)\s*$", re.MULTILINE)
_H3_RE = re.compile(r"^###\s+(.+?)\s*$", re.MULTILINE)
_GENERIC_TERMS = {
    "分析", "情况", "定位", "结构", "因素", "能力", "风险", "行业",
    "公司", "关键", "核心", "周期", "业务", "市场", "判断",
}


def _body(report: str) -> str:
    return re.split(r"^####\s+参考文献", report or "", maxsplit=1, flags=re.MULTILINE)[0]


def _section_terms(section: str) -> list[str]:
    head = re.split(r"[:：]", section, maxsplit=1)[0]
    chunks = re.split(r"与|及|和|、|/|（|）|\(|\)", head)
    terms: list[str] = []
    if 3 <= len(head) <= 18:
        terms.append(head)
    for chunk in chunks:
        chunk = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", chunk)
        if len(chunk) >= 3 and chunk not in _GENERIC_TERMS:
            terms.append(chunk)
    return list(dict.fromkeys(terms))


def _section_coverage(report_body: str, sections: list[str]) -> tuple[float, list[str]]:
    compact = re.sub(r"\s+", "", report_body)
    missing: list[str] = []
    for section in sections:
        terms = _section_terms(section)
        if not terms or not any(term in compact for term in terms):
            missing.append(section)
    covered = len(sections) - len(missing)
    return (covered / len(sections) if sections else 1.0), missing


def _domains(run: AnalysisRun, cited_ids: set[int]) -> set[str]:
    domains: set[str] = set()
    for idx in cited_ids:
        if not (1 <= idx <= len(run.evidence_pool)):
            continue
        host = (urlparse(run.evidence_pool[idx - 1].source_url or "").hostname or "").lower()
        if host:
            domains.add(host.removeprefix("www."))
    return domains


def _argument_metrics(report_body: str) -> dict:
    core_match = re.search(
        r"^#{2,3}\s+(?:\d+[.、]\s*)?核心发现.*?$([\s\S]*?)(?=^#{2,3}\s+(?:\d+[.、]\s*)?(?:展望|风险)|\Z)",
        report_body,
        re.MULTILINE,
    )
    core = core_match.group(1) if core_match else ""
    starts = list(re.finditer(r"^#{3,4}\s+.+?$", core, re.MULTILINE))
    blocks: list[str] = []
    for pos, match in enumerate(starts):
        end = starts[pos + 1].start() if pos + 1 < len(starts) else len(core)
        blocks.append(core[match.start():end])

    supported = 0
    reasoned = 0
    bounded = 0
    actionable = 0
    for block in blocks:
        supported += bool(_CITATION_RE.search(block))
        reasoned += bool(re.search(r"因此|意味着|原因|机制|导致|驱动|传导|取决于", block))
        bounded += bool(re.search(r"但|反例|边界|不足|缺口|不确定|尚未|无法", block))
        actionable += bool(re.search(r"关注|跟踪|观察|验证|触发|指标", block))
    denominator = max(len(blocks), 1)
    return {
        "core_finding_count": len(blocks),
        "supported_finding_rate": round(supported / denominator, 3),
        "reasoning_chain_rate": round(reasoned / denominator, 3),
        "boundary_rate": round(bounded / denominator, 3),
        "actionability_rate": round(actionable / denominator, 3),
    }


def measure_report(run: AnalysisRun, report: str) -> dict:
    """Measure accuracy, completeness and argument structure without an LLM judge."""
    report_body = _body(report)
    body_citations = {int(value) for value in _CITATION_RE.findall(report_body)}
    mapped_references = {int(value) for value in _REFERENCE_RE.findall(report or "")}
    valid_ids = set(range(1, len(run.evidence_pool) + 1))
    missing_reference_mappings = sorted(body_citations - mapped_references)
    invalid_citations = sorted(body_citations - valid_ids)

    pool = {idx: evidence for idx, evidence in enumerate(run.evidence_pool, 1)}
    grounding = audit_report_grounding(
        propagate_derived_table_citations(report_body),
        pool,
    )

    direct_insight_ids = {
        idx
        for insight in run.insights
        if insight.verdict not in {Verdict.REFUTED, Verdict.UNVERIFIABLE}
        for evidence in insight.evidence
        for idx in [next(
            (
                pool_idx for pool_idx, candidate in pool.items()
                if candidate.id == evidence.id
            ),
            0,
        )]
        if idx
    }
    used_direct_ids = body_citations & direct_insight_ids
    sections = run.profile.sections if run.profile else []
    coverage_rate, missing_sections = _section_coverage(report_body, sections)
    headings = [
        re.sub(r"^\d+(?:\.\d+)*[.、]?\s*", "", match.group(1)).strip()
        for match in re.finditer(r"^#{2,4}\s+(.+?)\s*$", report_body, re.MULTILINE)
    ]
    required_groups = (
        ("执行摘要",),
        ("行业格局", "基本事实"),
        ("核心发现",),
        ("展望",),
        ("风险",),
    )
    structure_rate = sum(
        any(alias in title for title in headings for alias in aliases)
        for aliases in required_groups
    ) / len(required_groups)

    checked = grounding["numeric_lines_checked"]
    failures = (
        len(grounding["uncited_numeric_lines"])
        + len(grounding["numeric_citation_mismatches"])
        + len(grounding["invalid_citation_ids"])
    )
    accuracy_score = (
        grounding["grounding_rate"] * 70
        + (1.0 if not missing_reference_mappings else 0.0) * 15
        + (1.0 if not invalid_citations else 0.0) * 15
    )
    completeness_score = coverage_rate * 70 + structure_rate * 30

    return {
        "length": len(report or ""),
        "body_citation_count": len(body_citations),
        "reference_mapping_count": len(mapped_references),
        "missing_reference_mappings": missing_reference_mappings,
        "invalid_citations": invalid_citations,
        "citation_domain_count": len(_domains(run, body_citations)),
        "direct_insight_evidence_coverage": round(
            len(used_direct_ids) / len(direct_insight_ids), 3
        ) if direct_insight_ids else 1.0,
        "section_coverage_rate": round(coverage_rate, 3),
        "missing_sections": missing_sections,
        "required_structure_rate": round(structure_rate, 3),
        "numeric_failure_rate": round(failures / checked, 3) if checked else 0.0,
        "accuracy_score": round(accuracy_score, 1),
        "completeness_score": round(completeness_score, 1),
        "grounding": grounding,
        "argument": _argument_metrics(report_body),
    }
