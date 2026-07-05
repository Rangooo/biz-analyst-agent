"""Integration test: demo pipeline runs six stages and produces valid narrative."""
from __future__ import annotations

import asyncio
import re

import pytest

import orchestrator as orch_mod
from orchestrator import Orchestrator
from schemas import AnalysisRun, Verdict

_FACTS_ALIASES = ("基本事实", "行业格局", "公司画像", "公司概况", "行业概况", "公司与行业概况")


@pytest.fixture(scope="module")
def demo_result():
    """Run demo pipeline once, shared across all tests in this module."""
    old_save_run = orch_mod.save_run
    old_save_episodic = orch_mod.memory_store.save_episodic
    orch_mod.save_run = lambda _run: None
    orch_mod.memory_store.save_episodic = lambda _summary: None
    try:
        run = AnalysisRun(query="奇富科技", provider="demo")
        orch = Orchestrator(run)
        orch.demo = True

        async def _run_pipeline():
            async for _ in orch.run_pipeline():
                pass
            return run, orch

        run, orch = asyncio.run(_run_pipeline())
        return run, orch
    finally:
        orch_mod.save_run = old_save_run
        orch_mod.memory_store.save_episodic = old_save_episodic


@pytest.mark.slow
class TestDemoPipeline:
    def test_narrative_produced(self, demo_result):
        run, _ = demo_result
        assert run.narrative_md, "narrative_md is empty"

    def test_no_structure_violations(self, demo_result):
        run, orch = demo_result
        violations = orch._check_structure_invariants(run.narrative_md)
        assert not violations, f"Structure violations: {violations[:3]}"

    def test_no_numbered_h2(self, demo_result):
        run, _ = demo_result
        h2_lines = [l for l in run.narrative_md.split("\n") if l.startswith("## ") and not l.startswith("### ")]
        numbered = [l for l in h2_lines if re.match(r"^## \d+", l)]
        assert not numbered, f"Found: {numbered[:2]}"

    def test_history_not_h2(self, demo_result):
        run, _ = demo_result
        h2_lines = [l for l in run.narrative_md.split("\n") if l.startswith("## ") and not l.startswith("### ")]
        assert not any("历史趋势" in l for l in h2_lines)

    def test_no_duplicate_fact_sections(self, demo_result):
        run, _ = demo_result
        lines = run.narrative_md.split("\n")
        h2_lines = [l for l in lines if l.startswith("## ") and not l.startswith("### ")]
        assert sum("执行摘要" in l for l in h2_lines) == 1
        assert sum(any(alias in l for alias in _FACTS_ALIASES) for l in h2_lines) == 1

    def test_history_inside_facts(self, demo_result):
        run, _ = demo_result
        lines = run.narrative_md.split("\n")
        facts_idx = next((i for i, l in enumerate(lines) if any(a in l for a in _FACTS_ALIASES)), -1)
        hist_idx = next((i for i, l in enumerate(lines) if "历史趋势" in l), -1)
        next_h2_idx = next(
            (i for i, l in enumerate(lines[facts_idx + 1:], facts_idx + 1)
             if l.startswith("## ") and not any(a in l for a in _FACTS_ALIASES)),
            len(lines),
        ) if facts_idx >= 0 else -1
        if hist_idx >= 0:
            assert facts_idx < hist_idx < next_h2_idx

    def test_no_footnote_definitions(self, demo_result):
        run, _ = demo_result
        defs = [l for l in run.narrative_md.split("\n") if re.match(r"^\[\^\d+\]:", l.strip())]
        assert not defs, f"Found {len(defs)} footnote definitions"

    def test_references_le_15(self, demo_result):
        run, _ = demo_result
        items = [l for l in run.narrative_md.split("\n") if l.strip().startswith("- **[^")]
        assert len(items) <= 15, f"Found {len(items)} reference items"

    def test_reference_title_h3(self, demo_result):
        run, _ = demo_result
        ref_lines = [l for l in run.narrative_md.split("\n") if "参考文献" in l and l.strip().startswith("#")]
        for line in ref_lines:
            assert line.strip().startswith("### "), f"Not h3: {line}"

    def test_no_future_words_in_falsification(self, demo_result):
        run, _ = demo_result
        future_words = ("若下季度", "若明年", "若后续", "如果未来", "待观察", "待验证")
        for ins in run.insights:
            cond = getattr(ins, "falsifiable_condition", "") or ""
            for w in future_words:
                assert w not in cond, f"{ins.claim[:20]} contains {w}"

    def test_footer_has_dates(self, demo_result):
        run, _ = demo_result
        assert "数据截至" in run.narrative_md
        assert "报告生成" in run.narrative_md

    def test_confidence_within_caps(self, demo_result):
        run, _ = demo_result
        caps = {Verdict.REFUTED: 0.20, Verdict.UNVERIFIABLE: 0.30}
        for ins in run.insights:
            cap = caps.get(ins.verdict)
            if cap is not None:
                assert (ins.confidence or 0) <= cap + 0.01, \
                    f"{ins.claim[:20]} verdict={ins.verdict} conf={ins.confidence}"

    def test_no_llm_reference_residue(self, demo_result):
        run, _ = demo_result
        residues = [l for l in run.narrative_md.split("\n") if l.strip().startswith("**") and "参考文献" in l]
        assert not residues
