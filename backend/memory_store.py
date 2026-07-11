"""
三类持久化存储 + Reflection 学习环。

持久化存储：
- Experience:   每次任务写（run 摘要 + eval 反馈），Reflection 时读
- Domain:       行业知识框架，Reflection 蒸馏后写，Scope 阶段读
- Behavior:     采集/证伪/报告策略，统一 trigger→action→success_rate

Reflection 是瞬态学习过程，不独立持久化——run 结束后蒸馏直接写入 Domain + Behavior。

存储：JSON 文件，无 DB 依赖，可直接 inspect。
线程安全：所有写操作在 _lock 内完成完整的读-改-写事务，防并发丢更新。

⚠ 部署边界：threading.RLock 仅保证单进程内线程安全。多 worker / 多进程部署
（如 uvicorn --workers N）共写同一文件仍可能丢更新。进入多进程部署前应迁移到
SQLite 或增加跨进程文件锁（fcntl/msvcrt）。当前单进程本地部署无此问题。
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

_MEM_DIR = Path(__file__).resolve().parent.parent / "memory"

# ---- 持久化文件路径 ----
_EXPERIENCE = _MEM_DIR / "experience.json"
_DOMAIN = _MEM_DIR / "domain.json"
_BEHAVIOR = _MEM_DIR / "behavior.json"

_ALLOWED_STRATEGY_STAGES = {"scope", "collect", "analyze", "falsify", "report"}

_lock = threading.RLock()
_cache: dict[str, Any] = {}

# ---- 默认初始结构 ----
_DEFAULTS: dict[Path, dict] = {
    _EXPERIENCE: {"runs": [], "eval_feedbacks": []},
    _DOMAIN: {"generic": {}},
    _BEHAVIOR: {"policies": [], "strategies": {"active": [], "pending": [], "rejected": []}},
}


def ensure_initialized():
    """确保存储目录和文件存在。首次启动时自动调用。

    如果 domain.json 不存在但 domain.json.example 存在，从模板复制。
    其余文件不存在时用空初始结构创建。
    """
    _MEM_DIR.mkdir(parents=True, exist_ok=True)
    # domain.json: 优先从 example 复制
    if not _DOMAIN.exists():
        example = _MEM_DIR / "domain.json.example"
        if example.exists():
            import shutil
            shutil.copy2(example, _DOMAIN)
        else:
            _DOMAIN.write_text(
                json.dumps(_DEFAULTS[_DOMAIN], ensure_ascii=False, indent=2),
                encoding="utf-8")
    for path in (_EXPERIENCE, _BEHAVIOR):
        if not path.exists():
            path.write_text(
                json.dumps(_DEFAULTS[path], ensure_ascii=False, indent=2),
                encoding="utf-8")


# 模块加载时自动初始化
ensure_initialized()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _load_json(path: Path) -> dict | list:
    """线程安全加载 JSON（带缓存）。文件不存在返回空 dict。只用于只读场景。"""
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


def _load_fresh(path: Path) -> dict | list:
    """锁内读取：绕过缓存从磁盘重读。调用方必须已持有 _lock。"""
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _atomic_write_locked(path: Path, data):
    """锁内原子写入。调用方必须已持有 _lock。写后刷新缓存。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    _cache[str(path)] = data


def _transact(path: Path, mutator: Callable[[dict | list], None]):
    """对 path 执行完整的读-改-写事务，全程持锁。

    mutator 接收从磁盘新读取的 data 对象，就地修改它。
    事务完成后自动原子写回并刷新缓存。
    """
    with _lock:
        data = _load_fresh(path)
        if not isinstance(data, dict):
            data = {}
        mutator(data)
        _atomic_write_locked(path, data)


def _invalidate(path: Path):
    """清缓存。"""
    with _lock:
        _cache.pop(str(path), None)


# 兼容旧调用：只读场景仍可用
def _atomic_write(path: Path, data):
    """原子写入 JSON（兼容桩，写操作优先用 _transact）。"""
    with _lock:
        _atomic_write_locked(path, data)


