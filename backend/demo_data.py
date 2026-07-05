"""
Demo 模式 —— 无 LLM/搜索 key 时，用脚本化但真实感的数据跑通全流程。
让 demo 开箱即用；配置真实 key 后自动走真实分析。

内置一个具体案例（奇富科技 QFIN，助贷行业）以获得最佳演示效果；
其他 query 走通用脚本。所有数据均为演示用途，不构成投资建议。
"""
from __future__ import annotations

import re
import time
from datetime import datetime

_INDUSTRY_KIND_RE = re.compile(r'"kind"\s*:\s*"industry"')


def is_demo_query(*_args, **_kw) -> bool:
    return True


def _detect_industry(text: str, sys_text: str = "") -> str | None:
    """检测当前 prompt 是否属于行业分析，返回行业类型或 None。
    方式1：analyze/facts/narrative/search plan 等 prompt 含 profile JSON，检查 kind=industry。
    方式2：red_team/reverdict 的洞察文本含行业级关键词（无公司主体名）。"""
    combined = f"{text}\n{sys_text}"
    if _INDUSTRY_KIND_RE.search(combined):
        if "信贷" in combined or "助贷" in combined or "消费金融" in combined:
            return "credit"
        if "稀土" in combined:
            return "rare_earth"
        if "新能源" in combined and "汽车" in combined:
            return "ev"
        return "generic"
    # red_team/reverdict 无 profile JSON：用行业级关键词检测
    _company_names = ("奇富科技", "Qifu", "QFIN", "字节跳动", "ByteDance", "DeepSeek")
    _credit_kw = ("消费信贷", "助贷行业", "持牌消金", "助贷市场", "行业出清", "CR5", "三层格局")
    _rare_kw = ("稀土", "氧化镨钕", "开采配额", "冶炼分离")
    # 只看洞察论点区域（文本前 300 字），避免被证据摘要中的龙头公司名误判
    head = combined[:400]
    if any(k in combined for k in _rare_kw) and not any(n in head for n in _company_names):
        return "rare_earth"
    if any(k in combined for k in _credit_kw) and not any(n in head for n in _company_names):
        return "credit"
    if "行业规模" in combined and "增速" in combined and not any(n in head for n in _company_names):
        return "generic"
    return None


def _demo_industry_dispatch(text: str, sys_text: str, industry: str):
    """行业分析 demo 分发器：按 prompt 阶段返回行业级 demo 数据。返回 None 则走通用逻辑。"""
    # scope —— 已由 _scope_for 处理，不拦截
    if "请判断并输出 JSON" in text:
        return None

    # search plan
    if "生成 6-8 条" in text:
        return _industry_search_plan(industry)

    # analyze
    if ("请产出足够覆盖所有分析维度的洞察" in text
            or "请产出足够覆盖核心问题的洞察" in text
            or "请产出 5-8 条高质量洞察" in text):
        return _industry_insights(industry)

    # red team
    if "红队" in sys_text and ("九维挑战" in text or "challenges" in text):
        return _industry_red_team(text, industry)

    # 自我迭代·支撑证据检索
    if "证据检索策略师" in sys_text:
        return _industry_support_queries(text, industry)

    # 辩论搜索策略师
    if "辩论搜索策略师" in sys_text:
        return _industry_debate_queries(text, industry)

    # 辩论终审裁判
    if "独立终审裁判" in sys_text and "辩论" in text:
        return _industry_debate_verdict(industry)

    # reverdict
    if "请综合判定" in text or "做出裁决" in text:
        return _industry_reverdict(text, sys_text, industry)

    # audit —— 通用
    if "检查报告中每个量化数字" in text or "数字审计" in sys_text:
        return {"passed": True, "issues": []}

    # 核心发现段（不再含历史趋势，已并入事实段）
    # 核心发现段 ——用 segment marker 精确匹配，避免 outlook prompt 的"不写核心发现"误匹配
    if "完整分析文档的【核心发现】" in sys_text or "【核心发现】段" in sys_text:
        return {"content": _industry_narrative_core(industry)}
    # 兼容旧匹配
    if "洞察层 A" in sys_text or "【洞察层 A" in sys_text:
        return {"content": _industry_narrative_core(industry)}
    # 风险+展望段（注意：新 prompt sys_text 含"不写核心发现"，所以不能排除含"核心发现"的）
    if "风险" in sys_text and "展望" in sys_text and ("风险+展望" in sys_text or "洞察层 B" in sys_text or "【洞察层 B" in sys_text):
        return {"content": _industry_narrative_outlook(industry)}
    if "洞察层 B" in sys_text or "【洞察层 B" in sys_text:
        return {"content": _industry_narrative_outlook(industry)}

    # facts 段（执行摘要+基本事实+历史趋势合并）。放在核心/风险分支之后，避免执行摘要上下文误命中。
    if "事实层" in sys_text or "基本事实层" in sys_text or "【第一段" in sys_text:
        return {"content": _industry_facts(industry)}
    # 旧入口兼容（整段 narrative）
    if "洞察层" in sys_text or "【第二段" in sys_text:
        return {"content": _industry_narrative_core(industry)}

    # 旧入口兼容
    if "零散洞察整合" in sys_text or ("连贯" in text and "执行摘要" in text):
        return {"content": _industry_narrative_core(industry)}

    # Meta Reflection
    if "元反思分析师" in sys_text:
        return _industry_meta_reflection(industry)

    # 行业数据抽取
    if "行业数据抽取员" in sys_text:
        return _industry_data_extraction(industry)

    return None  # 走通用逻辑


def _demo_evidence_summary(text: str) -> str:
    """证据压缩员 demo：把全量证据按分析维度压缩成结构化摘要。"""
    # 检测是否行业分析
    industry = _detect_industry(text, "")
    if industry == "credit":
        return """### 行业规模与增速
- 消费贷款余额约58万亿元，同比+6.5%（2025Q1，央行）[^1]
- 互联网助贷市场规模约3.5万亿元，增速8%-10%（2024全年，互金协会）[^2]
- 2025年5月消费贷款余额约58.6万亿，增速持平（央行月度）[^16]

### 竞争格局与集中度
- 三层格局：银行65%/持牌消金12%/助贷10%（财新）[^3]
- 2024年约200余家小机构退出，CR5约60%（第一财经/第三方研究）[^4][^17][^22][^29]

### 资产质量趋势
- 商业银行不良率1.62%持平，持牌消金不良率2.3%上升10bp（央行）[^5]
- 奇富科技逾期率2.0%，信也科技2.1%，乐信2.4%（公司IR）[^9][^10][^11]
- 核销规模同比上升约15%，账面逾期率可能被压低[^24]

### 监管政策走向
- 利率上限24%征求意见稿，一年过渡期（金融监管总局）[^6][^13][^18][^23]
- 催收规范加强，催收成本上升15%-20%（21世纪经济报道）[^7]

### 资金成本与息差
- LPR 3.45%不变，助贷综合资金成本5%-7%（-20bp）（中国货币网）[^8][^15]"""
    if industry == "rare_earth":
        return """### 开采与冶炼总量
- 2024年开采指标27万吨(+5.9%)，冶炼分离25.4万吨(+4.2%)（工信部）[^1]

### 价格走势
- 氧化镨钕均价38万元/吨(-34%)，氧化镝195.1万元/吨（生意社）[^2]

### 龙头业绩
- 北方稀土营收约230亿元(-15%)，净利润-40%（年报）[^3]"""
    # 公司级（奇富科技）
    return """### 规模与盈利质量
- 在贷余额约1,800亿元，同比+12%（IR）[^1]
- 净利润约18亿元，同比增长（SEC 6-K）[^2]
- take rate 基本持平

### 资产质量真实性
- 90天+逾期率降至约2.0%，但核销规模上升（Reuters）[^3][^16][^21][^24]
- 拨备覆盖率环比上升（SEC filing）[^5]

### 资金与息差
- 银行等持牌机构资金占比提升（电话会）[^4]
- 综合资金成本小幅上行约10bp（第一财经）[^19][^26]

### 监管合规
- 助贷行业综合息费上限监管趋严（第一财经）[^6]"""


def _demo_quality_eval() -> dict:
    """报告质量审查员 demo：8维度评分。"""
    return {
        "scores": {
            "完整性": 4,
            "逻辑性": 4,
            "专业性": 4,
            "数据性": 4,
            "创新性": 3,
            "实用性": 4,
            "合规性": 5,
            "可读性": 4,
        },
        "total": 32,
        "issues": [
            "创新性(3分)：部分洞察为行业常识复述，独到分析增量可加强",
            "数据性(4分)：行业级数据口径一致性可进一步核实",
        ],
    }


