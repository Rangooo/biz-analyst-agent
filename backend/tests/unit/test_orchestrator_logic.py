"""Unit tests: search budget limits, data gap appendix, select report insights."""
from __future__ import annotations

import pytest

import orchestrator
from orchestrator import Orchestrator
from schemas import (
    AnalysisRun,
    Evidence,
    FalsificationRecord,
    Insight,
    ObjectProfile,
    SourceTier,
    Verdict,
)


class TestSearchBudget:
    def test_budget_constants(self):
        assert orchestrator.MAX_COUNTER_QUERIES_PER_INSIGHT <= 2
        assert orchestrator.MAX_COUNTER_RESULTS_PER_QUERY <= 1
        assert orchestrator.MAX_SUPPORT_QUERIES_PER_INSIGHT <= 1
        assert orchestrator.MAX_DEBATE_QUERIES_PER_INSIGHT <= 1


class TestSelectReportInsights:
    def test_all_refuted_selects_zero(self):
        run = AnalysisRun(query="测试", provider="deepseek")
        run.profile = ObjectProfile(
            name="测试公司", kind="company", is_public=True, ticker="TEST",
            industry="测试", business_model="测试", template_key="generic",
            peers=[], leaders=[], key_questions=[], sections=["测试"],
        )
        run.insights = [
            Insight(section="测试", claim="论点1", reasoning="推理", falsifiable_condition="条件",
                    verdict=Verdict.REFUTED, confidence=0.2, is_falsifiable=True),
            Insight(section="测试", claim="论点2", reasoning="推理", falsifiable_condition="条件",
                    verdict=Verdict.REFUTED, confidence=0.15, is_falsifiable=True),
        ]
        orch = Orchestrator(run)
        orch.demo = True
        core, risk = orch._select_report_insights()
        assert len(core) == 0 and len(risk) == 0

    def test_unfalsifiable_but_logical_kept(self):
        run = AnalysisRun(query="测试", provider="deepseek")
        run.profile = ObjectProfile(
            name="测试公司", kind="company", is_public=True, ticker="TEST",
            industry="测试", business_model="测试", template_key="generic",
            peers=[], leaders=[], key_questions=[], sections=["测试"],
        )
        # Reasoning must be >50 chars and contain analytical cues for QUESTIONABLE to be kept
        run.insights = [
            Insight(section="测试", claim="杠杆率见顶导致增速下移",
                    reasoning="居民杠杆率已达62%（BIS口径），可支配收入增速5%-6%，因此消费信贷增量空间结构性收窄。"
                              "这意味着未来2-3年增速中枢5%-7%区间，靠放款量驱动利润的模型正在失效。",
                    falsifiable_condition="",
                    verdict=Verdict.QUESTIONABLE, confidence=0.5, is_falsifiable=False,
                    evidence=[Evidence(content="杠杆率62%", tier=5, supports=True)]),
        ]
        orch = Orchestrator(run)
        orch.demo = True
        core, risk = orch._select_report_insights()
        # Should appear in either core or risk (has analytical value)
        assert len(core) + len(risk) >= 1


class TestDataGapAppendix:
    def test_builds_appendix(self):
        run = AnalysisRun(query="测试行业")
        run.profile = ObjectProfile(
            name="测试行业", kind="industry", is_public=True, ticker="",
            industry="测试", business_model="", template_key="generic",
            peers=[], leaders=["龙头A"], key_questions=[], sections=["市场规模"],
        )
        run.collect_quality = {"status": "poor", "issues": ["缺少权威行业总量数据"]}
        run.industry_metrics = {"totals": [], "prices": []}
        ins = Insight(
            section="市场规模", claim="行业空间扩大", reasoning="推理",
            falsifiable_condition="现有行业统计口径若显示总量下降则推翻",
            verdict=Verdict.QUESTIONABLE, confidence=0.45,
        )
        ins.falsifications.append(FalsificationRecord(
            counter_hypothesis="空间未扩大",
            challenges=[{
                "dimension": "missing_evidence", "severity": "high",
                "challenge": "缺少当前已发布的行业协会总量口径",
                "search_query": "测试行业 行业协会 总量 2026",
            }],
        ))
        run.insights = [ins]

        orch = Orchestrator(run)
        ev = Evidence(content="测试证据", source_title="测试来源", source_url="https://example.com", as_of="2026-01-01")
        orch.evidence_pool = {1: ev}
        ins.evidence = [ev]
        appendix = orch._build_appendix_md(
            "正文 [UNSOURCED] 未披露 无数据 当前披露不足。"
            "FY2026 营收（百万元）和 Q4 净利润235亿元同时出现，表格涉及净利润与季度。"
            "云智能是利润暴增主因，不是一次性扰动，但分部利润未拆分、无法精确量化。[^1]"
        )
        assert "### 数据缺口与不确定性" not in appendix  # 已删除，只保留参考文献
        assert "#### 参考文献" in appendix
