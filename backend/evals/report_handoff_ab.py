"""Cheap report-stage A/B test using a completed research run.

Arm A regenerates only the Agent report from saved evidence + verified
insights. Arm B receives the same saved evidence but no Agent reasoning.
No collection, search, analysis, or falsification calls are repeated.

Usage:
    python -m evals.report_handoff_ab RUN_ID [PROVIDER]
"""
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

import prompts
from evals.fact_check import extract_and_verify_facts
from llm.client import LLMClient
from orchestrator import Orchestrator
from evals.report_pairwise_judge import judge_pairwise
from evals.report_quality_metrics import measure_report
from reporting.structure import _normalize_narrative, strip_generated_report_tail
from reporting.writing_contract import (
    audit_report_grounding,
    attach_exact_numeric_citations,
    ensure_dimension_coverage_boundaries,
    neutralize_uncited_tracking_thresholds,
    neutralize_unsupported_coverage_rows,
    neutralize_unsupported_scenarios,
    normalize_grouped_citations,
    normalize_plain_numeric_citations,
    propagate_derived_table_citations,
)
from schemas import Evidence
from store import load_run


def _text(value) -> str:
    if isinstance(value, dict):
        return str(value.get("content") or value.get("markdown") or "")
    return str(value or "")


def _baseline_prompt(run, evidence_digest: str) -> list[dict]:
    sections = run.profile.sections if run.profile else []
    profile = run.profile.model_dump_json() if run.profile else "{}"
    system = (
        "你是资深商业分析师。只根据给定画像与证据撰写中文深度分析报告。"
        "包含执行摘要、基本事实、核心发现、展望和风险；使用[^N]引用，输出Markdown。"
        "不得编造证据中不存在的事实或数字。"
        f"分析维度：{json.dumps(sections, ensure_ascii=False)}"
    )
    user = f"画像：\n{profile}\n\n证据：\n{evidence_digest}\n\n请直接撰写完整报告。"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _salvage_quality_error(exc: Exception) -> dict:
    raw = str(exc)
    dimensions = {
        "完整性", "逻辑性", "专业性", "数据性",
        "创新性", "实用性", "合规性", "可读性",
    }
    scores = {
        name: int(value)
        for name, value in re.findall(r'"([^"]+)"\s*:\s*(\d+)', raw)
        if name in dimensions
    }
    total_match = re.search(r'"total"\s*:\s*(\d+)', raw)
    if len(scores) == len(dimensions) and total_match:
        return {
            "scores": scores,
            "total": int(total_match.group(1)),
            "issues": ["评分解释被截断；维度分和总分已从结构化前缀恢复。"],
            "salvaged": True,
        }
    return {"error": f"{type(exc).__name__}: {raw[:240]}"}


async def _judge(client: LLMClient, report: str, sections: list[str],
                 evidence: list[str], skip_facts: bool = False,
                 skip_quality: bool = False) -> dict:
    reference_lines = [
        line for line in report.splitlines()
        if line.strip().startswith("- **[^")
    ][:30]
    eval_excerpt = report[:6500]
    if reference_lines:
        eval_excerpt += "\n\n【参考文献映射】\n" + "\n".join(reference_lines)
    if skip_quality:
        quality = {"skipped": True}
    else:
        try:
            quality_raw = await asyncio.to_thread(
                client.chat,
                prompts.quality_eval_prompt(eval_excerpt, sections),
                role="reviewer",
                temperature=0.2,
                max_tokens=3000,
            )
            quality_text = _text(quality_raw)
            quality = LLMClient._extract_json(quality_text)
        except Exception as exc:  # noqa: BLE001
            quality = _salvage_quality_error(
                ValueError(quality_text) if "quality_text" in locals() else exc
            )

    async def chat_json(messages, **_kwargs):
        return await asyncio.to_thread(
            client.chat_json,
            messages,
            role="reviewer",
            temperature=0.0,
            max_tokens=2000,
        )

    if skip_facts:
        facts = {"skipped": True}
    else:
        try:
            facts = await extract_and_verify_facts(report[:6500], evidence[:60], chat_json)
        except Exception as exc:  # noqa: BLE001
            facts = {"error": f"{type(exc).__name__}: {str(exc)[:240]}"}
    return {
        "quality": quality if isinstance(quality, dict) else {},
        "facts": facts if isinstance(facts, dict) else {},
    }


