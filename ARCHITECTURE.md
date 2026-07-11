# biz-analyst-agent 架构总结与对比

> 2026-07-11 更新 · 结合 GitHub 同类项目分析

## 一、当前架构

### 核心定位
自主分析流水线 + 自动证伪闭环，**明确区别于 chatbot**。不是交互问答，而是"给定主题→自主检索→分析→证伪→生成报告"的端到端 pipeline。

### 六阶段流水线

```
Scope → Collect → Analyze → Falsify → Refine → Report
```

| 阶段 | 核心逻辑 | 关键设计 |
|------|----------|----------|
| **Scope** | 动态生成分析维度（非固定模板） | Domain Memory 匹配行业框架；12视角池+sections驱动 |
| **Collect** | 多源检索（Exa主→Tavily补充→SEC/iFinD/em_news） | 时效过滤(days=180新闻/365证伪)；语义检索证据池(TF-IDF) |
| **Analyze** | 生成结构化洞察（非数据复述） | 论点需含量化含义+主动反驳共识；分析师只输出claim+reasoning+evidence+confidence，不写可证伪条件 |
| **Falsify** | 九维红队挑战+裁决 | 红队基于现有公开数据独立输出 falsification_path（推翻路径）；上下文感知(非上市不要求财报)；**时效约束**(注入today+data_as_of，禁止要求尚未发布的数据，`_sanitize_temporal_challenges`降级泛泛"数据过时"投诉)；solid放宽至允许low；自我迭代补强incomplete |
| **Refine** | stale_count强制转向+争议辩论 | 洞察自我迭代(搜支撑→二次裁决)；辩论回合(analyst vs red_team) |
| **Report** | 三段narrative+附录 | 事实层(摘要+事实+历史趋势)→核心发现→风险展望；结构归一化后处理 |

### 反chatbot三条约束（写进代码，2026-06-27 修订为不过度教条）
1. **证据冻结**：仅当 (a)无新反证 AND (b)现有支撑<2条 AND (c)无搜索能力 三者同时满足时才冻结置信度。现有证据充分或红队判定solid时不冻结。
2. **可证伪性（红队来定）**：可证伪条件不由分析师自述（天然倾向等未来数据），而是由红队在九维挑战中基于现有公开数据独立输出 `falsification_path`——"什么样的现已公开可查的证据会推翻此结论"。禁止"若下季度/若明年/若后续"等需要等待未来数据的假设。代码后处理 `_sanitize_falsification_path()` 检测未来假设词并替换。
3. **异源红队**：fresh-thread审查、第1轮强制全量证伪。红队对逻辑推导型洞察应攻击推理链（前提是否成立？推导是否有效？）而非要求更多外部证据。

### 三角色LLM管道
- **analyst**（主分析）：Scope/Collect/Analyze
- **red_team**（红队）：Falsify（九维挑战，fresh thread）
- **reviewer**（终审）：Reverdict/Report（裁决+撰写）
- 按tier+family自动指派，单Key降级透明标注

### 三类持久化存储 + Reflection 学习环

```
Experience ──> Reflection ──> Domain
                    └───────> Behavior

Domain ─────> Scope / Collect（决定看什么）
Behavior ───> Collect / Falsify / Report（决定怎么做）
```

| 存储 | 文件 | 用途 | 读取时机 |
|------|------|------|----------|
| **Experience** | `experience.json` | 任务摘要 + 评测反馈 | Reflection 时读（跨任务复盘输入） |
| **Domain** | `domain.json` | 行业分析框架与失败模式 | Scope 阶段读（决定看什么） |
| **Behavior** | `behavior.json` | 证伪策略 + 各阶段行为策略卡 | Collect/Falsify/Report 读（决定怎么做） |

**Reflection** 是瞬态学习过程（非第四层存储）：run 结束后分析 gaps，蒸馏产出直接写入 Domain + Behavior。

> ⚠ 线程安全：所有写操作通过 `_transact()` 在锁内完成读-改-写事务。当前仅保证单进程安全，多进程部署前需迁移到 SQLite。

### 证据体系
- 7级溯源等级（T1交易所→T7博客）
- 时效加权（freshness_factor：30d=1.0→365d=0.6）
- 语义检索（embedding(sentence-transformers) → TF-IDF → keyword 三级降级）
- URL自动升级tier（监管→T4/交易所→T3/权威媒体→T5）

### 报告结构（归一化后）
```
## 执行摘要 (纯判断，不含表格，末尾点明核心论点)
## 基本事实/行业格局 (摘要表+财务/格局表+龙头表+历史趋势表)
## 核心发现 (###子标题，每条呼应执行摘要论点)
## 风险与不确定性 (###子标题，每条引用具体核心发现)
## 展望与关注点 (跟踪表+情景推演)
---
参考文献 (核心引用≤15条，APA风格)
数据截至·数据源·免责声明
```

---

## 二、对比GitHub同类项目

