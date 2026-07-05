"""Unit tests: _normalize_narrative and related text post-processing."""
from __future__ import annotations

import pytest

from orchestrator import _normalize_narrative, _strip_llm_references, _extract_cited_ids


# --------------- _normalize_narrative ---------------


class TestNormalizeNarrative:
    def test_clean_numbered_h2(self):
        result = _normalize_narrative("## 1. 执行摘要\n内容\n## 3. 核心发现\n### 3.1 发现一\n内容")
        assert "## 执行摘要" in result
        assert "## 核心发现" in result
        assert "### 发现一" in result
        assert "### 1" not in result

    def test_single_hash_to_double(self):
        result = _normalize_narrative("# 执行摘要\n内容")
        assert result.startswith("## 执行摘要")

    def test_h4_to_h3(self):
        result = _normalize_narrative("## 核心发现\n#### 子标题\n内容")
        assert "### 子标题" in result

    def test_remove_residual_sections(self):
        result = _normalize_narrative("## 核心发现\n内容\n## 终审备注\n应删除\n## 风险\n保留")
        assert "终审备注" not in result
        assert "应删除" not in result
        assert "## 风险" in result
        assert "保留" in result

    def test_remove_research_methods(self):
        result = _normalize_narrative("## 执行摘要\n内容\n## 研究方法与数据说明\n应删除\n## 核心发现\n保留")
        assert "研究方法" not in result

    def test_empty_string(self):
        assert _normalize_narrative("") == ""

    def test_no_headings(self):
        result = _normalize_narrative("纯文本内容无标题")
        assert "纯文本" in result

    def test_multi_level_numbering(self):
        result = _normalize_narrative("## 1. 摘要\n### 1.1 子段\n### 1.2 子段\n## 2. 事实")
        assert "## 摘要" in result
        assert "### 子段" in result
        assert "## 事实" in result


class TestHistoryTrendDowngrade:
    def test_history_moves_into_facts(self):
        body = "## 基本事实\n事实\n\n## 核心发现\n发现\n\n## 历史趋势与拐点\n2023年100亿\n2024年120亿\n\n## 展望与关注点\n展望"
        result = _normalize_narrative(body)
        lines = result.split("\n")
        h2 = [l for l in lines if l.startswith("## ") and not l.startswith("### ")]
        assert "## 历史趋势与拐点" not in h2
        assert "### 历史趋势与拐点" in lines
        facts_idx = next(i for i, l in enumerate(lines) if l == "## 基本事实")
        hist_idx = next(i for i, l in enumerate(lines) if "历史趋势与拐点" in l)
        core_idx = next(i for i, l in enumerate(lines) if l == "## 核心发现")
        assert facts_idx < hist_idx < core_idx
        assert "2023年100亿" in result

    def test_duplicate_fact_sections_removed(self):
        body = (
            "## 执行摘要\n摘要一\n\n## 公司画像\n画像一\n\n### 财务概览\n财务一\n\n### 历史趋势\n趋势一\n\n"
            "## 执行摘要\n摘要二\n\n## 公司画像\n画像二\n\n### 财务概览\n财务二\n\n### 历史趋势\n趋势二\n\n"
            "## 核心发现\n发现"
        )
        result = _normalize_narrative(body)
        assert result.count("## 执行摘要") == 1
        assert result.count("## 公司画像") == 1
        assert result.count("### 财务概览") == 1
        assert result.count("### 历史趋势") == 1
        assert "## 核心发现" in result


# --------------- References ---------------


class TestStripLLMReferences:
    def test_strip_bold_reference_block(self):
        body = "## 核心发现\n正文[^1]。\n\n**参考文献**（182条）\n\n[^1]: 证据1\n[^2]: 证据2\n\n## 风险\n风险"
        result = _strip_llm_references(body)
        assert "**参考文献**" not in result
        assert "[^1]:" not in result
        assert "## 核心发现" in result
        assert "## 风险" in result

    def test_extract_cited_ids_excludes_definitions(self):
        text = "正文[^1]和[^2]。\n[^1]: 证据1定义\n[^2]: 证据2定义"
        ids = _extract_cited_ids(text)
        assert ids == {1, 2}

    def test_normalize_strips_references(self):
        body = "## 核心发现\n正文[^1]。\n\n**参考文献**（182条）\n\n[^1]: 证据1\n\n---\nfooter"
        result = _normalize_narrative(body)
        assert "**参考文献**" not in result
        assert "[^1]:" not in result
