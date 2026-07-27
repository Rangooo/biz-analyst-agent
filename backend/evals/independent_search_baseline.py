"""Arm C: a model plans its own web searches and writes from its own evidence."""
from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

BACKEND = Path(__file__).resolve().parents[1]
load_dotenv(BACKEND.parent / ".env")

from evals.report_pairwise_judge import judge_pairwise
from evals.report_quality_metrics import measure_report
from llm.client import LLMClient
from reporting.structure import _normalize_narrative, strip_generated_report_tail
from reporting.writing_contract import (
    audit_report_grounding,
    attach_exact_numeric_citations,
    ensure_dimension_coverage_boundaries,
    neutralize_uncited_tracking_thresholds,
    neutralize_unsupported_coverage_rows,
    normalize_grouped_citations,
    normalize_plain_numeric_citations,
    propagate_derived_table_citations,
)
from schemas import AnalysisRun, Evidence
from store import load_run
from tools.finance_sources import get_adapter


def _text(value) -> str:
    if isinstance(value, dict):
        return str(value.get("content") or value.get("markdown") or "")
    return str(value or "")


def _query_prompt(run: AnalysisRun) -> list[dict]:
    sections = run.profile.sections if run.profile else []
    return [
        {
            "role": "system",
            "content": (
                "你是独立研究员。只根据研究对象和分析维度规划联网搜索，"
                "不得使用或假设任何既有Agent证据。只输出JSON。"
            ),
        },
        {
            "role": "user",
            "content": f"""研究对象：{run.query}
分析维度：{json.dumps(sections, ensure_ascii=False)}
规划最多6条互不重复的搜索查询，兼顾官方数据、财务/经营、竞争、风险和反面证据。
输出：{{"queries":["查询1","查询2"]}}""",
        },
    ]


