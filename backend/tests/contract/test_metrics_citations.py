"""Contract tests: citation validator and run metrics."""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from citation_validator import extract_citation_ids, validate_report_citations
from run_metrics import build_run_metrics, evaluate_collect_quality, write_run_metrics
from schemas import AnalysisRun, Evidence, ObjectProfile, SourceTier, TraceEvent


class TestCitationValidator:
    def test_extract_ids(self):
        ids = extract_citation_ids("营收改善 [证据1]，利润率参考 [Evidence 3] 和 [证据1]。")
        assert ids == [1, 3]

    def test_missing_id_detected(self):
        pool = [Evidence(content="Revenue grew.", source_url="https://example.com/filing",
                         source_title="Annual filing", as_of="2025-12-31", tier=SourceTier.FILING)]
        result = validate_report_citations("结论来自 [证据1] 和 [证据2]。", pool)
        assert result.missing_ids == [2]
        assert not result.ok

    def test_incomplete_metadata_detected(self):
        pool = [
            Evidence(content="Revenue grew.", source_url="", source_title="Annual filing", as_of="2025-12-31"),
            Evidence(content="Margin improved.", source_url="https://example.com/report",
                     source_title="Industry report", published_at="2026-01-01"),
        ]
        result = validate_report_citations("结论来自 [证据1] 和 [证据2]。", pool)
        assert 1 in result.incomplete_evidence_ids
        assert 2 not in result.incomplete_evidence_ids


class TestRunMetrics:
    def _make_run(self):
        run = AnalysisRun(
            query="测试公司分析",
            profile=ObjectProfile(
                name="测试公司", industry="金融科技",
                sections=["规模与盈利质量", "资产质量真实性", "监管合规"],
            ),
            data_sources_used=["sec_edgar", "general_search"],
            data_as_of="2026-06-01",
        )
        return run

    def _make_pool(self):
        return [
            Evidence(content="营收增长", source_url="https://www.sec.gov/filing/a",
                     source_title="10-K", source_type="filing", tier=SourceTier.FILING, as_of="2025-12-31"),
            Evidence(content="季报", source_url="https://ir.example.com/q1",
                     source_title="Q1 release", source_type="notice",
                     tier=SourceTier.COMPANY_PR, published_at="2026-05-10"),
            Evidence(content="资产质量", source_url="https://news.example.com/x",
                     source_title="观察", source_type="news",
                     tier=SourceTier.THIRD_PARTY, published_at="2026-04-20"),
        ]

    def test_collect_quality_ok(self):
        run = self._make_run()
        # Use 8 evidences covering all sections
        pool = self._make_pool() * 3  # 9 items
        quality = evaluate_collect_quality(run, pool)
        assert quality["status"] == "ok"
        assert quality["score"] >= 70

    def test_collect_quality_poor(self):
        run = self._make_run()
        quality = evaluate_collect_quality(run, [])
        assert quality["status"] == "poor"
        assert any("证据量不足" in x for x in quality["issues"])

    def test_metrics_export(self):
        run = self._make_run()
        run.evidence_pool = self._make_pool()
        run.collect_quality = evaluate_collect_quality(run, run.evidence_pool)
        run.created_at = "2026-06-01T10:00:00"
        run.updated_at = "2026-06-01T10:02:30"
        run.trace.append(TraceEvent(stage="collect", type="search", title="mock search", ts="2026-06-01T10:00:10"))
        run.trace.append(TraceEvent(stage="report", type="diagnosis", title="报告阶段降级", detail="issue", ts="2026-06-01T10:02:00"))
        run.token_summary = {"total_calls": 2, "total_input": 100, "total_output": 50, "total_cost_usd": 0.01}
        run.run_metrics = build_run_metrics(run)

        with tempfile.TemporaryDirectory() as tmp:
            path = write_run_metrics(run, Path(tmp))
            data = json.loads(path.read_text(encoding="utf-8"))
            assert path.exists()
            assert data["run_id"] == run.id
            assert data["collect_quality"]["score"] == run.collect_quality["score"]
            assert data["diagnostics"][0]["title"] == "报告阶段降级"
