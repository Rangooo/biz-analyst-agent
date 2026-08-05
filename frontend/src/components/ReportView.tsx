import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Insight, Evidence, QualityEval, IndustryMetrics, RevenueSegments, PeerFinancialRow } from "../types";
import { DualAxisChart, QualityRadar, EvidenceTierChart, IndustryMetricsChart, PriceTrendChart, SegmentPieChart, PeerCompareChart } from "./Charts";

type Fin = { period: string; revenue?: number | null; net_income?: number | null; rev_growth?: number | null; ni_growth?: number | null };

// 引用克制渲染：[证据N]/[^N]/[N] → 可点击上标跳文末证据条目；不动 [^N]: 脚注定义
// remarkGfm 启用 GFM 表格/删除线/任务列表——react-markdown v9 默认不支持表格，必须加此插件
//
// 后处理表格修复（兜底 LLM 不规范的脚注/含糊词）：
// 1. 表格中"脚注"列（表头含"脚注/证据/溯源"且单元格是纯数字/数字重复）→ 转上标 [^N] 内联到前一行数字后，删除该列
// 2. 单元格内纯 "33 33" → 当作证据编号 33 转上标
// 3. 单元格内"—" → 改成更明确的"未披露"
// 4. "回落"后无数字 → 加注释提示"（相对上年，具体幅度见上文）"

function _fixTableCell(cell: string): string {
  // 把单元格内 "33 33" / "33,33" / "33  33" 这类重复纯数字 → 合并为单个 [^33]
  // 同时把独立的纯数字（如 "33" 单独占单元格）→ [^33]
  const trimmed = cell.trim();
  // 纯数字单元格（如 "33"）
  if (/^\d{1,3}$/.test(trimmed)) {
    return `[^${trimmed}]`;
  }
  // 多个不同编号（"33 32" / "33,32" / "33、32"）→ 合并为 [^33][^32]
  const multiMatch = trimmed.match(/^(\d{1,3})\s*[,、\s]\s*(\d{1,3})\s*[,、\s\s]*(\d{1,3})?\s*$/);
  if (multiMatch) {
    const nums = [multiMatch[1], multiMatch[2]];
    if (multiMatch[3]) nums.push(multiMatch[3]);
    const unique = [...new Set(nums)];
    return unique.map(n => `[^${n}]`).join('');
  }
  // 重复数字 "33 33" / "33,33" → 取第一个
  const repMatch = trimmed.match(/^(\d{1,3})\s*[,、\s]\s*\1$/);
  if (repMatch) {
    return `[^${repMatch[1]}]`;
  }
  return cell;
}

function _isTableSeparator(line: string): boolean {
  // markdown 表格分隔行：|---|---|---| 或 |:---|---:|
  const trimmed = line.trim();
  if (!trimmed.startsWith('|')) return false;
  return /^\|?[\s:|-]+\|?$/.test(trimmed) && trimmed.includes('---');
}

function _validateTable(lines: string[]): { valid: boolean; reason?: string } {
  // 检查 markdown 表格是否合法。表格需要：表头行 + 分隔行 + 至少一行数据。
  // 所有行的 | 数量应一致（cell 数固定）。
  if (lines.length < 3) {
    return { valid: false, reason: '行数不足' };
  }
  if (!_isTableSeparator(lines[1])) {
    return { valid: false, reason: '缺少分隔行' };
  }
  // 检查每行的 cell 数（去掉首尾 | 后 split）
  const colCount = (line: string) => line.split('|').length - 2;
  const expected = colCount(lines[0]);
  if (expected < 2) {
    return { valid: false, reason: '列数过少' };
  }
  for (let i = 0; i < lines.length; i++) {
    if (Math.abs(colCount(lines[i]) - expected) > 0) {
      return { valid: false, reason: `第 ${i + 1} 行列数不一致（期望 ${expected}，实际 ${colCount(lines[i])}）` };
    }
  }
  return { valid: true };
}