# ================================================================
#  1. Experience Memory — 历史任务 + 评测反馈
# ================================================================
# 合并原 episodic/runs/*.json + eval_feedback.json
# 结构: {"runs": [...], "eval_feedbacks": [...]}

def save_episodic(run_summary: dict):
    """追加一次任务的执行摘要。

    Write: 每次任务完成后（Report 阶段）
    Read:  Reflection 需要历史对比时
    """
    def _mutate(data):
        runs = data.setdefault("runs", [])
        run_summary["timestamp"] = _now()
        runs.append(run_summary)
        if len(runs) > 100:
            data["runs"] = runs[-100:]
    _transact(_EXPERIENCE, _mutate)


def load_recent_episodes(n: int = 5) -> list[dict]:
    """加载最近 N 次任务日志（供 Reflection 参考）。"""
    data = _load_json(_EXPERIENCE)
    if not isinstance(data, dict):
        return []
    runs = data.get("runs", [])
    if not isinstance(runs, list):
        return []
    return runs[-n:] if len(runs) >= n else list(runs)


def _normalize_query_key(query: str) -> str:
    """规范化 query 用于模糊匹配：去标点、去空格。"""
    q = re.sub(r"[\s\u3000]+", "", (query or "").lower())
    q = re.sub(r"[（()）,，。.!！?？·、/\\\"'`]+", "", q)
    return q.strip()[:80]


def save_eval_feedback(query: str, feedback: dict) -> None:
    """存储一次评测发现，供下次同主题 pipeline 运行时读取。"""
    try:
        def _mutate(data):
            feedbacks = data.setdefault("eval_feedbacks", [])
            entry = {
                "query": (query or "").strip()[:120],
                "query_key": _normalize_query_key(query),
                "timestamp": _now(),
            }
            entry.update(feedback)
            feedbacks.append(entry)
            if len(feedbacks) > 50:
                data["eval_feedbacks"] = feedbacks[-50:]
        _transact(_EXPERIENCE, _mutate)
    except Exception:  # noqa: BLE001
        pass


def load_eval_feedback(query: str, limit: int = 3) -> list[dict]:
    """加载与 query 相关的历史评测发现。"""
    try:
        data = _load_json(_EXPERIENCE)
        if not isinstance(data, dict):
            return []
        feedbacks = data.get("eval_feedbacks", [])
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
        matched.sort(key=lambda f: f.get("timestamp", ""), reverse=True)
        return list(reversed(matched[:limit]))
    except Exception:  # noqa: BLE001
        return []


def generalize_eval_weaknesses(past_evals: list[dict]) -> list[str]:
    """从历史评测中泛化提取 Agent 常见不足模式。"""
    if not past_evals:
        return []
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

    sorted_weaknesses = sorted(weakness_counts.items(), key=lambda x: x[1], reverse=True)
    return [w for w, _ in sorted_weaknesses]


# ================================================================
#  2. Domain Memory — 行业分析框架
# ================================================================
# 合并原 industry_rag/playbooks.json（去掉空的 by_industry/ 子目录）
# 结构: {"generic": {...}, "by_template_key": {...}}

def load_industry_rag(template_key: str = "") -> dict:
    """加载行业知识库。优先按 template_key 匹配，否则用 generic。

    Read: 任务前（Scope 阶段）
    """
    rag = _load_json(_DOMAIN)
    result = dict(rag.get("generic", {}))
    if template_key:
        by_key = rag.get("by_template_key", {})
        specific = by_key.get(template_key, {})
        for k, v in specific.items():
            if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                merged = dict(result[k])
                merged.update(v)
                result[k] = merged
            else:
                result[k] = v
    return result


def update_industry_rag(template_key: str, playbook_type: str, items: list[dict]):
    """向 Domain Memory 追加新行业模式（由 Reflection 调用）。"""
    if not items:
        return
    def _mutate(rag):
        by_tk = rag.setdefault("by_template_key", {})
        tpl = by_tk.setdefault(template_key or "generic", {})
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
        elif playbook_type == "industry_playbook":
            ip = tpl.setdefault("industry_playbook", {})
            if isinstance(ip, dict):
                pitfalls = ip.setdefault("common_pitfalls", [])
                if isinstance(pitfalls, list):
                    for item in items:
                        text = str(item.get("pattern", item.get("trap", "")))
                        if text and text not in pitfalls:
                            pitfalls.append(text)
    _transact(_DOMAIN, _mutate)


