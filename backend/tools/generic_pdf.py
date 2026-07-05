"""通用 PDF 文本/表格提取器 —— 用 pdfplumber 提取任意 PDF 的全文和表格。

与 pdf_parser.py 的区别：
- pdf_parser.py：A 股年报专用，硬编码定位三大报表+摘要表
- generic_pdf.py：通用模式，提取所有页面文本+所有表格，不做结构识别

适用场景：
- 用户上传研报/招股书/招股书/行业报告等任意 PDF
- agent 搜索时发现 PDF 链接，自动下载后调用本模块
- 输出：分块后的文本（每页一段）+ 提取的表格（按页组织）
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import pdfplumber

logger = logging.getLogger(__name__)


@dataclass
class PdfChunk:
    """一个 PDF 文本块：连续一页的纯文本。"""
    text: str
    page_num: int
    char_count: int


@dataclass
class PdfTableChunk:
    """一个 PDF 表格块：来自某一页的某个表格。"""
    rows: list[list[str]]
    page_num: int
    table_idx: int  # 当前页第几个表格


@dataclass
class GenericPdfResult:
    """通用 PDF 解析结果。"""
    text_chunks: list[PdfChunk] = field(default_factory=list)
    table_chunks: list[PdfTableChunk] = field(default_factory=list)
    total_pages: int = 0
    title: str = ""
    file_size: int = 0
    error: str = ""

    def to_markdown(self, max_pages: int | None = None) -> str:
        """把结果转成 markdown 文本：每页用 ## 页码 分隔，表格用 markdown table。"""
        lines: list[str] = []
        if self.title:
            lines.append(f"# {self.title}\n")
        lines.append(f"_共 {self.total_pages} 页 · {len(self.text_chunks)} 个文本块 · {len(self.table_chunks)} 个表格_\n")

        # 按页号合并文本块和表格块
        page_to_text: dict[int, list[str]] = {}
        page_to_tables: dict[int, list[PdfTableChunk]] = {}
        for ch in self.text_chunks:
            page_to_text.setdefault(ch.page_num, []).append(ch.text)
        for tb in self.table_chunks:
            page_to_tables.setdefault(tb.page_num, []).append(tb)

        page_nums = sorted(set(page_to_text) | set(page_to_tables))
        if max_pages:
            page_nums = page_nums[:max_pages]

        for pn in page_nums:
            lines.append(f"\n## 第 {pn} 页\n")
            for t in page_to_text.get(pn, []):
                lines.append(t.strip())
                lines.append("")
            for tb in page_to_tables.get(pn, []):
                if not tb.rows:
                    continue
                # 转为 markdown table
                header = tb.rows[0] if tb.rows else []
                lines.append("| " + " | ".join(_clean(c) for c in header) + " |")
                lines.append("|" + "|".join("---" for _ in header) + "|")
                for row in tb.rows[1:]:
                    lines.append("| " + " | ".join(_clean(c) for c in row) + " |")
                lines.append("")
        return "\n".join(lines)


def _clean(s) -> str:
    """清理表格 cell：去换行、多余空格、管道符转义。"""
    if s is None:
        return ""
    s = str(s).replace("\n", " ").replace("\r", " ").replace("|", "/")
    s = re.sub(r"\s+", " ", s).strip()
    return s or " "


def _is_scanned_page(text: str) -> bool:
    """启发式：扫描页通常文本极少（< 50 字符）。"""
    return len(text.strip()) < 50