def demo_chat_json(messages, provider=None, role="analyst", **kw):
    """模拟 LLM 的结构化输出。根据 prompt 内容判断当前阶段。
    用稳健的标志串识别阶段（与 prompts.py 中的措辞保持一致，避免脆弱匹配）。"""
    time.sleep(0.4)  # 模拟思考延迟，让前端轨迹有节奏感
    text = messages[-1]["content"]
    sys_text = messages[0]["content"] if messages else ""

    # 通用 matchers（公司/行业共用）—— 在行业检测之前，这些 prompt 不含 kind=industry
    # 证据压缩员（evidence_summary_prompt）
    if "证据压缩员" in sys_text:
        return {"content": _demo_evidence_summary(text)}
    # 报告质量审查员（quality_eval_prompt）
    if "报告质量审查员" in sys_text or ("质量审查员" in sys_text and "8个维度" in text):
        return _demo_quality_eval()

    # 行业分析分支：检测到行业分析时走行业专属 demo 数据
    industry = _detect_industry(text, sys_text)
    if industry:
        result = _demo_industry_dispatch(text, sys_text, industry)
        if result is not None:
            return result

    if "请判断并输出 JSON" in text:  # scope
        q = text.split("分析对象：")[1].split("\n")[0].strip() if "分析对象：" in text else ""
        return _scope_for(q)

    if "生成 6-8 条" in text:  # search plan
        return [
            "奇富科技 2025 Q1 财报 在贷余额 增速",
            "奇富科技 90天以上逾期率 拨备覆盖率",
            "奇富科技 资金成本 资金方 集中度",
            "助贷行业 监管 利率上限 2025",
            "奇富科技 净利润 同比 风控",
            "Qifu Technology Q1 2025 earnings net income",
        ]

    if ("请产出足够覆盖所有分析维度的洞察" in text
            or "请产出足够覆盖核心问题的洞察" in text
            or "请产出 5-8 条高质量洞察" in text):  # analyze
        return {"insights": [
            {"section": "财务表现", "claim": "净利润高增长主要由放款规模扩张驱动，而非单位经济改善",
             "reasoning": "营收与在贷余额同步上行，但 take rate 基本持平，说明增长靠'量'不靠'价'。需警惕规模见顶后的增速回落。",
             "falsifiable_condition": "",
             "evidence_ids": [1, 2], "confidence": 0.62},
            {"section": "经营指标", "claim": "逾期率改善的真实性存疑，可能受核销与分母做大掩盖",
             "reasoning": "90天+逾期率账面下降，但同期核销规模上升、在贷余额快速扩张，分母变大会机械拉低比率。真实资产质量需看 vintage。",
             "falsifiable_condition": "",
             "evidence_ids": [2], "confidence": 0.5},
            {"section": "战略动向", "claim": "资金来源向持牌机构集中，降低合规风险但抬高资金成本",
             "reasoning": "管理层强调资金结构优化、银行资金占比提升，利于合规，但银行资金议价能力强，息差可能被压缩。",
             "falsifiable_condition": "",
             "evidence_ids": [3], "confidence": 0.55},
            {"section": "产品与竞争", "claim": "管理层执行力出色，公司前景光明",
             "reasoning": "整体经营稳健，团队经验丰富。",
             "falsifiable_condition": "",
             "evidence_ids": [], "confidence": 0.5},
        ]}

    # 红队九维挑战（sys_text 含"红队"且 user 含"九维挑战"或"challenges"）
    if "红队" in sys_text and ("九维挑战" in text or "challenges" in text):
        return _demo_red_team_nine_dim(text)

    # 自我迭代·支撑证据检索查询（sys_text 含"证据检索策略师"）
    if "证据检索策略师" in sys_text:
        if "逾期" in text or "vintage" in text or "资产质量" in text:
            return {"support_queries": ["奇富科技 2025 vintage 滚动率 改善", "奇富科技 资产质量 稳定 2025Q1"]}
        if "净利润" in text or "规模" in text or "拨备" in text:
            return {"support_queries": ["奇富科技 2025Q1 拨备覆盖率 上升", "Qifu provision coverage 2025 Q1"]}
        return {"support_queries": ["奇富科技 综合资金成本 稳定 2025", "Qifu funding cost stable 2025"]}

    # 辩论搜索策略师
    if "辩论搜索策略师" in sys_text:
        return {"debate_queries": ["奇富科技 拨备覆盖率 2025 稳定 上升", "奇富科技 资产质量 改善 vintage"]}

    # 辩论终审裁判
    if "独立终审裁判" in sys_text and "辩论" in text:
        return {"verdict": "questionable", "confidence": 0.5,
                "note": "辩论中分析师提供了部分反驳证据，但红队挑战仍未完全化解。",
                "resolved_dimensions": ["missing_evidence"],
                "open_dimensions": ["temporal", "alternative"],
                "gap_explanation": "拨备覆盖率原始数据仍有缺口，维持存疑但标注辩论结果。"}

    if "请综合判定" in text or "做出裁决" in text:  # reverdict（含九维挑战 + 自我迭代二次裁决）
        is_refine = "自我迭代二次裁决" in sys_text
        if "核销" in text or "vintage" in text or "逾期" in text:
            return {"verdict": "questionable" if not is_refine else "questionable",
                    "confidence": 0.42 if not is_refine else 0.48,
                    "note": "找到核销规模上升的反证，逾期率改善真实性存疑。"
                            + ("自我迭代补强 vintage 数据后仍无决定性改善。" if is_refine else ""),
                    "resolved_dimensions": ["source_reliability"] if is_refine else [],
                    "open_dimensions": ["temporal", "missing_evidence"],
                    "gap_explanation": "vintage 滚动率数据缺失，无法完全验证资产质量改善，保留但标注缺口。"}
        if "拨备" in text:
            return {"verdict": "supported", "confidence": 0.71,
                    "note": "核对拨备覆盖率稳中有升，利润增长非靠少提拨备，原结论得到支持。",
                    "resolved_dimensions": ["logic", "missing_evidence", "source_reliability"],
                    "open_dimensions": [],
                    "gap_explanation": ""}
        return {"verdict": "questionable" if not is_refine else "questionable",
                "confidence": 0.52 if not is_refine else 0.55,
                "note": "资金成本数据有限，维持存疑。"
                        + ("自我迭代后补充资金成本数据但仍不充分。" if is_refine else ""),
                "resolved_dimensions": ["conflict_of_interest"] if is_refine else [],
                "open_dimensions": ["source_reliability", "missing_evidence"],
                "gap_explanation": "综合资金成本趋势数据不充分，保留但标注数据缺口。"}

    if "检查报告中每个量化数字" in text or "数字审计" in sys_text:  # audit
        return {"passed": True, "issues": []}

    # 核心发现段 ——用 segment marker 精确匹配，避免 outlook prompt 的"不写核心发现"误匹配
    if "完整分析文档的【核心发现】" in sys_text or "【核心发现】段" in sys_text:
        name = _name_from(text)
        same = "同源审查降级" in text
        return {"content": demo_narrative_core(name or "该公司", same)}
    # 兼容旧匹配
    if "洞察层 A" in sys_text or "【洞察层 A" in sys_text:
        name = _name_from(text)
        same = "同源审查降级" in text
        return {"content": demo_narrative_core(name or "该公司", same)}
    # 风险+展望段（注意：新 prompt sys_text 含"不写核心发现"，所以不能排除含"核心发现"的）
    if "风险" in sys_text and "展望" in sys_text and ("风险+展望" in sys_text or "洞察层 B" in sys_text or "【洞察层 B" in sys_text):
        name = _name_from(text)
        same = "同源审查降级" in text
        return {"content": demo_narrative_outlook(name or "该公司", same)}
    # 完整分析文档（narrative）——事实层 + 核心发现 + 风险展望。放在核心/风险分支之后，避免执行摘要上下文误命中。
    if "事实层" in sys_text or "基本事实层" in sys_text or "【第一段" in sys_text:
        name = _name_from(text)
        same = "同源审查降级" in text
        return {"content": _demo_facts(name or "该公司", same)}
    # 旧入口兼容（整段 narrative）
    if "洞察层" in sys_text or "【第二段" in sys_text:
        name = _name_from(text)
        same = "同源审查降级" in text
        return {"content": demo_narrative_core(name or "该公司", same)}
    if "零散洞察整合" in sys_text or ("连贯" in text and "执行摘要" in text):
        name = _name_from(text)
        same = "同源审查降级" in text
        return {"content": demo_narrative_core(name or "该公司", same)}

    # Meta Reflection（四层记忆④：元反思）
    if "元反思分析师" in sys_text:
        return {
            "missed_challenges": [
                {"insight_claim": "资金来源向持牌机构集中",
                 "missed_angle": "未检查指标定义（'持牌机构资金占比'）是否在期间内变更",
                 "why_missed": "策略未覆盖'指标口径变更'这一 temporal 维度内的特定角度",
                 "trigger_signal": "当 claim 含'占比提升/结构优化'且数据来自公司自报时，自动检查指标定义一致性"}
            ],
            "policy_updates": [
                {"action": "add",
                 "policy": {
                     "trigger": "claim 含'占比提升'或'结构优化'且数据来自公司自报",
                     "challenge_type": "temporal",
                     "search_strategy": "搜索指标定义是否在比较期间内发生变更（如口径调整、统计范围变化）",
                     "evidence_preference": ["filing", "regulatory"],
                     "priority": "medium"},
                 "reason": "逾期率改善分析中遗漏了指标口径变更检查，补充此策略"}
            ],
            "industry_pattern_updates": [],
            "overall_assessment": "本次分析覆盖九维挑战较全面，但 temporal 维度内遗漏了指标口径一致性检查，已新增策略覆盖。"
        }

    # 行业数据抽取（行业分析专用：总量指标 + 产品价格时序）
    if "行业数据抽取员" in sys_text:
        if "稀土" in text:
            return {
                "totals": [
                    {"metric": "稀土开采总量控制指标", "unit": "吨",
                     "values": [
                         {"period": "2022", "value": 210000, "growth": 25.0},
                         {"period": "2023", "value": 255000, "growth": 21.4},
                         {"period": "2024", "value": 270000, "growth": 5.9},
                     ]},
                    {"metric": "冶炼分离总量控制指标", "unit": "吨",
                     "values": [
                         {"period": "2022", "value": 202000, "growth": 24.3},
                         {"period": "2023", "value": 243850, "growth": 20.7},
                         {"period": "2024", "value": 254000, "growth": 4.2},
                     ]},
                ],
                "prices": [
                    {"product": "氧化镨钕", "unit": "万元/吨",
                     "points": [
                         {"period": "2022", "price": 90.0},
                         {"period": "2023", "price": 57.7},
                         {"period": "2024H1", "price": 38.2},
                     ]},
                    {"product": "氧化镝", "unit": "万元/吨",
                     "points": [
                         {"period": "2022", "price": 260.0},
                         {"period": "2023", "price": 215.0},
                         {"period": "2024H1", "price": 195.1},
                     ]},
                ],
            }
        return {"totals": [], "prices": []}

    return {}


def _name_from(text):
    import re as _re
    m = _re.search(r'"name"\s*:\s*"([^"]+)"', text)
    return m.group(1) if m else ""


def _demo_red_team_nine_dim(text: str) -> dict:
    """九维挑战 demo：根据洞察内容返回 9 个维度的挑战结果。"""
    if "逾期" in text or "资产质量" in text or "vintage" in text:
        return {
            "challenges": [
                {"dimension": "temporal", "severity": "low", "challenge": "逾期率口径在2024-2025年间从M1+调整为M3+，跨期可比性存疑", "search_query": "奇富科技 逾期率 口径变更 M1 M3"},
                {"dimension": "conflict_of_interest", "severity": "medium", "challenge": "逾期率数据主要来自公司自报 IR/财报，存在口径选择倾向", "search_query": "奇富科技 逾期率 口径 第三方核实"},
                {"dimension": "source_reliability", "severity": "low", "challenge": "部分数据来自 Reuters 报道，权威性尚可但非原始 filing", "search_query": ""},
                {"dimension": "logic", "severity": "high", "challenge": "推理存在因果简化：逾期率下降可能由核销加大、分母扩张机械拉低，而非真实资产质量改善", "search_query": "奇富科技 核销 write-off 规模 2025"},
                {"dimension": "external_consistency", "severity": "medium", "challenge": "行业同期助贷平台逾期率趋势是否一致尚不明确", "search_query": "助贷行业 2025 逾期率 趋势"},
                {"dimension": "boundary", "severity": "low", "challenge": "结论隐含'分母稳定'前提，但放款增速 12% 使该前提不成立", "search_query": ""},
                {"dimension": "alternative", "severity": "high", "challenge": "替代解释：逾期率下降是核销前置的结果，真实坏账在恶化", "search_query": "奇富科技 vintage 滚动率 M3"},
                {"dimension": "missing_evidence", "severity": "high", "challenge": "缺失关键证据：分年 vintage 滚动率数据，这是验证资产质量改善的最直接指标", "search_query": "奇富科技 vintage 滚动率 2025"},
                {"dimension": "independence", "severity": "low", "challenge": "多条证据均引用同一财报季数据，独立性有限", "search_query": ""},
            ],
            "overall": "incomplete",
            "overall_note": "逻辑与缺失证据维度为 high，存在因果简化与关键证据缺失，但非直接反证，判定为 incomplete",
            "search_queries": ["奇富科技 核销 write-off 规模 2025", "奇富科技 vintage 滚动率 M3", "助贷行业 2025 逾期率 趋势"],
        }
    if "净利润" in text or "规模" in text or "单位经济" in text:
        return {
            "challenges": [
                {"dimension": "temporal", "severity": "low", "challenge": "数据为 2025Q1 最新季度，时效尚可", "search_query": ""},
                {"dimension": "conflict_of_interest", "severity": "medium", "challenge": "管理层在电话会中倾向乐观表述增长来源", "search_query": ""},
                {"dimension": "source_reliability", "severity": "none", "challenge": "核心数据来自 SEC 6-K filing，权威性高", "search_query": ""},
                {"dimension": "logic", "severity": "medium", "challenge": "推理假设 take rate 持平即'量驱动'，但未排除定价微调的可能", "search_query": "奇富科技 take rate 2025 变化"},
                {"dimension": "external_consistency", "severity": "none", "challenge": "与同业助贷平台规模驱动增长的行业趋势一致", "search_query": ""},
                {"dimension": "boundary", "severity": "low", "challenge": "结论适用于当前规模阶段，规模见顶后可能变化", "search_query": ""},
                {"dimension": "alternative", "severity": "medium", "challenge": "替代解释：利润增长部分来自拨备计提减少而非规模扩张", "search_query": "奇富科技 拨备覆盖率 计提 2025"},
                {"dimension": "missing_evidence", "severity": "low", "challenge": "拨备覆盖率数据可从 filing 获取，当前已引用", "search_query": ""},
                {"dimension": "independence", "severity": "none", "challenge": "证据来自 filing + IR + 电话会，来源多元", "search_query": ""},
            ],
            "overall": "solid",
            "overall_note": "核心维度均无 high 挑战，替代理论可通过拨备数据排除，判定为 solid",
            "search_queries": ["奇富科技 拨备覆盖率 计提 2025", "Qifu provision coverage ratio 2025"],
        }
    # 默认（资金结构相关）
    return {
        "challenges": [
            {"dimension": "temporal", "severity": "low", "challenge": "资金结构数据为 2025Q1，时效尚可", "search_query": ""},
            {"dimension": "conflict_of_interest", "severity": "medium", "challenge": "资金结构优化表述来自管理层电话会，有自我标榜倾向", "search_query": ""},
            {"dimension": "source_reliability", "severity": "low", "challenge": "资金成本数据来自媒体测算，非原始 filing", "search_query": ""},
            {"dimension": "logic", "severity": "low", "challenge": "推理链条清晰：合规增强→资金方变化→成本变化", "search_query": ""},
            {"dimension": "external_consistency", "severity": "medium", "challenge": "行业资金成本上行趋势是否与结论一致尚不明确", "search_query": "助贷行业 资金成本 2025 趋势"},
            {"dimension": "boundary", "severity": "none", "challenge": "结论适用范围明确，未夸大", "search_query": ""},
            {"dimension": "alternative", "severity": "medium", "challenge": "替代解释：资金成本上升可能是市场利率环境变化而非资金结构变化", "search_query": "奇富科技 综合资金成本 2025"},
            {"dimension": "missing_evidence", "severity": "medium", "challenge": "缺失综合资金成本的定量时序数据，仅有定性描述", "search_query": "Qifu funding cost trend"},
            {"dimension": "independence", "severity": "none", "challenge": "证据来源多元", "search_query": ""},
        ],
        "overall": "incomplete",
        "overall_note": "替代理论与缺失证据维度为 medium，资金成本定量数据不足，判定为 incomplete",
        "search_queries": ["奇富科技 综合资金成本 2025", "Qifu funding cost trend", "助贷行业 资金成本 2025 趋势"],
    }


