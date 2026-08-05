"""Unit tests: Analyze fallback 占位洞察的止血行为。

issue: Analyze 阶段 LLM 输出 JSON 失败时触发 _fallback_analyze_items，
旧实现把所有 section 套同一句模板「{name}在「{section}」维度存在需要进一步验证的关键信号」，
还当作真洞察送进红队九维挑战与报告，导致报告正文满是套话/占位。

止血规则：
- fallback 项带 _is_fallback=True、confidence=0.0、claim 明确标注"分析未完成"，不再伪装成洞察
- 主循环将其置为 is_falsifiable=False + needs_human=True
- _select_report_insights 排除 needs_human 洞察（含"逻辑推导型"分支）
"""
from __future__ import annotations

from orchestrator import Orchestrator
from schemas import AnalysisRun, Insight, ObjectProfile, Verdict


class TestFallbackInsights:
    def _orch(self, sections=None):
        orch = Orchestrator.__new__(Orchestrator)
        run = AnalysisRun(id="t", query="x")
        run.profile = ObjectProfile(
            name="某短剧公司", kind="industry", is_public=False, ticker="",
            industry="短剧", business_model="", template_key="generic",
            peers=[], leaders=[], key_questions=["市场规模"], sections=sections or [],
        )
        orch.run = run
        orch.evidence_pool = {}
        return orch

    def test_fallback_items_are_marked_and_not_disguised(self):
        orch = self._orch(sections=["规模与周期定位", "竞争格局"])
        orch.evidence_pool = {"e1": object(), "e2": object()}
        items = orch._fallback_analyze_items(orch.run.profile)
        assert len(items) == 2
        for it in items:
            assert it["_is_fallback"] is True
            assert it["confidence"] == 0.0
            assert "存在需要进一步验证的关键信号" not in it["claim"]
            assert "分析未完成" in it["claim"]

    def test_select_report_insights_excludes_needs_human(self):
        """占位洞察即使 reasoning 足够长也不能进入报告（含"逻辑推导型"分支）。"""
        orch = self._orch()
        ok = Insight(
            section="财务", claim="真实洞察",
            reasoning="因为营收持续增长，这意味着公司有真实需求驱动，因此该结论成立。",
            confidence=0.8, verdict=Verdict.SUPPORTED, is_falsifiable=True,
            falsifiable_condition="",
        )
        fb = Insight(
            section="规模", claim="占位洞察",
            reasoning=("占位推理内容足够长以触发逻辑推导型分支判断。"
                       "因为占位洞察不应进入报告，这意味着需要 needs_human 拦截。"),
            confidence=0.35, verdict=Verdict.QUESTIONABLE,
            is_falsifiable=False, needs_human=True,
            falsifiable_condition="",
        )
        orch.run.insights = [ok, fb]
        core, risk = orch._select_report_insights()
        assert any(i.claim == "真实洞察" for i in core)
        assert all(i.claim != "占位洞察" for i in core + risk)
