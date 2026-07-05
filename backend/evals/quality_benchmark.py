"""分析质量基准集 —— 验证 agent 的分析结论正确性，不只是流程跑通。

与 golden_regression.py 的区别：
- golden_regression: 验证流程机制（有没有降级、有没有触发证伪、报告有没有生成）
- quality_benchmark: 验证分析质量（verdict 方向对不对、关键事实有没有出现、置信度合不合理）

10 个"已知答案"案例，从"能跑"到"跑对"。
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from schemas import AnalysisRun, Verdict
from orchestrator import Orchestrator

_passed = 0
_failed = 0


def check(name: str, condition: bool, detail: str = ""):
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  ✅ {name}")
    else:
        _failed += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))


async def run_case(query: str) -> AnalysisRun:
    """跑一个案例，返回最终的 AnalysisRun。"""
    run = AnalysisRun(query=query, provider="deepseek")
    orch = Orchestrator(run)
    async for _ in orch.run_pipeline():
        pass
    return run


async def main():
    global _passed, _failed

    print("=== 分析质量基准集 ===\n")

    # ---- 案例 1-6: 奇富科技（demo 模式，脚本化数据）----
    print("[案例1] 奇富科技 — 利润增长驱动因素")
    run = await run_case("奇富科技")
    check("流程完成", run.status == "done")
    # 利润增长由规模驱动 → 应该是 supported 或 questionable（不能是 refuted）
    profit_insights = [i for i in run.insights if "利润" in i.claim or "规模" in i.claim or "增长" in i.claim]
    if profit_insights:
        ins = profit_insights[0]
        check("verdict 方向正确（非推翻）", ins.verdict != Verdict.REFUTED,
              f"实际 verdict={ins.verdict.value}")
        check("置信度合理（0.2-0.9）", 0.2 <= ins.confidence <= 0.9,
              f"实际 confidence={ins.confidence}")
    else:
        check("找到利润相关洞察", False, "无利润/规模相关洞察")

    print("\n[案例2] 奇富科技 — 空话论断被红队降级")
    # 新架构：is_falsifiable 不再由分析师标记（全部默认True），由红队通过 verdict 判定
    _shallow = [i for i in run.insights if i.confidence < 0.35]
    check("存在被红队降级的空话论断(低置信≤0.35)", len(_shallow) > 0)

    print("\n[案例3] 奇富科技 — 证伪闭环完整性")
    # 新架构：所有洞察都经过红队检验
    all_falsified = all(len(i.falsifications) >= 1 for i in run.insights)
    check("所有洞察都经过≥1轮证伪", all_falsified)
    if run.insights:
        # 九维挑战结构验证（取第一条有 falsification record 的 insight）
        fr = next((i.falsifications[0] for i in run.insights if i.falsifications), None)
        check("九维挑战结构完整", hasattr(fr, "challenges") and len(fr.challenges) == 9,
              f"challenges 数={len(getattr(fr, 'challenges', []))}")
        check("有 overall_assessment", hasattr(fr, "overall_assessment") and fr.overall_assessment)

    print("\n[案例4] 奇富科技 — 完整文档(narrative)结构完整性")
    check("narrative 已生成", bool(run.narrative_md))
    if run.narrative_md:
        check("含核心洞察或执行摘要", "核心" in run.narrative_md or "摘要" in run.narrative_md or "#" in run.narrative_md)
        check("含免责声明", "不构成投资建议" in run.narrative_md)
        check("含参考文献或证据标注", "参考文献" in run.narrative_md or "[^" in run.narrative_md or "证据" in run.narrative_md)

    print("\n[案例5] 奇富科技 — 完整文档(narrative)生成")
    check("narrative 已生成", bool(run.narrative_md))
    if run.narrative_md:
        check("narrative 含执行摘要或标题", "摘要" in run.narrative_md or "# " in run.narrative_md)
        check("narrative 含数据截至声明", "数据截至" in run.narrative_md or "截至" in run.narrative_md)

    print("\n[案例6] 奇富科技 — 置信度分布合理性")
    # 新架构：所有洞察都参与置信度计算（不再按 is_falsifiable 过滤）
    confs = [i.confidence for i in run.insights]
    if confs:
        check("所有置信度在 [0.1, 0.95]", all(0.1 <= c <= 0.95 for c in confs),
              f"范围: {min(confs):.2f}-{max(confs):.2f}")
        check("置信度有区分度（max-min > 0.1）", max(confs) - min(confs) > 0.1,
              f"范围: {min(confs):.2f}-{max(confs):.2f}")

    # ---- 案例 7: 字节跳动（非上市适配）----
    print("\n[案例7] 字节跳动 — 非上市适配维度")
    run2 = await run_case("字节跳动")
    check("识别为非上市", not run2.profile.is_public)
    sections_str = " ".join(run2.profile.sections or [])
    check("含估值/融资维度", any(k in sections_str for k in ("估值", "融资", "生态位", "商业化")),
          f"sections={run2.profile.sections}")

    # ---- 案例 8: 行业分析 ----
    print("\n[案例8] 中国新能源汽车行业 — 行业维度覆盖")
    run3 = await run_case("中国新能源汽车行业")
    check("识别为行业", run3.profile.kind == "industry")
    sections_str3 = " ".join(run3.profile.sections or [])
    check("含增速/格局/份额维度", any(k in sections_str3 for k in ("增速", "格局", "份额", "集中度", "竞争")),
          f"sections={run3.profile.sections}")

    # ---- 案例 9: 精选洞察质量 ----
    print("\n[案例9] 奇富科技 — 洞察筛选质量")
    # 低置信洞察不应出现在报告核心结论中
    report_core = run.narrative_md
    for uf in _shallow:
        check(f"低置信洞察 '{uf.claim[:20]}' 不在执行摘要", uf.claim[:20] not in report_core[:500])

    # ---- 案例 10: 证据池质量 ----
    print("\n[案例10] 奇富科技 — 证据池质量")
    check("证据池非空", len(run.evidence_pool) > 0)
    if run.evidence_pool:
        tiers = [e.tier for e in run.evidence_pool]
        check("证据含多层级（≥2种 tier）", len(set(tiers)) >= 2,
              f"tier 分布: {sorted(set(tiers))}")
        check("证据含时效信息", any(e.as_of or e.published_at for e in run.evidence_pool))

    # ---- 总结 ----
    print(f"\n=== 结果：{_passed} 通过 / {_failed} 失败 ===")
    if _failed == 0:
        print("✅ 所有质量基准通过")
    else:
        print(f"⚠️ {_failed} 项未通过")

    # ============ P1.5: 已知答案验证 ============
    print("\n=== P1.5: 已知答案验证 ===\n")

    # 已知事实库：对 demo 模式的奇富科技/信贷行业，验证 agent 输出是否包含已知关键事实
    KNOWN_FACTS = {
        "奇富科技": [
            ("净利润约18亿", "2025Q1净利润数据"),
            ("在贷余额", "放款规模数据"),
            ("逾期率", "资产质量数据"),
            ("take rate", "单位经济数据"),
        ],
        "信贷": [
            ("58万亿", "消费信贷余额"),
            ("6.5%", "消费信贷增速"),
            ("24%", "利率上限"),
            ("LPR", "资金成本基准"),
        ],
        "稀土": [
            ("27万吨", "开采配额"),
            ("氧化镨钕", "核心产品"),
            ("北方稀土", "龙头公司"),
        ],
    }

    # 案例 11: 奇富科技已知事实验证
    print("[案例11] 奇富科技 — 已知事实验证")
    _facts_qifu = KNOWN_FACTS["奇富科技"]
    _narrative_qifu = run.narrative_md or ""
    _hits_qifu = 0
    for fact, desc in _facts_qifu:
        _hit = fact in _narrative_qifu or fact in " ".join(str(i.claim) + str(i.reasoning) for i in run.insights)
        check(f"含已知事实 '{fact}' ({desc})", _hit)
        if _hit:
            _hits_qifu += 1
    check("已知事实命中率≥50%", _hits_qifu >= len(_facts_qifu) / 2,
          f"命中 {_hits_qifu}/{len(_facts_qifu)}")

    # 案例 12: 信贷行业已知事实验证
    print("\n[案例12] 中国信贷行业 — 已知事实验证")
    _facts_credit = KNOWN_FACTS["信贷"]
    _narrative_credit = run3.narrative_md or "" if run3 else ""
    _hits_credit = 0
    for fact, desc in _facts_credit:
        _hit = fact in _narrative_credit or fact in " ".join(str(i.claim) + str(i.reasoning) for i in (run3.insights if run3 else []))
        check(f"含已知事实 '{fact}' ({desc})", _hit)
        if _hit:
            _hits_credit += 1
    check("已知事实命中率≥50%", _hits_credit >= len(_facts_credit) / 2,
          f"命中 {_hits_credit}/{len(_facts_credit)}")

    # 案例 13: 报告结构完整性验证
    print("\n[案例13] 奇富科技 — 报告结构完整性")
    _required_sections = ["执行摘要", "核心发现", "风险", "展望"]
    for sec in _required_sections:
        check(f"报告含 '{sec}' 段", sec in _narrative_qifu)
    check("报告含参考文献", "参考文献" in _narrative_qifu or "引用" in _narrative_qifu.lower())
    check("报告含免责声明", "不构成投资建议" in _narrative_qifu)

    # 案例 14: 洞察证伪完整性
    print("\n[案例14] 奇富科技 — 证伪闭环完整性")
    # 新架构：所有洞察都经过红队检验
    if run.insights:
        _all_falsified = all(len(i.falsifications) >= 1 for i in run.insights)
        check("所有洞察都经过≥1轮证伪", _all_falsified)
        _has_challenges = all(
            any(getattr(f, "challenges", None) for f in i.falsifications)
            for i in run.insights if i.falsifications
        )
        check("证伪记录含九维挑战", _has_challenges)
        _verdicts_assigned = all(i.verdict != Verdict.QUESTIONABLE or i.confidence > 0 for i in run.insights)
        check("所有洞察都有明确裁决", _verdicts_assigned)

    print(f"\n=== 最终结果：{_passed} 通过 / {_failed} 失败 ===")
    if _failed == 0:
        print("✅ 所有质量基准通过")
    else:
        print(f"⚠️ {_failed} 项未通过")
    return _failed


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