def parse_generic_pdf(
    pdf_path: str | Path,
    max_pages: int | None = None,
    include_tables: bool = True,
) -> GenericPdfResult:
    """解析任意 PDF 提取文本块和表格。

    Args:
        pdf_path: 本地 PDF 文件路径
        max_pages: 最多处理多少页（None=全部）
        include_tables: 是否提取表格

    Returns:
        GenericPdfResult：含文本块列表、表格块列表、总页数等
    """
    result = GenericPdfResult()
    path = Path(pdf_path)
    if not path.exists():
        result.error = f"文件不存在: {pdf_path}"
        return result
    result.file_size = path.stat().st_size
    # 文件名作为默认标题（去扩展名）
    result.title = path.stem[:80]

    try:
        with pdfplumber.open(str(path)) as pdf:
            total = len(pdf.pages)
            result.total_pages = total
            page_nums = range(total)
            if max_pages:
                page_nums = range(min(total, max_pages))

            for pn in page_nums:
                page = pdf.pages[pn]
                # 提取文本
                text = page.extract_text() or ""
                if not _is_scanned_page(text):
                    # 按段切块（连续空行分隔）
                    paragraphs = re.split(r"\n\s*\n", text)
                    for para in paragraphs:
                        para = para.strip()
                        if para:
                            result.text_chunks.append(PdfChunk(
                                text=para, page_num=pn + 1, char_count=len(para)
                            ))

                # 提取表格
                if include_tables:
                    try:
                        tables = page.extract_tables() or []
                        for ti, tbl in enumerate(tables):
                            if not tbl or not any(any(c for c in row) for row in tbl):
                                continue
                            # 清理每个 cell
                            cleaned = [[_clean(c) for c in row] for row in tbl]
                            result.table_chunks.append(PdfTableChunk(
                                rows=cleaned, page_num=pn + 1, table_idx=ti
                            ))
                    except Exception as e:
                        logger.warning("第 %d 页表格提取失败: %s", pn + 1, e)
    except Exception as e:
        result.error = f"PDF 打开失败: {e}"
        logger.exception("parse_generic_pdf 失败: %s", pdf_path)

    return result


def split_into_evidence(
    result: GenericPdfResult,
    source_title: str = "",
    source_url: str = "",
    max_chars_per_evidence: int = 1500,
) -> Iterable[dict]:
    """把 GenericPdfResult 切分成 Evidence 字典列表（供适配器入证据池）。

    每个文本块或表格块 = 一条 Evidence。超过 max_chars_per_evidence 的文本块会按段落再切。
    """
    from datetime import datetime
    title = source_title or result.title
    as_of = datetime.now().strftime("%Y-%m-%d")
    chunk_id = 0

    # 文本块
    for ch in result.text_chunks:
        text = ch.text.strip()
        if not text:
            continue
        # 超过长度限制按段落再切
        if len(text) <= max_chars_per_evidence:
            yield {
                "id": f"pdf_p{ch.page_num}_t{chunk_id}",
                "content": f"《{title}》 第 {ch.page_num} 页\n\n{text}",
                "source_url": source_url,
                "source_title": f"{title} - p{ch.page_num}",
                "as_of": as_of,
                "page": ch.page_num,
            }
            chunk_id += 1
        else:
            paragraphs = re.split(r"\n\s*\n", text)
            buffer = ""
            for p in paragraphs:
                if len(buffer) + len(p) + 2 > max_chars_per_evidence and buffer:
                    yield {
                        "id": f"pdf_p{ch.page_num}_t{chunk_id}",
                        "content": f"《{title}》 第 {ch.page_num} 页\n\n{buffer.strip()}",
                        "source_url": source_url,
                        "source_title": f"{title} - p{ch.page_num}",
                        "as_of": as_of,
                        "page": ch.page_num,
                    }
                    chunk_id += 1
                    buffer = ""
                buffer += p + "\n\n"
            if buffer.strip():
                yield {
                    "id": f"pdf_p{ch.page_num}_t{chunk_id}",
                    "content": f"《{title}》 第 {ch.page_num} 页\n\n{buffer.strip()}",
                    "source_url": source_url,
                    "source_title": f"{title} - p{ch.page_num}",
                    "as_of": as_of,
                    "page": ch.page_num,
                }
                chunk_id += 1

    # 表格块
    for tb in result.table_chunks:
        if not tb.rows:
            continue
        # 表格转 markdown
        md_lines = [f"《{title}》 第 {tb.page_num} 页 表格 {tb.table_idx + 1}"]
        if tb.rows:
            md_lines.append("| " + " | ".join(tb.rows[0]) + " |")
            md_lines.append("|" + "|".join("---" for _ in tb.rows[0]) + "|")
            for row in tb.rows[1:]:
                md_lines.append("| " + " | ".join(row) + " |")
        yield {
            "id": f"pdf_p{tb.page_num}_tbl{tb.table_idx}",
            "content": "\n".join(md_lines),
            "source_url": source_url,
            "source_title": f"{title} - p{tb.page_num} 表格",
            "as_of": as_of,
            "page": tb.page_num,
            "is_table": True,
        }