| 维度 | 本项目 | TradingAgents (18K★) | virattt/dexter | intellifin (92%准确率) |
|------|--------|---------------------|----------------|----------------------|
| **证伪闭环** | 九维挑战+自我迭代+辩论回合 | 无（仅多agent讨论） | 无 | 无 |
| **记忆系统** | 三类持久化存储+Reflection学习环 | 无 | 无 | 无 |
| **信源分级** | 7级tier+时效加权+URL自动升级 | 无 | 无 | 无 |
| **反chatbot约束** | 三条硬约束写进代码 | 无 | 无 | 无 |
| **质量评估** | 8维度评分+改进循环+防高估锚定 | 无 | eval benchmark | 准确率基准 |
| **断点续跑** | SQLite检查点 | 无 | 无 | 无 |
| **搜索多样性** | Exa+Tavily+Serper+SEC+iFinD+em_news | 单一搜索 | 单一搜索 | 单一搜索 |
| **上下文感知** | 红队按上市/非上市/行业调整+时效约束(禁止要求尚未发布的数据) | 无 | 无 | 无 |
| **报告质量** | 三段narrative+结构归一化+逻辑一致性检查 | 模板填充 | 摘要生成 | 结构化输出 |
| **成本追踪** | TokenTracker按provider/role/stage汇总 | 无 | 无 | 无 |

### 本项目独有优势
1. **九维证伪框架**——其他项目最多做多agent讨论，没有系统化的证伪维度
2. **持久化记忆+Reflection学习环**——Experience→Reflection→Domain/Behavior，跨任务自我改进
3. **信源分级+时效加权**——其他项目不区分来源可靠性
4. **上下文感知红队**——按公司类型调整挑战策略
5. **反chatbot硬约束**——代码层面防止"反思剧场"，可证伪条件须基于当前已公开可查数据（非未来假设），逻辑推导型洞察不降级

---

## 三、待补充完善的方向

### P0（高优先级）— 已全部完成 ✓

1. ~~**搜索召回率提升**~~ ✓
   - 已完成：Exa中英双语查询+URL去重；Tavily/Serper补充
   - 效果：中文主题搜索召回率显著提升

2. ~~**证据链攻防逻辑可视化**~~ ✓
   - 已完成：InsightCard重写为"攻防时间线"视图（analyst论点→red_team九维挑战→反证→补强→裁决）
   - 效果：用户可直观看到每条洞察的攻防全过程

3. ~~**报告逻辑连贯性验证**~~ ✓
   - 已完成：Report阶段新增 logic_consistency_prompt，检查核心发现是否呼应执行摘要、风险是否引用具体发现
   - 效果：消除"各说各话"问题

### P1（中优先级）— 已全部完成 ✓

4. ~~**嵌入语义检索升级**~~ ✓
   - 已完成：sentence-transformers生成嵌入向量，三级降级（embedding→TF-IDF→keyword）
   - 效果：跨表述/跨语言的证据检索准确率提升

5. ~~**分析质量基准集升级**~~ ✓
   - 已完成：quality_benchmark扩展至47项，含"已知答案验证"场景
   - 效果：可量化准确率并对比迭代效果

6. ~~**Token成本追踪**~~ ✓
   - 已完成：TokenTracker按provider/role/stage累计token+估算成本，每阶段set_stage()
   - 效果：可定位成本最高的阶段并优化

### P2（长期方向）

7. ~~**Human-in-the-loop UI**~~ ✓
   - 已完成：Scope阶段暂停等用户确认维度（asyncio.Event, 300s超时自动继续）
   - 效果：用户可在分析前调整/确认维度

8. **多模态证据**
   - 当前：仅文本证据
   - 改进：支持图表/PDF/截图作为证据（OCR+图像理解）
   - 预期：财报图表中的数据可直接被引用

9. **实时数据接入**
   - 当前：搜索+SEC+金融数据源
   - 改进：接入实时行情API（如逐笔交易），支持短周期分析
   - 预期：支持日内/周内级别的分析场景

10. **Multi-tenant部署**
    - 当前：单用户本地部署
    - 改进：多用户隔离+权限管理+任务队列
    - 预期：团队级使用

---

## 四、技术栈

| 层 | 技术 |
|----|------|
| 后端 | FastAPI + SSE · Python 3.13 |
| 前端 | Vite + React + TypeScript |
| LLM | 多family支持（OpenAI/Claude/Gemini/DeepSeek等） |
| 搜索 | Exa MCP(mcporter) + Tavily + Serper |
| 金融数据 | SEC EDGAR + iFinD + NeoData + MX_FinSearch |
| 向量检索 | embedding(sentence-transformers) → TF-IDF → keyword 三级降级 |
| 存储 | SQLite（检查点） + JSON（记忆系统） |
| 测试 | test_units(59) + test_structure_invariants(12) + test_offline(端到端) + test_smoke(需Key) + 运行时8条结构校验 | golden_regression | eval benchmark | 准确率基准 |