function _fixTableFootnoteColumn(md: string): string {
  // 找到所有 markdown 表格，检查是否有"脚注/证据/溯源"列
  // 如果有，把该列单元格内联到上一列数字后，并删除该列
  const lines = md.split("\n");
  let i = 0;
  const out: string[] = [];
  while (i < lines.length) {
    // 检测表格开始（| 开头）
    if (lines[i]?.startsWith("|")) {
      const tableLines: string[] = [];
      while (i < lines.length && lines[i]?.startsWith("|")) {
        tableLines.push(lines[i]);
        i++;
      }
      out.push(..._fixOneTable(tableLines));
    } else {
      out.push(lines[i]);
      i++;
    }
  }
  return out.join("\n");
}

function _fixOneTable(lines: string[]): string[] {
  if (lines.length < 2) return lines;
  // 解析表头找"脚注/证据/溯源"列
  const headerCells = lines[0].split("|").map(c => c.trim()).filter((_, idx, arr) => idx > 0 && idx < arr.length - 1);
  const footnoteColIdx = headerCells.findIndex(h =>
    /脚注|证据编号|证据|溯源|来源编号/i.test(h)
  );

  if (footnoteColIdx === -1) {
    // 没有脚注列，但单元格内仍可能有 "33 33" 这种重复——逐单元格修复
    return lines.map(line => {
      const cells = line.split("|");
      // 第一个和最后一个是空（| 开头/结尾）
      const fixed = cells.map((c, idx) => {
        if (idx === 0 || idx === cells.length - 1) return c;
        return _fixTableCell(c);
      });
      return fixed.join("|");
    });
  }

  // 有脚注列：把该列单元格内联到前一列数字后，然后删除该列
  return lines.map(line => {
    // 跳过分隔行（|---|---|---），避免错误地给它加脚注
    if (_isTableSeparator(line)) {
      const cells = line.split("|");
      const inner = cells.slice(1, -1);
      if (footnoteColIdx < inner.length) {
        inner.splice(footnoteColIdx, 1);
        return "|" + inner.join("|") + "|";
      }
      return line;
    }
    const cells = line.split("|");
    // cells[0] 和 cells[last] 是空的（因为 | 开头和结尾）
    const inner = cells.slice(1, -1);
    if (footnoteColIdx >= inner.length) return line;

    const footnoteCell = _fixTableCell(inner[footnoteColIdx]);
    const prevCell = inner[footnoteColIdx - 1] || "";

    // 把脚注内联到前一列末尾
    inner[footnoteColIdx - 1] = prevCell.replace(/\s*$/, "") + " " + footnoteCell;
    // 删除脚注列
    inner.splice(footnoteColIdx, 1);

    return "|" + inner.join("|") + "|";
  });
}

function _fixEmdashAndVagueWords(md: string): string {
  // 在表格单元格内（| 之间的内容）："—" → "未披露"，"回落"后无数字 → 加注释
  return md.replace(/\|[^|]*\|/g, (cell) => {
    let fixed = cell;
    // 单独的 — 或 - → 未披露
    fixed = fixed.replace(/\|\s*—\s*\|/g, "| 未披露 |");
    fixed = fixed.replace(/\|\s*-\s*\|/g, "| 未披露 |");
    // "回落"后没有数字 → 加括号提示
    fixed = fixed.replace(/回落(?![\s\S]?\d)/g, "回落（vs 上年）");
    return fixed;
  });
}

function _mergeConsecutiveFootnotes(md: string): string {
  // 相邻脚注 [^38][^41][^62] → 合并为单个 [^38,41,62]，避免渲染成上标后
  // 数字连读（"384162"）。逗号后用窄空格 U+2009 让上标更易读。
  // 不动脚注定义行（[^N]: ...）——定义行的] 后紧跟 :，不会被本正则匹配。
  return md.replace(/(?:\[\^\d+\](?!:)){2,}/g, (group) => {
    const nums = Array.from(group.matchAll(/\[\^(\d+)\]/g)).map(m => m[1]);
    const unique = [...new Set(nums)];
    return `[^${unique.join(",\u2009")}]`;
  });
}

