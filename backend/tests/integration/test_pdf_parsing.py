"""测试 pdf_parser 底层解析功能。

使用两个真实 A 股年报 PDF 作为集成测试数据源。
如果 PDF 文件不存在，跳过该测试（CI 环境不含大文件）。
"""
from __future__ import annotations

import os
import pytest
from pathlib import Path

# 真实 PDF 路径
PDF_ZHONGJI = Path("D:/12041853.pdf")   # 中际旭创 300308 2025 年报
PDF_YONGHUI = Path("D:/12102322.pdf")   # 永辉超市 601933 2025 年报

skip_no_pdf = pytest.mark.skipif(
    not PDF_ZHONGJI.exists() or not PDF_YONGHUI.exists(),
    reason="真实 PDF 文件不存在，跳过 PDF 集成测试",
)

# PDF 解析测试较慢（每个 PDF 200+ 页），标记为 slow
pytestmark = pytest.mark.slow

# ---------------------------------------------------------------------------
# 单元测试：纯函数
# ---------------------------------------------------------------------------

class TestPureHelpers:
    """测试不依赖 PDF 文件的纯工具函数。"""

    def test_is_numeric_positive(self):
        from tools.pdf_parser import _is_numeric
        assert _is_numeric("123")
        assert _is_numeric("-456.78")
        assert _is_numeric("1,234,567.89")
        assert _is_numeric("-1,000")
        assert _is_numeric("12.34%")

    def test_is_numeric_negative(self):
        from tools.pdf_parser import _is_numeric
        assert not _is_numeric("")
        assert not _is_numeric("-")
        assert not _is_numeric("不适用")
        assert not _is_numeric("营业收入")
        assert not _is_numeric("2025年")

    def test_parse_number(self):
        from tools.pdf_parser import parse_number
        assert parse_number("1,234,567.89") == pytest.approx(1234567.89)
        assert parse_number("-456.78") == pytest.approx(-456.78)
        assert parse_number("12.34%") == pytest.approx(12.34)
        assert parse_number("-") is None
        assert parse_number("不适用") is None
        assert parse_number("") is None

    def test_clean_cell(self):
        from tools.pdf_parser import _clean_cell
        assert _clean_cell(None) == ""
        assert _clean_cell("  hello\nworld  ") == "helloworld"

    def test_merge_continuation_rows(self):
        from tools.pdf_parser import _merge_continuation_rows
        # 模拟：第一行有数值，第二行只有文字（续行）
        rows = [
            ["归属于上市公司股东", "100", "200"],
            ["的净利润", None, None],
            ["营业收入", "300", "400"],
        ]
        merged = _merge_continuation_rows(rows)
        assert len(merged) == 2
        assert "归属于上市公司股东" in merged[0][0]
        assert "的净利润" in merged[0][0]
        assert merged[1][0] == "营业收入"

    def test_text_matches(self):
        from tools.pdf_parser import _text_matches
        text = "公司的营业收入大幅增长，净利润同比提升"
        assert _text_matches(text, ["营业收入", "净利润", "利润总额"], min_hits=2)
        assert not _text_matches(text, ["资产总计", "负债合计"], min_hits=1)


# ---------------------------------------------------------------------------
# 集成测试：真实 PDF
# ---------------------------------------------------------------------------

