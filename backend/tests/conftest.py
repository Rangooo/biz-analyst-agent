"""Root conftest: path setup + shared fixtures for all test layers."""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure backend root is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from schemas import (
    AnalysisRun,
    Evidence,
    FalsificationRecord,
    Insight,
    ObjectProfile,
    SourceTier,
    Verdict,
)


# --------------- Fixtures ---------------


@pytest.fixture
def sample_profile():
    """A minimal ObjectProfile for unit tests."""
    return ObjectProfile(
        name="测试公司",
        kind="company",
        is_public=True,
        ticker="TEST",
        industry="测试行业",
        business_model="测试模式",
        template_key="generic",
        peers=["竞品A"],
        leaders=[],
        key_questions=["增长是否真实"],
        sections=["财务表现", "风控质量"],
    )


@pytest.fixture
def sample_run(sample_profile):
    """An AnalysisRun with profile pre-attached."""
    run = AnalysisRun(query="测试公司", provider="deepseek")
    run.profile = sample_profile
    return run


@pytest.fixture
def sample_evidence():
    """A single evidence item for testing."""
    return Evidence(
        content="测试公司2025年净利润18亿元，同比增长20%",
        source_url="https://example.com/report",
        source_title="年度报告",
        source_type="filing",
        tier=SourceTier.FILING,
        as_of="2025-12-31",
    )
