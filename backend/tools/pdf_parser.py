"""A股年报 PDF 解析器 —— 使用 pdfplumber 提取文本和表格。

从 PDF 中自动定位关键财务报表页面（主要会计数据摘要、合并利润表、
合并资产负债表、合并现金流量表），提取表格并归一化为统一的中间格式。

设计要点：
  1. 纯规则+关键词定位，不依赖 LLM → 快速、确定性、可测试。
  2. 处理 pdfplumber 常见的表格瑕疵：跨行单元格(null行续)、空列、
     数值中的千分位逗号。
  3. 输出标准化的 ParsedTable（表名/列头/行列表），供下游
     financial_table_extractor 做指标映射。

典型 A 股年报结构：
  - 前 25 页：公司简介 + 主要会计数据和财务指标摘要表（3 年数据）
  - 中后部：合并资产负债表、合并利润表、合并现金流量表（2 年数据）
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass
class ParsedRow:
    """一行财务表格数据。label 为科目名称，values 按列序存数值字符串。"""
    label: str
    values: list[str | None] = field(default_factory=list)


@dataclass
class ParsedTable:
    """一张归一化的财务表格。"""
    name: str                              # summary / income / balance / cashflow
    headers: list[str] = field(default_factory=list)   # 列头
    rows: list[ParsedRow] = field(default_factory=list)
    page_range: tuple[int, int] = (0, 0)   # 起止页码 (1-based, inclusive)


@dataclass
class PdfParseResult:
    """PDF 解析总结果。"""
    filename: str = ""
    total_pages: int = 0
    company_name: str = ""
    stock_code: str = ""
    report_year: str = ""
    tables: list[ParsedTable] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 关键词集
# ---------------------------------------------------------------------------

_KW_SUMMARY = ["主要会计数据", "主要财务指标"]
_KW_INCOME = ["营业收入", "营业总收入", "净利润", "利润总额", "营业利润"]
_KW_BALANCE = ["资产总计", "负债合计", "流动资产", "货币资金", "所有者权益"]
_KW_CASHFLOW = ["经营活动产生", "投资活动产生", "筹资活动产生", "现金及现金等价物"]

# 数值清洗
_NUM_RE = re.compile(r"^-?[\d,]+(?:\.\d+)?$")
_PCT_RE = re.compile(r"^-?[\d.]+%$")


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _clean_cell(cell: str | None) -> str:
    """清洗单元格文本：去换行、去首尾空白。"""
    if cell is None:
        return ""
    return cell.replace("\n", "").strip()


def _is_numeric(s: str) -> bool:
    """判断是否为数值字符串（含千分位逗号、负号、百分号）。"""
    s = s.strip()
    if not s or s == "-" or s == "不适用":
        return False
    return bool(_NUM_RE.match(s) or _PCT_RE.match(s))


def _parse_number(s: str) -> float | None:
    """将数值字符串解析为 float。支持千分位逗号、负号、百分号。"""
    s = s.strip()
    if not s or s == "-" or s == "不适用":
        return None
    s = s.replace(",", "").replace("，", "")
    if s.endswith("%"):
        try:
            return float(s[:-1])
        except ValueError:
            return None
    try:
        return float(s)
    except ValueError:
        return None


def _merge_continuation_rows(raw_rows: list[list[str | None]]) -> list[list[str]]:
    """合并跨行单元格。

    pdfplumber 对多行文字的单元格会拆成多行，续行的 label 列通常为 None 或空。
    规则：
      1. 如果一行只有 label 文字没有数值 → 续行，追加到上一行的 label
      2. 如果一行有数值，但下一行只有 label 文字没有数值 → 将下一行的
         label 追加到当前行（前向合并）
    """
    merged: list[list[str]] = []
    for raw in raw_rows:
        cells = [_clean_cell(c) for c in raw]
        # 去掉全空行
        if not any(cells):
            continue
        # 找到第一个非空 cell
        first_nonempty = ""
        for c in cells:
            if c:
                first_nonempty = c
                break
        if not first_nonempty:
            continue
        # 判断是否有数值
        has_numeric = any(_is_numeric(c) for c in cells if c)
        # 判断是否为续行：没有数值，且前面有行可追加
        is_continuation = (
            merged
            and not has_numeric
            and not _is_numeric(first_nonempty)
        )
        if is_continuation:
            # 将文字追加到上一行的 label（第一个非空列）
            for i, c in enumerate(cells):
                if c and not _is_numeric(c):
                    # 找到上一行的 label 位置
                    for j, prev_c in enumerate(merged[-1]):
                        if prev_c and not _is_numeric(prev_c):
                            merged[-1][j] += c
                            break
                    break
        else:
            merged.append(cells)
    return merged


def _extract_label_values(merged_rows: list[list[str]], skip_pct_cols: bool = False) -> list[ParsedRow]:
    """从合并后的行中提取 label + values。

    列布局规则（适配 A 股年报常见格式）：
    - 第一个非空且非数值的列 → label
    - 其余非空且为数值的列 → values（按出现顺序）
    - 空列 / 纯装饰列被跳过

    skip_pct_cols: 如果为 True，跳过百分比列（适用于摘要表中的"同比增减"列）
    """
    rows: list[ParsedRow] = []
    for cells in merged_rows:
        label = ""
        values: list[str | None] = []
        for c in cells:
            if not c:
                continue
            if not label and not _is_numeric(c):
                label = c
            elif _is_numeric(c):
                # 跳过纯百分比列（摘要表中"增减"列）
                if skip_pct_cols and _PCT_RE.match(c.strip()):
                    continue
                values.append(c)
        if label:
            rows.append(ParsedRow(label=label, values=values))
    return rows


# ---------------------------------------------------------------------------
# 页面定位与表格提取
# ---------------------------------------------------------------------------

def _text_matches(text: str, keywords: list[str], min_hits: int = 2) -> bool:
    """文本中至少命中 min_hits 个关键词。"""
    hits = sum(1 for k in keywords if k in text)
    return hits >= min_hits


def _find_summary_tables(pdf: pdfplumber.PDF) -> list[ParsedTable]:
    """从前 25 页定位主要会计数据/主要财务指标摘要表。"""
    tables: list[ParsedTable] = []
    for i in range(min(25, len(pdf.pages))):
        page = pdf.pages[i]
        text = page.extract_text() or ""
        if not _text_matches(text, _KW_SUMMARY, min_hits=1):
            continue
        raw_tables = page.extract_tables() or []
        for raw in raw_tables:
            if not raw or len(raw) < 3:
                continue
            merged = _merge_continuation_rows(raw)
            # 先分析表头，确定哪些列是年度数据列、哪些是增减列
            headers, year_col_indices = _extract_summary_headers_with_indices(merged)
            # 只提取年度数据列的值
            rows = _extract_label_values_by_indices(merged, year_col_indices)
            # 摘要表至少应包含"营业收入"
            has_revenue = any("营业收入" in r.label for r in rows)
            if has_revenue:
                tables.append(ParsedTable(
                    name="summary", headers=headers, rows=rows,
                    page_range=(i + 1, i + 1),
                ))
    return tables


def _extract_summary_headers_with_indices(
    merged_rows: list[list[str]],
) -> tuple[list[str], list[int]]:
    """从摘要表的表头行提取年度列标签及其在原始行中的列索引。

    返回 (年度标签列表, 对应的原始列索引列表)。
    过滤掉"增减"、"变动"等描述列。
    """
    headers: list[str] = []
    indices: list[int] = []
    if not merged_rows:
        return headers, indices
    for row in merged_rows[:3]:
        found_headers: list[str] = []
        found_indices: list[int] = []
        for col_idx, c in enumerate(row):
            if not c:
                continue
            if re.search(r"20\d{2}", c):
                if any(kw in c for kw in ["增减", "变动", "比例"]):
                    continue
                found_headers.append(c)
                found_indices.append(col_idx)
        if found_headers:
            headers = found_headers
            indices = found_indices
            break
    return headers, indices


def _extract_label_values_by_indices(
    merged_rows: list[list[str]],
    value_col_indices: list[int],
) -> list[ParsedRow]:
    """按指定列索引提取 label + values（用于摘要表精确列映射）。

    如果 value_col_indices 为空，回退到全列扫描（兼容无法识别表头的情况）。
    """
    if not value_col_indices:
        return _extract_label_values(merged_rows, skip_pct_cols=True)

    rows: list[ParsedRow] = []
    for cells in merged_rows:
        label = ""
        # label 取第一个非空、非数值的 cell
        for c in cells:
            if c and not _is_numeric(c):
                label = c
                break
        if not label:
            continue
        values: list[str | None] = []
        for col_idx in value_col_indices:
            if col_idx < len(cells) and cells[col_idx] and _is_numeric(cells[col_idx]):
                values.append(cells[col_idx])
            else:
                # 列索引对不上时尝试向前/后偏移 1 列找数值
                found = False
                for offset in [-1, 1]:
                    alt = col_idx + offset
                    if 0 <= alt < len(cells) and cells[alt] and _is_numeric(cells[alt]):
                        values.append(cells[alt])
                        found = True
                        break
                if not found:
                    values.append(None)
        # 去尾部 None
        while values and values[-1] is None:
            values.pop()
        if values:
            rows.append(ParsedRow(label=label, values=values))
    return rows


def _find_statement_pages(pdf: pdfplumber.PDF, stmt_type: str) -> list[ParsedTable]:
    """在财务报表区域查找三大报表。

    stmt_type: 'income' | 'balance' | 'cashflow'

    A 股年报结构：前半部分是公司概况/经营分析，后半部分才是合并财务报表。
    为避免把"主营业务分析"中的利润数据误认为合并利润表，限制扫描范围为
    总页数的 30% 之后（且不少于 page 40）。
    """
    config = {
        "income": {
            "title_kw": ["合并利润表", "利润表"],
            "body_kw": _KW_INCOME,
            "name": "income",
        },
        "balance": {
            "title_kw": ["合并资产负债表", "资产负债表"],
            "body_kw": _KW_BALANCE,
            "name": "balance",
        },
        "cashflow": {
            "title_kw": ["合并现金流量表", "现金流量表"],
            "body_kw": _KW_CASHFLOW,
            "name": "cashflow",
        },
    }[stmt_type]

    # 只在后半部分扫描，避免把经营分析页面误判为财务报表
    start_page = max(int(len(pdf.pages) * 0.3), 40)
    # 第一遍：找包含标题关键词的页面（锚定页）
    anchor_pages: list[int] = []
    for i in range(start_page, len(pdf.pages)):
        text = pdf.pages[i].extract_text() or ""
        has_title = any(k in text for k in config["title_kw"])
        has_body = _text_matches(text, config["body_kw"], min_hits=2)
        if has_title and has_body:
            anchor_pages.append(i)

    if not anchor_pages:
        return []

    # 第二遍：从锚定页向后扩展，包含有大量特征关键词的续页
    # （报表可能跨 2-4 页，续页没有标题但有数据）
    first_anchor = anchor_pages[0]
    candidate_pages: list[int] = [first_anchor]
    for i in range(first_anchor + 1, min(first_anchor + 6, len(pdf.pages))):
        text = pdf.pages[i].extract_text() or ""
        # 续页需要有至少 1 个特征关键词，且不属于另一类报表的标题
        has_body = _text_matches(text, config["body_kw"], min_hits=1)
        # 排除明显属于其他报表的页面
        other_titles = []
        for other in ["income", "balance", "cashflow"]:
            if other != stmt_type:
                other_cfg = {
                    "income": ["合并利润表"],
                    "balance": ["合并资产负债表"],
                    "cashflow": ["合并现金流量表"],
                }[other]
                other_titles.extend(other_cfg)
        has_other_title = any(k in text for k in other_titles)
        if has_body and not has_other_title:
            candidate_pages.append(i)
        elif has_other_title:
            break  # 遇到下一张报表，停止扩展

    # 取第一组连续页面（合并报表优先于母公司报表）
    groups = [candidate_pages]
    tables: list[ParsedTable] = []
    for group in groups[:1]:
        all_rows: list[list[str | None]] = []
        headers: list[str] = []
        for pi in group:
            page = pdf.pages[pi]
            raw_tables = page.extract_tables() or []
            for raw in raw_tables:
                if not raw or len(raw) < 2:
                    continue
                # 检查这张子表是否属于目标报表类型
                # 将子表文本合并，检查是否包含特征关键词
                sub_text = " ".join(
                    _clean_cell(c) for row in raw for c in row if c
                )
                sub_has_body = _text_matches(sub_text, config["body_kw"], min_hits=2)
                if not sub_has_body:
                    # 同一页的其他报表子表，跳过
                    continue
                if not headers:
                    headers = _extract_statement_headers(raw)
                all_rows.extend(raw)

        merged = _merge_continuation_rows(all_rows)
        rows = _extract_label_values(merged)
        if rows:
            tables.append(ParsedTable(
                name=config["name"],
                headers=headers,
                rows=rows,
                page_range=(group[0] + 1, group[-1] + 1),
            ))
    return tables


def _extract_statement_headers(raw_table: list[list[str | None]]) -> list[str]:
    """从报表原始表格提取列头（年度信息）。"""
    headers: list[str] = []
    for row in raw_table[:3]:
        cells = [_clean_cell(c) for c in row]
        year_cells = [c for c in cells if c and ("年度" in c or "年末" in c or re.match(r"20\d{2}", c))]
        if year_cells:
            headers = year_cells
            break
    return headers


# ---------------------------------------------------------------------------
# 公司信息提取
# ---------------------------------------------------------------------------

_STOCK_CODE_RE = re.compile(r"(?:证券代码|股票代码|公司代码)[：:]\s*(\d{6})")
_COMPANY_NAME_RE = re.compile(r"([\u4e00-\u9fff]{2,20}(?:股份有限公司|有限公司))")
_REPORT_YEAR_RE = re.compile(r"(20\d{2})\s*年(?:度|年度)?(?:报告|年报)")


def _extract_meta(pdf: pdfplumber.PDF) -> dict[str, str]:
    """从前 5 页提取公司名称、股票代码、报告年度。"""
    meta: dict[str, str] = {}
    for i in range(min(5, len(pdf.pages))):
        text = pdf.pages[i].extract_text() or ""
        if not meta.get("stock_code"):
            m = _STOCK_CODE_RE.search(text)
            if m:
                meta["stock_code"] = m.group(1)
        if not meta.get("company_name"):
            m = _COMPANY_NAME_RE.search(text)
            if m:
                meta["company_name"] = m.group(1)
        if not meta.get("report_year"):
            m = _REPORT_YEAR_RE.search(text)
            if m:
                meta["report_year"] = m.group(1)
        if len(meta) >= 3:
            break
    return meta


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def parse_annual_report(pdf_path: str | Path) -> PdfParseResult:
    """解析 A 股年报 PDF，返回结构化中间结果。

    Args:
        pdf_path: PDF 文件路径

    Returns:
        PdfParseResult: 包含公司信息和所有识别到的财务表格
    """
    pdf_path = Path(pdf_path)
    result = PdfParseResult(filename=pdf_path.name)

    try:
        pdf = pdfplumber.open(str(pdf_path))
    except Exception as e:
        result.errors.append(f"无法打开 PDF: {e}")
        return result

    try:
        result.total_pages = len(pdf.pages)

        # 1. 提取公司元信息
        meta = _extract_meta(pdf)
        result.company_name = meta.get("company_name", "")
        result.stock_code = meta.get("stock_code", "")
        result.report_year = meta.get("report_year", "")

        # 2. 提取主要会计数据摘要表
        summaries = _find_summary_tables(pdf)
        result.tables.extend(summaries)

        # 3. 提取合并利润表
        income_tables = _find_statement_pages(pdf, "income")
        result.tables.extend(income_tables)

        # 4. 提取合并资产负债表
        balance_tables = _find_statement_pages(pdf, "balance")
        result.tables.extend(balance_tables)

        # 5. 提取合并现金流量表
        cashflow_tables = _find_statement_pages(pdf, "cashflow")
        result.tables.extend(cashflow_tables)

    except Exception as e:
        logger.error("PDF 解析异常: %s", e, exc_info=True)
        result.errors.append(f"解析异常: {e}")
    finally:
        pdf.close()

    return result


def parse_number(s: str) -> float | None:
    """公开的数值解析函数，供外部模块使用。"""
    return _parse_number(s)
