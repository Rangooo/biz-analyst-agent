from evals.report_pairwise_judge import _parse_compact_votes


def test_parse_compact_votes_recovers_final_line_after_reasoning():
    text = """先进行内部比较，这部分不应作为答案。
准确性=A;洞察深度=B;论证有效性=TIE;完整度=A;实用性=B;可读性=A;总体=A"""
    winners, overall = _parse_compact_votes(text)
    assert winners["准确性"] == "A"
    assert winners["论证有效性"] == "TIE"
    assert overall == "A"
