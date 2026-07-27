"""Blind, evidence-aware pairwise judging with order reversal and vote aggregation."""
from __future__ import annotations

import asyncio
import json
import re
from collections import Counter

from llm.client import LLMClient
from schemas import AnalysisRun, Verdict


DIMENSIONS = ("准确性", "洞察深度", "论证有效性", "完整度", "实用性", "可读性")


def _evidence_ledger(run: AnalysisRun, reports: tuple[str, str], max_items: int = 45) -> str:
    cited: list[int] = []
    for report in reports:
        for raw in re.findall(r"\[\^(\d{1,3})\]", report or ""):
            idx = int(raw)
            if idx not in cited:
                cited.append(idx)
    direct: list[int] = []
    id_to_idx = {evidence.id: idx for idx, evidence in enumerate(run.evidence_pool, 1)}
    for insight in run.insights:
        if insight.verdict in {Verdict.REFUTED, Verdict.UNVERIFIABLE}:
            continue
        for evidence in insight.evidence:
            idx = id_to_idx.get(evidence.id, 0)
            if idx and idx not in direct:
                direct.append(idx)
    selected = list(dict.fromkeys([*cited, *direct, *range(1, len(run.evidence_pool) + 1)]))
    lines: list[str] = []
    for idx in selected[:max_items]:
        evidence = run.evidence_pool[idx - 1]
        content = re.sub(r"\s+", " ", evidence.content or "").strip()
        lines.append(
            f"[{idx}] {evidence.tier_label}｜{evidence.source_title[:70]}｜{content[:480]}"
        )
    return "\n".join(lines)


def _prompt(run: AnalysisRun, first: str, second: str, ledger: str) -> list[dict]:
    sections = run.profile.sections if run.profile else []
    system = """你是严格的商业分析报告成对评审员。报告身份已盲化。
只比较报告A与报告B，不使用自身知识补充事实，只以给定证据账本为事实边界。

判断原则：
- 准确性：引用编号存在且证据内容支持表述；无依据的精确数字、阈值、概率、因果归因明显扣分。
- 洞察深度：奖励从事实到机制、商业含义和反例边界的增量推理，不奖励篇幅和术语堆砌。
- 论证有效性：主张、证据、机制、结论衔接紧密，反证或数据边界处理合理。
- 完整度：覆盖指定分析维度；若账本没有关键数据，明确说明边界优于编造填充，不能因诚实缺口而扣分。
- 实用性：有证据支持的跟踪指标和决策含义；无依据的数字阈值不算实用。
- 可读性：主线清楚、信息密度高、无重复，不能单纯奖励更长报告。

每个维度只能输出 A、B 或 TIE。不要输出理由、分析过程、JSON或Markdown。
响应必须只有一行，严格使用指定格式。"""
    user = f"""分析对象：{run.query}
应覆盖维度：{json.dumps(sections, ensure_ascii=False)}

【证据账本】
{ledger}

【报告A】
{first[:7500]}

【报告B】
{second[:7500]}

输出格式：
准确性=A;洞察深度=B;论证有效性=TIE;完整度=A;实用性=B;可读性=A;总体=A"""
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _parse_compact_votes(text: str) -> tuple[dict[str, str], str]:
    winners: dict[str, str] = {}
    for dimension in DIMENSIONS:
        matches = re.findall(
            rf"{re.escape(dimension)}\s*[=:：]\s*(A|B|TIE)",
            text or "",
            re.IGNORECASE,
        )
        if matches:
            winners[dimension] = matches[-1].upper()
    overall_matches = re.findall(
        r"(?:总体|overall)\s*[=:：]\s*(A|B|TIE)",
        text or "",
        re.IGNORECASE,
    )
    if len(winners) != len(DIMENSIONS) or not overall_matches:
        raise ValueError(f"无法解析成对评审票: {(text or '')[-500:]}")
    return winners, overall_matches[-1].upper()


async def judge_pairwise(
    run: AnalysisRun,
    agent_report: str,
    baseline_report: str,
    provider: str,
    repeats: int = 3,
) -> dict:
    """Judge the same pair repeatedly while reversing display order."""
    ledger = _evidence_ledger(run, (agent_report, baseline_report))
    client = LLMClient()
    rounds: list[dict] = []
    for round_idx in range(max(1, repeats)):
        swapped = round_idx % 2 == 1
        first, second = (
            (baseline_report, agent_report) if swapped
            else (agent_report, baseline_report)
        )
        raw = await asyncio.to_thread(
            client.chat,
            _prompt(run, first, second, ledger),
            provider=provider,
            role="reviewer",
            temperature=0.1,
            max_tokens=6000,
            timeout=180.0,
            retries=1,
        )
        text = raw if isinstance(raw, str) else str(
            raw.get("content") or raw.get("markdown") or ""
        )
        winners, overall_raw = _parse_compact_votes(text)
        normalized: dict[str, str] = {}
        for dimension in DIMENSIONS:
            winner = str(winners.get(dimension) or "TIE").upper()
            if swapped:
                winner = {"A": "model_evidence", "B": "agent", "TIE": "tie"}.get(
                    winner, "tie"
                )
            else:
                winner = {"A": "agent", "B": "model_evidence", "TIE": "tie"}.get(
                    winner, "tie"
                )
            normalized[dimension] = winner
        if swapped:
            overall = {"A": "model_evidence", "B": "agent", "TIE": "tie"}.get(
                overall_raw, "tie"
            )
        else:
            overall = {"A": "agent", "B": "model_evidence", "TIE": "tie"}.get(
                overall_raw, "tie"
            )
        rounds.append({
            "round": round_idx + 1,
            "swapped": swapped,
            "winners": normalized,
            "overall": overall,
        })

    aggregate: dict[str, dict] = {}
    for dimension in DIMENSIONS:
        votes = Counter(round_result["winners"][dimension] for round_result in rounds)
        top_count = max(votes.values())
        leaders = [label for label, count in votes.items() if count == top_count]
        aggregate[dimension] = {
            "winner": leaders[0] if len(leaders) == 1 else "tie",
            "votes": dict(votes),
        }
    overall_votes = Counter(round_result["overall"] for round_result in rounds)
    top_count = max(overall_votes.values())
    overall_leaders = [
        label for label, count in overall_votes.items() if count == top_count
    ]
    return {
        "repeats": len(rounds),
        "dimensions": aggregate,
        "overall": {
            "winner": overall_leaders[0] if len(overall_leaders) == 1 else "tie",
            "votes": dict(overall_votes),
        },
        "rounds": rounds,
    }