@skip_no_pdf
class TestZhongjiPdf:
    """中际旭创 300308 2025 年报解析测试。"""

    @pytest.fixture(scope="class")
    def result(self):
        from tools.pdf_parser import parse_annual_report
        return parse_annual_report(str(PDF_ZHONGJI))

    def test_meta(self, result):
        assert result.stock_code == "300308"
        assert "旭创" in result.company_name
        assert result.report_year == "2025"
        assert result.total_pages > 200

    def test_no_errors(self, result):
        assert len(result.errors) == 0

    def test_summary_table_found(self, result):
        summaries = [t for t in result.tables if t.name == "summary"]
        assert len(summaries) >= 1
        s = summaries[0]
        assert s.page_range[0] <= 15  # 摘要表在前 15 页

    def test_summary_has_3_years(self, result):
        summaries = [t for t in result.tables if t.name == "summary"]
        s = summaries[0]
        assert len(s.headers) == 3
        # 表头应含 2025、2024、2023
        years_in_headers = [h for h in s.headers if "2025" in h or "2024" in h or "2023" in h]
        assert len(years_in_headers) == 3

    def test_summary_revenue_row(self, result):
        from tools.pdf_parser import parse_number
        summaries = [t for t in result.tables if t.name == "summary"]
        s = summaries[0]
        revenue_rows = [r for r in s.rows if "营业收入" in r.label]
        assert len(revenue_rows) >= 1
        # 2025 年营收 ≈ 382.4 亿
        val = parse_number(revenue_rows[0].values[0])
        assert val is not None
        assert 35e9 < val < 45e9

    def test_income_table_found(self, result):
        incomes = [t for t in result.tables if t.name == "income"]
        assert len(incomes) >= 1
        inc = incomes[0]
        # 利润表应在后半部分
        assert inc.page_range[0] > 50
        # 应包含"营业总收入"或"营业收入"行
        labels = [r.label for r in inc.rows]
        assert any("营业" in l and "收入" in l for l in labels)

    def test_income_not_contaminated(self, result):
        """利润表不应混入资产负债表数据。"""
        incomes = [t for t in result.tables if t.name == "income"]
        if not incomes:
            pytest.skip("利润表未找到")
        inc = incomes[0]
        labels = [r.label for r in inc.rows]
        # 不应包含资产负债表的特征词
        for label in labels:
            assert "货币资金" not in label, f"利润表混入了资产负债表行: {label}"
            assert "应收账款" not in label, f"利润表混入了资产负债表行: {label}"

    def test_balance_table_found(self, result):
        balances = [t for t in result.tables if t.name == "balance"]
        assert len(balances) >= 1

    def test_cashflow_table_found(self, result):
        cashflows = [t for t in result.tables if t.name == "cashflow"]
        assert len(cashflows) >= 1


@skip_no_pdf
class TestYonghuiPdf:
    """永辉超市 601933 2025 年报解析测试。"""

    @pytest.fixture(scope="class")
    def result(self):
        from tools.pdf_parser import parse_annual_report
        return parse_annual_report(str(PDF_YONGHUI))

    def test_meta(self, result):
        assert result.stock_code == "601933"
        assert "永辉" in result.company_name
        assert result.report_year == "2025"
        assert result.total_pages > 200

    def test_no_errors(self, result):
        assert len(result.errors) == 0

    def test_summary_revenue(self, result):
        from tools.pdf_parser import parse_number
        summaries = [t for t in result.tables if t.name == "summary"]
        assert len(summaries) >= 1
        s = summaries[0]
        revenue_rows = [r for r in s.rows if "营业收入" in r.label]
        assert len(revenue_rows) >= 1
        # 2025 年营收 ≈ 535.08 亿
        val = parse_number(revenue_rows[0].values[0])
        assert val is not None
        assert 50e9 < val < 60e9

    def test_all_four_tables_found(self, result):
        table_names = {t.name for t in result.tables}
        assert "summary" in table_names
        assert "income" in table_names
        assert "balance" in table_names
        assert "cashflow" in table_names


# ---------------------------------------------------------------------------
# 集成测试：financial_table_extractor
# ---------------------------------------------------------------------------

@skip_no_pdf
class TestZhongjiExtractor:
    """中际旭创 300308 财报结构化提取。"""

    @pytest.fixture(scope="class")
    def extracted(self):
        from tools.pdf_parser import parse_annual_report
        from tools.financial_table_extractor import extract_financials
        pr = parse_annual_report(str(PDF_ZHONGJI))
        return extract_financials(pr)

    def test_3_years_data(self, extracted):
        assert len(extracted.financials) == 3

    def test_years_correct(self, extracted):
        periods = sorted(f["period"] for f in extracted.financials)
        assert periods == ["2023", "2024", "2025"]

    def test_revenue_2025(self, extracted):
        f2025 = next(f for f in extracted.financials if f["period"] == "2025")
        # 2025 年营业收入 382.40 亿
        assert 38e9 < f2025["revenue"] < 39e9

    def test_net_income_2025(self, extracted):
        f2025 = next(f for f in extracted.financials if f["period"] == "2025")
        # 2025 年净利润 107.97 亿 = 10,797,254,300
        assert 10.5e9 < f2025["net_income"] < 11.5e9

    def test_operating_cashflow_2025(self, extracted):
        f2025 = next(f for f in extracted.financials if f["period"] == "2025")
        # 2025 年经营现金流 108.96 亿
        assert 10.5e9 < f2025["operating_cashflow"] < 11.5e9

    def test_total_assets_2025(self, extracted):
        f2025 = next(f for f in extracted.financials if f["period"] == "2025")
        # 2025 年总资产 452.89 亿
        assert 44e9 < f2025["total_assets"] < 47e9

    def test_eps_basic(self, extracted):
        f2025 = next(f for f in extracted.financials if f["period"] == "2025")
        assert f2025["eps_basic"] == pytest.approx(9.80)

    def test_operating_income_from_income_stmt(self, extracted):
        """利润表补充了营业利润。"""
        f2025 = next(f for f in extracted.financials if f["period"] == "2025")
        assert "operating_income" in f2025
        # 2025 年营业利润 ≈ 135.97 亿
        assert 13e9 < f2025["operating_income"] < 14e9

    def test_source_tables(self, extracted):
        assert "summary" in extracted.source_tables
        assert "income" in extracted.source_tables

    def test_revenue_yoy_growth(self, extracted):
        """验证营收年度环比增长方向正确。"""
        fins = sorted(extracted.financials, key=lambda f: f["period"])
        for i in range(1, len(fins)):
            # 中际旭创近三年营收持续增长
            assert fins[i]["revenue"] > fins[i - 1]["revenue"]


