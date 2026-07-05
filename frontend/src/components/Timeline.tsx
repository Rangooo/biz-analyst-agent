import { useState } from "react";
import type { TraceEvent } from "../types";

const STAGE_NAME: Record<string, string> = {
  scope: "界定",
  collect: "采集",
  analyze: "分析",
  falsify: "证伪",
  refine: "迭代",
  report: "报告",
  llm_call: "调用",
  error: "错误",
};

// 需要折叠的事件类型（证据/搜索结果/LLM调用详情）
const COLLAPSIBLE_TYPES = new Set(["evidence", "search"]);

export function Timeline({ events }: { events: TraceEvent[] }) {
  // 按阶段分组，同阶段连续事件合并展示
  const groups: { stage: string; items: TraceEvent[] }[] = [];
  for (const ev of events) {
    const last = groups[groups.length - 1];
    if (last && last.stage === ev.stage) {
      last.items.push(ev);
    } else {
      groups.push({ stage: ev.stage, items: [ev] });
    }
  }

  return (
    <div className="timeline">
      {groups.map((g, gi) => (
        <div key={gi} className={`trace-group stage-${g.stage}`}>
          <div className="tg-header">
            <span className="tg-dot" />
            <span className="tg-stage">{STAGE_NAME[g.stage] || g.stage}</span>
            <span className="tg-count">{g.items.length} 步</span>
          </div>
          <div className="tg-items">
            {g.items.map((ev) => (
              <TraceItem key={ev.id} ev={ev} />
            ))}
          </div>
        </div>
      ))}
    </div>
  );
}

function TraceItem({ ev }: { ev: TraceEvent }) {
  const [open, setOpen] = useState(false);
  const collapsible = COLLAPSIBLE_TYPES.has(ev.type);
  const hasDetail = ev.detail && ev.detail.length > 0;
  const hasUrl = ev.payload?.url;
  const sourceLabel = ev.payload?.source || (hasUrl ? hostOf(ev.payload.url) : "");
  const evidenceDate = ev.payload?.as_of || ev.payload?.published;

  // 证据类：标题显示编号+来源摘要，内容折叠
  if (ev.type === "evidence") {
    return (
      <div className={`ti type-evidence${open ? " open" : ""}`}>
        <div className="ti-title" onClick={() => setOpen(!open)}>
          <span className="ti-arrow">{open ? "▾" : "▸"}</span>
          <span className="ti-text">{ev.title}</span>
          {sourceLabel && <span className="ti-host">{sourceLabel}</span>}
        </div>
        {open && (hasDetail || hasUrl || evidenceDate) && (
          <div className="ti-body">
            {hasDetail && <div className="ti-detail">{ev.detail}</div>}
            {(sourceLabel || evidenceDate) && (
              <div className="ti-detail" style={{ fontSize: 11 }}>
                来源：{sourceLabel || "未标注"}{evidenceDate ? ` · 日期：${evidenceDate}` : ""}
              </div>
            )}
            {hasUrl && (
              <a className="ti-url" href={ev.payload.url} target="_blank" rel="noreferrer">
                {ev.payload.url}
              </a>
            )}
          </div>
        )}
      </div>
    );
  }

  // 搜索类：显示 query，结果折叠
  if (ev.type === "search") {
    return (
      <div className={`ti type-search${open ? " open" : ""}`}>
        <div className="ti-title" onClick={() => setOpen(!open)}>
          <span className="ti-arrow">{open ? "▾" : "▸"}</span>
          <span className="ti-text">{ev.title}</span>
          {ev.payload?.source && <span className="ti-src">{ev.payload.source}</span>}
        </div>
        {open && hasDetail && <div className="ti-body"><div className="ti-detail">{ev.detail}</div></div>}
      </div>
    );
  }

  // 步骤类（thinking/narrative_ready 等）：标题+简短摘要直接展示
  return (
    <div className={`ti type-${ev.type}`}>
      <div className="ti-title">
        <span className="ti-text">{ev.title}</span>
        {ev.payload?.length && <span className="ti-meta">{ev.payload.length} 字符</span>}
      </div>
      {hasDetail && <div className="ti-detail">{ev.detail}</div>}
    </div>
  );
}

function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace("www.", "");
  } catch {
    return "";
  }
}
