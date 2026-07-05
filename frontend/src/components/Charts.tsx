import { Insight, Evidence, QualityEval, IndustryMetricRow, PriceTrendRow, RevenueSegments, PeerFinancialRow } from "../types";

type Fin = { period: string; revenue?: number | null; net_income?: number | null; rev_growth?: number | null; ni_growth?: number | null };

function fmt(n?: number | null) {
  if (n == null) return "—";
  const a = Math.abs(n);
  if (a >= 1e12) return (n / 1e12).toFixed(1) + "万亿";
  if (a >= 1e8) return (n / 1e8).toFixed(1) + "亿";
  if (a >= 1e4) return (n / 1e4).toFixed(1) + "万";
  return String(n);
}
const UP = "#d83a3a", DOWN = "#1d9e75"; // 涨红跌绿

// ===== 双轴图（营收/净利润柱 + 同比增速折线）—— 借鉴 FinSight 专业金融图表 =====
export function DualAxisChart({ rows }: { rows: Fin[] }) {
  if (!rows.length) return null;
  const n = rows.length;
  const W = 580, H = 240, padL = 12, padR = 12, padB = 30, padT = 24;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const cw = plotW / n;
  const maxV = Math.max(...rows.map(r => Math.max(r.revenue || 0, r.net_income || 0)), 1);
  const growths = rows.map(r => r.rev_growth).filter((g): g is number => g != null);
  const gMax = growths.length ? Math.max(...growths, 0) : 0;
  const gMin = growths.length ? Math.min(...growths, 0) : 0;
  const gRange = Math.max(gMax - gMin, 1);
  const yBar = (v: number) => padT + plotH - (v / maxV) * plotH;
  const yLine = (g: number) => padT + plotH - ((g - gMin) / gRange) * plotH;
  const barW = Math.min(22, cw / 3.2);
  const linePts = rows.map((r, i) => r.rev_growth != null ? `${padL + i * cw + cw / 2},${yLine(r.rev_growth)}` : null).filter(Boolean).join(" ");

  return (
    <div className="chart-card">
      <div className="chart-title">财务时序双轴图（柱：营收/净利润 · 折线：营收同比增速，涨红跌绿）</div>
      <svg width="100%" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet" role="img" aria-label="财务双轴图" style={{ maxWidth: 620 }}>
        {/* 横向网格线 */}
        {[0, 0.25, 0.5, 0.75, 1].map((f, i) => (
          <line key={i} x1={padL} y1={padT + plotH * f} x2={W - padR} y2={padT + plotH * f} stroke="#eceae3" strokeWidth="1" />
        ))}
        {/* 柱 */}
        {rows.map((r, i) => {
          const x0 = padL + i * cw + cw / 2;
          const rev = r.revenue || 0, ni = r.net_income || 0;
          return (
            <g key={i}>
              {rev > 0 && <rect x={x0 - barW - 1} y={yBar(rev)} width={barW} height={padT + plotH - yBar(rev)} fill="#3a7bd5" rx={2} />}
              {ni > 0 && <rect x={x0 + 1} y={yBar(ni)} width={barW} height={padT + plotH - yBar(ni)} fill="#0f6e56" rx={2} />}
              {rev > 0 && <text x={x0 - barW / 2 - 1} y={yBar(rev) - 3} textAnchor="middle" fontSize="9" fill="#3a7bd5">{fmt(rev)}</text>}
              {ni > 0 && <text x={x0 + barW / 2 + 1} y={yBar(ni) - 3} textAnchor="middle" fontSize="9" fill="#0f6e56">{fmt(ni)}</text>}
              <text x={x0} y={H - padB + 16} textAnchor="middle" fontSize="10" fill="#5f5e5a">{r.period}</text>
            </g>
          );
        })}
        {/* 增速折线 */}
        {linePts && <polyline points={linePts} fill="none" stroke="#c98a2b" strokeWidth="1.6" />}
        {rows.map((r, i) => {
          if (r.rev_growth == null) return null;
          const x0 = padL + i * cw + cw / 2;
          return (
            <g key={`g${i}`}>
              <circle cx={x0} cy={yLine(r.rev_growth)} r={2.6} fill={r.rev_growth >= 0 ? UP : DOWN} />
              <text x={x0} y={yLine(r.rev_growth) - 6} textAnchor="middle" fontSize="9" fontWeight="600" fill={r.rev_growth >= 0 ? UP : DOWN}>
                {r.rev_growth >= 0 ? "+" : ""}{r.rev_growth}%
              </text>
            </g>
          );
        })}
      </svg>
      <div className="chart-legend">
        <span className="lg"><i style={{ background: "#3a7bd5" }} />营收</span>
        <span className="lg"><i style={{ background: "#0f6e56" }} />净利润</span>
        <span className="lg"><i style={{ background: "#c98a2b" }} />营收同比增速</span>
        <span className="lg" style={{ color: "#888780" }}>增速点：涨红跌绿</span>
      </div>
    </div>
  );
}

