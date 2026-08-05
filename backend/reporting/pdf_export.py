"""Markdown report → PDF export（用 ReportLab Platypus 直接画真正的表格）。

原 xhtml2pdf 实现：xhtml2pdf 根本不渲染 <table>（列宽计算 None），只能用文本对齐
模拟，视觉上不直观。

新实现：用 ReportLab Platypus 直接解析 markdown，分别用 Paragraph/Table 渲染，
能画真正的表格（带边框、表头底色、行高自适应），复用已注册的 simhei 字体支持中文。
"""
from __future__ import annotations

import io
import logging
import re
from datetime import datetime
from pathlib import Path

import markdown as _markdown
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle, PageBreak
)

log = logging.getLogger(__name__)

# ---- 中文字体注册 ----
_FONT_CANDIDATES = [
    # Windows
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\STSONG.TTF",
    r"C:\Windows\Fonts\Deng.ttf",
    # macOS
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/Supplemental/STHeiti Light.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
    # Linux
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]

_CJK_FONT = None  # ReportLab 注册的字体名
_CJK_FONT_PATH = None  # 字体文件路径（用于 HTML 渲染）


def _register_cjk_font():
    """注册中文字体到 reportlab。支持 .ttf/.ttc，macOS/Linux/Windows 全平台。"""
    global _CJK_FONT, _CJK_FONT_PATH
    if _CJK_FONT is not None:
        return
    for p in _FONT_CANDIDATES:
        if not Path(p).exists():
            continue
        try:
            font_name = Path(p).stem.replace(" ", "")
            if p.lower().endswith(".ttc"):
                # TrueType Collection：必须指定 subfontIndex，否则 ReportLab 报错
                pdfmetrics.registerFont(TTFont(font_name, p, subfontIndex=0))
            else:
                pdfmetrics.registerFont(TTFont(font_name, p))
            _CJK_FONT = font_name
            _CJK_FONT_PATH = p
            log.info("PDF 中文字体注册: %s as %s", p, font_name)
            return
        except Exception as e:
            log.warning("注册字体 %s 失败: %s", p, e)
            continue

    # Fallback: 尝试 ReportLab 内置 CID 字体（需要 CMap 数据）
    try:
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont
        pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
        _CJK_FONT = "STSong-Light"
        log.info("使用 reportlab 内置 STSong-Light CID 字体")
    except Exception as e:
        # 最终 fallback：用 Helvetica（不支持中文但不会崩溃）
        _CJK_FONT = "Helvetica"
        log.warning(
            "无法注册任何中文字体 (STSong-Light 也不可用: %s)，"
            "PDF 中文将显示为方块。请安装中文字体或 reportlab[rl_accel]。", e
        )

_register_cjk_font()


# ---- 样式 ----
def _build_styles():
    """构建 ReportLab 样式表。"""
    base = getSampleStyleSheet()["Normal"]
    styles = {
        "title": ParagraphStyle(
            "Title", parent=base, fontName=_CJK_FONT, fontSize=18, leading=22,
            textColor=colors.HexColor("#185fa5"), spaceAfter=12, spaceBefore=0,
        ),
        "h2": ParagraphStyle(
            "H2", parent=base, fontName=_CJK_FONT, fontSize=14, leading=18,
            textColor=colors.HexColor("#2c2c2a"), spaceBefore=12, spaceAfter=6,
            borderWidth=0, borderColor=colors.HexColor("#e6e4dd"),
            borderPadding=4,
        ),
        "h3": ParagraphStyle(
            "H3", parent=base, fontName=_CJK_FONT, fontSize=12, leading=15,
            textColor=colors.HexColor("#2c2c2a"), spaceBefore=8, spaceAfter=4,
        ),
        "body": ParagraphStyle(
            "Body", parent=base, fontName=_CJK_FONT, fontSize=10, leading=15,
            textColor=colors.HexColor("#2c2c2a"), spaceAfter=6,
        ),
        "cover": ParagraphStyle(
            "Cover", parent=base, fontName=_CJK_FONT, fontSize=9, leading=12,
            textColor=colors.HexColor("#5f5e5a"), spaceAfter=12,
        ),
        "th": ParagraphStyle(
            "TH", parent=base, fontName=_CJK_FONT, fontSize=9, leading=12,
            textColor=colors.HexColor("#2c2c2a"), alignment=0,
        ),
        "td": ParagraphStyle(
            "TD", parent=base, fontName=_CJK_FONT, fontSize=9, leading=12,
            textColor=colors.HexColor("#2c2c2a"),
        ),
        "td_num": ParagraphStyle(
            "TDNum", parent=base, fontName=_CJK_FONT, fontSize=9, leading=12,
            textColor=colors.HexColor("#2c2c2a"), alignment=2,  # right
        ),
    }
    return styles


