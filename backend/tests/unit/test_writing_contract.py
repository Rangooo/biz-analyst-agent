from __future__ import annotations

from reporting.source_governance import sanitize_source_url
from reporting.writing_contract import (
    audit_report_grounding,
    attach_exact_numeric_citations,
    build_writing_contract,
    ensure_dimension_coverage_boundaries,
    neutralize_unsupported_scenarios,
    neutralize_uncited_tracking_thresholds,
    neutralize_unsupported_coverage_rows,
    normalize_grouped_citations,
    normalize_plain_numeric_citations,
    propagate_derived_table_citations,
    select_report_evidence_ids,
)
from schemas import AnalysisRun, Evidence, Insight, ObjectProfile, SourceTier, Verdict


def _run() -> AnalysisRun:
    run = AnalysisRun(query="测试公司")
    run.profile = ObjectProfile(
        name="测试公司",
        kind="company",
        is_public=True,
        ticker="TEST",
        industry="软件",
        business_model="订阅",
        sections=["财务", "竞争"],
    )
    return run


def test_report_evidence_selection_keeps_insight_evidence_first():
    run = _run()
    direct = Evidence(content="ARR为3亿美元", source_url="https://a.example/report", tier=SourceTier.MEDIA)
    authoritative = Evidence(content="监管统计", source_url="https://gov.example/data", tier=SourceTier.FILING)
    semantic = Evidence(content="其他相关材料", source_url="https://b.example/story", tier=SourceTier.MEDIA)
    evidence_pool = {1: direct, 2: authoritative, 3: semantic}
    index = {direct.id: 1, authoritative.id: 2, semantic.id: 3}
    insight = Insight(
        section="财务",
        claim="商业化加速",
        reasoning="ARR增长意味着商业化正在加速，因此需要核查持续性。",
        falsifiable_condition="现有公开ARR序列未增长",
        evidence=[direct],
        verdict=Verdict.SUPPORTED,
        confidence=0.75,
    )

    selected = select_report_evidence_ids(
        evidence_pool, [3], [insight], lambda ev: index.get(ev.id, 0), max_items=3
    )

    assert selected == [1, 3, 2]


def test_writing_contract_exposes_permission_and_data_boundary():
    run = _run()
    evidence = Evidence(
        content="ARR为3亿美元",
        source_url="https://example.com/report",
        tier=SourceTier.MEDIA,
    )
    evidence_pool = {1: evidence}
    insight = Insight(
        section="财务",
        claim="商业化加速",
        reasoning="ARR增长意味着商业化正在加速，因此需要核查收入质量和持续性。",
        falsifiable_condition="现有公开ARR序列未增长",
        evidence=[evidence],
        verdict=Verdict.SUPPORTED,
        confidence=0.75,
    )

    contract = build_writing_contract(
        run, evidence_pool, [insight], [], lambda ev: 1 if ev.id == evidence.id else 0
    )

    assert "主张—证据账本" in contract
    assert "支撑证据=[1]" in contract
    assert "不得虚构阈值、概率或影响金额" in contract
    assert "未形成可靠财务时序" in contract


def test_source_url_sanitizer_removes_tracking_and_spam_payload():
    url = (
        "https://finance.example.com/article?id=42&utm_source=test"
        "&oid=%E8%81%94%E7%B3%BBTG%3A%40spam%E6%92%9E%E5%BA%93"
        "#comments"
    )
    cleaned = sanitize_source_url(url)
    assert cleaned == "https://finance.example.com/article?id=42"


def test_grounding_audit_flags_uncited_and_invalid_numeric_claims():
    evidence = Evidence(content="公司披露ARR为3亿美元，同比增长60%。")
    report = (
        "ARR达到3亿美元，同比增长60%[^1]。\n"
        "估值达到315亿美元。\n"
        "另一指标为20%[^9]。"
    )
    audit = audit_report_grounding(report, {1: evidence})
    assert audit["numeric_lines_checked"] == 3
    assert audit["numeric_lines_grounded"] == 1
    assert len(audit["uncited_numeric_lines"]) == 1
    assert audit["invalid_citation_ids"] == [9]
    assert audit["passed"] is False


def test_neutralizes_unsupported_scenario_precision_and_model_names_are_not_numbers():
    report = (
        "- **乐观情景**：K3延续K2.5成功，ARR突破10亿美元，毛利率达到50%。\n"
        "- K2.5模型继续迭代。\n"
    )
    audit = audit_report_grounding(report, {})
    cleaned = neutralize_unsupported_scenarios(report, audit)
    assert "10亿美元" not in cleaned
    assert "50%" not in cleaned
    assert "公开证据不足以设定精确目标或概率" in cleaned
    assert "K2.5模型继续迭代" in cleaned