def _writer_prompt(run: AnalysisRun, digest: str) -> list[dict]:
    sections = run.profile.sections if run.profile else []
    kind = run.profile.kind if run.profile else "company"
    facts_title = "行业格局" if kind == "industry" else "基本事实"
    system = """你是资深商业分析师。只根据你自行搜索得到的证据撰写中文深度报告。
不得使用外部记忆补充事实。每个精确数字必须在同一句或同一表格行引用证据编号[^N]。
无证据时明确写数据边界，禁止虚构阈值、概率、预测或影响金额。
输出Markdown正文，不写参考文献，系统会自动生成。"""
    user = f"""研究对象：{run.query}
分析维度：{json.dumps(sections, ensure_ascii=False)}

【自行搜索证据】
{digest}

必须依次包含：
## 执行摘要
## {facts_title}
### 分析维度覆盖
用表格逐项原样列出全部分析维度、已验证结论或证据边界、关键证据。
## 核心发现
只选3条最重要发现，完成“主张→证据→机制→商业含义→反例/边界”。
## 展望与关注点
用最多6行跟踪表；观察信号不得自行设定数字阈值。
## 风险与不确定性

正文目标3500-5500字；行业报告可到8000字。只输出Markdown正文。"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


async def _search_queries(queries: list[str]) -> list[Evidence]:
    adapter = get_adapter("exa_search")
    if not adapter or not adapter.available:
        raise RuntimeError("Exa search backend unavailable")
    semaphore = asyncio.Semaphore(3)

    async def one(query: str):
        async with semaphore:
            return await asyncio.to_thread(
                adapter.search, query, "general", 4, 730
            )

    results = await asyncio.gather(*(one(query) for query in queries[:6]))
    evidence: list[Evidence] = []
    seen: set[str] = set()
    for result in results:
        for item in result.evidences:
            key = (item.source_url or "").strip().lower() or item.content[:120]
            if not key or key in seen:
                continue
            seen.add(key)
            evidence.append(item)
    return evidence[:24]


def _append_references(body: str, evidence: list[Evidence], offset: int) -> str:
    cited = {
        int(value) for value in re.findall(r"\[\^(\d{1,3})\]", body)
        if offset < int(value) <= offset + len(evidence)
    }
    lines = ["---", "", f"#### 参考文献（自行搜索引用 {len(cited)} 条）", ""]
    for idx in sorted(cited):
        item = evidence[idx - offset - 1]
        lines.append(
            f"- **[^{idx}]** {item.source_title or '网页资料'} "
            f"{item.source_url or '链接未提供'}"
        )
    return body.rstrip() + "\n\n" + "\n".join(lines)


def _mark_disallowed_citations(body: str, allowed_ids: set[int]) -> str:
    return re.sub(
        r"\[\^(\d{1,3})\]",
        lambda match: (
            match.group(0)
            if int(match.group(1)) in allowed_ids else "[UNSOURCED]"
        ),
        body or "",
    )


def _shift_citations(
    report: str,
    *,
    first_id: int,
    last_id: int,
    delta: int,
) -> str:
    """Shift a bounded citation namespace for a combined blind-judge ledger."""
    if delta <= 0:
        return report
    return re.sub(
        r"\[\^(\d{1,3})\]",
        lambda match: (
            f"[^{int(match.group(1)) + delta}]"
            if first_id <= int(match.group(1)) <= last_id
            else match.group(0)
        ),
        report or "",
    )


async def main(
    run_id: str,
    provider: str = "deepseek-v4-pro",
    pairwise_repeats: int = 0,
) -> dict:
    source = load_run(run_id)
    if source is None:
        raise SystemExit(f"run not found: {run_id}")
    client = LLMClient()
    client.set_stage("independent_search_baseline")

    started = time.perf_counter()
    plan_raw = await asyncio.to_thread(
        client.chat,
        _query_prompt(source),
        provider=provider,
        role="reviewer",
        temperature=0.2,
        max_tokens=1200,
    )
    try:
        plan = LLMClient._extract_json(_text(plan_raw))
    except Exception:  # noqa: BLE001
        plan = {}
    queries = [
        str(value).strip()
        for value in (plan.get("queries", []) if isinstance(plan, dict) else [])
        if str(value).strip()
    ][:6]
    if not queries:
        sections = source.profile.sections if source.profile else []
        if sections:
            positions = [
                round(idx * (len(sections) - 1) / min(5, len(sections) - 1))
                for idx in range(min(6, len(sections)))
            ] if len(sections) > 1 else [0]
            queries = [
                f"{source.query} {sections[pos]} 最新 数据 官方 报告 反面证据"
                for pos in dict.fromkeys(positions)
            ]
        else:
            queries = [f"{source.query} 最新 经营 财务 竞争 风险"]

    search_started = time.perf_counter()
    searched = await _search_queries(queries)
    search_seconds = time.perf_counter() - search_started
    offset = len(source.evidence_pool)
    digest = "\n".join(
        f"[{offset + idx}] {item.source_title}｜"
        f"{re.sub(r'\\s+', ' ', item.content or '').strip()[:420]}｜{item.source_url}"
        for idx, item in enumerate(searched, 1)
    )

    max_tokens = 6800 if source.profile and source.profile.kind == "industry" else 4200
    raw = await asyncio.to_thread(
        client.chat,
        _writer_prompt(source, digest),
        provider=provider,
        role="reviewer",
        temperature=0.4,
        max_tokens=max_tokens,
        timeout=480.0,
        retries=1,
    )
    body = _normalize_narrative(_text(raw))
    body = normalize_plain_numeric_citations(
        body,
        set(range(offset + 1, offset + len(searched) + 1)),
    )
    body = normalize_grouped_citations(body)
    body = _mark_disallowed_citations(
        body, set(range(offset + 1, offset + len(searched) + 1))
    )
    body, _ = ensure_dimension_coverage_boundaries(
        body, source.profile.sections if source.profile else []
    )
    body = neutralize_uncited_tracking_thresholds(body)
    body = propagate_derived_table_citations(body)

    combined = source.model_copy(deep=True)
    combined.evidence_pool = [*source.evidence_pool, *searched]
    own_pool = {
        offset + idx: item for idx, item in enumerate(searched, 1)
    }
    body = attach_exact_numeric_citations(body, own_pool)
    body = neutralize_unsupported_coverage_rows(
        body, audit_report_grounding(body, own_pool)
    )
    report = _append_references(body, searched, offset)
    elapsed = time.perf_counter() - started

    out_dir = BACKEND / "data" / "eval_comparisons" / "report_handoff_ab"
    out_dir.mkdir(parents=True, exist_ok=True)
    report_path = out_dir / f"{run_id}_independent.md"
    report_path.write_text(report, encoding="utf-8")
    agent_report = (out_dir / f"{run_id}_agent.md").read_text(encoding="utf-8")
    pairwise = (
        await judge_pairwise(combined, agent_report, report, provider, pairwise_repeats)
        if pairwise_repeats > 0 else None
    )
    result = {
        "source_run_id": run_id,
        "query": source.query,
        "provider": provider,
        "queries": queries,
        "searched_evidence_count": len(searched),
        "elapsed_seconds": round(elapsed, 2),
        "search_seconds": round(search_seconds, 2),
        "tokens": client.token_tracker.summary(),
        "length": len(report),
        "deterministic": {
            "agent_report": measure_report(combined, agent_report),
            "model_independent_search": measure_report(combined, report),
        },
        "pairwise": pairwise,
        "searched_evidence": [item.model_dump(mode="json") for item in searched],
    }
    (out_dir / f"{run_id}_independent_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return result


def reprocess_existing(run_id: str) -> dict:
    """Reapply deterministic citation governance without repeating search or LLM calls."""
    source = load_run(run_id)
    if source is None:
        raise SystemExit(f"run not found: {run_id}")
    out_dir = BACKEND / "data" / "eval_comparisons" / "report_handoff_ab"
    result_path = out_dir / f"{run_id}_independent_result.json"
    report_path = out_dir / f"{run_id}_independent.md"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    searched = [
        Evidence.model_validate(item)
        for item in result.get("searched_evidence", [])
    ]
    offset = len(source.evidence_pool)
    body = _normalize_narrative(
        strip_generated_report_tail(report_path.read_text(encoding="utf-8"))
    )
    body = normalize_plain_numeric_citations(
        body, set(range(offset + 1, offset + len(searched) + 1))
    )
    body = normalize_grouped_citations(body)
    body = _mark_disallowed_citations(
        body, set(range(offset + 1, offset + len(searched) + 1))
    )
    body, _ = ensure_dimension_coverage_boundaries(
        body, source.profile.sections if source.profile else []
    )
    body = neutralize_uncited_tracking_thresholds(body)
    body = propagate_derived_table_citations(body)
    combined = source.model_copy(deep=True)
    combined.evidence_pool = [*source.evidence_pool, *searched]
    own_pool = {
        offset + idx: item for idx, item in enumerate(searched, 1)
    }
    body = attach_exact_numeric_citations(body, own_pool)
    body = neutralize_unsupported_coverage_rows(
        body, audit_report_grounding(body, own_pool)
    )
    report = _append_references(body, searched, offset)
    report_path.write_text(report, encoding="utf-8")
    agent_report = (out_dir / f"{run_id}_agent.md").read_text(encoding="utf-8")
    result["length"] = len(report)
    result["deterministic"] = {
        "agent_report": measure_report(combined, agent_report),
        "model_independent_search": measure_report(combined, report),
    }
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return result


async def rejudge_existing(
    run_id: str,
    provider: str = "deepseek-v4-pro",
    repeats: int = 3,
) -> dict:
    """Blindly rejudge saved Agent and independent-search reports."""
    source = load_run(run_id)
    if source is None:
        raise SystemExit(f"run not found: {run_id}")
    out_dir = BACKEND / "data" / "eval_comparisons" / "report_handoff_ab"
    result_path = out_dir / f"{run_id}_independent_result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    searched = [
        Evidence.model_validate(item)
        for item in result.get("searched_evidence", [])
    ]
    ab_result_path = out_dir / f"{run_id}_result.json"
    ab_result = (
        json.loads(ab_result_path.read_text(encoding="utf-8"))
        if ab_result_path.exists() else {}
    )
    added = [
        Evidence.model_validate(item)
        for item in ab_result.get("report_added_evidence", [])
    ]
    combined = source.model_copy(deep=True)
    combined.evidence_pool = [*source.evidence_pool, *added, *searched]
    agent_report = (out_dir / f"{run_id}_agent.md").read_text(encoding="utf-8")
    independent_report = (
        out_dir / f"{run_id}_independent.md"
    ).read_text(encoding="utf-8")
    independent_report = _shift_citations(
        independent_report,
        first_id=len(source.evidence_pool) + 1,
        last_id=len(source.evidence_pool) + len(searched),
        delta=len(added),
    )
    pairwise = await judge_pairwise(
        combined,
        agent_report,
        independent_report,
        provider,
        repeats,
    )
    result["pairwise"] = pairwise
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    (out_dir / f"{run_id}_independent_pairwise.json").write_text(
        json.dumps(pairwise, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(pairwise, ensure_ascii=True, indent=2))
    return pairwise


if __name__ == "__main__":
    selected_run = sys.argv[1]
    selected_provider = sys.argv[2] if len(sys.argv) > 2 else "deepseek-v4-pro"
    pairwise_arg = next(
        (arg for arg in sys.argv[3:] if arg.startswith("--pairwise-repeats=")),
        "--pairwise-repeats=0",
    )
    if "--reprocess-existing" in sys.argv[3:]:
        reprocess_existing(selected_run)
    elif "--rejudge-existing" in sys.argv[3:]:
        asyncio.run(rejudge_existing(
            selected_run,
            selected_provider,
            int(pairwise_arg.split("=", 1)[1]) or 3,
        ))
    else:
        asyncio.run(main(
            selected_run,
            selected_provider,
            int(pairwise_arg.split("=", 1)[1]),
        ))
