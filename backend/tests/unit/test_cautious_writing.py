from prompts import narrative_full_prompt
from reporting.writing_contract import (
    build_cautious_gap_queries,
    select_cautious_report_evidence_ids,
    should_use_cautious_writing,
)
from schemas import Evidence, Insight, ObjectProfile, SourceTier, Verdict


def _insight(verdict: Verdict, confidence: float) -> Insight:
    return Insight(
        section="行业",
        claim="样本结论",
        reasoning="样本数据反映出结构变化，因此需要继续验证长期持续性。",
        falsifiable_condition="后续行业统计与该方向相反",
        verdict=verdict,
        confidence=confidence,
    )


def test_cautious_mode_triggers_for_weak_industry_insights_only():
    weak = [
        _insight(Verdict.SUPPORTED, 0.8),
        _insight(Verdict.QUESTIONABLE, 0.7),
        _insight(Verdict.QUESTIONABLE, 0.6),
    ]
    assert should_use_cautious_writing(weak, is_industry=True) is True
    assert should_use_cautious_writing(weak, is_industry=False) is False


def test_cautious_mode_stays_off_with_two_verified_majority_claims():
    strong = [
        _insight(Verdict.SUPPORTED, 0.8),
        _insight(Verdict.SUPPORTED, 0.75),
        _insight(Verdict.QUESTIONABLE, 0.6),
    ]
    assert should_use_cautious_writing(strong, is_industry=True) is False


def test_cautious_prompt_forbids_filling_three_findings():
    messages = narrative_full_prompt(
        "{}", "证据", "洞察", "风险",
        industry=True,
        weak_evidence_mode=True,
    )
    prompt = "\n".join(message["content"] for message in messages)
    assert "不得凑足更多" in prompt
    assert "审慎模式" in prompt


def test_cautious_gap_queries_are_fixed_and_target_primary_sources():
    profile = ObjectProfile(
        name="中国宠物经济行业",
        kind="industry",
        industry="宠物经济",
    )
    queries = build_cautious_gap_queries(profile, year=2026)
    assert len(queries) == 2
    assert "白皮书" in queries[0]
    assert "gov.cn" in queries[1]


def test_cautious_evidence_handoff_drops_unrelated_feed_and_prefers_primary():
    profile = ObjectProfile(
        name="中国宠物经济行业",
        kind="industry",
        industry="宠物经济",
    )
    pool = {
        1: Evidence(
            content="AI芯片行业季度景气度继续回升，相关公司收入增长。",
            source_title="AI芯片行业观察",
            source_url="https://finance.eastmoney.com/a/1.html",
            tier=SourceTier.THIRD_PARTY,
        ),
        2: Evidence(
            content="本报告分析中国宠物行业规模、结构、消费人群与宠物食品趋势。",
            source_title="2025年中国宠物行业市场报告",
            source_url="https://assets.kpmg.com/pet-report.pdf",
            tier=SourceTier.COMPANY_PR,
        ),
        3: Evidence(
            content="农业农村部进一步规范宠物诊疗行业管理和许可备案。",
            source_title="关于加强动物诊疗管理工作的通知",
            source_url="https://www.gov.cn/zhengce/pet.htm",
            tier=SourceTier.THIRD_PARTY,
        ),
        4: Evidence(
            content="宠物概念股今日上涨，成交额有所增加。",
            source_title="宠物经济概念股走高",
            source_url="https://finance.eastmoney.com/a/2.html",
            tier=SourceTier.THIRD_PARTY,
        ),
    }

    selected = select_cautious_report_evidence_ids(
        pool, [1, 2, 3, 4], profile, max_items=10
    )

    assert selected[:2] == [2, 3]
    assert 1 not in selected
    assert 4 not in selected
