"""Unit tests: listing detection, SEC ticker guard, source tier guessing."""
from __future__ import annotations

import pytest

from orchestrator import Orchestrator, _is_sec_ticker, _SOURCE_TIER_GROUPS
from schemas import AnalysisRun, Evidence, ObjectProfile, SourceTier


class TestDetectListing:
    @pytest.fixture
    def orch(self):
        return Orchestrator.__new__(Orchestrator)

    def test_hk_stock_detected(self, orch):
        results = [
            {"title": "蜜雪集团港股IPO上市",
             "content": "蜜雪冰城母公司蜜雪集团(02097.HK)在港交所主板挂牌上市"}
        ]
        listing = orch._detect_listing(results)
        assert listing is not None
        assert "02097" in listing.get("ticker", "")
        assert "蜜雪集团" in listing.get("name", "")

    def test_a_share_detected(self, orch):
        results = [
            {"title": "某公司A股上市",
             "content": "某科技股份(000001.SZ)在深圳证券交易所挂牌"}
        ]
        listing = orch._detect_listing(results)
        assert listing is not None
        assert "000001.SZ" in listing.get("ticker", "")

    def test_no_listing_returns_none(self, orch):
        results = [{"title": "某公司融资", "content": "某公司获得A轮融资1亿元"}]
        assert orch._detect_listing(results) is None

    def test_empty_returns_none(self, orch):
        assert orch._detect_listing([]) is None

    def test_none_returns_none(self, orch):
        assert orch._detect_listing(None) is None


class TestSecTickerGuard:
    def test_us_ticker_allowed(self):
        assert _is_sec_ticker("AAPL") is True

    def test_us_ticker_with_dot_allowed(self):
        assert _is_sec_ticker("BRK.B") is True

    def test_hk_numeric_disallowed(self):
        assert _is_sec_ticker("00700") is False

    def test_hk_suffix_disallowed(self):
        assert _is_sec_ticker("0700.HK") is False

    def test_a_share_disallowed(self):
        assert _is_sec_ticker("600519.SH") is False


class TestGuessTier:
    def test_config_has_three_groups(self):
        assert len(_SOURCE_TIER_GROUPS) == 3

    def test_regulator_domains(self):
        assert Orchestrator._guess_tier("https://www.sec.gov/x") == SourceTier.THIRD_PARTY
        assert Orchestrator._guess_tier("http://pbc.gov.cn/a") == SourceTier.THIRD_PARTY

    def test_company_pr_domains(self):
        assert Orchestrator._guess_tier("https://cninfo.com.cn/x") == SourceTier.COMPANY_PR
        assert Orchestrator._guess_tier("https://investor.apple.com") == SourceTier.COMPANY_PR
        assert Orchestrator._guess_tier("https://www1.hkexnews.hk/listedco/listconews/sehk/x.pdf") == SourceTier.COMPANY_PR

    def test_media_domains(self):
        assert Orchestrator._guess_tier("https://reuters.com/x") == SourceTier.THIRD_PARTY
        assert Orchestrator._guess_tier("https://finance.eastmoney.com/a/20260701.html") == SourceTier.THIRD_PARTY

    def test_unknown_domain_defaults_media(self):
        assert Orchestrator._guess_tier("https://random-blog.com/x") == SourceTier.MEDIA

    def test_profile_aware_company_official_disclosure(self):
        run = AnalysisRun(query="分析阿里巴巴集团控股有限公司（阿里巴巴）")
        run.profile = ObjectProfile(
            name="阿里巴巴集团控股有限公司", kind="company", is_public=True, ticker="BABA",
            industry="", business_model="", template_key="generic",
            peers=[], leaders=[], key_questions=[], sections=[],
        )
        orch = Orchestrator.__new__(Orchestrator)
        orch.run = run
        orch.evidence_pool = {}
        orch._ev_counter = 0
        ev = Evidence(
            content="阿里巴巴集团公布季度业绩。",
            source_title="阿里巴巴集团公布2026财年季度业绩",
            source_url="https://www.alibabagroup.com/zh-HK/document-1991237455038119936",
            tier=SourceTier.MEDIA,
        )
        orch._add_evidence(ev)
        assert ev.tier == SourceTier.COMPANY_PR

    def test_profile_aware_rule_is_not_company_specific(self):
        run = AnalysisRun(query="Analyze ExampleCorp")
        run.profile = ObjectProfile(
            name="ExampleCorp", kind="company", is_public=True, ticker="EXM",
            industry="", business_model="", template_key="generic",
            peers=[], leaders=[], key_questions=[], sections=[],
        )
        orch = Orchestrator.__new__(Orchestrator)
        orch.run = run
        orch.evidence_pool = {}
        orch._ev_counter = 0
        ev = Evidence(
            content="ExampleCorp announces quarterly results.",
            source_title="ExampleCorp Q4 FY2026 results",
            source_url="https://www.examplecorp.com/investors/financial-results/q4-2026.pdf",
            tier=SourceTier.MEDIA,
        )
        orch._add_evidence(ev)
        assert ev.tier == SourceTier.COMPANY_PR

    def test_media_pdf_is_not_upgraded_to_company_pr(self):
        run = AnalysisRun(query="分析阿里巴巴")
        run.profile = ObjectProfile(
            name="阿里巴巴集团控股有限公司", kind="company", is_public=True, ticker="BABA",
            industry="", business_model="", template_key="generic",
            peers=[], leaders=[], key_questions=[], sections=[],
        )
        orch = Orchestrator.__new__(Orchestrator)
        orch.run = run
        orch.evidence_pool = {}
        orch._ev_counter = 0
        ev = Evidence(
            content="阿里巴巴报告解读。",
            source_title="阿里巴巴财报报告解读",
            source_url="https://finance.sina.com.cn/stock/report.pdf",
            tier=SourceTier.MEDIA,
        )
        orch._add_evidence(ev)
        assert ev.tier == SourceTier.MEDIA