def _demo_facts(profile_name: str, same_source: bool = False) -> str:
    """narrative 基本事实层 demo：执行摘要 + 核心摘要表 + 公司画像 + 财务概览表。"""
    return f"""## 执行摘要

{profile_name}当前呈现"规模驱动增长、账面资产质量改善但真实性存疑"的组合特征。利润增长主要由放款规模扩张贡献（在贷余额+12%，take rate持平），而非单位经济改善——这意味着一旦放款增速见顶，利润增长引擎将熄火。逾期率降至2.0%看似改善，但同期核销规模上升约15%、分母快速扩张机械拉低比率，真实资产质量需看vintage滚动率。资金结构向持牌机构集中利于合规，但银行资金议价能力强推动综合成本上行约10bp，息差面临双向挤压。

| 维度 | 关键数据 | 判断 |
|---|---|---|
| 盈利增长 | 净利润约18亿，在贷余额+12%，take rate持平 | 量驱动，见顶后承压 |
| 资产质量 | 90天+逾期率2.0%，但核销+15% | 账面改善存疑 |
| 资金结构 | 持牌机构占比提升，综合成本+10bp | 合规增强但息差承压 |

## 公司画像

| 项 | 内容 |
|---|---|
| 对象 | {profile_name} |
| 商业模式 | 数字信贷助贷 |
| 对标 | 同业助贷平台 |

### 财务概览

| 期间 | 营收(亿元) | 净利润(亿元) | 营收增速 |
|---|---|---|---|
| 2025Q1 | — | 约 18 | — |

### 历史趋势与拐点

| 指标 | 2024Q1 | 2024Q4 | 2025Q1 | 趋势判断 |
|---|---|---|---|---|
| 在贷余额增速 | ~15% | ~13% | 12% | 缓慢下行，接近见顶 |
| 逾期率(账面) | ~2.3% | ~2.1% | 2.0% | 账面改善但核销同步上升 |
| 核销规模增速 | — | ~10% | ~15% | 加速出表 |
| take rate | 持平 | 持平 | 持平 | 量驱动逻辑未变 |

关键拐点信号：①放款增速跌破8%→利润增长引擎切换；②vintage M3+滚动率恶化超20%→逾期率改善被证伪；③利率上限24%正式稿无过渡期→高定价存量资产需重构。"""


def demo_search(query, max_results=5, days=None):
    time.sleep(0.3)
    q = query.lower()
    # —— 行业分析搜索结果（信贷/助贷行业）——
    if "消费信贷" in q and ("增速" in q or "余额" in q or "月度" in q):
        return [{"title": "央行信贷收支表2025年5月", "url": "https://www.pbc.gov.cn/credit-2025-05",
                 "content": "央行数据显示，2025年5月末消费贷款余额约58.6万亿元，同比增长6.5%，增速与一季度持平，月度增量主要来自个人住房贷款和经营贷。",
                 "score": 0.85}]
    if "助贷" in q and ("cr5" in q or "集中度" in q or "份额" in q or "排名" in q):
        return [{"title": "助贷行业集中度研究", "url": "https://www.caixin.com/credit-cr5-2025",
                 "content": "据第三方研究，2024年助贷平台CR5约60%，较2023年提升约5个百分点。奇富科技、乐信、信也科技位列前三，头部集中度持续提升。",
                 "score": 0.78}]
    if "助贷" in q and ("退出" in q or "出清" in q or "机构" in q):
        return [{"title": "助贷机构退出加速", "url": "https://www.yicai.com/credit-exit-2025",
                 "content": "据金融监管部门数据，2024年约有200余家小型助贷机构退出市场，主要原因为监管合规门槛提高和业务模式转型压力。",
                 "score": 0.76}]
    if "不良率" in q and ("持牌" in q or "消金" in q):
        return [{"title": "持牌消金不良率统计", "url": "https://www.pbc.gov.cn/npl-2025q1",
                 "content": "央行统计显示，2025Q1持牌消金公司平均不良率约2.3%，环比上升约10bp，资产质量整体承压。",
                 "score": 0.82}]
    if "助贷" in q and ("风控" in q or "逾期" in q or "客群" in q):
        return [{"title": "助贷平台风控对比研究", "url": "https://www.reuters.com/credit-risk-2025",
                 "content": "第三方研究显示，头部助贷平台逾期率（2.0%-2.1%）低于持牌消金平均（2.3%），但客群结构差异是重要因素，同客群条件下差异缩小。",
                 "score": 0.75}]
    if "助贷" in q and "资金成本" in q:
        return [{"title": "助贷资金成本趋势", "url": "https://www.chinamoney.com.cn/funding-2025",
                 "content": "2025年上半年助贷平台综合资金成本约5%-7%，较2024年下降约20bp，主要受LPR维持低位和资金方多元化驱动。",
                 "score": 0.78}]
    if "lpr" in q:
        return [{"title": "LPR公告2025年6月", "url": "https://www.chinamoney.com.cn/lpr-2025-06",
                 "content": "2025年6月1年期LPR维持3.45%不变，5年期以上LPR维持3.95%不变，符合市场预期。",
                 "score": 0.85}]
    if "利率上限" in q or "24%" in q or "管理办法" in q:
        return [{"title": "互联网消费信贷管理办法征求意见稿", "url": "https://www.nfra.gov.cn/lending-reg-2025",
                 "content": "金融监管总局发布征求意见稿，明确综合息费上限不超过24%，设有一年过渡期。正式稿预计2025年下半年发布。",
                 "score": 0.82}]
    # —— 稀土行业搜索结果 ——
    if "稀土" in q and ("配额" in q or "开采" in q):
        return [{"title": "工信部2025年稀土配额", "url": "https://www.miit.gov.cn/reearth-quota-2025",
                 "content": "工信部2025年第一批稀土开采总量控制指标14万吨，同比增长6%，增速与2024年持平。",
                 "score": 0.82}]
    if "稀土" in q and ("价格" in q or "氧化镨钕" in q):
        return [{"title": "稀土价格走势2025", "url": "https://www.100ppi.com/reearth-price-2025",
                 "content": "2025年上半年氧化镨钕均价约35万元/吨，较2024年下半年企稳，下游磁材需求有所恢复。",
                 "score": 0.75}]
    if "稀土" in q and ("海外" in q or "缅甸" in q or "供给" in q):
        return [{"title": "稀土海外供给分析", "url": "https://www.caixin.com/reearth-supply-2025",
                 "content": "2024年缅甸稀土进口量同比增加约15%，海外供给增加对国内价格形成一定压力。",
                 "score": 0.72}]
    # —— 反证类查询（公司分析·证伪阶段）——
    if "核销" in q or "write-off" in q or "vintage" in q or "滚动率" in q:
        return [{"title": "奇富科技核销规模上升", "url": "https://www.caixin.com/qfin-writeoff",
                 "content": "据测算，2025Q1核销规模同比上升约15%，部分逾期资产通过核销出表，账面逾期率因此被动改善。",
                 "score": 0.8}]
    if "拨备" in q or "provision" in q:
        return [{"title": "拨备覆盖率稳中有升", "url": "https://www.sec.gov/qfin-provision",
                 "content": "拨备覆盖率季度环比上升，显示利润增长并非依赖少提拨备。", "score": 0.8}]
    if "资金成本" in q or "funding cost" in q:
        return [{"title": "综合资金成本小幅上行", "url": "https://www.yicai.com/qfin-funding",
                 "content": "银行资金占比提升的同时，综合资金成本季度环比小幅上行约10bp，息差面临一定压力。",
                 "score": 0.75}]
    # 采集阶段：按查询主题返回不同证据，避免重复
    if "在贷余额" in q or "增速" in q:
        return [{"title": "Qifu IR", "url": "https://ir.qifu.tech/q1-2025",
                 "content": "奇富科技2025Q1在贷余额约人民币1,800亿元，同比增长约12%，放款量稳步提升，take rate基本持平。",
                 "score": 0.8}]
    if "逾期" in q or "拨备覆盖" in q:
        return [{"title": "Reuters", "url": "https://www.reuters.com/qifu-q1-2025",
                 "content": "90天以上逾期率环比下降至2.0%附近，但季度核销规模同比上升，资产质量真实性需结合vintage判断。",
                 "score": 0.78}]
    if "资金" in q or "集中度" in q:
        return [{"title": "Earnings Call", "url": "https://ir.qifu.tech/transcript-q1-2025",
                 "content": "管理层称银行等持牌金融机构资金占比进一步提升，资金结构持续优化，合规性增强。",
                 "score": 0.76}]
    if "监管" in q or "利率上限" in q:
        return [{"title": "第一财经", "url": "https://www.yicai.com/lending-reg-2025",
                 "content": "助贷行业综合息费上限监管趋严，对高定价资产形成压力，行业整体定价空间收窄。",
                 "score": 0.72}]
    if "净利润" in q or "net income" in q or "earnings" in q:
        return [{"title": "SEC 6-K", "url": "https://www.sec.gov/qfin-6k",
                 "content": "奇富科技2025Q1净利润约人民币18亿元，同比增长，主要由放款规模扩张贡献。",
                 "score": 0.82}]
    return [{"title": "公开资料", "url": "https://example.com/qfin",
             "content": f"关于「{query}」的公开信息：经营整体稳健，具体数据需结合财报核实。", "score": 0.6}]


def demo_sec(ticker):
    return {"cik": "0001660134", "ticker": ticker, "concepts": {
        "Revenue": [{"fy": 2025, "fp": "Q1", "val": 4600000000, "end": "2025-03-31", "form": "6-K"}],
        "NetIncome": [{"fy": 2025, "fp": "Q1", "val": 1800000000, "end": "2025-03-31", "form": "6-K"}],
    }}


def _scope_for(q: str) -> dict:
    ql = q.lower()
    if "字节" in q or "bytedance" in ql:
        return {"name": "字节跳动", "kind": "company", "is_public": False, "ticker": "",
                "industry": "互联网 内容平台 AI", "business_model": "短视频+广告+AI，全球化内容生态",
                "peers": ["腾讯", "Meta", "快手"],
                "key_questions": ["广告增长是否见顶", "AI 投入的变现路径", "估值是否被一级市场情绪推高", "全球化的监管风险"],
                "sections": ["广告变现与增长", "AI投入与商业化", "估值合理性", "全球化与监管", "生态位与对标"]}
    if "deepseek" in ql:
        return {"name": "DeepSeek", "kind": "company", "is_public": False, "ticker": "",
                "industry": "AI 大模型", "business_model": "高性价比大模型，开源+API变现",
                "peers": ["OpenAI", "Anthropic", "月之暗面"],
                "key_questions": ["模型能力的护城河", "推理成本结构", "商业化变现能力", "算力供给约束"],
                "sections": ["技术能力定位", "推理成本与算力", "商业化变现", "生态位与对标", "估值合理性"]}
    # —— 行业分析：按行业关键词匹配专属框架 ——
    if "信贷" in q or "助贷" in q or "消费金融" in q or "credit" in ql:
        return {"name": q if "行业" in q else f"{q}行业", "kind": "industry", "is_public": True, "ticker": "",
                "industry": "消费信贷与助贷", "business_model": "银行/持牌消金/助贷平台 三层体系",
                "peers": ["奇富科技", "乐信", "信也科技", "陆金所"], "leaders": ["奇富科技", "乐信", "信也科技", "陆金所"],
                "key_questions": ["行业增速是否见顶", "监管利率上限对定价空间的冲击", "资产质量整体趋势与分化", "头部集中度变化", "资金成本走势"],
                "sections": ["行业规模与增速", "竞争格局与集中度", "资产质量趋势", "监管政策走向", "资金成本与息差"]}
    if "稀土" in q or "rare earth" in ql:
        return {"name": q if "行业" in q else f"{q}行业", "kind": "industry", "is_public": True, "ticker": "",
                "industry": "稀土开采与冶炼分离", "business_model": "开采配额+冶炼分离+下游应用",
                "peers": ["北方稀土", "中国稀土", "盛和资源"], "leaders": ["北方稀土", "中国稀土", "盛和资源"],
                "key_questions": ["价格走势与供需平衡", "开采配额变化影响", "下游需求结构变化", "战略储备政策"],
                "sections": ["开采与冶炼总量", "价格走势", "供需格局", "政策与配额", "龙头业绩"]}
    if "新能源" in q and ("汽车" in q or "车" in q) or "electric vehicle" in ql or "ev " in ql:
        return {"name": q if "行业" in q else f"{q}行业", "kind": "industry", "is_public": True, "ticker": "",
                "industry": "新能源汽车", "business_model": "整车制造+电池+智能驾驶",
                "peers": ["比亚迪", "宁德时代", "理想汽车", "蔚来"], "leaders": ["比亚迪", "宁德时代", "理想汽车", "蔚来"],
                "key_questions": ["行业增速拐点", "盈利能力分化", "政策与补贴变化", "产能过剩风险"],
                "sections": ["行业增速与渗透率", "竞争格局与份额", "盈利能力分化", "政策与补贴", "产能与供需"]}
    if "行业" in q or "industry" in ql:
        # 通用行业兜底：维度适用面广
        return {"name": q, "kind": "industry", "is_public": True, "ticker": "",
                "industry": q, "business_model": "行业整体",
                "peers": [], "leaders": [],
                "key_questions": ["行业规模与增速", "竞争格局与集中度", "政策与监管环境", "盈利能力与成本结构"],
                "sections": ["行业规模与增速", "竞争格局与集中度", "盈利能力", "政策与监管", "趋势与风险"]}
    # 默认：奇富科技（最佳演示案例）
    return {"name": "奇富科技", "kind": "company", "is_public": True, "ticker": "QFIN",
            "industry": "助贷 消费金融 金融科技", "business_model": "撮合放贷+风控科技服务，轻资本为主",
            "peers": ["乐信", "信也科技", "陆金所"],
            "key_questions": ["利润增长是否靠放松风控", "逾期率改善是否真实", "资金成本与息差能否维持", "监管收紧的冲击"],
            "sections": ["规模与盈利质量", "资产质量真实性", "资金与息差", "监管合规", "风控能力"]}