# ================================================================
#  3. Behavior Memory — 统一策略库
# ================================================================
# 合并原 challenge_policies.json + strategy_cards.json
# 结构: {"policies": [...], "strategies": {"active": [], "pending": [], "rejected": []}}
# policies: 证伪阶段挑战策略（trigger→search_strategy→success_rate）
# strategies: 各阶段行为策略卡（trigger→action→effect_score）

# ---- 3a. Challenge Policies ----

def load_challenge_policies() -> list[dict]:
    """加载全部挑战策略。Read: 每次挑战前（Falsify 阶段）。"""
    store = _load_json(_BEHAVIOR)
    if not isinstance(store, dict):
        return []
    policies = store.get("policies", [])
    return policies if isinstance(policies, list) else []


def match_policies(claim: str, reasoning: str, policies: list[dict] | None = None) -> list[dict]:
    """根据论点内容匹配相关挑战策略。"""
    if policies is None:
        policies = load_challenge_policies()
    text = (claim + " " + reasoning).lower()
    matched = []
    for p in policies:
        trigger = str(p.get("trigger", "")).lower()
        keywords = [w.strip() for w in trigger.replace(",", " ").replace("，", " ").split() if len(w.strip()) > 2]
        if any(kw in text for kw in keywords):
            matched.append(p)
    pri_order = {"high": 0, "medium": 1, "low": 2}
    matched.sort(key=lambda p: pri_order.get(p.get("priority", "medium"), 1))
    return matched


def update_policy_success(policy_id: str, succeeded: bool):
    """更新策略的历史成功率（每次挑战后调用）。"""
    def _mutate(store):
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
    _transact(_BEHAVIOR, _mutate)


def apply_policy_updates(updates: list[dict]):
    """批量应用策略更新（由 Reflection 调用）。"""
    if not updates:
        return
    def _mutate(store):
        policies = store.setdefault("policies", [])
        for u in updates:
            if not isinstance(u, dict):
                continue
            action = u.get("action", "")
            if action == "add" and isinstance(u.get("policy"), dict):
                new_pol = u["policy"]
                trigger = (new_pol.get("trigger") or "").strip()
                ch_type = (new_pol.get("challenge_type") or "").strip()
                dup_idx = -1
                for i, p in enumerate(policies):
                    if ((p.get("trigger") or "").strip() == trigger
                            and (p.get("challenge_type") or "").strip() == ch_type):
                        dup_idx = i
                        break
                if dup_idx >= 0:
                    existing = policies[dup_idx]
                    existing["times_applied"] = existing.get("times_applied", 0) + new_pol.get("times_applied", 0)
                    existing["times_succeeded"] = existing.get("times_succeeded", 0) + new_pol.get("times_succeeded", 0)
                    existing["last_updated"] = _now()
                    for k, v in new_pol.items():
                        if k not in existing or not existing[k]:
                            existing[k] = v
                else:
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
    _transact(_BEHAVIOR, _mutate)


# ---- 3b. Strategy Cards (统一到 Behavior 文件) ----

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
    """Deterministic promotion gate for strategy cards."""
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
    data = _load_json(_BEHAVIOR)
    if not isinstance(data, dict):
        data = {}
    strategies = data.get("strategies", {})
    if not isinstance(strategies, dict):
        strategies = {}
    strategies.setdefault("active", [])
    strategies.setdefault("pending", [])
    strategies.setdefault("rejected", [])
    return strategies


