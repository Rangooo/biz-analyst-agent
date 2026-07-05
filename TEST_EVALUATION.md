# 测试与评估体系

更新日期：2026-07-05

## 分层原则

测试体系分四层，避免把工程回归、流程集成和模型质量评估混成一组数字：

| 层级 | 目录/入口 | 定位 | 默认是否进 quick |
|---|---|---|---|
| Unit | `backend/tests/unit/` | 纯函数、prompt 约束、后处理、置信度、检测逻辑 | 是 |
| Contract | `backend/tests/contract/` | 数据源适配器、配置安全、指标/引用、存储契约 | 是 |
| Integration | `backend/tests/integration/` | demo/mock pipeline、PDF 解析 | 否，标记 `slow` |
| Eval sanity | `backend/tests/eval/` | 真实 LLM 输出冒烟 | 否，标记 `smoke` |

## 运行方式

```bash
cd backend

# 日常工程回归：unit + contract（119 项，<5s）
python run_all_tests.py --quick

# 完整离线回归：unit + contract + integration，不跑真实 LLM
python run_all_tests.py

# 真实 LLM 冒烟测试：需要 API key
python run_all_tests.py --smoke

# Golden Answers 离线题库自检（需要真实 LLM）
python -m evals.golden_answers --report
```

## Golden 与普通测试的边界

普通测试回答"工程机制是否按预期运行"，应尽量确定、快速、可复现。

Golden Answers 回答"模型产物质量如何"，属于评估工具。它们可以手动运行，但不进入 quick 回归。

## 当前重点覆盖

- 证伪闭环：未来数据不能作为证伪条件；低证据/低置信结论有上限。
- 报告结构：执行摘要、事实层、历史趋势、核心发现、风险展望、参考文献不重复不串段。
- 数据治理：财务单位/期间混用、强归因缺直接证据在 pipeline 内直接处理（不再留附录）。
- 来源治理：监管/交易所/官方披露/媒体分层；公司官网识别依赖 profile-aware 规则，而非公司白名单。
- 采集契约：Exa、Wind、SEC、通用搜索等 adapter 返回统一 evidence 形状，失败要有 error meta。
- PDF 解析：A 股年报专用解析 + 通用 PDF 文本/表格提取。
- 事实校验：pipeline 内部 fact_check 提取断言并对照证据池验证，结果驱动报告迭代改进。

## 维护规则

- 新机制优先加 unit/contract；只有跨阶段行为才加 integration。
- mock pipeline 必须完全 mock LLM 文本与 JSON 调用、数据源 adapter、存储/记忆写入，不能依赖 `.env`。
- 异步测试统一用 `asyncio.run()` 或 `pytest.mark.asyncio`，不要使用 `asyncio.get_event_loop().run_until_complete()`。
- 新增真实 API 测试标记 `smoke`。
