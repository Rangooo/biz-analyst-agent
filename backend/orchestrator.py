"""
Agent 六阶段编排器 —— 项目的灵魂。

Scope → Collect → Analyze → Falsify → Refine → Report

区别于 chatbot 的工程保证（全部写进代码，不靠 prompt 自觉）：
1. 每轮迭代必须有【外部新证据】进入，否则禁止改动置信度（防反思剧场）。
2. 洞察必须可证伪，可证伪条件必须基于当前已公开可查的数据（不可用未来假设）；不可检验但逻辑严密的论断标注"逻辑推导型"保留，不降级。
3. 红队由【异源模型】扮演，fresh thread，不喂修订历史（防评分虚高）。
4. 收敛由量化证据强度门槛决定，编排器只驱动不宣判。
5. stale_count 防认知打转：连续无新证据 ≥2 强制转向，≥4 标记需人工。
6. Report 阶段直接生成 narrative 文档（不再生成结构化模板 report_md，避免冗余 token 消耗）。

以 async generator 形式逐事件 yield，供 SSE 流式推送。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator
from urllib.parse import urlparse

logger = logging.getLogger("biz_analyst.orchestrator")

import prompts
from framework.analysis_framework import (
    PRIVATE_COMPANY_OVERLAY,
    get_template,
    pick_template,
)
from llm.client import get_client
from schemas import (
    AnalysisRun,
    Evidence,
    FalsificationRecord,
    Insight,
    ObjectProfile,
    SourceTier,
    TraceEvent,
    Verdict,
)
from reporting.appendix import (
    build_appendix_md,
    collect_data_gaps,
    format_apa_reference,
)
from tools import search as search_tool
from tools import sec_edgar
from tools.fetch import fetch_text
from tools.evidence_index import EvidenceIndex
from store import save_run, load_run
from run_metrics import build_run_metrics, evaluate_collect_quality, write_run_metrics
from reporting.structure import (
    _FACTS_SECTION_ALIASES,
    _extract_cited_ids,
    _normalize_narrative,
    _strip_llm_references,
    check_structure_invariants,
    repair_structure_invariants_once,
)
from reporting.writing_contract import (
    audit_report_grounding,
    attach_exact_numeric_citations,
    build_cautious_gap_queries,
    build_writing_contract,
    ensure_dimension_coverage_boundaries,
    neutralize_unsupported_coverage_rows,
    neutralize_unsupported_scenarios,
    neutralize_uncited_tracking_thresholds,
    normalize_grouped_citations,
    normalize_plain_numeric_citations,
    propagate_derived_table_citations,
    select_cautious_report_evidence_ids,
    select_report_evidence_ids,
    should_use_cautious_writing,
)
from reporting.source_governance import sanitize_source_url
import memory_store
from memory_store import save_eval_feedback
from evals.fact_check import extract_and_verify_facts

# 收敛参数
CONF_THRESHOLD = 0.7      # 置信度达标门槛
MAX_REFINE_ROUNDS = 2     # 最大证伪迭代轮数（第2轮基于第1轮裁决做定向补证；靠早停防空转）
STALE_LIMIT = 2           # 连续无新证据轮数 → 强制转向
STALE_HUMAN = 4           # → 标记需人工
MAX_COUNTER_QUERIES_PER_INSIGHT = 2
MAX_COUNTER_RESULTS_PER_QUERY = 1
MAX_SUPPORT_QUERIES_PER_INSIGHT = 1
MAX_SUPPORT_RESULTS_PER_QUERY = 1
MAX_DEBATE_QUERIES_PER_INSIGHT = 1
MAX_DEBATE_RESULTS_PER_QUERY = 2


def _collect_gate_message(run: AnalysisRun, evidence_count: int,
                          search_state: dict | None = None) -> str:
    """Return a blocking message when collection has no auditable evidence."""
    if evidence_count > 0:
        return ""
    sources = ", ".join(run.data_sources_used or []) or "无"
    status = (search_state or {}).get("status") or "unknown"
    if status == "unavailable":
        hint = "搜索源不可用；请在设置中配置 EXA_API_KEY、TAVILY_API_KEY 或 SERPER_API_KEY 后重跑"
    elif status == "degraded":
        hint = "搜索源处于降级状态；建议更换或补充搜索源后重跑"
    else:
        hint = "可用数据源未返回有效证据；请补充搜索或金融数据源后重跑"
    profile = run.profile
    subject = profile.name if profile else run.query
    if profile and profile.is_public and profile.ticker:
        subject = f"{subject}（{profile.ticker}）"
    return (f"采集质量门禁未通过：{subject} 未采集到任何可溯源证据。"
            f"当前数据源：{sources}。{hint}。已停止后续分析，避免生成无证据的低置信洞察。")


def _is_sec_ticker(ticker: str) -> bool:
    """SEC EDGAR only supports US-listed tickers/CIKs, not HK/A-share codes."""
    t = (ticker or "").strip().upper()
    if not t:
        return False
    if any(mark in t for mark in (".HK", ".SS", ".SZ", ".SH")):
        return False
    if re.fullmatch(r"\d{4,6}", t):
        return False
    return bool(re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", t))


def _safe_float(v, default: float = 0.5) -> float:
    """容错解析模型自报的数值（可能是 "0.8"/"高"/"0.8分"/None/list）。失败回默认。"""
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        import re as _re
        m = _re.search(r"-?\d+(?:\.\d+)?", v)
        if m:
            try:
                return float(m.group())
            except ValueError:
                pass
    return default


def _safe_int(v, default: int = 0) -> int:
    """容错解析整数（用于质量评分 total/各维度分）。"""
    f = _safe_float(v, float(default))
    try:
        return int(round(f))
    except (ValueError, TypeError):
        return default


def _safe_str(v, max_len: int = 800) -> str:
    """容错解析模型返回的字符串字段（可能是 str/list/dict/None）。
    LLM 偶尔把单字段返回成 list/dict，统一兜底成 str，防 FalsificationRecord/TraceEvent 崩溃。"""
    if isinstance(v, str):
        return v
    if isinstance(v, (list, dict)):
        try:
            return json.dumps(v, ensure_ascii=False)[:max_len]
        except Exception:  # noqa: BLE001
            return str(v)[:max_len]
    return "" if v is None else str(v)[:max_len]


# 九维挑战维度中文名映射（供事件/报告展示）
_DIM_LABELS = {
    "temporal": "时效挑战", "conflict_of_interest": "利益相关",
    "source_reliability": "来源可靠性", "logic": "逻辑挑战",
    "external_consistency": "外部一致性", "boundary": "边界条件",
    "alternative": "替代理论", "missing_evidence": "缺失证据",
    "independence": "独立性",
}
_SEV_ORDER = {"high": 4, "medium": 3, "low": 2, "none": 1}


def _load_source_tiers() -> list[tuple[str, list[str]]]:
    """从 config/source_tiers.json 加载信源 tier 域名表。
    返回 [(SourceTier 枚举名, [域名,...]), ...]，按文件中的顺序（regulator→filing→auth_media）。
    文件缺失或损坏时回退到最小内置表，保证 _guess_tier 不崩。"""
    path = Path(__file__).parent / "config" / "source_tiers.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        logger.warning("加载 source_tiers.json 失败，使用内置兜底表", exc_info=True)
        return [("THIRD_PARTY", ["sec.gov", "gov.cn", "reuters", "bloomberg", "caixin"])]
    groups = []
    for key in ("regulator", "filing", "auth_media"):
        section = raw.get(key) or {}
        tier = section.get("tier", "MEDIA")
        domains = [d.lower() for d in (section.get("domains") or [])]
        if domains:
            groups.append((tier, domains))
    return groups


_SOURCE_TIER_GROUPS = _load_source_tiers()


_OFFICIAL_DISCLOSURE_MARKERS = (
    "investor", "investors", "ir.", "/ir/", "/ir-", "/financials", "/financial-reports",
    "annual-report", "annual_reports", "interim-report", "quarterly", "earnings",
    "results", "announcement", "disclosure", "filing", "report.pdf", "/reports/",
    "/documents/", "/document-", "/uploads/", "/static/",
)

_NON_COMPANY_SOURCE_DOMAINS = (
    "36kr.com", "sina.com.cn", "xueqiu.com", "eastmoney.com", "dfcfw.com",
    "10jqka.com.cn", "hexun.com", "hibor.com.cn", "guandian.cn", "guancha.cn",
    "reuters.com", "bloomberg.com", "wsj.com", "ft.com", "marketwatch.com",
)

_COMPANY_SUFFIX_RE = re.compile(
    r"(股份有限公司|控股有限公司|集团控股有限公司|集团有限公司|有限公司|股份|控股|集团|科技|公司)$"
)


def _source_host(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def _source_root_domain(host: str) -> str:
    parts = [p for p in host.split(".") if p and p != "www"]
    if len(parts) >= 3 and ".".join(parts[-2:]) in {"com.cn", "net.cn", "org.cn", "gov.cn"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _is_non_company_source(url: str) -> bool:
    host = _source_host(url)
    root = _source_root_domain(host)
    return any(d in host or d == root for d in _NON_COMPANY_SOURCE_DOMAINS)


def _has_official_disclosure_signal(url: str) -> bool:
    u = (url or "").lower()
    host = _source_host(u)
    path = (urlparse(u).path or "").lower() if u else ""
    return any(marker in u for marker in _OFFICIAL_DISCLOSURE_MARKERS) or host.startswith(("ir.", "investor."))


def _target_alias_tokens(*parts: str) -> set[str]:
    text = " ".join(p for p in parts if p)
    tokens: set[str] = set()
    for raw in re.findall(r"[\u4e00-\u9fff]{2,20}", text):
        cleaned = raw
        while True:
            new = _COMPANY_SUFFIX_RE.sub("", cleaned)
            if new == cleaned:
                break
            cleaned = new
        for cand in {raw, cleaned, cleaned[:4], cleaned[:3], cleaned[:2]}:
            if 2 <= len(cand) <= 20 and cand not in {"中国", "集团", "控股", "科技", "公司"}:
                tokens.add(cand)
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9&.-]{2,30}", text):
        cleaned = raw.lower().strip(".-")
        if cleaned not in {"inc", "ltd", "limited", "group", "holding", "company", "corp", "corporation"}:
            tokens.add(cleaned)
    return tokens


def _text_or_url_mentions_target(url: str, title: str, content: str, profile: ObjectProfile | None, query: str) -> bool:
    if not profile:
        return False
    haystack = f"{url} {title or ''} {content or ''}".lower()
    aliases = _target_alias_tokens(profile.name, profile.ticker, query)
    if not aliases:
        return False
    for alias in aliases:
        if alias.lower() in haystack:
            return True
    return False


# 网页 boilerplate 清洗 —— 金融资讯站(财联社/新华/新浪/雪球等)抓回的 content 常夹导航栏/
# 分享按钮/字体选择/浏览量/面包屑，原样进报告正文会很丑、进 LLM 上下文也污染判断。
_NAV_TOKENS = {
    "首页", "电报", "话题", "盯盘", "VIP", "FM", "投研", "下载", "全部", "加红", "公司",
    "看盘", "港美股", "基金", "提醒", "直播", "行情", "自选", "资讯", "公告", "研报",
    "更多", "正文", "下载APP", "登录", "注册", "客户端", "返回", "顶部",
}


def _is_boilerplate_line(line: str) -> bool:
    """判断一行是否为网页 chrome 噪声（导航/分享/字体/面包屑/孤立数字）。"""
    s = line.strip().strip("#").strip()
    if not s:
        return True
    # 纯标点/分隔线
    if set(s) <= set("-=_*~`|>·• "):
        return True
    # 面包屑 / "正文" 行
    if s.startswith(">") or s in ("正文", "新闻", "财经"):
        return True
    # 分享/字体/打印/下载 chrome
    if any(k in s for k in ("分享到", "分享给", "字体：", "字体:", "打印", "Email", "下载APP", "扫码", "关注微信")):
        return True
    # 页脚导航拼接行（如"关于我们网站声明联系方式用户反馈网站地图帮助"）
    if any(k in s for k in ("关于我们", "网站声明", "联系方式", "用户反馈", "网站地图",
                            "版权所有", "免责声明", "Copyright", "copyright")) and len(s) < 30:
        return True
    # 孤立浏览量/数字/时间戳（如 "1894" / "2025 08/22 19:14:39"）
    s2 = s.replace("/", "").replace(":", "").replace("-", "").replace(".", "").replace(" ", "")
    if s2.isdigit() and len(s2) <= 14:
        return True
    # 纯导航 token 组合（如 "首页 电报 话题" / "## 全部 ## 加红"）：去空格/## 后若全是 _NAV_TOKENS 则弃
    toks = [t for t in s.replace("#", " ").split() if t]
    if toks and all(t in _NAV_TOKENS for t in toks):
        return True
    return False


def _clean_evidence_text(text: str, max_len: int = 480) -> str:
    """剥网页 boilerplate：丢导航/分享/字体/浏览量行，从首个实质行起，合并空行，截断。"""
    if not text:
        return ""
    lines = [ln.strip() for ln in str(text).splitlines()]
    # 丢噪声行
    kept = [ln for ln in lines if not _is_boilerplate_line(ln)]
    # 找首个“实质行”作起点（含≥4个非日期中文，或纯数字+单位如"425.63亿元"），跳过残留的前置 chrome（含 logo+时间戳）
    start = 0
    for i, ln in enumerate(kept):
        cjk = sum(1 for ch in ln if "\u4e00" <= ch <= "\u9fff" and ch not in "年月日时分秒")
        if cjk >= 4:
            start = i
            break
    kept = kept[start:]
    # 合并连续空行
    out, blank = [], False
    for ln in kept:
        if not ln:
            if blank:
                continue
            blank = True
        else:
            blank = False
        out.append(ln)
    text = "\n".join(out).strip()
    return text[:max_len] if len(text) > max_len else text


# 未来假设词模式——falsification_path 禁止使用（须基于现有公开数据）
_FUTURE_WORDS_RE = re.compile(
    r"若(下|明|后|未来|下一|后续|来年)|如果(下|明|后|未来|下一|后续|来年)|"
    r"待(观察|验证|跟踪|披露)|需(进一步|持续跟踪|继续观察|等待)|"
    r"下一(期|季度|年度|期报|季报)|下(季报|季度)|"
    r"后续(数据|财报|报告|披露|观察|跟踪)|未来(数据|财报|报告|季度|观察|跟踪)"
)

# 数据陈旧模式——temporal 维度中泛泛的"数据过时"投诉（未指出具体更近期已发布来源）
_STALENESS_RE = re.compile(
    r"数据.*(过时|滞后|较旧|陈旧|老旧|不够新|时效性差)|"
    r"(需要|缺乏|缺少|未引用|未包含).*(更新|最新|近期|更近|较新).*(数据|财报|报告|数据源)|"
    r"(数据|证据).*(有|存在).*(滞后|延迟|过时)|"
    r"时效.*(不足|不够|较差|有问题)|"
    r"需要.*更.*新.*数据|需.*更新.*后.*验证"
)

# 具体来源引用模式——如果 temporal 挑战引用了具体的更近期文档/日期，则视为有效挑战
_SPECIFIC_NEWER_RE = re.compile(
    r"(20\d{2})\s*[年Qq度]?\s*[/\-年]?\s*(Q[1-4]|[一二三四]季度|上半年|下半年|年报|中报|季报)?|"
    r"(FY20\d{2})|"
    r"(10-K|10-Q|年报|中报|季报|一季报|三季报).*(20\d{2})"
)


def _sanitize_falsification_path(fp: str, sorted_ch: list | None = None,
                                  overall: str = "") -> str:
    """后处理红队输出的 falsification_path——检测并清除未来假设词。
    红队 prompt 已禁止未来假设，但 LLM 可能不遵守。代码兜底：
    含未来假设词时，从最高严重度挑战重新构造；即使 refuted 也不保留未来数据条件。"""
    if not fp:
        return fp
    if not _FUTURE_WORDS_RE.search(fp):
        return fp  # 无未来假设词，保留原样
    if "无——基于现有公开数据无法推翻" in fp:
        return "无——基于现有公开数据无法推翻"
    # 含未来假设词——尝试从最高严重度挑战重新构造
    if sorted_ch:
        for _ch in sorted_ch:
            _sev = str(_ch.get("severity", "none")).lower()
            if _sev in ("high", "medium"):
                _dim = _DIM_LABELS.get(_ch.get("dimension", ""), _ch.get("dimension", ""))
                _challenge = _safe_str(_ch.get("challenge", ""), 200)
                if _challenge and not _FUTURE_WORDS_RE.search(_challenge):
                    return f"红队{_dim}挑战({_sev})：{_challenge}"
    # 无法从挑战构造——不能保留未来假设；即使 refuted 也必须回到当前已发布证据
    if overall == "refuted":
        return "红队判定存在反证，但原推翻路径依赖未发布资料；需改用当前已发布反证复核"
    return "无——基于现有公开数据无法推翻"


def _sanitize_temporal_challenges(challenges: list, data_as_of: str,
                                   overall: str) -> tuple[list, str]:
    """后处理红队 temporal 维度挑战——降级基于不可得数据的"数据过时"投诉。

    红队 prompt 已注入时效约束，但 LLM 可能仍说"数据过时/需更新"而不指出
    具体的更近期已发布文档。此函数检测这类无效挑战并降级为 none。
    如果降级后无其他 medium/high 维度，同步升级 overall。

    返回 (修正后的 challenges, 修正后的 overall)。
    """
    if not challenges or not data_as_of:
        return challenges, overall

    # 从 data_as_of 提取年份，用于判断挑战是否引用了更近期的来源
    _as_of_year = 0
    for m in re.finditer(r"20(\d{2})", data_as_of):
        _as_of_year = max(_as_of_year, int(m.group(0)))
        break

    modified = False
    has_other_medium_high = False
    for ch in challenges:
        if not isinstance(ch, dict):
            continue
        _dim = str(ch.get("dimension", "")).lower()
        _sev = str(ch.get("severity", "none")).lower()
        if _dim != "temporal" or _sev == "none":
            if _sev in ("medium", "high"):
                has_other_medium_high = True
            continue

        _text = _safe_str(ch.get("challenge", ""), 300)
        # 检测是否为泛泛的"数据过时"投诉
        if not _STALENESS_RE.search(_text):
            if _sev in ("medium", "high"):
                has_other_medium_high = True
            continue

        # 检测是否引用了具体的更近期来源
        _has_specific = False
        for m in _SPECIFIC_NEWER_RE.finditer(_text):
            _yr_str = m.group(1) or m.group(3) or ""
            if _yr_str:
                _yr = int(_yr_str[:4]) if len(_yr_str) >= 4 else 0
                if _yr > _as_of_year:
                    _has_specific = True
                    break
            elif m.group(2) or m.group(4):
                # 有报告类型引用（年报/中报/10-K 等），视为具体
                _has_specific = True
                break

        if not _has_specific:
            # 泛泛"数据过时"且未引用具体更近期来源 → 降级
            ch["severity"] = "none"
            ch["challenge"] = (
                f"{_text}（已降级：未指出具体的更近期已发布文档，"
                f"当前最新可得数据截至 {data_as_of}，不可要求尚未发布的数据）"
            )
            modified = True
        else:
            if _sev in ("medium", "high"):
                has_other_medium_high = True

    # 如果降级了 temporal 挑战且无其他 medium/high，升级 overall
    _new_overall = overall
    if modified and not has_other_medium_high:
        if overall == "incomplete":
            _new_overall = "solid"
        elif overall == "refuted":
            # temporal 是唯一 high 维度被降级，但 refuted 需要更谨慎
            # 只有当确无其他 high 时才降为 incomplete
            _new_overall = "incomplete"

    return challenges, _new_overall


def _is_garbled(snippet: str) -> bool:
    """检测摘录是否为 PDF 抽取乱码（中文极少 + Latin Extended/符号主导）。"""
    if not snippet:
        return True
    cjk = sum(1 for ch in snippet if "\u4e00" <= ch <= "\u9fff")
    if cjk >= 3:
        return False  # 有足够中文，不判乱码
    # Latin Extended 字符多（ɳʣ¹˾ɹ̵Ǽ 这类 PDF 乱码典型）
    latin_ext = sum(1 for ch in snippet if ch.isalpha() and ord(ch) > 0x024F)
    if latin_ext >= 3:
        return True
    # 真正的 ASCII 字母数字占比低（符号/竖线主导，如 "| | - - | | 20241030"）
    ascii_alnum = sum(1 for ch in snippet if ch.isalnum() and ord(ch) < 0x0250)
    if ascii_alnum < len(snippet) * 0.3:
        return True
    return False


def _looks_like_thinking_draft(text: str) -> bool:
    """正文质量闸门：检测 LLM 输出是否为「思考/规划草稿」而非最终成文。

    弱模型在超长上下文下会输出整篇第一人称规划草稿（"让我先盘点…""现在设计报告结构"）
    甚至把 prompt 的禁止规则复述进去，正文结构（## 标题）一个都没有。
    命中 → 调用方应强制最强模型重写；再失败则拒绝落盘，绝不把草稿当报告。
    """
    if not text or not text.strip():
        return False
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # ① 报告必须有至少一个 markdown 二级标题；长文本无任何 ## 标题 = 未成文
    has_h2 = any(ln.startswith("## ") for ln in lines)
    if not has_h2 and len(text) > 800:
        return True
    # ② 第一人称规划/思考句特征（出现即视为思考草稿）
    planning_cues = (
        "我需要", "让我先", "让我再", "让我盘点", "我先盘点", "现在设计",
        "我再想想", "先理清任务", "设计报告结构", "OK。现在", "我打算",
        "我的写作计划", "让我梳理", "先盘点", "我来理清",
    )
    if any(cue in text for cue in planning_cues):
        return True
    return False


# 正文质量闸门重试时的强化约束后缀：模型上次输出了思考/规划草稿，
# 本次要求它直接产出正文（尊重当前 reviewer 模型配置，不强制换模型）。
_HARD_RETRY_SUFFIX = (
    "\n\n【重试·硬性要求】你上一次的输出是思考/规划草稿，不是最终正文，已被系统拒绝。"
    "本次必须直接产出最终报告正文：\n"
    "- 第一个字符就是 `## 执行摘要`，禁止以任何寒暄、规划或思考句开头\n"
    "- 禁止出现「我需要/让我先/我先盘点/现在设计/让我再/OK。现在/检查论证链」等第一人称过程描述\n"
    "- 全篇只包含读者可见的报告 markdown 正文，不包含任何元描述、大纲、草稿或自我指涉\n"
)


# —— 证伪闭环纯逻辑（无副作用，可独立单测）——

# refuted/unverifiable 洞察的置信度天花板。语义：confidence = "洞察成立的可信度"，
# 已推翻的洞察不可能高可信（修复 refuted 却 79% 置信度的 bug）。
_VERDICT_CONF_CAP = {
    Verdict.REFUTED: 0.20,
    Verdict.UNVERIFIABLE: 0.30,
}
DEBATE_CONF_THRESHOLD = 0.6   # 分析师原始置信度高于此值才触发辩论回合
STALE_DELTA = 0.03            # 置信度变动小于此值视为无实质进展（stale）
COUNTER_QUERY_LIMIT = 3       # 每条洞察最多搜索的反证查询数
RESULTS_PER_QUERY = 2         # 每个查询采纳的结果数
FALSIFY_CONCURRENCY = 8       # Falsify 阶段并发证伪的洞察数上限
# 裁决标签与置信度的一致性边界（避免"成立 45%"/"存疑 58%"这类自相矛盾的展示）：
# SUPPORTED 的置信度不得低于此值，否则降级为 QUESTIONABLE
SUPPORTED_MIN_CONF = 0.55
# QUESTIONABLE 的置信度不得高于此值，否则升级为 SUPPORTED
QUESTIONABLE_MAX_CONF = 0.70


def _should_freeze_confidence(new_counter: int, existing_support: int,
                              overall: str, search_available: bool) -> bool:
    """是否冻结置信度（防反思剧场）。
    仅当三者同时满足才冻结：(a)无新反证 AND (b)现有支撑<2条 AND (c)无搜索能力。
    现有证据已充分，或红队判定 solid 时，即使无新证据也允许裁决。"""
    has_sufficient_evidence = existing_support >= 2
    red_team_passes = overall == "solid"
    return (new_counter == 0 and not has_sufficient_evidence
            and not red_team_passes and not search_available)


def _fuse_confidence(model_conf: float, evidence_strength: float) -> float:
    """模型裁决置信度(0.7权重) + 代码量化证据强度(0.3权重)。
    模型有上下文理解能力，给更大权重；证据强度作为锚定修正。"""
    return round(model_conf * 0.7 + evidence_strength * 0.3, 3)


def _cap_confidence(verdict: Verdict, conf: float) -> float:
    """按 verdict 施加置信度天花板。"""
    cap = _VERDICT_CONF_CAP.get(verdict)
    if cap is not None and conf > cap:
        return cap
    return conf


def _align_verdict_confidence(verdict: Verdict, conf: float) -> Verdict:
    """对齐裁决标签与置信度，消除自相矛盾的展示。
    - SUPPORTED 但置信度过低（<SUPPORTED_MIN_CONF）→ 降级为 QUESTIONABLE（成立却低分说不通）
    - QUESTIONABLE 但置信度足够高（≥QUESTIONABLE_MAX_CONF）→ 升级为 SUPPORTED（存疑却高分说不通）
    - REFUTED/UNVERIFIABLE 已被 _cap_confidence 压到低位，保持不动。
    只调整标签，不改置信度数值——数值由证据强度量化得出，是更硬的信号。"""
    if verdict == Verdict.SUPPORTED and conf < SUPPORTED_MIN_CONF:
        return Verdict.QUESTIONABLE
    if verdict == Verdict.QUESTIONABLE and conf >= QUESTIONABLE_MAX_CONF:
        return Verdict.SUPPORTED
    return verdict


def _is_stale(final_conf: float, prev_conf: float, refined: bool) -> bool:
    """本轮置信度几乎没动且未补强 → 计为 stale。"""
    return abs(final_conf - prev_conf) < STALE_DELTA and not refined


def _dedup_queries(*query_lists) -> list[str]:
    """合并多个查询来源并按出现顺序去重（空串丢弃）。"""
    seen: set[str] = set()
    out: list[str] = []
    for lst in query_lists:
        for q in (lst or []):
            q = _safe_str(q, 200).strip()
            if q and q not in seen:
                seen.add(q)
                out.append(q)
    return out


class Orchestrator:
    def __init__(self, run: AnalysisRun):
        self.run = run
        self.llm = get_client()
        self.provider = run.provider
        self._ev_counter = 0
        self.evidence_pool: dict[int, Evidence] = {}  # 编号 -> 证据
        self._ev_index: EvidenceIndex | None = None  # 向量检索索引（Collect 后构建）
        self.active_strategy_cards: list[dict] = []
        self._applied_strategy_ids: set[str] = set(run.applied_strategy_ids or [])
        # Human-in-the-loop：Scope 确认信号
        self._scope_confirmed = asyncio.Event()
        self._scope_user_sections: list[str] | None = None  # 用户修改后的维度列表
        # Demo 模式：没有配置任何真实 LLM key 时，用脚本化数据跑通全流程（开箱即用）
        self.demo = not any(
            p["available"] for p in self.llm.list_available()
        )
        # 单 Key 降级检测：analyst 与 red_team 是否同源
        if not self.demo:
            # 尊重前端选的红队（analyze 时已写入 run.red_team_provider），未选则按 role_defaults
            self.red_team_provider = run.red_team_provider or self.llm.effective_provider("red_team") or run.provider
            run.red_team_provider = self.red_team_provider
            # 尊重前端选的终审（analyze 时已写入 run.reviewer_provider），未选则按 role_defaults
            self.reviewer_provider = run.reviewer_provider or self.llm.effective_provider("reviewer") or self.provider
            run.reviewer_provider = self.reviewer_provider
            a_eff = self.llm.effective_provider("analyst", self.provider)
            run.same_source_review = (not a_eff or not self.red_team_provider
                                      or a_eff == self.red_team_provider)
        else:
            self.red_team_provider = run.red_team_provider or ""
            self.reviewer_provider = run.reviewer_provider or ""

    # ---------- 事件辅助 ----------
    def _ev(self, stage: str, type_: str, title: str, detail: str = "", **payload) -> TraceEvent:
        # 防御：LLM/外部数据偶尔返回 list/dict 当 title/detail，TraceEvent 要 str，会 pydantic 校验炸
        # （曾出现红队 challenge 返回 list → "1 validation error for TraceEvent detail"）。
        # 统一在此兜底：非 str 一律 JSON 序列化为可读字符串，None→""。
        def _s(v):
            if v is None:
                return ""
            if isinstance(v, str):
                return v
            if isinstance(v, (list, dict)):
                import json as _j
                try:
                    return _j.dumps(v, ensure_ascii=False)[:600]
                except Exception:  # noqa: BLE001
                    return str(v)[:600]
            return str(v)
        e = TraceEvent(
            run_id=self.run.id, stage=stage, type=type_,
            title=_s(title), detail=_s(detail), payload=payload,
        )
        self.run.trace.append(e)
        return e

    def _strategy_context(self) -> dict:
        profile = self.run.profile
        return {
            "query": self.run.query,
            "name": profile.name if profile else self.run.query,
            "industry": profile.industry if profile else "",
            "template_key": profile.template_key if profile else "generic",
            "kind": profile.kind if profile else "",
            "sections": profile.sections if profile else [],
        }

    def _load_active_strategies(self):
        try:
            self.active_strategy_cards = memory_store.load_active_strategy_cards(
                self._strategy_context(), limit=10
            )
            self.run.applied_strategy_ids = list(dict.fromkeys(self.run.applied_strategy_ids or []))
        except Exception:  # noqa: BLE001
            logger.warning("加载 active strategy cards 失败，跳过", exc_info=True)
            self.active_strategy_cards = []

    def _stage_strategies(self, stage: str) -> list[dict]:
        return [c for c in self.active_strategy_cards if c.get("stage") == stage]

    def _mark_strategy_applied(self, card: dict):
        sid = str(card.get("id") or "").strip()
        if not sid:
            return
        self._applied_strategy_ids.add(sid)
        self.run.applied_strategy_ids = list(dict.fromkeys([*self.run.applied_strategy_ids, sid]))

    def _format_strategy_template(self, template: str, *, section: str = "", claim: str = "") -> str:
        profile = self.run.profile
        values = {
            "name": profile.name if profile else self.run.query,
            "query": self.run.query,
            "industry": profile.industry if profile else "",
            "ticker": profile.ticker if profile else "",
            "section": section,
            "claim": claim,
            "year": str(datetime.now().year),
        }
        out = template
        for key, value in values.items():
            out = out.replace("{" + key + "}", str(value or ""))
        return re.sub(r"\s+", " ", out).strip()

    def _strategy_queries(self, stage: str, *, section: str = "", claim: str = "", limit: int = 4) -> list[str]:
        queries: list[str] = []
        for card in self._stage_strategies(stage):
            action = card.get("action") if isinstance(card.get("action"), dict) else {}
            for template in action.get("query_templates") or []:
                q = self._format_strategy_template(str(template), section=section, claim=claim)
                if q:
                    queries.append(q)
                    self._mark_strategy_applied(card)
                if len(queries) >= limit:
                    return queries
        return queries

    def _add_evidence(self, ev: Evidence) -> int:
        # 入池前清洗网页 boilerplate（导航栏/分享按钮/字体选择/浏览量/面包屑等），
        # 否则 _build_report_md 与 _digest 会把 content[:160/240] 的垃圾铺进正文与 LLM 上下文。
        ev.content = _clean_evidence_text(ev.content)
        ev.source_title = (ev.source_title or "").strip()[:120]
        ev.source_url = sanitize_source_url(ev.source_url)
        # 信源权威性升级：如果 URL 指向监管/交易所/权威媒体/研报平台，
        # 但数据源适配器只给了 MEDIA(tier6)，则按 URL 升级到更准确的等级。
        # 只升级不降级——适配器已判定更高等级的（如 SEC filing）保持不变。
        if ev.source_url and int(ev.tier) >= 6:
            guessed = self._guess_tier_for_profile(ev.source_url, ev.source_title, ev.content)
            if int(guessed) < int(ev.tier):
                ev.tier = guessed
        key = self._evidence_key(ev)
        for idx, existing in self.evidence_pool.items():
            if self._evidence_key(existing) == key:
                return idx
        self._ev_counter += 1
        self.evidence_pool[self._ev_counter] = ev
        return self._ev_counter

    @staticmethod
    def _evidence_key(ev: Evidence) -> tuple[str, str, bool]:
        content = re.sub(r"\s+", "", (ev.content or "").lower())[:240]
        url = (ev.source_url or "").strip().lower()
        return url, content, bool(ev.supports)

    @staticmethod
    def _contains_evidence(items: list[Evidence], ev: Evidence) -> bool:
        return any(item.id == ev.id for item in items)

    def _build_evidence_index(self):
        """构建证据向量索引（TF-IDF + cosine），供语义检索用。
        在 Collect 阶段结束后调用。"""
        if not self.evidence_pool:
            return
        self._ev_index = EvidenceIndex()
        for idx, ev in self.evidence_pool.items():
            # 索引内容 = 标题 + 内容前 500 字（含关键数字和术语）
            text = f"{ev.source_title or ''} {ev.content or ''}"[:500]
            self._ev_index.add(idx, text)
        self._ev_index.build()

    def _semantic_search(self, query: str, top_k: int = 15) -> list[int]:
        """用向量索引检索与 query 最相关的证据编号。
        无索引时回退到全量（返回所有证据编号）。"""
        if self._ev_index and self._ev_index.size > 0:
            results = self._ev_index.search(query, top_k=top_k)
            return [idx for idx, _ in results]
        # 回退：返回全部证据编号
        return list(self.evidence_pool.keys())

    def _digest(self, ids: list[int] | None = None, only_support: bool | None = None,
                max_items: int = 80, content_len: int = 240,
                query: str = "") -> str:
        """证据摘要。ids 指定只列哪些编号；max_items 限制条数；content_len 限制每条内容长度。
        query 参数：提供时用向量检索找最相关的 top-K 证据（替代全量 dump）。
        全量(80条×240字≈27000字)塞进 narrative prompt 是 insights 段超时根因——
        facts 段用 max_items=30/content_len=200，insights 段只用引用过的证据+content_len=150。"""
        if ids is not None:
            pool = {i: self.evidence_pool[i] for i in ids if i in self.evidence_pool}
        elif query and self._ev_index and self._ev_index.size > 5:
            # 有查询上下文 + 索引可用 + 证据池>5条 → 语义检索 top-K
            semantic_ids = self._semantic_search(query, top_k=max_items)
            pool = {i: self.evidence_pool[i] for i in semantic_ids if i in self.evidence_pool}
        else:
            pool = self.evidence_pool
        items = []
        for i, ev in pool.items():
            if only_support is not None and ev.supports != only_support:
                continue
            tag = "支撑" if ev.supports else "反证"
            date = ev.as_of or ev.published_at
            when = f" · 截至{date}" if date else ""
            items.append(f"[{i}] ({ev.tier_label}/{tag}{when}) {ev.content[:content_len]} — 来源:{ev.source_url}")
        if len(items) > max_items:
            items = items[:max_items]
        return "\n".join(items) if items else "(暂无)"

    async def _chat_json(self, messages, role="analyst"):
        if self.demo:
            from demo_data import demo_chat_json
            return await asyncio.to_thread(demo_chat_json, messages, role=role)
        # 追踪实际使用的 provider
        actual = self.llm.effective_provider(role, self.provider)
        self._ev("llm_call", "info", f"LLM调用[{role}]", f"provider={actual}", role=role, provider=actual)
        return await asyncio.to_thread(
            self.llm.chat_json, messages, provider=self.provider, role=role
        )

    async def _chat_json_red(self, messages):
        if self.demo:
            from demo_data import demo_chat_json
            return await asyncio.to_thread(demo_chat_json, messages, role="red_team")
        # 红队用前端选定的 provider（self.red_team_provider），未选则 role_defaults 路由
        actual = self.llm.effective_provider("red_team", self.red_team_provider or None)
        self._ev("llm_call", "info", "红队LLM调用", f"provider={actual}", provider=actual)
        return await asyncio.to_thread(
            self.llm.chat_json, messages, provider=self.red_team_provider or None, role="red_team"
        )

    async def _chat_text(self, messages, role="analyst", timeout=120.0, retries=2,
                         max_tokens: int | None = None, provider_override: str | None = None):
        """非结构化文本输出（如完整分析文档 narrative）。
        analyst 用用户选的主分析；reviewer 用用户选的终审；其他角色走 role_defaults 路由。
        provider_override 非 None 时强制使用该 provider（正文质量闸门重写时用最强模型）。
        timeout 可调（reviewer 写长文需更长）。retries 越大底层线程占用越久
        (timeout×(retries+1)×provider数)，长文配合外层 wait_for 时降 retries 防线程池耗尽。"""
        if self.demo:
            from demo_data import demo_chat_json
            return await asyncio.to_thread(demo_chat_json, messages, role=role)
        if provider_override is not None:
            provider = provider_override
        elif role == "analyst":
            provider = self.provider
        elif role == "reviewer":
            provider = self.reviewer_provider
        else:
            provider = None
        # 追踪实际使用的 provider
        actual = self.llm.effective_provider(role, provider)
        self._ev("llm_call", "info", f"文本LLM调用[{role}]", f"provider={actual}", role=role, provider=actual)
        kwargs = {
            "provider": provider, "role": role, "timeout": timeout, "retries": retries,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        return await asyncio.to_thread(self.llm.chat, messages, **kwargs)

    def _search(self, query, n=5):
        if self.demo:
            from demo_data import demo_search
            return demo_search(query, n)
        return search_tool.search(query, n)

    def _search_available(self) -> bool:
        return self.demo or search_tool.has_search_backend()

    def _get_financials(self, ticker):
        if self.demo:
            from demo_data import demo_sec
            return demo_sec(ticker)
        return sec_edgar.get_key_financials(ticker)

    def _adapter(self, name: str):
        """获取数据源适配器（demo 模式下返回 None，走 demo 路径）。"""
        if self.demo:
            return None
        from tools.finance_sources import get_adapter
        a = get_adapter(name)
        return a if a and a.available else None

    def _counter_search(self, query, n=4, days=365):
        """证伪阶段的反证检索。

        反证需要来源多样性，不能被第一个有结果的搜索源垄断。
        P1 修复：根据分析对象类型调整数据源优先级——
        非上市公司优先通用搜索/Exa（em_news 对非金融公司覆盖差），
        上市/行业分析保持原优先级。
        days=365 默认限定近1年时效，避免命中过时信息。
        """
        if self.demo:
            from demo_data import demo_search
            return demo_search(query, n)
        rows: list[dict] = []
        seen_urls: set[str] = set()
        # 根据分析对象类型选择数据源优先级
        profile = self.run.profile
        if profile and not profile.is_public and profile.kind == "company":
            # 非上市公司：通用搜索优先（em_news 对非金融公司覆盖极差）
            source_order = ("general_search", "exa_search", "em_news", "wind", "ifind")
        else:
            # 上市公司/行业分析：金融数据源优先
            source_order = ("em_news", "wind", "ifind", "general_search", "exa_search")
        for name in source_order:
            ad = self._adapter(name)
            if not ad:
                continue
            try:
                res = ad.search(query, "news", n, days=days)
            except Exception:  # noqa: BLE001
                logger.debug("反证检索适配器 %s 失败，尝试下一个", name, exc_info=True)
                continue
            take = 1 if name == "exa_search" else max(1, min(2, n - len(rows)))
            added = 0
            for e in res.evidences:
                url = (e.source_url or "").strip().lower()
                key = url or f"{e.source_title}|{e.content[:80]}"
                if key in seen_urls:
                    continue
                seen_urls.add(key)
                rows.append({"title": e.source_title, "url": e.source_url, "content": e.content})
                added += 1
                if added >= take or len(rows) >= n:
                    break
            if len(rows) >= n:
                break
        return rows

    def _detect_listing(self, results: list[dict]) -> dict | None:
        """从搜索结果中检测上市信息（品牌名→上市主体映射）。
        匹配股票代码模式 + 上市关键词，提取上市主体名和股票代码。
        修复 badcase：蜜雪冰城→蜜雪集团(02097.HK) 未被识别。"""
        import re as _re
        if not results:
            return None
        listing_kw = ("上市", "IPO", "招股", "挂牌", "股票代码", "交所",
                      "纳斯达克", "港交所", "上交所", "深交所", "北交所")
        hk_pat = _re.compile(r'(\d{4,6})\.HK')
        a_pat = _re.compile(r'(\d{6})\.(SH|SZ|BJ)')
        us_pat = _re.compile(r'(?:NASDAQ|NYSE)[:\s]+([A-Z]{1,6})\b')
        name_pat = _re.compile(r'([\u4e00-\u9fa5]{2,8}(?:集团|股份|控股|科技|公司))')

        for r in results:
            text = ((r.get("title") or "") + " " + (r.get("content") or "")).strip()
            if not text or not any(k in text for k in listing_kw):
                continue
            ticker = ""
            m = hk_pat.search(text)
            if m:
                ticker = m.group(0)
            else:
                m = a_pat.search(text)
                if m:
                    ticker = m.group(0)
                else:
                    m = us_pat.search(text)
                    if m:
                        ticker = m.group(1)
            name = ""
            nm = name_pat.search(text)
            if nm:
                name = nm.group(1)
            if ticker or name:
                return {"ticker": ticker, "name": name, "raw": text[:200]}
        return None

    # ========== 主流程 ==========
    def _triage(self, stage: str, exc: Exception, ctx: str = "") -> tuple[str, dict]:
        """定位坏点 + 决策：重启(client 已重试)/降级(跳过保留成果)/升级(向人求助)。
        返回 (decision, diagnosis_dict)。"""
        msg = str(exc)
        low = msg.lower()
        if any(k in low for k in ("timeout", "timed out", "429", "rate limit", "connection", "transport", "readtimeout")):
            kind, decision = "瞬时错误(超时/限流/网络)", "degrade"
        elif "无法解析" in msg or "json" in low or "none" in low:
            kind, decision = "模型输出解析失败", "degrade"
        elif "没有可用的" in msg or "api key" in low or " 401" in msg or " 403" in msg or "auth" in low:
            kind, decision = "配置/鉴权错误", "escalate"
        else:
            kind, decision = "未知错误", "escalate"
        bad_point = f"{stage}阶段{('·'+ctx) if ctx else ''}"
        summary = f"{bad_point}·{kind}：{msg[:140]}"
        action = ("请检查 API Key/额度/网络后重跑该对象" if decision == "escalate"
                  else "已降级跳过并保留已产成果；该步可重跑补全")
        return decision, {
            "bad_point": bad_point, "error_type": kind, "error": msg[:300],
            "decision": decision, "summary": summary, "suggested_action": action,
        }

    async def run_pipeline(self) -> AsyncGenerator[TraceEvent, None]:
        self.run.status = "running"

        # 断点续跑：检查 checkpoint_stage，跳过已完成的阶段
        _completed = self.run.checkpoint_stage
        if _completed:
            # 恢复内部证据池
            for i, ev in enumerate(self.run.checkpoint_evidence, 1):
                self.evidence_pool[i] = ev
                self._ev_counter = i
            if self.evidence_pool:
                self._build_evidence_index()
            yield self._ev("scope", "thinking", "断点续跑",
                            f"从 {_completed} 阶段后恢复 · 证据池 {len(self.evidence_pool)} 条")

        # 开场：据可用资源（LLM/搜索/金融源）规划整条流程与 backup，并播报角色指派理由
        if not _completed:
            try:
                from llm.model_catalog import plan_resources
                from tools.search import search_status
                from tools.finance_sources import available_sources
                plan = plan_resources(self.llm.providers, self.llm._available_keys(),
                                      search_status()["status"], available_sources())
                yield self._ev("scope", "plan", "资源规划",
                                plan["reviewer_auto"],
                                **plan)
            except Exception:  # noqa: BLE001
                logger.warning("资源规划(plan_resources)失败，跳过", exc_info=True)

        # 关键阶段(scope/collect/analyze)：失败则无法继续，须升级人工
        _stages_critical = [
            (self._scope, "scope", "界定"), (self._collect, "collect", "采集"),
            (self._analyze, "analyze", "分析"),
        ]
        _stage_order = ["scope", "collect", "analyze", "falsify", "report"]
        _resume_from_idx = _stage_order.index(_completed) + 1 if _completed else 0
        for stage_fn, stage, label in _stages_critical[_resume_from_idx:]:
            self.llm.set_stage(stage)
            try:
                async for e in stage_fn():
                    yield e
                if self.run.status == "needs_human":
                    self._finalize_token_summary()
                    save_run(self.run)
                    return
                self._save_checkpoint(stage)
            except Exception as exc:  # noqa: BLE001
                decision, diag = self._triage(stage, exc)
                yield self._ev(stage, "diagnosis", f"{label}出错·{ {'degrade':'降级','escalate':'向人求助'}[decision] }",
                               diag["summary"], **diag)
                self.run.status = "needs_human"
                self.run.error = diag["summary"]
                self._finalize_token_summary()
                self._save_checkpoint(stage)
                return

        # 非关键阶段(证伪/报告)：失败不丢前面的成果，降级保留
        _stages_non_critical = [
            (self._falsify_and_refine, "falsify", "证伪"), (self._report, "report", "报告"),
        ]
        _resume_non_critical = _resume_from_idx - 3 if _resume_from_idx > 3 else 0
        for stage_fn, stage, label in _stages_non_critical[_resume_non_critical:]:
            self.llm.set_stage(stage)
            try:
                async for e in stage_fn():
                    yield e
                self._save_checkpoint(stage)
            except Exception as exc:  # noqa: BLE001
                decision, diag = self._triage(stage, exc)
                yield self._ev(stage, "diagnosis", f"{label}出错·{ {'degrade':'降级保留','escalate':'向人求助'}[decision] }",
                               diag["summary"], **diag)
                self._save_checkpoint(stage)
                if decision == "escalate":
                    self.run.status = "needs_human"
                    self.run.error = diag["summary"]
                    self._finalize_token_summary()
                    self._save_checkpoint(stage)
                    return
                # degrade：保留已产洞察，标记部分完成

        if self.run.status == "running":
            self.run.status = "done"
            self.run.checkpoint_stage = ""
            # P1.6: 输出 token 成本摘要
            _ts = self._finalize_token_summary()
            if _ts["total_calls"] > 0:
                yield self._ev("report", "token_summary", "Token 成本摘要",
                                f"调用 {_ts['total_calls']} 次 · 输入 {_ts['total_input']} / 输出 {_ts['total_output']} · 估算 ${_ts['total_cost_usd']:.4f}",
                                **_ts)
            self.run.applied_strategy_ids = list(self._applied_strategy_ids)
            self.run.strategy_effect = {"applied_count": len(self.run.applied_strategy_ids), "effect_updated": False}
            self.run.run_metrics = build_run_metrics(self.run)
            try:
                memory_store.update_strategy_effects(self.run.applied_strategy_ids, self.run.run_metrics)
                self.run.strategy_effect["effect_updated"] = bool(self.run.applied_strategy_ids)
                self.run.run_metrics["strategy_effect"] = self.run.strategy_effect
            except Exception:  # noqa: BLE001
                logger.warning("更新 strategy card 效果失败，跳过", exc_info=True)
            try:
                _metrics_path = write_run_metrics(self.run)
                yield self._ev("report", "metrics", "运行指标已写入", str(_metrics_path),
                               path=str(_metrics_path))
            except Exception as exc:  # noqa: BLE001
                yield self._ev("report", "diagnosis", "运行指标写入失败", _safe_str(exc, 200))
            yield self._ev("report", "done", "分析完成", f"共 {len(self.run.insights)} 条洞察")
        elif self.run.status != "needs_human":
            self.run.status = "partial"
            _ts = self._finalize_token_summary()
            if _ts["total_calls"] > 0:
                yield self._ev("report", "token_summary", "Token 成本摘要",
                                f"调用 {_ts['total_calls']} 次 · 输入 {_ts['total_input']} / 输出 {_ts['total_output']} · 估算 ${_ts['total_cost_usd']:.4f}",
                                **_ts)
            self.run.applied_strategy_ids = list(self._applied_strategy_ids)
            self.run.strategy_effect = {"applied_count": len(self.run.applied_strategy_ids), "effect_updated": False}
            self.run.run_metrics = build_run_metrics(self.run)
            try:
                memory_store.update_strategy_effects(self.run.applied_strategy_ids, self.run.run_metrics)
                self.run.strategy_effect["effect_updated"] = bool(self.run.applied_strategy_ids)
                self.run.run_metrics["strategy_effect"] = self.run.strategy_effect
            except Exception:  # noqa: BLE001
                logger.warning("更新 strategy card 效果失败，跳过", exc_info=True)
            try:
                _metrics_path = write_run_metrics(self.run)
                yield self._ev("report", "metrics", "运行指标已写入", str(_metrics_path),
                               path=str(_metrics_path))
            except Exception as exc:  # noqa: BLE001
                yield self._ev("report", "diagnosis", "运行指标写入失败", _safe_str(exc, 200))
            yield self._ev("report", "done", "部分完成",
                           f"证伪/报告阶段降级，已保留 {len(self.run.insights)} 条洞察")
        save_run(self.run)

    def _finalize_token_summary(self) -> dict:
        """Persist the token summary even when a run exits via needs_human."""
        _ts = self.llm.token_tracker.summary()
        self.run.token_summary = _ts
        return _ts

    def _save_checkpoint(self, stage: str):
        """保存检查点到 SQLite，支持断点续跑。"""
        try:
            self.run.checkpoint_stage = stage
            self.run.checkpoint_evidence = [self.evidence_pool[i] for i in sorted(self.evidence_pool)]
            save_run(self.run)
        except Exception:  # noqa: BLE001
            logger.warning("checkpoint 保存失败 stage=%s", stage, exc_info=True)  # 不影响主流程

    # ---------- ① Scope ----------
    async def _scope(self) -> AsyncGenerator[TraceEvent, None]:
        yield self._ev("scope", "thinking", "界定研究范围",
                        f"判断 “{self.run.query}” 的对象类型、行业、是否上市，并量身定制分析维度")
        data = await self._chat_json(prompts.scope_prompt(self.run.query))
        data = data if isinstance(data, dict) else {}
        tpl_key = pick_template(data.get("industry", ""))
        sections = data.get("sections") or []
        if not sections:
            # 降级：用模板默认维度
            sections = get_template(tpl_key).get("default_sections") or [
                "财务表现", "经营指标", "战略动向", "风险与展望"
            ]
        # 防御 LLM 输出非规范名称（如"期待着抖音的母公司"而非"字节跳动"）
        _raw_name = (data.get("name") or "").strip()
        # 规范公司/行业名通常 ≤20 字符，不含常见动词/叙述性词汇
        _NARRATIVE_MARKERS = ("期待", "关于", "分析", "介绍", "研究", "的母公司", "值得", "看好", "认为")
        if (not _raw_name
                or len(_raw_name) > 25
                or any(m in _raw_name for m in _NARRATIVE_MARKERS)):
            _raw_name = self.run.query
        profile = ObjectProfile(
            name=_raw_name,
            kind=data.get("kind", "company"),
            is_public=bool(data.get("is_public", True)),
            ticker=data.get("ticker", "") or "",
            industry=data.get("industry", ""),
            business_model=data.get("business_model", ""),
            template_key=tpl_key,
            peers=data.get("peers", []),
            leaders=data.get("leaders", []),
            key_questions=data.get("key_questions", []),
            sections=sections,
        )
        self.run.profile = profile

        # —— 品牌名→上市主体搜索验证 ——
        # 修复 badcase：用户搜"蜜雪冰城"，对应上市主体是"蜜雪集团"(02097.HK)，
        # 但 LLM 可能不知道该品牌已上市（特别是近期 IPO 的），误判 is_public=false。
        # 当 LLM 判非上市时，主动搜索验证是否有上市主体。
        if (not profile.is_public and profile.kind == "company"
                and self._search_available()):
            listing_q = f"{self.run.query} 上市 股票代码 IPO 招股书 母公司 港股 A股"
            yield self._ev("scope", "search", "品牌上市主体验证",
                            f"搜索「{self.run.query}」是否有上市主体", query=listing_q)
            try:
                _listing_results = await asyncio.to_thread(
                    self._counter_search, listing_q, 5, 730)
            except Exception:  # noqa: BLE001
                _listing_results = []
            listing = self._detect_listing(_listing_results)
            if listing:
                profile.is_public = True
                if listing.get("ticker") and not profile.ticker:
                    profile.ticker = listing["ticker"]
                if listing.get("name") and profile.name == self.run.query:
                    profile.name = listing["name"]
                yield self._ev("scope", "thinking",
                                f"发现上市主体：{profile.name}",
                                f"「{self.run.query}」对应上市主体 {profile.name}"
                                f"（{profile.ticker or '代码待补'}），已修正为上市公司",
                                profile=profile.model_dump())

        # 非必选：加载历史外部评测发现，作为本次 Report 阶段的通用改进提示。
        # 不是针对性复用具体 gaps，而是泛化提取 Agent 常见不足，让 LLM 写报告时自知。
        # 读取失败/无历史评测时静默跳过，不影响 pipeline。
        _eval_hints: list[str] = []
        try:
            _past_evals = memory_store.load_eval_feedback(self.run.query, limit=3)
            if _past_evals:
                _eval_hints = memory_store.generalize_eval_weaknesses(_past_evals)
                if _eval_hints:
                    yield self._ev(
                        "scope", "thinking",
                        f"历史评测参考（{len(_past_evals)} 条）",
                        "过往外部评测中 Agent 的常见不足（作为本次写作自知，非强制）：\n"
                        + "\n".join(f"- {h}" for h in _eval_hints[:6]),
                        eval_feedback_count=len(_past_evals),
                        eval_feedback_hints=_eval_hints[:6],
                    )
        except Exception:  # noqa: BLE001
            pass

        tpl = get_template(tpl_key)
        yield self._ev(
            "scope", "scope_done", f"对象：{profile.name}",
            f"{'上市' if profile.is_public else '非上市'} · {profile.industry} · 框架【{tpl['label']}】",
            profile=profile.model_dump(), template=tpl["label"], sections=sections,
        )
        if not profile.is_public and profile.kind == "company":
            yield self._ev("scope", "thinking", "非上市公司适配",
                            "公开数据稀疏，叠加估值/融资框架，强制标注数据可信度")
        if profile.kind == "industry":
            ld = ", ".join(profile.leaders[:3]) if profile.leaders else "（待补）"
            n_sec = len(profile.sections) if profile.sections else 0
            yield self._ev("scope", "thinking", "行业研究适配",
                            f"从12视角池选 {n_sec} 个维度：{', '.join(profile.sections[:4])}{'…' if n_sec > 4 else ''} · "
                            f"龙头锚点【{ld}】（业绩交叉验证）")

        # P2.7: Human-in-the-loop —— Scope 完成后发出审查点
        yield self._ev("scope", "review_point", "分析维度已生成（可确认/编辑后继续）",
                        f"对象：{profile.name} · {len(profile.sections or [])} 个维度",
                        review_type="scope",
                        sections=profile.sections or [],
                        profile=profile.model_dump())

        # 等待用户确认（超时 300s 自动继续，防止前端无响应时 pipeline 永久阻塞）
        try:
            await asyncio.wait_for(self._scope_confirmed.wait(), timeout=300.0)
            # 用户确认后可能修改了维度
            if self._scope_user_sections is not None:
                profile.sections = self._scope_user_sections
                self.run.profile = profile
                yield self._ev("scope", "thinking", "用户已确认/调整分析维度",
                                f"最终维度 ({len(profile.sections)})：{', '.join(profile.sections[:5])}{'…' if len(profile.sections) > 5 else ''}")
            else:
                yield self._ev("scope", "thinking", "用户已确认分析维度", "按原定维度继续")
        except asyncio.TimeoutError:
            yield self._ev("scope", "thinking", "审查点超时，自动继续",
                            "等待 5 分钟无响应，按原定维度继续")

        # —— 三层记忆·Domain：加载行业知识框架 ——
        self.industry_rag: dict = {}
        try:
            self.industry_rag = memory_store.load_industry_rag(tpl_key)
            n_pitfalls = len(self.industry_rag.get("failure_playbook", []))
            n_dims = len(self.industry_rag.get("industry_playbook", {}).get("key_dimensions", []))
            if n_pitfalls or n_dims:
                yield self._ev("scope", "thinking", "加载行业知识框架(Domain Memory)",
                                f"{n_dims} 维度 · {n_pitfalls} 条失败模式 · "
                                f"{len(self.industry_rag.get('evidence_playbook', {}).get('cross_validation_rules', []))} 条交叉验证规则")
        except Exception:  # noqa: BLE001
            logger.warning("加载 Industry RAG 记忆失败，跳过", exc_info=True)  # 记忆系统可选，不阻塞主流程

        # Active Strategy Store: load only after profile/sections are known.
        # These cards are deterministic, gated strategies that can change
        # future search/review behavior; they are not merely reflection logs.
        self._load_active_strategies()

    # ---------- ② Collect ----------
    async def _harvest(self, adapter, query, *, kind="news", max_results=5, take=3,
                        days=None, source=None, title_fn=None, content_len=160,
                        seen_urls=None, emit_search=None):
        """统一的"搜索→入池→产出证据事件"采集块，替代 _collect 中重复 7 次的样板。

        adapter: 数据源适配器（须有 .search(query, kind, max_results, days) → .evidences）
        take: 每个查询采纳的证据条数
        title_fn(idx, ev) -> str: 自定义 evidence 事件标题；缺省 "证据 [idx] · 标题前30字"
        seen_urls: 传入一个 set 时按 URL 跨查询去重（None 则不去重）
        emit_search: 传入字符串时先产出一条 search 事件
        以 async generator 形式逐事件 yield。
        """
        if emit_search is not None:
            yield self._ev("collect", "search", emit_search, query,
                            query=query, source=source or adapter.name)
        res = await asyncio.to_thread(adapter.search, query, kind, max_results, days=days)
        for ev in res.evidences[:take]:
            if seen_urls is not None:
                if ev.source_url in seen_urls:
                    continue  # 跨查询/中英查询可能返回相同 URL
                seen_urls.add(ev.source_url)
            idx = self._add_evidence(ev)
            if title_fn is not None:
                title = title_fn(idx, ev)
            else:
                title = f"证据 [{idx}] · {(ev.source_title or '')[:30]}"
            yield self._ev("collect", "evidence", title,
                            (ev.content or "")[:content_len] if content_len else ev.content,
                            evidence_id=idx, url=ev.source_url, tier=ev.tier_label,
                            source=source or adapter.name, published=ev.published_at,
                            as_of=ev.as_of)

    async def _collect(self) -> AsyncGenerator[TraceEvent, None]:
        profile = self.run.profile
        tpl = get_template(profile.template_key)
        sources_used: list[str] = []

        if self.demo:
            # 演示模式：用脚本化数据跑通多源采集
            async for e in self._collect_demo(profile):
                yield e
            sources_used = self.run.data_sources_used
        else:
            # —— 数据源 1：金融数据 API（Wind / NeoData / iFinD）—— 结构化行情+财报+公告+研报
            fin_api = self._adapter("wind") or self._adapter("neodata") or self._adapter("ifind")
            _cy = datetime.now().year
            _prev_y = _cy - 1
            if fin_api:
                sources_used.append(fin_api.name)
                queries = [
                    f"{profile.name} {_cy}年 最新季报 营收 净利润 毛利率",
                    f"{profile.name} {_prev_y}年 年报 营业收入 净利润 全年业绩",
                    f"{profile.name} 近三年 营业收入 净利润 增速 趋势",
                    f"{profile.name} 重大公告 事件",
                    f"{profile.name} 机构评级 目标价",
                ]
                for q in queries:
                    kind = "notice" if "公告" in q else "research" if "评级" in q else "financial"
                    async for e in self._harvest(
                        fin_api, q, kind=kind, max_results=5, take=3,
                        source=fin_api.name,
                        emit_search=f"金融数据源检索({fin_api.name})"):
                        yield e

            # —— 数据源 2：SEC EDGAR（美股上市）—— 一手 Filing + 多年财务时序
            if profile.is_public and profile.ticker and _is_sec_ticker(profile.ticker) and self._adapter("sec_edgar"):
                sources_used.append("sec_edgar")
                yield self._ev("collect", "search", "调取 SEC 财报",
                                f"EDGAR 拉取 {profile.ticker} 标准化财务事实（10-K + 10-Q）", source="sec_edgar")
                sec = self._adapter("sec_edgar")
                # 拉取 8 期财务事实（覆盖近2年季度+年度），确保拿到最新 filing
                res = await asyncio.to_thread(sec.search, profile.ticker, "filing", 8)
                for ev in res.evidences:
                    idx = self._add_evidence(ev)
                    yield self._ev("collect", "evidence", f"财报数据 [{idx}]",
                                    ev.content, evidence_id=idx, tier=ev.tier_label,
                                    source="sec_edgar", as_of=ev.as_of)
                # 构建多年财务时序（供报告图表：营收/净利润/经营利润 × 年度 + 增速）
                self.run.financials = self._build_financials(res.structured)
                if self.run.financials:
                    yield self._ev("collect", "thinking", "财务时序已构建",
                                    f"{len(self.run.financials)} 期 · 最新: {self.run.financials[0]['period']}"
                                    if self.run.financials else "")

            # —— 数据源 2b：港股披露易 / A股财报公告（非美股上市公司）——
            if profile.is_public and profile.ticker and not _is_sec_ticker(profile.ticker):
                _is_hk = ".HK" in profile.ticker.upper() or profile.ticker.upper().startswith("0") and len(profile.ticker.split(".")[0]) == 5
                _filing_adapter = self._adapter("hkex_disclosure") if _is_hk else self._adapter("a_share_filing")
                if _filing_adapter:
                    sources_used.append("hkex_disclosure" if _is_hk else "a_share_filing")
                    _filing_q = f"{profile.name} {profile.ticker} 年报 财报 业绩"
                    yield self._ev("collect", "search",
                                    f"{'港股披露易' if _is_hk else 'A股财报公告'}搜索",
                                    _filing_q, source="hkex_disclosure" if _is_hk else "a_share_filing")
                    _filing_res = await asyncio.to_thread(
                        _filing_adapter.search, _filing_q, "filing", 5, 730)
                    for ev in _filing_res.evidences:
                        idx = self._add_evidence(ev)
                        if idx is not None:
                            yield self._ev("collect", "evidence",
                                            f"财报公告 [{idx}]",
                                            ev.content[:140], evidence_id=idx,
                                            tier=ev.tier_label,
                                            source="hkex_disclosure" if _is_hk else "a_share_filing",
                                            url=ev.source_url)

            # —— 非上市公司专用检索：融资/估值/股东/牌照/用户规模/关联交易（公开数据稀疏，定向补）——
            # P0 修复：优先通用搜索/Exa（覆盖面广），em_news 仅作兜底（金融资讯库对非上市互联网公司覆盖极差）
            if not profile.is_public and profile.kind == "company":
                priv_queries = [
                    f"{profile.name} 融资轮次 估值 投资方",
                    f"{profile.name} 股东 股权结构 关联交易",
                    f"{profile.name} 牌照 监管 合规",
                    f"{profile.name} 用户规模 DAU GMV 营收 量级",
                ]
                # 优先通用/语义搜索（Exa/Tavily/Serper），Firecrawl 补位，em_news 兜底
                priv_src = (self._adapter("general_search")
                            or self._adapter("exa_search")
                            or self._adapter("firecrawl")
                            or self._adapter("em_news")
                            or fin_api)
                if priv_src:
                    if priv_src.name not in sources_used:
                        sources_used.append(priv_src.name)
                    yield self._ev("collect", "search", "非上市公司定向检索",
                                    f"融资/估值/股东/牌照/规模（数据稀疏，强制标注可信度）", source=priv_src.name)
                    for q in priv_queries:
                        async for e in self._harvest(
                            priv_src, q, kind="news", max_results=4, take=2,
                            source=priv_src.name):
                            yield e

            # —— 行业研究专用检索：按行业框架四维度采集（锚定监管最新口径 + 命名龙头业绩交叉验证）——
            # 修复要点：① 注入当前年月保证时效（央行社融/信贷月度数据）② 用 profile.leaders 按名查龙头财报
            #         ③ 查询映射到框架四维度：总量增速→产业链/集中度→龙头业绩→趋势风险
            if profile.kind == "industry":
                _now = datetime.now()
                _yy, _mm = _now.year, _now.month
                # 央行/金融监管总局通常每月中旬披露上月数据；可安全引用的最新月份
                _latest_m = _mm - 1 if _mm > 1 else 12
                _latest_y = _yy if _mm > 1 else _yy - 1
                leaders = profile.leaders or profile.peers[:3]
                ind_src = self._adapter("wind") or self._adapter("em_news") or self._adapter("general_search")
                # ① 按 sections 维度逐个采集（Scope 为该行业量身定制的维度）
                if ind_src and profile.sections:
                    if ind_src.name not in sources_used:
                        sources_used.append(ind_src.name)
                    yield self._ev("collect", "search", "行业定向检索·按分析维度",
                                    f"按 {len(profile.sections)} 个量身定制维度采集（锚定{_yy}年最新数据）",
                                    source=ind_src.name)
                    for sec in profile.sections:
                        q = f"{profile.name} {sec} {_yy}年 数据 趋势"
                        async for e in self._harvest(
                            ind_src, q, kind="news", max_results=5, take=3,
                            source=ind_src.name,
                            title_fn=lambda idx, ev, sec=sec: f"证据 [{idx}] · {sec} · {(ev.source_title or '')[:28]}"):
                            yield e
                # ② 当月最新数据（报价/出口/政策）—— 修复数据时效，确保拿到最新月份
                if ind_src:
                    yield self._ev("collect", "search", "最新数据检索",
                                    f"当月最新报价/出口/政策（锚定{_yy}年{_latest_m}月）", source=ind_src.name)
                    for q in (
                        f"{profile.name} 价格 报价 {_yy}年{_latest_m}月 最新",
                        f"{profile.name} 出口 进口 {_yy}年 同比",
                        f"{profile.name} 政策 监管 {_yy}年 最新",
                    ):
                        async for e in self._harvest(
                            ind_src, q, kind="news", max_results=4, take=2,
                            source=ind_src.name,
                            title_fn=lambda idx, ev: f"证据 [{idx}] · 最新 · {(ev.source_title or '')[:28]}"):
                            yield e
                # ③ 龙头公司业绩交叉验证 —— 按名查询，每家取更多证据（它们是行业判断的锚点）
                if leaders:
                    ld_src = fin_api or self._adapter("general_search") or ind_src
                    if ld_src:
                        if ld_src.name not in sources_used:
                            sources_used.append(ld_src.name)
                        yield self._ev("collect", "search", "龙头业绩交叉验证",
                                        f"按名查询 {', '.join(leaders[:3])} 最新财报/营收/份额（行业判断锚点）", source=ld_src.name)
                        for ld in leaders[:4]:
                            for q in (
                                f"{ld} {_yy} 最新 营收 净利润 财报 业绩",
                                f"{ld} 市场份额 行业排名 规模",
                            ):
                                _kind = "financial" if (fin_api and ld_src is fin_api) else "news"
                                async for e in self._harvest(
                                    ld_src, q, kind=_kind, max_results=4, take=2,
                                    source=ld_src.name,
                                    title_fn=lambda idx, ev, ld=ld: f"证据 [{idx}] · {ld} · {(ev.source_title or '')[:28]}"):
                                    yield e

            # —— 数据源 3：通用搜索（新闻/媒体/行业/监管）—— 公开信息广度
            # 优先级：Exa（免费语义搜索）→ Tavily/Serper（付费结构化搜索）
            # 新闻/动态类查询限定近 180 天时效，趋势/历史类不限
            gen = self._adapter("general_search")
            exa = self._adapter("exa_search")
            _cur_year = datetime.now().year
            # 区分时效查询 vs 历史趋势查询
            _RECENCY_KEYWORDS = ("最新", "近期", "动态", "新闻", "公告", str(_cur_year), str(_cur_year - 1), "current", "latest")

            # Exa 语义搜索作为主搜索源（免费、语义质量高、中英文混合友好）
            # P0.1: 中英双语查询提升召回——Exa英文召回远优于中文，自动生成英文变体
            # 财报搜索优化：包含最新季度/10-K/10-Q关键词，确保拿到最新财报而非旧数据
            if exa:
                sources_used.append("exa_search")
                # 计算最新可能的季度（当前月份推算）
                _now_m = datetime.now().month
                _latest_q = f"Q{(_now_m - 1) // 3 + 1}" if _now_m > 1 else "Q4"
                # 中英双语查询对：中文查一次+英文查一次，合并去重
                # P1 修复：非上市公司去掉金融术语（季报/10-K），改用业务指标查询词
                if profile.is_public:
                    _exa_query_pairs = [
                        (f"{profile.name} {profile.industry} {_cur_year} 最新动态",
                         f"{profile.name} {profile.industry} {_cur_year} latest news"),
                        (f"{profile.name} {_cur_year} 最新季报 营收 净利润 财报 10-K",
                         f"{profile.name} {_cur_year} quarterly earnings 10-K 10-Q revenue net income"),
                        (f"{profile.name} 历年 营收 净利润 增速 5年 财务数据",
                         f"{profile.name} annual revenue net income history 5 year financial data"),
                    ]
                else:
                    _exa_query_pairs = [
                        (f"{profile.name} {profile.industry} {_cur_year} 最新动态 发展",
                         f"{profile.name} {profile.industry} {_cur_year} latest news development"),
                        (f"{profile.name} {_cur_year} 融资 估值 投资方 投资人",
                         f"{profile.name} {_cur_year} funding valuation investors"),
                        (f"{profile.name} 用户规模 DAU MAU GMV 市场份额 商业模式",
                         f"{profile.name} user base DAU MAU GMV market share business model"),
                        (f"{profile.name} 营收 收入 盈利 财务 规模",
                         f"{profile.name} revenue profit financial scale"),
                    ]
                if profile.industry:
                    _exa_query_pairs.append(
                        (f"{profile.industry} 行业 增速 拐点 {_cur_year}",
                         f"{profile.industry} industry growth trend inflection {_cur_year}"))
                _exa_seen_urls: set[str] = set()
                for q_cn, q_en in _exa_query_pairs[:5]:
                    for q in (q_cn, q_en):
                        _is_recent = any(kw in q for kw in _RECENCY_KEYWORDS)
                        _days = (365 if "10-K" in q or "财报" in q or "financial" in q.lower()
                                 else (180 if _is_recent else None))
                        async for e in self._harvest(
                            exa, q, kind="general", max_results=5, take=3, days=_days,
                            source="exa_search", seen_urls=_exa_seen_urls,
                            emit_search="Exa语义搜索",
                            title_fn=lambda idx, ev: f"证据 [{idx}] · Exa · {(ev.source_title or '')[:28]}"):
                            yield e
            # Tavily/Serper 作为补充搜索源（结构化内容提取好，但有额度限制）
            # P0.1: 增加query reformulation——对关键查询自动生成同义变体提升召回
            if gen:
                if "general_search" not in sources_used:
                    sources_used.append("general_search")
                plan = await self._chat_json(prompts.search_plan_prompt(
                    profile.model_dump_json(), tpl))
                queries = plan if isinstance(plan, list) else plan.get("queries", [])
                queries = queries + [
                    f"{profile.name} 历年 营收 增速 趋势",
                    f"{profile.industry} 行业 增速 拐点" if profile.industry else "",
                ]
                _gen_seen_urls: set[str] = set()
                for q in [x for x in queries if x][:8]:
                    _is_recent = any(kw in q for kw in _RECENCY_KEYWORDS)
                    async for e in self._harvest(
                        gen, q, kind="news", max_results=5, take=3,
                        days=180 if _is_recent else None,
                        source="general_search", seen_urls=_gen_seen_urls,
                        emit_search="公开信息检索",
                        title_fn=lambda idx, ev: f"证据 [{idx}] · {(ev.source_title or '')[:36]}"):
                        yield e
            if (profile.is_public and profile.ticker and not _is_sec_ticker(profile.ticker)
                    and not (gen or exa or fin_api)):
                yield self._ev("collect", "thinking", "非美股财报源缺失",
                                f"{profile.ticker} 不是 SEC EDGAR 支持的美股代码；需配置 Exa/Tavily/Serper 或港股/A股金融数据源")
            if not gen and not exa and not sources_used:
                # 最后尝试 Firecrawl 作为 emergency fallback（免费层）
                _fc = self._adapter("firecrawl")
                if _fc:
                    sources_used.append("firecrawl")
                    yield self._ev("collect", "search", "Firecrawl 兜底搜索",
                                    "Exa/Tavily/Serper 均不可用，启用 Firecrawl 免费层搜索", source="firecrawl")
                    _fc_queries = [
                        f"{profile.name} {profile.industry or ''} {_cur_year} 最新动态",
                        f"{profile.name} 营收 增速 财报 业绩",
                    ]
                    _fc_seen: set[str] = set()
                    for q in _fc_queries:
                        async for e in self._harvest(
                            _fc, q, kind="general", max_results=5, take=3,
                            source="firecrawl", seen_urls=_fc_seen,
                            emit_search="Firecrawl搜索"):
                            yield e
                else:
                    yield self._ev("collect", "thinking", "无可用数据源",
                                    "未配置任何搜索/金融数据源 Key，仅能基于有限信息分析")
            self.run.data_sources_used = sources_used

        # 数据截至时点：取最新证据的 published_at / as_of；无时点兜底当前日期
        latest = max(
            (e.as_of or e.published_at for e in self.evidence_pool.values()
             if (e.as_of or e.published_at)),
            default="",
        )
        if not latest:
            from datetime import date as _date
            latest = _date.today().isoformat()
        self.run.data_as_of = latest

        if not self.evidence_pool:
            self.run.evidence_pool = []
            self.run.collect_quality = evaluate_collect_quality(self.run, self.evidence_pool.values())
            _cq = self.run.collect_quality
            _issue_note = "；".join(_cq.get("issues", [])[:2]) or "证据池覆盖良好"
            yield self._ev("collect", "quality_check", "采集质量自检",
                            f"{_cq.get('score', 0)}/100 · {_cq.get('status', 'unknown')} · "
                            f"{_cq.get('evidence_count', 0)} 条证据 · {_issue_note}",
                            collect_quality=_cq)
            try:
                _search_state = search_tool.search_status()
            except Exception:  # noqa: BLE001
                _search_state = {}
            gate_msg = _collect_gate_message(self.run, 0, _search_state)
            self.run.status = "needs_human"
            self.run.error = gate_msg
            yield self._ev("collect", "diagnosis", "采集质量门禁未通过", gate_msg,
                            evidence_count=0, sources=sources_used, search_status=_search_state)
            yield self._ev("collect", "collect_done", "采集未通过",
                            f"共 0 条证据 · 数据源: {', '.join(sources_used) or '无'} · 已停止后续分析",
                            sources=sources_used, as_of=latest)
            return

        # A股/港股等无 SEC 结构化数据时，从证据文本用 LLM 抽取多年财务时序（供图表）
        if not self.run.financials and self.run.profile and self.run.profile.is_public:
            try:
                yield self._ev("collect", "thinking", "抽取财务时序",
                                "SEC 无结构化财报，从证据文本抽取营收/净利润多年时序用于图表")
                data = await self._chat_json(prompts.extract_financials_prompt(
                    self.run.profile.model_dump_json(), self._digest()), role="analyst")
                rows = data if isinstance(data, list) else (data.get("financials") if isinstance(data, dict) else [])
                parsed = self._parse_financial_rows(rows)
                if parsed:
                    self.run.financials = parsed
            except Exception:  # noqa: BLE001
                logger.warning("财务时序抽取失败，跳过图表数据", exc_info=True)
        # 行业分析：从证据文本抽取行业总量指标 + 产品价格时序（供行业图表）
        if not self.run.industry_metrics and self.run.profile and self.run.profile.kind == "industry":
            try:
                yield self._ev("collect", "thinking", "抽取行业数据时序",
                                "从证据文本抽取行业总量指标 + 主要产品价格多年时序用于图表")
                data = await self._chat_json(prompts.extract_industry_data_prompt(
                    self.run.profile.model_dump_json(), self._digest()), role="analyst")
                if isinstance(data, dict):
                    totals = data.get("totals") or []
                    prices = data.get("prices") or []
                    if totals or prices:
                        self.run.industry_metrics = {"totals": totals, "prices": prices}
                        yield self._ev("collect", "thinking", "行业数据时序已抽取",
                                        f"{len(totals)} 个总量指标 + {len(prices)} 个产品价格")
            except Exception:  # noqa: BLE001
                logger.warning("行业数据时序抽取失败，跳过图表数据", exc_info=True)
        # 公司分析：从证据文本抽取业务板块营收拆分（供饼图）
        if (not self.run.revenue_segments.get("segments")
                and self.run.profile and self.run.profile.kind == "company"):
            try:
                yield self._ev("collect", "thinking", "抽取业务板块营收结构",
                                "从证据文本抽取业务板块/分部营收拆分用于饼图")
                seg_data = await self._chat_json(prompts.extract_revenue_segments_prompt(
                    self.run.profile.model_dump_json(), self._digest()), role="analyst")
                if isinstance(seg_data, dict) and seg_data.get("segments"):
                    self.run.revenue_segments = seg_data
                    yield self._ev("collect", "thinking", "业务板块营收结构已抽取",
                                    f"{len(seg_data['segments'])} 个板块 · FY{seg_data.get('fiscal_year', '?')}")
            except Exception:  # noqa: BLE001
                logger.warning("业务板块营收抽取失败，跳过饼图", exc_info=True)
        # 公司分析：给 peers 拉 SEC 财务数据（供同行对比柱状图）
        if (not self.run.peer_financials
                and self.run.profile and self.run.profile.kind == "company"
                and self.run.profile.peers
                and self._adapter("sec_edgar")):
            try:
                from tools.sec_edgar import get_key_financials as _peer_fin
                peer_rows = []
                for peer_name in self.run.profile.peers[:4]:  # 最多取 4 个 peers
                    # peers 可能是公司名不是 ticker，尝试直接用或推断
                    _pt = peer_name.strip().upper().replace(" ", "")
                    pfin = await asyncio.to_thread(_peer_fin, _pt)
                    if pfin.get("error"):
                        continue
                    concepts = pfin.get("concepts", {})
                    # 取最新年度(FY)营收和净利润
                    rev_row = next((r for r in (concepts.get("Revenue") or [])
                                    if r.get("period_type") == "annual" and r.get("val") is not None), None)
                    ni_row = next((r for r in (concepts.get("NetIncome") or [])
                                   if r.get("period_type") == "annual" and r.get("val") is not None), None)
                    if rev_row or ni_row:
                        peer_rows.append({
                            "name": peer_name,
                            "ticker": pfin.get("ticker", _pt),
                            "revenue": rev_row["val"] if rev_row else None,
                            "net_income": ni_row["val"] if ni_row else None,
                            "period": (rev_row or ni_row or {}).get("end", ""),
                        })
                # 主体自身也加入对比
                if peer_rows and self.run.financials:
                    latest_fy = next((f for f in reversed(self.run.financials)
                                      if f.get("revenue") is not None), None)
                    if latest_fy:
                        peer_rows.insert(0, {
                            "name": self.run.profile.name,
                            "ticker": self.run.profile.ticker,
                            "revenue": latest_fy.get("revenue"),
                            "net_income": latest_fy.get("net_income"),
                            "period": latest_fy.get("period", ""),
                            "is_subject": True,
                        })
                if peer_rows:
                    self.run.peer_financials = peer_rows
                    yield self._ev("collect", "thinking", "同行财务数据已拉取",
                                    f"{len(peer_rows)} 家公司对比 · 来源 SEC EDGAR")
            except Exception:  # noqa: BLE001
                logger.warning("同行财务数据拉取失败，跳过对比图", exc_info=True)
        # 借鉴 local-deep-researcher：LLM 驱动的知识 gap 反思（补搜前识别真正缺口）
        # 失败降级到下面的常规覆盖度补搜，不影响主流程
        if profile.sections and 5 <= len(self.evidence_pool) < 60:
            _gap_gen = self._adapter("general_search") or self._adapter("exa_search") or self._adapter("em_news")
            if _gap_gen:
                try:
                    _ev_digest = "\n".join(
                        f"- [{i}] {(e.source_title or '')[:40]}: {(e.content or '')[:80]}"
                        for i, e in list(self.evidence_pool.items())[:30])
                    _gap = await asyncio.wait_for(self._chat_json(
                        prompts.research_gap_prompt(profile.sections, _ev_digest, profile.name),
                        role="reviewer"), timeout=45.0)
                    _gap = _gap if isinstance(_gap, dict) else {}
                    _gap_queries = [str(q).strip() for q in (_gap.get("queries") or []) if str(q).strip()][:4]
                    if _gap_queries:
                        _gap_list = [str(g) for g in (_gap.get("gaps") or [])]
                        yield self._ev("collect", "search", "知识缺口识别",
                                        f"{len(_gap_queries)} 个缺口：{', '.join(g[:20] for g in _gap_list[:3])}")
                        for gq in _gap_queries:
                            try:
                                res = await asyncio.to_thread(_gap_gen.search, gq, "news", 4, days=365)
                                for ev in res.evidences[:2]:
                                    idx = self._add_evidence(ev)
                                    yield self._ev("collect", "evidence", f"缺口补搜 [{idx}]",
                                                    ev.content[:160], evidence_id=idx,
                                                    url=ev.source_url, tier=ev.tier_label,
                                                    source=_gap_gen.name, published=ev.published_at)
                            except Exception:  # noqa: BLE001
                                logger.warning("缺口补搜失败 query=%s", gq, exc_info=True)
                except Exception:  # noqa: BLE001
                    logger.warning("知识缺口反思失败，走常规覆盖度补搜", exc_info=True)

        # 借鉴 Lixin-TU：多轮搜索——检查 sections 覆盖度，不足的补搜（最多1轮）
        if profile.sections and len(self.evidence_pool) < 50:
            _gen = self._adapter("general_search") or self._adapter("exa_search") or self._adapter("em_news")
            if _gen:
                _weak = []
                for sec in profile.sections:
                    _kw = sec[:4]
                    _hits = sum(1 for e in self.evidence_pool.values()
                                if _kw in (e.content or "") or _kw in (e.source_title or ""))
                    if _hits < 2:
                        _weak.append(sec)
                if _weak:
                    yield self._ev("collect", "search", "覆盖度补搜",
                                    f"{len(_weak)} 个维度证据不足：{', '.join(_weak[:3])}")
                    for sec in _weak[:4]:
                        q = f"{profile.name} {sec} {datetime.now().year}年"
                        res = await asyncio.to_thread(_gen.search, q, "news", 4, days=365)
                        for ev in res.evidences[:2]:
                            idx = self._add_evidence(ev)
                            yield self._ev("collect", "evidence", f"补搜 [{idx}] · {sec}",
                                            ev.content[:160], evidence_id=idx,
                                            url=ev.source_url, tier=ev.tier_label,
                                            source=_gen.name, published=ev.published_at)

        # Active strategy cards: bounded, gated carry-over from past failures.
        # They add a few targeted searches after the standard collection plan.
        strategy_queries = self._strategy_queries("collect", limit=4)
        if strategy_queries:
            # 策略补搜数据源：非上市公司优先通用搜索（em_news 对非金融公司覆盖差）
            if not profile.is_public and profile.kind == "company":
                strat_src = (self._adapter("general_search") or self._adapter("exa_search")
                             or self._adapter("em_news") or self._adapter("wind"))
            else:
                strat_src = (self._adapter("wind") or self._adapter("em_news")
                             or self._adapter("general_search") or self._adapter("exa_search"))
            if strat_src:
                if strat_src.name not in sources_used:
                    sources_used.append(strat_src.name)
                for sq in strategy_queries:
                    try:
                        yield self._ev("collect", "search", "策略补搜", sq, source=strat_src.name)
                        res = await asyncio.to_thread(strat_src.search, sq, "news", 4, days=365)
                        for ev in res.evidences[:2]:
                            idx = self._add_evidence(ev)
                            yield self._ev("collect", "evidence", f"策略证据 [{idx}]",
                                            ev.content[:160], evidence_id=idx,
                                            url=ev.source_url, tier=ev.tier_label,
                                            source=strat_src.name, published=ev.published_at)
                    except Exception:  # noqa: BLE001
                        logger.warning("策略补搜失败 query=%s", sq, exc_info=True)
        self.run.evidence_pool = [self.evidence_pool[i] for i in sorted(self.evidence_pool)]
        self.run.collect_quality = evaluate_collect_quality(self.run, self.evidence_pool.values())
        _cq = self.run.collect_quality
        _issue_note = "；".join(_cq.get("issues", [])[:2]) or "证据池覆盖良好"
        yield self._ev("collect", "quality_check", "采集质量自检",
                        f"{_cq.get('score', 0)}/100 · {_cq.get('status', 'unknown')} · "
                        f"{_cq.get('evidence_count', 0)} 条证据 · {_issue_note}",
                        collect_quality=_cq)
        # 构建证据向量索引（供后续 Analyze/Falsify/Report 语义检索）
        self._build_evidence_index()
        _idx_info = f" · 向量索引{'已构建' if (self._ev_index and self._ev_index.has_numpy) else '关键词模式'}" if self._ev_index else ""
        yield self._ev("collect", "collect_done", "采集完成",
                        f"共 {len(self.evidence_pool)} 条证据 · 数据源: {', '.join(sources_used) or '无'}"
                        + (f" · 数据截至 {latest}" if latest else "")
                        + _idx_info,
                        sources=sources_used, as_of=latest)

    @staticmethod
    def _parse_financial_rows(rows) -> list[dict]:
        """把 LLM 抽取的财务行规整为 numeric 时序并算增速。"""
        if not isinstance(rows, list):
            return []
        def num(v):
            if v is None: return None
            if isinstance(v, (int, float)): return float(v)
            s = str(v).replace(",", "").replace("亿", "00000000").replace("万", "0000").strip()
            try: return float(s)
            except Exception: return None
        out = []
        for r in rows:
            if not isinstance(r, dict): continue
            try:
                period = str(r.get("period") or r.get("year") or "").strip()
                if not period: continue
                rev = num(r.get("revenue") or r.get("营收"))
                ni = num(r.get("net_income") or r.get("净利润"))
                oi = num(r.get("operating_income") or r.get("经营利润"))
                if rev is None and ni is None: continue
                out.append({"period": period, "revenue": rev, "net_income": ni, "operating_income": oi})
            except Exception:  # noqa: BLE001
                continue
        out.sort(key=lambda x: x["period"])
        # 修复：增速按期间类型分组计算（年对年、季对季），不跨口径比较
        # period 格式：年度如 "2024"，季度如 "2024Q1"/"2024Q3"
        def _period_type(p: str) -> str:
            """返回 'annual' 或 'quarterly'"""
            return "quarterly" if "Q" in p.upper() or "季" in p else "annual"
        # 按类型分组后分别求增速
        for ptype in ("annual", "quarterly"):
            entries = [e for e in out if _period_type(e["period"]) == ptype]
            prev_r = prev_n = None
            for e in entries:
                e["rev_growth"] = round((e["revenue"] - prev_r) / abs(prev_r) * 100, 1) if (e["revenue"] is not None and prev_r not in (None, 0)) else None
                e["ni_growth"] = round((e["net_income"] - prev_n) / abs(prev_n) * 100, 1) if (e["net_income"] is not None and prev_n not in (None, 0)) else None
                if e["revenue"] is not None: prev_r = e["revenue"]
                if e["net_income"] is not None: prev_n = e["net_income"]
        # 没匹配到任何类型的条目（混合期间），增速置 None（避免跨口径比较）
        for e in out:
            if "rev_growth" not in e:
                e["rev_growth"] = None
            if "ni_growth" not in e:
                e["ni_growth"] = None
        return out[-6:]

    @staticmethod
    def _strip_preamble(text: str) -> str:
        """剥除模型前导寒暄/废话——优先从首个 Markdown 标题(#)起；无标题则从分隔线起；都无则原样。"""
        lines = text.splitlines()
        # 优先找首个 # 标题
        for i, ln in enumerate(lines):
            if ln.strip().startswith("#"):
                return "\n".join(lines[i:]).lstrip("\n")
        # 退而找分隔线(---/***/___)
        for i, ln in enumerate(lines):
            s = ln.strip()
            if set(s) <= {"-", "*", "_"} and len(s) >= 3:
                return "\n".join(lines[i:]).lstrip("\n")
        return text

    @staticmethod
    def _guess_tier(url: str) -> SourceTier:
        """按 URL 猜信源权威性：监管/SEC > 财报Filing > 公司IR > 权威媒体 > 一般媒体。
        域名表外置于 config/source_tiers.json，按 regulator→filing→auth_media 顺序匹配。

        这里只做不依赖分析对象的通用 URL 分层；目标公司官网/IR 的动态识别
        由 _guess_tier_for_profile 负责，避免把某家公司域名写死在配置里。
        """
        u = url.lower()

        # 域名表匹配（regulator → filing → auth_media，先匹配先返回）
        for tier_name, domains in _SOURCE_TIER_GROUPS:
            if any(d in u for d in domains):
                # filing 组在域名表之后，但公司 IR 的 substring 规则需先于 auth_media——
                # 这里 filing 组本身已先于 auth_media，顺序由配置文件保证。
                return SourceTier[tier_name]

        # 公司投资者关系/官方IR（通用 URL 形态，避免 investorplace 等媒体误判）
        host = _source_host(u)
        path = (urlparse(u).path or "").lower() if u else ""
        if host.startswith(("investor.", "investors.", "ir.")) or "/investor" in path or "/ir/" in path:
            return SourceTier.COMPANY_PR

        # 一般媒体（默认）
        return SourceTier.MEDIA

    def _guess_tier_for_profile(self, url: str, source_title: str = "", content: str = "") -> SourceTier:
        """结合当前分析对象识别公司官方披露。

        泛化规则：
        - 监管/交易所/权威媒体等静态高等级来源保持原判定；
        - 财经媒体、搜索聚合、资讯平台不因 URL 带 pdf/report/announcement 自动升级；
        - 只有当 URL 带 IR/报告/公告等官方披露信号，且标题/正文/URL 命中当前目标公司
          别名时，才把一般网页升级为 COMPANY_PR。
        """
        guessed = self._guess_tier(url)
        if int(guessed) < int(SourceTier.MEDIA):
            return guessed
        if not url or _is_non_company_source(url):
            return guessed
        if not _has_official_disclosure_signal(url):
            return guessed
        if _text_or_url_mentions_target(url, source_title, content, self.run.profile, self.run.query):
            return SourceTier.COMPANY_PR
        return guessed

    @staticmethod
    def _build_financials(concepts) -> list[dict]:
        """从 SEC concepts 构建多年财务时序：[{period, revenue, net_income, operating_income, rev_growth, ni_growth}]。
        聚合所有指标名变体，优先年度(fp=FY)数据，按年升序，计算同比增速。取最近 6 年。"""
        if not isinstance(concepts, dict):
            return []
        METRICS = {
            "revenue": ["Revenue", "Revenues", "RevenueFromContractsWithCustomers", "TotalRevenue"],
            "net_income": ["NetIncome", "NetIncomeLoss", "ProfitLoss"],
            "operating_income": ["OperatingIncome", "OperatingIncomeLoss", "IncomeLossFromContinuingOperations"],
        }
        per_metric = {}
        for std, keys in METRICS.items():
            fy_rows = {}  # fy -> val（优先 FY）
            has_fy = False
            for k in keys:
                rows = concepts.get(k)
                if not rows:
                    continue
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    fy = r.get("fy"); val = r.get("val"); fp = r.get("fp", "")
                    if fy is None or val is None:
                        continue
                    if fp == "FY":
                        fy_rows[fy] = val; has_fy = True
                    elif not has_fy and fy not in fy_rows:
                        fy_rows[fy] = val  # 无年度数据时退而用季度（标记）
            per_metric[std] = fy_rows
        if not any(per_metric.values()):
            return []
        all_fy = sorted({fy for m in per_metric.values() for fy in m})[-6:]
        out = []
        prev_rev = prev_ni = None
        for fy in all_fy:
            rev = per_metric.get("revenue", {}).get(fy)
            ni = per_metric.get("net_income", {}).get(fy)
            oi = per_metric.get("operating_income", {}).get(fy)
            # 检查这个 fy 是否用了季度回退值（非 FY）
            used_quarterly = False
            for std_keys, std_metric in [("revenue", "revenue"), ("net_income", "net_income")]:
                keys = METRICS.get(std_metric, [])
                for k in keys:
                    rows = concepts.get(k, [])
                    for r in rows:
                        if isinstance(r, dict) and r.get("fy") == fy and r.get("fp", "") != "FY":
                            used_quarterly = True
                            break
            entry = {
                # 如果用了季度回退值，标记 period 为 "FY季度回退"，增速不与纯年度数据混算
                "period": f"{fy}(季)" if used_quarterly else str(fy),
                "revenue": rev,
                "net_income": ni,
                "operating_income": oi,
                # 标记为季度回退的条目，增速单独计算（不与年度混算）
                "rev_growth": None if used_quarterly else (round((rev - prev_rev) / abs(prev_rev) * 100, 1) if (rev is not None and prev_rev not in (None, 0)) else None),
                "ni_growth": None if used_quarterly else (round((ni - prev_ni) / abs(prev_ni) * 100, 1) if (ni is not None and prev_ni not in (None, 0)) else None),
            }
            out.append(entry)
            # 只有非季度回退的条目才更新 prev（避免季度值污染下一年度的增速基准）
            if not used_quarterly:
                if rev is not None:
                    prev_rev = rev
                if ni is not None:
                    prev_ni = ni
        return out

    async def _collect_demo(self, profile) -> AsyncGenerator[TraceEvent, None]:
        """演示模式的多源采集：模拟金融数据源+SEC+通用搜索，带时效。
        行业分析展示行业级采集（总量/格局/政策/龙头交叉验证），公司分析展示公司级采集。"""
        from demo_data import demo_collect_evidence, demo_sec
        sources_used = []
        is_industry = profile.kind == "industry"

        if is_industry:
            # —— 行业分析：行业定向检索 + 龙头交叉验证 ——
            sources_used.append("general_search")
            _yy = datetime.now().year
            yield self._ev("collect", "search", "行业定向检索·按分析维度",
                            f"按 {len(profile.sections)} 个维度采集（锚定{_yy}年最新数据）",
                            source="general_search")
            # 按维度逐个展示
            for sec in profile.sections:
                yield self._ev("collect", "search", f"维度检索·{sec}",
                                f"{profile.name} {sec} {_yy}年 数据 趋势", source="general_search")
            # 龙头交叉验证
            leaders = profile.leaders or []
            if leaders:
                yield self._ev("collect", "search", "龙头业绩交叉验证",
                                f"按名查询 {', '.join(leaders[:3])} 最新财报/营收/份额（行业判断锚点）",
                                source="general_search")
        else:
            # —— 公司分析：金融数据+SEC+通用搜索 ——
            sources_used.append("neodata")
            yield self._ev("collect", "search", "金融数据源检索(neodata)",
                            f"{profile.name} 最新财报 营收 净利润", source="neodata")
            # SEC（上市）
            if profile.is_public and profile.ticker:
                sources_used.append("sec_edgar")
                yield self._ev("collect", "search", "调取 SEC 财报",
                                f"EDGAR 拉取 {profile.ticker}", source="sec_edgar")
                fin = demo_sec(profile.ticker)
                for alias, rows in (fin.get("concepts") or {}).items():
                    if rows:
                        latest = rows[0]
                        content = (f"{alias} 最新({latest.get('fy')}{latest.get('fp')},"
                                   f"{latest.get('form')}): {latest.get('val'):,} USD 截至 {latest.get('end')}")
                        ev = Evidence(content=content,
                                      source_url=f"https://www.sec.gov/cgi-bin/browse-edgar?CIK={fin.get('cik','')}",
                                      source_title="SEC EDGAR", source_type="filing",
                                      tier=SourceTier.FILING, as_of=str(latest.get("end") or ""))
                        idx = self._add_evidence(ev)
                        yield self._ev("collect", "evidence", f"财报数据 [{idx}]",
                                        content, evidence_id=idx, tier=ev.tier_label,
                                        source="sec_edgar", as_of=ev.as_of)

        # 通用搜索（模拟带时效证据）—— 公司/行业共用，数据由 demo_collect_evidence 按类型返回
        if "general_search" not in sources_used:
            sources_used.append("general_search")
        if not is_industry:
            yield self._ev("collect", "search", "公开信息检索",
                            f"{profile.name} 经营动态 行业", source="general_search")
        for item in demo_collect_evidence(profile.name):
            ev = Evidence(
                content=item["content"], source_url=item["url"],
                source_title=item["title"], source_type=item.get("source_type", "web"),
                tier=SourceTier(item.get("tier", 6)),
                published_at=item.get("published_at", ""), as_of=item.get("as_of", ""),
            )
            idx = self._add_evidence(ev)
            label = item["title"][:30]
            yield self._ev("collect", "evidence", f"证据 [{idx}] · {label}",
                            item["content"][:160], evidence_id=idx,
                            url=item["url"], tier=ev.tier_label,
                            source="general_search", published=ev.published_at, as_of=ev.as_of)
        self.run.data_sources_used = sources_used

    # ---------- ③ Analyze ----------
    async def _analyze(self) -> AsyncGenerator[TraceEvent, None]:
        profile = self.run.profile
        tpl = get_template(profile.template_key)
        yield self._ev("analyze", "thinking", "结构化分析",
                        f"按 {len(profile.sections)} 个量身定制维度组织，优先最新可信证据，识别历史趋势拐点")
        # 语义检索：用 key_questions + sections 作为查询，找最相关的证据子集喂给 LLM
        _analyze_query = " ".join((profile.key_questions or []) + (profile.sections or []))
        try:
            data = await self._chat_json(prompts.analyze_prompt(
                profile.model_dump_json(), tpl, self._digest(query=_analyze_query, max_items=40),
                (not profile.is_public and profile.kind == "company"), profile.sections,
                industry=(profile.kind == "industry"),
                evidence_count=len(self.evidence_pool)))
        except Exception as exc:  # noqa: BLE001
            yield self._ev("analyze", "diagnosis", "分析输出解析失败·自动重试",
                            f"{_safe_str(exc, 160)}；已缩减证据上下文重试")
            try:
                data = await self._chat_json(prompts.analyze_prompt(
                    profile.model_dump_json(), tpl, self._digest(query=_analyze_query, max_items=16, content_len=120),
                    (not profile.is_public and profile.kind == "company"), profile.sections[:4],
                    industry=(profile.kind == "industry"),
                    evidence_count=len(self.evidence_pool)))
            except Exception as exc2:  # noqa: BLE001
                yield self._ev("analyze", "diagnosis", "分析输出解析失败·结构化兜底",
                                f"{_safe_str(exc2, 160)}；已基于分析维度和证据池生成最小洞察，继续进入证伪")
                data = {"insights": self._fallback_analyze_items(profile)}
        raw = data.get("insights", []) if isinstance(data, dict) else []
        if not isinstance(raw, list):
            raw = []
        if not raw:
            yield self._ev("analyze", "diagnosis", "分析输出为空·结构化兜底",
                            "模型未返回可用洞察，已基于分析维度和证据池生成最小洞察")
            raw = self._fallback_analyze_items(profile)
        for item in raw:
            if not isinstance(item, dict):
                continue
            # 分析师不再输出 falsifiable_condition（由红队在后续阶段基于九维挑战自行判断）
            is_fallback = bool(item.get("_is_fallback"))
            ev_ids = item.get("evidence_ids", []) or []
            if not isinstance(ev_ids, list):
                ev_ids = []
            ins = Insight(
                section=item.get("section", "其他"),
                claim=item.get("claim", ""),
                reasoning=item.get("reasoning", ""),
                falsifiable_condition="",  # 占位，由红队 _falsify_one 填充
                confidence=_safe_float(item.get("confidence"), 0.5),
                is_falsifiable=not is_fallback,  # 占位洞察不进红队（止血：不污染证伪与报告）
            )
            if is_fallback:
                ins.needs_human = True  # 占位洞察升级人工：不进报告、UI 提示重试
            for eid in ev_ids:
                if eid in self.evidence_pool:
                    ins.evidence.append(self.evidence_pool[eid])
            # 用代码量化重算初始置信度（LLM 权重 0.7，证据强度 0.3）
            # 分析阶段 insight 关联的证据有限（从 collect 池中 id 匹配），
            # 不能让低 strength 过度拉低 LLM 合理判断
            strength = ins.evidence_strength()
            if strength > 0:
                ins.confidence = round(ins.confidence * 0.7 + strength * 0.3, 3)
            # strength=0 时保留 LLM 自报值（证据尚未充分关联，不惩罚）
            ins.verdict = Verdict.QUESTIONABLE
            self.run.insights.append(ins)
            yield self._ev(
                "analyze", "insight",
                (f"⚠️ 占位洞察 · {ins.section}" if is_fallback else f"洞察 · {ins.section}"),
                ins.claim, insight_id=ins.id, confidence=ins.confidence)
        yield self._ev("analyze", "analyze_done", "初步洞察生成",
                        f"共 {len(self.run.insights)} 条，进入证伪")

    def _fallback_analyze_items(self, profile: ObjectProfile) -> list[dict]:
        """Analyze 阶段 LLM 输出失败时的占位项 —— 只保流程不中断，不伪装成真洞察。

        占位项带 _is_fallback 标记，主循环据此置为 is_falsifiable=False + needs_human=True，
        从而：不进红队九维挑战、不进 _select_report_insights、不污染报告正文（止血）。
        """
        sections = (profile.sections or profile.key_questions or ["综合判断"])[:4]
        ev_ids = sorted(self.evidence_pool)[:3]
        items = []
        for section in sections:
            items.append({
                "section": section,
                "claim": (
                    f"{profile.name}的「{section}」维度分析未完成（LLM 输出解析失败），"
                    f"已保留 {len(self.evidence_pool)} 条证据，请人工重试或补充分析。"
                ),
                "reasoning": (
                    f"占位洞察：Analyze 阶段 LLM 未产出稳定 JSON，为保证流程不中断暂以本项占位。"
                    f"该维度（{section}）需人工复核后重新分析，本项不参与证伪与报告。"
                ),
                "evidence_ids": ev_ids,
                "confidence": 0.0,
                "_is_fallback": True,
            })
        return items

    # ---------- ④⑤ Falsify + Refine（核心证伪闭环） ----------
    async def _falsify_and_refine(self) -> AsyncGenerator[TraceEvent, None]:
        for rnd in range(1, MAX_REFINE_ROUNDS + 1):
            if rnd == 1:
                # 第一轮：所有可证伪洞察都必须经红队检验（执行者不能自判，哪怕自报置信度高）
                pending = [
                    ins for ins in self.run.insights
                    if ins.is_falsifiable and not ins.needs_human
                ]
            else:
                # 后续轮：只追打未达标且未被推翻的
                pending = [
                    ins for ins in self.run.insights
                    if ins.is_falsifiable
                    and ins.verdict not in (Verdict.REFUTED,)
                    and ins.confidence < CONF_THRESHOLD
                    and not ins.needs_human
                ]
            if not pending:
                yield self._ev("refine", "thinking", "收敛达成",
                                f"第 {rnd} 轮：所有可证伪洞察已达置信门槛或已裁决")
                break
            yield self._ev("falsify", "thinking", f"证伪闭环 · 第 {rnd} 轮",
                            f"红队(异源模型)将并行攻击 {len(pending)} 条洞察")
            # 记录本轮起始置信度，用于早停判定
            pre_conf = {ins.id: ins.confidence for ins in pending}
            async for e in self._falsify_round_parallel(pending, rnd):
                yield e
            # 早停：本轮所有洞察置信度几乎未动 → 不再空转
            if rnd >= 2:
                moved = sum(1 for ins in pending if abs(ins.confidence - pre_conf.get(ins.id, ins.confidence)) >= STALE_DELTA)
                if moved == 0:
                    yield self._ev("refine", "thinking", "早停收敛",
                                    f"第 {rnd} 轮无置信度实质性变化，停止空转")
                    break

    async def _falsify_round_parallel(self, pending, rnd) -> AsyncGenerator[TraceEvent, None]:
        """并行证伪一轮：所有 pending 洞察并发跑 _falsify_one（信号量限并发8），事件实时 yield。
        单条出错不连累其他（降级该洞察、继续）。生成器被提前关闭(SSE 断连)时取消所有后台任务防泄漏。"""
        queue: asyncio.Queue = asyncio.Queue()
        sem = asyncio.Semaphore(FALSIFY_CONCURRENCY)
        remaining = len(pending)

        async def run_one(ins):
            try:
                async with sem:
                    async for ev in self._falsify_one(ins, rnd):
                        await queue.put(ev)
            except Exception as exc:  # noqa: BLE001
                decision, diag = self._triage("falsify", exc, ctx=ins.claim[:20])
                ins.needs_human = True
                await queue.put(self._ev("falsify", "diagnosis",
                               f"洞察证伪出错·{ {'degrade': '降级', 'escalate': '向人求助'}[decision] }",
                               diag["summary"], insight_id=ins.id, **diag))
            finally:
                await queue.put(None)

        tasks = [asyncio.create_task(run_one(ins)) for ins in pending]
        try:
            done = 0
            while done < remaining:
                ev = await queue.get()
                if ev is None:
                    done += 1
                    continue
                yield ev
        finally:
            # 正常结束 await gather 收尾；SSE 断连(GeneratorExit)时取消未完成任务，防后台继续烧 LLM
            for t in tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _challenges_digest(self, challenges: list) -> str:
        """格式化九维挑战为 digest 字符串，供 reverdict prompt 使用。"""
        if not challenges:
            return "(九维挑战未返回有效结果)"
        lines = []
        for c in challenges:
            if not isinstance(c, dict):
                continue
            dim = c.get("dimension", "?")
            label = _DIM_LABELS.get(dim, dim)
            sev = str(c.get("severity", "?")).lower()
            ch = _safe_str(c.get("challenge", ""), 120)
            lines.append(f"- [{label}/{dim}] 严重度:{sev} → {ch}")
        return "\n".join(lines)

    async def _falsify_one(self, ins: Insight, rnd: int) -> AsyncGenerator[TraceEvent, None]:
        # 红队 fresh thread：只给当前论点+证据，不喂上一轮修订历史
        support_digest = self._digest([self._idx_of(e) for e in ins.evidence], only_support=True)
        _is_pub = (self.run.profile.is_public if self.run.profile else True)
        _is_ind = (self.run.profile.kind == "industry" if self.run.profile else False)
        # 时效上下文——防止红队基于尚未发布的数据发起挑战
        from datetime import date as _date
        _today = _date.today().isoformat()
        _data_as_of = self.run.data_as_of or ""
        red = await self._chat_json_red(prompts.red_team_prompt(
            ins.claim, ins.reasoning, support_digest, is_public=_is_pub, industry=_is_ind,
            today=_today, data_as_of=_data_as_of))
        red = red if isinstance(red, dict) else {}

        # 解析九维挑战
        challenges = red.get("challenges", [])
        if not isinstance(challenges, list):
            challenges = []
        challenges = [c for c in challenges if isinstance(c, dict)]
        overall = str(red.get("overall", "incomplete")).lower()
        if overall not in ("solid", "incomplete", "refuted"):
            overall = "incomplete"
        overall_note = _safe_str(red.get("overall_note", ""), 300)

        # 后处理：降级 temporal 维度中基于不可得数据的"数据过时"投诉
        challenges, overall = _sanitize_temporal_challenges(challenges, _data_as_of, overall)

        # 兼容字段：从 challenges 中提取最有力的 counter_hypothesis 和 challenge 摘要
        sorted_ch = sorted(challenges, key=lambda c: _SEV_ORDER.get(
            str(c.get("severity", "none")).lower(), 0), reverse=True)
        top_ch = sorted_ch[0] if sorted_ch else {}
        counter_hypo = _safe_str(top_ch.get("challenge", "")) or overall_note
        challenge_summary = "; ".join(
            f"{_DIM_LABELS.get(c.get('dimension', '?'), c.get('dimension', '?'))}({c.get('severity', '?')})"
            for c in challenges[:5])

        # 收集需要搜索的查询（所有维度的 search_query + 合并的 search_queries），去重
        dim_queries = [
            c.get("search_query", "").strip()
            for c in challenges
            if isinstance(c.get("search_query", ""), str) and c.get("search_query", "").strip()
        ]
        queries = _dedup_queries(dim_queries, red.get("search_queries", []))

        # —— 三层记忆·Behavior：加载证伪策略，匹配相关策略 ——
        matched_policies: list[dict] = []
        try:
            all_policies = memory_store.load_challenge_policies()
            matched_policies = memory_store.match_policies(ins.claim, ins.reasoning, all_policies)
            # 把匹配策略的 search_strategy 补充为额外搜索查询（不占原有维度查询的名额）
            policy_queries = [_safe_str(mp.get("search_strategy", ""), 200)
                              for mp in matched_policies[:3]]
            queries = _dedup_queries(queries, policy_queries)
        except Exception:  # noqa: BLE001
            logger.warning("匹配证伪策略(challenge_policies)失败，跳过策略补搜", exc_info=True)

        strategy_queries = self._strategy_queries("falsify", claim=ins.claim, limit=2)
        if strategy_queries:
            queries = _dedup_queries(queries, strategy_queries)

        yield self._ev("falsify", "red_team", f"红队九维挑战：{ins.claim[:30]}",
                        challenge_summary[:200] or overall_note[:200], insight_id=ins.id,
                        counter_hypothesis=counter_hypo, overall=overall)

        rec = FalsificationRecord(
            round=rnd, counter_hypothesis=counter_hypo, red_team_challenge=challenge_summary,
            challenges=challenges, overall_assessment=overall,
            confidence_before=ins.confidence, verdict_before=ins.verdict,
        )

        # 搜索反证（按维度查询）——强制引入外部新证据
        new_counter = 0
        if self._search_available() or self._adapter("general_search") or self._adapter("neodata"):
            for q in queries[:MAX_COUNTER_QUERIES_PER_INSIGHT]:
                yield self._ev("falsify", "search", "搜寻反证", q,
                                insight_id=ins.id, query=q)
                results = await asyncio.to_thread(self._counter_search, q, 4)
                for res in results[:MAX_COUNTER_RESULTS_PER_QUERY]:
                    ev = Evidence(
                        content=res["content"][:400], source_url=res["url"],
                        source_title=res["title"], tier=self._guess_tier(res["url"]),
                        # 不硬标 supports=False：反证搜索结果可能是中性/支撑信息，
                        # 保持默认 True，由裁决 LLM 基于内容判断实际立场
                    )
                    before_count = len(self.evidence_pool)
                    idx = self._add_evidence(ev)
                    is_new_evidence = len(self.evidence_pool) > before_count
                    ev = self.evidence_pool[idx]
                    if not self._contains_evidence(ins.evidence, ev):
                        ins.evidence.append(ev)
                    if not self._contains_evidence(rec.counter_evidence, ev):
                        rec.counter_evidence.append(ev)
                        new_counter += 1
                    if is_new_evidence:
                        yield self._ev("falsify", "evidence", f"反证 [{idx}]",
                                        ev.content[:140], insight_id=ins.id,
                                        evidence_id=idx, supports=False,
                                        url=ev.source_url, source=ev.source_title,
                                        tier=ev.tier_label, published=ev.published_at,
                                        as_of=ev.as_of)

        # 关键约束：不一刀切"无新证据=冻结"。冻结条件见 _should_freeze_confidence。
        _existing_support = sum(1 for e in ins.evidence if e.supports)
        if _should_freeze_confidence(new_counter, _existing_support, overall,
                                     self._search_available()):
            ins.stale_count += 1
            rec.verdict_after = ins.verdict
            rec.confidence_after = ins.confidence
            rec.note = "现有证据不足且无法搜索新证据，冻结置信度。"
            ins.falsifications.append(rec)
            if ins.stale_count >= STALE_HUMAN:
                ins.needs_human = True
                yield self._ev("refine", "score", f"标记需人工：{ins.claim[:30]}",
                                "多轮无法获取新证据验证", insight_id=ins.id)
            return

        # 裁决阶段（无论是否找到新反证，只要现有证据充分或红队判定solid就可以裁决）
        challenges_digest = self._challenges_digest(challenges)
        verdict_data = await self._chat_json_red(prompts.reverdict_prompt(
            ins.claim, challenges_digest,
            self._digest([self._idx_of(e) for e in ins.evidence], only_support=True),
            self._digest([self._idx_of(e) for e in ins.evidence], only_support=False),
            today=_today, data_as_of=_data_as_of,
        ))
        verdict_data = verdict_data if isinstance(verdict_data, dict) else {}
        new_verdict = self._parse_verdict(verdict_data.get("verdict"))
        model_conf = _safe_float(verdict_data.get("confidence"), ins.confidence)
        # 代码量化证据强度与模型判断取交叉
        strength = ins.evidence_strength()
        final_conf = _fuse_confidence(model_conf, strength)

        open_dims = verdict_data.get("open_dimensions", []) or []
        if not isinstance(open_dims, list):
            open_dims = []
        gap_explanation = _safe_str(verdict_data.get("gap_explanation", ""), 400)

        # 自我迭代：incomplete 且有搜索能力 → 针对存疑维度搜索支撑证据补强
        new_support = 0
        refined = False
        if overall == "incomplete" and self._search_available() and open_dims:
            yield self._ev("refine", "thinking", f"自我迭代补强：{ins.claim[:30]}",
                            f"针对存疑维度 {', '.join(str(d) for d in open_dims[:3])} 搜索支撑证据")
            refine_q_raw = await self._chat_json_red(prompts.refine_search_prompt(
                ins.claim, [str(d) for d in open_dims], challenges_digest))
            refine_q_raw = refine_q_raw if isinstance(refine_q_raw, dict) else {}
            support_queries = refine_q_raw.get("support_queries", []) or []
            if isinstance(support_queries, str):
                support_queries = [support_queries]
            if not isinstance(support_queries, list):
                support_queries = []
            support_queries = [_safe_str(q, 200) for q in support_queries if q][:MAX_SUPPORT_QUERIES_PER_INSIGHT]

            for q in support_queries:
                yield self._ev("refine", "search", "搜寻支撑证据", q,
                                insight_id=ins.id, query=q)
                results = await asyncio.to_thread(self._counter_search, q, 4)
                for res in results[:MAX_SUPPORT_RESULTS_PER_QUERY]:
                    ev = Evidence(
                        content=res["content"][:400], source_url=res["url"],
                        source_title=res["title"], tier=self._guess_tier(res["url"]),
                        supports=True,  # 支撑证据
                    )
                    before_count = len(self.evidence_pool)
                    idx = self._add_evidence(ev)
                    is_new_evidence = len(self.evidence_pool) > before_count
                    ev = self.evidence_pool[idx]
                    if not self._contains_evidence(ins.evidence, ev):
                        ins.evidence.append(ev)
                    if not self._contains_evidence(rec.support_evidence, ev):
                        rec.support_evidence.append(ev)
                        new_support += 1
                    if is_new_evidence:
                        yield self._ev("refine", "evidence", f"支撑证据 [{idx}]",
                                        ev.content[:140], insight_id=ins.id,
                                        evidence_id=idx, supports=True,
                                        url=ev.source_url, source=ev.source_title,
                                        tier=ev.tier_label, published=ev.published_at,
                                        as_of=ev.as_of)

            # 有新支撑证据 → 二次裁决
            if new_support > 0:
                verdict_data2 = await self._chat_json_red(prompts.reverdict_prompt(
                    ins.claim, challenges_digest,
                    self._digest([self._idx_of(e) for e in ins.evidence], only_support=True),
                    self._digest([self._idx_of(e) for e in ins.evidence], only_support=False),
                    refinement=True,
                    today=_today, data_as_of=_data_as_of,
                ))
                verdict_data2 = verdict_data2 if isinstance(verdict_data2, dict) else {}
                new_verdict = self._parse_verdict(verdict_data2.get("verdict", new_verdict.value))
                model_conf = _safe_float(verdict_data2.get("confidence"), final_conf)
                strength = ins.evidence_strength()
                final_conf = _fuse_confidence(model_conf, strength)
                gap2 = _safe_str(verdict_data2.get("gap_explanation", ""), 400)
                if gap2:
                    gap_explanation = gap2
                od2 = verdict_data2.get("open_dimensions", [])
                if isinstance(od2, list) and od2:
                    open_dims = od2
                refined = True

        # stale 判定：本轮有新证据但置信度几乎没动 → 也算 stale
        if _is_stale(final_conf, ins.confidence, refined):
            ins.stale_count += 1
        else:
            ins.stale_count = 0

        # —— 争议洞察辩论回合（P2）——
        # 触发条件：红队判 refuted/incomplete + 分析师原始置信度高(>阈值) + 有搜索能力
        # 流程：分析师针对红队挑战搜反驳证据 → reviewer 终审裁决
        _original_conf = rec.confidence_before
        if (overall in ("refuted", "incomplete")
                and _original_conf > DEBATE_CONF_THRESHOLD
                and rnd == 1  # 只在首轮证伪触发辩论，不重复
                and self._search_available()):
            try:
                yield self._ev("refine", "thinking", f"触发辩论回合：{ins.claim[:30]}",
                                f"分析师原始置信度 {_original_conf:.0%} vs 红队判定 {overall} → 启动辩论",
                                insight_id=ins.id)
                # 分析师针对红队挑战搜反驳证据
                debate_plan = await self._chat_json(prompts.debate_search_prompt(
                    ins.claim, challenges_digest), role="analyst")
                debate_queries = debate_plan.get("debate_queries", []) if isinstance(debate_plan, dict) else []
                debate_evidence_ids = []
                for dq in (debate_queries or [])[:MAX_DEBATE_QUERIES_PER_INSIGHT]:
                    if not dq or len(dq) < 3:
                        continue
                    for res in self._counter_search(dq)[:MAX_DEBATE_RESULTS_PER_QUERY]:
                        ev = Evidence(
                            content=res["content"][:400], source_url=res["url"],
                            source_title=res["title"], tier=self._guess_tier(res["url"]),
                            supports=True,  # 辩论反驳证据是支撑性的
                        )
                        before_count = len(self.evidence_pool)
                        idx = self._add_evidence(ev)
                        is_new_evidence = len(self.evidence_pool) > before_count
                        ev = self.evidence_pool[idx]
                        if idx and not self._contains_evidence(ins.evidence, ev):
                            ins.evidence.append(ev)
                            debate_evidence_ids.append(idx)
                            if is_new_evidence:
                                yield self._ev("refine", "evidence", f"辩论反驳证据 [{idx}]",
                                                ev.content[:120], evidence_id=idx, insight_id=ins.id,
                                                url=ev.source_url, source=ev.source_title,
                                                tier=ev.tier_label, published=ev.published_at,
                                                as_of=ev.as_of)
                # 有反驳证据 → reviewer 终审裁决
                if debate_evidence_ids:
                    debate_support = self._digest(debate_evidence_ids, only_support=True)
                    debate_counter = self._digest([self._idx_of(e) for e in ins.evidence], only_support=False)
                    debate_verdict = await self._chat_json(prompts.debate_prompt(
                        ins.claim, ins.reasoning, challenges_digest,
                        debate_support, debate_counter), role="reviewer")
                    debate_verdict = debate_verdict if isinstance(debate_verdict, dict) else {}
                    if debate_verdict.get("verdict"):
                        debate_v = self._parse_verdict(debate_verdict.get("verdict", new_verdict.value))
                        debate_conf = _safe_float(debate_verdict.get("confidence"), final_conf)
                        strength = ins.evidence_strength()
                        debate_final_conf = _fuse_confidence(debate_conf, strength)
                        # 辩论结果覆盖之前的裁决
                        new_verdict = debate_v
                        final_conf = debate_final_conf
                        overall = ("solid" if debate_v == Verdict.SUPPORTED
                                   else "refuted" if debate_v == Verdict.REFUTED
                                   else "incomplete")
                        gap2 = _safe_str(debate_verdict.get("gap_explanation", ""), 400)
                        if gap2:
                            gap_explanation = gap2
                        od2 = debate_verdict.get("open_dimensions", [])
                        if isinstance(od2, list) and od2:
                            open_dims = od2
                        note2 = _safe_str(debate_verdict.get("note", ""), 400)
                        if note2:
                            rec.note = note2
                        yield self._ev("refine", "score", f"辩论终审：{debate_v.value}",
                                        f"{ins.claim[:30]} · 置信度→{debate_final_conf:.2f} · {overall}",
                                        insight_id=ins.id, verdict=debate_v.value,
                                        confidence=debate_final_conf, note=note2, overall=overall)
                else:
                    yield self._ev("refine", "thinking", f"辩论无效：{ins.claim[:30]}",
                                    "分析师未找到有效反驳证据，维持红队裁决",
                                    insight_id=ins.id)
            except Exception:  # noqa: BLE001
                logger.warning("辩论回合失败，维持原裁决（可选增强）", exc_info=True)

        # —— 置信度上限约束：verdict 决定置信度天花板（见 _VERDICT_CONF_CAP）——
        # 修复 bug：refuted 洞察曾出现 79% 置信度。语义：置信度=洞察成立的可信度，
        # 已推翻/无法验证的洞察不可能高可信。
        final_conf = _cap_confidence(new_verdict, final_conf)
        # —— 标签与置信度一致性对齐：消除"成立 45%"/"存疑 58%"这类自相矛盾展示 ——
        new_verdict = _align_verdict_confidence(new_verdict, final_conf)

        # —— 提取红队判定的推翻路径（falsification_path） ——
        # 红队基于九维挑战独立判断"什么现有证据会推翻此结论"，替代分析师自述的可证伪条件
        # 优先取最终裁决的 final_falsification_path，回退到首轮红队的 falsification_path
        _fp = _safe_str(verdict_data2.get("final_falsification_path", ""), 300) if refined else ""
        if not _fp:
            _fp = _safe_str(verdict_data.get("final_falsification_path", ""), 300)
        if not _fp:
            _fp = _safe_str(red.get("falsification_path", ""), 300)
        # 兜底：从最高严重度挑战构造推翻路径（demo 模式或 LLM 未输出时）
        if not _fp and sorted_ch:
            _top = sorted_ch[0]
            _sev = str(_top.get("severity", "none")).lower()
            if _sev in ("high", "medium"):
                _dim = _DIM_LABELS.get(_top.get("dimension", ""), _top.get("dimension", ""))
                _fp = f"红队{_dim}挑战({_sev})：{_safe_str(_top.get('challenge', ''), 200)}"
            elif overall == "solid":
                _fp = "无——基于现有公开数据无法推翻"
        # 后处理：清除红队输出中的未来假设词（LLM 可能不遵守 prompt 禁令）
        _fp = _sanitize_falsification_path(_fp, sorted_ch, overall)
        ins.falsifiable_condition = _fp

        # 最终分类与缺口说明（solid 保留 / incomplete 补强后保留或降级 / refuted 推翻）
        rec.verdict_after = new_verdict
        rec.confidence_after = final_conf
        rec.note = _safe_str(verdict_data.get("note", ""), 400)

        if overall == "refuted" or new_verdict == Verdict.REFUTED:
            ins.refinement_note = (f"九维挑战发现致命反证，已推翻。{gap_explanation}"
                                   if gap_explanation
                                   else "九维挑战综合判定为 refuted，存在 high 级别质疑构成直接反证。")
        elif overall == "incomplete" or new_verdict in (Verdict.QUESTIONABLE, Verdict.UNVERIFIABLE):
            open_str = ", ".join(str(d) for d in open_dims[:3]) if open_dims else "未明确"
            if refined and new_support > 0:
                ins.refinement_note = (f"自我迭代补强 {new_support} 条支撑证据后仍存疑（{open_str}）。"
                                       f"{gap_explanation}")
            else:
                ins.refinement_note = (f"九维挑战发现 {len(open_dims)} 个存疑维度（{open_str}），保留证据缺口。"
                                       f"{gap_explanation}")
        else:  # solid / supported
            ins.refinement_note = ("九维挑战均通过，洞察 solid。"
                                   if overall == "solid"
                                   else "经九维挑战与裁决，洞察成立。")

        ins.verdict = new_verdict
        ins.confidence = final_conf
        ins.falsifications.append(rec)

        refine_tag = "（自我迭代补强）" if refined else ""
        yield self._ev("refine", "score", f"重新裁决：{new_verdict.value}{refine_tag}",
                        f"{ins.claim[:30]} · 置信度 {rec.confidence_before:.2f}→{final_conf:.2f} · {overall}",
                        insight_id=ins.id, verdict=new_verdict.value,
                        confidence=final_conf, note=rec.note, overall=overall)

        # stale 强制转向提示
        if ins.stale_count >= STALE_LIMIT and ins.stale_count < STALE_HUMAN:
            yield self._ev("refine", "thinking", f"强制转向：{ins.claim[:30]}",
                            f"连续 {ins.stale_count} 轮无实质进展，下轮将换检索角度",
                            insight_id=ins.id)

        # —— 三层记忆·Behavior：更新挑战策略成功率 ——
        # matched_policies 中，如果该策略的 challenge_type 对应的维度在 challenges 中有 high/medium severity，
        # 则该策略"成功"（发现了实际问题）
        if matched_policies:
            try:
                high_med_dims = {
                    str(c.get("dimension", "")).lower()
                    for c in challenges
                    if isinstance(c, dict) and str(c.get("severity", "")).lower() in ("high", "medium")
                }
                for mp in matched_policies:
                    ct = str(mp.get("challenge_type", "")).lower()
                    succeeded = ct in high_med_dims
                    memory_store.update_policy_success(mp.get("id", ""), succeeded)
            except Exception:  # noqa: BLE001
                logger.warning("更新证伪策略成功率失败，跳过", exc_info=True)

    def _idx_of(self, ev: Evidence) -> int:
        for i, e in self.evidence_pool.items():
            if e.id == ev.id:
                return i
        return -1

    @staticmethod
    def _parse_verdict(v) -> Verdict:
        try:
            return Verdict(str(v))
        except Exception:  # noqa: BLE001
            return Verdict.QUESTIONABLE

    # ---------- ⑥ Report ----------
    async def _report(self) -> AsyncGenerator[TraceEvent, None]:
        # 恢复旧检查点/历史 run 时也执行一次 URL 治理；新采集证据已在
        # _add_evidence 中清洗，此处为幂等兜底。
        for _ev_item in self.evidence_pool.values():
            _ev_item.source_url = sanitize_source_url(_ev_item.source_url)
        # 同步证据池到 run（按编号顺序），供前端 [证据N]→#evidence-N→source_url 链式溯源
        self.run.evidence_pool = [self.evidence_pool[i] for i in sorted(self.evidence_pool)]
        # report_md 已移除（冗余：narrative_md 是唯一输出文档，insights/evidence_pool 提供结构化结论与支撑）
        self.run.report_md = ""
        yield self._ev("report", "thinking", "准备撰写完整分析文档",
                        "终审模型将生成叙事性分析文档（非结构化模板报告）")

        reviewer = self.llm.effective_provider("reviewer") or "reviewer"
        profile_json = self.run.profile.model_dump_json() if self.run.profile else "{}"
        is_pub = (self.run.profile.is_public if self.run.profile else True)
        is_ind = (self.run.profile.kind == "industry" if self.run.profile else False)
        same_src = self.run.same_source_review
        def _extract_text(v) -> str:
            if isinstance(v, dict):
                v = v.get("content") or v.get("markdown") or ""
            return str(v) if v else ""

        # —— 洞察筛选：只保留关键、确定、有启发价值的进入报告 ——
        core_insights, risk_insights = self._select_report_insights()
        # 行业题若通过证伪的核心洞察不足，强行凑满三条会把存疑洞察
        # 写成行业定律。此时切换为审慎写作：核心段只保留成立洞察，
        # 其余存疑项移入风险段，并在同一次写作调用内缩短输出。
        _supported_core = [
            ins for ins in core_insights
            if ins.verdict == Verdict.SUPPORTED and ins.confidence >= 0.70
        ]
        _weak_evidence_mode = should_use_cautious_writing(
            core_insights,
            is_industry=is_ind,
        )
        _gap_evidence_ids: list[int] = []
        if _weak_evidence_mode:
            # A weak run's lone “supported” insight can still be supported only
            # by secondary/aggregated news. Do not let its claim text anchor the
            # writer; rebuild observations from the cleaned evidence handoff.
            core_insights = []
            # 存疑洞察不能只靠“移入风险段”解决，因为模型仍会把它
            # 写成确定风险或情景阈值。弱证据模式下完全不向最终写手
            # 传递这些洞察，只允许从直接证据归纳有限风险。
            risk_insights = []

            # 弱证据行业题只做两条确定性补证查询，不调用 LLM 规划。
            # 目标是补白皮书/监管原文，而不是扩大搜索面；失败时继续
            # 审慎写作，不阻断报告。
            _gap_adapter = self._adapter("exa_search") or self._adapter("general_search")
            _gap_added = 0
            if _gap_adapter and self.run.profile:
                from datetime import date as _date
                _gap_queries = build_cautious_gap_queries(
                    self.run.profile,
                    year=_date.today().year,
                )
                try:
                    _gap_results = await asyncio.gather(*[
                        asyncio.to_thread(
                            _gap_adapter.search, query, "general", 4, 730
                        )
                        for query in _gap_queries
                    ])
                    _gap_candidates: list[Evidence] = []
                    for _gap_result in _gap_results:
                        _gap_candidates.extend(_gap_result.evidences)

                    def _gap_priority(item: Evidence) -> tuple[int, int]:
                        url = (item.source_url or "").lower()
                        authoritative = any(domain in url for domain in (
                            "gov.cn", "moa.gov.cn", "stats.gov.cn",
                            "kpmg.com", "pwc.", "deloitte.", "ey.com",
                        ))
                        return (0 if authoritative else 1, int(item.tier))

                    for _gap_item in sorted(_gap_candidates, key=_gap_priority)[:8]:
                        before = len(self.evidence_pool)
                        _gap_idx = self._add_evidence(_gap_item)
                        if _gap_idx not in _gap_evidence_ids:
                            _gap_evidence_ids.append(_gap_idx)
                        _gap_added += int(len(self.evidence_pool) > before)
                    if _gap_added:
                        if _gap_adapter.name not in self.run.data_sources_used:
                            self.run.data_sources_used.append(_gap_adapter.name)
                        self.run.evidence_pool = [
                            self.evidence_pool[idx] for idx in sorted(self.evidence_pool)
                        ]
                        self._build_evidence_index()
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "审慎模式定向补证失败，继续使用现有证据: %s",
                        str(exc)[:100],
                    )
            yield self._ev(
                "report", "thinking", "审慎模式·定向补证",
                f"固定查询2条 · 新增证据 {_gap_added} 条 · 零额外LLM调用",
            )
        core_digest = self._insights_digest(core_insights)
        risk_digest = self._insights_digest(risk_insights) if risk_insights else "(无额外存疑洞察)"
        excluded_n = len(self.run.insights) - len(core_insights) - len(risk_insights)
        yield self._ev("report", "thinking", "洞察筛选",
                        f"全部 {len(self.run.insights)} 条 → 精选 {len(core_insights)} 条核心 + "
                        f"{len(risk_insights)} 条风险 · 排除 {excluded_n} 条(推翻/不可检验/低置信)")
        insights_digest = core_digest  # 核心发现段用精选洞察
        sections = self.run.profile.sections if self.run.profile else []

        # ============ V2 架构：全量证据一次性生成 ============
        # 核心改动：不再压缩证据、不再分段生成，直接全量证据+洞察→一次性完整报告
        # 评测证明：分段+压缩导致信息丢失（数据性扣分），一次性写反而数据利用率更高
        _v2_success = False
        yield self._ev("report", "thinking", "V2架构·证据账本直传",
                        f"由 {reviewer} 一次性生成完整报告（核心主张证据强制保留）")

        # 构建报告证据 digest：先保证精选洞察的直接证据全部进入上下文，
        # 再补权威证据和语义 top-k。相比单纯语义排序，不增加 LLM 调用，
        # 但避免“洞察只留下证据编号、写作模型看不到证据正文”。
        _narrative_query = f"{self.run.profile.name} {self.run.profile.industry} {' '.join(sections)}"
        _semantic_ids = self._semantic_search(_narrative_query, top_k=45)
        _semantic_ids = list(dict.fromkeys([*_gap_evidence_ids, *_semantic_ids]))
        _writing_insights = [*core_insights, *risk_insights]
        _represented_sections = {ins.section for ins in _writing_insights}
        for _section in ([] if _weak_evidence_mode else sections):
            if _section in _represented_sections:
                continue
            _section_candidates = [
                ins for ins in self.run.insights
                if ins.section == _section
                and ins.verdict not in (Verdict.REFUTED, Verdict.UNVERIFIABLE)
                and ins.evidence
            ]
            if not _section_candidates:
                continue
            _best_section_insight = max(
                _section_candidates,
                key=lambda ins: ins.confidence * 0.6 + ins.evidence_strength() * 0.4,
            )
            _writing_insights.append(_best_section_insight)
            _represented_sections.add(_section)
        _report_evidence_ids = select_report_evidence_ids(
            self.evidence_pool,
            _semantic_ids,
            _writing_insights,
            self._idx_of,
            max_items=45,
        )
        if _weak_evidence_mode and self.run.profile:
            _report_evidence_ids = select_cautious_report_evidence_ids(
                self.evidence_pool,
                [*_gap_evidence_ids, *_semantic_ids],
                self.run.profile,
                max_items=32,
            )
        _direct_evidence_ids: list[int] = []
        for _insight in _writing_insights:
            for _evidence in _insight.evidence:
                _evidence_idx = self._idx_of(_evidence)
                if (
                    _evidence_idx in _report_evidence_ids
                    and _evidence_idx not in _direct_evidence_ids
                ):
                    _direct_evidence_ids.append(_evidence_idx)
        _direct_evidence_ids = _direct_evidence_ids[:24]
        _direct_id_set = set(_direct_evidence_ids)
        _background_evidence_ids = [
            idx for idx in _report_evidence_ids if idx not in _direct_id_set
        ]
        full_digest = (
            "【洞察直接证据（优先用于核心论证）】\n"
            f"{self._digest(ids=_direct_evidence_ids, max_items=24, content_len=420)}\n\n"
            "【相关背景证据（仅在直接相关时使用）】\n"
            f"{self._digest(ids=_background_evidence_ids, max_items=45, content_len=160)}"
        )
        writing_contract = build_writing_contract(
            self.run,
            self.evidence_pool,
            core_insights,
            risk_insights,
            self._idx_of,
            all_insights=self.run.insights,
        )
        if _weak_evidence_mode:
            writing_contract += (
                "\n\n【审慎写作模式（证据门禁自动触发）】\n"
                "- 通过证伪且高置信的核心洞察不足2条；不得把存疑洞察包装为行业结论。\n"
                "- 存疑洞察不得进入摘要、事实解释、核心发现、展望或风险段。\n"
                "- 核心发现可改为“已验证观察”，但每条必须由至少2个独立的权威/原始信源直接支持；"
                "否则只陈述单一来源事实及其边界，不得补写因果机制。\n"
                "- 不得为了凑足3条核心发现创造新主张；宁可缩短报告并明确证据缺口。"
            )

        # 结构化财务数据
        financials_block = ""
        if self.run.financials:
            _fin_lines: list[str] = ["\n\n【结构化财务数据（精确数值）】"]
            for f in self.run.financials[:8]:
                if not isinstance(f, dict):
                    continue
                parts = [str(f.get("period") or "")]
                rev = f.get("revenue")
                ni = f.get("net_income")
                rg = f.get("rev_growth")
                if rev is not None:
                    parts.append(f"营收{rev:,}")
                if ni is not None:
                    parts.append(f"净利润{ni:,}")
                if rg is not None:
                    parts.append(f"增速{rg}%")
                if len(parts) > 1:
                    _fin_lines.append(" | ".join(parts))
            if len(_fin_lines) > 1:
                financials_block = "\n".join(_fin_lines)

        # 策略卡约束
        report_constraints: list[str] = []
        for card in self._stage_strategies("report"):
            action = card.get("action") if isinstance(card.get("action"), dict) else {}
            report_constraints.extend(str(x) for x in action.get("prompt_hints") or [] if str(x).strip())
            report_constraints.extend(f"Check: {x}" for x in action.get("checks") or [] if str(x).strip())
            if action.get("prompt_hints") or action.get("checks"):
                self._mark_strategy_applied(card)

        # 竞争对比矩阵注入
        _comp_matrix = self._build_comparison_matrix()
        if _comp_matrix:
            full_digest += _comp_matrix

        try:
            _v2_raw = await asyncio.wait_for(self._chat_text(
                prompts.narrative_full_prompt(
                    profile_json, full_digest, insights_digest, risk_digest,
                    same_src, is_pub, is_ind,
                    financials_block=financials_block,
                    report_constraints=report_constraints or None,
                    writing_contract=writing_contract,
                    weak_evidence_mode=_weak_evidence_mode),
                role="reviewer", timeout=480.0, retries=1,
                max_tokens=(
                    6200 if _weak_evidence_mode
                    else (9500 if is_ind else 6000)
                )), timeout=500.0)
            _v2_text = self._strip_preamble(_extract_text(_v2_raw))
            if _v2_text and len(_v2_text) > 500:
                # —— 正文质量闸门：检测思考/规划草稿，命中则强化约束后重写（尊重当前 reviewer 模型）——
                if _looks_like_thinking_draft(_v2_text):
                    yield self._ev(
                        "report", "thinking", "⚠️ V2正文质量闸门·检测到思考草稿",
                        f"输出 {len(_v2_text)} 字符无正文结构，强化约束后重写",
                    )
                    _retry_msgs = prompts.narrative_full_prompt(
                        profile_json, full_digest, insights_digest, risk_digest,
                        same_src, is_pub, is_ind,
                        financials_block=financials_block,
                        report_constraints=report_constraints or None,
                        writing_contract=writing_contract,
                        weak_evidence_mode=_weak_evidence_mode)
                    _retry_msgs[-1] = dict(_retry_msgs[-1])
                    _retry_msgs[-1]["content"] = _retry_msgs[-1]["content"] + _HARD_RETRY_SUFFIX
                    _v2_raw = await asyncio.wait_for(self._chat_text(
                        _retry_msgs, role="reviewer", timeout=480.0, retries=1,
                        max_tokens=(
                            6200 if _weak_evidence_mode
                            else (9500 if is_ind else 6000)
                        )), timeout=500.0)
                    _v2_text = self._strip_preamble(_extract_text(_v2_raw))
                    if not _v2_text or len(_v2_text) <= 500 or _looks_like_thinking_draft(_v2_text):
                        raise RuntimeError(
                            "正文质量闸门：重写后仍为思考草稿，拒绝落盘")
                # 结构校验+自动修复
                from datetime import date as _date
                _today = _date.today().isoformat()
                _data_date = self.run.data_as_of or "未明确"
                footer = (f"\n\n---\n\n数据截至 {_data_date} · 报告生成 {_today} · "
                          f"数据源: {', '.join(self.run.data_sources_used) or '未配置'}\n\n"
                          "本报告基于公开信息，不构成投资建议。")
                _body = _normalize_narrative(_v2_text)
                _body = normalize_plain_numeric_citations(
                    _body, set(self.evidence_pool)
                )
                _body = normalize_grouped_citations(_body)
                _body, _boundary_sections = ensure_dimension_coverage_boundaries(
                    _body, sections
                )
                _body = neutralize_uncited_tracking_thresholds(_body)
                _body = propagate_derived_table_citations(_body)
                _citation_pool = (
                    {
                        idx: self.evidence_pool[idx]
                        for idx in _report_evidence_ids
                        if idx in self.evidence_pool
                    }
                    if _weak_evidence_mode
                    else self.evidence_pool
                )
                _body = attach_exact_numeric_citations(_body, _citation_pool)
                _grounding_audit = audit_report_grounding(_body, self.evidence_pool)
                _coverage_neutralized = neutralize_unsupported_coverage_rows(
                    _body, _grounding_audit
                )
                if _coverage_neutralized != _body:
                    _body = _coverage_neutralized
                    _grounding_audit = audit_report_grounding(
                        _body, self.evidence_pool
                    )
                _neutralized = neutralize_unsupported_scenarios(_body, _grounding_audit)
                if _neutralized != _body:
                    _body = _neutralized
                    _grounding_audit = audit_report_grounding(_body, self.evidence_pool)
                apx = self._build_appendix_md(_body)
                narrative = _body + "\n\n" + apx + footer
                self.run.narrative_md = narrative
                _v2_success = True
                yield self._ev("report", "thinking", "V2架构·一次性生成完成",
                                f"{len(narrative)} 字符 · 终审 {reviewer} · 全量证据直传")
                yield self._ev(
                    "report", "thinking", "数字溯源审计",
                    f"覆盖率 {_grounding_audit['grounding_rate']:.0%} · "
                    f"无引用数字行 {len(_grounding_audit['uncited_numeric_lines'])} · "
                    f"无效引用 {len(_grounding_audit['invalid_citation_ids'])}",
                )

                # 结构不变量校验
                _violations = self._check_structure_invariants(self.run.narrative_md)
                if _violations:
                    repaired, remaining = self._repair_structure_invariants_once(self.run.narrative_md, _violations)
                    if repaired != self.run.narrative_md:
                        self.run.narrative_md = repaired
                    if remaining:
                        yield self._ev("report", "thinking", "结构不变量校验·发现问题",
                                        f"{len(remaining)} 项违规: {'; '.join(remaining[:3])}")
                    else:
                        yield self._ev("report", "thinking", "结构不变量自动修复通过", "")
        except Exception as exc:  # noqa: BLE001
            logger.warning("V2 架构一次性生成失败，降级到分段模式: %s", str(exc)[:80])
            yield self._ev("report", "thinking", "V2架构降级",
                            f"一次性生成失败({str(exc)[:50]})，降级到分段模式")

        # V2 成功后只记录确定性审计；质量打分放到离线评测，避免报告阶段
        # 再消耗一次 LLM 调用并把“自评”误当作真实质量提升。
        if _v2_success:
            narrative = self.run.narrative_md
            self.run.quality_eval = {
                "scores": {},
                "total": None,
                "issues": [],
                "min_score": None,
                "round": 1,
                "passed": bool(_grounding_audit.get("passed")),
                "architecture": "v2_grounded_single_pass",
                "grounding_audit": _grounding_audit,
                "grounding_repair_used": False,
            }

            yield self._ev("report", "narrative_ready", "V2完整分析文档已生成",
                            f"{len(narrative)} 字符 · 终审 {reviewer} · V2全量直传架构",
                            length=len(narrative))

            # Experience + Reflection
            try:
                run_summary = memory_store.build_run_summary(self.run)
                memory_store.save_episodic(run_summary)
            except Exception:  # noqa: BLE001
                pass
            return  # V2 成功，跳过旧分段逻辑

        # ============ 以下是旧版分段逻辑（V2 失败时的 fallback）============
        yield self._ev("report", "thinking", "终审模型撰写完整分析文档(分段模式)",
                        f"由 {reviewer} 拆两段生成（基本事实层 + 洞察层），附录代码生成")
        evidence_summary = ""
        try:
            _sum_raw = await asyncio.wait_for(self._chat_text(
                prompts.evidence_summary_prompt(full_digest, sections),
                role="reviewer", timeout=120.0), timeout=140.0)
            evidence_summary = self._strip_preamble(_extract_text(_sum_raw)) if _sum_raw else ""
            yield self._ev("report", "thinking", "证据摘要已生成(Summary驱动)",
                            f"语义检索 {len(full_digest)} 字 → 摘要 {len(evidence_summary)} 字")
        except Exception as exc:  # noqa: BLE001
            evidence_summary = self._digest(query=_narrative_query, max_items=30, content_len=150)
            yield self._ev("report", "thinking", "证据摘要降级",
                            f"Summary 生成失败({str(exc)[:40]})，降级精简 digest {len(evidence_summary)} 字")
        if not evidence_summary:
            evidence_summary = self._digest(query=_narrative_query, max_items=30, content_len=150)

        # 【关键】把结构化财务数据直接注入证据摘要
        # 解决"财务概览表全是未披露"问题：evidence_summary 来自 _digest（只取部分证据content），
        # 可能遗漏已抽取的精确财务数字。将 self.run.financials 格式化后追加，确保 LLM 看到结构化数据。
        # 注意：self.run.financials 是 list[dict]，不是对象——用 dict.get() 访问字段
        if self.run.financials:
            _fin_lines: list[str] = []
            _fin_lines.append("\n\n【结构化财务数据（来自已采集证据，精确数值，必须用于财务概览表）】\n")
            for f in self.run.financials[:8]:  # 近 8 期
                if not isinstance(f, dict):
                    continue
                parts = [str(f.get("period") or "")]
                rev = f.get("revenue")
                ni = f.get("net_income")
                rg = f.get("rev_growth")
                gp = f.get("gross_profit")
                oi = f.get("operating_income")
                if rev is not None:
                    parts.append(f"营收{rev:,}")
                if ni is not None:
                    parts.append(f"净利润{ni:,}")
                if rg is not None:
                    parts.append(f"增速{rg}%")
                if gp is not None:
                    parts.append(f"毛利{gp:,}")
                if oi is not None:
                    parts.append(f"经营利润{oi:,}")
                if len(parts) > 1:
                    _fin_lines.append(" | ".join(parts))
            if len(_fin_lines) > 1:
                evidence_summary += "\n".join(_fin_lines)
                yield self._ev("report", "thinking", "财务数据已注入",
                                f"{len(self.run.financials)} 期 SEC 财务数据追加到证据摘要")

        # 【改进1】竞争对比矩阵注入——零 LLM 调用，用代码从证据池提取 peers/leaders 指标
        # 构建"自身 vs 竞品/龙头"对比矩阵，让后续核心发现段自动产出竞争洞察
        _comp_matrix = self._build_comparison_matrix()
        if _comp_matrix:
            evidence_summary += _comp_matrix
            yield self._ev("report", "thinking", "竞争对比矩阵已注入",
                            f"对比维度 {_comp_matrix.count('|') // 4} 行 · "
                            f"帮助核心发现段产出竞争洞察")

        report_constraints: list[str] = []
        for card in self._stage_strategies("report"):
            action = card.get("action") if isinstance(card.get("action"), dict) else {}
            report_constraints.extend(str(x) for x in action.get("prompt_hints") or [] if str(x).strip())
            report_constraints.extend(f"Check: {x}" for x in action.get("checks") or [] if str(x).strip())
            if action.get("prompt_hints") or action.get("checks"):
                self._mark_strategy_applied(card)
        if report_constraints:
            evidence_summary += "\n\n【内部质量约束】\n" + "\n".join(f"- {x}" for x in report_constraints[:6])

        # 各段独立命名追踪，避免 parts 列表索引错位（facts 失败时 parts[0] 不再是 facts）
        seg_facts: str = ""
        seg_core: str = ""
        seg_outlook: str = ""
        seg_fallback: str = ""
        diag_parts: list[str] = []

        def _assemble(pre_body: str | None = None) -> str:
            """按固定顺序拼接已生成的各段 + 附录 + footer。任一段可空。
            拼装后做结构归一化：清理数字编号、确保标题层级、删除残留的终审备注。
            传入 pre_body 时跳过段拼接，直接用润色后的正文（仍归一化+附录+footer）。"""
            if pre_body is None:
                segs = [s for s in (seg_facts, seg_core, seg_outlook, seg_fallback) if s]
                body = "\n\n".join(segs)
            else:
                body = pre_body
            body = _normalize_narrative(body)
            apx = self._build_appendix_md(body)
            out = body + "\n\n" + apx + footer
            return out.replace("{date}", self.run.data_as_of or "未明确").replace(
                "{sources}", ", ".join(self.run.data_sources_used) or "未配置")

        # footer：区分"数据截至"（证据最新时间）和"报告生成"（当前时间）
        # data_as_of 是证据中最新的财报/披露日期（如 2026-03-31），不是当前日期
        from datetime import date as _date
        _today = _date.today().isoformat()
        _data_date = self.run.data_as_of or "未明确"
        # 如果 data_as_of 和今天差距超过 30 天，同时显示两者（让读者知道数据时效）
        _date_display = f"数据截至 {_data_date} · 报告生成 {_today}"
        footer = (f"\n\n---\n\n{_date_display} · "
                  f"数据源: {', '.join(self.run.data_sources_used) or '未配置'}\n\n"
                  "本报告基于公开信息，不构成投资建议。")

        # 段1：事实层（执行摘要 + 基本事实/行业格局 + 历史趋势，timeout 240s）
        try:
            facts = await asyncio.wait_for(self._chat_text(
                prompts.narrative_facts_prompt(profile_json, evidence_summary, same_src, is_pub, is_ind),
                role="reviewer", timeout=240.0, retries=1), timeout=260.0)
            seg_facts = self._strip_preamble(_extract_text(facts))
            if seg_facts:
                yield self._ev("report", "thinking", "事实层已生成(摘要+事实+趋势)", f"{len(seg_facts)} 字符")
        except Exception as exc:  # noqa: BLE001
            diag_parts.append(f"事实层失败:{str(exc)[:60]}")

        # 段2：核心发现 — 传入执行摘要文本以保持逻辑连贯（timeout 400s）
        try:
            ins_core = await asyncio.wait_for(self._chat_text(
                prompts.narrative_insights_core_prompt(profile_json, insights_digest, evidence_summary,
                                                       same_src, is_pub, is_ind,
                                                       exec_summary=seg_facts),
                role="reviewer", timeout=400.0, retries=1), timeout=420.0)
            seg_core = self._strip_preamble(_extract_text(ins_core))
            if seg_core:
                yield self._ev("report", "thinking", "核心发现已生成", f"{len(seg_core)} 字符")
        except Exception as exc:  # noqa: BLE001
            diag_parts.append(f"核心发现失败:{str(exc)[:60]}")

        # 段3：风险 + 展望 — 传入核心发现文本以引用具体发现（timeout 400s）
        outlook_insights = core_digest + "\n\n--- 存疑洞察（进入风险段）---\n" + risk_digest
        try:
            ins_outlook = await asyncio.wait_for(self._chat_text(
                prompts.narrative_insights_outlook_prompt(profile_json, outlook_insights, evidence_summary,
                                                          same_src, is_pub, is_ind,
                                                          core_findings=seg_core),
                role="reviewer", timeout=400.0, retries=1), timeout=420.0)
            seg_outlook = self._strip_preamble(_extract_text(ins_outlook))
            if seg_outlook:
                yield self._ev("report", "thinking", "风险展望已生成", f"{len(seg_outlook)} 字符")
        except Exception as exc:  # noqa: BLE001
            diag_parts.append(f"风险展望失败:{str(exc)[:60]}")

        # 降级兜底：洞察层 A/B 都失败 → 从 insights 数据直接生成结构化模板（不靠 LLM）
        if not seg_core and not seg_outlook and self.run.insights:
            seg_fallback = self._build_insights_fallback()
            if seg_fallback:
                yield self._ev("report", "thinking", "洞察层降级兜底(结构化模板)", f"{len(seg_fallback)} 字符")

        if seg_facts or seg_core or seg_outlook or seg_fallback:
            narrative = _assemble()
            self.run.narrative_md = narrative

            # P0.3: 逻辑一致性验证（三段narrative是否连贯）
            if seg_facts and (seg_core or seg_fallback) and (seg_outlook or seg_fallback):
                try:
                    coherence = await asyncio.wait_for(self._chat_json(
                        prompts.logic_consistency_prompt(
                            seg_facts[:2000], (seg_core or seg_fallback)[:2000],
                            (seg_outlook or seg_fallback)[:2000]),
                        role="reviewer"), timeout=45.0)
                    if isinstance(coherence, dict):
                        _coh = coherence.get("coherent", True)
                        _thesis = _safe_str(coherence.get("thesis_identified", ""), 200)
                        _contras = coherence.get("contradictions", [])
                        if isinstance(_contras, list) and _contras:
                            yield self._ev("report", "thinking", "逻辑一致性检查·发现矛盾",
                                            f"论点: {_thesis} · 矛盾: {'; '.join(str(c)[:60] for c in _contras[:2])}")
                        elif not _coh:
                            _sugs = coherence.get("suggestions", [])
                            yield self._ev("report", "thinking", "逻辑一致性检查·待改进",
                                            f"论点: {_thesis} · 建议: {'; '.join(str(s)[:60] for s in (_sugs or [])[:2])}")
                        else:
                            yield self._ev("report", "thinking", "逻辑一致性检查·通过",
                                            f"论点: {_thesis}")
                except Exception:  # noqa: BLE001
                    logger.warning("逻辑一致性检查失败，跳过（增强项）", exc_info=True)

            # 借鉴 Lixin-TU：质量评估 + 改进循环（最多2轮，8维度评分<30或最低<3触发改进）
            # 历史评测发现作为通用自知提示（非针对性复用），在 quality_eval 改进循环中
            # 提示 LLM 注意这些常见不足，但不强制补具体维度。
            # _eval_hints 是 _scope 函数的局部变量，这里重新读取（防止 NameError）
            try:
                _past_evals = memory_store.load_eval_feedback(self.run.query, limit=3)
                _prior_eval_hints = memory_store.generalize_eval_weaknesses(_past_evals) if _past_evals else []
            except Exception:
                _prior_eval_hints = []

            for _qr in range(2):
                try:
                    qa = await asyncio.wait_for(self._chat_json(
                        prompts.quality_eval_prompt(narrative[:6000], sections),
                        role="reviewer"), timeout=60.0)
                    qa = qa if isinstance(qa, dict) else {}
                    _scores = qa.get("scores", {}) if isinstance(qa.get("scores"), dict) else {}
                    _issues = qa.get("issues", []) if isinstance(qa.get("issues"), list) else []
                    _total = _safe_int(qa.get("total"), sum(_safe_int(v, 0) for v in _scores.values()))
                    _min_s = min((_safe_int(v, 0) for v in _scores.values()), default=0)
                    # 借鉴 virattt eval：把评分结果存进 run，供前端可视化与质量回归
                    self.run.quality_eval = {
                        "scores": _scores, "total": _total, "issues": _issues,
                        "min_score": _min_s, "round": _qr + 1,
                        "passed": _total >= 30 and _min_s >= 3,
                    }
                    yield self._ev("report", "thinking", f"质量评估(第{_qr+1}轮)",
                                    f"总分 {_total}/40 · 最低 {_min_s} · "
                                    f"{'  '.join(f'{k}:{v}' for k, v in list(_scores.items())[:4])}")

                    # 事实校验：用证据池对照报告断言（借鉴外部评测 fact_check）
                    # 让 pipeline 内部也能发现"断言无法验证/与证据矛盾"，驱动改进循环
                    try:
                        _fact_evidence = [e.content for e in self.evidence_pool
                                          if hasattr(e, "content") and e.content][:30]
                        if _fact_evidence and len(narrative) > 200:
                            async def _fact_chat_json(messages, **kwargs):
                                kwargs.pop("timeout", None)
                                return await asyncio.wait_for(
                                    self._chat_json(messages, role="reviewer"),
                                    timeout=60.0)

                            _fact_result = await extract_and_verify_facts(
                                narrative[:6000], _fact_evidence, _fact_chat_json)
                            _fact_score = _fact_result.get("fact_score")
                            _fact_pass = _fact_result.get("fact_pass", 0)
                            _fact_total = _fact_result.get("fact_total", 0)
                            _fact_gaps = _fact_result.get("gaps", [])

                            self.run.quality_eval["fact_score"] = _fact_score
                            self.run.quality_eval["fact_pass"] = _fact_pass
                            self.run.quality_eval["fact_total"] = _fact_total

                            if _fact_total > 0:
                                yield self._ev("report", "thinking",
                                                f"事实校验(第{_qr+1}轮)",
                                                f"断言 {_fact_pass}/{_fact_total} 通过 · "
                                                f"score={_fact_score}")
                                # 把事实问题合并到 issues 驱动补证
                                if _fact_gaps:
                                    _issues = list(_issues) + [
                                        f"[事实校验] {g}" for g in _fact_gaps[:3]
                                    ]
                                    self.run.quality_eval["issues"] = _issues
                    except asyncio.TimeoutError:
                        yield self._ev("report", "thinking", "事实校验超时", "跳过本轮事实校验")
                    except Exception:  # noqa: BLE001
                        logger.warning("事实校验失败，跳过", exc_info=True)

                    if _total >= 30 and _min_s >= 3:
                        break
                    yield self._ev("report", "thinking", f"报告改进(第{_qr+1}轮)",
                                    f"问题：{'; '.join(str(i) for i in _issues[:2])[:100]}")
                    # 历史评测发现作为自知提示（非针对性复用），只提示不强制
                    if _qr == 0 and _prior_eval_hints:
                        yield self._ev("report", "thinking",
                                        "历史评测自知提示",
                                        "过往外部评测中 Agent 常见不足（仅作自知参考，不强制针对补全）：\n"
                                        + "\n".join(f"- {h}" for h in _prior_eval_hints[:4]))
                    # ---- 评测驱动迭代闭环：根据 issues 定向搜索补充证据 ----
                    _eval_new_evidence = 0
                    if self._search_available() and _issues:
                        try:
                            _search_plan = await asyncio.wait_for(self._chat_json(
                                prompts.eval_driven_search_prompt(
                                    self.run.query, _issues, _scores),
                                role="analyst"), timeout=30.0)
                            _search_plan = _search_plan if isinstance(_search_plan, dict) else {}
                            _补证查询 = _search_plan.get("补证查询", []) or []
                            if isinstance(_补证查询, str):
                                _补证查询 = [_补证查询]
                            _补证查询 = [_safe_str(q, 200) for q in _补证查询 if q][:4]
                            for _sq in _补证查询:
                                yield self._ev("report", "search", "评测驱动补证搜索", _sq)
                                _results = await asyncio.to_thread(
                                    self._counter_search, _sq, 3)
                                for _res in _results[:2]:
                                    _ev = Evidence(
                                        content=_res["content"][:400],
                                        source_url=_res["url"],
                                        source_title=_res["title"],
                                        tier=self._guess_tier(_res["url"]),
                                        supports=True,
                                    )
                                    _before = len(self.evidence_pool)
                                    _eidx = self._add_evidence(_ev)
                                    if len(self.evidence_pool) > _before:
                                        _eval_new_evidence += 1
                                        yield self._ev("report", "evidence",
                                                        f"补证证据 [{_eidx}]",
                                                        _ev.content[:140],
                                                        url=_ev.source_url,
                                                        source=_ev.source_title,
                                                        tier=_ev.tier_label)
                        except Exception:  # noqa: BLE001
                            logger.warning("评测驱动搜索失败，跳过补证", exc_info=True)
                    if _eval_new_evidence > 0:
                        yield self._ev("report", "thinking", "评测驱动补证完成",
                                        f"新增 {_eval_new_evidence} 条证据 · 更新 evidence_summary")
                        # 刷新 evidence_summary 供后续重写使用
                        evidence_summary = self._digest(query=_narrative_query, max_items=30, content_len=150)
                    # ---- 评测驱动补证结束，继续重写报告 ----
                    # 用拆分后的两段重新生成洞察层，直接覆写 seg_core/seg_outlook（命名追踪，无索引错位）
                    try:
                        _core2 = await asyncio.wait_for(self._chat_text(
                            prompts.narrative_insights_core_prompt(profile_json, insights_digest, evidence_summary,
                                                                   same_src, is_pub, is_ind,
                                                                   exec_summary=seg_facts),
                            role="reviewer", timeout=400.0, retries=1), timeout=420.0)
                        _core2 = self._strip_preamble(_extract_text(_core2))
                        _outlook2 = await asyncio.wait_for(self._chat_text(
                            prompts.narrative_insights_outlook_prompt(profile_json, outlook_insights, evidence_summary,
                                                                      same_src, is_pub, is_ind,
                                                                      core_findings=_core2 or seg_core),
                            role="reviewer", timeout=400.0, retries=1), timeout=420.0)
                        _outlook2 = self._strip_preamble(_extract_text(_outlook2))
                        if _core2 or _outlook2:
                            if _core2:
                                seg_core = _core2
                            if _outlook2:
                                seg_outlook = _outlook2
                            seg_fallback = ""  # 有真实洞察层后撤掉兜底段
                            narrative = _assemble()
                            self.run.narrative_md = narrative
                    except Exception:
                        break
                except Exception:
                    break

            # 非必选：把 pipeline 内部 quality_eval 的发现存到 eval_feedback，
            # 供下次同主题 pipeline 运行时读取泛化自知。失败静默。
            try:
                _qe = self.run.quality_eval or {}
                if _qe and _qe.get("total") is not None:
                    save_eval_feedback(self.run.query, {
                        "run_id": self.run.id,
                        "source": "internal_quality_eval",
                        "agent_total_score": _qe.get("total"),
                        "fact_score": _qe.get("fact_score"),
                        "fact_pass": _qe.get("fact_pass", 0),
                        "fact_total": _qe.get("fact_total", 0),
                        "min_score": _qe.get("min_score"),
                        "passed": _qe.get("passed"),
                        "gaps": _qe.get("issues", []),
                        "evidence_count": len(self.evidence_pool),
                    })
            except Exception:  # noqa: BLE001
                pass

            # 【改进3】段间衔接微手术（替代旧版全文润色）
            # 只生成修补指令（JSON），由代码精准执行插入/删除，token 消耗降 60%
            try:
                _segs = [s for s in (seg_facts, seg_core, seg_outlook, seg_fallback) if s]
                _pre_body = _normalize_narrative("\n\n".join(_segs))
                if _pre_body and len(_pre_body) > 200:
                    _polish_raw = await asyncio.wait_for(self._chat_json(
                        prompts.narrative_polish_prompt(_pre_body),
                        role="reviewer"), timeout=60.0)
                    _polish_raw = _polish_raw if isinstance(_polish_raw, dict) else {}
                    _transitions = _polish_raw.get("transitions", [])
                    _applied = 0
                    _polished_body = _pre_body
                    if isinstance(_transitions, list):
                        for tr in _transitions[:4]:
                            if not isinstance(tr, dict):
                                continue
                            op = tr.get("操作", "")
                            content = tr.get("内容", "")
                            location = tr.get("位置", "")
                            if not content:
                                continue
                            if "插入" in op and content:
                                # 在目标段开头插入过渡句
                                if "核心发现" in location:
                                    _marker = "## 核心发现"
                                    if _marker in _polished_body:
                                        _polished_body = _polished_body.replace(
                                            _marker, f"{_marker}\n\n{content}", 1)
                                        _applied += 1
                                elif "展望" in location:
                                    _marker = "## 展望与关注点"
                                    if _marker in _polished_body:
                                        _polished_body = _polished_body.replace(
                                            _marker, f"{_marker}\n\n{content}", 1)
                                        _applied += 1
                                elif "风险" in location:
                                    _marker = "## 风险与不确定性"
                                    if _marker in _polished_body:
                                        _polished_body = _polished_body.replace(
                                            _marker, f"{_marker}\n\n{content}", 1)
                                        _applied += 1
                            elif "删除" in op and content and len(content) >= 6:
                                # 删除重复片段
                                if content in _polished_body:
                                    # 只删除第二次出现
                                    _first = _polished_body.find(content)
                                    _second = _polished_body.find(content, _first + len(content))
                                    if _second > 0:
                                        _polished_body = (_polished_body[:_second]
                                                         + _polished_body[_second + len(content):])
                                        _applied += 1
                    if _applied > 0:
                        narrative = _assemble(_polished_body)
                        self.run.narrative_md = narrative
                        yield self._ev("report", "thinking", "段间衔接修补完成",
                                        f"应用 {_applied} 条修补指令 · {len(narrative)} 字符")
                    else:
                        yield self._ev("report", "thinking", "段间衔接检查通过",
                                        "无需修补 · 各段衔接自然")
            except Exception:  # noqa: BLE001
                logger.warning("段间衔接修补失败，保留原版本", exc_info=True)

            # —— 结构不变量自动校验（自我发现问题）——
            # 用真实 LLM 输出作为校验对象，不依赖构造输入
            # 校验失败 -> 自动做一次确定性修复，再复检；仍失败才暴露给用户。
            _violations = self._check_structure_invariants(self.run.narrative_md)
            if _violations:
                repaired, remaining = self._repair_structure_invariants_once(self.run.narrative_md, _violations)
                if repaired != self.run.narrative_md:
                    self.run.narrative_md = repaired
                if remaining:
                    yield self._ev("report", "thinking", "⚠️ 结构不变量校验·发现问题",
                                    f"{len(remaining)} 项违规: {'; '.join(remaining[:3])}")
                else:
                    yield self._ev("report", "thinking", "结构不变量自动修复通过",
                                    f"已修复 {len(_violations)} 项违规 · {len(self.run.narrative_md)} 字符")
            else:
                yield self._ev("report", "thinking", "结构不变量校验通过",
                                f"8 项规则全部通过 · {len(self.run.narrative_md)} 字符")

            if diag_parts:
                self.run.status = "partial"
                yield self._ev("report", "narrative_ready",
                               "完整分析文档已生成（部分段降级）",
                               f"{len(narrative)} 字符 · 终审 {reviewer} · {';'.join(diag_parts)}", length=len(narrative))
            else:
                yield self._ev("report", "narrative_ready", "完整分析文档已生成（终审模型·两段+附录）",
                                f"{len(narrative)} 字符 · 终审 {reviewer}", length=len(narrative))
        else:
            # 所有段都失败：结构化兜底（执行摘要表+基本事实+附录，不裸倒证据）
            self.run.status = "partial"
            self.run.error = "完整文档生成失败：" + ";".join(diag_parts)[:120]
            fb = self._build_narrative_fallback() + footer
            self.run.narrative_md = fb
            yield self._ev("report", "diagnosis", "完整文档生成失败·结构化兜底",
                            f"终审模型各段均超时/出错({';'.join(diag_parts)})。已用结构化模板兜底（执行摘要表+基本事实+附录），不裸倒证据。", length=len(fb))

        # —— 持久化存储：写入 Experience + Reflection 蒸馏到 Domain/Behavior ——
        # 用局部变量记录状态（不在 _report 里改 self.run.status，让 run_pipeline 统一设）
        _episodic_status = "partial" if diag_parts else "done"
        try:
            run_summary = memory_store.build_run_summary(self.run)
            run_summary["status"] = _episodic_status  # 覆盖为最终状态
            memory_store.save_episodic(run_summary)
            yield self._ev("report", "thinking", "记忆系统·写入 Experience",
                            f"任务摘要已保存（{len(run_summary.get('insights', []))} 条洞察）")
        except Exception:  # noqa: BLE001
            logger.warning("写入 Experience 记忆失败，跳过", exc_info=True)
            run_summary = {}

        if run_summary and not self.demo:
            # Reflection: 蒸馏到 Domain + Behavior（demo 模式跳过防污染）
            try:
                recent_eps = memory_store.load_recent_episodes(n=3)
                existing_knowledge = memory_store.load_existing_knowledge_summary()
                reflection_raw = await asyncio.wait_for(self._chat_json(
                    prompts.meta_reflection_prompt(
                        json.dumps(run_summary, ensure_ascii=False)[:4000],
                        json.dumps(recent_eps, ensure_ascii=False)[:2000],
                        json.dumps(existing_knowledge, ensure_ascii=False)[:2000],
                    ), role="reviewer"), timeout=60.0)
                reflection = reflection_raw if isinstance(reflection_raw, dict) else {}
                missed = reflection.get("missed_challenges", []) or []
                pol_updates = reflection.get("policy_updates", []) or []
                ind_updates = reflection.get("industry_pattern_updates", []) or []
                assessment = _safe_str(reflection.get("overall_assessment", ""), 200)
                # 策略更新 → Behavior
                if isinstance(pol_updates, list) and pol_updates:
                    memory_store.apply_policy_updates(pol_updates)
                # 行业模式 → Domain
                if isinstance(ind_updates, list) and ind_updates:
                    _tk = self.run.profile.template_key if self.run.profile else "generic"
                    from collections import defaultdict as _dd
                    _groups: dict[str, list[dict]] = _dd(list)
                    for u in ind_updates:
                        if isinstance(u, dict):
                            _pt = u.get("playbook_type", "failure_playbook")
                            _groups[_pt].append(u)
                    for _pt, _items in _groups.items():
                        memory_store.update_industry_rag(_tk, _pt, _items)
                # 策略卡 → Behavior
                strategy_result = {"activated": [], "pending": [], "rejected": []}
                try:
                    strategy_candidates = memory_store.generate_strategy_candidates(run_summary)
                    strategy_result = memory_store.promote_strategy_candidates(
                        strategy_candidates, source_run_id=self.run.id
                    )
                except Exception:  # noqa: BLE001
                    logger.warning("生成/晋升 strategy cards 失败，跳过", exc_info=True)
                yield self._ev("report", "thinking", "记忆系统·Reflection 蒸馏完成",
                                f"遗漏挑战 {len(missed) if isinstance(missed, list) else 0} 条 · "
                                f"Behavior 策略更新 {len(pol_updates) if isinstance(pol_updates, list) else 0} 条 · "
                                f"Domain 行业模式更新 {len(ind_updates) if isinstance(ind_updates, list) else 0} 条 · "
                                f"策略卡激活 {len(strategy_result.get('activated', []))} 条"
                                + (f" · {assessment[:60]}" if assessment else ""))
            except Exception:  # noqa: BLE001
                logger.warning("Reflection 蒸馏失败，跳过（可选增强）", exc_info=True)
        elif self.demo:
            yield self._ev("report", "thinking", "记忆系统·Demo模式跳过 Reflection",
                            "demo 模式不写入真实学习记忆，避免脚本化假数据污染策略库")

    def _select_report_insights(self) -> tuple[list[Insight], list[Insight]]:
        """筛选进入报告的洞察 —— 只保留关键、确定、有启发价值的。

        返回 (core, risk)：
        - core: 进入核心发现段的精选洞察（SUPPORTED + 高价值 QUESTIONABLE）
        - risk: 进入风险段的低重要性存疑洞察

        筛选规则（代码确定，非 LLM）：
        排除: REFUTED / UNVERIFIABLE / 置信度<0.3 / 无实质推理(<20字)
        保留: SUPPORTED+conf>=0.35 / QUESTIONABLE+conf>=0.3 且推理有分析增量(非复述)
        排序: confidence×0.5 + evidence_strength×0.3 + importance×0.2
        上限: 公司 4 条 / 行业 6 条

        QUESTIONABLE 价值过滤（issue #5）：
        - 存疑论点必须提供真正的分析增量，而非为了陈列而陈列
        - 判断标准：推理长度>50字 且 不只是复述事实（含「意味着/反映/说明/暗示/指向/因为」等分析性词汇）
        - 低价值存疑洞察直接排除，不进 risk 段
        """
        core: list[Insight] = []
        risk: list[Insight] = []
        # 分析性词汇集合——判断推理是否含真正的分析增量而非纯复述
        ANALYTICAL_CUES = ("意味着", "反映", "说明", "暗示", "指向", "因为", "由于",
                           "表明", "预示", "验证", "背离", "矛盾", "转折", "拐点",
                           "实质", "核心矛盾", "关键在于", "归根", "本质上",
                           "所以", "因此", "从而", "导致", "驱动", "根本")

        def _has_analytical_value(ins: Insight) -> bool:
            """判断存疑洞察是否有分析价值——推理有分析增量而非纯事实复述。"""
            r = (ins.reasoning or "").strip()
            if len(r) < 50:
                return False
            r_lower = r.lower()
            return any(cue in r for cue in ANALYTICAL_CUES)

        for ins in self.run.insights:
            if ins.needs_human:  # 占位/需人工介入的洞察不自动进入报告（止血）
                continue
            # 不可证伪的洞察不直接排除——如果是逻辑推导型（推理充分且含分析性词汇），仍可进入报告
            if not ins.is_falsifiable:
                # 逻辑推导型洞察：推理>80字 + 含分析性词汇 + 有证据引用 → 保留进核心段标注"逻辑推导型"
                if ins.reasoning and len(ins.reasoning.strip()) > 80 and _has_analytical_value(ins):
                    if ins.evidence:  # 至少有1条前置事实支撑
                        core.append(ins)
                continue
            if ins.verdict in (Verdict.REFUTED, Verdict.UNVERIFIABLE):
                continue
            if not ins.reasoning or len(ins.reasoning.strip()) < 20:
                continue
            if ins.verdict == Verdict.SUPPORTED and ins.confidence >= 0.35:
                core.append(ins)
            elif ins.verdict == Verdict.QUESTIONABLE and ins.confidence >= 0.3:
                # 存疑洞察必须有分析价值才进核心段（issue #5：不为陈列而陈列）
                if _has_analytical_value(ins):
                    core.append(ins)  # 存疑但有价值 → 核心发现段带标注
            elif ins.verdict == Verdict.QUESTIONABLE and ins.confidence >= 0.2:
                # 低置信存疑进风险段——同样需要分析价值
                if _has_analytical_value(ins):
                    risk.append(ins)
            elif ins.verdict == Verdict.SUPPORTED and ins.confidence >= 0.25:
                risk.append(ins)  # 低置信但成立 → 风险段简述
        # 排序：综合置信度、证据强度、关键问题相关性
        def _score(ins: Insight) -> float:
            importance = 0.5
            if self.run.profile and self.run.profile.key_questions:
                claim_text = (ins.claim + " " + ins.reasoning).lower()
                for q in self.run.profile.key_questions:
                    if any(w.lower() in claim_text for w in q if len(w) > 1):
                        importance = 0.8
                        break
            return ins.confidence * 0.5 + ins.evidence_strength() * 0.3 + importance * 0.2
        core.sort(key=_score, reverse=True)
        risk.sort(key=_score, reverse=True)
        # 上限
        is_industry = self.run.profile and self.run.profile.kind == "industry"
        max_core = 6 if is_industry else 3
        if len(core) > max_core:
            overflow = core[max_core:]
            risk.extend([i for i in overflow if i.verdict == Verdict.QUESTIONABLE and _has_analytical_value(i)])
            core = core[:max_core]
        # 风险段也设上限——避免为陈列而陈列（issue #5）
        max_risk = 3 if is_industry else 2
        if len(risk) > max_risk:
            risk = risk[:max_risk]
        return core, risk

    def _insights_digest(self, insights: list[Insight] | None = None) -> str:
        """给 narrative prompt 用的洞察摘要（含九维挑战结论与自我迭代状态）。
        insights 参数为空时用全部洞察；传入精选列表时只格式化精选的。"""
        if insights is None:
            insights = self.run.insights
        verdict_label = {
            Verdict.SUPPORTED: "成立", Verdict.QUESTIONABLE: "存疑",
            Verdict.REFUTED: "推翻", Verdict.UNVERIFIABLE: "不可检验",
        }
        lines = []
        for i, ins in enumerate(insights, 1):
            ev_ids = [self._idx_of(e) for e in ins.evidence]
            # 存疑洞察加标注，提示 LLM 在核心发现段标注不确定性
            caveat_tag = " · [存疑·带标注]" if ins.verdict == Verdict.QUESTIONABLE else ""
            # 提取最新一次九维挑战的 overall 与存疑维度
            overall_tag = ""
            open_dims_str = ""
            if ins.falsifications:
                fr = ins.falsifications[-1]
                oa = getattr(fr, "overall_assessment", "")
                if oa:
                    overall_tag = f" · 九维:{oa}"
                chs = getattr(fr, "challenges", []) or []
                high_dims = [_DIM_LABELS.get(c.get("dimension", "?"), c.get("dimension", "?"))
                             for c in chs if isinstance(c, dict)
                             and str(c.get("severity", "")).lower() in ("high", "medium")]
                if high_dims:
                    open_dims_str = f" · 存疑维度:{','.join(high_dims[:3])}"
            refine_tag = f" · {ins.refinement_note[:60]}" if ins.refinement_note else ""
            lines.append(
                f"{i}. [{verdict_label.get(ins.verdict, '?')}/置信{ins.confidence:.0%}{overall_tag}{open_dims_str}{caveat_tag}] "
                f"({ins.section}) {ins.claim}\n   推理链:{ins.reasoning[:320]}\n"
                f"   可证伪边界:{ins.falsifiable_condition[:140] or '无'}\n"
                f"   证据:{ev_ids}{refine_tag}"
            )
        return "\n".join(lines)

    def _build_report_md(self) -> str:
        """结构化兜底报告（narrative 拆分调用也失败时用）—— 带执行摘要表 + 紧凑洞察 + 附录，
        不再把证据全文 dump 进正文（避免网页 boilerplate 垃圾污染）。"""
        p = self.run.profile
        tpl = get_template(p.template_key)
        review_note = (
            "同源审查降级（仅单一模型可用，红队非异源）" if self.run.same_source_review
            else "双模型红队对抗证伪"
        )
        verdict_label = {
            Verdict.SUPPORTED: "✅ 成立", Verdict.QUESTIONABLE: "⚠️ 存疑",
            Verdict.REFUTED: "❌ 已推翻", Verdict.UNVERIFIABLE: "❓ 不可检验",
        }
        lines = [
            f"# {p.name} · 商业分析报告",
            "",
            "## 执行摘要",
            "",
            "| # | 核心结论 | 裁决 | 置信度 | 证据 |",
            "|---|---|---|---|---|",
        ]
        # 使用精选洞察（筛选后的核心发现），不罗列全部
        core_ins, risk_ins = self._select_report_insights()
        for i, ins in enumerate(core_ins, 1):
            _nums = [self._idx_of(e) for e in ins.evidence]
            ev_ids = ",".join(str(n) for n in _nums if n > 0) or "—"
            lines.append(
                f"| {i} | {ins.claim[:60]} | {verdict_label.get(ins.verdict, ins.verdict.value)} | "
                f"{ins.confidence:.0%} | {ev_ids} |"
            )
        lines += [
            "",
            f"> 数据截至 {self.run.data_as_of or '未明确'} · 数据源: {', '.join(self.run.data_sources_used) or '未配置'} · "
            f"证据 {len(self.evidence_pool)} 条 · {review_note}",
            "",
            "## 基本事实",
            "",
        ]
        # 行业报告不显示"上市/非上市"标签，也不产出公司档案表——聚焦行业总量/格局/龙头
        if p.kind == "industry":
            lines += [
                f"- **行业**：{p.name}　|　分类：{p.industry}　|　框架：{tpl['label']}",
                f"- **核心驱动**：{p.business_model}",
                f"- **主要龙头**：{', '.join(p.leaders) if p.leaders else '—'}"
                + (f"　|　对标公司：{', '.join(p.peers) if p.peers else '—'}" if p.peers else ""),
            ]
        else:
            lines += [
                f"- **对象**：{p.name}（{'上市' if p.is_public else '非上市'}{f'·{p.ticker}' if p.ticker else ''}）",
                f"- **行业**：{p.industry}　|　框架：{tpl['label']}",
                f"- **商业模式**：{p.business_model}",
                f"- **对标/龙头**：{', '.join(p.peers) if p.peers else '—'}"
                + (f"　|　龙头：{', '.join(p.leaders)}" if getattr(p, 'leaders', None) else ""),
            ]
        # 财务时序（若有）
        if self.run.financials:
            lines += ["", "| 期间 | 营收 | 净利润 | 营收增速 |", "|---|---|---|---|"]
            for r in self.run.financials:
                rg = f"{r.get('rev_growth')}%" if r.get('rev_growth') is not None else "—"
                lines.append(
                    f"| {r.get('period','')} | {r.get('revenue','—')} | {r.get('net_income','—')} | {rg} |"
                )
        lines += ["", "## 核心洞察", ""]
        # 按动态维度分组（section 名出现在报告里，便于校验维度覆盖）
        from collections import defaultdict
        by_sec = defaultdict(list)
        for ins in core_ins:  # 只用精选洞察，不罗列全部
            by_sec[ins.section].append(ins)
        idx = 0
        for sec, items in by_sec.items():
            lines.append(f"### {sec}")
            for ins in items:
                idx += 1
                ev_ids = ",".join(str(self._idx_of(e)) for e in ins.evidence if self._idx_of(e))
                lines.append(f"**{idx}. {ins.claim}**")
                lines.append(f"裁决：{verdict_label.get(ins.verdict, ins.verdict.value)}　置信度：{ins.confidence:.0%}"
                             + ("　⚑ 需人工复核" if ins.needs_human else ""))
                if ins.reasoning:
                    lines.append("")
                    lines.append(ins.reasoning)
                if ins.falsifiable_condition:
                    lines.append(f"\n> 可证伪条件：{ins.falsifiable_condition}")
                if ev_ids:
                    lines.append(f"\n*证据：[{ev_ids}]*")
                lines.append("")
        # 数据来源与免责声明（简短）
        lines += [
            "---",
            f"*证据 {len(self.evidence_pool)} 条 · {review_note} · 数据源: {', '.join(self.run.data_sources_used) or '未配置'}"
            + (f" · 数据截至 {self.run.data_as_of}" if self.run.data_as_of else "")
            + (" · 同源审查降级，结论需人工复核。" if self.run.same_source_review else "")
            + "*",
            "*本报告基于公开信息，不构成投资建议。*",
        ]
        # 附录（代码生成，紧凑）
        lines.append(self._build_appendix_md())
        lines += [
            "",
            "---",
            "",
            f"数据截至 {self.run.data_as_of or '未明确'} · 数据源: {', '.join(self.run.data_sources_used) or '未配置'}",
            "",
            "本报告基于公开信息，不构成投资建议。",
        ]
        return "\n".join(lines)

    def _format_apa_reference(self, ev: Evidence, idx: int) -> str:
        """Compatibility wrapper around reporting.appendix."""
        return format_apa_reference(ev, idx)

    def _check_structure_invariants(self, narrative: str) -> list[str]:
        """Compatibility wrapper around reporting.structure."""
        return check_structure_invariants(narrative, self.run.insights)

    def _repair_structure_invariants_once(self, narrative: str, violations: list[str] | None = None) -> tuple[str, list[str]]:
        """Compatibility wrapper around reporting.structure."""
        return repair_structure_invariants_once(narrative, violations, self.run.insights)

    def _collect_data_gaps(self, body: str = ""):
        """Compatibility wrapper around reporting.appendix."""
        return collect_data_gaps(self.run, body)

    def _build_comparison_matrix(self) -> str:
        """【改进1】零 LLM 调用，从证据池中提取竞品/龙头关键指标，构建对比矩阵。
        原理：证据池中很可能已有 peers/leaders 的数据（Collect 阶段搜索了竞品），
        此方法用关键词匹配把它们提取出来，构成"自身 vs 竞品"对比表。
        这给核心发现段提供了结构化的竞争分析素材，让 LLM 无需自己去证据里翻找。
        """
        if not self.run.profile:
            return ""
        peers = (self.run.profile.peers or [])[:5]
        leaders = (self.run.profile.leaders or [])[:4]
        target_name = self.run.profile.name or ""
        if not target_name or (not peers and not leaders):
            return ""

        # 收集所有比较实体（去重）
        comp_entities = []
        _seen = {target_name.lower()}
        for p in peers + leaders:
            if p and p.lower() not in _seen:
                comp_entities.append(p)
                _seen.add(p.lower())
        if not comp_entities:
            return ""

        # 从证据池中提取每个实体相关的关键数据片段
        entity_snippets: dict[str, list[str]] = {e: [] for e in comp_entities}
        entity_snippets[target_name] = []

        for ev_idx, ev in self.evidence_pool.items():
            content = getattr(ev, "content", "") or ""
            if not content:
                continue
            # 检查该证据属于哪个实体
            content_lower = content.lower()
            for entity in [target_name] + comp_entities:
                if entity.lower() in content_lower:
                    # 提取含数字的句子作为关键数据
                    sentences = re.split(r'[。；;！!？?\n]', content[:400])
                    for sent in sentences:
                        if re.search(r'\d+[%％亿万]|\d+\.\d+', sent) and len(sent) > 10:
                            entity_snippets[entity].append(f"{sent.strip()[:100]} [^{ev_idx}]")
                            if len(entity_snippets[entity]) >= 3:
                                break

        # 只有当至少有 1 个竞品有数据时才生成矩阵
        comp_with_data = [e for e in comp_entities if entity_snippets.get(e)]
        if not comp_with_data:
            return ""

        # 构建对比矩阵 Markdown
        lines = ["\n\n【竞争对比矩阵（代码提取，供核心发现段分析竞争维度）】\n"]
        lines.append(f"分析主体: {target_name}")
        lines.append(f"对标公司: {', '.join(comp_with_data[:4])}\n")

        for entity in [target_name] + comp_with_data[:4]:
            snippets = entity_snippets.get(entity, [])
            if snippets:
                label = "【本体】" if entity == target_name else "【对标】"
                lines.append(f"{label} {entity}: {' | '.join(snippets[:3])}")

        lines.append("")
        lines.append("分析提示: 请在核心发现中基于上述对比数据，分析竞争差异与相对优劣势，"
                     "不只是描述自身指标。")

        return "\n".join(lines)

    def _build_appendix_md(self, extra_text: str = "") -> str:
        """Compatibility wrapper around reporting.appendix."""
        return build_appendix_md(self.run, self.evidence_pool, self._idx_of, extra_text)

    def _build_narrative_fallback(self) -> str:
        """narrative 两段调用都失败时的最终兜底：用 _build_report_md 结构化模板（已含执行摘要表+基本事实+附录）。
        比裸倒证据干净，至少有可读结构。"""
        return self._build_report_md()

    def _build_insights_fallback(self) -> str:
        """洞察层 LLM 调用全部失败时的降级兜底：从精选洞察数据直接生成结构化洞察段。
        不靠 LLM 写长文，纯数据模板，保证至少有可读的核心发现+风险+终审结构。"""
        core_ins, risk_ins = self._select_report_insights()
        if not core_ins and not risk_ins:
            return ""
        lines = ["# 核心发现（降级模式·结构化模板）", ""]
        for i, ins in enumerate(core_ins[:6], 1):
            conf_pct = round((ins.confidence or 0) * 100)
            verdict_label = {"supported": "成立", "questionable": "存疑", "refuted": "推翻"}.get(ins.verdict, ins.verdict)
            lines.append(f"## {i}. {ins.claim[:80]}")
            lines.append(f"**(置信度 {conf_pct}%·{verdict_label})**\n")
            if ins.reasoning:
                lines.append(f"{ins.reasoning[:200]}\n")
            # 证据引用：ins.evidence 是 Evidence 对象列表，需转成证据池编号
            ev_nums = [self._idx_of(e) for e in ins.evidence[:5]]
            ev_refs = [f"[^{n}]" for n in ev_nums if n > 0]
            if ev_refs:
                lines.append(f"证据支撑：{' '.join(ev_refs)}\n")
            # 九维挑战结论 + 自我迭代状态
            for fr in (ins.falsifications or [])[:1]:
                oa = getattr(fr, "overall_assessment", "")
                if oa:
                    lines.append(f"九维挑战：{oa}\n")
                note = getattr(fr, "note", "") or getattr(fr, "red_team_challenge", "")
                if note:
                    lines.append(f"红队质疑：{str(note)[:120]}\n")
            if ins.refinement_note:
                lines.append(f"迭代结论：{ins.refinement_note[:150]}\n")
        # 风险（用筛选出的 risk_insights）
        if risk_ins:
            lines.append("# 风险与不确定性\n")
            for ins in risk_ins[:4]:
                lines.append(f"- **{ins.claim[:60]}** — 置信度仅 {round((ins.confidence or 0)*100)}%，{'红队提出有效反证' if ins.falsifications else '证据不足'}\n")
        # 终审
        lines.append("# 终审备注\n")
        lines.append("- ①观点突出：降级模式，观点来自洞察数据而非 LLM 写作，可能缺乏论证深度\n")
        lines.append("- ②论证充分：降级模式，仅有结论+证据编号，缺推理过程\n")
        lines.append("- ③结构 MECE：通过（按置信度排序，supported→questionable→refuted）\n")
        lines.append("- ④数字可溯源：通过（证据编号 [^N] 对应附录）\n")
        lines.append("\n> ⚠ 本段为洞察层 LLM 调用超时后的降级兜底，由代码从洞察数据直接生成。建议重跑或换模型获取完整论证。\n")
        return "\n".join(lines)
