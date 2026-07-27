"""三方对比评测：Agent管道 vs 模型基于Agent证据写 vs 模型独立写。

验证 Agent 管道的报告质量是否明显优于"模型直接写作"，
以及改进后的 prompt 框架是否产生结构性差异。

用法：
    python evals/three_way_comparison.py [query]
    # 默认 query: "奇富科技"

输出：三份报告 + 8 维度 LLM 评分对比表 + 事实校验对比
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))

from schemas import AnalysisRun
from orchestrator import Orchestrator
from llm.client import get_client
import prompts


# ─── 配置 ───────────────────────────────────────────
DEFAULT_QUERY = "奇富科技"
MAX_EVIDENCE_CHARS = 8000  # 给 baseline 模型的证据摘要长度上限
JUDGE_TIMEOUT = 60


# ─── Arm A: Agent 完整管道 ──────────────────────────
async def run_agent_pipeline(query: str) -> tuple[str, AnalysisRun, float]:
    """跑完整 Agent 管道，返回 (narrative_md, run, elapsed_seconds)."""
    run = AnalysisRun(query=query, provider="deepseek")
    orch = Orchestrator(run)
    t0 = time.time()
    async for _ in orch.run_pipeline():
        pass
    elapsed = time.time() - t0
    return run.narrative_md or "", run, elapsed


# ─── Arm B: 模型基于 Agent 搜集的证据写 ─────────────
async def run_model_with_evidence(query: str, run: AnalysisRun) -> tuple[str, float]:
    """给模型同样的证据摘要+profile，让它一次性写完整报告。
    这是"模型直接写作"的最佳版本——它看到了 Agent 搜集的全部证据。"""
    client = get_client()

    # 构建证据摘要（模拟 Agent 的 _digest 输出但不做 LLM 压缩）
    evidence_texts = []
    for i, ev in enumerate(run.evidence_pool[:40], 1):
        content = (ev.content or "")[:200]
        tier_label = getattr(ev, "tier_label", ev.tier or "")
        url = ev.source_url or ""
        evidence_texts.append(f"[{i}] ({tier_label}) {content} — {url}")
    evidence_digest = "\n".join(evidence_texts)[:MAX_EVIDENCE_CHARS]

    profile_json = run.profile.model_dump_json() if run.profile else "{}"
    sections = run.profile.sections if run.profile else []

    # 一次性 prompt：给证据+profile，要求写完整报告
    sys_msg = (
        "你是资深商业分析师。根据以下证据和分析对象信息，撰写一份完整的分析报告。"
        "报告应包含：执行摘要、基本事实（含表格）、核心发现（3-5条）、展望与关注点、风险与不确定性。"
        "要求：数据密度高，观点有方向性判断，引用证据编号 [^N]。输出 Markdown。"
        f"\n总字数目标 3000-5000 字。分析维度：{json.dumps(sections, ensure_ascii=False)}"
    )
    user_msg = f"""分析对象：
{profile_json}

可用证据（共 {len(run.evidence_pool)} 条，按溯源等级排列）：
{evidence_digest}