// ===== 8 维度质量评分雷达图 —— 借鉴 virattt eval =====
export function QualityRadar({ qa }: { qa: QualityEval }) {
  const dims = Object.entries(qa.scores || {});
  if (dims.length < 3) return null;
  const W = 360, H = 320, cx = W / 2, cy = H / 2 + 6, R = 110, maxScore = 5;
  const N = dims.length;
  const angle = (i: number) => (Math.PI * 2 * i) / N - Math.PI / 2;
  const pt = (i: number, v: number) => {
    const r = (v / maxScore) * R;
    return [cx + r * Math.cos(angle(i)), cy + r * Math.sin(angle(i))];
  };
  const poly = dims.map(([, v], i) => pt(i, v).join(",")).join(" ");
  const total = qa.total || dims.reduce((s, [, v]) => s + v, 0);
  const passed = qa.passed ?? (total >= 28 && Math.min(...dims.map(([, v]) => v)) >= 3);

  return (
    <div className="chart-card">
      <div className="chart-title">
        报告质量评分（8 维度 · 终审自评）
        <span style={{ float: "right", color: passed ? "#0f6e56" : "#c98a2b", fontWeight: 600 }}>
          {total}/40 {passed ? "达标" : "待改进"}
        </span>
      </div>
      <div style={{ fontSize: 10, color: "#a0a09a", marginBottom: 4, textAlign: "center" }}>
        评分标准：1=极差 2=差 3=一般 4=良好 5=优秀 · 达标线：总分≥28 且每项≥3
      </div>
      <svg width="100%" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet" role="img" aria-label="质量评分雷达图" style={{ maxWidth: 400, display: "block", margin: "0 auto" }}>
        {/* 同心网格 */}
        {[1, 2, 3, 4, 5].map((lvl) => {
          const ring = dims.map((_, i) => pt(i, lvl).join(",")).join(" ");
          return <polygon key={lvl} points={ring} fill="none" stroke="#e6e4dd" strokeWidth="1" />;
        })}
        {/* 轴线 + 维度标签 */}
        {dims.map(([name, v], i) => {
          const [ax, ay] = pt(i, maxScore);
          const [lx, ly] = pt(i, maxScore + 0.85);
          const anchor = Math.abs(lx - cx) < 12 ? "middle" : lx > cx ? "start" : "end";
          const low = v < 3;
          return (
            <g key={name}>
              <line x1={cx} y1={cy} x2={ax} y2={ay} stroke="#eceae3" strokeWidth="1" />
              <text x={lx} y={ly} textAnchor={anchor} dominantBaseline="middle" fontSize="11" fill={low ? UP : "#5f5e5a"} fontWeight={low ? 600 : 400}>
                {name} {v}
              </text>
            </g>
          );
        })}
        {/* 数据多边形 */}
        <polygon points={poly} fill="rgba(58,123,213,0.18)" stroke="#185fa5" strokeWidth="1.8" />
        {dims.map(([, v], i) => {
          const [px, py] = pt(i, v);
          return <circle key={i} cx={px} cy={py} r={2.6} fill={v < 3 ? UP : "#185fa5"} />;
        })}
      </svg>
      {qa.issues && qa.issues.length > 0 && (
        <div style={{ fontSize: 11, color: "#888780", marginTop: 6, lineHeight: 1.6 }}>
          <b style={{ color: "#a32d2d" }}>待改进：</b>
          {qa.issues.slice(0, 3).map((it, i) => <div key={i}>· {it}</div>)}
        </div>
      )}
    </div>
  );
}

