import { useState, useMemo } from "react";
import { DataSourceInfo, ProviderInfo } from "../types";

/* ── 模型家族 → API Key 映射 ── */
const FAMILY_KEY: Record<string, { env: string; label: string; url: string; note?: string }> = {
  deepseek: { env: "DEEPSEEK_API_KEY", label: "DeepSeek API Key", url: "https://platform.deepseek.com" },
  openai: { env: "OPENAI_API_KEY", label: "OpenAI API Key", url: "https://platform.openai.com/api-keys" },
  anthropic: { env: "ANTHROPIC_API_KEY", label: "Anthropic API Key", url: "https://console.anthropic.com" },
  google: { env: "GEMINI_API_KEY", label: "Gemini API Key", url: "https://aistudio.google.com/apikey" },
  qwen: { env: "DASHSCOPE_API_KEY", label: "DashScope API Key", url: "https://dashscope.console.aliyun.com" },
  welm: { env: "WELM_API_KEY", label: "WeLM API Key", url: "https://welm.weixin.qq.com" },
  ollama: { env: "OLLAMA_API_KEY", label: "Ollama 无需 Key", url: "", note: "本地运行，占位填 test 即可" },
};

/* ─── 数据源分层定义 ─── */
interface SourceDef {
  env: string;
  label: string;
  purpose: string;        // 一句话说明这个数据源做什么
  inputType: "api_key" | "script_path" | "user_agent" | "mcp_config" | "none";
  inputHint: string;      // 输入框里的 placeholder
  cost: string;           // 配置成本描述
  url?: string;           // 获取链接
  priority: "required" | "recommended" | "optional";
  adapterName: string;    // 对应后端 adapter name
  note?: string;          // 补充说明
  available?: boolean;
  envVars?: string[];
  configuredEnvs?: string[];
  statusNote?: string;
}

const FALLBACK_DATA_SOURCES: SourceDef[] = [
  // ─── 必须配置 ───
  {
    env: "EXA_API_KEY", label: "Exa 语义搜索", adapterName: "exa_search",
    purpose: "核心搜索引擎 — Agent 的「眼睛」，用于采集所有外部证据",
    inputType: "api_key", inputHint: "Bearer token（UUID 格式）",
    cost: "免费 1000 次/月", url: "https://dashboard.exa.ai/api-keys",
    priority: "required",
    note: "无此配置 Agent 将完全无法搜索证据。已配 mcporter 时可留空。",
  },
  // ─── 建议配置 ───
  {
    env: "SEC_USER_AGENT", label: "SEC EDGAR 美股财报", adapterName: "sec_edgar",
    purpose: "免费拉取美股上市公司 SEC 财务数据（营收/净利/资产负债）",
    inputType: "user_agent", inputHint: "格式：公司名 邮箱，如 MyApp admin@example.com",
    cost: "完全免费，无额度限制", url: "https://www.sec.gov/os/accessing-edgar-data",
    priority: "recommended",
    note: "分析美股公司时自动调用。不配则只靠搜索证据里的财务数字。",
  },
  {
    env: "TAVILY_API_KEY", label: "Tavily 通用搜索", adapterName: "general_search",
    purpose: "备用搜索引擎 — Exa 限流或失败时自动降级使用",
    inputType: "api_key", inputHint: "tvly-xxxxxxxx",
    cost: "免费 1000 次/月", url: "https://tavily.com",
    priority: "recommended",
    note: "与 Exa 互为冗余。如果 Exa 已配好且额度充足可不配。",
  },
  // ─── 可选/高级 ───
  {
    env: "SERPER_API_KEY", label: "Serper Google 搜索", adapterName: "general_search",
    purpose: "第三搜索源 — Google 结果，适合英文查询",
    inputType: "api_key", inputHint: "xxxxxxxxxxxxxxxxxxxxxxxx",
    cost: "免费 2500 次", url: "https://serper.dev",
    priority: "optional",
    note: "已有 Exa + Tavily 通常足够。",
  },
  {
    env: "WIND_MCP_TOOL", label: "Wind / AIFin Market", adapterName: "wind",
    purpose: "A股专业金融数据 — 研报、财务、公告（通过 MCP 协议）",
    inputType: "mcp_config", inputHint: "Tool 名，如 financial_docs.get_financial_news",
    cost: "约 1000 次/日", url: "",
    priority: "optional",
    note: "需要配合 mcporter 安装 Wind MCP server。适合 A 股深度分析。",
  },
  {
    env: "NEODATA_SCRIPT", label: "NeoData 金融数据", adapterName: "neodata",
    purpose: "A股/港美股结构化数据（行情、财务、估值）",
    inputType: "script_path", inputHint: "",
    cost: "服务器扩展", url: "",
    priority: "optional",
    note: "已安装，自动启用。",
  },
  {
    env: "IFIND_SCRIPT", label: "同花顺 iFinD", adapterName: "ifind",
    purpose: "A股专业终端数据（基本面、技术面、资金流）",
    inputType: "script_path", inputHint: "",
    cost: "服务器扩展", url: "",
    priority: "optional",
    note: "已安装，自动启用。",
  },
  {
    env: "", label: "东方财富资讯", adapterName: "em_news",
    purpose: "A股新闻/公告免费检索 — 内置数据源，开箱即用无需配置",
    inputType: "none", inputHint: "",
    cost: "内置 · 完全免费", url: "",
    priority: "recommended",
    note: "内置 HTTP 直连东方财富公开接口，无需任何配置。",
  },
];