def test_grounding_accepts_whitespace_rounding_and_million_to_yi_conversion():
    evidence = Evidence(content="（百万元）2022年营业收入 4,617；增长率748.4 %")
    report = "| 营收(亿元) | 46.17[^1] |\n| 增速 | 748%[^1] |"
    audit = audit_report_grounding(report, {1: evidence})
    assert audit["grounding_rate"] == 1.0


def test_propagates_input_citations_to_derived_yoy_row():
    report = "| 营收 | 100[^2] | 120[^3] |\n| YoY | — | 20% |"
    repaired = propagate_derived_table_citations(report)
    assert "| YoY（按相邻输入计算）[^2][^3] |" in repaired


def test_propagates_table_inputs_to_following_derived_sum_statement():
    report = (
        "| IP甲 | 100[^2] |\n"
        "| IP乙 | 80[^3] |\n"
        "- **测算声明**：两者合计180亿元。"
    )
    repaired = propagate_derived_table_citations(report)
    assert "合计180亿元。[^3][^2]" in repaired


def test_normalizes_grouped_citations():
    assert normalize_grouped_citations("结论[^38,39，33]。") == "结论[^38][^39][^33]。"


def test_normalizes_plain_numeric_citation_only_for_valid_evidence_id():
    repaired = normalize_plain_numeric_citations("结论[92]，年份[202]。", {92})
    assert repaired == "结论[^92]，年份[202]。"


def test_neutralizes_only_uncited_numeric_tracking_signal():
    report = """## 展望与关注点
| 指标 | 当前值 | 观察信号 | 影响 |
|---|---|---|---|
| 手机出货 | 当前承压[^2] | 2026年环比下滑5%以上 | 需求恶化 |
| 公司指引 | 当前值[^3] | 官方目标20%[^3] | 达标 |
"""
    repaired = neutralize_uncited_tracking_thresholds(report)
    assert "2026年环比下滑5%以上" not in repaired
    assert "现有证据不足以设定精确阈值" in repaired
    assert "官方目标20%[^3]" in repaired


def test_adds_boundary_only_row_for_missing_dimension():
    report = """## 基本事实
### 分析维度覆盖
| 分析维度 | 结论 | 证据 |
|---|---|---|
| 增长质量 | 已验证 | [^1] |

## 核心发现
内容
"""
    repaired, missing = ensure_dimension_coverage_boundaries(
        report, ["增长质量", "政策与监管"]
    )
    assert missing == ["政策与监管"]
    assert "| 政策与监管 | **证据边界**" in repaired
    assert "不作方向性强结论" in repaired


def test_attaches_citations_only_when_all_numeric_tokens_are_supported():
    pool = {
        1: Evidence(content="北方华创毛利率为44%。"),
        2: Evidence(content="中芯国际毛利率20%，华虹半导体毛利率13%。"),
    }
    report = (
        "北方华创毛利率44%，高于中芯国际20%和华虹半导体13%。\n"
        "另一家公司毛利率50%，高于华虹半导体13%。"
    )
    repaired = attach_exact_numeric_citations(report, pool)
    first, second = repaired.splitlines()
    assert "[^1]" in first and "[^2]" in first
    assert "[^" not in second


def test_repairs_existing_but_incomplete_numeric_citation():
    pool = {
        1: Evidence(content="公司2025年营收100亿元。"),
        2: Evidence(content="公司2024年营收80亿元。"),
    }
    repaired = attach_exact_numeric_citations(
        "| 营收 | 2024年80亿元[^1] | 2025年100亿元[^1] |",
        pool,
    )
    assert "[^2]" in repaired


def test_neutralizes_mismatched_precision_only_in_coverage_table():
    report = """### 分析维度覆盖
| 维度 | 结论 | 证据 |
|---|---|---|
| 竞争格局 | CR5超过90%[^1] | [^1] |

### 其他表格
| 指标 | 90%[^1] |
"""
    repaired = neutralize_unsupported_coverage_rows(
        report,
        {"numeric_citation_mismatches": ["| 竞争格局 | CR5超过90%[^1] | [^1] |"]},
    )
    assert "无可核验量化口径" in repaired
    assert "| 指标 | 90%[^1] |" in repaired


def test_writing_contract_keeps_omitted_section_coverage_note():
    run = _run()
    run.profile.sections = ["财务", "监管"]
    evidence = Evidence(content="监管要求概率公示", tier=SourceTier.THIRD_PARTY)
    omitted = Insight(
        section="监管",
        claim="概率公示要求将提高合规成本",
        reasoning="监管要求意味着企业需要改造披露与售后流程。",
        falsifiable_condition="现行规则未要求概率公示",
        evidence=[evidence],
        verdict=Verdict.SUPPORTED,
        confidence=0.75,
    )
    contract = build_writing_contract(
        run,
        {1: evidence},
        [],
        [],
        lambda ev: 1,
        all_insights=[omitted],
    )
    assert "未入核心发现、但必须简要覆盖" in contract
    assert "概率公示要求将提高合规成本" in contract