async def main(run_id: str, provider: str = "deepseek-v4-pro",
               reuse_baseline: bool = False, skip_facts: bool = False,
               pairwise_repeats: int = 0, skip_quality: bool = False) -> dict:
    source_run = load_run(run_id)
    if source_run is None:
        raise SystemExit(f"run not found: {run_id}")

    # Work on an in-memory copy so the source run remains authoritative.
    run = source_run.model_copy(deep=True)
    run.provider = provider
    run.reviewer_provider = provider
    run.red_team_provider = provider
    run.narrative_md = ""
    run.quality_eval = {}
    run.trace = []

    orch = Orchestrator(run)
    orch.provider = provider
    orch.reviewer_provider = provider
    orch.red_team_provider = provider
    orch.evidence_pool = {idx: ev for idx, ev in enumerate(run.evidence_pool, 1)}
    orch._ev_counter = len(orch.evidence_pool)
    orch._build_evidence_index()

    t0 = time.perf_counter()
    async for _ in orch._report():
        pass
    elapsed_a = time.perf_counter() - t0
    report_a = run.narrative_md
    report_tokens = orch.llm.token_tracker.summary()

    evidence_digest = orch._digest(
        ids=list(orch.evidence_pool)[:60],
        max_items=60,
        content_len=220,
    )
    out_dir = BACKEND / "data" / "eval_comparisons" / "report_handoff_ab"
    out_dir.mkdir(parents=True, exist_ok=True)
    baseline_path = out_dir / f"{run_id}_baseline.md"
    if reuse_baseline and baseline_path.exists():
        report_b = baseline_path.read_text(encoding="utf-8")
        elapsed_b = 0.0
        baseline_tokens = {"reused": True}
    else:
        baseline_client = LLMClient()
        baseline_client.set_stage("baseline_report")
        t0 = time.perf_counter()
        report_b = _text(await asyncio.to_thread(
            baseline_client.chat,
            _baseline_prompt(run, evidence_digest),
            provider=provider,
            role="reviewer",
            temperature=0.4,
            max_tokens=8192,
            timeout=480.0,
            retries=1,
        ))
        elapsed_b = time.perf_counter() - t0
        baseline_tokens = baseline_client.token_tracker.summary()

    (out_dir / f"{run_id}_agent.md").write_text(report_a, encoding="utf-8")
    baseline_path.write_text(report_b, encoding="utf-8")

    sections = run.profile.sections if run.profile else []
    evidence_text = [ev.content for ev in run.evidence_pool if ev.content]
    judge_client = LLMClient()
    judge_a, judge_b = await asyncio.gather(
        _judge(judge_client, report_a, sections, evidence_text, skip_facts, skip_quality),
        _judge(judge_client, report_b, sections, evidence_text, skip_facts, skip_quality),
    )

    pool = {idx: ev for idx, ev in enumerate(run.evidence_pool, 1)}
    deterministic = {
        "agent_report": measure_report(run, report_a),
        "model_evidence": measure_report(run, report_b),
    }
    pairwise = (
        await judge_pairwise(run, report_a, report_b, provider, pairwise_repeats)
        if pairwise_repeats > 0 else None
    )
    result = {
        "source_run_id": run_id,
        "query": run.query,
        "provider": provider,
        "elapsed_seconds": {"agent_report": round(elapsed_a, 2), "model_evidence": round(elapsed_b, 2)},
        "tokens": {"agent_report": report_tokens, "model_evidence": baseline_tokens},
        "length": {"agent_report": len(report_a), "model_evidence": len(report_b)},
        "quality": {"agent_report": judge_a, "model_evidence": judge_b},
        "grounding": {
            "agent_report": audit_report_grounding(report_a, pool),
            "model_evidence": audit_report_grounding(report_b, pool),
        },
        "deterministic": deterministic,
        "pairwise": pairwise,
        "report_added_evidence": [
            item.model_dump(mode="json")
            for item in run.evidence_pool[len(source_run.evidence_pool):]
        ],
    }
    (out_dir / f"{run_id}_result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return result


def reprocess_existing_agent(run_id: str) -> dict:
    """Reapply the current deterministic production safeguards to saved Arm A."""
    source = load_run(run_id)
    if source is None:
        raise SystemExit(f"run not found: {run_id}")
    out_dir = BACKEND / "data" / "eval_comparisons" / "report_handoff_ab"
    report_path = out_dir / f"{run_id}_agent.md"
    result_path = out_dir / f"{run_id}_result.json"
    result = json.loads(result_path.read_text(encoding="utf-8"))
    added = [
        Evidence.model_validate(item)
        for item in result.get("report_added_evidence", [])
    ]
    combined_evidence = [*source.evidence_pool, *added]
    body = _normalize_narrative(
        strip_generated_report_tail(report_path.read_text(encoding="utf-8"))
    )
    pool = {idx: ev for idx, ev in enumerate(combined_evidence, 1)}
    body = normalize_plain_numeric_citations(body, set(pool))
    body = normalize_grouped_citations(body)
    body, _ = ensure_dimension_coverage_boundaries(
        body, source.profile.sections if source.profile else []
    )
    body = neutralize_uncited_tracking_thresholds(body)
    body = propagate_derived_table_citations(body)
    body = attach_exact_numeric_citations(body, pool)
    audit = audit_report_grounding(body, pool)
    body = neutralize_unsupported_coverage_rows(body, audit)
    audit = audit_report_grounding(body, pool)
    body = neutralize_unsupported_scenarios(body, audit)
    audit = audit_report_grounding(body, pool)

    run = source.model_copy(deep=True)
    run.evidence_pool = combined_evidence
    orch = Orchestrator(run)
    orch.evidence_pool = pool
    orch._ev_counter = len(pool)
    orch._build_evidence_index()
    from datetime import date
    footer = (
        f"\n\n---\n\n数据截至 {source.data_as_of or '未明确'} · "
        f"报告生成 {date.today().isoformat()} · "
        f"数据源: {', '.join(source.data_sources_used) or '未配置'}\n\n"
        "本报告基于公开信息，不构成投资建议。"
    )
    report = body + "\n\n" + orch._build_appendix_md(body) + footer
    report_path.write_text(report, encoding="utf-8")

    result["length"]["agent_report"] = len(report)
    result["grounding"]["agent_report"] = audit
    result["deterministic"]["agent_report"] = measure_report(run, report)
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=True, indent=2))
    return result


