import { TokenBucket, TokenSummary } from "../types";

const STAGE_LABEL: Record<string, string> = {
  scope: "界定",
  collect: "采集",
  analyze: "分析",
  falsify: "证伪",
  refine: "补证",
  report: "报告",
  unknown: "未知",
};

const ROLE_LABEL: Record<string, string> = {
  analyst: "主分析",
  red_team: "红队",
  reviewer: "终审",
};

function formatToken(n?: number): string {
  const value = Number(n || 0);
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 10_000) return `${Math.round(value / 1000)}k`;
  if (value >= 1000) return `${(value / 1000).toFixed(1)}k`;
  return String(value);
}

function formatCost(n?: number): string {
  const value = Number(n || 0);
  if (value <= 0) return "$0";
  if (value < 0.01) return "<$0.01";
  return `$${value.toFixed(2)}`;
}

function bucketRows(data?: Record<string, TokenBucket>, labels?: Record<string, string>) {
  return Object.entries(data || {})
    .map(([key, value]) => ({ key, label: labels?.[key] || key, ...value }))
    .sort((a, b) => (b.cost_usd || 0) - (a.cost_usd || 0));
}

export function TokenCostPanel({ summary }: { summary?: TokenSummary }) {
  const hasUsage = !!summary && Number(summary.total_calls || 0) > 0;
  const stageRows = bucketRows(summary?.by_stage, STAGE_LABEL);
  const roleRows = bucketRows(summary?.by_role, ROLE_LABEL);
  const providerRows = bucketRows(summary?.by_provider);
  const maxStageCost = Math.max(...stageRows.map((r) => r.cost_usd || 0), 0);
  const topStage = stageRows[0];
  const topRole = roleRows[0];

  if (!summary) return null;

  if (!hasUsage) {
    return (
      <div className="token-card token-card-empty">
        <div className="token-title">Token 成本</div>
        <div className="token-empty">
          本次运行尚未记录 token 用量。可能原因：旧版运行记录、任务异常中断，或模型代理未返回 usage。
        </div>
      </div>
    );
  }

  return (
    <div className="token-card">
      <div className="token-head">
        <div>
          <div className="token-title">Token 成本</div>
          <div className="token-sub">
            {topStage && `最耗费阶段：${topStage.label}`}
            {topStage && topRole && " · "}
            {topRole && `最耗费角色：${topRole.label}`}
          </div>
        </div>
        <div className="token-cost">{formatCost(summary.total_cost_usd)}</div>
      </div>

      <div className="token-kpis">
        <div><span>调用</span><b>{summary.total_calls}</b></div>
        <div><span>输入</span><b>{formatToken(summary.total_input)}</b></div>
        <div><span>输出</span><b>{formatToken(summary.total_output)}</b></div>
      </div>

      {stageRows.length > 0 && (
        <div className="token-section">
          <div className="token-section-title">按阶段</div>
          {stageRows.map((row) => {
            const pct = maxStageCost > 0 ? Math.max(4, Math.round((row.cost_usd / maxStageCost) * 100)) : 4;
            return (
              <div className="token-row" key={row.key}>
                <div className="token-row-label">{row.label}</div>
                <div className="token-row-track">
                  <div className="token-row-fill" style={{ width: `${pct}%` }} />
                  <span>{row.calls} 次 · {formatToken(row.input + row.output)} · {formatCost(row.cost_usd)}</span>
                </div>
              </div>
            );
          })}
        </div>
      )}

      {(roleRows.length > 0 || providerRows.length > 0) && (
        <div className="token-splits">
          {roleRows.length > 0 && (
            <div>
              <div className="token-section-title">按角色</div>
              {roleRows.map((row) => (
                <div className="token-chip" key={row.key}>
                  <span>{row.label}</span><b>{formatCost(row.cost_usd)}</b>
                </div>
              ))}
            </div>
          )}
          {providerRows.length > 0 && (
            <div>
              <div className="token-section-title">按模型</div>
              {providerRows.slice(0, 4).map((row) => (
                <div className="token-chip" key={row.key}>
                  <span>{row.label}</span><b>{formatCost(row.cost_usd)}</b>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
