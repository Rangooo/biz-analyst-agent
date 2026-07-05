"""
黄金案例回归集 —— 防止"自我迭代"把系统越改越差。

留存一组已知正确行为的断言，每次改动 prompt / 框架 / 编排逻辑后运行，
确保核心不变量不被破坏。这是 scholar-loop "确定性守卫" 思想的轻量落地。

运行：cd backend && python -m evals.golden_regression
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import orchestrator as orch_mod
from demo_data import demo_chat_json, demo_search, demo_sec
from llm.client import get_client
from schemas import AnalysisRun, Verdict
from orchestrator import Orchestrator


async def run_case(query: str) -> AnalysisRun:
    # 强制 demo 模式 + 注入脚本数据
    get_client().chat_json = demo_chat_json
    orch_mod.search_tool.search = demo_search
    orch_mod.search_tool.has_search_backend = lambda: True
    orch_mod.sec_edgar.get_key_financials = demo_sec
    run = AnalysisRun(query=query, provider="deepseek")
    orch = Orchestrator(run)
    orch.demo = True
    async for _ in orch.run_pipeline():
        pass
    return run


def check(name: str, cond: bool):
    mark = "✅" if cond else "❌"
    print(f"  {mark} {name}")
    if not cond:
        check.failed += 1
check.failed = 0


async def main():
    print("=== 黄金回归集 ===\n")

    print("[案例1] 奇富科技（上市·助贷）")
    run = await run_case("奇富科技")
    check("流程正常完成", run.status == "done")
    check("命中助贷模板", run.profile.template_key == "fintech_lending")
    check("识别为上市公司且有ticker", run.profile.is_public and run.profile.ticker == "QFIN")
    check("产出至少4条洞察", len(run.insights) >= 4)

    # 核心不变量：空话/无推理链的论断必须被红队降级（低置信度）
    # 新架构：is_falsifiable 不再由分析师标记（全部默认True），由红队通过 verdict 判定
    _shallow = [i for i in run.insights if i.confidence < 0.35]
    check("空话论断被红队降级(低置信≤0.35)", len(_shallow) >= 1)

    # 核心不变量：所有洞察都必须经过红队检验（执行者不能自判）
    # 新架构：所有洞察默认 is_falsifiable=True，红队对每条都做九维挑战
    check("所有洞察都经过≥1轮证伪", all(len(i.falsifications) >= 1 for i in run.insights))

    # 核心不变量：证伪必须引入外部新证据（有反证）
    has_counter = any(
        any(not e.supports for e in i.evidence) for i in run.insights
    )
    check("证伪过程引入了外部反证", has_counter)

    # 核心不变量：报告包含免责声明与证据溯源
    check("narrative 含免责声明", "不构成投资建议" in run.narrative_md)
    check("narrative 含证据溯源或参考文献", "[^" in run.narrative_md or "证据" in run.narrative_md or "参考" in run.narrative_md)

    print("\n[案例2] 字节跳动（非上市）")
    run2 = await run_case("字节跳动")
    check("识别为非上市公司", not run2.profile.is_public)
    check("非上市公司无ticker", run2.profile.ticker == "")
    check("流程正常完成", run2.status == "done")
    check("动态生成非上市适配维度(含估值/生态位/商业化)",
          any("估值" in s or "生态" in s or "商业化" in s for s in run2.profile.sections))

    print("\n[案例3] 行业分析")
    run3 = await run_case("中国新能源汽车行业")
    check("识别为行业", run3.profile.kind == "industry")
    check("行业有对标公司作为载体", len(run3.profile.peers) >= 1)
    check("动态生成行业维度(含增速/格局/份额)",
          any("增速" in s or "格局" in s or "份额" in s for s in run3.profile.sections))

    print("\n[案例4] 新能力：时效/动态维度/完整文档/多数据源（奇富科技）")
    # 重新跑一次拿到完整 evidence_pool
    run = await run_case("奇富科技")
    check("动态维度非固定四板块",
          bool(run.profile.sections) and run.profile.sections != ["财务表现", "经营指标", "战略动向", "产品与竞争"])
    all_ev = [e for ins in run.insights for e in ins.evidence]
    check("证据含时效字段(published_at或as_of)",
          any(e.as_of or e.published_at for e in all_ev))
    check("记录数据截至时点", bool(run.data_as_of))
    check("记录使用的数据源(≥2个)", len(run.data_sources_used) >= 2)
    check("生成完整分析文档(narrative)", bool(run.narrative_md) and "执行摘要" in run.narrative_md)
    check("完整文档含历史趋势", "历史趋势" in run.narrative_md or "拐点" in run.narrative_md)
    check("完整文档含数据截至声明", "数据截至" in run.narrative_md)
    # narrative 是 LLM 自由撰写的文档，不强制要求包含精确的维度名称（insights 已单独展示）
    check("narrative 非空且有实质内容", len(run.narrative_md) > 200)

    print(f"\n=== 结果：{'全部通过 ✅' if check.failed == 0 else f'{check.failed} 项失败 ❌'} ===")
    sys.exit(1 if check.failed else 0)


asyncio.run(main())
