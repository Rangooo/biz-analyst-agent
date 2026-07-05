import { useState } from "react";
import { Insight, TIER_LABEL, VERDICT_META, ChallengeItem } from "../types";

const DIM_LABELS: Record<string, string> = {
  temporal: "时效", conflict_of_interest: "利益相关", source_reliability: "来源可靠性",
  logic: "逻辑", external_consistency: "外部一致性", boundary: "边界条件",
  alternative: "替代理论", missing_evidence: "缺失证据", independence: "独立性",
};

const SEV_COLORS: Record<string, string> = {
  none: "#5f5e5a", low: "#854f0b", medium: "#c98a2b", high: "#a32d2d",
};
const SEV_BG: Record<string, string> = {
  none: "#f1efe8", low: "#faeeda", medium: "#fdf3e0", high: "#fcebeb",
};

function evidenceKey(e: { source_url?: string; content?: string; supports?: boolean }) {
  return `${e.source_url || ""}|${(e.content || "").replace(/\s+/g, "").slice(0, 160)}|${e.supports ? "1" : "0"}`;
}

function uniqueEvidence<T extends { source_url?: string; content?: string; supports?: boolean }>(items: T[] = []): T[] {
  const seen = new Set<string>();
  const out: T[] = [];
  for (const item of items) {
    const key = evidenceKey(item);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push(item);
  }
  return out;
}

function sourceMeta(e: { source_url?: string; source_title?: string; as_of?: string; published_at?: string }) {
  let host = "";
  try {
    host = e.source_url ? new URL(e.source_url).hostname.replace("www.", "") : "";
  } catch {
    host = "";
  }
  const label = e.source_title || host || "未标注来源";
  const date = e.as_of || e.published_at || "";
  return { host, label, date };
}

function EvidenceMeta({ e }: { e: { source_url?: string; source_title?: string; as_of?: string; published_at?: string } }) {
  const meta = sourceMeta(e);
  return (
    <div style={{ fontSize: "11px", color: "#6d6a63", marginTop: 2 }}>
      来源：
      {e.source_url ? (
        <a href={e.source_url} target="_blank" rel="noreferrer" style={{ color: "#185fa5" }}>
          {meta.label}
        </a>
      ) : (
        <span>{meta.label}</span>
      )}
      {meta.host && meta.host !== meta.label && <span> · {meta.host}</span>}
      {meta.date && <span> · {meta.date}</span>}
    </div>
  );
}

