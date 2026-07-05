"""LLM-as-judge 可证伪条件评分客户端。

配合 goldens/falsifiability_rubric.md 的 4 维 rubric 和 goldens/judge_prompt.md 的 prompt。
在 golden_answers.py --live 模式中调用，对每条洞察的可证伪条件做精确 4 维评分。

规则版（check_falsifiable_quality_v2）是近似实现，离线可用；
本模块是 LLM 版，更精确但需要 API Key。

用法：
    from evals.judge_client import score_condition
    result = score_condition(claim, condition)
    # result = {"exempt": False, "scores": {...}, "total": 6, "pass": True, ...}
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
ROOT_DIR = BACKEND_DIR.parent
sys.path.insert(0, str(BACKEND_DIR))

# judge prompt 从 goldens/judge_prompt.md 加载
_JUDGE_PROMPT_PATH = None


def _get_judge_system_prompt() -> str:
    """从 goldens/judge_prompt.md 提取 System Prompt 文本。"""
    global _JUDGE_PROMPT_PATH
    if _JUDGE_PROMPT_PATH is None:
        p = ROOT_DIR / "goldens" / "judge_prompt.md"
        _JUDGE_PROMPT_PATH = p
    if not _JUDGE_PROMPT_PATH.exists():
        return _DEFAULT_JUDGE_PROMPT  # 回退到内置 prompt
    text = _JUDGE_PROMPT_PATH.read_text(encoding="utf-8")
    # 提取 ## System Prompt 后 ``` 包裹的内容
    if "## System Prompt" in text:
        parts = text.split("## System Prompt", 1)[1]
        if "```" in parts:
            return parts.split("```", 1)[1].rsplit("```", 1)[0]
    return text


_DEFAULT_JUDGE_PROMPT = """你是一名严苛的商业分析评审员，专门评估"可证伪条件"的质量。
对给定的（洞察，可证伪条件）pair 按 4 维 rubric 打分，输出严格 JSON。

# 4 维评分（每维 0-2 分）
A. 数据可得性: 0=未来数据/不可得, 1=存在但受限, 2=公开年报/监管/协会
B. 时间锚定: 0=含未来假设词, 1=模糊, 2=明确≤当前可观测期
C. 阈值明确: 0=仅定性, 1=方向但无数值, 2=数值阈值+口径
D. 单一变量: 0=多因素混合, 1=主变量+次因素, 2=单一可观测变量

豁免：空条件或"逻辑推导型"→ {"exempt": true, "reason": "logical_derivation"}
输出：{"exempt": false, "scores": {"data_availability": 0-2, "temporal_anchoring": 0-2, "threshold_specificity": 0-2, "single_variable": 0-2}, "total": 0-8, "pass": total>=6, "violations": [...], "fix_suggestion": "..."}
"""


def score_condition(claim: str, condition: str, provider: str = "deepseek") -> dict:
    """用 LLM 对单条 (claim, condition) 做 4 维评分。

    返回 dict: {exempt, scores, total, pass, violations, fix_suggestion}
    如果 LLM 不可用或解析失败，回退到规则版 v2。
    """
    c = (condition or "").strip()
    if not c:
        return {"exempt": True, "reason": "logical_derivation", "total": 8, "pass": True}

    system = _get_judge_system_prompt()
    user = f"洞察：{claim}\n\n可证伪条件：{c}"

    try:
        from llm.client import get_client
        client = get_client()
        resp = client.chat(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            role="reviewer",
            provider=provider,
            temperature=0.0,
            max_tokens=512,
        )
        text = resp.strip()
        # 去除可能的 markdown 包裹
        if text.startswith("```"):
            text = text.split("```", 1)[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.rsplit("```", 1)[0]
        result = json.loads(text)
        result.setdefault("pass", result.get("total", 0) >= 6)
        return result
    except Exception:
        # 回退到规则版 v2
        from evals.golden_answers import check_falsifiable_quality_v2
        total, scores, detail = check_falsifiable_quality_v2(c)
        return {
            "exempt": scores.get("exempt", False),
            "scores": scores,
            "total": total,
            "pass": total >= 6,
            "violations": [detail] if total < 6 else [],
            "fix_suggestion": "",
            "fallback": True,  # 标记使用了规则版回退
        }


def score_batch(claims_conditions: list[tuple[str, str]], provider: str = "deepseek") -> list[dict]:
    """批量评分。claims_conditions = [(claim, condition), ...]"""
    return [score_condition(c, cond, provider) for c, cond in claims_conditions]


if __name__ == "__main__":
    # 快速测试
    test_cases = [
        ("消费信贷增速放缓至6.5%", "若央行最新金融统计报告显示消费贷款增速高于10%（当前为6.5%），则结论被推翻"),
        ("增速放缓是结构性的", "若下季度增速回升至10%以上则推翻"),
        ("杠杆率见顶导致增速下移", ""),
    ]
    for claim, cond in test_cases:
        r = score_condition(claim, cond)
        print(f"洞察: {claim[:30]}...")
        print(f"条件: {cond[:50] if cond else '(空)'}")
        print(f"结果: {r}")
        print()