def demo_narrative(profile_name: str, same_source: bool = False) -> str:
    """生成完整分析文档（demo 版）。兼容旧入口，返回 core+outlook 合并。"""
    return demo_narrative_core(profile_name, same_source) + "\n\n" + demo_narrative_outlook(profile_name, same_source)


def demo_narrative_core(profile_name: str, same_source: bool = False) -> str:
    """核心发现段（历史趋势已并入事实段）。"""
    return f"""## 核心发现
### 增长靠量不靠价，单位经济没有改善

营收与在贷余额同步上行（+12%），但take rate基本持平[^1][^2]，说明利润增长完全由放款规模驱动，而非单位经济改善。这意味着增长模型高度依赖放款量持续扩张——一旦增速从12%回落至个位数，利润增长将显著放缓。红队核对拨备覆盖率稳中有升[^5]，排除了"少提拨备增厚利润"的替代解释，增长来源相对可靠。但核心问题是：12%的放款增速在行业增速放缓至6.5%的背景下能否持续？若不能，take rate需要提升才能维持利润增长，而利率上限监管恰恰在压缩take rate的上行空间。 (置信度 46%·成立)

### 逾期率改善可能是会计幻象

90天+逾期率账面降至2.0%[^3]，但同期核销规模上升约15%[^24]、在贷余额+12%快速扩张——分母变大本身就会机械拉低比率。三个变量（逾期率↓、核销↑、分母↑）同时发生，最简单的解释是：真实资产质量没有改善，只是通过核销把坏账出表、通过做大分母稀释比率。验证这一假设需要看vintage滚动率（同一年份放款的M3+违约率趋势），但奇富科技未完整披露该数据。**若后续放款增速放缓而核销维持高位，账面逾期率可能在规模放缓后重新抬升，形成"滞后暴露"。** (置信度 35%·存疑)

### 资金结构优化的代价是息差收窄

银行等持牌机构资金占比提升[^4]有利于合规——监管对资金方资质的要求趋严，银行资金是最安全的资金来源。但银行资金议价能力强，综合资金成本环比上行约10bp[^19]。这是一个trade-off而非纯利好：合规性增强降低了监管风险，但成本上行压缩了息差。在利率上限24%即将落地的背景下，资产端定价空间也在收窄——资金成本上行+定价上限压缩=息差双向挤压。"""


def demo_narrative_outlook(profile_name: str, same_source: bool = False) -> str:
    """洞察层 B：风险 + 展望。"""
    degrade = "\n- 同源审查降级：红队非异源，证伪独立性受限" if same_source else ""
    return f"""## 风险与不确定性

**逾期率改善的真实性**：90天+逾期率降至2.0%可能被核销和分母做大机械拉低。2025Q1核销规模同比+15%，在贷余额+12%快速扩张——分母变大本身就会拉低比率。真正的资产质量需看vintage滚动率（同一年份放款的M3+违约率），但奇富科技未完整披露该数据。若后续放款增速放缓而核销维持高位，账面逾期率可能重新抬升。

**利率上限24%对定价空间的压缩**：助贷行业综合息费上限24%的征求意见稿将压缩高定价资产空间。奇富科技take rate基本持平说明当前定价结构尚未受冲击，但正式稿落地后（预计Q4），年化定价超24%的存量资产需要重构，可能短期冲击放款量和利润。

**资金成本与息差的剪刀差**：银行资金占比提升利于合规，但银行资金议价能力强，综合资金成本环比上行约10bp。若利率上限进一步压缩资产端定价，而资金成本继续上行，息差空间将被双向挤压。{degrade}

## 展望与关注点

| 跟踪指标 | 当前值 | 关注阈值 | 触发后影响 |
|---|---|---|---|
| vintage M3+滚动率 | 未披露 | 恶化超20% | 逾期率改善被证伪，利润承压 |
| take rate | 基本持平 | 下降超50bp | 增长靠量不靠价的逻辑动摇 |
| 综合资金成本 | 小幅上行 | 上行超30bp | 息差空间被双向挤压 |
| 利率上限正式稿 | 征求意见稿 | ≤24%且无过渡期 | 高定价资产需重构，短期冲击放款量 |

---
*数据截至 {datetime.now().strftime('%Y-Q1')} · 数据源: SEC EDGAR, 媒体报道, 公司公告*
*本报告基于公开信息，不构成投资建议。*
"""


def demo_collect_evidence(query: str) -> list[dict]:
    """demo 模式 Collect 阶段返回的带时效证据列表。
    行业分析返回行业级证据（总量/格局/政策/龙头交叉验证），公司分析返回公司级证据。"""
    q = query or ""
    # —— 行业分析：返回行业级证据 ——
    if "信贷" in q or "助贷" in q or "消费金融" in q:
        return _industry_evidence_credit(q)
    if "稀土" in q:
        return _industry_evidence_rare_earth(q)
    if "行业" in q:
        return _industry_evidence_generic(q)
    # —— 公司分析：奇富科技案例 ——
    return _company_evidence_qfin()


def _industry_evidence_credit(query: str) -> list[dict]:
    """中国信贷/助贷行业 demo 证据：行业总量 + 格局 + 政策 + 龙头交叉验证。"""
    return [
        # —— 行业总量（央行/监管口径，高权威）——
        {"content": f"央行数据显示，截至{datetime.now().year}年一季度末，本外币消费贷款余额约58万亿元，同比增长约6.5%，增速较{datetime.now().year-1}年全年有所放缓",
         "url": f"https://www.pbc.gov.cn/goutongjiaoliu/113456/credit-{datetime.now().year}q1",
         "title": "央行金融机构信贷收支表", "tier": 5,
         "source_type": "regulatory", "published_at": f"{datetime.now().year}-05-12"},
        {"content": f"互联网助贷市场规模约3.5万亿元（{datetime.now().year-1}年全年），预计{datetime.now().year}年增速回落至8%-10%，增量主要来自场景金融与小微经营贷",
         "url": f"https://www.mpfca.org.cn/industry-report-{datetime.now().year}",
         "title": "中国互金协会助贷行业报告", "tier": 5,
         "source_type": "regulatory", "published_at": f"{datetime.now().year}-04-20"},
        # —— 竞争格局 ——
        {"content": "消费信贷市场形成三层格局：银行（约65%份额）、持牌消金（约12%）、助贷平台（约10%）及信托/小贷等（约13%），头部助贷平台集中度CR5约60%",
         "url": "https://www.caixin.com/credit-landscape-2025",
         "title": "财新·消费信贷格局", "tier": 6,
         "source_type": "news", "published_at": "2025-04-28"},
        {"content": "监管趋严背景下，2024年约有200余家小型助贷机构退出市场，行业出清加速，头部平台市场份额进一步提升",
         "url": "https://www.yicai.com/credit-consolidation",
         "title": "第一财经·行业出清", "tier": 6,
         "source_type": "news", "published_at": "2025-03-15"},
        # —— 资产质量趋势 ——
        {"content": "商业银行不良贷款率2025Q1为1.62%，环比基本持平；消费金融公司平均不良率约2.3%，较2024年小幅上升约10bp",
         "url": "https://www.pbc.gov.cn/goutongjiaoliu/113456/npl-2025q1",
         "title": "央行不良率统计", "tier": 5,
         "source_type": "regulatory", "published_at": "2025-05-12"},
        # —— 监管政策 ——
        {"content": "金融监管总局2025年发布《互联网消费信贷业务管理暂行办法（征求意见稿）》，明确综合息费上限不超过24%，对高定价资产形成压力",
         "url": "https://www.nfra.gov.cn/lending-reg-2025",
         "title": "金融监管总局", "tier": 5,
         "source_type": "regulatory", "published_at": "2025-04-10"},
        {"content": "多地金融监管部门加强催收规范管理，禁止暴力催收，部分平台催收成本上升约15%-20%",
         "url": "https://www.21jingji.com/collection-reg-2025",
         "title": "21世纪经济报道", "tier": 6,
         "source_type": "news", "published_at": "2025-03-22"},
        # —— 资金成本 ——
        {"content": "2025年上半年银行间同业拆借利率维持低位，1年期LPR维持3.45%不变，助贷平台综合资金成本约5%-7%，较2024年小幅下降约20bp",
         "url": "https://www.chinamoney.com.cn/lpr-2025",
         "title": "中国货币网·LPR公告", "tier": 5,
         "source_type": "regulatory", "published_at": "2025-06-20"},
        # —— 龙头业绩交叉验证 ——
        {"content": "奇富科技2025Q1在贷余额约1,800亿元，同比+12%，净利润约18亿元，take rate基本持平",
         "url": "https://ir.qifu.tech/q1-2025", "title": "奇富科技IR", "tier": 3,
         "source_type": "financial_api", "published_at": "2025-05-20"},
        {"content": "乐信2025Q1放款量同比增长约8%，净利润同比基本持平，90天+逾期率约2.4%，环比小幅上升",
         "url": "https://ir.lexinfintech.com/q1-2025", "title": "乐信IR", "tier": 3,
         "source_type": "financial_api", "published_at": "2025-05-21"},
        {"content": "信也科技2025Q1国际业务收入占比提升至约35%，国内业务增速放缓至约5%，整体逾期率约2.1%",
         "url": "https://ir.ppdfai.com/q1-2025", "title": "信也科技IR", "tier": 3,
         "source_type": "financial_api", "published_at": "2025-05-22"},
    ]


def _industry_evidence_rare_earth(query: str) -> list[dict]:
    """稀土行业 demo 证据。"""
    return [
        {"content": "工信部2024年稀土开采总量控制指标27万吨，同比增长5.9%；冶炼分离指标25.4万吨，同比增长4.2%",
         "url": "https://www.miit.gov.cn/reearth-quota-2024",
         "title": "工信部稀土配额", "tier": 5,
         "source_type": "regulatory", "published_at": "2024-08-20"},
        {"content": "2024年氧化镨钕均价约38万元/吨，较2023年下跌约34%，下游磁材需求增速放缓",
         "url": "https://www.100ppi.com/reearth-price-2024",
         "title": "生意社稀土价格", "tier": 6,
         "source_type": "news", "published_at": "2025-01-15"},
        {"content": "北方稀土2024年营收约230亿元，同比下降约15%，净利润下降约40%，主要受价格下行影响",
         "url": "https://www.cninfo.com.cn/northree-2024",
         "title": "北方稀土年报", "tier": 1,
         "source_type": "filing", "as_of": "2024-12-31"},
    ]


def _industry_evidence_generic(query: str) -> list[dict]:
    """通用行业兜底证据。"""
    return [
        {"content": f"据行业研究，{query}整体规模保持增长，但增速有所放缓，行业进入结构调整期",
         "url": "https://example.com/industry-overview",
         "title": "行业概览", "tier": 6,
         "source_type": "news", "published_at": "2025-05-01"},
        {"content": f"监管政策趋严，{query}准入门槛提高，中小企业面临出清压力",
         "url": "https://example.com/industry-regulation",
         "title": "政策动态", "tier": 6,
         "source_type": "news", "published_at": "2025-04-15"},
        {"content": f"{query}头部企业市场份额提升，集中度进一步向龙头集中",
         "url": "https://example.com/industry-landscape",
         "title": "竞争格局", "tier": 6,
         "source_type": "news", "published_at": "2025-04-20"},
    ]


def _company_evidence_qfin() -> list[dict]:
    """奇富科技公司级 demo 证据（原有数据）。"""
    return [
        {"content": "奇富科技2025Q1在贷余额约1,800亿元，同比+12%，take rate基本持平",
         "url": "https://ir.qifu.tech/q1-2025", "title": "Qifu IR", "tier": 3,
         "source_type": "financial_api", "published_at": "2025-05-20"},
        {"content": "2025Q1净利润约18亿元，同比增长，由放款规模扩张贡献",
         "url": "https://www.sec.gov/qfin-6k", "title": "SEC 6-K", "tier": 1,
         "source_type": "filing", "as_of": "2025-03-31"},
        {"content": "90天+逾期率环比降至2.0%附近，但季度核销规模同比上升",
         "url": "https://www.reuters.com/qifu-q1-2025", "title": "Reuters", "tier": 6,
         "source_type": "news", "published_at": "2025-05-22"},
        {"content": "管理层称银行等持牌机构资金占比进一步提升，资金结构优化",
         "url": "https://ir.qifu.tech/transcript-q1-2025", "title": "Earnings Call", "tier": 2,
         "source_type": "notice", "published_at": "2025-05-20"},
        {"content": "拨备覆盖率季度环比上升，利润增长非依赖少提拨备",
         "url": "https://www.sec.gov/qfin-provision", "title": "拨备数据", "tier": 1,
         "source_type": "filing", "as_of": "2025-03-31"},
        {"content": "助贷行业综合息费上限监管趋严，定价空间收窄",
         "url": "https://www.yicai.com/lending-reg-2025", "title": "第一财经", "tier": 6,
         "source_type": "news", "published_at": "2025-04-15"},
    ]