def promote_strategy_candidates(candidates: list[dict], *, source_run_id: str = "") -> dict:
    """Promote deterministic strategy candidates into the active store."""
    result = {"activated": [], "pending": [], "rejected": []}
    if not candidates:
        return result

    def _mutate(store_data):
        strategies = store_data.setdefault("strategies", {})
        if not isinstance(strategies, dict):
            strategies = {}
            store_data["strategies"] = strategies
        active = strategies.setdefault("active", [])
        pending = strategies.setdefault("pending", [])
        rejected = strategies.setdefault("rejected", [])
        existing_ids = {str(c.get("id")) for c in active + pending}

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

    _transact(_BEHAVIOR, _mutate)
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
    strategies = load_strategy_store()
    cards = [
        normalize_strategy_card(c)
        for c in strategies.get("active", [])
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
    def _mutate(store_data):
        strategies = store_data.get("strategies", {})
        if not isinstance(strategies, dict):
            return
        active = strategies.get("active", [])
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
    _transact(_BEHAVIOR, _mutate)


# ================================================================
#  4. Reflection — 瞬态，不持久化
# ================================================================
# Reflection 现在是 orchestrator 管道内的一个步骤。
# 原 save_reflection / load_reflections 保留为空操作兼容桩，
# 供 orchestrator 调用时不报错。反思结果直接写入 Domain + Behavior。

def load_reflections(n: int = 10) -> list[dict]:
    """兼容桩：返回已沉淀的 Domain + Behavior 知识摘要，供 Reflection 避免重复。"""
    return load_existing_knowledge_summary()


def load_existing_knowledge_summary() -> list[dict]:
    """加载已沉淀的 Domain 和 Behavior 知识摘要，供 Reflection 了解已有策略。

    返回一个精简的列表，让 LLM 知道哪些模式/策略已经被捕获了，
    避免重复生成相同的策略更新。
    """
    summary: list[dict] = []

    # Domain: 已有的 failure_playbook 模式
    try:
        domain = _load_json(_DOMAIN)
        if isinstance(domain, dict):
            generic = domain.get("generic", {})
            if isinstance(generic, dict):
                fp = generic.get("failure_playbook", [])
                if isinstance(fp, list):
                    for item in fp[-10:]:
                        if isinstance(item, dict):
                            summary.append({
                                "type": "domain_pattern",
                                "pattern": item.get("pattern", "")[:60],
                                "trap": item.get("trap", "")[:60],
                            })
            by_tk = domain.get("by_template_key", {})
            if isinstance(by_tk, dict):
                for tk, tpl_data in by_tk.items():
                    if isinstance(tpl_data, dict):
                        fp2 = tpl_data.get("failure_playbook", [])
                        if isinstance(fp2, list):
                            for item in fp2[-5:]:
                                if isinstance(item, dict):
                                    summary.append({
                                        "type": "domain_pattern",
                                        "template_key": tk,
                                        "pattern": item.get("pattern", "")[:60],
                                    })
    except Exception:  # noqa: BLE001
        pass

    # Behavior: 已有的 challenge policies（高成功率优先）
    try:
        behavior = _load_json(_BEHAVIOR)
        if isinstance(behavior, dict):
            policies = behavior.get("policies", [])
            if isinstance(policies, list):
                sorted_pols = sorted(
                    (p for p in policies if isinstance(p, dict)),
                    key=lambda p: p.get("historical_success_rate", 0),
                    reverse=True,
                )
                for p in sorted_pols[:10]:
                    summary.append({
                        "type": "behavior_policy",
                        "trigger": p.get("trigger", "")[:60],
                        "challenge_type": p.get("challenge_type", ""),
                        "success_rate": p.get("historical_success_rate", 0),
                    })
    except Exception:  # noqa: BLE001
        pass

    return summary


def save_reflection(reflection: dict) -> bool:
    """兼容桩：反思结果不再独立持久化。

    policy_updates 和 industry_pattern_updates 由 orchestrator 直接调用
    apply_policy_updates() 和 update_industry_rag() 写入 Behavior / Domain。
    返回 True 表示"处理完毕"。
    """
    return True


# ================================================================
#  5. Shared Helpers
# ================================================================

def build_run_summary(run) -> dict:
    """从 AnalysisRun 对象构建 experience memory 摘要。"""
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