const WIND_EXTRA_FIELDS = [
  { env: "WIND_MCP_SERVER", label: "Server 名", placeholder: "wind" },
  { env: "WIND_MCP_QUERY_ARG", label: "Query 参数名", placeholder: "query" },
  { env: "WIND_MCP_LIMIT_ARG", label: "Limit 参数名", placeholder: "maxResults" },
  { env: "WIND_MCP_KIND_ARG", label: "Kind 参数名", placeholder: "kind" },
  { env: "WIND_MCP_COMMAND", label: "命令模板", placeholder: "wind-mcp search --query \"{query}\" --limit {max_results}" },
];

const inp: React.CSSProperties = {
  width: "100%", padding: "7px 10px", border: "1px solid #e6e4dd", borderRadius: 8,
  fontSize: 13, background: "#fff", color: "#2c2c2a",
};

const ROLE_META: { key: "analyst" | "red_team" | "reviewer"; label: string; desc: string; color: string; bg: string }[] = [
  { key: "analyst", label: "主分析", desc: "核心分析与洞察生成", color: "#0f6e56", bg: "#e1f5ee" },
  { key: "red_team", label: "红队", desc: "异源对抗证伪（建议不同家族）", color: "#a32d2d", bg: "#fcebeb" },
  { key: "reviewer", label: "终审", desc: "撰写完整报告（推理模型更佳）", color: "#7a5cb8", bg: "#f0edfa" },
];

const PRIORITY_STYLE: Record<string, { label: string; color: string; bg: string; border: string }> = {
  required: { label: "必须", color: "#a32d2d", bg: "#fef0f0", border: "#f5c6c6" },
  recommended: { label: "建议", color: "#854f0b", bg: "#fef9ee", border: "#f5e3b5" },
  optional: { label: "可选", color: "#5f5e5a", bg: "#f9f9f7", border: "#e6e4dd" },
};

function normalizeSource(ds: DataSourceInfo): SourceDef {
  const fallback = FALLBACK_DATA_SOURCES.find((s) => s.adapterName === ds.name);
  const envVars = ds.env_vars || fallback?.envVars || (fallback?.env ? [fallback.env] : []);
  return {
    env: ds.primary_env || fallback?.env || envVars[0] || "",
    label: ds.label || fallback?.label || ds.name,
    purpose: ds.purpose || fallback?.purpose || "",
    inputType: ds.input_type || fallback?.inputType || "api_key",
    inputHint: ds.input_hint || fallback?.inputHint || "",
    cost: ds.cost || fallback?.cost || "",
    url: ds.url || fallback?.url || "",
    priority: ds.priority || fallback?.priority || "optional",
    adapterName: ds.name,
    note: ds.note || fallback?.note || "",
    available: ds.available,
    envVars,
    configuredEnvs: ds.configured_envs || [],
    statusNote: ds.status_note,
  };
}