# ======================================================================
# 行业分析 demo 数据（按行业类型返回行业级洞察/证伪/叙事）
# ======================================================================

def _industry_search_plan(industry: str) -> list[str]:
    if industry == "credit":
        return [
            "中国消费信贷行业 2025 规模 增速 央行",
            "助贷行业 市场规模 竞争格局 集中度 2025",
            "消费金融 不良率 逾期率 趋势 2025",
            "互联网消费信贷 监管 利率上限 24% 2025",
            "LPR 助贷 资金成本 2025 趋势",
            "奇富科技 乐信 信也科技 2025Q1 财报 业绩",
        ]
    if industry == "rare_earth":
        return [
            "稀土 2024 开采总量控制指标 工信部",
            "氧化镨钕 价格走势 2024 2025",
            "稀土 下游需求 磁材 新能源 2025",
            "北方稀土 中国稀土 2024 年报 业绩",
            "稀土 出口 配额 政策 2025",
        ]
    return [
        "行业规模 增速 2025 趋势",
        "竞争格局 集中度 龙头 2025",
        "监管政策 行业 2025 最新",
        "行业 龙头企业 业绩 2025",
    ]


def _industry_insights(industry: str) -> dict:
    if industry == "credit":
        return {"insights": [
            {"section": "行业规模与增速", "claim": "消费信贷行业增速放缓至6.5%，助贷市场增速回落至8%-10%，行业进入存量竞争阶段",
             "reasoning": "央行数据显示消费贷款余额约58万亿同比+6.5%，互金协会报告助贷规模约3.5万亿增速8%-10%，均较上年放缓。增量主要来自场景金融与小微经营贷，传统现金贷增速已接近零。增速下行的结构性原因是居民杠杆率已达62%（BIS口径），可支配收入增速5%-6%，增量空间收窄。以助贷市场3.5万亿基数计，增速从15%降至8%意味着年度增量减少约2,500亿。",
             "falsifiable_condition": "",
             "evidence_ids": [1, 2], "confidence": 0.68},
            {"section": "行业规模与增速", "claim": "增速放缓是结构性的而非周期性的——2025年5月增速6.5%与Q1持平，消费已有温和复苏但增速未反弹",
             "reasoning": "如果是周期性放缓，增速应在消费复苏后回升。但2025年5月增速6.5%与Q1持平，消费已有温和复苏迹象——增速却没有反弹，说明制约因素不是需求波动而是杠杆率天花板。这与日本1990年代后期的经验一致：杠杆率见顶后，消费信贷增速中枢永久性下移。",
             "falsifiable_condition": "",
             "evidence_ids": [1, 14], "confidence": 0.55},
            {"section": "竞争格局与集中度", "claim": "监管趋严加速行业出清，头部助贷平台集中度CR5约60%进一步提升",
             "reasoning": "2024年约200余家小型助贷机构退出市场，头部平台份额提升。三层格局中银行占约65%、持牌消金约12%、助贷约10%。出清主要受监管合规门槛提高驱动。",
             "falsifiable_condition": "",
             "evidence_ids": [3, 4], "confidence": 0.6},
            {"section": "竞争格局与集中度", "claim": "CR5约60%的'集中度提升'叙事需要打折——统计口径不明，跨年比较存在偏差",
             "reasoning": "CR5是放款量还是余额？是否包含银行助贷业务？若口径包含银行，CR5可能降至40%以下；若仅含纯助贷平台余额，CR5可能高达70%+。口径差异足以改变结论方向。此外，机构退出的归因混杂——监管合规门槛提高和经济周期下行导致的自然淘汰无法分离。",
             "falsifiable_condition": "",
             "evidence_ids": [3, 17, 21], "confidence": 0.5},
            {"section": "资产质量趋势", "claim": "行业资产质量整体承压，持牌消金不良率约2.3%连续3季度上升，反映2022-2023年扩张期发放的次级贷款正进入风险暴露期",
             "reasoning": "央行数据显示商业银行不良率1.62%基本持平，但持牌消金不良率从2.1%连续升至2.3%。这是典型的'扩张期后遗症'——2022-2023年行业快速扩张时发放的次级贷款，经过12-18个月的风险暴露期，开始以不良率上升的形式体现。若这一趋势持续，2.5%可能是触发监管收紧的阈值。",
             "falsifiable_condition": "",
             "evidence_ids": [5], "confidence": 0.58},
            {"section": "资产质量趋势", "claim": "头部助贷逾期率更低更可能是客群分层而非风控技术差异",
             "reasoning": "头部助贷逾期率（奇富2.0%、信也2.1%）低于持牌消金平均2.3%，表面看是风控优势。但助贷平台聚焦优质次级客群（FICO等价分650+），持牌消金下沉更深（600-650）。逾期率差异完全可以由客群差异解释，不需要引入'风控技术差异'这一额外假设。乐信逾期率2.4%偏高也印证了客群定位对逾期率的影响大于风控技术。验证需要同客群条件下不同平台的vintage滚动率数据。",
             "falsifiable_condition": "",
             "evidence_ids": [5, 9, 10, 11], "confidence": 0.55},
            {"section": "监管政策走向", "claim": "利率上限24%将重构助贷商业模式——但影响是分化的：轻资本模式受益，重资本模式承压",
             "reasoning": "年化定价超24%的资产约占助贷存量15%-20%（约5,000-7,000亿），这些资产要么压降、要么重构定价结构。但头部平台已提前布局轻资本转型，科技服务费收入占比提升至30%+——科技服务费不受24%利率上限约束。这意味着轻资本模式不仅不受冲击，反而可能因竞争对手退出而受益。中小平台缺乏技术能力，面临实质性退出压力。",
             "falsifiable_condition": "",
             "evidence_ids": [6, 13], "confidence": 0.55},
            {"section": "资金成本与息差", "claim": "资金成本下降20bp被定价上限对冲——净息差方向取决于业务结构",
             "reasoning": "LPR维持3.45%不变，助贷综合资金成本5%-7%较2024年下降约20bp。但资产端定价空间收窄的幅度（15%-20%存量资产受24%上限影响）远大于资金成本改善的幅度（约20bp）。简单算术：20bp的资金成本改善 vs 15%-20%资产需要重新定价，后者的收入影响是前者的数十倍。轻资本模式（科技服务费不受上限约束）→资金成本下降+定价不变→息差改善；重资本模式（自营放款受24%约束）→资金成本下降20bp但定价端可能压缩200bp+→息差收窄。",
             "falsifiable_condition": "",
             "evidence_ids": [8, 15], "confidence": 0.52},
            {"section": "行业规模与增速", "claim": "轻资本转型是所有头部平台的共同方向，但路径分化：奇富靠科技服务费、信也靠国际化、乐信靠场景深耕",
             "reasoning": "面对增速见顶和利率上限双重压力，头部平台均在向轻资本转型，但路径不同。奇富科技科技服务费占比已提升至30%+，靠技术输出变现；信也科技国际业务收入占比35%且增速远超国内，靠地理分散对冲；乐信逾期率偏高2.4%，靠消费场景深耕维持差异化。三种路径的风险收益特征完全不同：科技服务费稳定性最高但增速最低，国际化增速最高但面临地缘风险，场景深耕护城河最深但资产质量压力最大。",
             "falsifiable_condition": "",
             "evidence_ids": [9, 10, 11], "confidence": 0.5},
        ]}
    if industry == "rare_earth":
        return {"insights": [
            {"section": "开采与冶炼总量", "claim": "2024年稀土开采总量控制指标27万吨，同比增长5.9%，增速较2023年明显放缓",
             "reasoning": "工信部数据显示2024年开采指标27万吨同比+5.9%（2023年+21.4%），冶炼分离指标25.4万吨同比+4.2%。增速放缓反映供需平衡考量。",
             "falsifiable_condition": "",
             "evidence_ids": [1], "confidence": 0.65},
            {"section": "价格走势", "claim": "氧化镨钕均价较2023年下跌约34%，下游磁材需求增速放缓是主因",
             "reasoning": "2024年氧化镨钕均价约38万元/吨，较2023年57.7万元/吨下跌约34%。价格下行主要受下游需求疲软和供给增加双重影响。",
             "falsifiable_condition": "",
             "evidence_ids": [2], "confidence": 0.6},
            {"section": "龙头业绩", "claim": "北方稀土2024年净利润下降约40%，主要受价格下行拖累",
             "reasoning": "北方稀土年报显示营收约230亿元同比-15%，净利润下降约40%。业绩下滑幅度超过价格跌幅，反映量价齐跌和成本刚性。",
             "falsifiable_condition": "",
             "evidence_ids": [3], "confidence": 0.62},
        ]}
    # generic
    return {"insights": [
        {"section": "行业规模与增速", "claim": "行业整体规模保持增长，但增速有所放缓，进入结构调整期",
         "reasoning": "行业研究显示规模保持增长但增速放缓，结构调整加速。增量主要来自新兴细分领域。",
         "falsifiable_condition": "",
         "evidence_ids": [1], "confidence": 0.55},
        {"section": "竞争格局与集中度", "claim": "监管趋严加速中小企业出清，头部企业市场份额提升",
         "reasoning": "准入门槛提高，中小企业面临合规压力退出市场，龙头份额进一步集中。",
         "falsifiable_condition": "",
         "evidence_ids": [2, 3], "confidence": 0.5},
    ]}


def _industry_red_team(text: str, industry: str) -> dict:
    """行业洞察的九维挑战 demo。"""
    if industry == "credit":
        return _credit_red_team(text)
    if industry == "rare_earth":
        return _rare_earth_red_team(text)
    return _generic_industry_red_team(text)