function CiteMarkdown({ md }: { md: string }) {
  let enriched = md;
  // 1. 先修表格（脚注列内联 + 单元格内纯数字转上标）
  enriched = _fixTableFootnoteColumn(enriched);
  // 2. 修含糊词和 —
  enriched = _fixEmdashAndVagueWords(enriched);
  // 3. 标准引用转上标链接
  //    连续脚注（如 [^38][^41]）必须先合并成带逗号的引用组，否则渲染成紧贴的
  //    上标数字会连读成 "3841"、表格里 [^35][^47][^36] 会变成 "354736"。
  enriched = _mergeConsecutiveFootnotes(enriched);
  enriched = enriched
    .replace(/\[证据\s*(\d+)\]/g, (_, n) => `[\u200b${n}](#evidence-${n})`)
    .replace(/\[\^([\d,\u2009]+)\](?!:)/g, (_, n) => {
      const first = String(n).split(",")[0].trim();
      return `[\u200b${n}](#evidence-${first})`;
    })
    .replace(/\[(\d+)\]/g, (_, n) => `[\u200b${n}](#evidence-${n})`);
  return <Markdown remarkPlugins={[remarkGfm]}>{enriched}</Markdown>;
}

export function ReportView({
  md,
  query,
  runId,
  asOf,
  docMode,
  insights = [],
  financials = [],
  evidencePool = [],
  qualityEval,
  industryMetrics,
  revenueSegments,
  peerFinancials = [],
}: {
  md: string;
  query: string;
  runId?: string;
  asOf?: string;
  docMode?: boolean;
  insights?: Insight[];
  financials?: Fin[];
  evidencePool?: Evidence[];
  qualityEval?: QualityEval;
  industryMetrics?: IndustryMetrics | null;
  revenueSegments?: RevenueSegments | null;
  peerFinancials?: PeerFinancialRow[];
}) {
  function download() {
    const blob = new Blob([md], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${query || "analysis"}_${docMode ? "document" : "report"}.md`;
    a.click();
    URL.revokeObjectURL(url);
  }

  function downloadPdf() {
    if (!runId) return;
    // Trigger browser download via API
    const a = document.createElement("a");
    a.href = `/api/export/${runId}`;
    a.download = `${query || "analysis"}.pdf`;
    a.click();
  }

  // 证据溯源已内嵌在报告 Markdown 末尾的「参考文献」段（APA 风格，代码生成），
  // 不再在前端重复渲染第二份证据列表。

  return (
    <div>
      <div style={{ marginBottom: 12, display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <button className="btn ghost" onClick={download}>导出 Markdown</button>
        {runId && <button className="btn ghost" onClick={downloadPdf}>导出 PDF</button>}
        {asOf && <span style={{ fontSize: 12, color: "#5f5e5a" }}>数据截至 {asOf}</span>}
        {docMode && <span style={{ fontSize: 12, color: "#185fa5" }}>完整分析文档 · 终审模型撰写</span>}
      </div>

      {/* 可视化 1：财务时序双轴图（营收/净利润柱 + 增速折线）—— 仅公司分析有财务时序时 */}
      {financials.length > 0 && <DualAxisChart rows={financials} />}

      {/* 可视化 1a：营收结构饼图（业务板块拆分）—— 公司分析有分部数据时 */}
      {revenueSegments && revenueSegments.segments && revenueSegments.segments.length >= 2 && (
        <SegmentPieChart data={revenueSegments} />
      )}

      {/* 可视化 1b：同行对比柱状图（主体 vs Peers）—— 有同行数据时 */}
      {peerFinancials.length >= 2 && <PeerCompareChart rows={peerFinancials} />}

      {/* 可视化 1c：行业总量指标柱状图（行业分析专用） */}
      {industryMetrics && industryMetrics.totals && industryMetrics.totals.length > 0 && (
        <IndustryMetricsChart rows={industryMetrics.totals} />
      )}

      {/* 可视化 1d：行业价格趋势折线图（行业分析专用） */}
      {industryMetrics && industryMetrics.prices && industryMetrics.prices.length > 0 && (
        <PriceTrendChart rows={industryMetrics.prices} />
      )}

      {/* 可视化 2：质量评分雷达图（借鉴 virattt eval，仅完整文档模式有评分时显示） */}
      {docMode && qualityEval && qualityEval.scores && Object.keys(qualityEval.scores).length >= 3 && (
        <QualityRadar qa={qualityEval} />
      )}

      {/* 可视化 3：证据来源分布（借鉴 FinSight 视觉增强） */}
      {evidencePool.length > 0 && <EvidenceTierChart evidence={evidencePool} />}

      <div className="report-md">
        <CiteMarkdown md={md} />
      </div>
    </div>
  );
}