function buildSourceDefs(dataSources: DataSourceInfo[]): SourceDef[] {
  const fromBackend = dataSources.map(normalizeSource);
  const seen = new Set(fromBackend.map((s) => s.adapterName));
  const fallbackOnly = FALLBACK_DATA_SOURCES.filter((s) => !seen.has(s.adapterName));
  return [...fromBackend, ...fallbackOnly];
}

export function SettingsPanel({
  providers,
  dataSources,
  rolePlan,
  provider,
  redTeamSel,
  reviewerSel,
  onPickProvider,
  onPickRedTeam,
  onPickReviewer,
  onClose,
  onSaved,
}: {
  providers: ProviderInfo[];
  dataSources: DataSourceInfo[];
  rolePlan?: { analyst: string | null; red_team: string | null; reviewer: string | null };
  provider: string;
  redTeamSel: string;
  reviewerSel: string;
  onPickProvider: (k: string) => void;
  onPickRedTeam: (k: string) => void;
  onPickReviewer: (k: string) => void;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [keys, setKeys] = useState<Record<string, string>>({});
  const [saving, setSaving] = useState(false);
  const [msg, setMsg] = useState("");
  const [showCustom, setShowCustom] = useState(false);
  const [showOptional, setShowOptional] = useState(false);
  const [showWindExtra, setShowWindExtra] = useState(false);
  const [cp, setCp] = useState({ label: "", base_url: "", model: "", api_key: "", extra_headers: "", family: "custom", tier: "3", reasoning: false });
  const [cpMsg, setCpMsg] = useState("");

  /* 按家族分组模型，方便下拉框 optgroup */
  const grouped = useMemo(() => {
    const m: Record<string, ProviderInfo[]> = {};
    for (const p of providers) {
      const fam = p.family || "custom";
      (m[fam] ||= []).push(p);
    }
    return m;
  }, [providers]);

  /* 判断某个模型是否需要 Key 输入 */
  function needsKey(p: ProviderInfo): string | null {
    if (p.available) return null;
    const fam = p.family || "";
    const info = FAMILY_KEY[fam];
    return info ? info.env : null;
  }

  /* 当前各角色选中的模型 */
  const roleSel: Record<string, string> = {
    analyst: provider,
    red_team: redTeamSel,
    reviewer: reviewerSel,
  };
  const rolePick: Record<string, (k: string) => void> = {
    analyst: onPickProvider,
    red_team: onPickRedTeam,
    reviewer: onPickReviewer,
  };

  async function save() {
    setSaving(true);
    setMsg("");
    try {
      const res = await fetch("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ keys }),
      });
      const d = await res.json();
      const applied = d.applied || [];
      const ignored = d.ignored || [];
      setMsg(`已生效：${applied.join(", ") || "无变更"}。` +
        (ignored.length ? ` 已忽略无效配置：${ignored.join(", ")}。` : "") +
        (d.same_source_review ? " 仍为同源降级（建议再加一个异源模型）。" : " 异源对抗已就绪。"));
      setKeys({});
      onSaved();
    } catch {
      setMsg("保存失败 — 请确认后端已启动");
    } finally {
      setSaving(false);
    }
  }

  /* 按后端目录分组数据源 */
  const sourceDefs = useMemo(() => buildSourceDefs(dataSources), [dataSources]);
  const requiredSources = sourceDefs.filter(s => s.priority === "required");
  const recommendedSources = sourceDefs.filter(s => s.priority === "recommended");
  // 可选数据源默认折叠，但不隐藏未就绪项，避免用户误以为系统没有这些能力。
  const optionalSources = sourceDefs.filter(s => {
    if (s.priority !== "optional") return false;
    return true;
  });

  function renderSourceCard(s: SourceDef) {
    const pStyle = PRIORITY_STYLE[s.priority];
    const ds = dataSources.find(d => d.name === s.adapterName);
    const isAvailable = s.available ?? ds?.available ?? false;
    const isScriptType = s.inputType === "script_path";
    const isNoConfig = s.inputType === "none";
    const simpleEnvList = (s.envVars && s.envVars.length > 0 ? s.envVars : (s.env ? [s.env] : []))
      .filter((env) => env && !["PYBIN", "NODEBIN", "IFIND_WRAPPER"].includes(env));
    const inputEnvs = s.inputType === "mcp_config" ? (s.env ? [s.env] : []) : simpleEnvList;
    const hasInputs = inputEnvs.length > 0;

    return (
      <div key={`${s.adapterName}:${s.env || s.label}`} style={{
        border: `1px solid ${isAvailable ? "#b2dfcc" : pStyle.border}`,
        borderRadius: 10, padding: "12px 14px", marginBottom: 10,
        background: isAvailable ? "#f4fdf8" : pStyle.bg,
      }}>
        {/* 顶部：名称 + 状态标签 */}
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
          <span style={{ fontSize: 13, fontWeight: 600, color: "#2c2c2a" }}>{s.label}</span>
          {isAvailable ? (
            <span style={{ fontSize: 10, fontWeight: 500, color: "#0f6e56", background: "#d4f5e5", padding: "2px 7px", borderRadius: 8 }}>
              ✓ 已就绪
            </span>
          ) : (
            <span style={{ fontSize: 10, fontWeight: 500, color: pStyle.color, background: pStyle.bg, padding: "2px 7px", borderRadius: 8, border: `1px solid ${pStyle.border}` }}>
              {pStyle.label}
            </span>
          )}
          <span style={{ fontSize: 11, color: "#888780", marginLeft: "auto" }}>{s.cost}</span>
        </div>

        {/* 用途说明 */}
        <div style={{ fontSize: 12, color: "#5f5e5a", marginBottom: 8, lineHeight: 1.5 }}>
          {s.purpose}
        </div>

        {/* 输入区 / 状态说明 */}
        {isNoConfig ? (
          <div style={{ fontSize: 11, color: isAvailable ? "#0f6e56" : "#854f0b", padding: "6px 10px", background: isAvailable ? "#e8f8f0" : "#fef9ee", borderRadius: 6 }}>
            {s.statusNote || "无需配置，后端内置可用。"}
          </div>
        ) : isAvailable && s.inputType !== "mcp_config" ? (
          // 已配置好的 API key / 脚本类
          <div>
            <div style={{ fontSize: 11, color: "#0f6e56", padding: "6px 10px", background: "#e8f8f0", borderRadius: 6 }}>
              {s.statusNote || (isScriptType ? "✓ 已检测到本地脚本" : "✓ 已配置，正常工作中")}
              {s.configuredEnvs && s.configuredEnvs.length > 0 ? ` · ${s.configuredEnvs.join(", ")}` : ""}
            </div>
            {ds?.detected_path && (
              <div style={{ fontSize: 10, color: "#5f6e5a", marginTop: 4, fontFamily: "monospace", wordBreak: "break-all" }}>
                📄 {ds.detected_path}
              </div>
            )}
          </div>
        ) : hasInputs ? (
          // 需要输入的
          <div>
            {/* script_path 类型：未检测到脚本，明确指引该填什么 */}
            {isScriptType && !isAvailable && (
              <div style={{ fontSize: 10, color: "#854f0b", padding: "5px 9px", background: "#fef9ee", borderRadius: 6, marginBottom: 6, lineHeight: 1.5 }}>
                <b>填写提示：</b>这里是 <b>本地脚本路径</b>（如 <code style={{ background: "#fcf3df", padding: "0 4px" }}>/path/to/neodata/scripts/query.py</code>），不是 token。<br />
                如果已安装扩展脚本留空即可——后端会自动探测标准路径。手动填绝对路径可覆盖自动探测。
              </div>
            )}
            <div style={{ display: "grid", gridTemplateColumns: inputEnvs.length > 1 ? "1fr 1fr" : "1fr", gap: 6 }}>
              {inputEnvs.map((env) => (
                <input
                  key={env}
                  type={s.inputType === "api_key" ? "password" : "text"}
                  placeholder={inputEnvs.length > 1 ? `${env}：${s.inputHint}` : s.inputHint || env}
                  value={keys[env] || ""}
                  onChange={(e) => setKeys({ ...keys, [env]: e.target.value })}
                  style={{ ...inp, fontSize: 12 }}
                />
              ))}
            </div>
            {/* Wind 额外字段 */}
            {s.env === "WIND_MCP_TOOL" && (
              <div style={{ marginTop: 6 }}>
                <button className="btn ghost" style={{ fontSize: 11, padding: "2px 8px" }} onClick={() => setShowWindExtra(!showWindExtra)}>
                  {showWindExtra ? "收起高级配置" : "展开 MCP 高级配置"}
                </button>
                {showWindExtra && (
                  <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 6, marginTop: 6 }}>
                    {WIND_EXTRA_FIELDS.map(f => (
                      <input key={f.env} type="text" placeholder={`${f.label}: ${f.placeholder}`}
                        value={keys[f.env] || ""}
                        onChange={(e) => setKeys({ ...keys, [f.env]: e.target.value })}
                        style={{ ...inp, fontSize: 11 }} />
                    ))}
                  </div>
                )}
              </div>
            )}
            <div style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 4 }}>
              {s.url && (
                <a href={s.url} target="_blank" rel="noreferrer" style={{ fontSize: 11, color: "#185fa5", textDecoration: "none" }}>
                  → 获取
                </a>
              )}
            </div>
          </div>
        ) : (
          <div style={{ fontSize: 11, color: "#854f0b", padding: "6px 10px", background: "#fef9ee", borderRadius: 6 }}>
            {s.statusNote || "当前数据源需要在后端或 .env 中配置。"}
          </div>
        )}

        {/* 补充说明 */}
        {s.note && (
          <div style={{ fontSize: 11, color: "#888780", marginTop: 6, fontStyle: "italic" }}>
            {s.note}
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="panel" style={{ marginBottom: 16, border: "1px solid #b5d4f4" }}>
      {/* 标题栏 */}
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 14 }}>
        <h2 style={{ margin: 0 }}>模型与数据源配置</h2>
        <button className="btn ghost" onClick={onClose}>收起</button>
      </div>

      {/* ── Section 1: 三个角色卡片 ── */}
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr 1fr", gap: 12, marginBottom: 14 }}>
        {ROLE_META.map((role) => {
          const sel = roleSel[role.key];
          const selP = providers.find((p) => p.key === sel);
          const needEnv = selP ? needsKey(selP) : null;
          const autoKey = rolePlan?.[role.key as keyof typeof rolePlan];
          const autoP = providers.find((p) => p.key === autoKey);
          return (
            <div key={role.key} style={{
              border: `1px solid ${role.color}33`, borderRadius: 10, padding: 12,
              background: role.bg + "55",
            }}>
              {/* 角色标题 */}
              <div style={{ display: "flex", alignItems: "center", gap: 6, marginBottom: 8 }}>
                <span style={{
                  fontSize: 11, fontWeight: 600, color: "#fff", padding: "2px 8px",
                  borderRadius: 8, background: role.color,
                }}>{role.label}</span>
                <span style={{ fontSize: 11, color: "#888780" }}>{role.desc}</span>
              </div>

              {/* 模型下拉框 */}
              <select
                value={sel}
                onChange={(e) => rolePick[role.key](e.target.value)}
                style={{ ...inp, marginBottom: 6, fontWeight: 500 }}
              >
                <option value="">自动指派{autoP ? `（当前：${autoP.label}）` : ""}</option>
                {Object.entries(grouped).map(([fam, list]) => (
                  <optgroup key={fam} label={fam.toUpperCase()}>
                    {list.map((p) => (
                      <option key={p.key} value={p.key} disabled={!p.available && !FAMILY_KEY[p.family || ""]}>
                        {p.label}{p.available ? " ✅" : " · 需Key"}{p.reasoning ? " · 推理" : ""}
                      </option>
                    ))}
                  </optgroup>
                ))}
              </select>

              {/* 选中模型状态 */}
              {selP ? (
                <div style={{ fontSize: 11, color: "#5f5e5a" }}>
                  {selP.available ? (
                    <span style={{ color: role.color }}>✅ {selP.model} · T{selP.tier} 已就绪</span>
                  ) : needEnv ? (
                    <div>
                      <div style={{ color: "#854f0b", marginBottom: 4 }}>⚠️ 需要 {FAMILY_KEY[selP.family || ""]?.label || needEnv}</div>
                      <input
                        type="password"
                        placeholder={needEnv}
                        value={keys[needEnv] || ""}
                        onChange={(e) => setKeys({ ...keys, [needEnv]: e.target.value })}
                        style={inp}
                      />
                      {FAMILY_KEY[selP.family || ""]?.url && (
                        <a href={FAMILY_KEY[selP.family || ""].url} target="_blank" rel="noreferrer"
                          style={{ fontSize: 11, color: "#185fa5", textDecoration: "none" }}>
                          → 获取 Key
                        </a>
                      )}
                    </div>
                  ) : (
                    <span style={{ color: "#888780" }}>○ {selP.model}（自定义模型，无需 Key）</span>
                  )}
                </div>
              ) : (
                <div style={{ fontSize: 11, color: "#888780" }}>
                  由系统按 tier + 家族自动分配最优模型
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* 同源警告 */}
      {(() => {
        const aFam = providers.find((p) => p.key === (provider || rolePlan?.analyst))?.family;
        const rFam = providers.find((p) => p.key === (redTeamSel || rolePlan?.red_team))?.family;
        if (aFam && rFam && aFam === rFam) {
          return (
            <div style={{ fontSize: 12, color: "#854f0b", background: "#faeeda", padding: "6px 10px", borderRadius: 8, marginBottom: 14 }}>
              ⚠ 主分析与红队同源（{aFam}），证伪独立性受限——建议为红队选一个不同家族的模型
            </div>
          );
        }
        return null;
      })()}

      {/* ── Section 2: 数据源（分层） ── */}
      <div style={{ borderTop: "1px dashed #e6e4dd", paddingTop: 14, marginBottom: 14 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 12 }}>
          <span style={{ fontSize: 14, fontWeight: 600, color: "#2c2c2a" }}>数据源配置</span>
          <span style={{ fontSize: 11, color: "#888780" }}>Agent 搜索证据和拉取财务数据的来源</span>
        </div>

        {/* 必须配置 */}
        <div style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: "#a32d2d", marginBottom: 8, display: "flex", alignItems: "center", gap: 6 }}>
            <span style={{ width: 6, height: 6, borderRadius: "50%", background: "#a32d2d", display: "inline-block" }} />
            必须配置 — 缺少则 Agent 无法正常工作
          </div>
          {requiredSources.map(renderSourceCard)}
        </div>

        {/* 建议配置 */}
        <div style={{ marginBottom: 14 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: "#854f0b", marginBottom: 8, display: "flex", alignItems: "center", gap: 6 }}>
            <span style={{ width: 6, height: 6, borderRadius: "50%", background: "#854f0b", display: "inline-block" }} />
            建议配置 — 显著提升分析质量
          </div>
          {recommendedSources.map(renderSourceCard)}
        </div>

        {/* 可选/高级 — 默认折叠 */}
        <div>
          <button className="btn ghost" style={{ fontSize: 12, color: "#5f5e5a" }} onClick={() => setShowOptional(!showOptional)}>
            {showOptional ? "收起" : "展开"} 高级/可选数据源（{optionalSources.length} 项 · 大多需安装扩展脚本）
          </button>
          {showOptional && (
            <div style={{ marginTop: 10 }}>
              <div style={{ fontSize: 11, color: "#5f5e5a", marginBottom: 10, padding: "8px 12px", background: "#f4f6fa", borderRadius: 6, border: "1px solid #d8e1ee" }}>
                <b>这些数据源不是 HTTP API</b>——填 token 不会生效。<br />
                <b>两种配置方式（选一即可）：</b><br />
                · <b>推荐</b>：安装对应扩展脚本（如 neodata-financial-data / ifind-finance-data / wind-mcp），后端自动识别路径<br />
                · <b>手动</b>：填入本地脚本路径或 MCP 命令（下面的输入框）。如果你只分析 A 股公开数据，不装也没影响——东方财富资讯是开箱即用的
              </div>
              {optionalSources.map(renderSourceCard)}
            </div>
          )}
        </div>
      </div>

      {/* ── 保存按钮 ── */}
      <div style={{ display: "flex", gap: 10, alignItems: "center", marginBottom: 12 }}>
        <button className="btn" onClick={save} disabled={saving}>
          {saving ? "保存中…" : "保存并生效"}
        </button>
        <span style={{ fontSize: 12, color: "#5f5e5a" }}>
          Key 仅存于后端进程内存，重启后端需重新填或写入 .env 文件
        </span>
        {msg && <span style={{ fontSize: 12, color: msg.includes("失败") ? "#a32d2d" : "#0f6e56" }}>{msg}</span>}
      </div>

      {/* ── Section 3: 自定义模型（折叠） ── */}
      <div style={{ borderTop: "1px dashed #e6e4dd", paddingTop: 12 }}>
        <button className="btn ghost" onClick={() => setShowCustom((s) => !s)}>
          {showCustom ? "收起" : "＋ 添加自定义模型（内网代理/自部署）"}
        </button>
        {showCustom && (
          <div style={{ marginTop: 10, fontSize: 12 }}>
            <div style={{ color: "#888780", marginBottom: 6 }}>
              适合自部署 vLLM/Ollama 或内网 OpenAI 兼容端点。配置写入 providers.local.yaml（不随仓库共享），api_key 写入 .env。
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
              <input placeholder="显示名，如 My-Custom-LLM" value={cp.label} onChange={(e) => setCp({ ...cp, label: e.target.value })} style={inp} />
              <input placeholder="base_url，如 http://localhost:11434/v1" value={cp.base_url} onChange={(e) => setCp({ ...cp, base_url: e.target.value })} style={inp} />
              <input placeholder="model 名，如 WeLM-v4-80B-A3B-Instruct-Preview" value={cp.model} onChange={(e) => setCp({ ...cp, model: e.target.value })} style={inp} />
              <input placeholder="api_key（占位填 test 也行）" value={cp.api_key} onChange={(e) => setCp({ ...cp, api_key: e.target.value })} style={inp} />
              <input placeholder="家族 family：deepseek/openai/welm/glm/custom" value={cp.family} onChange={(e) => setCp({ ...cp, family: e.target.value })} style={inp} />
              <input placeholder="智能程度 tier 1-5" value={cp.tier} onChange={(e) => setCp({ ...cp, tier: e.target.value })} style={inp} />
            </div>
            <input placeholder="extra_headers（JSON，如 {&quot;X-Custom-Header&quot;:&quot;ENV_VAR_NAME&quot;}），可选" value={cp.extra_headers} onChange={(e) => setCp({ ...cp, extra_headers: e.target.value })} style={{ ...inp, width: "100%", marginTop: 8 }} />
            <label style={{ fontSize: 12, marginLeft: 4 }}>
              <input type="checkbox" checked={cp.reasoning} onChange={(e) => setCp({ ...cp, reasoning: e.target.checked })} /> 推理模型（适合终审，慢）
            </label>
            <div style={{ marginTop: 8, display: "flex", gap: 10, alignItems: "center" }}>
              <button className="btn" onClick={async () => {
                let extra: Record<string, string> = {};
                if (cp.extra_headers.trim()) { try { extra = JSON.parse(cp.extra_headers); } catch { setCpMsg("extra_headers JSON 解析失败"); return; } }
                setCpMsg("添加中…");
                try {
                  const r = await fetch("/api/providers/custom", {
                    method: "POST", headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ label: cp.label, base_url: cp.base_url, model: cp.model, api_key: cp.api_key, extra_headers: extra, family: cp.family, tier: Number(cp.tier) || 3, reasoning: cp.reasoning }),
                  });
                  const d = await r.json();
                  setCpMsg(d.error || `已添加：${d.key}（已自动并入角色指派）`);
                  setCp({ label: "", base_url: "", model: "", api_key: "", extra_headers: "", family: "custom", tier: "3", reasoning: false });
                  onSaved();
                } catch { setCpMsg("添加失败"); }
              }}>添加并生效</button>
              {cpMsg && <span style={{ fontSize: 12, color: "#0f6e56" }}>{cpMsg}</span>}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