请撰写完整分析报告（执行摘要 + 基本事实/表格 + 核心发现 + 展望 + 风险）。"""

    t0 = time.time()
    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                client.chat,
                messages=[{"role": "system", "content": sys_msg},
                          {"role": "user", "content": user_msg}],
                role="reviewer",
                temperature=0.4,
                max_tokens=8192,
            ),
            timeout=420.0,
        )
        text = resp if isinstance(resp, str) else (resp.get("content") or resp.get("markdown") or str(resp))
    except Exception as e:
        text = f"[生成失败: {e}]"
    elapsed = time.time() - t0
    return text, elapsed


# ─── Arm C: 模型独立写（只给 query）───────────────
async def run_model_alone(query: str) -> tuple[str, float]:
    """只给 query，不给证据，让模型基于自身知识写报告。"""
    client = get_client()

    sys_msg = (
        "你是资深商业分析师。根据你的知识，撰写一份关于以下分析对象的深度分析报告。"
        "报告应包含：执行摘要、基本事实（含表格）、核心发现（3-5条）、展望与关注点、风险与不确定性。"
        "要求：数据密度高，观点有方向性判断。输出 Markdown。总字数目标 3000-5000 字。"
    )
    user_msg = f"请撰写关于「{query}」的完整深度分析报告。"

    t0 = time.time()
    try:
        resp = await asyncio.wait_for(
            asyncio.to_thread(
                client.chat,
                messages=[{"role": "system", "content": sys_msg},
                          {"role": "user", "content": user_msg}],
                role="reviewer",
                temperature=0.4,
                max_tokens=8192,
            ),
            timeout=420.0,
        )
        text = resp if isinstance(resp, str) else (resp.get("content") or resp.get("markdown") or str(resp))
    except Exception as e:
        text = f"[生成失败: {e}]"
    elapsed = time.time() - t0
    return text, elapsed


# ─── 评分：8 维度 + 事实校验 ────────────────────────
async def judge_report(report_md: str, sections: list[str], evidence_texts: list[str],
                       label: str) -> dict:
    """对一份报告做 8 维度评分 + 事实校验。"""
    client = get_client()

    # 8 维度评分
    eval_msgs = prompts.quality_eval_prompt(report_md[:8000], sections)
    try:
        scores_raw = await asyncio.wait_for(
            asyncio.to_thread(
                client.chat_json,
                messages=eval_msgs,
                role="reviewer",
                temperature=0.3,
                max_tokens=2000,
            ),
            timeout=JUDGE_TIMEOUT,
        )
        scores = scores_raw if isinstance(scores_raw, dict) else {}
    except Exception as e:
        scores = {"error": str(e)}

    # 事实校验
    fact_result = {}
    if evidence_texts and len(report_md) > 200:
        try:
            from evals.fact_check import extract_and_verify_facts

            async def _chat_json(messages, **kwargs):
                kwargs.pop("timeout", None)
                kwargs.pop("role", None)
                return await asyncio.wait_for(
                    asyncio.to_thread(
                        client.chat_json,
                        messages=messages,
                        role="reviewer",
                        temperature=0.0,
                        max_tokens=2000,
                    ),
                    timeout=60.0,
                )

            fact_result = await extract_and_verify_facts(
                report_md[:6000], evidence_texts[:30], _chat_json)
        except Exception as e:
            fact_result = {"error": str(e)}

    return {
        "label": label,
        "quality_scores": scores.get("scores", {}),
        "quality_total": scores.get("total", 0),
        "quality_issues": scores.get("issues", []),
        "fact_score": fact_result.get("fact_score"),
        "fact_pass": fact_result.get("fact_pass", 0),
        "fact_total": fact_result.get("fact_total", 0),
        "gaps": fact_result.get("gaps", []),
        "report_length": len(report_md),
    }


# ─── 主流程 ─────────────────────────────────────────
async def main(query: str = DEFAULT_QUERY):
    print(f"╔══════════════════════════════════════════╗")
    print(f"║  三方对比评测: {query}")
    print(f"╚══════════════════════════════════════════╝\n")

    # --- Arm A: Agent 完整管道 ---
    print("▶ [Arm A] 运行 Agent 完整管道...")
    report_a, run, elapsed_a = await run_agent_pipeline(query)
    print(f"  完成 · {len(report_a)} 字符 · {elapsed_a:.1f}s")

    # --- Arm B: 模型基于 Agent 证据写 ---
    print("▶ [Arm B] 模型基于 Agent 搜集的证据直接写...")
    report_b, elapsed_b = await run_model_with_evidence(query, run)
    print(f"  完成 · {len(report_b)} 字符 · {elapsed_b:.1f}s")

    # --- Arm C: 模型独立写 ---
    print("▶ [Arm C] 模型独立写（只给 query，无证据）...")
    report_c, elapsed_c = await run_model_alone(query)
    print(f"  完成 · {len(report_c)} 字符 · {elapsed_c:.1f}s")

    # --- 评分 ---
    print("\n▶ 评分中（8 维度 + 事实校验）...")
    sections = run.profile.sections if run.profile else []
    evidence_texts = [ev.content for ev in run.evidence_pool if ev.content][:30]

    judge_a, judge_b, judge_c = await asyncio.gather(
        judge_report(report_a, sections, evidence_texts, "Agent管道"),
        judge_report(report_b, sections, evidence_texts, "模型+Agent证据"),
        judge_report(report_c, sections, evidence_texts, "模型独立写"),
    )

    # --- 输出对比表 ---
    print("\n" + "=" * 72)
    print("                     三方对比评测结果")
    print("=" * 72)

    # 维度对比
    dims = ["完整性", "逻辑性", "专业性", "数据性", "创新性", "实用性", "合规性", "可读性"]
    print(f"\n{'维度':<8} {'Agent管道':>10} {'模型+证据':>10} {'模型独立':>10}")
    print("-" * 48)
    for d in dims:
        sa = judge_a["quality_scores"].get(d, "?")
        sb = judge_b["quality_scores"].get(d, "?")
        sc = judge_c["quality_scores"].get(d, "?")
        print(f"{d:<8} {str(sa):>10} {str(sb):>10} {str(sc):>10}")

    print("-" * 48)
    print(f"{'总分':<8} {judge_a['quality_total']:>10} {judge_b['quality_total']:>10} {judge_c['quality_total']:>10}")
    print(f"{'事实通过':<8} {judge_a['fact_pass']}/{judge_a['fact_total']:>7} "
          f"{judge_b['fact_pass']}/{judge_b['fact_total']:>7} "
          f"{judge_c['fact_pass']}/{judge_c['fact_total']:>7}")
    print(f"{'字数':<8} {judge_a['report_length']:>10} {judge_b['report_length']:>10} {judge_c['report_length']:>10}")
    print(f"{'耗时':<8} {elapsed_a:>9.1f}s {elapsed_b:>9.1f}s {elapsed_c:>9.1f}s")

    # 问题对比
    print("\n--- Agent管道问题 ---")
    for issue in judge_a.get("quality_issues", [])[:3]:
        print(f"  • {issue}")
    print("\n--- 模型+证据问题 ---")
    for issue in judge_b.get("quality_issues", [])[:3]:
        print(f"  • {issue}")
    print("\n--- 模型独立问题 ---")
    for issue in judge_c.get("quality_issues", [])[:3]:
        print(f"  • {issue}")

    # 保存详细结果
    output_dir = BACKEND_DIR / "data" / "eval_comparisons"
    output_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "query": query,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "elapsed": {"agent": elapsed_a, "model_evidence": elapsed_b, "model_alone": elapsed_c},
        "judgments": {"agent": judge_a, "model_evidence": judge_b, "model_alone": judge_c},
    }
    out_path = output_dir / f"comparison_{query[:10]}_{int(time.time())}.json"
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n详细结果已保存: {out_path}")

    # 保存三份报告
    reports_dir = output_dir / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    (reports_dir / f"A_agent_{ts}.md").write_text(report_a, encoding="utf-8")
    (reports_dir / f"B_model_evidence_{ts}.md").write_text(report_b, encoding="utf-8")
    (reports_dir / f"C_model_alone_{ts}.md").write_text(report_c, encoding="utf-8")
    print(f"三份报告已保存: {reports_dir}/")

    # 判断结论
    total_a = judge_a["quality_total"] or 0
    total_b = judge_b["quality_total"] or 0
    total_c = judge_c["quality_total"] or 0
    print(f"\n{'=' * 72}")
    if total_a > total_b and total_a > total_c:
        margin_b = total_a - total_b
        margin_c = total_a - total_c
        print(f"✅ Agent管道胜出 · 领先模型+证据 {margin_b} 分 · 领先模型独立 {margin_c} 分")
    elif total_b >= total_a:
        print(f"⚠️  模型+证据 ≥ Agent管道 · 需检查 Agent 管道是否真正增值")
    else:
        print(f"⚠️  结果不明确 · A={total_a} B={total_b} C={total_c}")

    return result


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_QUERY
    asyncio.run(main(q))
