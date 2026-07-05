"""Fact-checking evaluation: extract factual claims and verify against evidence.

Given an analysis text and its evidence pool, this module:
1. Extracts 5-8 verifiable factual claims (numbers, dates, rankings, trends)
2. Cross-checks each claim against the evidence pool
3. Returns a structured fact_score dict that plugs into the eval comparison report

This fills the previously-null `fact_score` / `fact_pass` / `fact_total` fields.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# --------------- prompts ---------------

_EXTRACT_CLAIMS_PROMPT = [
    {
        "role": "system",
        "content": (
            "你是事实核查员。从分析报告中提取 5-8 条可验证的事实断言（具体数字、日期、排名、趋势），"
            "每条断言标注它在文中的原始表述。只输出 JSON，不要解释。\n\n"
            "规则：\n"
            "- 只提取包含具体数字/百分比/年份/排名/对比的断言，跳过主观判断和泛泛表述\n"
            "- 如果文中标注了 [估算] [待验证] [UNSOURCED]，也要提取但标记 is_estimated=true\n"
            "- 每条断言 30-80 字，保留原始数字和单位\n\n"
            "输出格式：\n"
            '{"claims": [{"text": "...", "category": "revenue|growth|market_share|user_metric|ranking|other", "is_estimated": false}]}'
        ),
    },
]

_VERIFY_CLAIMS_PROMPT_SYSTEM = (
    "你是事实核查员。逐条验证以下事实断言是否被证据支持。\n\n"
    "对每条断言，判断：\n"
    "- supported: 证据中有明确数据支持（数字吻合或趋势一致）\n"
    "- contradicted: 证据中有与之矛盾的数据\n"
    "- unverifiable: 证据中没有相关信息，无法判断\n"
    "- outdated: 断言引用的数据明显过时（超过1年且有更新数据可用）\n\n"
    "只输出 JSON，不要解释。\n"
    '输出格式：{"verdicts": [{"claim_index": 0, "verdict": "supported|contradicted|unverifiable|outdated", "reason": "...简短理由..."}]}'
)


# --------------- core functions ---------------


async def extract_and_verify_facts(
    text: str,
    evidence_texts: list[str],
    chat_json_fn,
    *,
    max_claims: int = 8,
) -> dict[str, Any]:
    """Extract factual claims from text and verify against evidence.

    Args:
        text: The analysis report text to fact-check
        evidence_texts: List of evidence snippets (content strings)
        chat_json_fn: Async function(messages, **kwargs) -> dict for LLM calls
        max_claims: Max number of claims to extract

    Returns:
        Dict with fact_score, fact_pass, fact_total, claims, verdicts, etc.
    """
    if not text or len(text) < 100:
        return _empty_result("文本过短，无法提取事实断言")

    # Step 1: Extract claims
    extract_messages = _EXTRACT_CLAIMS_PROMPT + [
        {"role": "user", "content": f"分析报告全文（截取前6000字）：\n\n{text[:6000]}"}
    ]
    try:
        extract_result = await chat_json_fn(
            extract_messages, role="reviewer", temperature=0.0, max_tokens=2000, timeout=30
        )
    except Exception as e:
        logger.warning("Fact extraction failed: %s", e)
        return _empty_result(f"事实提取失败: {str(e)[:60]}")

    if not isinstance(extract_result, dict):
        return _empty_result("事实提取返回格式异常")

    claims = extract_result.get("claims", [])
    if not isinstance(claims, list) or not claims:
        return _empty_result("未提取到可验证的事实断言")

    claims = claims[:max_claims]

    # Step 2: Build evidence digest for verification
    evidence_digest = _build_evidence_digest(evidence_texts, max_chars=4000)

    # Step 3: Verify claims
    verify_messages = [
        {"role": "system", "content": _VERIFY_CLAIMS_PROMPT_SYSTEM},
        {
            "role": "user",
            "content": (
                f"## 待验证断言（共 {len(claims)} 条）\n\n"
                + "\n".join(f"[{i}] {c.get('text', '')}" for i, c in enumerate(claims))
                + f"\n\n## 可用证据\n\n{evidence_digest}"
            ),
        },
    ]
    try:
        verify_result = await chat_json_fn(
            verify_messages, role="reviewer", temperature=0.0, max_tokens=2000, timeout=30
        )
    except Exception as e:
        logger.warning("Fact verification failed: %s", e)
        return _partial_result(claims, f"事实验证失败: {str(e)[:60]}")

    if not isinstance(verify_result, dict):
        return _partial_result(claims, "事实验证返回格式异常")

    verdicts = verify_result.get("verdicts", [])
    if not isinstance(verdicts, list):
        verdicts = []

    # Step 4: Compute scores
    return _compute_fact_score(claims, verdicts)


def _build_evidence_digest(evidence_texts: list[str], max_chars: int = 4000) -> str:
    """Build a condensed evidence digest for the verifier."""
    if not evidence_texts:
        return "(无可用证据)"
    parts = []
    total = 0
    for i, ev in enumerate(evidence_texts):
        snippet = ev[:300]
        if total + len(snippet) > max_chars:
            parts.append(f"...（还有 {len(evidence_texts) - i} 条证据未列出）")
            break
        parts.append(f"[证据{i+1}] {snippet}")
        total += len(snippet)
    return "\n".join(parts)


def _compute_fact_score(claims: list[dict], verdicts: list[dict]) -> dict[str, Any]:
    """Compute the aggregate fact score from claims and verdicts."""
    total = len(claims)
    if total == 0:
        return _empty_result("无断言")

    # Map verdicts to claims by index
    verdict_map: dict[int, dict] = {}
    for v in verdicts:
        if isinstance(v, dict) and isinstance(v.get("claim_index"), int):
            verdict_map[v["claim_index"]] = v

    supported = 0
    contradicted = 0
    outdated = 0
    unverifiable = 0
    enriched_claims = []

    for i, claim in enumerate(claims):
        v = verdict_map.get(i, {})
        verdict = v.get("verdict", "unverifiable")
        reason = v.get("reason", "")

        if verdict == "supported":
            supported += 1
        elif verdict == "contradicted":
            contradicted += 1
        elif verdict == "outdated":
            outdated += 1
        else:
            unverifiable += 1

        enriched_claims.append({
            "text": claim.get("text", ""),
            "category": claim.get("category", "other"),
            "is_estimated": claim.get("is_estimated", False),
            "verdict": verdict,
            "reason": reason,
        })

    # Score: supported counts fully, outdated half, contradicted is negative, unverifiable is neutral
    # Scale: 0-100
    if total > 0:
        fact_score = round(
            max(0, min(100, (supported * 100 + outdated * 30 - contradicted * 50) / total)),
            1,
        )
    else:
        fact_score = 0.0

    gaps = []
    if contradicted > 0:
        gaps.append(f"{contradicted} 条断言与证据矛盾")
    if outdated > 0:
        gaps.append(f"{outdated} 条数据可能过时")
    if unverifiable > total * 0.5:
        gaps.append(f"{unverifiable}/{total} 条断言无法从现有证据验证")

    return {
        "fact_score": fact_score,
        "fact_pass": supported,
        "fact_total": total,
        "fact_contradicted": contradicted,
        "fact_outdated": outdated,
        "fact_unverifiable": unverifiable,
        "claims": enriched_claims,
        "gaps": gaps,
        "note": "",
    }


def _empty_result(note: str) -> dict[str, Any]:
    return {
        "fact_score": None,
        "fact_pass": 0,
        "fact_total": 0,
        "fact_contradicted": 0,
        "fact_outdated": 0,
        "fact_unverifiable": 0,
        "claims": [],
        "gaps": [],
        "note": note,
    }


def _partial_result(claims: list[dict], note: str) -> dict[str, Any]:
    return {
        "fact_score": None,
        "fact_pass": 0,
        "fact_total": len(claims),
        "fact_contradicted": 0,
        "fact_outdated": 0,
        "fact_unverifiable": len(claims),
        "claims": [{"text": c.get("text", ""), "category": c.get("category", "other"),
                     "is_estimated": c.get("is_estimated", False),
                     "verdict": "unverifiable", "reason": note} for c in claims],
        "gaps": [note],
        "note": note,
    }


# ===================== 遗漏检测 =====================

_COVERAGE_CHECK_PROMPT = [
    {
        "role": "system",
        "content": (
            "你是分析报告质量审查员。给定分析主题，列出一份『必须覆盖的关键问题』清单，"
            "然后逐条检查报告是否覆盖了每个问题。只输出 JSON，不要解释。\n\n"
            "规则：\n"
            "- 列出 6-10 个该主题分析**必须回答**的关键问题（如营收增速、竞争格局、核心风险等）\n"
            "- 对每个问题判断 covered/partial/missing\n"
            "- covered: 报告中有实质性讨论（不是一句话带过）\n"
            "- partial: 提到了但缺乏具体数据或深入分析\n"
            "- missing: 完全没有涉及\n\n"
            "输出格式：\n"
            '{"questions": [{"question": "...", "status": "covered|partial|missing", "note": "简短说明"}], '
            '"coverage_score": 0.0-1.0, "critical_omissions": ["最严重的遗漏"]}'
        ),
    },
]


async def check_coverage(
    query: str,
    text: str,
    chat_json_fn,
) -> dict[str, Any]:
    """Check if the report covers all critical questions for the topic.

    Returns coverage_score (0-1), questions list, and critical_omissions.
    """
    if not text or len(text) < 200:
        return {"coverage_score": None, "questions": [], "critical_omissions": [], "note": "文本过短"}

    messages = _COVERAGE_CHECK_PROMPT + [
        {"role": "user", "content": f"分析主题：{query}\n\n报告全文（截取前6000字）：\n\n{text[:6000]}"}
    ]
    try:
        result = await chat_json_fn(
            messages, role="reviewer", temperature=0.0, max_tokens=2000, timeout=30
        )
    except Exception as e:
        logger.warning("Coverage check failed: %s", e)
        return {"coverage_score": None, "questions": [], "critical_omissions": [], "note": f"遗漏检测失败: {str(e)[:60]}"}

    if not isinstance(result, dict):
        return {"coverage_score": None, "questions": [], "critical_omissions": [], "note": "返回格式异常"}

    questions = result.get("questions", [])
    if not isinstance(questions, list):
        questions = []

    # Compute coverage_score from verdicts if not provided
    coverage_score = result.get("coverage_score")
    if coverage_score is None and questions:
        covered = sum(1 for q in questions if isinstance(q, dict) and q.get("status") == "covered")
        partial = sum(1 for q in questions if isinstance(q, dict) and q.get("status") == "partial")
        coverage_score = round((covered + partial * 0.5) / max(len(questions), 1), 2)

    critical_omissions = result.get("critical_omissions", [])
    if not isinstance(critical_omissions, list):
        critical_omissions = []

    return {
        "coverage_score": coverage_score,
        "questions": questions,
        "critical_omissions": critical_omissions,
        "note": "",
    }


# ===================== 时效性检查 =====================

_TIMELINESS_CHECK_PROMPT = [
    {
        "role": "system",
        "content": (
            "你是数据时效性审查员。检查分析报告中引用的数据是否在合理时效范围内。只输出 JSON。\n\n"
            "规则：\n"
            "- 提取报告中出现的所有年份/季度/日期引用\n"
            "- 判断数据是否过时（距离当前时间超过 18 个月的核心数据视为过时）\n"
            "- 行业趋势和历史对比允许使用较老数据\n"
            "- 核心财务数据（营收、净利、增速）应为最近 12 个月\n\n"
            "输出格式：\n"
            '{"timeliness_score": 0.0-1.0, "latest_data_period": "报告中最新的数据时间点", '
            '"outdated_items": [{"data": "哪个数据", "period": "数据时间", "severity": "high|medium|low"}], '
            '"note": "时效性总评"}'
        ),
    },
]


async def check_timeliness(
    text: str,
    current_date: str,
    chat_json_fn,
) -> dict[str, Any]:
    """Check if the data cited in the report is reasonably current.

    Returns timeliness_score (0-1), outdated_items, and note.
    """
    if not text or len(text) < 200:
        return {"timeliness_score": None, "outdated_items": [], "latest_data_period": "", "note": "文本过短"}

    messages = _TIMELINESS_CHECK_PROMPT + [
        {"role": "user", "content": f"当前日期：{current_date}\n\n报告全文（截取前5000字）：\n\n{text[:5000]}"}
    ]
    try:
        result = await chat_json_fn(
            messages, role="reviewer", temperature=0.0, max_tokens=1500, timeout=30
        )
    except Exception as e:
        logger.warning("Timeliness check failed: %s", e)
        return {"timeliness_score": None, "outdated_items": [], "latest_data_period": "", "note": f"时效性检查失败: {str(e)[:60]}"}

    if not isinstance(result, dict):
        return {"timeliness_score": None, "outdated_items": [], "latest_data_period": "", "note": "返回格式异常"}

    return {
        "timeliness_score": result.get("timeliness_score"),
        "outdated_items": result.get("outdated_items", []),
        "latest_data_period": result.get("latest_data_period", ""),
        "note": result.get("note", ""),
    }
