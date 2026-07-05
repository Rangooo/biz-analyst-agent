"""Golden Answer Set 运行器 —— 验证 agent 分析的"正确性"，而非仅"能跑"。

与其他测试的区别：
- golden_regression: 流程机制（有没有降级/证伪/生成报告）—— 跑 demo 数据
- quality_benchmark: 流程质量（verdict 方向/结构完整）—— 跑 demo 数据
- golden_answers (本文件): **真实 LLM 输出的事实正确性 + 洞察深度 + 可证伪条件质量**

三类校验：
1. 事实校验集：required_facts 数值±容差匹配；forbidden_facts 防幻觉负向断言
2. 洞察方向集：expected_directions 语义覆盖（关键词 OR）；unexpected_directions 错误方向检测
3. 可证伪条件质量：规则检查——基于当前可查数据、无"若下季度/若明年"等未来假设

两种运行模式：
- 默认（离线自检）：不调用真实 LLM，验证 golden_data 自身一致性 + 比对器逻辑正确性。
  通过 make eval-offline / python -m evals.golden_answers --report 手动运行，零成本零网络。
- --live（真实验证）：对每个 query 真实跑一遍 agent，比对真实输出。需 API Key，耗时耗钱。
  改 prompt/框架后手动跑，量化"分析质量"是否回归。

用法：
  python -m evals.golden_answers            # 离线自检（默认）
  python -m evals.golden_answers --live     # 真实 LLM 验证（需 Key）
  python -m evals.golden_answers --live --case 0   # 只跑第 0 个案例
  python -m evals.golden_answers --report   # 离线自检 + 打印题库统计
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

try:
    from evals.golden_trend import record_trend, show_trend
except Exception:  # noqa: BLE001
    record_trend = None
    show_trend = None
import asyncio

from evals.golden_data import CASES as _BASE_CASES, GoldenCase, FactCheck

# 合并扩展案例（goldens/extension_cases.py）—— 覆盖港股新股/非上市跨境/海外BNPL/创新药/跨境电商
try:
    sys.path.insert(0, str(ROOT_DIR / "goldens"))
    from extension_cases import EXTENSION_CASES  # noqa: E402
    CASES: list[GoldenCase] = _BASE_CASES + EXTENSION_CASES
except ImportError:
    CASES = _BASE_CASES  # 扩展案例不可用时退回基础案例

_passed = 0
_failed = 0


def check(name: str, condition: bool, detail: str = ""):
    global _passed, _failed
    if condition:
        _passed += 1
        print(f"  ✅ {name}")
    else:
        _failed += 1
        print(f"  ❌ {name}" + (f" — {detail}" if detail else ""))


# ─────────────────────────────────────────────────────────────────────────
# 比对器（纯函数，无副作用，可独立单测）
# ─────────────────────────────────────────────────────────────────────────

# 未来假设词黑名单（可证伪条件不应依赖这些）
_FUTURE_TOKENS = [
    "若下季度", "若下一季度", "若明年", "若来年", "若后续季度", "若后续",
    "若未来", "若H2", "若下半年", "若2026", "若2027", "若2028", "若明",
    "下季度将", "明年将", "未来将公布", "将公布的", "后续将发布",
    "若次年", "若下一年", "等待未来", "若日后",
]
# 当前可查数据引用白名单（好的可证伪条件应含这类锚点）
_PRESENT_TOKENS = [
    "最新", "当前", "现已", "已发布", "已公开", "现行", "年报显示",
    "财报显示", "最近一期", "已可查", "公开数据", "现有", "已披露",
    "当前数据", "最新财报", "最新公告", "现已公开",
]


def _norm(text: str) -> str:
    """归一化：去空白、统一大小写（保留中文），便于关键词匹配。"""
    return (text or "").replace(" ", "").replace(",", "").lower()


def hit_any(haystack: str, keywords: list[str]) -> tuple[bool, str]:
    """命中任一关键词即返回 (True, 命中的词)。大小写不敏感、去千分位。"""
    h = _norm(haystack)
    for kw in keywords:
        if _norm(kw) in h:
            return True, kw
    return False, ""


def extract_numbers(text: str) -> list[float]:
    """抽取文本中的数值（支持千分位、小数）。用于事实数值校验。"""
    cleaned = (text or "").replace(",", "")
    nums = re.findall(r"\d+(?:\.\d+)?", cleaned)
    out = []
    for n in nums:
        try:
            out.append(float(n))
        except ValueError:
            pass
    return out


def check_fact(text: str, fact: FactCheck) -> tuple[bool, str]:
    """校验一条事实是否出现且数值在容差内。

    返回 (通过, 说明)。
    - 先校验关键词命中（OR）。
    - 若 fact.value 非 None，再校验文本中是否存在落在 [value*(1-tol), value*(1+tol)] 的数值。
    """
    hit, kw = hit_any(text, fact.keywords)
    if not hit:
        return False, f"关键词未命中({'/'.join(fact.keywords[:3])}...)"
    if fact.value is None:
        return True, f"关键词命中[{kw}]"
    # 数值校验
    lo = fact.value * (1 - fact.tolerance)
    hi = fact.value * (1 + fact.tolerance)
    for num in extract_numbers(text):
        if lo <= num <= hi:
            return True, f"数值匹配({num}≈{fact.value}{fact.unit})"
    return False, f"无数值落在[{lo:.1f},{hi:.1f}]{fact.unit}(期望≈{fact.value})"


def check_forbidden(text: str, forbidden: list[str]) -> list[str]:
    """返回出现的禁止词列表（应为空）。"""
    h = _norm(text)
    return [f for f in forbidden if _norm(f) in h]


def direction_coverage(text: str, directions: list[dict]) -> tuple[float, list[str], list[str]]:
    """计算期望方向的覆盖率。

    返回 (覆盖率, 已覆盖方向名, 未覆盖的must方向名)。
    """
    if not directions:
        return 1.0, [], []
    covered, missed_must = [], []
    for d in directions:
        hit, _ = hit_any(text, d["keywords"])
        if hit:
            covered.append(d["name"])
        elif d.get("must"):
            missed_must.append(d["name"])
    return len(covered) / len(directions), covered, missed_must


def detect_unexpected(text: str, unexpected: list[dict]) -> list[str]:
    """返回命中的错误方向名（应为空）。"""
    out = []
    for d in unexpected:
        hit, _ = hit_any(text, d["keywords"])
        if hit:
            out.append(d["name"])
    return out


def check_falsifiable_quality(condition: str) -> tuple[bool, str]:
    """v1 校验：单条可证伪条件是否合格（关键词黑名单快速过滤）。

    合格标准：
    - 非空（空字符串视为"逻辑推导型"，单独处理，不在此函数判负）
    - 不含未来假设词（黑名单）
    返回 (合格, 说明)。
    """
    c = (condition or "").strip()
    if not c:
        return True, "空(逻辑推导型,豁免)"
    h = _norm(c)
    for tok in _FUTURE_TOKENS:
        if _norm(tok) in h:
            return False, f"含未来假设词[{tok}]"
    return True, "无未来假设词"


# ── v2: 4 维评分（规则近似版，离线可用；LLM 版见 judge_client.py）────────
# 参考 goldens/falsifiability_rubric.md 的 4 维 rubric：
# A.数据可得性 B.时间锚定 C.阈值明确 D.单一变量，每维 0-2 分，≥6 通过
_KNOWN_DATA_SOURCES = [
    "央行", "中汽协", "wind", "年报", "财报", "招股书", "监管", "sec",
    "港交所", "上交所", "深交所", "公告", "披露", "ifind",
    "乘联会", "艾瑞", "cbinsights", "sneresearch", "互金协会",
    "工信部", "发改委", "财政部", "海关", "cbp", "cfpb",
    "最新财报", "最新年报", "最新公告", "最新数据", "官方",
    "港交所披露", "交易所", "协会", "研究院", "统计局",
]
_QUALITATIVE_TOKENS = ["大幅", "显著", "明显", "持续", "长期", "一定程度", "较大"]


def check_falsifiable_quality_v2(condition: str) -> tuple[int, dict, str]:
    """v2 规则版 4 维评分。返回 (总分0-8, 各维度分明细, 说明)。

    空条件豁免（逻辑推导型），返回 8 分 + exempt=True。
    这是 rubric 的规则近似——LLM-as-judge 版（更精确）见 judge_client.py。
    """
    c = (condition or "").strip()
    if not c:
        return 8, {"exempt": True, "reason": "logical_derivation"}, "空(逻辑推导型,豁免)"

    h = _norm(c)
    scores = {}

    # A. 数据可得性：是否引用已知公开数据源
    has_source = any(_norm(s) in h for s in _KNOWN_DATA_SOURCES)
    scores["data_availability"] = 2 if has_source else 1

    # B. 时间锚定：v1 黑名单 + 当前锚点白名单
    has_future = any(_norm(tok) in h for tok in _FUTURE_TOKENS)
    has_present = any(_norm(tok) in h for tok in _PRESENT_TOKENS)
    if has_future:
        scores["temporal_anchoring"] = 0
    elif has_present:
        scores["temporal_anchoring"] = 2
    else:
        scores["temporal_anchoring"] = 1

    # C. 阈值明确：是否含数值阈值
    has_number = bool(re.search(r"\d+(?:\.\d+)?", c))
    has_qualitative = any(_norm(t) in h for t in _QUALITATIVE_TOKENS)
    if has_number:
        scores["threshold_specificity"] = 2
    elif has_qualitative:
        scores["threshold_specificity"] = 0
    else:
        scores["threshold_specificity"] = 1

    # D. 单一变量：检查是否多"且/同时/并且"混合
    multi_cond = len(re.findall(r"且|同时|并且", c)) >= 2
    scores["single_variable"] = 1 if multi_cond else 2

    total = sum(v for k, v in scores.items() if k != "exempt")
    detail = (f"A{scores['data_availability']}+B{scores['temporal_anchoring']}"
              f"+C{scores['threshold_specificity']}+D{scores['single_variable']}={total}/8")
    return total, scores, detail


# ─────────────────────────────────────────────────────────────────────────
# 模式一：离线自检（验证题库一致性 + 比对器逻辑）—— 由 eval-offline 单独运行
# ─────────────────────────────────────────────────────────────────────────
def run_offline():
    global _passed, _failed
    print("=== Golden Answer Set 离线自检 ===\n")

    # ---- A. 题库一致性 ----
    print("[A] 题库一致性")
    _has_ext = len(CASES) > 9
    check(f"案例数≥{'14' if _has_ext else '9'}", len(CASES) >= (14 if _has_ext else 9),
          f"实际{len(CASES)}{'(含扩展案例)' if _has_ext else ''}")
    kinds = {c.kind for c in CASES}
    check("覆盖 company 和 industry", kinds == {"company", "industry"}, f"实际{kinds}")
    has_public = any(c.is_public for c in CASES)
    has_private = any(not c.is_public for c in CASES)
    check("覆盖上市公司", has_public)
    check("覆盖非上市公司/行业", has_private)
    # 覆盖海内外
    overseas = any(k in c.query for c in CASES for k in ["英伟达", "NVIDIA", "台积电", "TSMC", "SpaceX", "Affirm", "SHEIN"])
    domestic = any(k in c.query for c in CASES for k in ["奇富", "茅台", "宁德", "助贷", "新能源", "蜜雪", "创新药"])
    check("覆盖海外标的", overseas)
    check("覆盖国内标的", domestic)
    if _has_ext:
        # 扩展案例专属覆盖验证
        has_hk = any(k in c.query for c in CASES for k in ["蜜雪", "02097"])
        has_bnpl = any("Affirm" in c.query for c in CASES)
        has_pharma = any("创新药" in c.query for c in CASES)
        has_crossborder = any("跨境电商" in c.query for c in CASES)
        check("扩展: 覆盖港股标的", has_hk)
        check("扩展: 覆盖海外BNPL", has_bnpl)
        check("扩展: 覆盖创新药行业", has_pharma)
        check("扩展: 覆盖跨境电商行业", has_crossborder)

    for i, c in enumerate(CASES):
        check(f"案例{i}[{c.query[:12]}] 有required_facts或expected_directions",
              bool(c.required_facts or c.expected_directions))
        # 每个 fact 必须有来源
        facts_have_source = all(f.source for f in c.required_facts)
        check(f"案例{i} 所有fact带来源(可溯源)", facts_have_source,
              "存在无source的fact" if not facts_have_source else "")
        # must 方向不能超过总数（否则覆盖率永远不达标）
        must_count = sum(1 for d in c.expected_directions if d.get("must"))
        check(f"案例{i} must方向数合理(≤总数)", must_count <= len(c.expected_directions) or not c.expected_directions)

    # ---- B. 比对器逻辑正确性（用构造文本验证）----
    print("\n[B] 事实比对器")
    qfin = CASES[0]
    # 正例：含真实数字的文本应通过
    good_text = "奇富科技2025年营业收入192.05亿元，GAAP净利润59.76亿元，年末在贷余额1260.12亿元，营收同比增长11.9%，净利润同比下降4.4%。"
    rev_fact = qfin.required_facts[0]  # 营业收入 192.05
    ok, msg = check_fact(good_text, rev_fact)
    check("正确营收数值能通过校验", ok, msg)
    # 反例：错误数字应不通过
    bad_text = "奇富科技2025年营业收入500亿元。"
    ok2, _ = check_fact(bad_text, rev_fact)
    check("错误营收数值被拦截", not ok2)
    # 关键词缺失应不通过
    ok3, _ = check_fact("一段无关文本", rev_fact)
    check("关键词缺失被拦截", not ok3)

    print("\n[C] 防幻觉负向断言")
    # 奇富禁止词："亏损" 出现应被检出
    forb_hit = check_forbidden("公司今年出现亏损", qfin.forbidden_facts)
    check("禁止词'亏损'被检出", "亏损" in forb_hit)
    # 正常文本不应触发
    clean = check_forbidden("公司盈利稳健，营收增长", qfin.forbidden_facts)
    check("正常文本不误报禁止词", len(clean) == 0, f"误报{clean}")

    print("\n[D] 洞察方向覆盖")
    # 含轻资本+逾期的文本应有较高覆盖
    rich = "奇富正加速轻资本/技术服务转型，在贷余额主动收缩，逾期率有所上升，同时加大股票回购和分红。"
    cov, covered, missed = direction_coverage(rich, qfin.expected_directions)
    check("丰富洞察文本覆盖率达标", cov >= qfin.min_direction_coverage, f"覆盖率{cov:.0%}")
    check("must方向(轻资本转型)被覆盖", len(missed) == 0, f"漏:{missed}")
    # 贫瘠文本应覆盖率低
    poor = "这家公司还不错。"
    cov2, _, missed2 = direction_coverage(poor, qfin.expected_directions)
    check("贫瘠文本覆盖率低", cov2 < qfin.min_direction_coverage)

    print("\n[E] 错误方向检测")
    # 行业案例：把比亚迪当行业
    ev = next(c for c in CASES if "新能源汽车行业" in c.query)
    bad_dir = "比亚迪销量即行业，比亚迪市占即行业整体水平。"
    hit_bad = detect_unexpected(bad_dir, ev.unexpected_directions)
    check("'把比亚迪当行业'错误方向被检出", len(hit_bad) > 0, f"检出:{hit_bad}")
    good_dir = "行业销量1649万辆，比亚迪份额超35%但仅是头部之一。"
    hit_good = detect_unexpected(good_dir, ev.unexpected_directions)
    check("正确区分公司与行业不误报", len(hit_good) == 0, f"误报:{hit_good}")

    print("\n[F] 可证伪条件质量")
    # 好条件：基于当前可查数据
    good_cond = "若央行最新金融统计报告显示消费贷款增速高于10%（当前为6.5%），则结论被推翻"
    ok_f, msg_f = check_falsifiable_quality(good_cond)
    check("基于当前数据的可证伪条件合格", ok_f, msg_f)
    # 坏条件：未来假设
    bad_conds = [
        "若下季度营收转正则推翻",
        "若2026年Q2财报显示净利润转负",
        "若明年增速回升至10%",
        "若后续季度逾期率回落",
    ]
    all_caught = all(not check_falsifiable_quality(bc)[0] for bc in bad_conds)
    check("未来假设型可证伪条件全部被拦截", all_caught,
          "存在漏网" if not all_caught else "")
    # 空条件（逻辑推导型）应豁免
    ok_empty, _ = check_falsifiable_quality("")
    check("空可证伪条件(逻辑推导型)豁免", ok_empty)

    print("\n[F2] 可证伪条件 v2 4维评分")
    # 好条件：含数据源+当前锚点+数值阈值+单一变量 → 应≥6分
    good_v2, scores_v2, detail_v2 = check_falsifiable_quality_v2(good_cond)
    check("好条件 v2 评分≥6", good_v2 >= 6, f"{detail_v2}")
    # 坏条件：未来假设 → B=0，总分应<6
    bad_v2, _, detail_bad = check_falsifiable_quality_v2("若下季度营收转正则推翻")
    check("未来假设条件 v2 评分<6", bad_v2 < 6, f"{detail_bad}")
    # 定性条件：无数值 → C=0
    qual_v2, _, detail_qual = check_falsifiable_quality_v2("若增速大幅回升则推翻")
    check("定性条件 v2 评分<6", qual_v2 < 6, f"{detail_qual}")
    # 空条件豁免
    empty_v2, empty_scores, _ = check_falsifiable_quality_v2("")
    check("空条件 v2 豁免(8分)", empty_v2 == 8 and empty_scores.get("exempt"))
    # 扩展案例中的好条件也应通过（蜜雪冰城供应链相关）
    if _has_ext:
        mixue = next((c for c in CASES if "蜜雪" in c.query), None)
        if mixue and mixue.required_facts:
            # 构造一个基于当前数据的好条件
            good_ext = "若蜜雪冰城最新年报显示供应链批发收入占比低于50%（当前占比为主要利润来源），则结论被推翻"
            ext_v2, _, ext_detail = check_falsifiable_quality_v2(good_ext)
            check("扩展案例好条件 v2 评分≥6", ext_v2 >= 6, f"{ext_detail}")

    # ---- G. demo 数据可证伪条件质量回归（确保我们自己的脚本无未来假设）----
    print("\n[G] demo数据可证伪条件无未来假设")
    try:
        import demo_data
        # 提取 demo 中所有 falsifiable_condition 字符串
        import inspect
        src = inspect.getsource(demo_data)
        conds = re.findall(r'"falsifiable_condition":\s*"([^"]*)"', src)
        violations = []
        v2_low = []
        for cond in conds:
            ok_c, _ = check_falsifiable_quality(cond)
            if not ok_c:
                violations.append(cond[:40])
            # v2 评分：所有非空条件应≥4分（宽松阈值，规则版是近似）
            if cond.strip():
                v2_total, _, _ = check_falsifiable_quality_v2(cond)
                if v2_total < 4:
                    v2_low.append((cond[:30], v2_total))
        check(f"demo所有可证伪条件无未来假设({len(conds)}条)", len(violations) == 0,
              f"违规:{violations[:3]}")
        check(f"demo非空条件 v2 评分均≥4({len([c for c in conds if c.strip()])}条)", len(v2_low) == 0,
              f"低分:{v2_low[:3]}")
    except Exception as e:
        check("demo数据可读取", False, str(e))

    print(f"\n=== 离线自检结果：{_passed} 通过 / {_failed} 失败 ===")
    if _failed == 0:
        print("✅ Golden Answer Set 离线自检全部通过")
    else:
        print(f"⚠️ {_failed} 项未通过")
    if record_trend:
        record_trend("offline", _passed, _failed)
    return _failed


# ─────────────────────────────────────────────────────────────────────────
# 模式二：真实 LLM 验证（需 API Key）
# ─────────────────────────────────────────────────────────────────────────
async def run_one_case_live(case: GoldenCase) -> dict:
    """真实跑一个案例，返回评测结果 dict。"""
    from schemas import AnalysisRun
    from orchestrator import Orchestrator

    run = AnalysisRun(query=case.query, provider="deepseek")
    orch = Orchestrator(run)
    async for _ in orch.run_pipeline():
        pass

    # 汇总待校验文本：narrative + 所有洞察 claim/reasoning
    text_parts = [run.narrative_md or ""]
    for ins in run.insights:
        text_parts.append(ins.claim)
        text_parts.append(ins.reasoning)
    full_text = "\n".join(text_parts)

    # 1. 事实校验
    fact_results = []
    for f in case.required_facts:
        ok, msg = check_fact(full_text, f)
        fact_results.append((f.label, ok, msg))
    fact_pass = sum(1 for _, ok, _ in fact_results if ok)

    # 2. 防幻觉
    forbidden_hits = check_forbidden(full_text, case.forbidden_facts)

    # 3. 洞察方向
    cov, covered, missed_must = direction_coverage(full_text, case.expected_directions)
    unexpected_hits = detect_unexpected(full_text, case.unexpected_directions)

    # 4. 可证伪条件质量 v1（黑名单快速过滤）
    fals_violations = []
    for ins in run.insights:
        ok_f, msg_f = check_falsifiable_quality(ins.falsifiable_condition)
        if not ok_f:
            fals_violations.append((ins.claim[:30], ins.falsifiable_condition[:40], msg_f))

    # 5. 可证伪条件 v2 4维评分（规则版，LLM 版可选）
    fals_v2_results = []
    for ins in run.insights:
        total_v2, scores_v2, detail_v2 = check_falsifiable_quality_v2(ins.falsifiable_condition)
        if total_v2 < 6 and not scores_v2.get("exempt"):
            fals_v2_results.append((ins.claim[:30], total_v2, detail_v2))

    # 6. LLM-as-judge（可选，需 API Key）
    fals_judge_results = []
    try:
        from evals.judge_client import score_condition
        for ins in run.insights:
            if ins.falsifiable_condition.strip():
                jr = score_condition(ins.claim, ins.falsifiable_condition)
                if not jr.get("pass") and not jr.get("exempt"):
                    fals_judge_results.append((ins.claim[:30], jr.get("total", 0), jr.get("violations", [])))
    except Exception:
        pass  # judge_client 不可用时跳过

    return {
        "query": case.query,
        "status": run.status,
        "fact_results": fact_results,
        "fact_pass": fact_pass,
        "fact_total": len(case.required_facts),
        "forbidden_hits": forbidden_hits,
        "coverage": cov,
        "covered": covered,
        "missed_must": missed_must,
        "unexpected_hits": unexpected_hits,
        "fals_violations": fals_violations,
        "fals_v2_results": fals_v2_results,
        "fals_judge_results": fals_judge_results,
        "insight_count": len(run.insights),
    }


async def run_live(case_idx: int | None = None):
    global _passed, _failed
    print("=== Golden Answer Set 真实 LLM 验证 ===")
    print("⚠️ 调用真实 LLM + 搜索，耗时耗 token。\n")

    cases = CASES if case_idx is None else [CASES[case_idx]]
    for c in cases:
        print(f"\n{'='*60}\n▶ {c.query}\n  ({c.note})\n{'='*60}")
        try:
            r = await run_one_case_live(c)
        except Exception as e:
            check(f"[{c.query[:16]}] 流程完成", False, f"崩溃:{e}")
            continue

        check(f"[{c.query[:16]}] 流程完成", r["status"] == "done", f"status={r['status']}")
        # 事实
        for label, ok, msg in r["fact_results"]:
            check(f"  事实·{label}", ok, msg)
        # 防幻觉
        check(f"  无幻觉(禁止词)", len(r["forbidden_hits"]) == 0,
              f"出现:{r['forbidden_hits']}")
        # 洞察方向
        check(f"  洞察方向覆盖率≥{c.min_direction_coverage:.0%}",
              r["coverage"] >= c.min_direction_coverage,
              f"实际{r['coverage']:.0%},已覆盖:{r['covered']}")
        check(f"  必备洞察方向全覆盖", len(r["missed_must"]) == 0,
              f"漏:{r['missed_must']}")
        check(f"  无错误洞察方向", len(r["unexpected_hits"]) == 0,
              f"出现:{r['unexpected_hits']}")
        # 可证伪条件质量 v1（黑名单）
        check(f"  可证伪条件无未来假设({r['insight_count']}条洞察)",
              len(r["fals_violations"]) == 0,
              f"违规{len(r['fals_violations'])}条:{r['fals_violations'][:2]}")
        # 可证伪条件 v2 4维评分
        check(f"  可证伪条件 v2 4维评分均≥6({r['insight_count']}条洞察)",
              len(r["fals_v2_results"]) == 0,
              f"低分{len(r['fals_v2_results'])}条:{r['fals_v2_results'][:2]}")
        # LLM-as-judge（如有结果）
        if r.get("fals_judge_results"):
            check(f"  LLM-judge 评分均通过({len(r['fals_judge_results'])}条低分)",
                  len(r["fals_judge_results"]) == 0,
                  f"低分:{r['fals_judge_results'][:2]}")

    print(f"\n=== 真实验证结果：{_passed} 通过 / {_failed} 失败 ===")
    if record_trend:
        record_trend("live", _passed, _failed)
    return _failed


# ─────────────────────────────────────────────────────────────────────────
def print_report():
    print("\n=== Golden Answer Set 题库统计 ===\n")
    print(f"{'#':<3}{'类型':<10}{'上市':<6}{'Query':<28}{'事实':<6}{'方向':<6}")
    print("-" * 60)
    for i, c in enumerate(CASES):
        pub = "是" if c.is_public else "否"
        print(f"{i:<3}{c.kind:<10}{pub:<6}{c.query[:26]:<28}"
              f"{len(c.required_facts):<6}{len(c.expected_directions):<6}")
    print("-" * 60)
    total_facts = sum(len(c.required_facts) for c in CASES)
    total_dirs = sum(len(c.expected_directions) for c in CASES)
    print(f"共 {len(CASES)} 案例 · {total_facts} 事实校验项 · {total_dirs} 洞察方向\n")


def main():
    if "--trend" in sys.argv:
        if not show_trend:
            print("趋势模块未加载")
            return 0
        n = 20
        try:
            i = sys.argv.index("--trend")
            if len(sys.argv) > i + 1:
                n = int(sys.argv[i + 1])
        except (ValueError, IndexError):
            pass
        return show_trend(n)
    if "--report" in sys.argv:
        print_report()
    if "--live" in sys.argv:
        idx = None
        if "--case" in sys.argv:
            idx = int(sys.argv[sys.argv.index("--case") + 1])
        return asyncio.run(run_live(idx))
    return run_offline()


if __name__ == "__main__":
    sys.exit(main())
