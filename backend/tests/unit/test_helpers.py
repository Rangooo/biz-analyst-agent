"""Unit tests: _harvest, _dedup_queries, _collect_gate_message, token tracker, evidence index, LLM extract."""
from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace

import pytest

from orchestrator import Orchestrator, _dedup_queries, _collect_gate_message
from schemas import AnalysisRun, Evidence, ObjectProfile, SourceTier


class TestDeduplicateQueries:
    def test_cross_source_dedup(self):
        assert _dedup_queries(["a", "b"], ["b", "c"]) == ["a", "b", "c"]

    def test_discard_empty(self):
        assert _dedup_queries(["a", "", "  "], [None]) == ["a"]


class TestCollectGateMessage:
    def test_zero_evidence_triggers_gate(self):
        run = AnalysisRun(query="腾讯控股")
        run.profile = ObjectProfile(
            name="腾讯控股有限公司", kind="company", is_public=True, ticker="00700",
            industry="互联网综合科技", business_model="", template_key="internet_saas",
            peers=[], leaders=[], key_questions=[], sections=[],
        )
        run.data_sources_used = ["sec_edgar"]
        msg = _collect_gate_message(run, 0, {"status": "unavailable"})
        assert "采集质量门禁未通过" in msg
        assert "EXA_API_KEY" in msg
        assert "已停止后续分析" in msg

    def test_nonzero_evidence_no_gate(self):
        run = AnalysisRun(query="腾讯控股")
        run.profile = ObjectProfile(
            name="腾讯", kind="company", is_public=True, ticker="00700",
            industry="互联网", business_model="", template_key="generic",
            peers=[], leaders=[], key_questions=[], sections=[],
        )
        assert _collect_gate_message(run, 1, {"status": "ok"}) == ""


class TestHarvest:
    """Test _harvest helper for search→pool→event pipeline."""

    @pytest.fixture
    def orch_with_pool(self):
        orch = Orchestrator.__new__(Orchestrator)
        orch.run = AnalysisRun(id="t1", query="x")
        orch.evidence_pool = {}
        orch._ev_counter = 0
        return orch

    def _make_adapter(self, urls):
        class _FakeRes:
            def __init__(self, evs):
                self.evidences = evs

        class _FakeAdapter:
            name = "fake_src"
            def __init__(self, urls):
                self._urls = urls
            def search(self, query, kind="news", max_results=5, days=None):
                evs = [
                    Evidence(content="内容", source_url=url, source_title="标题",
                             source_type="web", tier=SourceTier.MEDIA)
                    for url in urls
                ]
                return _FakeRes(evs)

        return _FakeAdapter(urls)

    def test_take_limits_evidence(self, orch_with_pool):
        ad = self._make_adapter(["http://a", "http://b", "http://c"])

        async def _run():
            events = []
            async for e in orch_with_pool._harvest(ad, "q1", kind="news", max_results=5, take=2,
                                                    source="fake_src", emit_search="检索"):
                events.append(e)
            return events

        events = asyncio.run(_run())
        search_evs = [e for e in events if e.type == "search"]
        ev_evs = [e for e in events if e.type == "evidence"]
        assert len(search_evs) == 1
        assert len(ev_evs) == 2
        assert len(orch_with_pool.evidence_pool) == 2

    def test_url_dedup(self, orch_with_pool):
        ad = self._make_adapter(["http://dup", "http://dup"])

        async def _run():
            seen = set()
            n = 0
            for q in ("qa", "qb"):
                async for e in orch_with_pool._harvest(ad, q, take=3, source="fake_src", seen_urls=seen):
                    if e.type == "evidence":
                        n += 1
            return n

        n_ev = asyncio.run(_run())
        assert n_ev == 1


class TestStrategyQueries:
    def test_strategy_template_renders_and_records(self):
        run = AnalysisRun(id="s1", query="腾讯控股")
        run.profile = ObjectProfile(
            name="腾讯控股", kind="company", is_public=True, ticker="00700",
            industry="互联网综合服务", business_model="", template_key="internet_saas",
            peers=[], leaders=[], key_questions=[], sections=["游戏收入"],
        )
        orch = Orchestrator.__new__(Orchestrator)
        orch.run = run
        orch.active_strategy_cards = [{
            "id": "sc_test",
            "stage": "collect",
            "action": {"query_templates": ["{name} {section} {year} 财报 公告"]},
        }]
        orch._applied_strategy_ids = set()

        queries = orch._strategy_queries("collect", section="游戏收入", limit=1)

        assert queries == [f"腾讯控股 游戏收入 {datetime.now().year} 财报 公告"]
        assert run.applied_strategy_ids == ["sc_test"]


class TestTokenTracker:
    def test_record_and_summary(self):
        from llm.client import TokenTracker

        tracker = TokenTracker()
        assert tracker.summary()["total_calls"] == 0

        tracker.record("deepseek-chat", "analyst", "scope", 1000, 500)
        tracker.record("deepseek-chat", "reviewer", "report", 2000, 1000)
        tracker.record("gpt-4o", "red_team", "falsify", 1500, 800)

        s = tracker.summary()
        assert s["total_calls"] == 3
        assert s["total_input"] == 4500
        assert s["total_output"] == 2300
        assert s["total_cost_usd"] > 0
        assert "scope" in s["by_stage"]
        assert s["by_stage"]["scope"]["calls"] == 1


class TestEvidenceIndex:
    def test_search_finds_relevant(self):
        from tools.evidence_index import EvidenceIndex

        idx = EvidenceIndex()
        idx.add(1, "奇富科技 2025年净利润 18亿元 同比增长")
        idx.add(2, "稀土氧化镨钕价格 38万元/吨 下跌34%")
        idx.add(3, "LPR 3.45% 维持不变")
        idx.build()

        assert idx.mode in ("embedding", "tfidf")
        assert idx.size == 3
        results = idx.search("净利润 增长", top_k=2)
        assert len(results) > 0
        assert results[0][0] == 1

        results2 = idx.search("稀土 价格", top_k=2)
        assert any(r[0] == 2 for r in results2)


class TestLLMExtractJson:
    def test_bare_json(self):
        from llm.client import LLMClient
        assert LLMClient._extract_json('{"key": "value"}') == {"key": "value"}

    def test_markdown_wrapped(self):
        from llm.client import LLMClient
        assert LLMClient._extract_json('```json\n{"key": "value"}\n```') == {"key": "value"}

    def test_prefix_json(self):
        from llm.client import LLMClient
        result = LLMClient._extract_json('好的，这是结果：\n{"key": "value"}')
        assert isinstance(result, dict)

    def test_salvage_truncated_insights(self):
        from llm.client import LLMClient
        broken = (
            '{"insights": ['
            '{"section": "技术节点", "claim": "17nm追赶并非简单追赶", '
            '"reasoning": "已有证据显示量产仍需验证", "evidence_ids": [1], "confidence": 0.42},'
            '{"section": "成本", "claim": "第二条被截断'
        )
        result = LLMClient._extract_json(broken)

        assert len(result["insights"]) == 1
        assert result["insights"][0]["section"] == "技术节点"

    def test_non_json_raises(self):
        from llm.client import LLMClient
        with pytest.raises(Exception):
            LLMClient._extract_json("这不是JSON")
