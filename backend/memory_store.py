"""
四层长期记忆系统 —— Industry RAG / Challenge Policy Store / Episodic Memory / Meta Reflection.

设计原则：
- Industry RAG:       任务前读，模式积累足够才写
- Challenge Policies: 每次挑战前读，由 Meta Reflection 更新
- Episodic Memory:    每次任务写，很少读
- Meta Reflection:    每次任务后读（历史反思）+ 写（新反思）

存储：JSON 文件，无 DB 依赖，可直接 inspect。
线程安全：读取用缓存 + 锁，写入用原子替换。
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

_MEM_DIR = Path(__file__).resolve().parent.parent / "memory"
_INDUSTRY_RAG = _MEM_DIR / "industry_rag" / "playbooks.json"
_CHALLENGE_POLICIES = _MEM_DIR / "challenge_policies.json"
_STRATEGY_CARDS = _MEM_DIR / "strategy_cards.json"
_EPISODIC_DIR = _MEM_DIR / "episodic" / "runs"
_META_REFLECTION = _MEM_DIR / "meta_reflection" / "reflections.json"
_EVAL_FEEDBACK = _MEM_DIR / "eval_feedback.json"

_ALLOWED_STRATEGY_STAGES = {"scope", "collect", "analyze", "falsify", "report"}

_lock = threading.RLock()
_cache: dict[str, Any] = {}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _load_json(path: Path) -> dict | list:
    """线程安全加载 JSON（带缓存）。文件不存在返回空 dict。"""
    key = str(path)
    if key in _cache:
        return _cache[key]
    with _lock:
        if key in _cache:
            return _cache[key]
        if not path.exists():
            data: dict | list = {}
        else:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
        _cache[key] = data
        return data


def _invalidate(path: Path):
    """写后清缓存，下次读重新加载。"""
    key = str(path)
    with _lock:
        _cache.pop(key, None)


def _atomic_write(path: Path, data):
    """原子写入 JSON（先写临时文件再 rename，防写一半崩溃）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    _invalidate(path)


# ============ 1. Industry RAG ============

def load_industry_rag(template_key: str = "") -> dict:
    """加载行业知识库（playbooks）。优先按 template_key 匹配，否则用 generic。

    Read: 任务前（Scope 阶段）
    Write: 模式积累足够后（Meta Reflection 触发）
    """
    rag = _load_json(_INDUSTRY_RAG)
    result = dict(rag.get("generic", {}))
    if template_key:
        by_key = rag.get("by_template_key", {})
        specific = by_key.get(template_key, {})
        # 深合并：specific 覆盖 generic 同名键
        for k, v in specific.items():
            if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                merged = dict(result[k])
                merged.update(v)
                result[k] = merged
            else:
                result[k] = v
    return result


def update_industry_rag(template_key: str, playbook_type: str, items: list[dict]):
    """向 Industry RAG 追加新模式（由 Meta Reflection 调用）。

    template_key: 模板 key（如 fintech_lending / internet_saas / generic）
    playbook_type: 追加目标（如 failure_playbook / industry_playbook.common_pitfalls）
    items: 要追加的模式列表，每个是 dict（如 {pattern, trap, fix}）

    自动去重：已有相同 pattern 的条目不重复追加。
    """
    if not items:
        return
    rag = _load_json(_INDUSTRY_RAG)
    # 确保路径存在
    by_tk = rag.setdefault("by_template_key", {})
    tpl = by_tk.setdefault(template_key or "generic", {})
    # failure_playbook 是列表，直接追加
    if playbook_type in ("failure_playbook", "source_playbook"):
        target = tpl.setdefault(playbook_type, [])
        if not isinstance(target, list):
            target = []
            tpl[playbook_type] = target
        existing_patterns = {str(item.get("pattern", "")) for item in target if isinstance(item, dict)}
        for item in items:
            if isinstance(item, dict) and str(item.get("pattern", "")) not in existing_patterns:
                target.append(item)
                existing_patterns.add(str(item.get("pattern", "")))
    # industry_playbook 是 dict，往 common_pitfalls 列表追加
    elif playbook_type == "industry_playbook":
        ip = tpl.setdefault("industry_playbook", {})
        if isinstance(ip, dict):
            pitfalls = ip.setdefault("common_pitfalls", [])
            if isinstance(pitfalls, list):
                for item in items:
                    text = str(item.get("pattern", item.get("trap", "")))
                    if text and text not in pitfalls:
                        pitfalls.append(text)
    _atomic_write(_INDUSTRY_RAG, rag)


