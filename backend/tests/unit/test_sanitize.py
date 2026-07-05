"""Unit tests: _sanitize_falsification_path and _sanitize_temporal_challenges."""
from __future__ import annotations

import pytest

from orchestrator import _sanitize_falsification_path, _sanitize_temporal_challenges


class TestSanitizeFalsificationPath:
    def test_future_hypothesis_cleared(self):
        result = _sanitize_falsification_path("若下季度收入增速回升至10%以上则推翻", [], "solid")
        assert "若下季度" not in result
        assert "无——基于现有公开数据无法推翻" in result

    def test_refuted_also_clears_future(self):
        result = _sanitize_falsification_path("若后续财报披露利润下滑则推翻", [], "refuted")
        assert "若后续" not in result
        assert "未发布" in result

    def test_trailing_future_verification_cleared(self):
        result = _sanitize_falsification_path(
            "无——基于现有公开数据无法推翻；若需进一步验证，可查腾讯后续财报", [], "solid")
        assert result == "无——基于现有公开数据无法推翻"

    def test_no_future_preserved(self):
        fp = "腾讯2025Q1经营利润率降至35%以下"
        assert _sanitize_falsification_path(fp, [], "refuted") == fp


class TestSanitizeTemporalChallenges:
    def test_generic_stale_downgraded(self):
        challenges = [
            {"dimension": "temporal", "severity": "medium",
             "challenge": "数据已过时，需要更新的财报数据验证"},
            {"dimension": "logic", "severity": "high",
             "challenge": "推理存在因果简化"},
        ]
        fixed_ch, fixed_ov = _sanitize_temporal_challenges(challenges, "2025-12-31", "incomplete")
        temp = [c for c in fixed_ch if c.get("dimension") == "temporal"][0]
        assert temp["severity"] == "none"
        assert fixed_ov == "incomplete"  # other high keeps it

    def test_specific_reference_not_downgraded(self):
        challenges = [
            {"dimension": "temporal", "severity": "medium",
             "challenge": "2025年报已于2026年4月发布但未引用，数据滞后"},
        ]
        fixed_ch, _ = _sanitize_temporal_challenges(challenges, "2025-12-31", "incomplete")
        temp = [c for c in fixed_ch if c.get("dimension") == "temporal"][0]
        assert temp["severity"] == "medium"

    def test_overall_upgrades_to_solid_after_downgrade(self):
        challenges = [
            {"dimension": "temporal", "severity": "medium",
             "challenge": "数据存在滞后，需要更近期的数据"},
            {"dimension": "source_reliability", "severity": "low",
             "challenge": "来源权威性尚可"},
        ]
        _, fixed_ov = _sanitize_temporal_challenges(challenges, "2025-12-31", "incomplete")
        assert fixed_ov == "solid"

    def test_non_temporal_unaffected(self):
        challenges = [
            {"dimension": "missing_evidence", "severity": "high",
             "challenge": "缺失 vintage 滚动率数据"},
        ]
        fixed_ch, fixed_ov = _sanitize_temporal_challenges(challenges, "2025-12-31", "incomplete")
        me = [c for c in fixed_ch if c.get("dimension") == "missing_evidence"][0]
        assert me["severity"] == "high"
        assert fixed_ov == "incomplete"

    def test_no_date_passthrough(self):
        """Without data_as_of, function should be no-op."""
        challenges = [
            {"dimension": "temporal", "severity": "medium", "challenge": "data stale"},
        ]
        fixed_ch, fixed_ov = _sanitize_temporal_challenges(challenges, None, "incomplete")
        # No date → cannot judge, should pass through unchanged
        temp = [c for c in fixed_ch if c.get("dimension") == "temporal"][0]
        assert temp["severity"] == "medium"
