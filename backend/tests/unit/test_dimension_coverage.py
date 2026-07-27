from reporting.writing_contract import ensure_dimension_coverage_boundaries


def test_dimension_prefix_counts_as_covered_without_duplicate_boundary_row():
    report = """## 行业格局
### 分析维度覆盖
| 分析维度 | 结论 | 证据 |
|---|---|---|
| 规模与周期定位 | 景气分化 | [^1] |

## 核心发现
内容
"""
    section = "规模与周期定位：市场规模、增速与周期位置"
    repaired, missing = ensure_dimension_coverage_boundaries(report, [section])
    assert missing == []
    assert repaired.count("规模与周期定位") == 1


def test_existing_boundary_duplicate_is_removed():
    report = """### 分析维度覆盖
| 分析维度 | 结论 | 证据 |
|---|---|---|
| 规模与周期定位 | 景气分化 | [^1] |
| 规模与周期定位：市场规模与周期 | **证据边界**：证据不足 | 无可靠证据 |
"""
    repaired, missing = ensure_dimension_coverage_boundaries(
        report, ["规模与周期定位：市场规模与周期"]
    )
    assert missing == []
    assert repaired.count("| 规模与周期定位") == 1
    assert "证据边界" not in repaired