def _credit_red_team(text: str) -> dict:
    if "增速" in text and "规模" in text:
        return {
            "challenges": [
                {"dimension": "temporal", "severity": "low", "challenge": "央行数据为2025Q1，时效尚可，但月度数据可能已更新", "search_query": "央行 消费信贷 余额 2025 最新月度"},
                {"dimension": "conflict_of_interest", "severity": "medium", "challenge": "助贷市场规模数据来自互金协会报告，协会有行业利益倾向，可能高估规模", "search_query": "互金协会 助贷规模 口径 第三方核实"},
                {"dimension": "source_reliability", "severity": "low", "challenge": "央行信贷收支表为高权威来源，但助贷市场3.5万亿规模来自行业报告，权威性较低", "search_query": ""},
                {"dimension": "logic", "severity": "low", "challenge": "推理链条清晰：央行+协会数据均指向增速放缓", "search_query": ""},
                {"dimension": "external_consistency", "severity": "medium", "challenge": "不同来源对消费信贷口径不一致（是否含房贷、信用卡分期等），需确认口径可比性", "search_query": "消费信贷 口径 定义 央行 统计范围"},
                {"dimension": "boundary", "severity": "low", "challenge": "结论适用于整体行业，但细分场景（如场景金融、小微贷）增速可能不同", "search_query": ""},
                {"dimension": "alternative", "severity": "medium", "challenge": "替代解释：增速放缓可能是短期季节性波动而非趋势性变化", "search_query": "消费信贷 增速 季节性 2024 2025"},
                {"dimension": "missing_evidence", "severity": "low", "challenge": "缺少分场景的细分增速数据，无法确认增量结构", "search_query": "消费信贷 场景金融 小微贷 增速 2025"},
                {"dimension": "independence", "severity": "low", "challenge": "央行数据和互金协会数据来源独立，互为佐证", "search_query": ""},
            ],
            "overall": "solid",
            "overall_note": "核心维度均为low/medium，央行高权威数据支撑主结论，口径差异需注意但不影响方向判断",
            "search_queries": ["央行 消费信贷 余额 2025 最新月度", "消费信贷 口径 定义 央行 统计范围"],
        }
    if "集中度" in text or "出清" in text or "格局" in text:
        return {
            "challenges": [
                {"dimension": "temporal", "severity": "low", "challenge": "行业出清数据为2024年全年，需确认2025年趋势是否延续", "search_query": "助贷机构 退出 数量 2025"},
                {"dimension": "conflict_of_interest", "severity": "medium", "challenge": "集中度CR5数据来源为媒体报道，可能有头部平台公关倾向", "search_query": "助贷 CR5 集中度 第三方 研究"},
                {"dimension": "source_reliability", "severity": "medium", "challenge": "200余家机构退出的数据来自媒体，非监管原始数据", "search_query": "金融监管 助贷 机构 退出 名单 2024"},
                {"dimension": "logic", "severity": "medium", "challenge": "集中度提升的推理假设'退出机构份额转移至头部'，但实际可能转移至银行或持牌消金", "search_query": "助贷 退出 份额 转移 银行 持牌"},
                {"dimension": "external_consistency", "severity": "low", "challenge": "行业出清趋势与其他监管趋严行业的表现一致", "search_query": ""},
                {"dimension": "boundary", "severity": "low", "challenge": "CR5约60%的结论适用于助贷平台细分，不含银行和持牌消金", "search_query": ""},
                {"dimension": "alternative", "severity": "medium", "challenge": "替代解释：机构退出可能是经济周期下行导致而非监管趋严", "search_query": "助贷 退出 原因 经济周期 监管 2024"},
                {"dimension": "missing_evidence", "severity": "medium", "challenge": "缺少CR5具体名单和各家份额的定量数据", "search_query": "助贷平台 市场份额 排名 2024 2025"},
                {"dimension": "independence", "severity": "low", "challenge": "格局数据和出清数据来源不同，互为佐证", "search_query": ""},
            ],
            "overall": "incomplete",
            "overall_note": "集中度数据来源单一且口径可能不一致，替代理论（经济周期）未被排除，判定为incomplete",
            "search_queries": ["助贷 CR5 集中度 第三方 研究", "助贷 退出 原因 经济周期 监管 2024"],
        }
    if "不良" in text or "逾期" in text or "资产质量" in text:
        return {
            "challenges": [
                {"dimension": "temporal", "severity": "low", "challenge": "不良率数据为2025Q1，时效尚可", "search_query": ""},
                {"dimension": "conflict_of_interest", "severity": "medium", "challenge": "头部助贷平台逾期率数据来自公司自报IR，存在口径选择倾向", "search_query": "奇富科技 乐信 逾期率 口径 第三方"},
                {"dimension": "source_reliability", "severity": "low", "challenge": "央行不良率统计为高权威来源", "search_query": ""},
                {"dimension": "logic", "severity": "medium", "challenge": "推理假设'头部逾期率低=风控优势'，但可能受客群结构差异影响而非风控技术", "search_query": "助贷 客群 风控 逾期率 对比"},
                {"dimension": "external_consistency", "severity": "low", "challenge": "持牌消金不良率上升与宏观经济承压趋势一致", "search_query": ""},
                {"dimension": "boundary", "severity": "medium", "challenge": "头部平台风控优势结论基于单季度数据，经济下行期可能反转", "search_query": ""},
                {"dimension": "alternative", "severity": "medium", "challenge": "替代解释：头部平台逾期率低是因为客群更优质（选择效应），而非风控技术更强", "search_query": "助贷 客群质量 风控技术 逾期率 分解"},
                {"dimension": "missing_evidence", "severity": "medium", "challenge": "缺少同客群条件下不同平台逾期率对比数据", "search_query": "助贷 同客群 逾期率 对比 风控"},
                {"dimension": "independence", "severity": "low", "challenge": "央行数据和公司IR数据来源独立", "search_query": ""},
            ],
            "overall": "incomplete",
            "overall_note": "逻辑与替代理论维度为medium，风控优势vs客群选择的因果未完全分离，判定为incomplete",
            "search_queries": ["助贷 客群 风控 逾期率 对比", "助贷 同客群 逾期率 对比 风控"],
        }
    if "利率" in text or "监管" in text or "定价" in text:
        return {
            "challenges": [
                {"dimension": "temporal", "severity": "low", "challenge": "征求意见稿为2025年4月发布，时效好", "search_query": ""},
                {"dimension": "conflict_of_interest", "severity": "none", "challenge": "监管文件为中立来源", "search_query": ""},
                {"dimension": "source_reliability", "severity": "none", "challenge": "金融监管总局为最高权威来源", "search_query": ""},
                {"dimension": "logic", "severity": "medium", "challenge": "'压缩高定价资产空间'推理成立，但'定价能力分化'需要更多支撑", "search_query": "助贷 定价 分化 24% 上限 影响"},
                {"dimension": "external_consistency", "severity": "none", "challenge": "与此前民间借贷利率上限司法保护规定方向一致", "search_query": ""},
                {"dimension": "boundary", "severity": "medium", "challenge": "结论基于征求意见稿，正式稿可能调整（上限比例、过渡期等）", "search_query": "互联网消费信贷 管理办法 正式稿 2025"},
                {"dimension": "alternative", "severity": "low", "challenge": "无明显替代解释", "search_query": ""},
                {"dimension": "missing_evidence", "severity": "medium", "challenge": "缺少各平台年化定价超24%资产占比数据", "search_query": "助贷 年化利率 24% 资产 占比"},
                {"dimension": "independence", "severity": "none", "challenge": "监管文件为独立来源", "search_query": ""},
            ],
            "overall": "solid",
            "overall_note": "核心维度无high，监管文件权威性高，主要不确定性在正式稿最终口径",
            "search_queries": ["互联网消费信贷 管理办法 正式稿 2025", "助贷 年化利率 24% 资产 占比"],
        }
    # 默认（资金成本相关）
    return {
        "challenges": [
            {"dimension": "temporal", "severity": "low", "challenge": "LPR数据为2025年上半年，时效好", "search_query": ""},
            {"dimension": "conflict_of_interest", "severity": "low", "challenge": "LPR为央行公告，无利益倾向", "search_query": ""},
            {"dimension": "source_reliability", "severity": "medium", "challenge": "助贷综合资金成本5%-7%为估算值，非原始数据", "search_query": "助贷 综合资金成本 数据 来源"},
            {"dimension": "logic", "severity": "medium", "challenge": "推理假设'资金成本下降+定价上限=息差承压'，但实际息差取决于业务结构", "search_query": "助贷 息差 take rate 资金成本 2025"},
            {"dimension": "external_consistency", "severity": "low", "challenge": "LPR不变与其他宏观经济数据一致", "search_query": ""},
            {"dimension": "boundary", "severity": "low", "challenge": "结论适用于助贷平台整体，不同资金结构平台差异大", "search_query": ""},
            {"dimension": "alternative", "severity": "low", "challenge": "无明显替代解释", "search_query": ""},
            {"dimension": "missing_evidence", "severity": "high", "challenge": "缺少各平台资金成本时序数据，仅有定性估算", "search_query": "助贷平台 资金成本 季度 趋势 2024 2025"},
            {"dimension": "independence", "severity": "low", "challenge": "LPR数据独立，但资金成本估算来源单一", "search_query": ""},
        ],
        "overall": "incomplete",
        "overall_note": "缺失证据维度为high，资金成本定量数据不足，判定为incomplete",
        "search_queries": ["助贷 综合资金成本 数据 来源", "助贷平台 资金成本 季度 趋势 2024 2025"],
    }


def _rare_earth_red_team(text: str) -> dict:
    return {
        "challenges": [
            {"dimension": "temporal", "severity": "low", "challenge": "配额数据为2024年全年，2025年配额尚未发布", "search_query": "稀土 开采配额 2025 工信部"},
            {"dimension": "conflict_of_interest", "severity": "none", "challenge": "工信部配额为中立监管来源", "search_query": ""},
            {"dimension": "source_reliability", "severity": "low", "challenge": "价格数据来自生意社，权威性中等", "search_query": "稀土价格 上海有色 安泰科 2024"},
            {"dimension": "logic", "severity": "low", "challenge": "推理链条清晰", "search_query": ""},
            {"dimension": "external_consistency", "severity": "low", "challenge": "与行业已知供需趋势一致", "search_query": ""},
            {"dimension": "boundary", "severity": "low", "challenge": "结论适用范围明确", "search_query": ""},
            {"dimension": "alternative", "severity": "medium", "challenge": "价格下跌可能部分因海外供给增加而非仅需求疲软", "search_query": "稀土 海外供给 缅甸 美国 2024"},
            {"dimension": "missing_evidence", "severity": "medium", "challenge": "缺少分品种供需平衡表", "search_query": "稀土 供需平衡 氧化镨钕 氧化镝 2024"},
            {"dimension": "independence", "severity": "low", "challenge": "配额和价格数据来源独立", "search_query": ""},
        ],
        "overall": "incomplete",
        "overall_note": "替代理论与缺失证据维度为medium，海外供给因素未充分覆盖，判定为incomplete",
        "search_queries": ["稀土 海外供给 缅甸 美国 2024", "稀土 供需平衡 氧化镨钕 氧化镝 2024"],
    }


def _generic_industry_red_team(text: str) -> dict:
    return {
        "challenges": [
            {"dimension": "temporal", "severity": "low", "challenge": "数据时效尚可", "search_query": ""},
            {"dimension": "conflict_of_interest", "severity": "low", "challenge": "数据来源基本中立", "search_query": ""},
            {"dimension": "source_reliability", "severity": "medium", "challenge": "部分数据来自行业报告，权威性待核实", "search_query": ""},
            {"dimension": "logic", "severity": "low", "challenge": "推理链条基本清晰", "search_query": ""},
            {"dimension": "external_consistency", "severity": "low", "challenge": "与行业已知趋势一致", "search_query": ""},
            {"dimension": "boundary", "severity": "low", "challenge": "结论适用范围明确", "search_query": ""},
            {"dimension": "alternative", "severity": "medium", "challenge": "行业出清可能受多重因素影响，监管仅为其一", "search_query": ""},
            {"dimension": "missing_evidence", "severity": "medium", "challenge": "缺少定量集中度数据", "search_query": ""},
            {"dimension": "independence", "severity": "low", "challenge": "来源基本独立", "search_query": ""},
        ],
        "overall": "incomplete",
        "overall_note": "替代理论与缺失证据维度为medium，判定为incomplete",
        "search_queries": [],
    }


def _industry_support_queries(text: str, industry: str) -> dict:
    if industry == "credit":
        if "增速" in text or "规模" in text:
            return {"support_queries": ["消费信贷 增速 2025 最新月度 央行", "助贷 规模 增速 2025 互金协会"]}
        if "集中度" in text or "出清" in text:
            return {"support_queries": ["助贷 CR5 市场份额 2025 研究", "助贷 机构 退出 2024 2025 监管"]}
        if "不良" in text or "逾期" in text:
            return {"support_queries": ["持牌消金 不良率 2025 趋势", "助贷 风控 逾期率 对比 2025"]}
        return {"support_queries": ["助贷 资金成本 2025 趋势", "LPR 助贷 息差 2025"]}
    return {"support_queries": ["行业 龙头 业绩 2025 交叉验证", "行业 政策 趋势 2025 最新"]}


def _industry_debate_queries(text: str, industry: str) -> dict:
    if industry == "credit":
        return {"debate_queries": ["助贷 集中度 CR5 提升 2025 第三方", "助贷 风控优势 客群选择 逾期率 2025"]}
    return {"debate_queries": ["行业 集中度 提升 第三方研究 2025", "行业 出清 监管 经济周期 2025"]}


def _industry_debate_verdict(industry: str) -> dict:
    return {"verdict": "questionable", "confidence": 0.5,
            "note": "辩论中分析师提供了部分反驳证据，但红队挑战仍未完全化解。",
            "resolved_dimensions": ["missing_evidence"],
            "open_dimensions": ["temporal", "alternative"],
            "gap_explanation": "关键定量数据仍有缺口，维持存疑但标注辩论结果。"}


def _industry_reverdict(text: str, sys_text: str, industry: str) -> dict:
    is_refine = "自我迭代二次裁决" in sys_text
    if industry == "credit":
        if "增速" in text and "规模" in text:
            return {"verdict": "supported", "confidence": 0.72,
                    "note": "央行高权威数据支撑增速放缓结论，口径差异不影响方向判断。",
                    "resolved_dimensions": ["source_reliability", "external_consistency"],
                    "open_dimensions": [],
                    "gap_explanation": ""}
        if "集中度" in text or "出清" in text:
            return {"verdict": "questionable",
                    "confidence": 0.45 if not is_refine else 0.5,
                    "note": "集中度数据来源单一，经济周期替代理论未完全排除。"
                            + ("自我迭代补充CR5研究数据但仍未完全排除替代解释。" if is_refine else ""),
                    "resolved_dimensions": ["missing_evidence"] if is_refine else [],
                    "open_dimensions": ["alternative", "source_reliability"],
                    "gap_explanation": "CR5定量数据口径不一致，经济周期vs监管趋严的归因无法完全分离。"}
        if "不良" in text or "逾期" in text:
            return {"verdict": "questionable",
                    "confidence": 0.42 if not is_refine else 0.48,
                    "note": "风控优势vs客群选择的因果未完全分离。"
                            + ("自我迭代补充同客群对比数据但样本有限。" if is_refine else ""),
                    "resolved_dimensions": ["logic"] if is_refine else [],
                    "open_dimensions": ["alternative", "missing_evidence"],
                    "gap_explanation": "缺少同客群条件下不同平台风控效果对比数据。"}
        if "利率" in text or "监管" in text:
            return {"verdict": "supported", "confidence": 0.7,
                    "note": "监管文件原文可查，权威性高，利率上限24%结论成立。",
                    "resolved_dimensions": ["source_reliability", "boundary"],
                    "open_dimensions": [],
                    "gap_explanation": ""}
        return {"verdict": "questionable",
                "confidence": 0.48 if not is_refine else 0.52,
                "note": "资金成本定量数据不足。"
                        + ("自我迭代补充资金成本趋势数据但仍不充分。" if is_refine else ""),
                "resolved_dimensions": ["conflict_of_interest"] if is_refine else [],
                "open_dimensions": ["missing_evidence"],
                "gap_explanation": "各平台资金成本时序数据缺失。"}
    if industry == "rare_earth":
        return {"verdict": "questionable", "confidence": 0.5,
                "note": "海外供给因素未充分覆盖。" + ("自我迭代补充供需平衡数据但仍不完整。" if is_refine else ""),
                "resolved_dimensions": ["source_reliability"] if is_refine else [],
                "open_dimensions": ["alternative", "missing_evidence"],
                "gap_explanation": "缺少分品种供需平衡表。"}
    return {"verdict": "questionable", "confidence": 0.48,
            "note": "数据来源有限。" + ("自我迭代后仍不充分。" if is_refine else ""),
            "resolved_dimensions": [], "open_dimensions": ["missing_evidence"],
            "gap_explanation": "定量数据不足。"}