# ============ 2. Challenge Policy Store ============

def load_challenge_policies() -> list[dict]:
    """加载全部挑战策略。

    Read: 每次挑战前（Falsify 阶段）
    Write: 由 Meta Reflection 更新
    """
    store = _load_json(_CHALLENGE_POLICIES)
    policies = store.get("policies", [])
    return policies if isinstance(policies, list) else []


def match_policies(claim: str, reasoning: str, policies: list[dict] | None = None) -> list[dict]:
    """根据论点内容匹配相关挑战策略，返回按 priority 排序的策略列表。

    匹配逻辑：策略的 trigger 关键词出现在 claim/reasoning 中则匹配。
    返回的每条策略附带 search_strategy 和 evidence_preference，供红队参考。
    """
    if policies is None:
        policies = load_challenge_policies()
    text = (claim + " " + reasoning).lower()
    matched = []
    for p in policies:
        trigger = str(p.get("trigger", "")).lower()
        # 简单关键词匹配：trigger 中的关键名词出现在 claim 中
        keywords = [w.strip() for w in trigger.replace(",", " ").replace("，", " ").split() if len(w.strip()) > 2]
        if any(kw in text for kw in keywords):
            matched.append(p)
    # 按 priority 排序
    pri_order = {"high": 0, "medium": 1, "low": 2}
    matched.sort(key=lambda p: pri_order.get(p.get("priority", "medium"), 1))
    return matched


def update_policy_success(policy_id: str, succeeded: bool):
    """更新策略的历史成功率（每次挑战后调用）。

    succeeded=True: 该策略的挑战发现了实际问题（severity high/medium）
    succeeded=False: 该策略的挑战未发现问题
    """
    store = _load_json(_CHALLENGE_POLICIES)
    policies = store.get("policies", [])
    for p in policies:
        if p.get("id") == policy_id:
            p["times_applied"] = int(p.get("times_applied", 0)) + 1
            if succeeded:
                p["times_succeeded"] = int(p.get("times_succeeded", 0)) + 1
            applied = p["times_applied"]
            succeeded_n = p["times_succeeded"]
            p["historical_success_rate"] = round(succeeded_n / applied, 3) if applied > 0 else 0.0
            p["last_updated"] = _now()
            break
    _atomic_write(_CHALLENGE_POLICIES, store)


def apply_policy_updates(updates: list[dict]):
    """批量应用策略更新（由 Meta Reflection 调用）。

    每条 update: {action: "add"/"update", policy: {...}} 或 {action: "update", policy_id, field, value}

    **去重规则**：add 操作若已存在相同 trigger+challenge_type 的策略，则合并更新（
    times_applied/times_succeeded 累加）而非重复追加。防止 demo 模式反复产生相同策略导致文件膨胀。
    """
    if not updates:
        return
    store = _load_json(_CHALLENGE_POLICIES)
    policies = store.setdefault("policies", [])
    for u in updates:
        if not isinstance(u, dict):
            continue
        action = u.get("action", "")
        if action == "add" and isinstance(u.get("policy"), dict):
            new_pol = u["policy"]
            trigger = (new_pol.get("trigger") or "").strip()
            ch_type = (new_pol.get("challenge_type") or "").strip()
            # 去重：查找已有相同 trigger+challenge_type 的策略
            dup_idx = -1
            for i, p in enumerate(policies):
                if ((p.get("trigger") or "").strip() == trigger
                        and (p.get("challenge_type") or "").strip() == ch_type):
                    dup_idx = i
                    break
            if dup_idx >= 0:
                # 合并到已有策略：累加计数、保留最新字段
                existing = policies[dup_idx]
                existing["times_applied"] = existing.get("times_applied", 0) + new_pol.get("times_applied", 0)
                existing["times_succeeded"] = existing.get("times_succeeded", 0) + new_pol.get("times_succeeded", 0)
                existing["last_updated"] = _now()
                # 新策略有而旧的没有的字段，补上（但不覆盖已有的非空值）
                for k, v in new_pol.items():
                    if k not in existing or not existing[k]:
                        existing[k] = v
            else:
                # 全新策略：正常追加
                if not new_pol.get("id"):
                    new_pol["id"] = f"pol_{_now().replace(':', '').replace('-', '')}_{len(policies)}"
                new_pol.setdefault("times_applied", 0)
                new_pol.setdefault("times_succeeded", 0)
                new_pol.setdefault("historical_success_rate", 0.0)
                new_pol["last_updated"] = _now()
                policies.append(new_pol)
        elif action == "update" and u.get("policy_id"):
            pid = u["policy_id"]
            for p in policies:
                if p.get("id") == pid:
                    if "field" in u and "value" in u:
                        p[u["field"]] = u["value"]
                    elif isinstance(u.get("policy"), dict):
                        p.update(u["policy"])
                    p["last_updated"] = _now()
                    break
    _atomic_write(_CHALLENGE_POLICIES, store)