@skip_no_pdf
class TestYonghuiExtractor:
    """永辉超市 601933 财报结构化提取。"""

    @pytest.fixture(scope="class")
    def extracted(self):
        from tools.pdf_parser import parse_annual_report
        from tools.financial_table_extractor import extract_financials
        pr = parse_annual_report(str(PDF_YONGHUI))
        return extract_financials(pr)

    def test_3_years_data(self, extracted):
        assert len(extracted.financials) == 3

    def test_revenue_2025(self, extracted):
        f2025 = next(f for f in extracted.financials if f["period"] == "2025")
        # 2025 年营业收入 535.08 亿
        assert 53e9 < f2025["revenue"] < 54e9

    def test_negative_net_income(self, extracted):
        """永辉超市近三年亏损。"""
        for f in extracted.financials:
            assert f["net_income"] < 0, f"期待亏损但得到正值: period={f['period']}"

    def test_net_income_2025(self, extracted):
        f2025 = next(f for f in extracted.financials if f["period"] == "2025")
        # 2025 年净利润 -25.52 亿 = -2,552,341,936.85
        assert -2.6e9 < f2025["net_income"] < -2.5e9

    def test_total_assets_declining(self, extracted):
        """永辉总资产逐年下降。"""
        fins = sorted(extracted.financials, key=lambda f: f["period"])
        for i in range(1, len(fins)):
            if "total_assets" in fins[i] and "total_assets" in fins[i - 1]:
                assert fins[i]["total_assets"] < fins[i - 1]["total_assets"]

    def test_operating_cashflow_positive(self, extracted):
        """永辉经营现金流为正（超市模式）。"""
        for f in extracted.financials:
            if "operating_cashflow" in f:
                assert f["operating_cashflow"] > 0

    def test_source_includes_all(self, extracted):
        for src in ["summary", "income", "balance", "cashflow"]:
            assert src in extracted.source_tables


# ---------------------------------------------------------------------------
# 单元测试：extractor 匹配逻辑
# ---------------------------------------------------------------------------

class TestMatchLabel:
    """测试科目名称模糊匹配。"""

    def test_exact_match(self):
        from tools.financial_table_extractor import _match_label
        assert _match_label("营业收入", ["营业收入"])

    def test_with_unit_annotation(self):
        from tools.financial_table_extractor import _match_label
        assert _match_label("营业收入（元）", ["营业收入"])
        assert _match_label("基本每股收益（元/股）", ["基本每股收益"])

    def test_with_prefix(self):
        from tools.financial_table_extractor import _match_label
        assert _match_label("归属于上市公司股东的净利润", ["净利润"])
        assert _match_label("归属于上市公司股东的净利润（元）", [
            "归属于上市公司股东的净利润"
        ])

    def test_no_match(self):
        from tools.financial_table_extractor import _match_label
        assert not _match_label("营业收入", ["净利润"])
        assert not _match_label("管理费用", ["销售费用"])

    def test_priority_matching(self):
        """长候选词优先匹配。"""
        from tools.financial_table_extractor import _match_label
        # 两个候选词都能匹配，但更具体的应优先
        assert _match_label(
            "归属于上市公司股东的净利润",
            ["归属于上市公司股东的净利润", "净利润"],
        )
