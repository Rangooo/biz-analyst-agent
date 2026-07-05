"""
模型能力目录 + 资源驱动的角色自动指派 + 流程规划。

设计目标（项目可共享）：
- 不把任何用户私有的内网代理硬编码进仓库；providers.yaml 只放主流公网预设，
  用户私有/内网配置放 providers.local.yaml（gitignore）。
- 用户配置好自己有的模型/key 后，agent 按"智能程度(tier)+家族(family)"自动指派
  主分析/红队/终审，并据可用资源（LLM/搜索/金融源）规划整条流程与 backup。
- 用户仍可在前端下拉手动覆盖。

tier（智能程度，1-5，5 最强）；family（用于异源判定，不同家族才算异源红队）；
reasoning（推理模型，适合终审/深度审核，但慢）。
"""
from __future__ import annotations

from typing import Any

# 主流公网模型预设（共享仓库自带）。key 唯一；用户在 .env 填对应 API key 即启用。
PRESETS: list[dict] = [
    {"key": "deepseek-v4-pro", "label": "DeepSeek-V4-Pro", "family": "deepseek", "tier": 5,
     "base_url": "https://api.deepseek.com/v1", "model": "deepseek-v4-pro",
     "api_key_env": "DEEPSEEK_API_KEY", "reasoning": False},
    {"key": "deepseek-v4-flash", "label": "DeepSeek-V4-Flash", "family": "deepseek", "tier": 4,
     "base_url": "https://api.deepseek.com/v1", "model": "deepseek-v4-flash",
     "api_key_env": "DEEPSEEK_API_KEY", "reasoning": False},
    {"key": "openai", "label": "OpenAI GPT-4o", "family": "openai", "tier": 4,
     "base_url": "https://api.openai.com/v1", "model": "gpt-4o",
     "api_key_env": "OPENAI_API_KEY", "reasoning": False},
    {"key": "claude", "label": "Claude Sonnet", "family": "anthropic", "tier": 5,
     "base_url": "https://api.anthropic.com/v1", "model": "claude-sonnet-4-5",
     "api_key_env": "ANTHROPIC_API_KEY", "reasoning": False},
    {"key": "gemini", "label": "Gemini 2.5 Pro", "family": "google", "tier": 5,
     "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "model": "gemini-2.5-pro",
     "api_key_env": "GEMINI_API_KEY", "reasoning": False},
    {"key": "qwen", "label": "Qwen-Max", "family": "qwen", "tier": 4,
     "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-max",
     "api_key_env": "DASHSCOPE_API_KEY", "reasoning": False},
    {"key": "welm", "label": "WeLM", "family": "welm", "tier": 3,
     "base_url": "https://welm.weixin.qq.com/v1", "model": "welm-pro",
     "api_key_env": "WELM_API_KEY", "reasoning": False},
    {"key": "ollama", "label": "Ollama (本地)", "family": "ollama", "tier": 3,
     "base_url": "http://localhost:11434/v1", "model": "qwen2.5:14b",
     "api_key_env": "OLLAMA_API_KEY", "reasoning": False},
]

# 金融/搜索数据源预设（展示/规划用；运行时权威目录见 tools.finance_sources.DATA_SOURCE_CATALOG）
DATA_SOURCE_PRESETS: list[dict] = [
    {"key": "tavily", "label": "Tavily 搜索", "kind": "search", "env": "TAVILY_API_KEY", "free_quota": 1000},
    {"key": "serper", "label": "Serper 搜索", "kind": "search", "env": "SERPER_API_KEY", "free_quota": 2500},
    {"key": "sec_edgar", "label": "SEC EDGAR 财报", "kind": "finance", "env": "SEC_USER_AGENT", "free": True},
    {"key": "wind", "label": "Wind MCP / AIFin Market", "kind": "finance", "env": "WIND_MCP_TOOL",
     "free_quota": 1000, "note": "AIFin Market 每日约1000次；推荐通过 mcporter 调用 server.tool"},
    {"key": "ifind", "label": "iFinD 同花顺", "kind": "finance", "env": "IFIND_SCRIPT", "note": "skill 脚本路径"},
    {"key": "neodata", "label": "NeoData 金融", "kind": "finance", "env": "NEODATA_SCRIPT", "note": "skill 脚本路径"},
    {"key": "em_news", "label": "东方财富资讯", "kind": "finance", "env": "",
     "free": True, "note": "内置公开资讯检索，无需 API key 或脚本路径"},
    {"key": "exa_search", "label": "Exa 语义搜索", "kind": "search", "env": "EXA_API_KEY", "free": True,
     "note": "优先使用 EXA_API_KEY 直连 Exa API；未配置时回退 mcporter MCP"},
]


