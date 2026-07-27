from evals.independent_search_baseline import (
    _mark_disallowed_citations,
    _shift_citations,
)


def test_disallowed_independent_citations_cannot_borrow_agent_evidence():
    repaired = _mark_disallowed_citations(
        "自搜证据[^92]，未获授权证据[^4]。", {88, 89, 90, 91, 92}
    )
    assert "[^92]" in repaired
    assert "[^4]" not in repaired
    assert "[UNSOURCED]" in repaired


def test_shift_citations_moves_only_independent_namespace():
    report = "Agent[^2], independent[^5][^6], year 2026."
    shifted = _shift_citations(
        report,
        first_id=5,
        last_id=6,
        delta=3,
    )
    assert shifted == "Agent[^2], independent[^8][^9], year 2026."