# ---- Markdown 解析 ----
def _parse_table_rows(table_lines: list[str]) -> tuple[list[list[str]], int]:
    """解析 markdown 表格行为 cell 文本列表，返回 (rows, header_row_count)。
    header_row_count 通常是 1（第一行是表头，第二行是分隔线）。
    """
    rows = []
    for line in table_lines:
        if not line.strip().startswith("|"):
            continue
        # 切分单元格
        cells = line.strip().strip("|").split("|")
        cells = [c.strip() for c in cells]
        # 跳过分隔行（|---|---|）
        if all(re.match(r"^:?-+:?$", c) for c in cells if c):
            continue
        rows.append(cells)
    return rows, 1  # 默认 1 行表头


def _md_to_flowables(md_text: str, styles: dict) -> list:
    """把 markdown 转成 ReportLab Flowable 列表。"""
    flowables = []
    lines = md_text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]

        # 标题
        if line.startswith("### "):
            flowables.append(Paragraph(_inline(line[4:].strip(), styles), styles["h3"]))
            i += 1
        elif line.startswith("## "):
            flowables.append(Paragraph(_inline(line[3:].strip(), styles), styles["h2"]))
            i += 1
        elif line.startswith("# "):
            flowables.append(Paragraph(_inline(line[2:].strip(), styles), styles["title"]))
            i += 1
        # 表格
        elif "|" in line and i + 1 < len(lines) and "---" in lines[i + 1]:
            # 找到连续表格行
            table_lines = []
            j = i
            while j < len(lines) and lines[j].strip().startswith("|"):
                table_lines.append(lines[j])
                j += 1
            rows, header_count = _parse_table_rows(table_lines)
            if rows:
                flowables.append(_build_table(rows, header_count, styles))
            i = j
        # 空行
        elif not line.strip():
            i += 1
        # 引用
        elif line.startswith("> "):
            quote_text = line[2:].strip()
            flowables.append(Paragraph(_inline(quote_text, styles), styles["body"]))
            i += 1
        # 普通段落（每行独立成段，保留 markdown 的换行语义）
        else:
            # markdown 列表项（- 或 * 开头）
            list_match = re.match(r"^[\-\*]\s+(.+)$", line.strip())
            if list_match:
                # 列表项用 bullet + 缩进
                flowables.append(Paragraph("• " + _inline(list_match.group(1), styles), styles["body"]))
                i += 1
            else:
                # 普通段落：每行作为独立段落（报告类文本通常每行都要换行）
                flowables.append(Paragraph(_inline(line.strip(), styles), styles["body"]))
                i += 1
    return flowables


def _is_block_start(line: str) -> bool:
    """判断是否新 block 的开始（标题/表格/引用/分隔线）。"""
    s = line.strip()
    return (
        s.startswith("#") or
        ("|" in s and s.startswith("|")) or
        s.startswith("> ") or
        s.startswith("---")
    )


def _inline(text: str, styles: dict) -> str:
    """把 markdown inline 语法（粗体/斜体/链接/脚注）转成 ReportLab Paragraph 标签。

    ReportLab Paragraph 支持的标签：<b>、<i>、<u>、<font>、<br/> 等，不支持 markdown 原生语法。
    """
    # 连续脚注 [^38][^41][^62] 必须先合并为一个 <sup>38, 41, 62</sup>，
    # 否则相邻上标数字会连读成 "384162"（前端渲染同样处理）。
    def _merge_footnotes(m: "re.Match[str]") -> str:
        nums = re.findall(r"\[\^(\d+)\]", m.group(0))
        uniq = list(dict.fromkeys(nums))
        return "<sup>" + ", ".join(uniq) + "</sup>"

    text = re.sub(r"(?:\[\^\d+\](?!:)){2,}", _merge_footnotes, text)
    # 脚注 [^N] → <sup>N</sup>
    text = re.sub(r"\[\^(\d+)\](?!:)", r"<sup>\1</sup>", text)
    # 引用 [证据N] / [N] → <sup>N</sup>
    text = re.sub(r"\[证据\s*(\d+)\]", r"<sup>\1</sup>", text)
    text = re.sub(r"(?<!\n)\[(\d{1,3})\](?!:)", r"<sup>\1</sup>", text)
    # 粗体 **xxx** → <b>xxx</b>
    text = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", text)
    # 斜体 *xxx* → <i>xxx</i>（避免影响粗体 **）
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", text)
    # 转义 < > &
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    # 但保留 ReportLab 标签（<b>/<i>/<sup>）
    text = text.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")
    text = text.replace("&lt;i&gt;", "<i>").replace("&lt;/i&gt;", "</i>")
    text = text.replace("&lt;sup&gt;", "<sup>").replace("&lt;/sup&gt;", "</sup>")
    return text