def _meta(providers_cfg: dict) -> list[dict]:
    """给 providers 配置补 family/tier/reasoning 元信息。
    优先用 provider 自带声明(tier/family/reasoning)，其次查 PRESETS，否则 custom 默认。"""
    by_key = {p["key"]: p for p in PRESETS}
    out = []
    for k, p in providers_cfg.items():
        preset = by_key.get(k, {})
        out.append({
            "key": k, "label": p.get("label", k),
            "family": p.get("family", preset.get("family", "custom")),
            "tier": p.get("tier", preset.get("tier", 3)),
            "reasoning": p.get("reasoning", preset.get("reasoning", False)),
            "model": p.get("model", ""), "is_primary": p.get("is_primary", False),
            "custom": k not in by_key,
        })
    return out


def auto_assign_roles(providers_cfg: dict, available_keys: set[str]) -> dict:
    """按智能程度+家族自动指派 analyst/red_team/reviewer。
    - analyst：tier 最高的非推理模型（核心分析，要强+快）；只有推理模型时退而用之。
    - reviewer：写完整文档+自审，要"有逻辑+会写作+快"→ tier 最高的非推理模型（可与 analyst 同；
      推理模型(R1)虽逻辑强但写长文太慢易超时，仅当无强非推理模型时才退而用之）。
    - red_team：与 analyst 不同家族（异源），优先非推理（红队调用多，要快）；无则任意异实例；再无则同源降级。
    返回 {analyst, red_team, reviewer, same_source, reason}。"""
    metas = [m for m in _meta(providers_cfg) if m["key"] in available_keys]
    if not metas:
        return {"analyst": None, "red_team": None, "reviewer": None,
                "same_source": True, "reason": "无可用 LLM"}
    # 非推理优先（快+会写作），tier 降序
    by_fast = sorted(metas, key=lambda m: (-m["tier"], 1 if m["reasoning"] else 0))
    analyst = by_fast[0]
    # reviewer：与 analyst 同标准（最强非推理）；若无强非推理才退到推理
    reviewer = by_fast[0] if by_fast[0] else next(iter(metas), None)
    # red_team：与 analyst 不同家族，非推理优先
    diff_fam = [m for m in metas if m is not analyst and m["family"] != analyst["family"]]
    diff_fam.sort(key=lambda m: (1 if m["reasoning"] else 0, -m["tier"]))
    red = diff_fam[0] if diff_fam else next((m for m in metas if m is not analyst), None)
    same = (not red) or (analyst["key"] == red["key"])
    reason = (f"主分析={analyst['label']}(tier{analyst['tier']})；"
              f"终审={reviewer['label']}(tier{reviewer['tier']}{'·推理' if reviewer['reasoning'] else '·非推理快写'})；"
              f"红队={'无→同源降级' if not red else red['label']+'(' + ('异源' if red['family'] != analyst['family'] else '同源') + ')'}")
    return {"analyst": analyst["key"], "red_team": red["key"] if red else None,
            "reviewer": reviewer["key"], "same_source": same, "reason": reason}


def plan_resources(providers_cfg: dict, available_keys: set[str],
                   search_status: str, fin_sources: list[str]) -> dict:
    """据可用资源规划整条流程与 backup。返回 plan dict（供编排器开场播报）。"""
    roles = auto_assign_roles(providers_cfg, available_keys)
    # 证据/反证检索 backup 链：免费金融源优先 → 通用搜索 → 无
    chain = [s for s in ("wind", "em_news", "ifind", "neodata", "sec_edgar") if s in fin_sources]
    if search_status in ("ok", "degraded"):
        chain.append("general_search")
    grounded = len(chain) > 0
    dual = not roles["same_source"]
    plan = {
        "analyst": roles["analyst"], "red_team": roles["red_team"],
        "reviewer": roles["reviewer"], "same_source": roles["same_source"],
        "dual_model": dual, "grounded": grounded,
        "search_chain": chain,
        "search_status": search_status,
        "reviewer_auto": roles["reason"] if roles["analyst"] else "无可用模型",
    }
    return plan