// ===== 证据来源分布（按溯源等级 T1-T7）—— 借鉴 FinSight 视觉增强 =====
const TIER_NAME: Record<number, string> = {
  1: "财报/电话会", 2: "公告", 3: "公司IR/投资者日", 4: "监管", 5: "权威媒体", 6: "一般媒体", 7: "估算/未证实",
};
const TIER_COLOR: Record<number, string> = {
  1: "#0f6e56", 2: "#1d9e75", 3: "#185fa5", 4: "#3a7bd5", 5: "#7a5cb8", 6: "#888780", 7: "#a32d2d",
};
export function EvidenceTierChart({ evidence }: { evidence: Evidence[] }) {
  if (!evidence.length) return null;
  const counts: Record<number, number> = {};
  evidence.forEach((e) => {
    const t = (e as any).tier ?? 6;
    counts[t] = (counts[t] || 0) + 1;
  });
  const tiers = Object.keys(counts).map(Number).sort((a, b) => a - b);
  const total = evidence.length;
  const maxC = Math.max(...Object.values(counts), 1);
  const W = 560, rowH = 26, padL = 96, padR = 48, barMaxW = W - padL - padR;

  return (
    <div className="chart-card">
      <div className="chart-title">证据来源分布（共 {total} 条，按溯源权威等级）</div>
      <svg width="100%" viewBox={`0 0 ${W} ${tiers.length * rowH + 10}`} preserveAspectRatio="xMidYMid meet" role="img" aria-label="证据来源分布" style={{ maxWidth: 600 }}>
        {tiers.map((t, i) => {
          const c = counts[t];
          const w = (c / maxC) * barMaxW;
          const y = i * rowH + 6;
          return (
            <g key={t}>
              <text x={padL - 6} y={y + 13} textAnchor="end" fontSize="10" fill="#5f5e5a">T{t} {TIER_NAME[t] || ""}</text>
              <rect x={padL} y={y + 3} width={w} height={16} fill={TIER_COLOR[t] || "#888780"} rx={2} opacity={0.85} />
              <text x={padL + w + 6} y={y + 13} fontSize="10" fontWeight="600" fill={TIER_COLOR[t] || "#888780"}>
                {c} ({Math.round((c / total) * 100)}%)
              </text>
            </g>
          );
        })}
      </svg>
      <div style={{ fontSize: 10, color: "#888780", marginTop: 4 }}>
        T1-T3 为高权威源（财报/公告/IR）；T6-T7 为一般媒体/估算，采信需谨慎。
      </div>
    </div>
  );
}

