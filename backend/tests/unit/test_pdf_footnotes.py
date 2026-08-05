"""Unit tests: PDF 导出的连续脚注合并。

issue: 相邻脚注 [^38][^41] 逐个转 <sup> 后，上标数字会连读成 "3841"；
表格里 [^35][^47][^36] 变成 "354736"（用户截图反馈）。
修复：先把连续脚注合并成单个 <sup>38, 41</sup>。
"""
from __future__ import annotations

from reporting.pdf_export import _inline


class TestFootnoteMerge:
    def test_consecutive_footnotes_merged_with_separator(self):
        out = _inline("盈利高度承压[^38][^41]。", {})
        assert "<sup>38, 41</sup>" in out
        assert "<sup>38</sup><sup>41</sup>" not in out

    def test_three_footnotes_merged(self):
        out = _inline("|规模 | 结论 | [^35][^47][^36] |", {})
        assert "<sup>35, 47, 36</sup>" in out

    def test_duplicate_ids_deduped(self):
        out = _inline("重复[^38][^38][^41]", {})
        assert "<sup>38, 41</sup>" in out

    def test_single_footnote_unchanged(self):
        out = _inline("单个脚注[^12]。", {})
        assert "<sup>12</sup>" in out

    def test_footnote_definition_not_touched(self):
        # 定义行 [^12]: ... 不应被转成 sup
        out = _inline("[^12]: 某来源 http://example.com", {})
        assert "<sup>" not in out
