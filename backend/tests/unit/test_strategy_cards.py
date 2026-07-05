from __future__ import annotations

import memory_store


def _tmp_strategy_store(monkeypatch, tmp_path):
    path = tmp_path / "strategy_cards.json"
    monkeypatch.setattr(memory_store, "_STRATEGY_CARDS", path)
    memory_store._cache.clear()
    return path


def test_strategy_gate_rejects_non_actionable_card(tmp_path, monkeypatch):
    _tmp_strategy_store(monkeypatch, tmp_path)

    ok, issues = memory_store.strategy_gate({
        "stage": "collect",
        "trigger": "港股互联网",
        "problem": "too vague",
        "action": {},
        "success_metric": "better",
        "confidence": 0.8,
    })

    assert not ok
    assert "missing_action" in issues


def test_generate_and_promote_collect_strategy(tmp_path, monkeypatch):
    _tmp_strategy_store(monkeypatch, tmp_path)
    summary = {
        "run_id": "r1",
        "query": "腾讯控股",
        "template_key": "internet_saas",
        "industry": "互联网综合服务",
        "collect_quality": {
            "score": 42,
            "issues": ["缺少财报、公告或监管等高权威信源"],
        },
        "quality_eval": {},
        "insights": [],
    }

    candidates = memory_store.generate_strategy_candidates(summary)
    result = memory_store.promote_strategy_candidates(candidates, source_run_id="r1")

    assert len(result["activated"]) == 1
    card = result["activated"][0]
    assert card["stage"] == "collect"
    assert card["status"] == "active"
    assert card["source_run_id"] == "r1"


def test_active_strategy_cards_match_context(tmp_path, monkeypatch):
    _tmp_strategy_store(monkeypatch, tmp_path)
    memory_store.promote_strategy_candidates([
        {
            "stage": "collect",
            "trigger": "internet_saas 互联网综合服务 腾讯控股",
            "problem": "High authority source gap",
            "action": {"query_templates": ["{name} {year} 财报 公告"]},
            "success_metric": "Collect one tier<=3 evidence",
            "confidence": 0.8,
        }
    ])

    cards = memory_store.load_active_strategy_cards({
        "query": "腾讯控股",
        "name": "腾讯控股",
        "industry": "互联网综合服务",
        "template_key": "internet_saas",
        "sections": ["游戏", "广告"],
    }, stage="collect")

    assert len(cards) == 1
    assert cards[0]["action"]["query_templates"][0]