def _industry_facts(industry: str) -> str:
    if industry == "credit":
        return """## 执行摘要

市场普遍将消费信贷58万亿余额同比+6.5%解读为"稳健增长"，但我们认为这是增速中枢永久性下移的信号。居民杠杆率已达62%（BIS口径），可支配收入增速5%-6%——这不是周期性波动后会被消费复苏逆转的放缓，而是杠杆率见顶导致的结构性转折。2025年5月增速6.5%与Q1持平，消费已有温和复苏迹象但增速未反弹，佐证了这一判断。

监管端24%利率上限征求意见稿将进一步重塑格局。年化定价超24%的资产约占助贷存量15%-20%（约5,000-7,000亿），但影响是分化的：头部平台已布局轻资本转型（科技服务费占比30%+，不受24%约束），反而可能受益于竞争对手退出；中小平台缺乏技术能力，面临实质性退出压力。行业出清加速（200余家小机构退出），但"CR5约60%"的数字需要打折——统计口径不明（放款量还是余额？是否含银行助贷？），跨年比较存在偏差。

资产质量分化是更值得深究的信号。持牌消金不良率从2.1%连续三个季度升至2.3%，反映2022-2023年扩张期发放的次级贷款正进入风险暴露期。头部助贷逾期率更低（2.0%-2.1%），市场解读为"风控优势"，但这更可能是客群分层的结果——助贷平台聚焦650+客群，持牌消金下沉至600-650。缺少同客群条件下的vintage对比，当前无法区分"风控好"和"客群好"。这个区分至关重要：前者意味着头部平台可以维持低定价获取优质客群的正循环，后者意味着一旦客群下沉逾期率将快速恶化。

面对增速见顶和利率上限双重压力，头部平台均在向轻资本转型，但路径分化：奇富靠科技服务费、信也靠国际化（国际收入占比35%）、乐信靠场景深耕。三种路径的风险收益特征完全不同。

| 维度 | 关键数据 | 判断 |
|---|---|---|
| 行业规模 | 消费信贷余额约58万亿，同比+6.5%（2023年8.3%） | 增速中枢下移 |
| 竞争格局 | 银行65%/持牌消金12%/助贷10%，CR5约60%（口径待核实） | 出清加速但口径存疑 |
| 资产质量 | 持牌消金不良率2.3%（+10bp连续3季度上升），头部助贷2.0%-2.1% | 分化但归因待定 |
| 监管政策 | 利率上限24%征求意见稿，一年过渡期，预计Q4正式稿 | 定价空间压缩15%-20% |
| 资金成本 | LPR 3.45%不变，助贷综合成本5%-7%（-20bp） | 改善但被定价上限对冲 |

## 行业格局

消费信贷市场三层体系的核心分化在于**资金成本与客群定位的错配**：银行资金成本最低（约2%-3%）但受资本充足率约束无法充分下沉；持牌消金资金成本中等（约4%-5%）但牌照允许更高风险偏好；助贷平台资金成本最高（5%-7%）但凭借风控技术和场景获客切入银行不愿触达的客群。这一生态平衡正被利率上限监管打破——24%上限压缩助贷定价空间，迫使头部平台向轻资本（科技服务费）转型，中小平台面临退出。

| 层级 | 份额 | 资金成本 | 客群定位 | 利率上限冲击 |
|---|---|---|---|---|
| 银行 | ~65% | 2%-3% | 优质客群（FICO等价650+） | 轻微（定价本就低于24%） |
| 持牌消金 | ~12% | 4%-5% | 次优客群（600-650） | 中等（部分产品接近24%） |
| 助贷平台 | ~10% | 5%-7% | 次级客群+场景金融 | 显著（15%-20%存量资产需重构） |

### 龙头业绩交叉验证

头部助贷平台2025Q1业绩呈现"量增质稳"特征，但增速分化揭示战略分野：

| 公司 | 在贷余额 | 增速 | 逾期率 | 战略方向 |
|---|---|---|---|---|
| 奇富科技 | ~1,800亿 | +12% | ~2.0% | 轻资本转型，科技服务费占比提升 |
| 乐信 | — | +8% | ~2.4% | 逾期率偏高，消费场景依赖度高 |
| 信也科技 | — | 国际+35%/国内+5% | ~2.1% | 国际化分散风险，国内增速接近见顶 |

上表显示头部平台增速分化明显，奇富凭借轻资本转型维持双位数增长，而乐信逾期率偏高反映消费场景依赖风险。

### 历史趋势

| 指标 | 2023 | 2024 | 2025Q1 | 趋势判断 |
|---|---|---|---|---|
| 消费信贷增速 | 8.3% | 6.7% | 6.5% | 中枢下移，5%-7%区间企稳 |
| 助贷市场增速 | 15%+ | 12% | 8%-10% | 接近见顶 |
| 持牌消金不良率 | 2.1% | 2.2% | 2.3% | 连续上升，风险暴露期 |
| LPR | 3.55% | 3.45% | 3.45% | 低位稳定 |

关键拐点信号：①利率上限正式稿若维持24%且无过渡期→5,000-7,000亿资产需重构；②消费信贷增速跌破5%→存量博弈白热化；③持牌消金不良率突破2.5%→触发监管收紧。"""
    if industry == "rare_earth":
        return """## 执行摘要

2024年稀土行业呈现"量增价跌利缩"的困境。开采配额27万吨（+5.9%）延续增长但增速骤降（2023年+21.4%），氧化镨钕均价38万元/吨（-34%）已逼近部分企业成本线。北方稀土净利润-40%的降幅远超价格跌幅，反映量价齐跌叠加成本刚性的三重压力。核心矛盾在于：配额增速放缓意在托底价格，但海外供给（缅甸+15%）对冲了国内控量效果，价格企稳仍需等待下游磁材需求实质性恢复。

| 维度 | 关键数据 | 判断 |
|---|---|---|
| 开采配额 | 27万吨，+5.9%（2023年+21.4%） | 增速骤降，意在托底 |
| 价格走势 | 氧化镨钕38万元/吨，-34% | 逼近成本线 |
| 龙头业绩 | 北方稀土净利润-40% | 量价齐跌+成本刚性 |

## 行业格局

稀土供给端受工信部配额严格控制（年增速5%-6%），但海外供给（缅甸、美国、澳洲）占比已从2020年的约15%升至2024年的约25%，削弱了国内控量的价格传导效力。需求端高度集中：钕铁硼磁材占稀土消费约40%，下游为新能源车（35%）、风电（25%）、消费电子（20%）。价格企稳的关键变量不是配额而是下游需求——若新能源车销量增速维持在20%+，磁材需求可在2025H2拉动价格反弹至45-50万元/吨区间。

| 项 | 内容 |
|---|---|
| 供给结构 | 国内配额控制（75%）+海外进口（25%，缅甸为主） |
| 需求结构 | 钕铁硼磁材40%，催化剂25%，抛光粉15%，其他20% |
| 龙头锚点 | 北方稀土（轻稀土）、中国稀土（中重稀土）、盛和资源 |"""
    return """## 执行摘要

1. 行业整体规模保持增长，但增速有所放缓，进入结构调整期。
2. 监管趋严加速中小企业出清，头部企业市场份额提升。

| 维度 | 关键数据 | 判断 |
|---|---|---|
| 行业规模 | 保持增长，增速放缓 | 结构调整 |
| 竞争格局 | 头部份额提升，中小企业退出 | 集中度提升 |

## 基本事实

| 项 | 内容 |
|---|---|
| 对象 | 该行业 |
| 商业模式 | 行业整体 |"""


def _industry_narrative(industry: str) -> str:
    """兼容旧入口：返回 core+outlook 合并。"""
    return _industry_narrative_core(industry) + "\n\n" + _industry_narrative_outlook(industry)


def _industry_narrative_core(industry: str) -> str:
    """洞察层 A：核心发现 + 历史趋势。"""
    if industry == "credit":
        return """## 核心发现

### 增速放缓是结构性的，不是周期性的

消费信贷余额58万亿同比+6.5%，较2023年的8.3%连续下行[^1][^16]。市场将其解读为"周期性放缓，消费复苏后将回升"，但我们认为这是**增速中枢的永久性下移**——居民杠杆率已达62%（BIS口径），可支配收入增速5%-6%，消费信贷的增量空间正在结构性收窄。助贷市场3.5万亿规模增速回落至8%-10%[^2]，增量主要来自场景金融（分期）和小微经营贷，传统现金贷增速已接近零。

**含义**：未来2-3年增速中枢5%-7%区间，靠放款量驱动利润的模型正在失效。以助贷市场3.5万亿基数计，增速从15%降至8%意味着年度增量减少约2,500亿——这约等于一家头部助贷平台的全年放款量。增速见顶后，竞争从"抢增量"转向"抢存量"，定价能力成为胜负手。

**反驳"周期性"论**：如果是周期性放缓，增速应在消费复苏后回升。但2025年5月增速6.5%与Q1持平[^16]，消费已有温和复苏迹象——增速却没有反弹，说明制约因素不是需求波动而是杠杆率天花板。 (置信度 39%·成立)

### 集中度"提升"的叙事需要打折

200余家小机构退出、CR5约60%的数据指向集中度提升[^3][^4][^17][^22]，但这组数据有两个致命问题。第一，**口径不明**：CR5是放款量还是余额？是否包含银行助贷业务？若以放款量计且包含银行，CR5可能降至40%以下；若仅含纯助贷平台余额，CR5可能高达70%+。口径差异足以改变结论方向。第二，**归因混杂**：机构退出确实在发生，但监管合规门槛提高和经济周期下行导致的自然淘汰无法分离——后者与"监管驱动的集中度提升"是完全不同的叙事。

**我们能确定的**：中小机构在退出，头部份额有提升迹象。**我们不能确定的**：提升幅度、提升驱动力的归因、以及CR5的可靠口径。建议在引用CR5时必须标注口径限定，否则该数据点不具备跨年可比性。 (置信度 38%·存疑)

### 资产质量分化是客群分层，不是风控差异

持牌消金不良率从2.1%连续三个季度升至2.3%[^5]，反映2022-2023年扩张期发放的次级贷款正进入风险暴露期。头部助贷逾期率更低（奇富2.0%、信也2.1%）[^9][^11]，市场解读为"头部平台风控技术优势"，但我们认为这更可能是**客群分层的结果**。

核心论据：助贷平台聚焦优质次级客群（FICO等价分650+），持牌消金下沉更深（600-650）。逾期率差异完全可以由客群差异解释，不需要引入"风控技术差异"这一额外假设。乐信逾期率2.4%偏高[^10]也印证了这一点——乐信消费场景依赖度高、客群略下沉，逾期率就偏高，说明客群定位对逾期率的影响大于风控技术。

**为什么这很重要**：如果"风控优势"成立→头部平台可以维持更低定价获取优质客群，形成正循环，估值应享溢价。如果是"客群选择效应"→逾期率低是因为客群本就优质，一旦客群质量下行（如为维持增速而下沉），逾期率将快速恶化。两种解释指向完全不同的投资逻辑。验证需要同客群条件下不同平台的vintage滚动率数据，当前公开数据不足以做出该判断——这是本报告最重要的未解问题。 (置信度 45%·成立·因果归因存疑)

### 利率上限24%将重构助贷商业模式——但影响是分化的

征求意见稿明确综合息费上限24%，设一年过渡期[^6][^13]。市场担忧"压缩所有平台利润"，但我们认为影响是**高度分化的**。

年化定价超24%的资产约占助贷存量的15%-20%（约5,000-7,000亿），这些资产要么压降、要么重构定价结构。但头部平台（奇富、信也）已提前布局轻资本转型，科技服务费收入占比提升至30%+——**科技服务费不受24%利率上限约束**，因为它是服务费而非利息。这意味着轻资本模式不仅不受冲击，反而可能因竞争对手退出而受益。中小平台缺乏技术能力，转型路径不清晰，面临实质性退出压力。

**正式稿的三个核心变量**：①上限比例——24%维持还是上调至28%？②过渡期——是否设1年允许存量自然到期？③适用范围——是否覆盖助贷撮合模式（若覆盖，轻资本模式也受冲击）？任一变量偏紧都将加速行业出清。预计Q4发布。 (置信度 36%·成立)

### 资金成本改善被定价上限对冲——净息差取决于业务结构

LPR维持3.45%不变，助贷综合资金成本5%-7%较2024年下降约20bp[^8][^15]。表面看是利好，但资产端定价空间收窄的幅度（15%-20%存量资产受24%上限影响）远大于资金成本改善的幅度（约20bp）。简单算术：20bp的资金成本改善 vs 15%-20%资产需要重新定价，后者的收入影响是前者的数十倍。

**净息差方向取决于业务结构**：轻资本模式（科技服务费，不受利率上限约束）→资金成本下降+定价不变→息差改善；重资本模式（自营放款，受24%约束）→资金成本下降20bp但定价端可能压缩200bp+→息差大概率收窄。这意味着同一个监管环境下，不同商业模式的盈利走势可能完全相反。 (置信度 37%·存疑)"""
    if industry == "rare_earth":
        return """## 核心发现

### 配额增速骤降意在托底价格，但效果被海外供给稀释

2024年开采配额27万吨（+5.9%），增速较2023年的21.4%骤降[^1]。这是工信部对价格下行的主动回应——通过控量减少供给压力。但效果被海外供给增加对冲：缅甸稀土进口量2024年同比+15%，海外供给占比已从2020年的约15%升至25%。**国内控量的价格传导效力正在减弱，配额不再是稀土价格的充分决定变量。** (置信度 50%·成立)

### 价格下跌34%已逼近成本线，但反弹需等待需求

氧化镨钕均价从2023年的57.7万元/吨跌至2024年的38万元/吨（-34%）[^2]，已接近部分中小企业的综合成本线（约35万元/吨）。价格继续下跌空间有限，但反弹需要下游磁材需求实质性恢复——当前新能源车增速放缓至20%（2023年为35%+），风电装机不及预期，钕铁硼磁材订单量同比下滑约10%。**价格更可能在35-42万元/吨区间震荡，趋势性反弹需等待2025H2新能源车产销旺季。** (置信度 40%·存疑)

### 龙头业绩承压幅度超预期，量价齐跌叠加成本刚性

北方稀土2024年营收-15%、净利润-40%[^3]，净利润降幅是价格跌幅（34%）的1.2倍，反映量价齐跌叠加成本刚性的三重压力。包钢稀土矿开采成本相对固定（折旧+人工+能源），产量下调后单位成本被摊薄效率下降。**若2025年价格维持在38万元/吨以下超过2个季度，部分高成本产能可能被动停产。** (置信度 42%·成立)

## 历史趋势与拐点

| 指标 | 2022 | 2023 | 2024 | 趋势判断 |
|---|---|---|---|---|
| 开采配额增速 | 25% | 21.4% | 5.9% | 骤降，控量意图明确 |
| 氧化镨钕价格 | 90万 | 57.7万 | 38万 | -58%，逼近成本线 |
| 北方稀土净利润 | — | — | -40% | 量价齐跌+成本刚性 |

关键拐点信号：①氧化镨钕跌破35万元/吨→高成本产能停产，供给收缩托底；②新能源车月销量增速回升至30%+→磁材需求拉动价格反弹至45-50万元/吨；③2025年配额增速回升至10%+→价格承压加剧。"""
    return """## 核心发现

1. **行业增速放缓但保持增长**：增量主要来自新兴细分领域，传统业务增速接近见顶。
2. **集中度提升**：准入门槛提高，中小企业退出，龙头份额集中。但归因（监管vs周期）需进一步分离。

行业正从高速增长期进入结构调整期。关键拐点信号：政策松紧变化、龙头业绩分化、下游需求恢复节奏。"""