# ============ 2.5. Active Strategy Store ============

def _strategy_id(card: dict) -> str:
    body = json.dumps(
        {
            "stage": card.get("stage", ""),
            "trigger": card.get("trigger", ""),
            "action": card.get("action", {}),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return "sc_" + hashlib.sha1(body.encode("utf-8")).hexdigest()[:12]


def _as_text_list(value) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return []


def _strategy_tokens(text: str) -> list[str]:
    return [
        t.lower()
        for t in re.findall(r"[\u4e00-\u9fffA-Za-z0-9_]{2,}", text or "")
        if len(t.strip()) >= 2
    ]


def normalize_strategy_card(card: dict) -> dict:
    """Normalize a candidate strategy card without trusting model output."""
    if not isinstance(card, dict):
        return {}
    action = card.get("action") if isinstance(card.get("action"), dict) else {}
    normalized = {
        "id": str(card.get("id") or "").strip(),
        "status": str(card.get("status") or "candidate").strip(),
        "stage": str(card.get("stage") or "").strip().lower(),
        "trigger": str(card.get("trigger") or "").strip(),
        "problem": str(card.get("problem") or "").strip(),
        "action": {
            "query_templates": _as_text_list(action.get("query_templates")),
            "prompt_hints": _as_text_list(action.get("prompt_hints")),
            "checks": _as_text_list(action.get("checks")),
        },
        "success_metric": str(card.get("success_metric") or "").strip(),
        "confidence": float(card.get("confidence", 0.6) or 0.0),
        "source": str(card.get("source") or "").strip(),
        "created_at": card.get("created_at") or _now(),
        "last_updated": card.get("last_updated") or _now(),
        "times_applied": int(card.get("times_applied", 0) or 0),
        "times_succeeded": int(card.get("times_succeeded", 0) or 0),
        "effect_score": float(card.get("effect_score", 0.0) or 0.0),
    }
    if not normalized["id"]:
        normalized["id"] = _strategy_id(normalized)
    return normalized


def strategy_gate(card: dict) -> tuple[bool, list[str]]:
    """Deterministic promotion gate for strategy cards.

    This is intentionally stricter than meta reflection: a card must be
    actionable in a specific stage before it can affect future runs.
    """
    c = normalize_strategy_card(card)
    issues = []
    if c.get("stage") not in _ALLOWED_STRATEGY_STAGES:
        issues.append("invalid_stage")
    if len(c.get("trigger", "")) < 3:
        issues.append("missing_trigger")
    if len(c.get("problem", "")) < 6:
        issues.append("missing_problem")
    action = c.get("action", {})
    if not any(action.get(k) for k in ("query_templates", "prompt_hints", "checks")):
        issues.append("missing_action")
    if len(c.get("success_metric", "")) < 6:
        issues.append("missing_success_metric")
    if c.get("confidence", 0) < 0.55:
        issues.append("low_confidence")
    return not issues, issues


def load_strategy_store() -> dict:
    data = _load_json(_STRATEGY_CARDS)
    if not isinstance(data, dict):
        data = {}
    data.setdefault("active", [])
    data.setdefault("pending", [])
    data.setdefault("rejected", [])
    return data


def promote_strategy_candidates(candidates: list[dict], *, source_run_id: str = "") -> dict:
    """Promote deterministic strategy candidates into the active store.

    LLM reflection can suggest raw ideas elsewhere, but this function is the
    only path that makes a strategy active.
    """
    store = load_strategy_store()
    active = store.setdefault("active", [])
    pending = store.setdefault("pending", [])
    rejected = store.setdefault("rejected", [])
    existing_ids = {str(c.get("id")) for c in active + pending}
    result = {"activated": [], "pending": [], "rejected": []}

    for raw in candidates or []:
        card = normalize_strategy_card(raw)
        if source_run_id:
            card["source_run_id"] = source_run_id
        ok, issues = strategy_gate(card)
        if card["id"] in existing_ids:
            continue
        if ok:
            card["status"] = "active"
            active.append(card)
            existing_ids.add(card["id"])
            result["activated"].append(card)
        else:
            card["status"] = "rejected" if "low_confidence" in issues else "pending"
            card["gate_issues"] = issues
            target = rejected if card["status"] == "rejected" else pending
            target.append(card)
            existing_ids.add(card["id"])
            result[card["status"]].append(card)

    _atomic_write(_STRATEGY_CARDS, store)
    return result


def _matches_strategy(card: dict, context: dict) -> bool:
    haystack = " ".join(
        str(context.get(k, ""))
        for k in ("query", "name", "industry", "template_key", "kind")
    )
    sections = context.get("sections") or []
    if isinstance(sections, list):
        haystack += " " + " ".join(str(s) for s in sections)
    hay = haystack.lower()
    tokens = _strategy_tokens(str(card.get("trigger", "")))
    if not tokens:
        return False
    return any(t in hay for t in tokens)


def load_active_strategy_cards(context: dict | None = None, *, stage: str = "", limit: int = 8) -> list[dict]:
    store = load_strategy_store()
    cards = [
        normalize_strategy_card(c)
        for c in store.get("active", [])
        if isinstance(c, dict) and c.get("status", "active") == "active"
    ]
    if stage:
        cards = [c for c in cards if c.get("stage") == stage]
    if context:
        cards = [c for c in cards if _matches_strategy(c, context)]
    cards.sort(key=lambda c: (-(c.get("effect_score", 0.0)), -c.get("confidence", 0.0), c.get("created_at", "")))
    return cards[:limit]


def generate_strategy_candidates(run_summary: dict) -> list[dict]:
    """Convert concrete run failures into executable strategy candidates."""
    if not isinstance(run_summary, dict):
        return []
    query = str(run_summary.get("query") or "")
    template_key = str(run_summary.get("template_key") or "generic")
    industry = str(run_summary.get("industry") or "")
    trigger_base = " ".join(x for x in [template_key, industry, query] if x).strip() or "generic"
    cq = run_summary.get("collect_quality") or {}
    issues = [str(i) for i in (cq.get("issues") or [])]
    candidates: list[dict] = []

    def add(stage: str, problem: str, action: dict, metric: str, confidence: float = 0.65):
        candidates.append({
            "stage": stage,
            "trigger": trigger_base,
            "problem": problem,
            "action": action,
            "success_metric": metric,
            "confidence": confidence,
            "source": "deterministic_failure_classifier",
        })

    if any(("权威" in i or "高权威" in i or "authoritative" in i.lower()) for i in issues):
        add(
            "collect",
            "High-authority sources were missing in similar tasks.",
            {"query_templates": [
                "{name} {year} 财报 公告 业绩说明会",
                "{name} investor relations annual report results {year}",
            ]},
            "Next run should collect at least one tier<=3 evidence item.",
        )
    if any(("来源多样" in i or "第二类" in i or "divers" in i.lower()) for i in issues):
        add(
            "collect",
            "Evidence came from too few independent source classes.",
            {"query_templates": [
                "{name} {year} 公告 财报",
                "{name} {year} Reuters Bloomberg 财新",
            ]},
            "Next run should use at least two independent source domains.",
        )
    if any(("维度" in i or "section" in i.lower()) for i in issues):
        add(
            "collect",
            "Some analysis dimensions lacked supporting evidence.",
            {"query_templates": ["{name} {section} {year} 数据 趋势 来源"]},
            "Next run should improve section coverage above 0.7.",
        )

    quality = run_summary.get("quality_eval") or {}
    q_issues = [str(i) for i in (quality.get("issues") or [])]
    if any(("重复" in i or "结构" in i or "章节" in i) for i in q_issues):
        add(
            "report",
            "Final narrative had structural duplication or section drift.",
            {"checks": ["no_duplicate_major_sections", "single_executive_summary"],
             "prompt_hints": ["Write each major section once; merge repeated history/trend content into facts."]},
            "Next report should pass structure invariant checks.",
            confidence=0.7,
        )

    insights = run_summary.get("insights") or []
    weak_falsify = [
        i for i in insights
        if isinstance(i, dict)
        and (
            str(i.get("overall_assessment", "")).lower() == "incomplete"
            or "missing_evidence" in (i.get("challenges_high_medium") or [])
            or "alternative" in (i.get("challenges_high_medium") or [])
        )
    ]
    if len(weak_falsify) >= 2:
        add(
            "falsify",
            "Repeated incomplete verdicts indicate missing counter-evidence searches.",
            {"query_templates": [
                "{name} {claim} 反证 替代解释 数据",
                "{name} {claim} 风险 口径 监管 核实",
            ]},
            "Next run should reduce incomplete/refuted uncertainty for matched claims.",
            confidence=0.68,
        )
    return candidates


def update_strategy_effects(strategy_ids: list[str], run_metrics: dict):
    """Update coarse strategy effect counters after a run finishes."""
    if not strategy_ids:
        return
    store = load_strategy_store()
    active = store.get("active", [])
    cq = run_metrics.get("collect_quality") or {}
    qe = run_metrics.get("quality_eval") or {}
    collect_ok = int(cq.get("score", 0) or 0) >= 70
    report_ok = int(qe.get("total", qe.get("score", 0)) or 0) >= 3 if qe else True
    succeeded = collect_ok and report_ok and run_metrics.get("status") in {"done", "partial"}
    id_set = set(strategy_ids)
    for card in active:
        if card.get("id") not in id_set:
            continue
        card["times_applied"] = int(card.get("times_applied", 0) or 0) + 1
        if succeeded:
            card["times_succeeded"] = int(card.get("times_succeeded", 0) or 0) + 1
        applied = max(1, int(card.get("times_applied", 1) or 1))
        card["effect_score"] = round(int(card.get("times_succeeded", 0) or 0) / applied, 3)
        card["last_updated"] = _now()
    _atomic_write(_STRATEGY_CARDS, store)


# ============ 3. Episodic Memory ============

def save_episodic(run_summary: dict):
    """追加一次任务的执行日志到当天的 episodic 文件。

    Write: 每次任务完成后（Report 阶段）
    Read:  很少（仅 Meta Reflection 需要历史对比时）
    """
    _EPISODIC_DIR.mkdir(parents=True, exist_ok=True)
    path = _EPISODIC_DIR / f"{_today()}.json"
    data = _load_json(path)
    if not isinstance(data, dict):
        data = {}
    runs = data.setdefault("runs", [])
    run_summary["timestamp"] = _now()
    runs.append(run_summary)
    _atomic_write(path, data)


def load_recent_episodes(n: int = 5) -> list[dict]:
    """加载最近 N 次任务日志（供 Meta Reflection 参考）。

    Read: Meta Reflection 时
    """
    if not _EPISODIC_DIR.exists():
        return []
    files = sorted(_EPISODIC_DIR.glob("*.json"), reverse=True)
    episodes = []
    for f in files:
        data = _load_json(f)
        runs = data.get("runs", []) if isinstance(data, dict) else []
        episodes.extend(runs)
        if len(episodes) >= n:
            break
    # 返回最近的 N 条（列表末尾是最新写入的）
    return episodes[-n:] if len(episodes) >= n else episodes


# ============ 4. Meta Reflection ============

def load_reflections(n: int = 10) -> list[dict]:
    """加载最近 N 条反思记录。

    Read: 每次任务后（Meta Reflection 前，参考历史反思避免重复）
    """
    data = _load_json(_META_REFLECTION)
    reflections = data.get("reflections", []) if isinstance(data, dict) else []
    return reflections[-n:] if reflections else []


def save_reflection(reflection: dict):
    """追加一条反思记录。

    Write: 每次任务后（Report 阶段末尾）
    **去重规则**：若最近 10 条反思中有相同 missed_challenges 内容（同一遗漏），跳过不写。
    这防止 demo 模式或重复分析产生大量完全相同的反思记录。
    """
    data = _load_json(_META_REFLECTION)
    if not isinstance(data, dict):
        data = {}
    reflections = data.setdefault("reflections", [])
    # 轻量去重：对比 missed_challenges 列表的字符串指纹
    new_mc = reflection.get("missed_challenges")
    is_dup = False
    if isinstance(new_mc, list) and new_mc:
        new_fingerprint = str(sorted(
            (c.get("insight_claim", "")[:60] for c in new_mc if isinstance(c, dict))
        ))
        # 只检查最近 10 条，避免全量扫描
        for r in reflections[-10:]:
            existing_mc = r.get("missed_challenges")
            if isinstance(existing_mc, list):
                existing_fp = str(sorted(
                    (c.get("insight_claim", "")[:60] for c in existing_mc if isinstance(c, dict))
                ))
                if new_fingerprint == existing_fp:
                    is_dup = True
                    break
    if not is_dup:
        reflection["timestamp"] = _now()
        reflections.append(reflection)
        # 保留最近 500 条，防止无限膨胀
        if len(reflections) > 500:
            reflections[:] = reflections[-500:]
        _atomic_write(_META_REFLECTION, data)
    return not is_dup  # 返回是否实际写入了（供调用方日志用）


def build_run_summary(run) -> dict:
    """从 AnalysisRun 对象构建 episodic memory 摘要。"""
    insights_log = []
    for ins in run.insights:
        fr = ins.falsifications[-1] if ins.falsifications else None
        challenges_applied = []
        if fr:
            challenges_applied = [
                c.get("dimension", "?") for c in (getattr(fr, "challenges", []) or [])
                if isinstance(c, dict) and str(c.get("severity", "")).lower() in ("medium", "high")
            ]
        insights_log.append({
            "claim": ins.claim[:80],
            "section": ins.section,
            "verdict": ins.verdict.value if hasattr(ins.verdict, "value") else str(ins.verdict),
            "confidence_before": float(getattr(fr, "confidence_before", 0)) if fr else 0,
            "confidence_after": ins.confidence,
            "challenges_high_medium": challenges_applied,
            "overall_assessment": getattr(fr, "overall_assessment", "") if fr else "",
            "evidence_count": len(ins.evidence),
            "refinement_note": ins.refinement_note[:120] if ins.refinement_note else "",
        })
    return {
        "run_id": run.id,
        "query": run.query,
        "template_key": run.profile.template_key if run.profile else "",
        "industry": run.profile.industry if run.profile else "",
        "status": run.status,
        "insights": insights_log,
        "quality_eval": run.quality_eval,
        "collect_quality": run.collect_quality,
        "data_sources": run.data_sources_used,
        "evidence_count": len(run.evidence_pool),
        "applied_strategy_ids": getattr(run, "applied_strategy_ids", []),
        "strategy_effect": getattr(run, "strategy_effect", {}),
    }


# ============ 5. External Eval Feedback (optional cross-run loop) ============
#
# 外部评测端点 (/api/eval/live-chatbot-comparison) 的事后评测发现，
# 存下来供下一次同主题 pipeline 运行时读取注入。
# 非必选：读取失败/空时 pipeline 正常继续；存储失败不影响评测端点返回。
# 设计：按 query 规范化匹配，累积存储，保留最近 50 条防无限增长。


def _normalize_query_key(query: str) -> str:
    """规范化 query 用于模糊匹配：小写、去标点、去多余空格。"""
    q = re.sub(r"[\s\u3000]+", "", (query or "").lower())
    q = re.sub(r"[（()）,，。.!！?？·、/\\\"'`]+", "", q)
    return q.strip()[:80]


def save_eval_feedback(query: str, feedback: dict) -> None:
    """存储一次外部评测发现，供下次同主题 pipeline 运行时读取。

    Args:
        query: 分析对象（如"小红书 商业分析"）
        feedback: {run_id, fact_score, coverage_score, timeliness_score, gaps, ...}

    非必选：失败静默，不影响评测端点返回。
    """
    try:
        data = _load_json(_EVAL_FEEDBACK)
        if not isinstance(data, dict):
            data = {}
        feedbacks = data.setdefault("feedbacks", [])
        entry = {
            "query": (query or "").strip()[:120],
            "query_key": _normalize_query_key(query),
            "timestamp": _now(),
        }
        entry.update(feedback)
        feedbacks.append(entry)
        # 保留最近 50 条，防止无限增长
        if len(feedbacks) > 50:
            data["feedbacks"] = feedbacks[-50:]
        _atomic_write(_EVAL_FEEDBACK, data)
    except Exception:  # noqa: BLE001
        # 存储失败不影响评测端点返回
        pass


def load_eval_feedback(query: str, limit: int = 3) -> list[dict]:
    """加载与 query 相关的历史外部评测发现（最近 limit 条）。

    匹配规则：query_key 包含关系（任一方向子串匹配），避免完全 miss。
    非必选：无历史评测或读取失败时返回空列表。

    Args:
        query: 当前分析对象
        limit: 最多返回几条

    Returns:
        list[dict]，每条含 fact_score/coverage_score/timeliness_score/gaps 等
    """
    try:
        data = _load_json(_EVAL_FEEDBACK)
        if not isinstance(data, dict):
            return []
        feedbacks = data.get("feedbacks", [])
        if not isinstance(feedbacks, list) or not feedbacks:
            return []
        qk = _normalize_query_key(query)
        if not qk:
            return []
        matched = [
            f for f in feedbacks
            if isinstance(f, dict)
            and (qk in (f.get("query_key") or "")
                 or (f.get("query_key") or "") in qk)
        ]
        # 按 timestamp 降序取最近 limit 条，再正序返回（旧→新）
        matched.sort(key=lambda f: f.get("timestamp", ""), reverse=True)
        return list(reversed(matched[-limit:]))
    except Exception:  # noqa: BLE001
        return []


def generalize_eval_weaknesses(past_evals: list[dict]) -> list[str]:
    """从历史外部评测中泛化提取 Agent 常见不足模式。

    不是针对性复用具体 gaps（"上次缺了云业务分析"），而是泛化成通用经验
    （"Agent 容易结构松散、推算不严谨"），让 LLM 写报告时自知。

    Args:
        past_evals: load_eval_feedback 返回的历史评测列表

    Returns:
        泛化后的不足模式列表（如"结构松散，篇幅需要治理"）
    """
    if not past_evals:
        return []

    # 统计各类不足出现的次数
    weakness_counts: dict[str, int] = {}
    for pe in past_evals:
        if not isinstance(pe, dict):
            continue
        gaps = pe.get("gaps") or []
        fact = pe.get("fact_score")
        structure = pe.get("structure_score")
        coverage = pe.get("coverage_score")
        timeliness = pe.get("timeliness_score")
        gaps_text = " ".join(str(g) for g in gaps) if isinstance(gaps, list) else str(gaps)

        # 按模式匹配泛化
        if any(k in gaps_text for k in ("结构", "篇幅", "治理", "structure")):
            weakness_counts["结构松散，篇幅需要治理"] = weakness_counts.get("结构松散，篇幅需要治理", 0) + 1
        if fact is not None and fact < 50:
            weakness_counts["事实断言无法从证据验证"] = weakness_counts.get("事实断言无法从证据验证", 0) + 1
        if any(k in gaps_text for k in ("遗漏", "覆盖", "coverage", "维度")):
            weakness_counts["分析维度覆盖不全"] = weakness_counts.get("分析维度覆盖不全", 0) + 1
        if coverage is not None and coverage < 0.6:
            weakness_counts["关键问题遗漏检测分数低"] = weakness_counts.get("关键问题遗漏检测分数低", 0) + 1
        if any(k in gaps_text for k in ("数据", "推算", "估算", "口径", "验证")):
            weakness_counts["数据推算不严谨，估算值当精确值"] = weakness_counts.get("数据推算不严谨，估算值当精确值", 0) + 1
        if any(k in gaps_text for k in ("过时", "时效", "timeliness", "旧")):
            weakness_counts["引用数据过时"] = weakness_counts.get("引用数据过时", 0) + 1
        if timeliness is not None and timeliness < 0.5:
            weakness_counts["时效性评分低"] = weakness_counts.get("时效性评分低", 0) + 1

    # 按出现次数降序，只保留出现≥1次的模式
    sorted_weaknesses = sorted(weakness_counts.items(), key=lambda x: x[1], reverse=True)
    return [w for w, _ in sorted_weaknesses]
