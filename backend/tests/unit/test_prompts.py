"""Unit tests: prompt structure validation (minimal, non-brittle)."""
from __future__ import annotations

import inspect

import pytest

import prompts


class TestFalsificationArchitecture:
    """Verify analyst doesn't write falsifiable_condition; red team owns it."""

    def test_analyze_no_falsifiable_condition(self):
        msgs = prompts.analyze_prompt(
            '{"name":"测试公司","is_public":true}',
            {"key_metrics": ["营收"], "trap_questions": []}, "证据", private=False)
        user_text = msgs[1]["content"]
        assert "不需要写可证伪条件" in user_text
        assert "falsifiable_condition" not in user_text

    def test_red_team_outputs_falsification_path(self):
        msgs = prompts.red_team_prompt("论点", "推理", "证据")
        assert "falsification_path" in msgs[1]["content"]
        assert "当前证据反查路径" in msgs[0]["content"]
        assert "若下季度/若明年/若后续" in msgs[1]["content"]

    def test_reverdict_outputs_final_path(self):
        msgs = prompts.reverdict_prompt("论点", "挑战", "支撑", "反证")
        assert "final_falsification_path" in msgs[1]["content"]


class TestRedTeamContext:
    def test_non_public_hint(self):
        msgs = prompts.red_team_prompt("论点", "推理", "证据", is_public=False, industry=False)
        sys_text = msgs[0]["content"]
        assert "非上市公司" in sys_text

    def test_industry_hint(self):
        msgs = prompts.red_team_prompt("论点", "推理", "证据", is_public=True, industry=True)
        assert "行业分析" in msgs[0]["content"]

    def test_temporal_constraint_with_dates(self):
        msgs = prompts.red_team_prompt("论点", "推理", "证据",
                                        is_public=True, industry=False,
                                        today="2026-06-29", data_as_of="2025-12-31")
        sys_text = msgs[0]["content"]
        assert "时效约束" in sys_text
        assert "2026-06-29" in sys_text
        assert "2025-12-31" in sys_text

    def test_no_dates_no_temporal(self):
        msgs = prompts.red_team_prompt("论点", "推理", "证据")
        assert "时效约束" not in msgs[0]["content"]

    def test_missing_evidence_antibluff(self):
        msgs = prompts.red_team_prompt("论点", "推理", "证据", industry=True)
        assert "禁止空话挑战" in msgs[0]["content"]
        assert "具体缺了哪份可获取的文档" in msgs[0]["content"]


class TestNarrativeGovernance:
    def test_facts_prompt_governance(self):
        text = "\n".join(m["content"] for m in prompts.narrative_facts_prompt("{}", "证据", False, True, False))
        assert "3000-5000" in text
        assert "公司官方财报" in text
        assert "港股/A股公司禁止写成 SEC EDGAR" in text

    def test_core_prompt_governance(self):
        text = "\n".join(m["content"] for m in prompts.narrative_insights_core_prompt("{}", "洞察", "证据", False, True, False))
        assert "强归因闸门" in text
        assert "数字单位闸门" in text
        assert "精选 3 条" in text

    def test_outlook_prompt_governance(self):
        text = "\n".join(m["content"] for m in prompts.narrative_insights_outlook_prompt("{}", "洞察", "证据", False, True, False))
        assert "最多写3个风险" in text
        assert "不是可证伪条件" in text

    def test_industry_extras(self):
        facts_ind = "\n".join(m["content"] for m in prompts.narrative_facts_prompt("{}", "证据", False, True, True))
        assert "TAM/SAM/SOM" in facts_ind
        assert "CR3/CR5/CR8" in facts_ind

    def test_full_prompt_uses_grounded_writing_contract(self):
        msgs = prompts.narrative_full_prompt(
            "{}", "证据", "洞察", "风险",
            writing_contract="【Agent写作契约】不得虚构阈值、概率或影响金额",
        )
        text = "\n".join(m["content"] for m in msgs)
        assert "不得虚构阈值、概率或影响金额" in text
        assert "不强制给情景概率或跟踪阈值" in text
        assert "数据可得性与口径" in text


class TestQualityEvalPrompt:
    def test_default_score_2(self):
        msgs = prompts.quality_eval_prompt("报告内容", ["维度1"])
        sys_text = msgs[0]["content"]
        assert "默认评分应该是2" in sys_text
        assert "4分或以上" in sys_text

    def test_high_total_requires_issues(self):
        msgs = prompts.quality_eval_prompt("报告内容", ["维度1"])
        assert "至少3个具体问题" in msgs[0]["content"]

class TestSparseDataPrompt:
    def test_sparse_triggers_advice(self):
        msgs = prompts.analyze_prompt(
            '{"name":"某初创公司","is_public":false}',
            {"key_metrics": ["营收"], "trap_questions": ["增长是否真实"]},
            "证据1\n证据2", private=True, evidence_count=5)
        text = msgs[1]["content"]
        assert "宁缺毋滥" in text
        assert "3-4" in text

    def test_rich_data_no_advice(self):
        msgs = prompts.analyze_prompt(
            '{"name":"某非上市公司","is_public":false}',
            {"key_metrics": ["营收"], "trap_questions": ["增长"]},
            "证据" * 20, private=True, evidence_count=15)
        assert "宁缺毋滥" not in msgs[1]["content"]


class TestFinancialExtractPrompt:
    def test_covers_quarters(self):
        text = "\n".join(m["content"] for m in prompts.extract_financials_prompt("{}", "证据"))
        assert "最新 2-4 个季度" in text
        assert "不要把季度数据年化" in text


class TestLogicConsistencyPrompt:
    def test_structure(self):
        msgs = prompts.logic_consistency_prompt("执行摘要", "核心发现", "风险展望")
        assert len(msgs) == 2
        assert "逻辑一致性" in msgs[0]["content"]
        assert "执行摘要" in msgs[1]["content"]
