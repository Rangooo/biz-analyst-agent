export interface ProviderInfo {
  key: string;
  label: string;
  model: string;
  is_primary: boolean;
  available: boolean;
  family?: string;
  tier?: number;
  reasoning?: boolean;
}

export interface TraceEvent {
  id: string;
  run_id: string;
  stage: string;
  type: string;
  title: string;
  detail: string;
  payload: Record<string, any>;
  ts: string;
}

export interface Evidence {
  id: string;
  content: string;
  source_url: string;
  source_title: string;
  source_type?: string;
  tier: number;
  fetched_at: string;
  published_at?: string;
  as_of?: string;
  supports: boolean;
}

export interface ChallengeItem {
  dimension: string;
  severity: string;  // none/low/medium/high
  challenge: string;
  search_query: string;
}

export interface FalsificationRecord {
  round: number;
  counter_hypothesis: string;
  red_team_challenge: string;
  challenges: ChallengeItem[];
  overall_assessment: string;  // solid/incomplete/refuted
  counter_evidence: Evidence[];
  support_evidence: Evidence[];
  confidence_before: number;
  confidence_after: number;
  note: string;
  refinement_note: string;
}

export interface Insight {
  id: string;
  section: string;
  claim: string;
  reasoning: string;
  falsifiable_condition: string;
  evidence: Evidence[];
  confidence: number;
  verdict: string;
  falsifications: FalsificationRecord[];
  is_falsifiable: boolean;
  stale_count: number;
  needs_human: boolean;
}

export interface ObjectProfile {
  name: string;
  kind: string;
  is_public: boolean;
  ticker: string;
  industry: string;
  business_model: string;
  template_key: string;
  peers: string[];
  leaders: string[];
  key_questions: string[];
  sections: string[];
}

export interface QualityEval {
  scores: Record<string, number>;
  total: number;
  issues: string[];
  min_score?: number;
  round?: number;
  passed?: boolean;
}

export interface TokenBucket {
  calls: number;
  input: number;
  output: number;
  cost_usd: number;
}

export interface TokenSummary {
  total_calls: number;
  total_input: number;
  total_output: number;
  total_cost_usd: number;
  by_stage?: Record<string, TokenBucket>;
  by_role?: Record<string, TokenBucket>;
  by_provider?: Record<string, TokenBucket>;
}

export interface IndustryMetricRow {
  metric: string;      // 指标名称（如"稀土开采总量指标"）
  unit: string;        // 单位（如"吨"）
  values: { period: string; value: number; growth?: number | null }[];  // 多年时序
}

export interface PriceTrendRow {
  product: string;     // 产品名（如"氧化镨钕"）
  unit: string;        // 单位（如"万元/吨"）
  points: { period: string; price: number }[];  // 价格时序点
}

export interface IndustryMetrics {
  totals: IndustryMetricRow[];   // 行业总量指标（产量/配额等）
  prices: PriceTrendRow[];       // 主要产品价格趋势
}

export interface RevenueSegment {
  name: string;
  revenue: number;
  pct: number;
}

export interface RevenueSegments {
  fiscal_year: string;
  currency: string;
  segments: RevenueSegment[];
}

export interface PeerFinancialRow {
  name: string;
  ticker: string;
  revenue?: number | null;
  net_income?: number | null;
  period: string;
  is_subject?: boolean;
}

export interface DataGap {
  topic: string;
  gap_type: string;
  detail: string;
  impact: string;
  suggested_source: string;
  priority: string;
}

export interface AnalysisRun {
  id: string;
  query: string;
  provider: string;
  red_team_provider: string;
  reviewer_provider: string;
  same_source_review: boolean;
  status: string;
  profile: ObjectProfile | null;
  insights: Insight[];
  trace: TraceEvent[];
  evidence_pool: Evidence[];
  report_md: string;
  narrative_md: string;
  financials: { period: string; revenue?: number | null; net_income?: number | null; operating_income?: number | null; rev_growth?: number | null; ni_growth?: number | null }[];
  industry_metrics?: IndustryMetrics | null;
  revenue_segments?: RevenueSegments | null;
  peer_financials?: PeerFinancialRow[];
  data_gaps?: DataGap[];
  quality_eval?: QualityEval;
  token_summary?: TokenSummary;
  data_sources_used: string[];
  data_as_of: string;
  error: string;
}

export interface RunSummary {
  id: string;
  query: string;
  status: string;
  created_at: string;
}

export interface DataSourceInfo {
  name: string;
  available: boolean;
  label?: string;
  purpose?: string;
  priority?: "required" | "recommended" | "optional";
  category?: string;
  input_type?: "api_key" | "script_path" | "user_agent" | "mcp_config" | "none";
  env_vars?: string[];
  primary_env?: string;
  input_hint?: string;
  cost?: string;
  url?: string;
  note?: string;
  configured_envs?: string[];
  missing_envs?: string[];
  status_note?: string;
  detected_path?: string;  // 后端检测到的本地脚本路径（已就绪时返回）
}

export interface SearchStatus {
  status: string; // ok | degraded | unavailable
  tavily: boolean;
  serper: boolean;
  exhausted: boolean;
  cache_size: number;
}

export interface RolePlan {
  analyst: string | null;
  red_team: string | null;
  reviewer: string | null;
  same_source: boolean;
  reason: string;
}

export interface HealthInfo {
  status: string;
  search_backend: boolean;
  search_status: SearchStatus;
  providers: ProviderInfo[];
  demo_mode: boolean;
  same_source_review: boolean;
  analyst_provider: string;
  red_team_provider: string;
  reviewer_provider: string;
  role_plan: RolePlan | Record<string, never>;
  data_sources: DataSourceInfo[];
}

export const TIER_LABEL: Record<number, string> = {
  1: "财报/Filing",
  2: "电话会原文",
  3: "公司公告",
  4: "投资者日",
  5: "可信第三方",
  6: "媒体报道",
  7: "估算",
};

export const VERDICT_META: Record<string, { label: string; color: string; bg: string }> = {
  supported: { label: "成立", color: "#0f6e56", bg: "#e1f5ee" },
  questionable: { label: "存疑", color: "#854f0b", bg: "#faeeda" },
  refuted: { label: "已推翻", color: "#a32d2d", bg: "#fcebeb" },
  unverifiable: { label: "不可检验", color: "#5f5e5a", bg: "#f1efe8" },
};
