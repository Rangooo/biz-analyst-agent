"""Contract tests: data-source adapters return correct shapes."""
from __future__ import annotations

import json
import os

import pytest

from schemas import SourceTier
from tools import finance_sources as fs


class FakeProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeHttpClient:
    last_headers = {}
    last_json = {}

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, headers=None, json=None):
        FakeHttpClient.last_headers = headers or {}
        FakeHttpClient.last_json = json or {}
        return FakeResponse({
            "results": [{
                "title": "API Result",
                "url": "https://example.com/api",
                "publishedDate": "2026-06-01T00:00:00.000Z",
                "highlights": ["api highlight"],
            }]
        })


class TestGeneralSearchContract:
    def test_adapter_maps_correctly(self, monkeypatch):
        import tools.search as search_mod
        monkeypatch.setenv("TAVILY_API_KEY", "test-key")
        monkeypatch.setattr(search_mod, "search",
                            lambda query, max_results=5, days=None: [
                                {"title": "Result A", "url": "https://example.com/a",
                                 "content": "Alpha content", "score": 0.9}
                            ])
        adapter = fs.TavilySerperAdapter()
        result = adapter.search("alpha", max_results=1, days=30)
        ev = result.evidences[0]
        assert adapter.available
        assert ev.source_title == "Result A"
        assert ev.source_url.endswith("/a")
        assert ev.source_type == "web"
        assert ev.tier == SourceTier.MEDIA


class TestSecAdapterContract:
    def test_filing_evidence_shape(self, monkeypatch):
        import tools.sec_edgar as sec_mod
        monkeypatch.setattr(sec_mod, "get_key_financials", lambda ticker: {
            "cik": "0000123456",
            "concepts": {
                "Revenue": [
                    {"fy": 2024, "fp": "FY", "val": 110, "end": "2024-12-31", "form": "10-K", "period_type": "annual"},
                    {"fy": 2025, "fp": "Q1", "val": 30, "end": "2025-03-31", "form": "10-Q", "period_type": "quarter"},
                ]
            },
            "annual_ends": ["2024-12-31"],
            "quarter_ends": ["2025-03-31"],
        })
        result = fs.SecEdgarAdapter().search("QFIN")
        assert all(e.tier == SourceTier.FILING and e.source_type == "filing" for e in result.evidences)
        assert "2025-03-31" in {e.as_of for e in result.evidences}
        assert "Revenue" in result.structured


class TestExaParserContract:
    def test_mcporter_parser(self, monkeypatch):
        payload = {
            "content": [{
                "text": (
                    "Title: First\nURL: https://example.com/1\nPublished: 2026-01-02\n"
                    "Highlights:\nfirst highlight\n---\n"
                    "Title: Second\nURL: https://example.com/2\nPublished: N/A\nHighlights:\nsecond highlight"
                )
            }]
        }
        calls = []

        def fake_run(*args, **kwargs):
            calls.append((args, kwargs))
            return FakeProcess(stdout=json.dumps(payload))

        monkeypatch.setattr(fs.subprocess, "run", fake_run)
        adapter = fs.ExaSearchAdapter()
        adapter._api_key = ""
        adapter._mcporter = "mcporter"
        adapter.available = True
        result = adapter.search("query", max_results=2, days=7)
        assert len(result.evidences) == 2
        assert result.evidences[0].published_at == "2026-01-02"
        assert "first highlight" in result.evidences[0].content

    def test_api_mode(self, monkeypatch):
        monkeypatch.setenv("EXA_API_KEY", "test-exa-key")
        monkeypatch.setattr(fs, "httpx", type("M", (), {"Client": FakeHttpClient})())
        # Re-init adapter with the patched env
        adapter = fs.ExaSearchAdapter()
        result = adapter.search("query", max_results=1, days=30)
        assert adapter.available
        assert FakeHttpClient.last_headers.get("x-api-key") == "test-exa-key"
        assert result.evidences[0].source_title == "API Result"


class TestSubprocessErrorMeta:
    def test_returns_error_gracefully(self, monkeypatch):
        monkeypatch.setattr(fs.subprocess, "run",
                            lambda *a, **kw: FakeProcess(returncode=1, stderr="backend unavailable"))
        adapter = fs.NeoDataAdapter()
        adapter.available = True
        result = adapter.search("query")
        assert "backend unavailable" in result.meta.get("error", "")
        assert result.evidences == []


class TestDataSourceCatalogContract:
    def test_list_adapters_exposes_configuration_metadata(self):
        fs._REGISTRY = {}
        rows = {row["name"]: row for row in fs.list_adapters()}

        assert rows["exa_search"]["priority"] == "required"
        assert rows["exa_search"]["primary_env"] == "EXA_API_KEY"
        assert "TAVILY_API_KEY" in rows["general_search"]["env_vars"]
        assert "SERPER_API_KEY" in rows["general_search"]["env_vars"]

        em_news = rows["em_news"]
        assert em_news["available"] is True
        assert em_news["input_type"] == "none"
        assert em_news["env_vars"] == []
        assert "无需配置" in em_news["status_note"]

    def test_runtime_env_allowlist_excludes_fake_eastmoney_key(self):
        envs = fs.runtime_config_envs()
        assert "EXA_API_KEY" in envs
        assert "WIND_MCP_TOOL" in envs
        assert "EASTMONEY_API_KEY" not in envs

    def test_script_adapters_read_runtime_env(self, monkeypatch, tmp_path):
        script = tmp_path / "query.py"
        script.write_text("print('{}')", encoding="utf-8")
        monkeypatch.setenv("NEODATA_SCRIPT", str(script))

        adapter = fs.NeoDataAdapter()
        assert adapter.available
        assert adapter.script == str(script)
