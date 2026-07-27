from schemas import AnalysisRun, Evidence, Insight, ObjectProfile, Verdict
from evals.report_quality_metrics import measure_report


def _run() -> AnalysisRun:
    evidence = Evidence(
        content="公司披露2025年收入100亿元，同比增长20%。",
        source_url="https://example.com/report",
    )
    insight = Insight(
        section="增长质量",
        claim="增长仍在延续",
        reasoning="收入增长意味着需求仍在扩张，但需要观察持续性。",
        falsifiable_condition="后续收入不再增长",
        evidence=[evidence],
        verdict=Verdict.SUPPORTED,
        confidence=0.8,
    )
    return AnalysisRun(
        query="测试公司",
        profile=ObjectProfile(
            name="测试公司",
            kind="company",
            is_public=True,
            industry="软件",
            business_model="订阅",
            sections=["增长质量", "竞争格局"],
        ),
        evidence_pool=[evidence],
        insights=[insight],
    )


def test_measure_report_combines_grounding_coverage_and_argument_metrics():
    run = _run()
    report = """## 执行摘要
增长仍在延续。

## 基本事实
2025年收入100亿元，同比增长20%[^1]。

### 分析维度覆盖
增长质量已有数据支撑；竞争格局尚有证据缺口。

## 核心发现
### 增长具备延续性
收入增长意味着需求仍在扩张[^1]，但仍需跟踪后续收入验证。

## 展望与关注点
关注收入增长。

## 风险与不确定性
竞争格局证据不足。

#### 参考文献
- **[^1]** Source.
"""
    metrics = measure_report(run, report)
    assert metrics["accuracy_score"] == 100.0
    assert metrics["section_coverage_rate"] == 1.0
    assert metrics["required_structure_rate"] == 1.0
    assert metrics["missing_reference_mappings"] == []
    assert metrics["argument"]["supported_finding_rate"] == 1.0
    assert metrics["argument"]["reasoning_chain_rate"] == 1.0


def test_measure_report_detects_missing_mapping_and_section():
    run = _run()
    report = "## 执行摘要\n2025年收入100亿元[^1]。\n\n## 核心发现\n内容。"
    metrics = measure_report(run, report)
    assert metrics["missing_reference_mappings"] == [1]
    assert metrics["section_coverage_rate"] < 1.0
    assert metrics["required_structure_rate"] < 1.0
