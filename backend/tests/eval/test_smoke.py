"""Smoke tests: real LLM output parsing (skipped without API key)."""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.smoke


def has_api_key() -> bool:
    for env_var in ("DEEPSEEK_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
                     "GOOGLE_API_KEY", "WELM_API_KEY"):
        if os.getenv(env_var):
            return True
    return False


skip_no_key = pytest.mark.skipif(not has_api_key(), reason="No LLM API key available")


@skip_no_key
class TestScopeSmoke:
    @pytest.mark.asyncio
    async def test_scope_parses(self):
        from schemas import AnalysisRun
        from orchestrator import Orchestrator
        import prompts

        run = AnalysisRun(query="奇富科技", provider="deepseek")
        orch = Orchestrator(run)
        orch.demo = False
        data = await orch._chat_json(prompts.scope_prompt("奇富科技"))
        assert isinstance(data, dict)
        assert "name" in data
        assert "industry" in data
        assert isinstance(data.get("sections"), list) and len(data["sections"]) > 0


@skip_no_key
class TestRedTeamSmoke:
    @pytest.mark.asyncio
    async def test_red_team_parses(self):
        from orchestrator import Orchestrator
        from schemas import AnalysisRun
        import prompts

        run = AnalysisRun(query="奇富科技", provider="deepseek")
        orch = Orchestrator(run)
        orch.demo = False
        data = await orch._chat_json_red(prompts.red_team_prompt(
            "利润增长由放款规模驱动",
            "在贷余额+12%但take rate持平",
            "[1] 奇富科技2025Q1在贷余额1,800亿 +12%",
            is_public=True, industry=False))
        assert isinstance(data, dict)
        challenges = data.get("challenges", [])
        assert len(challenges) == 9
        assert data.get("overall", "").lower() in ("solid", "incomplete", "refuted", "")