async def rejudge_existing_reports(
    run_id: str,
    provider: str = "deepseek-v4-pro",
    repeats: int = 3,
) -> dict:
    """Blindly rejudge the saved Agent and same-evidence baseline reports."""
    source = load_run(run_id)
    if source is None:
        raise SystemExit(f"run not found: {run_id}")
    out_dir = BACKEND / "data" / "eval_comparisons" / "report_handoff_ab"
    result_path = out_dir / f"{run_id}_result.json"
    report_a = (out_dir / f"{run_id}_agent.md").read_text(encoding="utf-8")
    report_b = (out_dir / f"{run_id}_baseline.md").read_text(encoding="utf-8")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    added = [
        Evidence.model_validate(item)
        for item in result.get("report_added_evidence", [])
    ]
    combined = source.model_copy(deep=True)
    combined.evidence_pool = [*source.evidence_pool, *added]
    pairwise = await judge_pairwise(
        combined, report_a, report_b, provider, repeats
    )
    result["pairwise"] = pairwise
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (out_dir / f"{run_id}_pairwise.json").write_text(
        json.dumps(pairwise, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(pairwise, ensure_ascii=True, indent=2))
    return pairwise


if __name__ == "__main__":
    selected_run = sys.argv[1] if len(sys.argv) > 1 else "5532bb9046ff"
    selected_provider = sys.argv[2] if len(sys.argv) > 2 else "deepseek-v4-pro"
    reuse = "--reuse-baseline" in sys.argv[3:]
    skip_fact_check = "--skip-facts" in sys.argv[3:]
    skip_quality = "--skip-quality" in sys.argv[3:]
    pairwise_arg = next(
        (arg for arg in sys.argv[3:] if arg.startswith("--pairwise-repeats=")),
        "--pairwise-repeats=0",
    )
    pairwise_repeats = int(pairwise_arg.split("=", 1)[1])
    if "--reprocess-existing-agent" in sys.argv[3:]:
        reprocess_existing_agent(selected_run)
    elif "--rejudge-existing" in sys.argv[3:]:
        asyncio.run(rejudge_existing_reports(
            selected_run,
            selected_provider,
            pairwise_repeats or 3,
        ))
    else:
        asyncio.run(main(
            selected_run,
            selected_provider,
            reuse,
            skip_fact_check,
            pairwise_repeats,
            skip_quality,
        ))
