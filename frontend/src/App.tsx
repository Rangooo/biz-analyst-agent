import { useEffect, useRef, useState } from "react";
import {
  AnalysisRun,
  DataSourceInfo,
  Evidence,
  HealthInfo,
  IndustryMetrics,
  Insight,
  ObjectProfile,
  PeerFinancialRow,
  ProviderInfo,
  QualityEval,
  RevenueSegments,
  RunSummary,
  TraceEvent,
} from "./types";
import { Timeline } from "./components/Timeline";
import { InsightCard } from "./components/InsightCard";
import { ReportView } from "./components/ReportView";
import { SettingsPanel } from "./components/SettingsPanel";

const EXAMPLES = ["奇富科技", "Snowflake", "字节跳动", "中国新能源汽车行业", "DeepSeek"];

/** Terminal statuses — keep in sync with backend _TERMINAL_STATUSES */
const TERMINAL_STATUSES = ["done", "partial", "needs_human", "interrupted", "error"] as const;

export default function App() {
  const [query, setQuery] = useState("");
  const [provider, setProvider] = useState("deepseek");
  const [redTeamSel, setRedTeamSel] = useState("");
  const [reviewerSel, setReviewerSel] = useState("");
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  const [dataSources, setDataSources] = useState<DataSourceInfo[]>([]);
  const [searchOk, setSearchOk] = useState(true);
  const [searchDegraded, setSearchDegraded] = useState(false);
  const [demoMode, setDemoMode] = useState(false);
  const [sameSource, setSameSource] = useState(false);
  const [analyst, setAnalyst] = useState("");
  const [redTeam, setRedTeam] = useState("");
  const [reviewer, setReviewer] = useState("");
  const [roleReason, setRoleReason] = useState("");
  const [rolePlan, setRolePlan] = useState<{ analyst: string | null; red_team: string | null; reviewer: string | null }>({ analyst: null, red_team: null, reviewer: null });
  const [recentRuns, setRecentRuns] = useState<RunSummary[]>([]);
  const [resolveCands, setResolveCands] = useState<{ name: string; note: string; kind: string }[] | null>(null);
  const [resolving, setResolving] = useState(false);
  const [running, setRunning] = useState(false);
  const [status, setStatus] = useState("");
  const [trace, setTrace] = useState<TraceEvent[]>([]);
  const [profile, setProfile] = useState<ObjectProfile | null>(null);
  const [insights, setInsights] = useState<Insight[]>([]);
  const [narrativeMd, setNarrativeMd] = useState("");
  const [financials, setFinancials] = useState<{ period: string; revenue?: number | null; net_income?: number | null; rev_growth?: number | null; ni_growth?: number | null }[]>([]);
  const [evidencePool, setEvidencePool] = useState<Evidence[]>([]);
  const [qualityEval, setQualityEval] = useState<QualityEval | undefined>(undefined);
  const [industryMetrics, setIndustryMetrics] = useState<IndustryMetrics | null>(null);
  const [revenueSegments, setRevenueSegments] = useState<RevenueSegments | null>(null);
  const [peerFinancials, setPeerFinancials] = useState<PeerFinancialRow[]>([]);
  const [dataAsOf, setDataAsOf] = useState("");
  const [tab, setTab] = useState<"insights" | "document">("insights");
  const [showSettings, setShowSettings] = useState(false);
  const esRef = useRef<EventSource | null>(null);
  const [runId, setRunId] = useState("");
  // Human-in-the-loop: Scope 审查
  const [scopeReview, setScopeReview] = useState<{ sections: string[]; profile: ObjectProfile } | null>(null);
  const [scopeSections, setScopeSections] = useState<string[]>([]);
  const [dragScopeIndex, setDragScopeIndex] = useState<number | null>(null);
  // PDF 上传
  const [pdfUploading, setPdfUploading] = useState(false);
  const [pdfPath, setPdfPath] = useState("");
  const [pdfResult, setPdfResult] = useState<{
    company_name: string; stock_code: string; report_year: string;
    years_extracted: number; metrics_found: string[];
  } | null>(null);
  const pdfInputRef = useRef<HTMLInputElement>(null);

  async function handlePdfUpload(file: File) {
    setPdfUploading(true);
    setPdfResult(null);
    try {
      const form = new FormData();
      form.append("file", file);
      const url = runId ? `/api/upload-annual-report?run_id=${runId}` : "/api/upload-annual-report";
      const res = await fetch(url, { method: "POST", body: form });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        alert(`PDF 解析失败: ${err.detail || JSON.stringify(err)}`);
        return;
      }
      const data = await res.json();
      setPdfResult(data);
    } catch (e: unknown) {
      alert(`PDF 上传异常: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setPdfUploading(false);
    }
  }

  async function handlePdfPathImport() {
    const path = pdfPath.trim();
    if (!path) return;
    setPdfUploading(true);
    setPdfResult(null);
    try {
      const res = await fetch("/api/import-annual-report-path", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ path, run_id: runId || "" }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: res.statusText }));
        alert(`PDF 解析失败: ${err.detail || JSON.stringify(err)}`);
        return;
      }
      const data = await res.json();
      setPdfResult(data);
      setPdfPath("");
    } catch (e: unknown) {
      alert(`PDF 路径导入异常: ${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setPdfUploading(false);
    }
  }

  function moveScopeSection(from: number, to: number) {
    if (from === to || from < 0 || to < 0) return;
    setScopeSections((prev) => {
      if (from >= prev.length || to >= prev.length) return prev;
      const next = [...prev];
      const [item] = next.splice(from, 1);
      next.splice(to, 0, item);
      return next;
    });
  }

  async function loadHealth(analystOverride?: string, redTeamOverride?: string) {
    try {
      const params = new URLSearchParams();
      if (analystOverride) params.set("analyst", analystOverride);
      if (redTeamOverride) params.set("red_team", redTeamOverride);
      const url = params.toString() ? `/api/health?${params}` : "/api/health";
      const d: HealthInfo = await fetch(url).then((r) => r.json());
      setProviders(d.providers || []);
      setSearchOk(!!d.search_backend);
      setSearchDegraded(d.search_status?.status === "degraded");
      setDemoMode(!!d.demo_mode);
      setSameSource(!!d.same_source_review);
      setAnalyst(d.analyst_provider || "");
      setRedTeam(d.red_team_provider || "");
      setReviewer(d.reviewer_provider || "");
      setRoleReason((d as any).role_plan?.reason || "");
      setRolePlan((d as any).role_plan || { analyst: null, red_team: null, reviewer: null });
      setDataSources(d.data_sources || []);
      // 只在当前选择不可用（或未设置）时才自动切到首个可用，否则保留用户选择
      setProvider((prev) => {
        const cur = (d.providers || []).find((p) => p.key === prev);
        if (cur && cur.available) return prev;
        const firstAvail = (d.providers || []).find((p) => p.available);
        return firstAvail ? firstAvail.key : prev;
      });
      setRedTeamSel((prev) => {
        const cur = (d.providers || []).find((p) => p.key === prev);
        if (cur && cur.available) return prev;
        return d.red_team_provider || prev;
      });
      setReviewerSel((prev) => {
        const cur = (d.providers || []).find((p) => p.key === prev);
        if (cur && cur.available) return prev;
        return d.reviewer_provider || prev;
      });
    } catch {
      /* ignore */
    }
  }

  async function loadRecentRuns() {
    try {
      const runs: RunSummary[] = await fetch("/api/runs").then((r) => r.json());
      setRecentRuns(runs || []);
    } catch {
      setRecentRuns([]);
    }
  }

  function applyRun(run: AnalysisRun) {
    setRunId(run.id);
    setQuery(run.query || "");
    setTrace(run.trace || []);
    setInsights(run.insights || []);
    setNarrativeMd(run.narrative_md || "");
    setFinancials(run.financials || []);
    setEvidencePool(run.evidence_pool || []);
    setQualityEval(run.quality_eval);
    setIndustryMetrics(run.industry_metrics || null);
    setRevenueSegments(run.revenue_segments || null);
    setPeerFinancials(run.peer_financials || []);
    setDataAsOf(run.data_as_of || "");
    setProfile(run.profile);
    setSameSource(run.same_source_review);
    setStatus(run.status);
    setRunning(false);
    setTab(run.narrative_md ? "document" : "insights");
  }

  async function openRun(id: string) {
    if (!id || running) return;
    try {
      const run: AnalysisRun = await fetch(`/api/run/${id}`).then((r) => r.json());
      applyRun(run);
    } catch {
      setStatus("error");
    }
  }

  function pickProvider(key: string) {
    setProvider(key);
    loadHealth(key, redTeamSel || undefined); // 按所选主分析+红队刷新配对与同源判定
  }

  function providerLabel(key: string) {
    const p = providers.find((x) => x.key === key);
    return p ? p.label : key;
  }

  function pickRedTeam(key: string) {
    setRedTeamSel(key);
    loadHealth(provider, key); // 按所选红队刷新同源判定
  }

  function pickReviewer(key: string) {
    setReviewerSel(key);
  }

  useEffect(() => {
    loadHealth();
    loadRecentRuns();
  }, []);

  function reset() {
    setTrace([]);
    setProfile(null);
    setInsights([]);
    setNarrativeMd("");
    setFinancials([]);
    setEvidencePool([]);
    setQualityEval(undefined);
    setIndustryMetrics(null);
    setDataAsOf("");
    setStatus("");
    setScopeReview(null);
    setScopeSections([]);
  }

  async function confirmScope(sections?: string[]) {
    if (!runId) return;
    try {
      await fetch(`/api/confirm_scope/${runId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ sections: sections || null }),
      });
    } catch { /* ignore */ }
    setScopeReview(null);
  }

  async function start() {
    if (!query.trim() || running || resolving) return;
    // 实体消歧：模糊输入（如"蚂蚁"）先确认指向哪个主体，再进入分析
    setResolving(true);
    setResolveCands(null);
    try {
      const r = await fetch("/api/resolve", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: query.trim() }),
      });
      const d = await r.json();
      if (d.ambiguous && (d.candidates || []).length > 1) {
        setResolving(false);
        setResolveCands(d.candidates);
        return;
      }
      const resolved = d.default || query.trim();
      runAnalysis(resolved === query.trim() ? query.trim() : `${resolved}（${query.trim()}）`);
    } catch {
      setResolving(false);
      runAnalysis(query.trim());
    }
  }

  async function runAnalysis(q: string) {
    setResolveCands(null);
    setResolving(false);
    reset();
    setRunning(true);
    setStatus("running");
    setTab("insights");
    try {
      const res = await fetch("/api/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query: q, provider, red_team_provider: redTeamSel || undefined, reviewer_provider: reviewerSel || undefined }),
      });
      const { run_id } = await res.json();
      setRunId(run_id);
      connectSSE(run_id);
    } catch {
      setRunning(false);
      setStatus("error");
    }
  }

  function handleTrace(ev: TraceEvent) {
    if (ev.type === "scope_done" && ev.payload.profile) {
      setProfile(ev.payload.profile as ObjectProfile);
    }
    // Human-in-the-loop: 暂停等待用户确认
    if (ev.type === "review_point" && ev.payload.review_type === "scope") {
      const sections = (ev.payload.sections || []) as string[];
      setScopeReview({ sections, profile: ev.payload.profile as ObjectProfile });
      setScopeSections([...sections]);
    }
    if (ev.type === "insight") {
      setInsights((prev) => {
        if (prev.find((i) => i.id === ev.payload.insight_id)) return prev;
        return [
          ...prev,
          {
            id: ev.payload.insight_id,
            section: ev.title.replace("洞察 · ", ""),
            claim: ev.detail,
            reasoning: "",
            falsifiable_condition: ev.payload.falsifiable_condition || "",
            evidence: [],
            confidence: ev.payload.confidence ?? 0.5,
            verdict: "questionable",
            falsifications: [],
            is_falsifiable: ev.payload.falsifiable ?? true,
            stale_count: 0,
            needs_human: false,
          },
        ];
      });
    }
    if (ev.type === "score" && ev.payload.insight_id) {
      setInsights((prev) =>
        prev.map((i) =>
          i.id === ev.payload.insight_id
            ? {
                ...i,
                verdict: ev.payload.verdict || i.verdict,
                confidence: ev.payload.confidence ?? i.confidence,
              }
            : i
        )
      );
    }
    if (ev.type === "narrative_ready") {
      // 完整文档生成完，切到文档 tab 可选
    }
  }

  function connectSSE(id: string) {
    const es = new EventSource(`/api/stream/${id}`);
    esRef.current = es;
    es.addEventListener("trace", (e: MessageEvent) => {
      const ev: TraceEvent = JSON.parse(e.data);
      setTrace((t) => [...t, ev]);
      handleTrace(ev);
    });
    es.addEventListener("result", (e: MessageEvent) => {
      const run: AnalysisRun = JSON.parse(e.data);
      applyRun(run);
      loadRecentRuns();
      es.close();
    });
    es.addEventListener("error", () => {
      setRunning(false);
      setStatus("error");
      es.close();
    });
  }

  async function resumeRun() {
    if (!runId) return;
    setRunning(true);
    setStatus("running");
    try {
      const res = await fetch(`/api/resume/${runId}`, { method: "POST" });
      if (!res.ok) {
        const e = await res.json().catch(() => ({}));
        alert(e.detail || "恢复失败：无可用检查点");
        setRunning(false);
        setStatus("needs_human");
        return;
      }
      connectSSE(runId);
    } catch {
      setRunning(false);
      setStatus("error");
    }
  }

  const statusPill =
    status === "done" ? (
      <span className="status-pill status-done">已完成</span>
    ) : status === "partial" ? (
      <span className="status-pill status-partial">部分完成</span>
    ) : status === "needs_human" ? (
      <span className="status-pill status-partial">需人工</span>
    ) : status === "error" ? (
      <span className="status-pill status-error">出错</span>
    ) : null;

  const [showLimit, setShowLimit] = useState(5);
  const [expanded, setExpanded] = useState(false);
  const uniqueRecentRuns = recentRuns.filter(
    (run, idx, arr) => idx === arr.findIndex((item) => item.query === run.query && item.status === run.status)
  );
  const doneRunsAll = uniqueRecentRuns.filter(
    (r) => r.status === "done" || r.status === "partial" || r.status === "needs_human"
  ).slice(0, 15);
  const doneRuns = expanded ? doneRunsAll : doneRunsAll.slice(0, 5);
  const formatRunTime = (value: string) => {
    const dt = new Date(value);
    if (Number.isNaN(dt.getTime())) return value;
    return dt.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
  };

  return (
    <div className="app">
      <div className="header">
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div>
            <h1>商业分析 Agent</h1>
            <div className="sub">
              自主分析流水线 · <b>采集 → 分析 → 证伪 → 迭代 → 报告</b> ·
              每条洞察带证据链与置信度，红队对抗证伪 —— 区别于 chatbot
            </div>
          </div>
          <button className="btn ghost" onClick={() => setShowSettings((s) => !s)}>
            ⚙ 数据源 / Key
          </button>
        </div>
      </div>

      {showSettings && (
        <SettingsPanel
          providers={providers}
          dataSources={dataSources}
          rolePlan={rolePlan}
          provider={provider}
          redTeamSel={redTeamSel}
          reviewerSel={reviewerSel}
          onPickProvider={pickProvider}
          onPickRedTeam={pickRedTeam}
          onPickReviewer={pickReviewer}
          onClose={() => setShowSettings(false)}
          onSaved={loadHealth}
        />
      )}

      <div className="input-bar">
        <input
          type="text"
          placeholder="输入公司或行业，例如：奇富科技 / Snowflake / 中国新能源汽车行业"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && start()}
          disabled={running}
        />
        <select
          value={provider}
          onChange={(e) => pickProvider(e.target.value)}
          disabled={running}
          aria-label="主分析模型"
          title="选择作为主分析的模型"
        >
          {providers.map((p) => (
            <option key={p.key} value={p.key} disabled={!p.available}>
              {p.label}
              {p.available ? "" : " · 未配置"}
            </option>
          ))}
          {providers.length === 0 && <option value="deepseek">DeepSeek</option>}
        </select>
        <select
          value={redTeamSel}
          onChange={(e) => pickRedTeam(e.target.value)}
          disabled={running || demoMode}
          aria-label="红队模型"
          title="选择作为红队证伪的模型（异源更佳）"
          className="select-red"
        >
          <option value="">红队(默认)</option>
          {providers.map((p) => (
            <option key={p.key} value={p.key} disabled={!p.available}>
              {p.label}
              {p.available ? "" : " · 未配置"}
            </option>
          ))}
        </select>
        <select
          value={reviewerSel}
          onChange={(e) => pickReviewer(e.target.value)}
          disabled={running || demoMode}
          aria-label="终审模型"
          title="选择作为终审撰写报告的模型（推理模型更佳）"
          className="select-reviewer"
        >
          <option value="">终审(默认)</option>
          {providers.map((p) => (
            <option key={p.key} value={p.key} disabled={!p.available}>
              {p.label}
              {p.available ? "" : " · 未配置"}
            </option>
          ))}
        </select>
        <button className="btn" onClick={start} disabled={running || resolving || !query.trim()}>
          {resolving ? "确认实体中…" : running ? "分析中…" : "开始分析"}
        </button>
        <input
          ref={pdfInputRef}
          type="file"
          accept=".pdf"
          style={{ display: "none" }}
          onChange={(e) => {
            const file = e.target.files?.[0];
            if (file) handlePdfUpload(file);
            e.target.value = "";
          }}
        />
        <button
          className="btn ghost"
          onClick={() => pdfInputRef.current?.click()}
          disabled={pdfUploading}
          title="上传 A 股年报 PDF，自动提取三大报表数据"
        >
          {pdfUploading ? "解析中…" : "📄 上传年报"}
        </button>
        <input
          className="pdf-path-input"
          value={pdfPath}
          onChange={(e) => setPdfPath(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") handlePdfPathImport();
          }}
          placeholder="或粘贴本地PDF路径"
          disabled={pdfUploading}
          title="浏览器无法读取文件时，粘贴本机 PDF 路径导入"
        />
        <button
          className="btn ghost"
          onClick={handlePdfPathImport}
          disabled={pdfUploading || !pdfPath.trim()}
          title="从后端本机路径导入 PDF"
        >
          导入路径
        </button>
        {statusPill}
        {status === "needs_human" && runId && !running && (
          <button className="btn" onClick={resumeRun}>从断点继续</button>
        )}
      </div>

      {resolveCands && (
        <div className="resolve-pop">
          <div className="resolve-title">「{query}」可能指代多个主体，请确认：</div>
          {resolveCands.map((c, i) => (
            <button key={i} className="resolve-opt" onClick={() => runAnalysis(c.name)}>
              <b>{c.name}</b>{c.note && <span className="resolve-note"> — {c.note}</span>}
            </button>
          ))}
          <button className="resolve-cancel" onClick={() => setResolveCands(null)}>取消</button>
        </div>
      )}

      {/* Human-in-the-loop: Scope 审查确认 */}
      {scopeReview && (
        <div className="scope-review">
          <div className="scope-review-header">
            <h3>确认分析维度</h3>
            <span className="scope-review-sub">
              对象：<b>{scopeReview.profile?.name}</b> · 可拖拽排序、删除或新增维度
            </span>
          </div>
          <div className="scope-sections-list">
            {scopeSections.map((s, i) => (
              <div
                key={i}
                className={`scope-section-item ${dragScopeIndex === i ? "scope-section-dragging" : ""}`}
                onDragOver={(e) => {
                  e.preventDefault();
                  e.dataTransfer.dropEffect = "move";
                }}
                onDrop={(e) => {
                  e.preventDefault();
                  if (dragScopeIndex !== null) moveScopeSection(dragScopeIndex, i);
                  setDragScopeIndex(null);
                }}
                onDragEnd={() => setDragScopeIndex(null)}
              >
                <button
                  type="button"
                  className="scope-section-drag"
                  draggable
                  onDragStart={(e) => {
                    setDragScopeIndex(i);
                    e.dataTransfer.effectAllowed = "move";
                    e.dataTransfer.setData("text/plain", String(i));
                  }}
                  title="拖拽排序"
                  aria-label={`拖拽排序第 ${i + 1} 个维度`}
                >↕</button>
                <button
                  type="button"
                  className="scope-section-move"
                  onClick={() => moveScopeSection(i, i - 1)}
                  disabled={i === 0}
                  title="上移"
                  aria-label={`上移第 ${i + 1} 个维度`}
                >▲</button>
                <button
                  type="button"
                  className="scope-section-move"
                  onClick={() => moveScopeSection(i, i + 1)}
                  disabled={i === scopeSections.length - 1}
                  title="下移"
                  aria-label={`下移第 ${i + 1} 个维度`}
                >▼</button>
                <span className="scope-section-num">{i + 1}</span>
                <input
                  type="text"
                  value={s}
                  onChange={(e) => {
                    const updated = [...scopeSections];
                    updated[i] = e.target.value;
                    setScopeSections(updated);
                  }}
                  className="scope-section-input"
                />
                <button
                  className="scope-section-del"
                  onClick={() => setScopeSections(scopeSections.filter((_, j) => j !== i))}
                  title="删除该维度"
                >×</button>
              </div>
            ))}
          </div>
          <div className="scope-review-actions">
            <button
              className="btn ghost"
              onClick={() => setScopeSections([...scopeSections, ""])}
            >+ 新增维度</button>
            <div style={{ flex: 1 }} />
            <button
              className="btn ghost"
              onClick={() => confirmScope()}
            >按原维度继续</button>
            <button
              className="btn"
              onClick={() => confirmScope(scopeSections.filter(s => s.trim()))}
            >确认并继续</button>
          </div>
        </div>
      )}

      {pdfResult && (
        <div className="pdf-result-banner">
          <span className="pdf-result-icon">✅</span>
          <span>
            已解析 <b>{pdfResult.company_name}</b>({pdfResult.stock_code})
            {pdfResult.report_year}年报 — {pdfResult.years_extracted}年数据,
            {pdfResult.metrics_found.length}项指标
          </span>
          <button className="btn-close" onClick={() => setPdfResult(null)}>×</button>
        </div>
      )}

      {!demoMode && providers.some((p) => p.available) && (
        <div className={`pairing ${sameSource ? "pairing-warn" : "pairing-ok"}`}>
          <span className="pairing-label">主分析</span>
          <b>{providerLabel(provider)}</b>
          <span className="pairing-sep">·</span>
          <span className="pairing-label">红队</span>
          <b>{providerLabel(redTeamSel || redTeam)}</b>
          <span className="pairing-sep">·</span>
          <span className="pairing-label">终审</span>
          <b>{providerLabel(reviewerSel || reviewer)}</b>
          {sameSource ? (
            <span className="pairing-note"> 同源审查降级，证伪独立性受限——建议再配一个异源模型作红队</span>
          ) : (
            <span className="pairing-note"> 双模型对抗证伪 · 终审模型撰写完整报告</span>
          )}
          {(() => {
            const hasOverride = (provider && analyst && provider !== analyst)
              || (redTeamSel && redTeam && redTeamSel !== redTeam)
              || (reviewerSel && reviewer && reviewerSel !== reviewer);
            if (hasOverride) return <span className="pairing-reason" style={{ color: "#06a" }}> [手动选模型已生效]</span>;
            if (roleReason) return <span className="pairing-reason"> 默认指派：{roleReason}</span>;
            return null;
          })()}
        </div>
      )}

      <div className="examples">
        试试：
        {EXAMPLES.map((ex) => (
          <a key={ex} onClick={() => !running && setQuery(ex)}>
            {ex}
          </a>
        ))}
      </div>

      {uniqueRecentRuns.length > 0 && (
        <div className="recent-runs">
          <span className="recent-label">最近报告</span>
          {doneRunsAll.length > 0 ? (
            <div className="recent-row">
              {doneRuns.map((r, i) => (
                <button key={r.id} className={`recent-chip${i === 0 ? " latest" : ""}`} onClick={() => openRun(r.id)} disabled={running} title={r.query}>
                  <span className="recent-chip-query">{r.query}</span>
                </button>
              ))}
              {doneRunsAll.length > 5 && (
                <button className="recent-chip more" onClick={() => setExpanded(!expanded)} disabled={running}>
                  {expanded ? "收起" : `+${doneRunsAll.length - 5}`}
                </button>
              )}
            </div>
          ) : (
            <span className="recent-muted">暂无可打开的完成报告</span>
          )}
        </div>
      )}

      {demoMode && (
        <div className="warn-bar">
          🎬 <b>演示模式</b>：未检测到 LLM API Key，正在使用内置脚本化数据演示完整流程（推荐先试"奇富科技"）。
          样例数据固定到 2025Q1 / 2025-05-22，仅用于展示产品流程，不代表当前最新披露。
          点击右上 <b>数据源 / Key</b> 填入 Key，或在 <code>.env</code> 配置后重启后端，即可做真实联网分析。
        </div>
      )}
      {!demoMode && !searchOk && dataSources.filter((d) => d.available).length === 0 && (
        <div className="warn-bar">
          未检测到任何搜索/金融数据源（Exa/Tavily/Serper 均不可用），agent 将无法联网采集。
          请安装 <b>mcporter</b>（免费 Exa 语义搜索）或在 <b>.env</b> 配置 Tavily/Serper Key。详见 README。
        </div>
      )}
      {!demoMode && searchDegraded && (
        <div className="warn-bar">
          ⚠ <b>搜索补充源降级</b>：Exa 主搜索正常，但 Tavily 额度可能已耗尽，已回退到 Serper。
          证伪反证检索将优先用免费源以省额度。建议在 <b>.env</b> 配置/续费 Tavily 或 Serper 以获得最佳补充搜索效果。
        </div>
      )}

      <div className="grid">
        {/* 左：画像 + 执行轨迹 */}
        <div className="panel">
          {profile && (
            <div className="profile-card">
              <div className="name">{profile.name}</div>
              <div className="tags">
                {profile.kind !== "industry" && (
                  <span className={`tag ${profile.is_public ? "pub" : "priv"}`}>
                    {profile.is_public ? "上市" : "非上市"}
                  </span>
                )}
                {profile.ticker && <span className="tag">{profile.ticker}</span>}
                {profile.industry && <span className="tag">{profile.industry}</span>}
              </div>
              {profile.business_model && <div className="bm">{profile.business_model}</div>}
              {profile.sections?.length > 0 && (
                <div className="kq">
                  <b>分析维度（量身定制）：</b>
                  <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 4 }}>
                    {profile.sections.map((s, i) => (
                      <span key={i} className="tag" style={{ background: "#e6f1fb", color: "#185fa5" }}>
                        {s}
                      </span>
                    ))}
                  </div>
                </div>
              )}
              {profile.key_questions?.length > 0 && (
                <div className="kq">
                  <b>关键问题：</b>
                  <ul>
                    {profile.key_questions.map((q, i) => (
                      <li key={i}>{q}</li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          )}
          <h2>
            执行轨迹 <span className="count">{trace.length} 步</span>
          </h2>
          {trace.length === 0 ? (
            <div className="empty">输入对象并开始，这里实时显示 agent 的工作过程</div>
          ) : (
            <Timeline events={trace} />
          )}
        </div>

        {/* 右：洞察 / 洞察报告 / 完整文档 */}
        <div className="panel">
          <div className="tabs">
            <div className={`tab ${tab === "insights" ? "active" : ""}`} onClick={() => setTab("insights")}>
              洞察 {insights.length > 0 && `(${insights.length})`}
            </div>
            <div className={`tab ${tab === "document" ? "active" : ""}`} onClick={() => setTab("document")}>
              完整分析文档
            </div>
          </div>

          {tab === "insights" ? (
            insights.length === 0 ? (
              <div className="empty">洞察将在分析阶段逐条出现，并随证伪过程更新置信度</div>
            ) : (
              insights.map((ins) => <InsightCard key={ins.id} insight={ins} />)
            )
          ) : narrativeMd ? (
            <ReportView md={narrativeMd} query={query} runId={runId} asOf={dataAsOf} docMode insights={insights} financials={financials} evidencePool={evidencePool} qualityEval={qualityEval} industryMetrics={industryMetrics} revenueSegments={revenueSegments} peerFinancials={peerFinancials} />
          ) : (
            <div className="empty">完整分析文档将在报告阶段生成（含执行摘要、核心发现、风险展望）</div>
          )}
        </div>
      </div>
    </div>
  );
}
