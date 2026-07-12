"""Contract tests for the Firecrawl v2 adapter."""
from tools import firecrawl_adapter as fc


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeClient:
    calls = []

    def __init__(self, *args, **kwargs):
        self.headers = kwargs.get("headers", {})

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, url, json):
        self.calls.append((url, self.headers, json))
        if url.endswith("/scrape"):
            return FakeResponse({"success": True, "data": {"markdown": "# Example"}})
        return FakeResponse({"success": True, "data": {"web": [{
            "title": "Example", "url": "https://example.com", "description": "Result"
        }]}})


def test_keyless_without_api_key(monkeypatch, tmp_path):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.setattr(fc, "_budget", fc._KeylessBudget(tmp_path / "usage.json"))
    monkeypatch.setattr(fc.httpx, "Client", FakeClient)
    FakeClient.calls.clear()
    assert fc.FirecrawlAdapter().available
    assert fc.search_web("test")[0]["url"] == "https://example.com"
    assert "Authorization" not in FakeClient.calls[0][1]
    assert fc.firecrawl_budget_status()["used"] == 2


def test_keyless_stops_at_monthly_limit(monkeypatch, tmp_path):
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    budget = fc._KeylessBudget(tmp_path / "usage.json")
    budget.record(fc.KEYLESS_MONTHLY_CREDITS)
    monkeypatch.setattr(fc, "_budget", budget)
    monkeypatch.setattr(fc.httpx, "Client", FakeClient)
    FakeClient.calls.clear()
    assert not fc.FirecrawlAdapter().available
    assert fc.scrape_url("https://example.com") == ""
    assert FakeClient.calls == []


def test_v2_search_and_scrape(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    monkeypatch.setattr(fc.httpx, "Client", FakeClient)
    FakeClient.calls.clear()

    result = fc.FirecrawlAdapter().search("test", max_results=1)
    assert result.evidences[0].source_url == "https://example.com"
    assert fc.scrape_url("https://example.com") == "# Example"
    assert all(url.startswith("https://api.firecrawl.dev/v2/") for url, _, _ in FakeClient.calls)
    assert all(headers["Authorization"] == "Bearer fc-test" for _, headers, _ in FakeClient.calls)