def _industry_narrative_outlook(industry: str) -> str:
    """洞察层 B：风险 + 展望。"""
    if industry == "credit":
        return """## 风险与不确定性

**CR5集中度数据的口径风险**：60%的CR5来自财新报道引用的第三方研究，未披露统计口径。若以放款量计且包含银行助贷业务，CR5可能降至40%以下；若以在贷余额计且仅含纯助贷平台，CR5可能高达70%+。口径差异足以改变"集中度提升"的结论方向。建议引用时标注口径限定。

**风控优势vs客群选择效应**：头部助贷逾期率低（2.0%-2.1%）的归因存在根本性歧义。若为风控技术优势→头部平台可以维持更低定价获取优质客群，形成正循环；若为客群选择效应→逾期率低是因为客群本就优质，风控技术差异不大，一旦客群质量下行逾期率将快速恶化。缺少同客群条件下不同平台的vintage滚动率数据，当前无法区分。这是本报告最重要的未解问题。

**利率上限正式稿的三个不确定性**：①上限比例——24%维持还是上调至28%（LPR四倍约13.8%的司法保护线vs市场实践24%）？②过渡期——是否设1年过渡期允许存量资产自然到期？③适用范围——是否覆盖助贷撮合模式（若覆盖，轻资本模式也受冲击）？任一变量偏紧都将加速行业出清，预计Q4发布。

**持牌消金不良率趋势的系统性风险**：2.1%→2.2%→2.3%连续三个季度上升，若突破2.5%可能触发监管收紧（如限制新业务、提高拨备要求），进而导致行业整体放款量骤降。这一风险在当前分析中未充分定价。

## 展望与关注点

| 跟踪指标 | 当前值 | 关注阈值 | 触发后影响 |
|---|---|---|---|
| 利率上限正式稿 | 征求求意见稿(24%) | 正式稿≤24%且无过渡期 | 加速出清，头部份额跳升5-10pp |
| 消费信贷月度增速 | 6.5% | 跌破5% | 存量博弈白热化，价格战风险 |
| 持牌消金不良率 | 2.3% | 突破2.5% | 行业风控收紧，放款量骤降 |
| 奇富科技科技服务费占比 | ~30% | 超过40% | 轻资本转型成功，估值逻辑切换 |
| LPR | 3.45% | 下调至3.25% | 资金成本改善，但被定价上限对冲 |

**情景推演**：
- **基准情景（概率50%）**：利率上限维持24%设1年过渡期，消费信贷增速5%-6%，不良率稳定在2.3%-2.4%，头部平台轻资本转型推进。行业温和出清，龙头份额缓升。
- **乐观情景（概率20%）**：利率上限上调至28%或适用范围排除助贷撮合，消费复苏拉动增速回升至7%-8%。行业格局基本稳定，中小平台获得喘息。
- **悲观情景（概率30%）**：利率上限24%无过渡期，消费持续疲软增速跌破5%，不良率突破2.5%。行业剧烈出清，30%+中小机构退出，头部平台也面临存量资产质量压力。

---
*数据截至 {datetime.now().strftime('%Y-Q1')} · 数据源: 央行信贷收支表, 金融监管总局, 互金协会, 龙头公司IR/财报, 权威媒体*
*本报告基于公开信息，不构成投资建议。*
"""
    if industry == "rare_earth":
        return """## 风险与不确定性

**价格归因的遗漏变量**：将价格下跌34%主要归因于需求疲软可能低估了海外供给的冲击。缅甸稀土矿2024年进口量+15%，且缅甸矿以中重稀土为主、品位较高，对国内价格的压制效果可能比纯吨位数据更大。缺少分品种（轻稀土vs中重稀土）的供需平衡表，当前归因可能存在遗漏变量偏误。

**配额政策的反向风险**：2024年配额增速骤降至5.9%意在托底价格，但若2025年下游需求恢复而配额增速维持低位，可能出现供不应求→价格暴涨→下游磁材企业成本压力→需求反噬的负反馈循环。配额政策需要在"托底"和"不过度"之间平衡。

**北方稀土的成本刚性被低估**：净利润-40%远超价格-34%的跌幅，说明成本刚性（折旧+人工+能源+环保）的拖累被市场低估。若价格维持低位超过2个季度，高成本产能被动停产可能导致供给收缩，但也会冲击龙头企业的盈利能力。

## 展望与关注点

| 跟踪指标 | 当前值 | 关注阈值 | 触发后影响 |
|---|---|---|---|
| 2025年第一批配额增速 | 5.9%(2024) | 回升至10%+ | 供给增加，价格承压 |
| 氧化镨钕价格 | 38万元/吨 | 跌破35万 | 高成本产能停产，供给收缩 |
| 新能源车月销增速 | ~20% | 回升至30%+ | 磁材需求拉动价格反弹至45-50万 |
| 缅甸稀土进口量 | +15%(2024) | 持续+15%以上 | 海外供给继续压制国内价格 |

---
*数据截至 {datetime.now().year}-06-30 · 数据源: 工信部, 生意社, 上市公司年报*
*本报告基于公开信息，不构成投资建议。*
"""


def _industry_meta_reflection(industry: str) -> dict:
    if industry == "credit":
        return {
            "missed_challenges": [
                {"insight_claim": "头部助贷平台风控优势凸显",
                 "missed_angle": "未检查'风控优势'与'客群选择效应'的因果分离——逾期率低可能因客群优质而非风控技术强",
                 "why_missed": "策略未覆盖'选择性偏差'这一 alternative 维度内的特定角度",
                 "trigger_signal": "当 claim 比较不同主体绩效差异且未控制客群结构时，自动检查选择性偏差"}
            ],
            "policy_updates": [
                {"action": "add",
                 "policy": {
                     "trigger": "claim 比较不同主体绩效差异且未控制客群结构",
                     "challenge_type": "alternative",
                     "search_strategy": "搜索同客群条件下不同主体的绩效对比数据，检查选择性偏差",
                     "evidence_preference": ["filing", "regulatory"],
                     "priority": "medium"},
                 "reason": "风控优势分析中遗漏了客群选择效应检查，补充此策略"}
            ],
            "industry_pattern_updates": [],
            "overall_assessment": "本次行业分析覆盖九维挑战较全面，但 alternative 维度遗漏了选择性偏差检查，已新增策略覆盖。"
        }
    return {
        "missed_challenges": [
            {"insight_claim": "行业集中度提升",
             "missed_angle": "未充分检查行业出清的多因素归因（监管vs经济周期）",
             "why_missed": "策略未覆盖多因素归因分离",
             "trigger_signal": "当 claim 含'出清/集中度提升'时，自动检查替代归因"}
        ],
        "policy_updates": [
            {"action": "add",
             "policy": {
                 "trigger": "claim 含'出清/集中度提升'",
                 "challenge_type": "alternative",
                 "search_strategy": "搜索行业出清的多因素分析，检查监管vs经济周期归因",
                 "evidence_preference": ["regulatory", "news"],
                 "priority": "medium"},
             "reason": "集中度分析中遗漏了多因素归因分离"}
        ],
        "industry_pattern_updates": [],
        "overall_assessment": "本次分析覆盖较好，但 alternative 维度可加强多因素归因分离。"
    }


def _industry_data_extraction(industry: str) -> dict:
    if industry == "credit":
        return {
            "totals": [
                {"metric": "消费贷款余额", "unit": "万亿元",
                 "values": [
                     {"period": "2023", "value": 52.0, "growth": 8.3},
                     {"period": "2024", "value": 55.5, "growth": 6.7},
                     {"period": "2025Q1", "value": 58.0, "growth": 6.5},
                 ]},
                {"metric": "互联网助贷市场规模", "unit": "万亿元",
                 "values": [
                     {"period": "2023", "value": 3.0, "growth": 15.0},
                     {"period": "2024", "value": 3.5, "growth": 16.7},
                     {"period": "2025E", "value": 3.8, "growth": 8.6},
                 ]},
                {"metric": "持牌消金不良率", "unit": "%",
                 "values": [
                     {"period": "2023", "value": 2.1, "growth": 0.0},
                     {"period": "2024", "value": 2.2, "growth": 4.8},
                     {"period": "2025Q1", "value": 2.3, "growth": 4.5},
                 ]},
            ],
            "prices": [
                {"product": "1年期LPR", "unit": "%",
                 "points": [
                     {"period": "2023", "price": 3.55},
                     {"period": "2024", "price": 3.45},
                     {"period": "2025H1", "price": 3.45},
                 ]},
            ],
        }
    if industry == "rare_earth":
        return {
            "totals": [
                {"metric": "稀土开采总量控制指标", "unit": "吨",
                 "values": [
                     {"period": "2022", "value": 210000, "growth": 25.0},
                     {"period": "2023", "value": 255000, "growth": 21.4},
                     {"period": "2024", "value": 270000, "growth": 5.9},
                 ]},
                {"metric": "冶炼分离总量控制指标", "unit": "吨",
                 "values": [
                     {"period": "2022", "value": 202000, "growth": 24.3},
                     {"period": "2023", "value": 243850, "growth": 20.7},
                     {"period": "2024", "value": 254000, "growth": 4.2},
                 ]},
            ],
            "prices": [
                {"product": "氧化镨钕", "unit": "万元/吨",
                 "points": [
                     {"period": "2022", "price": 90.0},
                     {"period": "2023", "price": 57.7},
                     {"period": "2024H1", "price": 38.2},
                 ]},
                {"product": "氧化镝", "unit": "万元/吨",
                 "points": [
                     {"period": "2022", "price": 260.0},
                     {"period": "2023", "price": 215.0},
                     {"period": "2024H1", "price": 195.1},
                 ]},
            ],
        }
    return {"totals": [], "prices": []}
