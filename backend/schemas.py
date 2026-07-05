"""
核心数据模型 —— 这些 schema 是"非 chatbot"的工程保证。
每条洞察强制带证据链、可证伪命题、置信度；每次状态变化留痕、可审计。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Optional
from uuid import uuid4

from pydantic import BaseModel, Field


def _uid() -> str:
    return uuid4().hex[:12]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class SourceTier(int, Enum):
    """信息溯源等级（数字越小权威性越高）。
    权威排序：财报/Filing > 电话会 > 公司公告/新闻稿 > 投资者日 > 监管/权威媒体 > 一般媒体 > 估算。
    公司新闻稿虽优先于媒体，但需警惕口径偏差与夸大（分析时标注）。"""

    FILING = 1          # 公司财报 / SEC Filing
    TRANSCRIPT = 2      # 管理层电话会原文
    COMPANY_PR = 3      # 公司公告 / 新闻稿（警惕口径偏差）
    INVESTOR_DAY = 4    # 投资者日 / 战略发布会
    THIRD_PARTY = 5     # 监管披露 / 权威媒体（Reuters/Bloomberg/财新/第一财经/SEC/证监会）
    MEDIA = 6           # 一般媒体报道（需谨慎）
    ESTIMATE = 7        # 分析师 / 自行估算

    @property
    def label(self) -> str:
        return {
            1: "财报/Filing", 2: "电话会原文", 3: "公司公告",
            4: "投资者日", 5: "监管/权威媒体", 6: "一般媒体", 7: "估算",
        }[int(self)]


class Verdict(str, Enum):
    """证伪后的裁决。"""

    SUPPORTED = "supported"        # 成立：证据充分
    QUESTIONABLE = "questionable"  # 存疑：证据不足或有矛盾
    REFUTED = "refuted"            # 推翻：找到有力反证
    UNVERIFIABLE = "unverifiable"  # 不可检验：没有新证据可判定


class Evidence(BaseModel):
    """一条证据 —— 必须可溯源、可定位时效。"""

    id: str = Field(default_factory=_uid)
    content: str                       # 证据内容（文本摘要）
    source_url: str = ""               # 来源 URL
    source_title: str = ""             # 来源标题
    source_type: str = ""              # 数据源类型: filing/financial_api/news/notice/research/report/web
    tier: SourceTier = SourceTier.MEDIA
    relevance: float = 1.0             # 与论点相关度 0~1
    importance: float = 1.0            # 对结论的重要度 0~1
    fetched_at: str = Field(default_factory=_now)
    published_at: str = ""
    as_of: str = ""
    supports: bool = True
    # P2.8: 多模态证据支持
    evidence_type: str = "text"        # text/image/pdf/table（默认文本）
    image_url: str = ""                # 图片/PDF 的 URL（多模态证据）

    @property
    def tier_label(self) -> str:
        return self.tier.label

    def freshness_factor(self, now: Optional[datetime] = None) -> float:
        """时效因子：越新越接近 1.0，越旧衰减。
        - 无日期：0.6（中性偏低，鼓励补具体日期）
        - 30天内：1.0；90天内：0.9；180天内：0.75；365天内：0.6；更旧：0.4
        财报类(as_of)按报告期算，其它按 published_at 算。
        """
        ref = now or datetime.now()
        date_str = self.as_of or self.published_at
        if not date_str:
            return 0.6
        try:
            # 兼容 YYYY-Qn
            if "Q" in date_str and len(date_str) >= 6:
                y, q = date_str[:4], int(date_str[-1])
                month = q * 3
                dt = datetime(int(y), month, 28)
            else:
                dt = datetime.fromisoformat(date_str[:10])
        except Exception:  # noqa: BLE001
            return 0.6
        days = (ref - dt).days
        if days <= 30:
            return 1.0
        if days <= 90:
            return 0.9
        if days <= 180:
            return 0.75
        if days <= 365:
            return 0.6
        return 0.4


class FalsificationRecord(BaseModel):
    """证伪记录 —— 红队检验了什么、找到什么反证、结论如何变化。
    九维挑战框架：每条洞察经 9 个独立维度审查，按整体评估分类(solid/incomplete/refuted)。"""

    id: str = Field(default_factory=_uid)
    round: int = 1
    counter_hypothesis: str            # 反面假设："如果这条不成立会怎样"（兼容字段，取九维中最有力的替代理论）
    red_team_challenge: str = ""       # 红队质疑摘要（兼容字段，取九维中 severity 最高的挑战）
    challenges: list[dict] = Field(default_factory=list)  # 九维挑战结果 [{dimension, severity, challenge, search_query}]
    overall_assessment: str = ""       # solid/incomplete/refuted —— 九维综合判断
    counter_evidence: list[Evidence] = Field(default_factory=list)  # 找到的反证
    support_evidence: list[Evidence] = Field(default_factory=list)  # 自我迭代补强的支撑证据
    verdict_before: Optional[Verdict] = None
    verdict_after: Optional[Verdict] = None
    confidence_before: float = 0.0
    confidence_after: float = 0.0
    note: str = ""                     # 结论变化说明
    refinement_note: str = ""          # 自我迭代后的缺口说明（保留/降级/推翻的理由）
    created_at: str = Field(default_factory=_now)


class Insight(BaseModel):
    """一条洞察 —— 强制带证据链、可证伪命题、置信度。"""

    id: str = Field(default_factory=_uid)
    section: str                       # 所属板块（财务表现/经营指标/战略/产品...）
    claim: str                         # 论点
    reasoning: str = ""                # 推理过程（含"言外之意"）
    falsifiable_condition: str         # 红队基于九维挑战判定的推翻路径："什么现已公开可查的证据会推翻此结论"——由红队填充，非分析师自述
    evidence: list[Evidence] = Field(default_factory=list)
    confidence: float = 0.5            # 0~1
    verdict: Verdict = Verdict.QUESTIONABLE
    falsifications: list[FalsificationRecord] = Field(default_factory=list)
    is_falsifiable: bool = True        # 命题是否可被证伪
    # —— 借鉴 scholar-loop / ARIS：防认知打转 ——
    stale_count: int = 0               # 连续无新证据的轮数；≥2 强制转向，≥4 需人工
    needs_human: bool = False          # 反复无法验证，升级人工
    refinement_note: str = ""          # 自我迭代后的状态说明（solid保留/incomplete补强后保留或降级/refuted推翻的缺口说明）
    audit_passed: Optional[bool] = None  # 零上下文数字核对是否通过（Report 前）
    created_at: str = Field(default_factory=_now)

    def evidence_strength(self) -> float:
        """证据强度 = f(数量, 溯源等级, 支撑/反证一致性, 时效)。
        时效衰减确保"可信的最新数据"为主导判断依据。"""
        if not self.evidence:
            return 0.0
        supporting = [e for e in self.evidence if e.supports]
        opposing = [e for e in self.evidence if not e.supports]
        if not supporting:
            return 0.0
        # 权威性(溯源等级) × 时效 × 相关度 × 重要度
        def w(e: Evidence) -> float:
            base = max(0.2, 1.0 - (int(e.tier) - 1) * 0.13)
            return base * e.freshness_factor() * max(0.1, e.relevance) * max(0.1, e.importance)
        sup = sum(w(e) for e in supporting)
        opp = sum(w(e) for e in opposing)
        total = sup + opp
        if total == 0:
            return 0.0
        consistency = sup / total          # 支撑证据占比
        volume = min(1.0, sup / 2.0)       # 2 条高质量支撑即饱和
        return round(consistency * volume, 3)


class TraceEvent(BaseModel):
    """执行轨迹事件 —— 流式推送给前端，让用户看到"分析师在工作"。"""

    id: str = Field(default_factory=_uid)
    run_id: str = ""
    stage: str                         # scope/collect/analyze/falsify/refine/report
    type: str                          # 事件类型：thinking/search/evidence/hypothesis/red_team/score/done
    title: str
    detail: str = ""
    payload: dict = Field(default_factory=dict)
    ts: str = Field(default_factory=_now)


class DataGap(BaseModel):
    """报告级数据缺口/不确定性。

    用于把"无源数字、未披露、来源冲突、证据缺口"显式写入附录。
    这类信息不是失败状态，而是分析产物的一部分：告诉读者哪些判断
    已被证据支撑，哪些仍受公开数据限制。
    """

    topic: str
    gap_type: str = "missing_data"     # missing_data/unsourced/conflict/source_limit/sparse_disclosure
    detail: str = ""
    impact: str = ""                   # 对结论的影响
    suggested_source: str = ""         # 建议补查渠道/材料
    priority: str = "medium"           # high/medium/low


class ObjectProfile(BaseModel):
    """分析对象画像 —— Scope 阶段产出，决定加载哪套框架与分析维度。"""

    name: str
    kind: str = "company"              # company / industry
    is_public: bool = True             # 是否上市
    ticker: str = ""                   # 股票代码（如有）
    industry: str = ""                 # 所属行业
    business_model: str = ""           # 商业模式简述
    template_key: str = "generic"      # 命中的行业模板 key
    peers: list[str] = Field(default_factory=list)         # 对标公司
    leaders: list[str] = Field(default_factory=list)       # 行业龙头（按份额/规模最具代表性，非任意上市企业；行业研究交叉验证锚点）
    key_questions: list[str] = Field(default_factory=list)  # 本次关键问题
    # 动态分析维度：由 Scope 阶段根据对象特性生成（四板块仅为参考，不固化）
    sections: list[str] = Field(default_factory=list)


class AnalysisRun(BaseModel):
    """一次完整分析任务的状态 —— 外置、可审计的核心状态对象。"""

    id: str = Field(default_factory=_uid)
    query: str                         # 用户输入
    provider: str = "deepseek"         # 主分析模型
    red_team_provider: str = ""        # 红队模型（为空则按 role_defaults；与主模型相同时标注同源降级）
    reviewer_provider: str = ""        # 终审模型（为空则按 role_defaults；前端可选）
    same_source_review: bool = False   # 是否处于"同源审查降级"（仅一个模型可用时）
    status: str = "pending"            # pending/running/done/error
    profile: Optional[ObjectProfile] = None
    insights: list[Insight] = Field(default_factory=list)
    trace: list[TraceEvent] = Field(default_factory=list)
    evidence_pool: list[Evidence] = Field(default_factory=list)  # 证据池(按编号顺序，供报告引用溯源：[证据N]→#evidence-N→source_url)
    report_md: str = ""                # 洞察式报告（带证据链）
    narrative_md: str = ""             # 完整分析文档（连贯叙事，带引用）
    financials: list[dict] = Field(default_factory=list)  # 多年财务时序（营收/利润/增速，供图表）
    industry_metrics: dict = Field(default_factory=dict)  # 行业级数据时序（总量指标+价格趋势，供行业图表）
    revenue_segments: dict = Field(default_factory=dict)  # 业务板块营收拆分（供饼图）{fiscal_year, currency, segments:[{name,revenue,pct}]}
    peer_financials: list[dict] = Field(default_factory=list)  # 同行财务对比 [{name, ticker, revenue, net_income, period}]
    data_gaps: list[DataGap] = Field(default_factory=list)  # 报告级数据缺口/无源项/来源冲突
    quality_eval: dict = Field(default_factory=dict)  # 借鉴 virattt eval：8维度质量评分 {scores:{}, total, issues:[]}
    collect_quality: dict = Field(default_factory=dict)  # Collect 阶段证据池质量自检 {score,status,issues,...}
    run_metrics: dict = Field(default_factory=dict)  # 运行级观测指标，最终同步写入 data/run_metrics/*.json
    applied_strategy_ids: list[str] = Field(default_factory=list)  # 本次运行实际应用的 active strategy cards
    strategy_effect: dict = Field(default_factory=dict)  # 策略卡应用后的粗粒度效果记录
    data_sources_used: list[str] = Field(default_factory=list)  # 实际命中的数据源
    data_as_of: str = ""               # 本次分析数据截至时点
    error: str = ""
    checkpoint_stage: str = ""         # 最后完成的阶段（scope/collect/analyze/falsify/report），用于断点续跑
    checkpoint_evidence: list[Evidence] = Field(default_factory=list)  # 检查点：内部证据池快照（dict[int,Evidence] 序列化为 list）
    # P1.6: token 成本追踪
    token_summary: dict = Field(default_factory=dict)  # {total_calls, total_input, total_output, total_cost_usd, by_stage}
    # P0.3: 逻辑一致性检查结果
    coherence_check: dict = Field(default_factory=dict)  # {coherent, thesis_identified, contradictions, suggestions}
    # P2.7: Human-in-the-loop 审查点
    human_review: dict = Field(default_factory=dict)  # {scope_confirmed: bool, scope_edits: {}, falsify_annotations: []}
    created_at: str = Field(default_factory=_now)
    updated_at: str = Field(default_factory=_now)