def _build_table(rows: list[list[str]], header_count: int, styles: dict) -> Table:
    """构建 ReportLab Table 元素。"""
    # 把每个 cell 包成 Paragraph（支持多行 + 中文字体）
    col_count = max(len(r) for r in rows)
    # 补齐列数
    for r in rows:
        while len(r) < col_count:
            r.append("")

    # 判断每列是数字还是文本（启发式：包含数字且没有大量中文 → 数字列）
    is_num_col = []
    for ci in range(col_count):
        col_texts = [r[ci] for r in rows[header_count:]] if len(rows) > header_count else [r[ci] for r in rows]
        # 如果所有非空值都符合数字模式（包含数字且 < 30% 中文字符）→ 数字列
        non_empty = [t for t in col_texts if t.strip()]
        if non_empty and all(
            re.search(r"\d", t) and sum(1 for c in t if "\u4e00" <= c <= "\u9fff") / max(len(t), 1) < 0.3
            for t in non_empty
        ):
            is_num_col.append(True)
        else:
            is_num_col.append(False)

    # 检测 YoY 副行：第一列是 "YoY" 标签的行 → 浅灰底色+小字
    is_yoy_row = []
    for ri, r in enumerate(rows):
        first_cell = r[0].strip() if r else ""
        is_yoy_row.append(first_cell in ("YoY", "yoy", "同比", "增速"))

    table_data = []
    for ri, r in enumerate(rows):
        is_header = ri < header_count
        is_yoy = is_yoy_row[ri] if ri < len(is_yoy_row) else False
        row_cells = []
        for ci, cell in enumerate(r):
            if is_yoy:
                # YoY 副行：用小字+斜体
                from reportlab.lib.styles import ParagraphStyle
                yoy_style = ParagraphStyle(
                    "YoY", parent=styles["td"], fontSize=8,
                    textColor=colors.HexColor("#888780"), leftIndent=8
                )
                cell_html = cell.replace("\n", "<br/>")
                row_cells.append(Paragraph(_inline(cell_html, styles), yoy_style))
            else:
                style = styles["th"] if is_header else (styles["td_num"] if is_num_col[ci] else styles["td"])
                cell_html = cell.replace("\n", "<br/>")
                row_cells.append(Paragraph(_inline(cell_html, styles), style))
        table_data.append(row_cells)

    # 计算列宽：总宽 ~16.5cm，按 cell 字符数分配（最小 2.5cm/列）
    available_width = 16.5  # cm
    min_col = 2.5
    col_weights = []
    for ci in range(col_count):
        col_texts = [r[ci] for r in rows]
        weight = 0
        for t in col_texts:
            weight += sum(2 if ord(ch) > 127 else 1 for ch in t) + 1
        col_weights.append(max(weight, 1))
    total_weight = sum(col_weights)
    col_widths = [max(min_col, available_width * w / total_weight) for w in col_weights]
    total = sum(col_widths)
    if total > available_width:
        scale = available_width / total
        col_widths = [w * scale for w in col_widths]
    # 单位转换 cm → point（ReportLab Table 用 point）
    col_widths_pt = [w * 28.35 for w in col_widths]

    table = Table(table_data, colWidths=col_widths_pt, repeatRows=header_count)
    style_cmds = [
        # 表头底色
        ("BACKGROUND", (0, 0), (-1, header_count - 1), colors.HexColor("#ede9e0")),
        # 网格
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#b4b2a9")),
        # 内边距
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        # 斑马纹（跳过头部和 YoY 副行）
    ]
    # 斑马纹：只对数据行交替（不应用于 YoY 副行）
    data_row_indices = [ri for ri in range(header_count, len(rows)) if not is_yoy_row[ri]]
    for i, ri in enumerate(data_row_indices):
        if i % 2 == 1:
            style_cmds.append(("BACKGROUND", (0, ri), (-1, ri), colors.HexColor("#f7f5f0")))

    # YoY 副行：浅灰底色
    for ri, is_yoy in enumerate(is_yoy_row):
        if is_yoy:
            style_cmds.append(("BACKGROUND", (0, ri), (-1, ri), colors.HexColor("#f9f7f2")))
            style_cmds.append(("TOPPADDING", (0, ri), (-1, ri), 1))
            style_cmds.append(("BOTTOMPADDING", (0, ri), (-1, ri), 4))

    for ci, is_num in enumerate(is_num_col):
        if is_num:
            style_cmds.append(("ALIGN", (ci, header_count), (ci, -1), "RIGHT"))
    for ci, is_num in enumerate(is_num_col):
        if is_num:
            style_cmds.append(("TEXTCOLOR", (ci, header_count), (ci, -1), colors.HexColor("#5f5e5a")))

    table.setStyle(TableStyle(style_cmds))
    return table


def render_pdf(
    md_text: str,
    query: str = "",
    data_as_of: str = "",
) -> bytes:
    """Render a Markdown report string to PDF bytes.

    真正的 PDF 表格：边框 + 表头底色 + 斑马纹 + 数字列右对齐 + 中文支持。
    """
    styles = _build_styles()
    flowables = []

    # 封面元信息
    cover_parts = []
    if query:
        cover_parts.append(f"分析对象: {query}")
    if data_as_of:
        cover_parts.append(f"数据截至: {data_as_of}")
    cover_parts.append(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    flowables.append(Paragraph(" &nbsp;|&nbsp; ".join(cover_parts), styles["cover"]))

    # 解析 markdown → Flowable
    flowables.extend(_md_to_flowables(md_text, styles))

    # 生成 PDF
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=1.8 * cm, rightMargin=1.8 * cm,
        topMargin=2.2 * cm, bottomMargin=2.5 * cm,
        title=query or "分析报告",
    )
    doc.build(flowables)
    return buf.getvalue()