export function InsightCard({ insight }: { insight: Insight }) {
  const [open, setOpen] = useState(false);
  const [reasoningOpen, setReasoningOpen] = useState(false);
  const vm = VERDICT_META[insight.verdict] || VERDICT_META.questionable;
  const conf = Math.round(insight.confidence * 100);
  const support = uniqueEvidence(insight.evidence.filter((e) => e.supports));
  const counter = uniqueEvidence(insight.evidence.filter((e) => !e.supports));
  const lastFalsification = insight.falsifications[insight.falsifications.length - 1];

  // 证据质量摘要：按 tier 统计
  const tierCounts: Record<number, number> = {};
  insight.evidence.forEach((e) => { tierCounts[e.tier] = (tierCounts[e.tier] || 0) + 1; });
  const highTierCount = (tierCounts[1] || 0) + (tierCounts[2] || 0) + (tierCounts[3] || 0);
  const totalEvidence = insight.evidence.length;

  return (
    <div className="insight">
      <div className="sec">{insight.section}</div>
      <div className="claim">{insight.claim}</div>

      <div className="meta">
        <span className="badge" style={{ background: vm.bg, color: vm.color }}>
          {vm.label}
        </span>
        <div className="conf-bar" title={`置信度 ${conf}%（数字越大越可信，基于证据强度计算）`}>
          <div className="conf-fill" style={{ width: `${conf}%`, background: vm.color }} />
        </div>
        <span className="conf-val" title="数字越大越可信">置信度 {conf}%</span>
        {lastFalsification && lastFalsification.confidence_before !== undefined && (
          <span className="conf-delta" title={`初始 ${Math.round((lastFalsification.confidence_before || 0) * 100)}% → 终态 ${conf}%`} style={{
            fontSize: "11px", color: conf > Math.round((lastFalsification.confidence_before || 0) * 100) ? "#0f6e56" : "#a32d2d",
            fontWeight: 500,
          }}>
            ({Math.round((lastFalsification.confidence_before || 0) * 100)}%→{conf}%)
          </span>
        )}
        {insight.needs_human && <span className="needs-human">需人工复核</span>}
      </div>

      {/* 推理审计面板 —— 可折叠，展示分析师的推理过程和证据溯源 */}
      {insight.reasoning && (
        <div className="reasoning-audit">
          <div
            className="reasoning-toggle"
            onClick={() => setReasoningOpen(!reasoningOpen)}
            style={{
              display: "flex", alignItems: "center", gap: "6px", cursor: "pointer",
              fontSize: "12px", color: "#185fa5", padding: "4px 0", userSelect: "none",
            }}
          >
            <span style={{ fontSize: "10px", width: "12px", textAlign: "center" }}>
              {reasoningOpen ? "▼" : "▶"}
            </span>
            <span style={{ fontWeight: 500 }}>推理过程</span>
            {totalEvidence > 0 && (
              <span style={{
                fontSize: "10px", color: "#888", fontWeight: 400,
              }}>
                {totalEvidence}条证据{highTierCount > 0 ? `（${highTierCount}条高可信度）` : ""}
              </span>
            )}
          </div>
          {reasoningOpen && (
            <div style={{
              background: "#faf9f6", border: "1px solid #e6e4dd", borderRadius: "8px",
              padding: "10px 12px", marginTop: "4px",
            }}>
              {/* 推理正文 */}
              <div style={{ fontSize: "13px", color: "#2c2c2a", lineHeight: 1.7 }}>
                {insight.reasoning}
              </div>

              {/* 证据溯源摘要 */}
              {totalEvidence > 0 && (
                <div style={{ marginTop: "10px", paddingTop: "8px", borderTop: "1px dashed #e6e4dd" }}>
                  <div style={{ fontSize: "11px", fontWeight: 600, color: "#5f5e5a", marginBottom: "6px" }}>
                    证据溯源（{support.length} 支撑 / {counter.length} 反证）
                  </div>
                  <div style={{ display: "flex", gap: "4px", flexWrap: "wrap", marginBottom: "6px" }}>
                    {Object.entries(tierCounts).sort(([a], [b]) => Number(a) - Number(b)).map(([tier, count]) => (
                      <span key={tier} style={{
                        fontSize: "10px", padding: "1px 6px", borderRadius: "3px",
                        background: Number(tier) <= 3 ? "#e1f5ee" : Number(tier) <= 5 ? "#faeeda" : "#f1efe8",
                        color: Number(tier) <= 3 ? "#0f6e56" : Number(tier) <= 5 ? "#854f0b" : "#5f5e5a",
                      }}>
                        {TIER_LABEL[Number(tier)] || `T${tier}`} x{count}
                      </span>
                    ))}
                  </div>
                  {/* 关键证据（只展示前3条高tier的） */}
                  {support.filter(e => e.tier <= 4).slice(0, 3).map((e, i) => (
                    <div key={i} style={{
                      fontSize: "11px", color: "#5f5e5a", padding: "3px 0",
                      borderBottom: "1px solid #f1efe8",
                    }}>
                      <span style={{
                        fontSize: "9px", padding: "1px 4px", borderRadius: "3px",
                        background: "#e1f5ee", color: "#0f6e56", marginRight: "4px",
                      }}>
                        {TIER_LABEL[e.tier]}
                      </span>
                      {e.content?.slice(0, 80)}{e.content && e.content.length > 80 ? "..." : ""}
                      <EvidenceMeta e={e} />
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}

      {/* 红队推翻路径 —— 由红队基于九维挑战独立判断，非分析师自述 */}
      {insight.falsifiable_condition && (
        <div className="falsify-cond" style={{
          background: insight.verdict === "refuted" ? "#fcebeb" :
                     insight.verdict === "questionable" || insight.verdict === "unverifiable" ? "#fdf3e0"
                     : "#f0f9eb",
          color: insight.verdict === "refuted" ? "#a32d2d" :
                 insight.verdict === "questionable" || insight.verdict === "unverifiable" ? "#c98a2b"
                 : "#0f6e56",
          borderLeft: `3px solid ${
            insight.verdict === "refuted" ? "#a32d2d" :
            insight.verdict === "questionable" || insight.verdict === "unverifiable" ? "#c98a2b"
            : "#0f6e56"
          }`,
        }}>
        {insight.verdict === "refuted" ? "推翻路径" :
         insight.verdict === "questionable" || insight.verdict === "unverifiable" ? "存疑要点" :
         "无有效攻击点"}：{insight.falsifiable_condition}
        </div>
      )}

      {/* 攻防时间线摘要 —— 只显示有问题的维度（severity ≠ none） */}
      {lastFalsification && lastFalsification.challenges && (() => {
        const problemDims = lastFalsification.challenges.filter(
          (c) => c.severity && (c.severity || "").toLowerCase() !== "none"
        );
        if (problemDims.length === 0) return null;
        return (
          <div className="timeline-summary" style={{
            display: "flex", gap: "4px", flexWrap: "wrap", marginTop: "8px",
            fontSize: "11px",
          }}>
            {problemDims.map((c, i) => {
              const sev = (c.severity || "").toLowerCase();
              return (
                <span key={i} title={`${DIM_LABELS[c.dimension] || c.dimension}: ${c.challenge}`}
                  style={{
                    padding: "2px 6px", borderRadius: "3px",
                    background: SEV_BG[sev] || "#f1efe8",
                    color: SEV_COLORS[sev] || "#5f5e5a",
                    border: `1px solid ${SEV_COLORS[sev] || "#ddd"}33`,
                  }}>
                  {DIM_LABELS[c.dimension] || c.dimension?.slice(0, 4)}
                  ·{sev}
                </span>
              );
            })}
          </div>
        );
      })()}

      {(support.length > 0 || counter.length > 0 || insight.falsifications.length > 0) && (
        <span className="collapse-toggle" onClick={() => setOpen(!open)}>
          {open ? "收起攻防详情 ▲" : `展开攻防详情 (${support.length}支撑/${counter.length}反证/${insight.falsifications.length}轮证伪) ▼`}
        </span>
      )}

      {open && (
        <div className="attack-defense-timeline">
          {/* 攻防时间线 */}
          {insight.falsifications.length > 0 && (
            <div className="ad-section">
              <div className="fb-title">攻防时间线</div>
              {insight.falsifications.map((r, i) => (
                <div key={i} className="ad-round" style={{
                  borderLeft: "3px solid #e6e4dd", paddingLeft: "12px",
                  marginBottom: "12px", position: "relative",
                }}>
                  {/* 步骤1: 分析师提出论点 */}
                  <div className="ad-step" style={{ marginBottom: "6px" }}>
                    <span style={{ color: "#185fa5", fontWeight: 600, fontSize: "12px" }}>① 分析师论点</span>
                    <div style={{ fontSize: "12px", color: "#5f5e5a", marginTop: "2px" }}>
                      置信度 {(r.confidence_before * 100).toFixed(0)}%
                    </div>
                  </div>

                  {/* 步骤2: 红队九维挑战 */}
                  {r.challenges && r.challenges.length > 0 && (
                    <div className="ad-step" style={{ marginBottom: "6px" }}>
                      <span style={{ color: "#a32d2d", fontWeight: 600, fontSize: "12px" }}>② 红队九维挑战</span>
                      <div style={{ marginTop: "4px" }}>
                        {r.challenges.filter(c => c.severity && c.severity.toLowerCase() !== "none").map((c, j) => {
                          const sev = (c.severity || "").toLowerCase();
                          return (
                            <div key={j} style={{
                              fontSize: "11px", padding: "3px 6px", marginBottom: "2px",
                              background: SEV_BG[sev] || "#f1efe8",
                              borderLeft: `2px solid ${SEV_COLORS[sev] || "#ddd"}`,
                            }}>
                              <b>{DIM_LABELS[c.dimension] || c.dimension}</b>
                              <span style={{ color: SEV_COLORS[sev], marginLeft: "4px" }}>{sev}</span>
                              {" — "}{c.challenge}
                            </div>
                          );
                        })}
                        {r.challenges.filter(c => c.severity && c.severity.toLowerCase() !== "none").length === 0 && (
                          <div style={{ fontSize: "11px", color: "#0f6e56" }}>九维均无明显挑战</div>
                        )}
                      </div>
                      <div style={{ fontSize: "11px", color: "#999", marginTop: "2px" }}>
                        综合判定：<b>{r.overall_assessment || "—"}</b>
                      </div>
                    </div>
                  )}

                  {/* 步骤3: 反证搜索 */}
                  {r.counter_evidence && r.counter_evidence.length > 0 && (
                    <div className="ad-step" style={{ marginBottom: "6px" }}>
                      <span style={{ color: "#a32d2d", fontWeight: 600, fontSize: "12px" }}>③ 红队找到反证</span>
                      {uniqueEvidence(r.counter_evidence).map((e, j) => (
                        <div key={j} className="ev counter" style={{ fontSize: "11px", marginTop: "2px" }}>
                          <span className="tier">{TIER_LABEL[e.tier]}</span>
                          <span>
                            {e.content?.slice(0, 100)}
                            <EvidenceMeta e={e} />
                          </span>
                        </div>
                      ))}
                    </div>
                  )}

                  {/* 步骤4: 自我迭代补强 */}
                  {r.support_evidence && r.support_evidence.length > 0 && (
                    <div className="ad-step" style={{ marginBottom: "6px" }}>
                      <span style={{ color: "#0f6e56", fontWeight: 600, fontSize: "12px" }}>④ 分析师补强证据</span>
                      {uniqueEvidence(r.support_evidence).map((e, j) => (
                        <div key={j} className="ev" style={{ fontSize: "11px", marginTop: "2px" }}>
                          <span className="tier">{TIER_LABEL[e.tier]}</span>
                          <span>
                            {e.content?.slice(0, 100)}
                            <EvidenceMeta e={e} />
                          </span>
                        </div>
                      ))}
                    </div>
                  )}

                  {/* 步骤5: 终审裁决 */}
                  <div className="ad-step">
                    <span style={{ color: vm.color, fontWeight: 600, fontSize: "12px" }}>⑤ 终审裁决</span>
                    <div style={{ fontSize: "12px", marginTop: "2px" }}>
                      置信度 {(r.confidence_before * 100).toFixed(0)}% → <b>{(r.confidence_after * 100).toFixed(0)}%</b>
                      {" · "}<span style={{ color: vm.color }}>{vm.label}</span>
                    </div>
                    {r.note && <div style={{ fontSize: "11px", color: "#888", marginTop: "2px" }}>{r.note}</div>}
                    {r.refinement_note && <div style={{ fontSize: "11px", color: "#c98a2b", marginTop: "2px" }}>{r.refinement_note}</div>}
                  </div>
                </div>
              ))}
            </div>
          )}

          {/* 支撑证据列表 */}
          {support.length > 0 && (
            <div className="evidence-list">
              <div className="fb-title" style={{ color: "#0f6e56" }}>支撑证据</div>
              {support.map((e) => (
                <div className="ev" key={e.id}>
                  <span className="tier">{TIER_LABEL[e.tier]}</span>
                  <span>
                    {e.content}
                    <EvidenceMeta e={e} />
                  </span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
