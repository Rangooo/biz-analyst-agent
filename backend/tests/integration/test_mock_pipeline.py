"""Integration test: full mock pipeline with fake LLM (from test_offline)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import orchestrator as orch_mod
from llm.client import get_client
from orchestrator import Orchestrator
from schemas import AnalysisRun, Evidence, SourceTier
import tools.search as search_tool
import tools.sec_edgar as sec_edgar


def _fake_chat_json(messages, provider=None, role="analyst", **kw):
    text = messages[-1]["content"]
    sys_text = messages[0]["content"] if messages else ""

    if "请判断并输出 JSON" in text:
        return {
            "name": "奇富科技", "kind": "company", "is_public": True,
            "ticker": "QFIN", "industry": "助贷 消费金融",
            "business_model": "撮合放贷+科技服务",
            "peers": ["乐信", "信也科技"], "leaders": [],
            "key_questions": ["利润增长是否靠放松风控"],
            "sections": ["财务表现", "经营指标", "风控质量", "战略动向"],
            "template_key": "fintech_lending",
        }
    if "请生成 6-8 条" in text:
        return ["奇富科技 2025 逾期率", "奇富科技 在贷余额"]
    if "请产出足够覆盖所有分析维度的洞察" in text or "请产出足够覆盖核心问题的洞察" in text:
        return {"insights": [
            {"section": "财务表现", "claim": "利润增长由规模驱动且风控稳健",
             "reasoning": (
                 "在贷余额扩张与营收增长同步，说明利润弹性主要来自规模释放；"
                 "但若单位 take rate 没有改善，增长质量仍取决于资产质量和拨备水平。"
                 "这意味着该判断需要用逾期率、核销率和拨备覆盖率交叉验证，而不能只看净利润增速。"
             ), "falsifiable_condition": "",
             "evidence_ids": [1], "confidence": 0.6},
        ]}
    if "九维挑战框架" in sys_text or "待审查的洞察" in text:
        return {
            "challenges": [
                {"dimension": "temporal", "severity": "low", "challenge": "数据略有滞后", "search_query": ""},
                {"dimension": "conflict_of_interest", "severity": "low", "challenge": "自报数据", "search_query": ""},
                {"dimension": "source_reliability", "severity": "low", "challenge": "来源为新闻", "search_query": ""},
                {"dimension": "logic", "severity": "none", "challenge": "", "search_query": ""},
                {"dimension": "external_consistency", "severity": "none", "challenge": "", "search_query": ""},
                {"dimension": "boundary", "severity": "none", "challenge": "", "search_query": ""},
                {"dimension": "alternative", "severity": "medium", "challenge": "可能靠少提拨备", "search_query": "奇富科技 拨备"},
                {"dimension": "missing_evidence", "severity": "medium", "challenge": "缺原始数据", "search_query": "奇富科技 拨备覆盖率"},
                {"dimension": "independence", "severity": "low", "challenge": "同源", "search_query": ""},
            ],
            "overall_assessment": "incomplete",
        }
    if "证据检索策略师" in sys_text:
        return {"support_queries": ["奇富科技 2025 拨备覆盖率 稳定"]}
    if "请综合判定" in text:
        is_refine = "自我迭代二次裁决" in sys_text
        return {"verdict": "questionable", "confidence": 0.55 if not is_refine else 0.6,
                "note": "数据不足维持存疑。", "resolved_dimensions": ["missing_evidence"] if is_refine else [],
                "open_dimensions": ["temporal"], "gap_explanation": "拨备数据缺失。"}
    if "检查报告中每个量化数字" in text or "数字审计" in sys_text:
        return {"passed": True, "issues": []}
    if "证据压缩员" in sys_text:
        return {"summary": "营收增长，拨备稳定。"}
    if "财务数据抽取员" in sys_text:
        return [{"period": "2024", "revenue": 40000000000, "net_income": 6000000000}]
    if "行业数据抽取员" in sys_text:
        return {"totals": [], "prices": []}
    if "事实层" in sys_text or "基本事实层" in sys_text or "【第一段" in sys_text:
        return {"content": "## 执行摘要\n\n利润增长由规模驱动。\n\n## 基本事实\n\n- 对象：奇富科技\n\n### 历史趋势\n\n| 指标 | 2024Q1 | 2025Q1 |\n|---|---|---|\n| 增速 | 15% | 12% |\n"}
    if "核心发现" in sys_text and "历史趋势" not in sys_text:
        return {"content": "## 核心发现\n\n利润增长靠规模驱动。"}
    if "风险" in sys_text and "展望" in sys_text and "核心发现" not in sys_text:
        return {"content": "## 风险与不确定性\n\n拨保数据存疑。"}
    if "终审质量评分员" in sys_text:
        return {"scores": {"完整性": 4, "逻辑性": 4, "专业性": 4, "数据性": 3, "创新性": 3, "实用性": 4, "合规性": 5, "可读性": 4},
                "total": 31, "issues": [], "passed": True}
    if "元反思分析师" in sys_text:
        return {"missed_challenges": [], "policy_updates": [], "industry_pattern_updates": [],
                "overall_assessment": "覆盖较全面。"}
    return {}


def _fake_chat_text(messages, provider=None, role="analyst", **kw):
    data = _fake_chat_json(messages, provider=provider, role=role, **kw)
    if isinstance(data, dict) and "content" in data:
        return data["content"]
    return "## 执行摘要\n\n利润增长由规模驱动，但需要用资产质量和拨备数据交叉验证。"


@pytest.mark.slow
class TestMockPipeline:
    def test_six_stages_complete(self, monkeypatch):
        monkeypatch.setattr(get_client(), "chat_json", _fake_chat_json)
        monkeypatch.setattr(get_client(), "chat", _fake_chat_text)
        monkeypatch.setattr(orch_mod, "save_run", lambda _run: None)
        monkeypatch.setattr(orch_mod.memory_store, "save_episodic", lambda _summary: None)
        monkeypatch.setattr(search_tool, "search",
                            lambda query, max_results=5, days=None: [
                                {"title": f"结果:{query}", "url": "https://example.com/x",
                                 "content": f"关于 {query} 的信息", "score": 0.8}
                            ])
        monkeypatch.setattr(search_tool, "has_search_backend", lambda: True)
        monkeypatch.setattr(sec_edgar, "get_key_financials", lambda ticker: {
            "cik": "0001", "ticker": ticker,
            "concepts": {"Revenue": [{"fy": 2025, "fp": "Q1", "val": 4500000000, "end": "2025-03-31", "form": "10-Q"}]},
            "annual_ends": [], "quarter_ends": ["2025-03-31"],
        })

        run = AnalysisRun(query="奇富科技", provider="deepseek")
        orch = Orchestrator(run)
        orch.demo = False
        orch.red_team_provider = run.red_team_provider or run.provider
        orch.reviewer_provider = run.reviewer_provider or run.provider

        class _FakeAdapter:
            def __init__(self, name):
                self.name = name
                self.available = True

            def search(self, query, kind="news", max_results=5, days=None):
                if self.name == "sec_edgar":
                    return SimpleNamespace(
                        evidences=[
                            Evidence(
                                content="奇富科技2025Q1营收45亿元，在贷余额同比增长12%，take rate持平。",
                                source_url="https://www.sec.gov/qfin",
                                source_title="Qifu SEC 6-K",
                                source_type="filing",
                                tier=SourceTier.FILING,
                                as_of="2025-03-31",
                            )
                        ],
                        structured={
                            "Revenue": [
                                {"fy": 2025, "fp": "Q1", "val": 4500000000, "end": "2025-03-31", "form": "10-Q"},
                            ],
                            "NetIncome": [
                                {"fy": 2025, "fp": "Q1", "val": 600000000, "end": "2025-03-31", "form": "10-Q"},
                            ],
                        },
                    )
                return SimpleNamespace(
                    evidences=[
                        Evidence(
                            content=f"关于 {query} 的补充证据：拨备覆盖率稳定，逾期率需要结合核销观察。",
                            source_url=f"https://example.com/{self.name}",
                            source_title=f"{self.name} result",
                            source_type="web",
                            tier=SourceTier.THIRD_PARTY,
                            published_at="2025-05-22",
                        )
                    ],
                    structured={},
                )

        adapters = {
            "sec_edgar": _FakeAdapter("sec_edgar"),
            "general_search": _FakeAdapter("general_search"),
        }
        orch._adapter = lambda name: adapters.get(name)

        async def _run():
            async for _ in orch.run_pipeline():
                pass

        asyncio.run(_run())

        assert run.status == "done"
        assert run.narrative_md
        assert any(i.falsifications for i in run.insights), "证伪闭环未触发"
        # Check nine-dimension structure
        fr = next((i.falsifications[0] for i in run.insights if i.falsifications), None)
        if fr:
            assert hasattr(fr, "challenges")
            assert len(fr.challenges) == 9