// ===== 洞察置信度与裁决条形（保留原有，独立成组件）=====
import { VERDICT_META } from "../types";
export function ConfidenceBars({ insights }: { insights: Insight[] }) {
  if (!insights.length) return null;
  return (
    <div className="chart-card">
      <div className="chart-title">洞察置信度与裁决（共 {insights.length} 条）</div>
      {insights.map((ins, i) => {
        const vm = VERDICT_META[ins.verdict] || VERDICT_META.questionable;
        const pctv = Math.round((ins.confidence || 0) * 100);
        return (
          <div key={ins.id} className="bar-row" title={ins.claim}>
            <span className="bar-label">{i + 1}. {ins.section}</span>
            <div className="bar-track">
              <div className="bar-fill" style={{ width: `${pctv}%`, background: vm.color }} />
              <span className="bar-pct" style={{ color: vm.color }}>{vm.label} {pctv}%</span>
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ===== 行业总量指标柱状图（行业分析专用）=====
export function IndustryMetricsChart({ rows }: { rows: IndustryMetricRow[] }) {
  if (!rows || !rows.length) return null;
  // 每个指标一张子图，纵向排列
  return (
    <div className="chart-card">
      <div className="chart-title">行业总量指标趋势（按年度，涨红跌绿）</div>
      {rows.map((row, ri) => {
        const pts = row.values || [];
        if (pts.length < 2) return null;
        const n = pts.length;
        const W = 580, H = 200, padL = 50, padR = 60, padB = 30, padT = 24;
        const plotW = W - padL - padR, plotH = H - padT - padB;
        const cw = plotW / n;
        const vals = pts.map(p => p.value || 0);
        const maxV = Math.max(...vals, 1);
        const growths = pts.map(p => p.growth).filter((g): g is number => g != null);
        const gMax = growths.length ? Math.max(...growths, 0) : 0;
        const gMin = growths.length ? Math.min(...growths, 0) : 0;
        const gRange = Math.max(gMax - gMin, 1);
        const yBar = (v: number) => padT + plotH - (v / maxV) * plotH;
        const yLine = (g: number) => padT + plotH - ((g - gMin) / gRange) * plotH;
        const barW = Math.min(28, cw / 2.5);
        const linePts = pts.map((p, i) => p.growth != null ? `${padL + i * cw + cw / 2},${yLine(p.growth)}` : null).filter(Boolean).join(" ");

        return (
          <div key={ri} style={{ marginBottom: 8 }}>
            <div style={{ fontSize: 11, fontWeight: 600, color: "#185fa5", marginBottom: 2 }}>{row.metric}（{row.unit}）</div>
            <svg width="100%" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet" role="img" aria-label={row.metric} style={{ maxWidth: 620 }}>
              {[0, 0.5, 1].map((f, i) => (
                <line key={i} x1={padL} y1={padT + plotH * f} x2={W - padR} y2={padT + plotH * f} stroke="#eceae3" strokeWidth="1" />
              ))}
              {pts.map((p, i) => {
                const x0 = padL + i * cw + cw / 2;
                const v = p.value || 0;
                return (
                  <g key={i}>
                    <rect x={x0 - barW / 2} y={yBar(v)} width={barW} height={padT + plotH - yBar(v)} fill="#3a7bd5" rx={2} opacity={0.85} />
                    <text x={x0} y={yBar(v) - 3} textAnchor="middle" fontSize="9" fill="#3a7bd5">{fmt(v)}</text>
                    <text x={x0} y={H - padB + 16} textAnchor="middle" fontSize="10" fill="#5f5e5a">{p.period}</text>
                  </g>
                );
              })}
              {linePts && <polyline points={linePts} fill="none" stroke="#c98a2b" strokeWidth="1.6" />}
              {pts.map((p, i) => {
                if (p.growth == null) return null;
                const x0 = padL + i * cw + cw / 2;
                return (
                  <g key={`g${i}`}>
                    <circle cx={x0} cy={yLine(p.growth)} r={2.6} fill={p.growth >= 0 ? UP : DOWN} />
                    <text x={x0} y={yLine(p.growth) - 6} textAnchor="middle" fontSize="9" fontWeight="600" fill={p.growth >= 0 ? UP : DOWN}>
                      {p.growth >= 0 ? "+" : ""}{p.growth}%
                    </text>
                  </g>
                );
              })}
            </svg>
            {ri === 0 && (
              <div className="chart-legend">
                <span className="lg"><i style={{ background: "#3a7bd5" }} />总量</span>
                <span className="lg"><i style={{ background: "#c98a2b" }} />同比增速</span>
                <span className="lg" style={{ color: "#888780" }}>增速点：涨红跌绿</span>
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ===== 行业价格趋势折线图（行业分析专用）=====
export function PriceTrendChart({ rows }: { rows: PriceTrendRow[] }) {
  if (!rows || !rows.length) return null;
  // 多产品叠加在一张图上（归一化为各自首点=100便于对比趋势）
  const colors = ["#d83a3a", "#1d9e75", "#185fa5", "#c98a2b", "#7a5cb8"];
  const W = 580, H = 240, padL = 50, padR = 120, padB = 30, padT = 24;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  // 收集所有时点
  const allPeriods = Array.from(new Set(rows.flatMap(r => r.points.map(p => p.period)))).sort();
  const n = allPeriods.length;
  if (n < 2) return null;
  const xStep = plotW / Math.max(n - 1, 1);
  const xCoord = (i: number) => padL + i * xStep;

  return (
    <div className="chart-card">
      <div className="chart-title">主要产品价格趋势（归一化对比，以首期为100）</div>
      <svg width="100%" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet" role="img" aria-label="价格趋势图" style={{ maxWidth: 620 }}>
        {[0, 0.25, 0.5, 0.75, 1].map((f, i) => (
          <line key={i} x1={padL} y1={padT + plotH * f} x2={W - padR} y2={padT + plotH * f} stroke="#eceae3" strokeWidth="1" />
        ))}
        {allPeriods.map((p, i) => (
          <text key={i} x={xCoord(i)} y={H - padB + 16} textAnchor="middle" fontSize="10" fill="#5f5e5a">{p}</text>
        ))}
        {rows.map((row, ri) => {
          const pts = row.points.filter(p => allPeriods.includes(p.period));
          if (pts.length < 2) return null;
          const base = pts[0].price || 1;
          const color = colors[ri % colors.length];
          const coords = pts.map((p, i) => {
            const pi = allPeriods.indexOf(p.period);
            const norm = ((p.price || 0) / base) * 100;
            const y = padT + plotH - (Math.min(norm, 200) / 200) * plotH;
            return `${xCoord(pi)},${y}`;
          }).join(" ");
          const lastPt = pts[pts.length - 1];
          const lastIdx = allPeriods.indexOf(lastPt.period);
          const lastNorm = ((lastPt.price || 0) / base) * 100;
          const lastY = padT + plotH - (Math.min(lastNorm, 200) / 200) * plotH;
          return (
            <g key={ri}>
              <polyline points={coords} fill="none" stroke={color} strokeWidth="1.8" />
              {pts.map((p, i) => {
                const pi = allPeriods.indexOf(p.period);
                const norm = ((p.price || 0) / base) * 100;
                const y = padT + plotH - (Math.min(norm, 200) / 200) * plotH;
                return <circle key={i} cx={xCoord(pi)} cy={y} r={2.5} fill={color} />;
              })}
              <text x={W - padR + 6} y={lastY} fontSize="10" fill={color} fontWeight="600" dominantBaseline="middle">
                {row.product} {lastPt.price}{row.unit}
              </text>
            </g>
          );
        })}
      </svg>
      <div style={{ fontSize: 10, color: "#888780", marginTop: 4 }}>
        归一化基准：各产品以首期价格为100，对比相对涨跌幅度。实际价格见右侧标注。
      </div>
    </div>
  );
}

// ===== 营收结构饼图（业务板块拆分）=====
const SEG_COLORS = ["#3a7bd5", "#0f6e56", "#c98a2b", "#7a5cb8", "#d83a3a", "#1d9e75", "#854f0b", "#185fa5"];
export function SegmentPieChart({ data }: { data: RevenueSegments }) {
  const segs = data.segments || [];
  if (segs.length < 2) return null;
  const W = 560, H = 280, cx = 170, cy = H / 2, R = 105;
  let startAngle = -Math.PI / 2;

  const slices = segs.map((s, i) => {
    const pct = (s.pct || 0) / 100;
    const angle = pct * Math.PI * 2;
    const endAngle = startAngle + angle;
    const largeArc = angle > Math.PI ? 1 : 0;
    const x1 = cx + R * Math.cos(startAngle);
    const y1 = cy + R * Math.sin(startAngle);
    const x2 = cx + R * Math.cos(endAngle);
    const y2 = cy + R * Math.sin(endAngle);
    const midAngle = startAngle + angle / 2;
    const labelR = R + 16;
    const lx = cx + labelR * Math.cos(midAngle);
    const ly = cy + labelR * Math.sin(midAngle);
    const path = `M ${cx} ${cy} L ${x1} ${y1} A ${R} ${R} 0 ${largeArc} 1 ${x2} ${y2} Z`;
    const color = SEG_COLORS[i % SEG_COLORS.length];
    startAngle = endAngle;
    return { path, color, lx, ly, midAngle, s };
  });

  const currencyLabel = data.currency === "USD" ? "美元" : data.currency === "CNY" ? "人民币" : (data.currency || "");

  return (
    <div className="chart-card">
      <div className="chart-title">
        营收结构（业务板块拆分 · FY{data.fiscal_year}{currencyLabel ? ` · ${currencyLabel}` : ""})
      </div>
      <svg width="100%" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet" role="img" aria-label="营收结构饼图" style={{ maxWidth: 600 }}>
        {slices.map(({ path, color }, i) => (
          <path key={i} d={path} fill={color} opacity={0.85} stroke="#fff" strokeWidth="1.5" />
        ))}
        {slices.map(({ lx, ly, midAngle, s, color }, i) => {
          const anchor = Math.cos(midAngle) >= 0 ? "start" : "end";
          return (s.pct || 0) >= 4 ? (
            <text key={`t${i}`} x={lx} y={ly} textAnchor={anchor} dominantBaseline="middle"
                  fontSize="10" fill={color} fontWeight="600">
              {s.name} {s.pct}%
            </text>
          ) : null;
        })}
      </svg>
      <div className="chart-legend" style={{ flexWrap: "wrap" }}>
        {segs.map((s, i) => (
          <span key={i} className="lg">
            <i style={{ background: SEG_COLORS[i % SEG_COLORS.length] }} />
            {s.name} {fmt(s.revenue)} ({s.pct}%)
          </span>
        ))}
      </div>
    </div>
  );
}

// ===== 同行对比柱状图（主体 vs Peers）=====
export function PeerCompareChart({ rows }: { rows: PeerFinancialRow[] }) {
  if (!rows || rows.length < 2) return null;
  const W = 580, H = 260, padL = 12, padR = 12, padB = 50, padT = 28;
  const plotW = W - padL - padR, plotH = H - padT - padB;
  const n = rows.length;
  const groupW = plotW / n;
  const maxV = Math.max(...rows.map(r => Math.max(r.revenue || 0, r.net_income || 0)), 1);
  const barW = Math.min(26, groupW / 3.5);
  const yBar = (v: number) => padT + plotH - (Math.max(v, 0) / maxV) * plotH;

  return (
    <div className="chart-card">
      <div className="chart-title">同行财务对比（最新年度 · 营收/净利润）</div>
      <svg width="100%" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="xMidYMid meet" role="img" aria-label="同行对比柱状图" style={{ maxWidth: 620 }}>
        {[0, 0.25, 0.5, 0.75, 1].map((f, i) => (
          <line key={i} x1={padL} y1={padT + plotH * f} x2={W - padR} y2={padT + plotH * f} stroke="#eceae3" strokeWidth="1" />
        ))}
        {rows.map((r, i) => {
          const x0 = padL + i * groupW + groupW / 2;
          const rev = r.revenue || 0, ni = r.net_income || 0;
          const isSubject = r.is_subject;
          return (
            <g key={i}>
              {rev > 0 && (
                <>
                  <rect x={x0 - barW - 1} y={yBar(rev)} width={barW}
                        height={padT + plotH - yBar(rev)}
                        fill={isSubject ? "#185fa5" : "#3a7bd5"} rx={2}
                        opacity={isSubject ? 1 : 0.7} />
                  <text x={x0 - barW / 2 - 1} y={yBar(rev) - 3} textAnchor="middle"
                        fontSize="9" fill="#3a7bd5" fontWeight={isSubject ? "700" : "400"}>
                    {fmt(rev)}
                  </text>
                </>
              )}
              {ni > 0 && (
                <>
                  <rect x={x0 + 1} y={yBar(ni)} width={barW}
                        height={padT + plotH - yBar(ni)}
                        fill={isSubject ? "#0a5540" : "#0f6e56"} rx={2}
                        opacity={isSubject ? 1 : 0.7} />
                  <text x={x0 + barW / 2 + 1} y={yBar(ni) - 3} textAnchor="middle"
                        fontSize="9" fill="#0f6e56" fontWeight={isSubject ? "700" : "400"}>
                    {fmt(ni)}
                  </text>
                </>
              )}
              <text x={x0} y={H - padB + 14} textAnchor="middle" fontSize="10"
                    fill={isSubject ? "#185fa5" : "#5f5e5a"} fontWeight={isSubject ? "700" : "400"}>
                {r.name.length > 6 ? r.name.slice(0, 6) + "…" : r.name}
              </text>
              <text x={x0} y={H - padB + 28} textAnchor="middle" fontSize="8" fill="#a0a09a">
                {r.ticker} · {r.period}
              </text>
              {isSubject && (
                <text x={x0} y={H - padB + 40} textAnchor="middle" fontSize="8" fill="#185fa5" fontWeight="600">
                  ★ 分析主体
                </text>
              )}
            </g>
          );
        })}
      </svg>
      <div className="chart-legend">
        <span className="lg"><i style={{ background: "#3a7bd5" }} />营收</span>
        <span className="lg"><i style={{ background: "#0f6e56" }} />净利润</span>
        <span className="lg" style={{ color: "#185fa5" }}>★ 深色=分析主体</span>
      </div>
    </div>
  );
}
