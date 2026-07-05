"""财报表结构化提取器 —— 从 pdf_parser 的中间结果中提取标准化财务指标。

将 ParsedTable 中的科目行映射为 run.financials 兼容的结构化数据，
输出 list[dict]（与 SEC EDGAR / LLM 抽取路径输出格式一致）。

指标映射策略：
  1. 优先使用"主要会计数据摘要表"（前 25 页，包含 3 年完整数据）。
  2. 用合并利润表/资产负债表/现金流量表补充摘要表不含的指标。
  3. 关键词模糊匹配：同一指标在不同公司年报中可能有不同表述
     （如"营业总收入"vs"营业收入"），使用多候选词 + 优先级。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from tools.pdf_parser import PdfParseResult, ParsedTable, ParsedRow, parse_number

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 指标映射配置
# ---------------------------------------------------------------------------

@dataclass
class MetricDef:
    """一个标准化指标的定义。"""
    key: str                    # 输出字段名（如 revenue, net_income）
    candidates: list[str]       # 候选科目关键词（按优先级排列）
    negate: bool = False        # 是否取反（如支出项目转为正值）


# 核心指标集
CORE_METRICS = [
    MetricDef("revenue", ["营业收入", "营业总收入"]),
    MetricDef("net_income", [
        "归属于上市公司股东的净利润",
        "归属于母公司所有者的净利润",
        "归属于母公司股东的净利润",
        "净利润",
    ]),
    MetricDef("net_income_deducted", [
        "归属于上市公司股东的扣除非经常性损益的净利润",
        "扣除非经常性损益的净利润",
        "扣除非经常性损益后的净利润",
    ]),
    MetricDef("operating_income", ["营业利润"]),
    MetricDef("gross_profit", ["毛利润", "毛利"]),
    MetricDef("total_profit", ["利润总额"]),
    MetricDef("total_assets", ["资产总额", "资产总计", "总资产"]),
    MetricDef("total_equity", [
        "归属于上市公司股东的净资产",
        "归属于母公司所有者权益合计",
        "所有者权益合计",
    ]),
    MetricDef("operating_cashflow", [
        "经营活动产生的现金流量净额",
    ]),
    MetricDef("eps_basic", ["基本每股收益"]),
    MetricDef("eps_diluted", ["稀释每股收益"]),
    MetricDef("roe_weighted", ["加权平均净资产收益率"]),
]

# 利润表补充指标
INCOME_METRICS = [
    MetricDef("cost_of_revenue", ["营业成本", "营业总成本"]),
    MetricDef("selling_expense", ["销售费用"]),
    MetricDef("admin_expense", ["管理费用"]),
    MetricDef("rd_expense", ["研发费用"]),
    MetricDef("finance_expense", ["财务费用"]),
    MetricDef("operating_income", ["营业利润"]),
    MetricDef("total_profit", ["利润总额"]),
    MetricDef("income_tax", ["所得税费用"]),
]

# 资产负债表补充指标
BALANCE_METRICS = [
    MetricDef("total_current_assets", ["流动资产合计"]),
    MetricDef("total_noncurrent_assets", ["非流动资产合计"]),
    MetricDef("total_assets", ["资产总计", "资产总额"]),
    MetricDef("total_current_liabilities", ["流动负债合计"]),
    MetricDef("total_noncurrent_liabilities", ["非流动负债合计"]),
    MetricDef("total_liabilities", ["负债合计", "负债总额"]),
    MetricDef("total_equity", ["所有者权益合计", "股东权益合计"]),
]

# 现金流量表补充指标
CASHFLOW_METRICS = [
    MetricDef("operating_cashflow", ["经营活动产生的现金流量净额"]),
    MetricDef("investing_cashflow", ["投资活动产生的现金流量净额"]),
    MetricDef("financing_cashflow", ["筹资活动产生的现金流量净额"]),
    MetricDef("cash_end", ["期末现金及现金等价物余额"]),
]


# ---------------------------------------------------------------------------
# 匹配逻辑
# ---------------------------------------------------------------------------

def _match_label(label: str, candidates: list[str]) -> bool:
    """判断科目标签是否匹配任一候选词。

    采用包含匹配（非精确匹配），因为年报中的科目名称可能有额外修饰语。
    按候选词长度倒序匹配，优先匹配最具体的描述。

    特殊处理：忽略括号中的单位标注（如"（元）"、"(元/股)"）
    """
    label_clean = re.sub(r"[（(][^)）]*[)）]", "", label).replace(" ", "")
    for cand in sorted(candidates, key=len, reverse=True):
        cand_clean = cand.replace(" ", "").replace("（", "(").replace("）", ")")
        if cand_clean in label_clean:
            return True
    return False


def _find_metric_value(
    rows: list[ParsedRow],
    metric: MetricDef,
    col_index: int = 0,
) -> float | None:
    """从表格行中查找指标值。

    col_index: 目标列索引（0=最近一年, 1=上年, 2=更早年）
    """
    for row in rows:
        if _match_label(row.label, metric.candidates):
            if col_index < len(row.values):
                val = parse_number(row.values[col_index] or "")
                if val is not None and metric.negate:
                    val = -val
                return val
    return None


# ---------------------------------------------------------------------------
# 年度信息提取
# ---------------------------------------------------------------------------

_YEAR_RE = re.compile(r"(20\d{2})")


def _extract_years_from_headers(headers: list[str]) -> list[str]:
    """从表头中提取年度标签。"""
    years: list[str] = []
    for h in headers:
        m = _YEAR_RE.search(h)
        if m:
            years.append(m.group(1))
    return years


# ---------------------------------------------------------------------------
# 主提取逻辑
# ---------------------------------------------------------------------------

@dataclass
class ExtractedFinancials:
    """提取结果。"""
    company_name: str = ""
    stock_code: str = ""
    report_year: str = ""
    # 与 run.financials 兼容的格式
    financials: list[dict] = field(default_factory=list)
    # 补充指标（不在 run.financials 标准字段中）
    supplementary: dict = field(default_factory=dict)
    # 诊断信息
    metrics_found: list[str] = field(default_factory=list)
    metrics_missing: list[str] = field(default_factory=list)
    source_tables: list[str] = field(default_factory=list)


def extract_financials(parse_result: PdfParseResult) -> ExtractedFinancials:
    """从 PDF 解析结果中提取标准化财务指标。

    优先使用摘要表（3 年数据），然后用三大报表补充。
    输出与 run.financials 格式一致的 list[dict]。
    """
    result = ExtractedFinancials(
        company_name=parse_result.company_name,
        stock_code=parse_result.stock_code,
        report_year=parse_result.report_year,
    )

    # 按表名分组
    tables_by_name: dict[str, list[ParsedTable]] = {}
    for t in parse_result.tables:
        tables_by_name.setdefault(t.name, []).append(t)

    # --- 策略 1：从摘要表提取（通常有 3 年数据） ---
    summary_tables = tables_by_name.get("summary", [])
    if summary_tables:
        result.source_tables.append("summary")
        # 合并所有摘要表的行（有些公司分两张表）
        all_summary_rows: list[ParsedRow] = []
        years: list[str] = []
        for st in summary_tables:
            all_summary_rows.extend(st.rows)
            if not years:
                years = _extract_years_from_headers(st.headers)

        if not years:
            # 兜底：从 report_year 推算
            if parse_result.report_year:
                ry = int(parse_result.report_year)
                years = [str(ry), str(ry - 1), str(ry - 2)]

        # 为每个年度构建 financials 行
        for col_idx, year in enumerate(years):
            row_data: dict = {"period": year}
            for metric in CORE_METRICS:
                val = _find_metric_value(all_summary_rows, metric, col_idx)
                if val is not None:
                    row_data[metric.key] = val
                    if metric.key not in result.metrics_found:
                        result.metrics_found.append(metric.key)
            # 至少有营收或净利润才保留
            if "revenue" in row_data or "net_income" in row_data:
                result.financials.append(row_data)

    # --- 策略 2：从利润表/资产负债表/现金流量表补充 ---
    for stmt_name, metrics in [
        ("income", INCOME_METRICS),
        ("balance", BALANCE_METRICS),
        ("cashflow", CASHFLOW_METRICS),
    ]:
        stmt_tables = tables_by_name.get(stmt_name, [])
        if not stmt_tables:
            continue
        result.source_tables.append(stmt_name)
        stmt = stmt_tables[0]
        stmt_years = _extract_years_from_headers(stmt.headers)

        for col_idx, year in enumerate(stmt_years[:2]):  # 报表通常只有 2 年
            # 找到对应年度的 financials 行
            fin_row = next((f for f in result.financials if f.get("period") == year), None)
            if fin_row is None:
                fin_row = {"period": year}
                result.financials.append(fin_row)

            for metric in metrics:
                # 只在该指标尚未有值时补充
                if metric.key not in fin_row or fin_row[metric.key] is None:
                    val = _find_metric_value(stmt.rows, metric, col_idx)
                    if val is not None:
                        fin_row[metric.key] = val
                        if metric.key not in result.metrics_found:
                            result.metrics_found.append(metric.key)

    # 按年度排序（升序）
    result.financials.sort(key=lambda r: r.get("period", ""))

    # 统计缺失指标
    all_keys = {m.key for m in CORE_METRICS}
    result.metrics_missing = [k for k in all_keys if k not in result.metrics_found]

    return result
