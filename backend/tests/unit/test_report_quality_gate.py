"""Unit tests: 正文质量闸门 _looks_like_thinking_draft。

issue: 弱模型（deepseek-v4-flash）在 V2 全量证据一次性生成时输出整篇
「思考/规划草稿」（"让我先盘点…""现在设计报告结构"），连 prompt 的禁止规则
都复述进输出，最终报告正文全是草稿。闸门检测命中后强制最强模型重写。
"""
from __future__ import annotations

from orchestrator import _looks_like_thinking_draft


class TestThinkingDraftGate:
    def test_normal_report_passes(self):
        text = (
            "## 执行摘要\n"
            "2025年短剧行业产值约900亿元，免费模式占比2/3。\n\n"
            "## 核心发现\n"
            "### 付费引擎熄火\n"
            "付费用户增速跌破15%，IAA广告占71%。"
        )
        assert _looks_like_thinking_draft(text) is False

    def test_planning_draft_without_h2_detected(self):
        text = (
            "嗯，我需要为用户撰写一份深度分析报告。\n"
            "让我先理清任务要求和证据情况。\n"
            "市场规模类：900亿、533亿、千亿。\n"
            "用户类：8.51亿、7.18亿。\n"
            "现在设计报告结构：\n"
            "执行摘要（2段）：第一段回答发生了什么……"
        ) * 8
        assert len(text) > 800
        assert _looks_like_thinking_draft(text) is True

    def test_planning_cues_trigger_even_with_h2(self):
        text = (
            "## 执行摘要\n"
            "我先盘点证据池中的关键数字，然后再写正文。"
        )
        assert _looks_like_thinking_draft(text) is True

    def test_short_or_empty_text_not_draft(self):
        assert _looks_like_thinking_draft("") is False
        assert _looks_like_thinking_draft("  ") is False
        assert _looks_like_thinking_draft("短文本无标题") is False

    def test_long_text_with_h2_and_no_cues_passes(self):
        body = "## 执行摘要\n核心判断。\n## 基本事实\n表格内容。\n## 核心发现\n发现内容。\n" * 20
        assert len(body) > 800
        assert _looks_like_thinking_draft(body) is False
