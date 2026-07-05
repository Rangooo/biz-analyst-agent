"""Unit tests: confidence cap, fusion, alignment, freeze logic."""
from __future__ import annotations

import pytest

from orchestrator import (
    _align_verdict_confidence,
    _cap_confidence,
    _fuse_confidence,
    _is_stale,
    _should_freeze_confidence,
)
from schemas import Verdict


class TestCapConfidence:
    def test_refuted_caps_to_020(self):
        assert _cap_confidence(Verdict.REFUTED, 0.79) == 0.20

    def test_unverifiable_caps_to_030(self):
        assert _cap_confidence(Verdict.UNVERIFIABLE, 0.55) == 0.30

    def test_below_cap_unchanged(self):
        assert _cap_confidence(Verdict.REFUTED, 0.10) == 0.10

    def test_supported_no_cap(self):
        assert _cap_confidence(Verdict.SUPPORTED, 0.90) == 0.90


class TestFuseConfidence:
    def test_weighted_fusion(self):
        assert _fuse_confidence(0.8, 0.6) == round(0.8 * 0.7 + 0.6 * 0.3, 3)


class TestAlignVerdictConfidence:
    def test_supported_low_confidence_downgrade(self):
        assert _align_verdict_confidence(Verdict.SUPPORTED, 0.45) == Verdict.QUESTIONABLE

    def test_supported_adequate_stays(self):
        assert _align_verdict_confidence(Verdict.SUPPORTED, 0.60) == Verdict.SUPPORTED

    def test_questionable_high_upgrades(self):
        assert _align_verdict_confidence(Verdict.QUESTIONABLE, 0.72) == Verdict.SUPPORTED

    def test_questionable_mid_stays(self):
        assert _align_verdict_confidence(Verdict.QUESTIONABLE, 0.58) == Verdict.QUESTIONABLE

    def test_refuted_unaffected(self):
        assert _align_verdict_confidence(Verdict.REFUTED, 0.10) == Verdict.REFUTED

    def test_unverifiable_unaffected(self):
        assert _align_verdict_confidence(Verdict.UNVERIFIABLE, 0.25) == Verdict.UNVERIFIABLE


class TestFreezeConfidence:
    def test_freeze_when_all_conditions_met(self):
        assert _should_freeze_confidence(0, 1, "incomplete", False) is True

    def test_not_frozen_sufficient_support(self):
        assert _should_freeze_confidence(0, 2, "incomplete", False) is False

    def test_not_frozen_solid(self):
        assert _should_freeze_confidence(0, 0, "solid", False) is False

    def test_not_frozen_has_search(self):
        assert _should_freeze_confidence(0, 0, "incomplete", True) is False

    def test_not_frozen_has_counter(self):
        assert _should_freeze_confidence(1, 0, "incomplete", False) is False


class TestIsStale:
    def test_stale_when_barely_changed(self):
        assert _is_stale(0.50, 0.49, False) is True

    def test_not_stale_when_reinforced(self):
        assert _is_stale(0.50, 0.49, True) is False

    def test_not_stale_when_changed_significantly(self):
        assert _is_stale(0.80, 0.50, False) is False
