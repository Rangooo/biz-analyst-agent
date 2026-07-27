"""Deterministic report-planning helpers.

The research pipeline has already spent most of its cost collecting and
falsifying evidence.  These helpers preserve that work at the report handoff
without adding another LLM call.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
import re
from urllib.parse import urlparse

from schemas import AnalysisRun, Evidence, Insight, ObjectProfile, SourceTier, Verdict


def _root_domain(url: str) -> str:
    try:
        host = (urlparse(url).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""
    parts = [part for part in host.split(".") if part and part != "www"]
    if len(parts) >= 3 and ".".join(parts[-2:]) in {"com.cn", "net.cn", "org.cn", "gov.cn"}:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def should_use_cautious_writing(
    insights: Iterable[Insight],
    *,
    is_industry: bool,
) -> bool:
    """Return whether an industry report lacks enough verified core claims."""
    if not is_industry:
        return False
    candidates = list(insights)
    if not candidates:
        return True
    supported = [
        insight for insight in candidates
        if insight.verdict == Verdict.SUPPORTED and insight.confidence >= 0.70
    ]
    return len(supported) < 2 or len(supported) / len(candidates) < 0.5


def build_cautious_gap_queries(
    profile: ObjectProfile,
    *,
    year: int,
) -> list[str]:
    """Build two deterministic, high-yield queries without an LLM planner."""
    subject = (profile.name or profile.industry or "").strip()
    return [
        f"{subject} {year - 1} {year} 行业白皮书 市场规模 竞争格局 PDF",
        f"{subject} 监管 标准 政策 官方 gov.cn",
    ]


def select_cautious_report_evidence_ids(
    evidence_pool: dict[int, Evidence],
    candidate_ids: Iterable[int],
    profile: ObjectProfile,
    *,
    max_items: int = 32,
) -> list[int]:
    """Build a clean handoff for weak-evidence industry reports."""
    subject_terms: set[str] = set()
    for raw in (profile.name, profile.industry):
        term = re.sub(r"(中国|行业|市场|研究|分析|\s)+", "", raw or "")
        if len(term) >= 2:
            subject_terms.add(term)
    subject_terms |= {term[:2] for term in list(subject_terms) if len(term) >= 2}
    trusted_domains = (
        "gov.cn", "moa.gov.cn", "stats.gov.cn", "cninfo.com.cn",
        "sse.com.cn", "szse.cn", "news.cn", "xinhuanet.com",
        "cctv.com", "kpmg.com", "pwc.", "deloitte.", "ey.com",
    )
    blocked_domains = (
        "eastmoney.com", "qq.com", "sohu.com", "renrendoc.com",
        "baidu.com",
    )
    research_markers = (
        "年报", "年度报告", "公告", "白皮书", "市场报告", "监管",
        "管理办法", "国家标准", "行业标准", "统计公报",
    )
    candidates = list(dict.fromkeys(candidate_ids))
    candidate_set = set(candidates)
    ordered = list(dict.fromkeys([*candidates, *sorted(evidence_pool)]))
    ranked: list[tuple[tuple[int, int, int, int], int]] = []
    for order, idx in enumerate(ordered):
        evidence = evidence_pool.get(idx)
        if evidence is None:
            continue
        title = evidence.source_title or ""
        content = evidence.content or ""
        url = (evidence.source_url or "").lower()
        haystack = re.sub(r"\s+", "", f"{title} {content[:1200]}")
        if subject_terms and not any(term in haystack for term in subject_terms):
            continue
        trusted = any(domain in url for domain in trusted_domains)
        research = any(marker in title for marker in research_markers)
        blocked = any(domain in url for domain in blocked_domains)
        if blocked and not research:
            continue
        if not trusted and not research and int(evidence.tier) > 5:
            continue
        if len(content.strip()) < 60 and not trusted:
            continue
        ranked.append(((
            0 if trusted else (1 if research else 2),
            int(evidence.tier),
            0 if idx in candidate_set else 1,
            order,
        ), idx))
    ranked.sort()
    return [idx for _, idx in ranked[:max_items]]


def select_report_evidence_ids(
    evidence_pool: dict[int, Evidence],
    semantic_ids: Iterable[int],
    insights: Iterable[Insight],
    idx_of: Callable[[Evidence], int],
    max_items: int = 60,
) -> list[int]:
    """Select evidence for writing while guaranteeing claim support survives.

    Order:
    1. evidence explicitly attached to selected insights;
    2. semantic retrieval results;
    3. authoritative/fresh evidence as a final fill.

    This fixes a failure mode where semantic top-k omitted the exact evidence
    behind a verified insight, leaving the writer with an id but no source text.
    """
    selected: list[int] = []
    seen: set[int] = set()

    def add(idx: int) -> None:
        if idx in evidence_pool and idx not in seen and len(selected) < max_items:
            selected.append(idx)
            seen.add(idx)

    for insight in insights:
        supporting = [ev for ev in insight.evidence if ev.supports]
        opposing = [ev for ev in insight.evidence if not ev.supports]
        for ev in [*supporting, *opposing]:
            add(idx_of(ev))

    authoritative = sorted(
        evidence_pool,
        key=lambda idx: (
            int(evidence_pool[idx].tier),
            -evidence_pool[idx].freshness_factor(),
            -float(evidence_pool[idx].importance or 0),
            idx,
        ),
    )
    for idx in semantic_ids:
        add(idx)
    for idx in authoritative:
        add(idx)
    return selected


def _open_challenges(insight: Insight) -> list[str]:
    if not insight.falsifications:
        return []
    challenges = insight.falsifications[-1].challenges or []
    return [
        str(item.get("challenge") or "").strip()[:100]
        for item in challenges
        if isinstance(item, dict)
        and str(item.get("severity") or "").lower() in {"high", "medium"}
        and str(item.get("challenge") or "").strip()
    ][:2]


def _claim_permission(insight: Insight) -> str:
    strength = insight.evidence_strength()
    supporting = [ev for ev in insight.evidence if ev.supports]
    best_tier = min((int(ev.tier) for ev in supporting), default=7)
    independent_domains = {
        _root_domain(ev.source_url) for ev in supporting
        if _root_domain(ev.source_url)
    }
    if best_tier >= 5 or len(independent_domains) < 2:
        return (
            "仅可写为有限样本/单一信源指向的工作假设；"
            "不得外推为全市场事实、必然因果或不可逆趋势"
        )
    if (
        insight.verdict == Verdict.SUPPORTED
        and insight.confidence >= 0.70
        and strength >= 0.55
    ):
        return "可作方向性结论；因果归因仍须有直接证据"
    if insight.verdict == Verdict.SUPPORTED and insight.confidence >= 0.50:
        return "仅可写“证据表明/指向”；不得写“证明、必然、核心驱动”"
    return "必须明确标注存疑或推演，不得进入摘要作为确定事实"


def build_writing_contract(
    run: AnalysisRun,
    evidence_pool: dict[int, Evidence],
    core_insights: list[Insight],
    risk_insights: list[Insight],
    idx_of: Callable[[Evidence], int],
    all_insights: list[Insight] | None = None,
) -> str:
    """Create a compact claim-evidence contract for the final writer."""
    lines = [
        "【Agent写作契约（代码生成，优先级高）】",
        "这不是正文素材，而是写作边界。违反任一条都视为报告错误：",
        "1. 画像字段只用于识别对象，不是事实证据；名称、代码、估值、日期等进入正文必须有证据编号。",
        "2. 每个精确数字/日期/排名须在同句紧邻引用；所引证据必须实际包含该数字或可复算输入。",
        "3. 自行测算必须展示“公式+输入值+输入证据”；否则改为定性表述，不得虚构阈值、概率或影响金额。",
        "4. 不得把ARR写成营收、把融资估值写成市值、把季度值年化、混用币种/单位或不同统计口径。",
        "5. 冲突数据并列写出口径/时间差异；无法解释时不得选一个值作为确定事实。",
        "6. 展望只使用证据已有当前值；无依据的触发阈值写“尚无可靠阈值”，情景可定性且不强制分配概率。",
        "7. 市场份额必须说明统计对象、地区、期间和度量口径；缺口未知时明确写“口径未披露”。",
        "8. 单家公司或少数样本只能说明“样本分化/样本显示”，不得直接外推为行业利润池、行业周期或不可逆趋势。",
        "9. 引用必须支持紧邻主张本身，不能只因含有相同数字或关键词而引用；优先公司公告、监管、统计机构和原始研究。",
        "",
        "【主张—证据账本】",
    ]
    if not core_insights:
        lines.append("- 无通过筛选的核心主张：报告应以事实边界和风险说明为主，不得制造洞察。")

    for pos, insight in enumerate(core_insights, 1):
        support_ids = [idx_of(ev) for ev in insight.evidence if ev.supports and idx_of(ev)]
        counter_ids = [idx_of(ev) for ev in insight.evidence if not ev.supports and idx_of(ev)]
        domains = {
            _root_domain(evidence_pool[idx].source_url)
            for idx in support_ids
            if idx in evidence_pool and _root_domain(evidence_pool[idx].source_url)
        }
        best_tier = min(
            (int(evidence_pool[idx].tier) for idx in support_ids if idx in evidence_pool),
            default=7,
        )
        challenges = _open_challenges(insight)
        lines.extend([
            f"- C{pos}｜{insight.claim[:100]}",
            f"  状态={insight.verdict.value}；置信={insight.confidence:.0%}；"
            f"证据强度={insight.evidence_strength():.2f}；最佳信源=T{best_tier}；"
            f"独立域名={len(domains)}",
            f"  支撑证据={support_ids or '无'}；反证={counter_ids or '无'}",
            f"  表述权限={_claim_permission(insight)}",
        ])
        if challenges:
            lines.append(f"  未解问题={'；'.join(challenges)}")
        if insight.refinement_note:
            lines.append(f"  迭代结论={insight.refinement_note[:120]}")

    if risk_insights:
        lines.append("")
        lines.append("【仅可进入风险段的主张】")
        for insight in risk_insights:
            ids = [idx_of(ev) for ev in insight.evidence if idx_of(ev)]
            lines.append(
                f"- {insight.claim[:100]}｜{insight.verdict.value}/{insight.confidence:.0%}｜证据={ids or '无'}"
            )

    if profile := run.profile:
        coverage = [str(item).strip() for item in profile.sections if str(item).strip()]
        questions = [str(item).strip() for item in profile.key_questions if str(item).strip()]
        if coverage or questions or profile.peers or profile.leaders:
            lines.append("")
            lines.append("【最低覆盖清单】")
            if coverage:
                lines.append(f"- 分析维度：{'；'.join(coverage[:8])}")
            if questions:
                lines.append(f"- 必答问题：{'；'.join(questions[:6])}")
            comparables = list(dict.fromkeys([*(profile.peers or []), *(profile.leaders or [])]))
            if comparables:
                lines.append(f"- 竞争对标：{'、'.join(comparables[:6])}")
            lines.append("- 无可靠证据的维度只说明数据边界，不得为满足覆盖而编造内容。")

            represented = {ins.section for ins in [*core_insights, *risk_insights]}
            omitted_notes: list[str] = []
            candidates = all_insights if all_insights is not None else []
            for section in coverage:
                if section in represented:
                    continue
                viable = [
                    ins for ins in candidates
                    if ins.section == section
                    and ins.verdict not in {Verdict.REFUTED, Verdict.UNVERIFIABLE}
                    and ins.evidence
                ]
                if not viable:
                    continue
                best = max(
                    viable,
                    key=lambda ins: ins.confidence * 0.6 + ins.evidence_strength() * 0.4,
                )
                ids = [idx_of(ev) for ev in best.evidence if idx_of(ev) > 0]
                omitted_notes.append(
                    f"- {section}：{best.claim[:90]}｜{best.verdict.value}/{best.confidence:.0%}"
                    f"｜证据={ids[:5]}｜推理边界：{best.reasoning[:140]}"
                )
            if omitted_notes:
                lines.append("【未入核心发现、但必须简要覆盖的研究结论】")
                lines.extend(omitted_notes[:6])

    boundary_notes: list[str] = []
    profile = run.profile
    if profile and profile.kind == "company" and profile.is_public and not run.financials:
        boundary_notes.append("未形成可靠财务时序：不得生成财务同比、利润率趋势或盈利拐点。")
    if profile and profile.kind == "industry":
        metrics = run.industry_metrics if isinstance(run.industry_metrics, dict) else {}
        if not metrics.get("totals"):
            boundary_notes.append("缺少行业总量时序：TAM/增速/集中度只能在有证据输入时计算。")
        if not metrics.get("prices") and any(
            key in " ".join(profile.sections or []) for key in ("价格", "供需")
        ):
            boundary_notes.append("缺少行业价格时序：不得断言价格拐点。")
    for issue in (run.collect_quality or {}).get("issues", [])[:3]:
        if str(issue).strip():
            boundary_notes.append(str(issue).strip()[:140])

    if boundary_notes:
        lines.append("")
        lines.append("【报告级数据边界】")
        lines.extend(f"- {note}" for note in boundary_notes[:5])

    return "\n".join(lines)


def neutralize_unsupported_scenarios(report: str, grounding_audit: dict) -> str:
    """Deterministically remove invented precision from scenario prose."""
    if not report:
        return report
    flagged = set(grounding_audit.get("uncited_numeric_lines") or [])
    if not flagged:
        return report
    result = report
    for line in flagged:
        stripped = line.strip()
        if not stripped or stripped not in result:
            continue
        scenario = next(
            (name for name in ("乐观情景", "中性情景", "基准情景", "悲观情景")
             if name in stripped),
            "",
        )
        if scenario:
            direction = {
                "乐观情景": "核心经营指标持续改善且得到公开数据验证时成立。",
                "中性情景": "核心经营指标保持稳定时成立。",
                "基准情景": "核心经营指标保持稳定时成立。",
                "悲观情景": "核心经营指标持续恶化时成立。",
            }[scenario]
            prefix = re.match(r"^(\s*[-*]\s*)", line)
            bullet = prefix.group(1) if prefix else "- "
            replacement = f"{bullet}**{scenario}**：{direction}公开证据不足以设定精确目标或概率。"
            result = result.replace(line, replacement, 1)
            continue
        if stripped.startswith("|") and "推演" in stripped:
            cells = line.split("|")
            for idx in range(2, len(cells) - 1):
                if _NUMERIC_TOKEN_RE.search(cells[idx]) and "[^" not in cells[idx]:
                    cells[idx] = " 证据不足，暂不量化 "
            result = result.replace(line, "|".join(cells), 1)
            continue
        if stripped.startswith("|") and ("| 画像" in stripped or "| 证据池画像" in stripped):
            cells = line.split("|")
            for idx in range(1, len(cells) - 1):
                if _NUMERIC_TOKEN_RE.search(cells[idx]) and "[^" not in cells[idx]:
                    cells[idx] = " 未核实 "
            result = result.replace(line, "|".join(cells), 1)
    return result


_NUMERIC_TOKEN_RE = re.compile(
    r"(?<![\^A-Za-z0-9])(?:\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)"
    r"(?:%|％|亿美元|亿元|万元|美元|人民币|元|亿|万|人|家|倍|年|月|日)?"
    r"(?![A-Za-z0-9])"
)
_CITATION_RE = re.compile(r"\[\^(\d{1,3})\]")
_GROUPED_CITATION_RE = re.compile(
    r"\[\^(\d{1,3}(?:\s*[,，、]\s*\d{1,3})+)\]"
)


def _norm_number(value: str) -> str:
    return re.sub(r"\s+", "", value.replace(",", "").replace("％", "%")).lower()


def _numeric_token_supported(token: str, cited_text: str, context: str = "") -> bool:
    cited_norm = _norm_number(cited_text)
    if token in cited_norm:
        return True
    date_match = re.fullmatch(r"(20\d{2})\.(\d{1,2})", token)
    if date_match:
        year, month = date_match.groups()
        if any(
            variant in cited_text
            for variant in (
                f"{year}-{int(month):02d}",
                f"{year}/{int(month):02d}",
                f"{year}年{int(month)}月",
            )
        ):
            return True
    match = re.search(r"\d+(?:\.\d+)?", token)
    if not match:
        return False
    value = float(match.group())
    cited_values = [
        float(raw.replace(",", ""))
        for raw in re.findall(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?", cited_text)
    ]
    if token.endswith("%"):
        return any(abs(candidate - value) <= 0.5 for candidate in cited_values)
    if ("亿元" in token or "亿元" in context) and "百万元" in cited_text:
        return any(abs(candidate - value * 100) <= max(0.5, value) for candidate in cited_values)
    return any(abs(candidate - value) <= 0.05 for candidate in cited_values)


def propagate_derived_table_citations(report: str) -> str:
    """Attach input citations to uncited YoY/growth rows in markdown tables."""
    lines = (report or "").splitlines()
    for pos, line in enumerate(lines):
        if not line.strip().startswith("|") or _CITATION_RE.search(line):
            continue
        cells = line.split("|")
        label = cells[1].strip() if len(cells) > 2 else ""
        if not re.search(r"YoY|同比|增速", label, re.IGNORECASE):
            continue
        if not _NUMERIC_TOKEN_RE.search(line):
            continue
        previous_ids: list[str] = []
        for previous in reversed(lines[max(0, pos - 2):pos]):
            previous_ids.extend(_CITATION_RE.findall(previous))
        previous_ids = list(dict.fromkeys(previous_ids))
        if not previous_ids:
            continue
        citations = "".join(f"[^{idx}]" for idx in previous_ids)
        cells[1] = f" {label}（按相邻输入计算）{citations} "
        lines[pos] = "|".join(cells)
    for pos, line in enumerate(lines):
        if (
            _CITATION_RE.search(line)
            or not _NUMERIC_TOKEN_RE.search(line)
            or not re.search(r"测算|合计|加总|计算", line)
        ):
            continue
        previous_ids: list[str] = []
        for previous in reversed(lines[max(0, pos - 12):pos]):
            if previous.strip().startswith("#"):
                break
            previous_ids.extend(_CITATION_RE.findall(previous))
        previous_ids = list(dict.fromkeys(previous_ids))
        if previous_ids:
            lines[pos] = line.rstrip() + "".join(
                f"[^{idx}]" for idx in previous_ids[:8]
            )
    return "\n".join(lines)


def normalize_grouped_citations(report: str) -> str:
    """Convert ``[^1,2,3]`` into valid, independently traceable footnotes."""
    return _GROUPED_CITATION_RE.sub(
        lambda match: "".join(
            f"[^{value}]"
            for value in re.findall(r"\d{1,3}", match.group(1))
        ),
        report or "",
    )


def normalize_plain_numeric_citations(
    report: str,
    valid_ids: set[int],
) -> str:
    """Convert plain ``[12]`` evidence markers when 12 is a valid pool id."""
    return re.sub(
        r"(?<!\^)\[(\d{1,3})\]",
        lambda match: (
            f"[^{match.group(1)}]"
            if int(match.group(1)) in valid_ids else match.group(0)
        ),
        report or "",
    )


def neutralize_uncited_tracking_thresholds(report: str) -> str:
    """Remove invented precision from the observation-signal table column."""
    lines = (report or "").splitlines()
    in_outlook = False
    signal_column: int | None = None
    for pos, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("## "):
            in_outlook = "展望与关注点" in stripped
            signal_column = None
            continue
        if not in_outlook or not stripped.startswith("|"):
            continue
        cells = line.split("|")
        if signal_column is None and any("观察信号" in cell for cell in cells):
            signal_column = next(
                idx for idx, cell in enumerate(cells) if "观察信号" in cell
            )
            continue
        if signal_column is None or signal_column >= len(cells):
            continue
        signal = cells[signal_column]
        if (
            _NUMERIC_TOKEN_RE.search(signal)
            and not _CITATION_RE.search(signal)
            and not re.fullmatch(r"\s*:?-{3,}:?\s*", signal)
        ):
            cells[signal_column] = (
                " 相对当前披露值出现持续且方向明确的变化"
                "（现有证据不足以设定精确阈值） "
            )
            lines[pos] = "|".join(cells)
    return "\n".join(lines)


def ensure_dimension_coverage_boundaries(
    report: str,
    sections: list[str],
) -> tuple[str, list[str]]:
    """Add boundary-only rows for dimensions omitted from the mandated table."""
    if not report or not sections:
        return report, []
    lines = report.splitlines()
    heading_idx = next(
        (idx for idx, line in enumerate(lines) if line.strip() == "### 分析维度覆盖"),
        -1,
    )
    if heading_idx < 0:
        return report, []
    table_start = next(
        (
            idx for idx in range(heading_idx + 1, len(lines))
            if lines[idx].strip().startswith("|")
        ),
        -1,
    )
    if table_start < 0:
        return report, []
    table_end = table_start
    while table_end < len(lines) and lines[table_end].strip().startswith("|"):
        table_end += 1
    def dimension_key(value: str) -> str:
        value = re.sub(r"[*_`]+", "", value or "")
        value = re.split(r"[:：]", value, maxsplit=1)[0]
        return re.sub(r"\s+", "", value)

    # Keep one row per semantic dimension. Prefer a substantive row over a
    # boundary-only row left by an earlier processing version.
    table_lines = lines[table_start:table_end]
    kept: list[str] = []
    key_pos: dict[str, int] = {}
    for row in table_lines:
        cells = [cell.strip() for cell in row.strip().strip("|").split("|")]
        if (
            len(cells) < 2
            or cells[0] in {"分析维度", "维度"}
            or re.fullmatch(r":?-{3,}:?", cells[0] or "")
        ):
            kept.append(row)
            continue
        key = dimension_key(cells[0])
        if not key or key not in key_pos:
            key_pos[key] = len(kept)
            kept.append(row)
            continue
        old_pos = key_pos[key]
        if "证据边界" in kept[old_pos] and "证据边界" not in row:
            kept[old_pos] = row
    lines[table_start:table_end] = kept
    table_end = table_start + len(kept)
    table_text = re.sub(r"\s+", "", "\n".join(kept))

    missing = [
        section for section in sections
        if not (
            re.sub(r"\s+", "", section) in table_text
            or (
                dimension_key(section)
                and dimension_key(section) in table_text
            )
        )
    ]
    if not missing:
        return "\n".join(lines), []
    rows = [
        f"| {section} | **证据边界**：现有证据与通过证伪的洞察不足，不作方向性强结论。 | 无可靠证据 |"
        for section in missing
    ]
    lines[table_end:table_end] = rows
    return "\n".join(lines), missing


def _chinese_bigrams(text: str) -> set[str]:
    normalized = (text or "").replace("营业收入", "营收").replace("营业额", "营收")
    compact = re.sub(r"[^\u4e00-\u9fff]", "", normalized)
    return {compact[idx:idx + 2] for idx in range(max(0, len(compact) - 1))}


def _evidence_search_text(evidence: Evidence) -> str:
    return " ".join(
        value for value in (
            evidence.source_title,
            evidence.content,
            evidence.as_of,
            evidence.published_at,
        )
        if value
    )


def attach_exact_numeric_citations(
    report: str,
    evidence_pool: dict[int, Evidence],
    max_citations: int = 4,
) -> str:
    """Cite uncited numeric prose only when evidence covers every numeric token."""
    lines = (report or "").splitlines()
    for pos, raw_line in enumerate(lines):
        line = raw_line.strip()
        if (
            not line
            or line.startswith("#")
            or any(marker in line for marker in ("未披露", "数据缺失", "证据池中缺失"))
        ):
            continue
        tokens = [_norm_number(value) for value in _NUMERIC_TOKEN_RE.findall(line)]
        tokens = [token for token in tokens if token not in {"1", "2", "3", "4", "5"}]
        if not tokens:
            continue
        existing_ids = [
            int(value) for value in _CITATION_RE.findall(line)
            if int(value) in evidence_pool
        ]
        uncovered = {
            token_idx for token_idx, token in enumerate(tokens)
            if not any(
                _numeric_token_supported(
                    token, _evidence_search_text(evidence_pool[idx]), line
                )
                for idx in existing_ids
            )
        }
        if not uncovered:
            continue
        line_bigrams = _chinese_bigrams(line)
        candidates: list[tuple[int, set[int], int]] = []
        for idx, evidence in evidence_pool.items():
            if idx in existing_ids:
                continue
            content = _evidence_search_text(evidence)
            overlap = len(line_bigrams & _chinese_bigrams(content))
            if overlap < (1 if line.startswith("|") else 2):
                continue
            covered = {
                token_idx for token_idx, token in enumerate(tokens)
                if _numeric_token_supported(token, content, line)
            }
            if covered:
                candidates.append((idx, covered, int(evidence.tier)))
        selected: list[int] = []
        while uncovered and len(selected) < max_citations:
            choices = [
                item for item in candidates
                if item[0] not in selected and item[1] & uncovered
            ]
            if not choices:
                break
            best_idx, best_covered, _ = max(
                choices,
                key=lambda item: (len(item[1] & uncovered), -item[2]),
            )
            selected.append(best_idx)
            uncovered -= best_covered
        if uncovered or not selected:
            continue
        citations = "".join(f"[^{idx}]" for idx in selected)
        if raw_line.rstrip().endswith("|"):
            lines[pos] = raw_line.rstrip()[:-1].rstrip() + f" {citations} |"
        else:
            lines[pos] = raw_line.rstrip() + citations
    return "\n".join(lines)


def neutralize_unsupported_coverage_rows(
    report: str,
    grounding_audit: dict,
) -> str:
    """Downgrade unsupported precision in the duplicate dimension-coverage table."""
    mismatches = set(grounding_audit.get("numeric_citation_mismatches") or [])
    if not mismatches:
        return report
    lines = (report or "").splitlines()
    in_coverage = False
    for pos, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("### "):
            in_coverage = stripped == "### 分析维度覆盖"
            continue
        if in_coverage and stripped.startswith("## "):
            in_coverage = False
        if not in_coverage or stripped not in mismatches or not stripped.startswith("|"):
            continue
        cells = line.split("|")
        if len(cells) < 4:
            continue
        cells[2] = " **证据边界**：现有证据不足以支持该精确量化口径，仅保留定性方向。 "
        cells[3] = " 无可核验量化口径 "
        lines[pos] = "|".join(cells[:4]) + "|"
    return "\n".join(lines)


def audit_report_grounding(report: str, evidence_pool: dict[int, Evidence]) -> dict:
    """Run a conservative, zero-LLM grounding audit over the final report.

    This is observability, not an automatic factual verdict.  It catches the
    high-value mechanical failures: missing/invalid citations and numeric
    statements whose cited snippets do not contain the stated value or inputs.
    """
    checked = 0
    grounded = 0
    uncited: list[str] = []
    numeric_mismatch: list[str] = []
    invalid_citations: set[int] = set()

    for raw_line in (report or "").splitlines():
        line = raw_line.strip()
        if (
            not line
            or line.startswith("#")
            or line.startswith("- **[^")
            or re.fullmatch(r"[\s|:\-]+", line)
            or line.startswith("数据截至 ")
            or (
                line.startswith("|")
                and any(label in line for label in ("| 指标", "| 字段", "| 维度", "| 项目"))
            )
        ):
            continue
        citations = [int(value) for value in _CITATION_RE.findall(line)]
        for idx in citations:
            if idx not in evidence_pool:
                invalid_citations.add(idx)
        without_citations = _CITATION_RE.sub("", line)
        without_confidence = re.sub(r"置信度\s*\d+(?:\.\d+)?%", "", without_citations)
        tokens = [_norm_number(v) for v in _NUMERIC_TOKEN_RE.findall(without_confidence)]
        tokens = [v for v in tokens if v not in {"1", "2", "3", "4", "5"}]
        if not tokens:
            continue
        checked += 1
        valid_ids = [idx for idx in citations if idx in evidence_pool]
        if not valid_ids:
            if not citations:
                uncited.append(line[:500])
            continue

        cited_text = " ".join(
            _evidence_search_text(evidence_pool[idx]) for idx in valid_ids
        )
        matched = [
            token for token in tokens
            if _numeric_token_supported(token, cited_text, without_citations)
        ]
        is_explicit_derivation = any(marker in line for marker in ("按", "测算", "推演", "计算", "合计"))
        if matched or is_explicit_derivation:
            grounded += 1
        else:
            numeric_mismatch.append(line[:500])

    rate = round(grounded / checked, 3) if checked else 1.0
    return {
        "numeric_lines_checked": checked,
        "numeric_lines_grounded": grounded,
        "grounding_rate": rate,
        "uncited_numeric_lines": uncited[:10],
        "numeric_citation_mismatches": numeric_mismatch[:10],
        "invalid_citation_ids": sorted(invalid_citations),
        "passed": not invalid_citations and not uncited and rate >= 0.85,
    }
