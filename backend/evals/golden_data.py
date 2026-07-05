"""Golden Answer Set —— 基准答案数据（人工策划、真实可溯源）。

这是 golden_answers.py 的"题库"，与执行/比对逻辑分离，便于维护扩充。

三类校验：
1. 事实校验集（required_facts / forbidden_facts）—— 防数据幻觉
2. 洞察方向集（expected_directions / unexpected_directions）—— 验证分析深度
3. 可证伪条件质量（由 golden_answers.py 的规则检查器统一处理，无需逐案例配置）

【数据来源原则】
- 所有 fact 必须来自官方财报/监管披露/权威媒体，带 source + as_of。
- 数值容差由 FactCheck.tolerance 控制（默认 ±12%，可逐条覆盖）。
- 非上市公司数据稀疏，required_facts 以"媒体披露口径"为主并标注 confidence。
- 数据截止：2026-06（2025 年报普遍已发布）。维护时请更新 as_of 与来源。

【重要】这是"基准答案"，本身绝不能有幻觉。新增/修改 fact 时务必核对官方来源。
"""
from __future__ import annotations

from dataclasses import dataclass, field


# ─────────────────────────────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────────────────────────────
@dataclass
class FactCheck:
    """一条事实校验项。

    label:       人类可读标签（如"营业收入"）
    keywords:    必须命中的关键词（同义词任一命中即可，OR 关系）
    value:       期望数值（None 表示只校验关键词出现，不校验数值）
    unit:        单位（仅用于展示）
    tolerance:   数值容差（相对比例，如 0.12 表示 ±12%）
    source:      数据来源（溯源用）
    as_of:       报告期
    confidence:  数据可信度 high/medium/low（非上市公司多为 medium）
    """
    label: str
    keywords: list[str]
    value: float | None = None
    unit: str = ""
    tolerance: float = 0.12
    source: str = ""
    as_of: str = ""
    confidence: str = "high"


@dataclass
class GoldenCase:
    """一个 golden 案例。"""
    query: str
    kind: str                                  # company / industry
    is_public: bool
    note: str = ""                             # 案例说明（覆盖什么场景）
    # 1. 事实校验集
    required_facts: list[FactCheck] = field(default_factory=list)
    forbidden_facts: list[str] = field(default_factory=list)   # 必须不出现的（防幻觉负向断言）
    # 2. 洞察方向集
    expected_directions: list[dict] = field(default_factory=list)
    # 每项 {"name": 方向名, "keywords": [命中任一即算覆盖], "must": bool 是否必须覆盖}
    unexpected_directions: list[dict] = field(default_factory=list)
    # 每项 {"name": 错误方向名, "keywords": [出现即扣分]}
    min_direction_coverage: float = 0.5        # 期望方向最低覆盖率（must 项必须全中）


# ─────────────────────────────────────────────────────────────────────────
# 案例库
# ─────────────────────────────────────────────────────────────────────────
CASES: list[GoldenCase] = [

    # ===== 1. 奇富科技（美股/港股上市，金融科技）—— 事实校验主案例 =====
    GoldenCase(
        query="分析奇富科技2025年业绩",
        kind="company",
        is_public=True,
        note="上市金融科技公司，事实校验主案例。数据来自2026-03-18官方年报。",
        required_facts=[
            FactCheck("营业收入", ["192", "营收", "收入"], 192.05, "亿元", 0.05,
                      "奇富科技2025年报(2026-03-18)", "FY2025", "high"),
            FactCheck("GAAP净利润", ["59.76", "59.8", "净利润", "60亿"], 59.76, "亿元", 0.08,
                      "奇富科技2025年报", "FY2025", "high"),
            FactCheck("在贷余额", ["1260", "1,260", "在贷", "1260.12", "贷款余额"], 1260.12, "亿元", 0.08,
                      "奇富科技2025年报(截至2025-12-31)", "FY2025", "high"),
            FactCheck("增长趋势-营收增长", ["营收", "收入", "增长", "11.9", "增"], None, "",
                      0.12, "奇富科技2025年报", "FY2025", "high"),
            FactCheck("增长趋势-利润承压", ["净利润", "下降", "承压", "下滑", "微降", "-4", "回落"], None, "",
                      0.12, "奇富科技2025年报(GAAP净利润同比-4.4%)", "FY2025", "high"),
        ],
        # 防幻觉：奇富2025确实回购+派息（已发生），所以这些不能列为"禁止出现"
        # 但要禁止编造未发生的资本动作
        forbidden_facts=[
            "亏损", "净亏损",          # 奇富2025是盈利的，不能说亏损
            "增发新股", "配股", "定向增发",  # 2025年未做股权融资稀释
            "营收下降", "营收下滑", "收入下降",  # 营收是+11.9%增长，不能说下降
        ],
        expected_directions=[
            {"name": "轻资本/技术服务转型", "keywords": ["轻资本", "技术服务", "科技服务", "ICE", "平台服务"], "must": True},
            {"name": "在贷余额收缩/规模主动调整", "keywords": ["收缩", "下降", "调整", "缩表", "余额下降", "主动"], "must": False},
            {"name": "资产质量/逾期率变化", "keywords": ["逾期", "资产质量", "不良", "2.71", "风险暴露"], "must": False},
            {"name": "股东回报(回购+分红)", "keywords": ["回购", "分红", "派息", "股息", "股东回报"], "must": False},
            {"name": "利润增长引擎切换(量→价/质)", "keywords": ["驱动", "引擎", "take rate", "单位经济", "质量", "结构"], "must": False},
        ],
        unexpected_directions=[
            {"name": "把奇富数据当成整个助贷行业", "keywords": ["整个行业规模", "行业总规模", "全行业余额"]},
            {"name": "无依据的高增长叙事", "keywords": ["高速增长", "爆发式增长", "翻倍增长"]},
            {"name": "用传统银行估值框架套助贷", "keywords": ["市净率为主", "PB-ROE框架", "类似工商银行", "按银行股估值"]},
        ],
        min_direction_coverage=0.4,
    ),

    # ===== 2. 英伟达（美股，AI芯片）=====
    GoldenCase(
        query="分析英伟达(NVIDIA)的业绩与竞争格局",
        kind="company",
        is_public=True,
        note="美股AI芯片龙头，海外上市公司。注意财年特殊(FY2025=2024.2-2025.1)。",
        required_facts=[
            FactCheck("数据中心业务主导", ["数据中心", "data center", "data centre"], None, "", 0.12,
                      "NVIDIA官方财报", "FY2025", "high"),
            FactCheck("高增长", ["增长", "增", "翻倍", "94", "翻"], None, "", 0.12,
                      "NVIDIA官方财报(FY2025 Q3营收同比+94%，FY2025=2024.2-2025.1)", "FY2025 Q3(非全年)", "high"),
            FactCheck("Blackwell新架构", ["Blackwell", "新架构", "新一代", "B200", "GB200"], None, "", 0.12,
                      "NVIDIA官方", "FY2025", "high"),
        ],
        forbidden_facts=[
            "亏损", "净亏损",          # 英伟达高度盈利
            "营收下滑", "收入下降",     # 营收高增长
            "市场份额被超越", "失去AI芯片龙头地位",  # 仍是龙头
        ],
        expected_directions=[
            {"name": "CUDA软件生态护城河", "keywords": ["CUDA", "软件生态", "生态", "开发者", "锁定", "护城河"], "must": False},
            {"name": "客户集中度风险(超大规模云厂商)", "keywords": ["客户集中", "集中度", "云厂商", "大客户", "超大规模", "Microsoft", "Meta", "依赖"], "must": True},
            {"name": "供应链/先进封装瓶颈", "keywords": ["供应链", "HBM", "封装", "产能", "瓶颈", "台积电", "供应紧张"], "must": False},
            {"name": "云厂商自研芯片威胁", "keywords": ["自研", "定制芯片", "ASIC", "TPU", "替代", "议价"], "must": False},
        ],
        unexpected_directions=[
            {"name": "只看营收增长不看风险", "keywords": ["前景无忧", "毫无风险", "增长确定"]},
        ],
        min_direction_coverage=0.4,
    ),

    # ===== 3. 台积电（美股/台股，半导体代工）=====
    GoldenCase(
        query="分析台积电(TSMC)的业绩与行业地位",
        kind="company",
        is_public=True,
        note="半导体代工龙头，海外上市公司。",
        required_facts=[
            FactCheck("先进制程(3nm/5nm)主导", ["先进制程", "3nm", "5nm", "2nm", "N2", "纳米"], None, "", 0.12,
                      "台积电官方法说会(2026-01-15)", "FY2025 Q4", "high"),
            FactCheck("HPC/AI需求驱动", ["HPC", "高性能计算", "AI", "人工智能"], None, "", 0.12,
                      "台积电官方", "FY2025", "high"),
            FactCheck("高毛利率", ["毛利", "62", "60%", "毛利率"], None, "", 0.12,
                      "台积电官方(Q4毛利率62.3%)", "FY2025 Q4", "high"),
        ],
        forbidden_facts=[
            "亏损", "净亏损",
            "失去代工龙头", "被超越",
            "先进制程落后",          # 台积电先进制程领先
        ],
        expected_directions=[
            {"name": "AI/HPC需求拉动先进制程", "keywords": ["AI", "HPC", "需求", "拉动", "先进制程", "驱动"], "must": True},
            {"name": "海外建厂拖累毛利率", "keywords": ["海外", "亚利桑那", "美国", "建厂", "毛利", "爬坡", "成本"], "must": False},
            {"name": "2nm量产/技术领先", "keywords": ["2nm", "N2", "量产", "领先", "良率"], "must": False},
            {"name": "地缘政治/客户集中风险", "keywords": ["地缘", "集中", "大客户", "依赖", "苹果", "英伟达"], "must": False},
        ],
        unexpected_directions=[
            {"name": "忽视海外扩产成本", "keywords": ["毛利率持续提升无忧", "成本无压力"]},
        ],
        min_direction_coverage=0.4,
    ),

    # ===== 4. 字节跳动（非上市，互联网）=====
    GoldenCase(
        query="分析字节跳动(ByteDance)的业务与估值",
        kind="company",
        is_public=False,
        note="非上市公司代表。重点验证：标注数据可信度、不强行精确化、区分官方vs媒体披露。",
        required_facts=[
            FactCheck("TikTok/抖音核心业务", ["TikTok", "抖音", "短视频"], None, "", 0.12,
                      "媒体披露", "2025-2026", "medium"),
            FactCheck("AI投入(豆包/火山引擎)", ["豆包", "Doubao", "火山引擎", "AI", "大模型"], None, "", 0.12,
                      "媒体披露", "2025-2026", "medium"),
            FactCheck("电商业务(GMV)", ["电商", "GMV", "TikTok Shop", "带货"], None, "", 0.12,
                      "媒体披露", "2025", "medium"),
        ],
        forbidden_facts=[
            "官方财报显示",          # 非上市公司无官方财报，不能这么说
            "根据其招股书",          # 字节未上市，无招股书
            "已在纳斯达克上市", "已在港交所上市", "已IPO",  # 字节未上市
        ],
        expected_directions=[
            {"name": "非上市数据可信度标注", "keywords": ["数据可信度", "未经证实", "媒体披露", "估算", "知情人士", "口径", "不可得", "难以核实"], "must": True},
            {"name": "全球化(TikTok Shop)增长曲线", "keywords": ["全球化", "海外", "TikTok Shop", "出海", "国际"], "must": False},
            {"name": "AI商业化路径与不确定性", "keywords": ["AI", "豆包", "商业化", "变现", "火山引擎"], "must": False},
            {"name": "TikTok美国地缘政治风险", "keywords": ["地缘", "美国", "监管", "政治", "重组", "剥离"], "must": False},
        ],
        unexpected_directions=[
            {"name": "用上市公司精确财务框架套非上市公司", "keywords": ["市盈率为", "PE为", "ROE精确", "每股收益为"]},
            {"name": "把媒体估算当确凿事实", "keywords": ["确切营收为", "财报确认", "官方数据显示营收"]},
        ],
        min_direction_coverage=0.4,
    ),

    # ===== 5. SpaceX（2026-06新上市，航天/卫星互联网）=====
    GoldenCase(
        query="分析SpaceX的业务结构与估值逻辑",
        kind="company",
        is_public=True,
        note="航天/卫星互联网，2026-06-12纳斯达克IPO(代码SPCX，发行价$135，估值1.77万亿)。重点：区分已兑现现金流vs远期期权、累计亏损413亿。",
        required_facts=[
            FactCheck("Starlink为主要收入来源", ["Starlink", "星链", "卫星互联网"], None, "", 0.12,
                      "SpaceX招股书/财报(2025 Starlink营收113.87亿美元)", "FY2025", "high"),
            FactCheck("星舰(Starship)研发", ["星舰", "Starship", "可复用", "火箭"], None, "", 0.12,
                      "SpaceX招股书/媒体披露", "2025-2026", "high"),
            FactCheck("IPO上市(纳斯达克SPCX)", ["纳斯达克", "SPCX", "上市", "IPO", "135美元"], None, "", 0.12,
                      "SpaceX IPO(2026-06-12,纳斯达克,代码SPCX)", "2026-06", "high"),
        ],
        forbidden_facts=[
            "星舰已实现完全可复用商业运营",   # 截至2026年中星舰仍在验证阶段
            "Starlink已覆盖全球用户",       # 用户约千万级，远未全球覆盖
            "SpaceX尚未上市", "SpaceX是非上市公司", "未IPO",  # 2026-06已IPO
        ],
        expected_directions=[
            {"name": "Starlink现金牛vs星舰远期期权", "keywords": ["现金牛", "现金流", "期权", "远期", "未兑现", "估值溢价", "Starlink"], "must": True},
            {"name": "累计亏损vs高估值张力", "keywords": ["亏损", "413", "累计", "估值", "张力", "落差", "盈利"], "must": False},
            {"name": "星舰技术不确定性", "keywords": ["技术不确定", "验证阶段", "试飞", "可复用", "里程碑", "FAA", "监管"], "must": False},
            {"name": "Starlink用户增长曲线", "keywords": ["用户增长", "订阅", "渗透", "ARPU", "新兴市场"], "must": False},
        ],
        unexpected_directions=[
            {"name": "把试飞成功当技术成熟", "keywords": ["技术已成熟", "已可商用", "验证完成"]},
            {"name": "无视累计亏损谈盈利", "keywords": ["已实现盈利", "盈利稳健", "无亏损"]},
        ],
        min_direction_coverage=0.4,
    ),

    # ===== 6. 贵州茅台（A股，白酒）=====
    GoldenCase(
        query="分析贵州茅台2025年业绩",
        kind="company",
        is_public=True,
        note="A股消费龙头。2025年首次营收净利双降，重点验证不误判为'衰退'。",
        required_facts=[
            FactCheck("营业收入", ["1720", "1,720", "营收", "1720.54"], 1720.54, "亿元", 0.05,
                      "贵州茅台2025年报(2026-04-17)", "FY2025", "high"),
            FactCheck("净利润", ["823", "净利润", "823.20", "归母"], 823.20, "亿元", 0.06,
                      "贵州茅台2025年报", "FY2025", "high"),
            FactCheck("直销渠道占比过半", ["直销", "50", "i茅台", "渠道"], None, "", 0.12,
                      "贵州茅台2025年报(直销占比50.06%)", "FY2025", "high"),
            FactCheck("增长趋势-营收净利双降", ["下降", "下滑", "双降", "回落", "-1.2", "-4.5", "首次"], None, "",
                      0.12, "贵州茅台2025年报(营收-1.20%/净利-4.53%)", "FY2025", "high"),
        ],
        forbidden_facts=[
            "亏损", "净亏损",
            "营收大幅增长", "净利润高增长", "业绩高增",   # 2025是双降，不能说高增长
            "营收同比增长", "净利润同比增长",            # 实际是双降
        ],
        expected_directions=[
            {"name": "主动降速/渠道改革(直销超批发)", "keywords": ["主动", "降速", "渠道改革", "直销", "转型", "调整"], "must": True},
            {"name": "飞天批价下行/价格倒挂", "keywords": ["批价", "批发价", "倒挂", "价格下行", "1499", "破价"], "must": False},
            {"name": "双降非衰退(结构性调整)", "keywords": ["非衰退", "结构", "调整", "并非衰退", "战略"], "must": False},
            {"name": "系列酒拖累vs主品牌稳健", "keywords": ["系列酒", "茅台酒", "结构", "拖累", "分化"], "must": False},
        ],
        unexpected_directions=[
            {"name": "误判双降为衰退/崩盘", "keywords": ["衰退", "崩盘", "一蹶不振", "护城河消失"]},
            {"name": "无视双降谈高增长", "keywords": ["业绩高增长", "营收高速增长"]},
        ],
        min_direction_coverage=0.4,
    ),

    # ===== 7. 宁德时代（A股/港股，动力电池）=====
    GoldenCase(
        query="分析宁德时代2025年业绩",
        kind="company",
        is_public=True,
        note="A股+港股动力电池龙头。重点验证不把'海外市占30%'误读为'海外业务主导'。",
        required_facts=[
            FactCheck("营业收入", ["4237", "4,237", "营收", "4237.02"], 4237.02, "亿元", 0.05,
                      "宁德时代2025年报(2026-03-10)", "FY2025", "high"),
            FactCheck("净利润", ["722", "净利润", "722.01", "归母"], 722.01, "亿元", 0.06,
                      "宁德时代2025年报", "FY2025", "high"),
            FactCheck("动力电池全球市占第一", ["市占", "39", "第一", "全球", "份额"], None, "", 0.12,
                      "宁德时代2025年报(全球动力电池市占39.2%)", "FY2025", "high"),
            FactCheck("增长趋势-净利高增", ["净利润", "增长", "42", "增", "高增"], None, "",
                      0.12, "宁德时代2025年报(净利同比+42.28%)", "FY2025", "high"),
        ],
        forbidden_facts=[
            "亏损", "净亏损",
            "营收下降", "净利润下降", "业绩下滑",   # 2025是双增长
            "市占率跌出全球前列",                  # 仍是全球第一
        ],
        expected_directions=[
            {"name": "海外高毛利成利润引擎", "keywords": ["海外", "毛利", "利润引擎", "溢价", "出海", "国际"], "must": True},
            {"name": "储能增速放缓隐患", "keywords": ["储能", "增速放缓", "增长放缓", "8.99", "减弱"], "must": False},
            {"name": "国内价格战/毛利承压", "keywords": ["价格战", "国内", "毛利", "承压", "竞争"], "must": False},
            {"name": "技术路线(钠电/麒麟/神行)", "keywords": ["钠电", "麒麟", "神行", "技术路线", "迭代"], "must": False},
        ],
        unexpected_directions=[
            {"name": "把海外市占30%等同海外业务主导", "keywords": ["海外业务主导", "海外为主", "以海外为核心"]},
            {"name": "以储能低增速否定储能潜力", "keywords": ["储能失利", "储能没落", "储能失败"]},
            {"name": "把电池厂当整车厂分析", "keywords": ["按整车厂估值", "类似比亚迪的估值", "车企框架", "终端售价定价权"]},
        ],
        min_direction_coverage=0.4,
    ),

    # ===== 8. 中国助贷行业（行业级）=====
    GoldenCase(
        query="分析中国助贷行业",
        kind="industry",
        is_public=False,
        note="行业级分析。重点验证不把单一公司数据当行业数据。",
        required_facts=[
            FactCheck("行业增速放缓/存量竞争", ["增速放缓", "存量", "放缓", "见顶", "增速回落"], None, "", 0.12,
                      "央行/互金协会数据", "2025", "medium"),
            FactCheck("利率上限24%监管", ["24%", "利率上限", "定价上限", "监管"], None, "", 0.12,
                      "监管政策", "2025", "medium"),
        ],
        forbidden_facts=[
            "助贷行业整体亏损",
        ],
        expected_directions=[
            {"name": "增速放缓/存量竞争", "keywords": ["增速放缓", "存量", "见顶", "结构性", "杠杆率"], "must": True},
            {"name": "利率上限重构商业模式", "keywords": ["利率上限", "24%", "定价", "重构", "轻资本"], "must": False},
            {"name": "集中度提升/行业出清", "keywords": ["集中度", "CR5", "出清", "头部", "份额"], "must": False},
            {"name": "轻资本转型分化", "keywords": ["轻资本", "转型", "分化", "科技服务"], "must": False},
        ],
        unexpected_directions=[
            {"name": "把公司级数据当行业级数据", "keywords": ["奇富营收即行业", "某公司即代表行业", "以单一平台数据代表全行业"]},
        ],
        min_direction_coverage=0.4,
    ),

    # ===== 9. 中国新能源汽车行业（行业级）=====
    GoldenCase(
        query="分析中国新能源汽车行业",
        kind="industry",
        is_public=False,
        note="行业级分析。重点验证不把比亚迪数据当成整个行业数据。",
        required_facts=[
            FactCheck("行业销量", ["1649", "1600", "销量", "万辆"], 1649, "万辆", 0.08,
                      "中汽协(2025销量1649万辆)", "2025", "high"),
            FactCheck("渗透率", ["47.9", "渗透率", "47", "48%"], 47.9, "%", 0.10,
                      "中汽协(2025渗透率47.9%)", "2025", "high"),
            FactCheck("出口翻倍增长", ["出口", "翻倍", "103", "增长一倍", "261"], None, "", 0.12,
                      "中汽协(出口261.5万辆,+103.7%)", "2025", "high"),
        ],
        forbidden_facts=[
            "行业整体销量下降", "渗透率下降",   # 2025是增长
        ],
        expected_directions=[
            {"name": "出口成新增长极", "keywords": ["出口", "增长极", "翻倍", "海外市场", "出海"], "must": True},
            {"name": "企业分化加剧/集中度", "keywords": ["分化", "集中度", "出清", "TOP5", "头部", "比亚迪份额"], "must": False},
            {"name": "渗透率逼近天花板/新场景", "keywords": ["渗透率", "天花板", "商用车", "饱和", "新场景"], "must": False},
            {"name": "价格战缓解但盈利承压", "keywords": ["价格战", "盈利", "毛利", "承压", "缓解"], "must": False},
        ],
        unexpected_directions=[
            {"name": "把比亚迪数据当成整个行业", "keywords": ["比亚迪销量即行业", "比亚迪市占即行业", "以比亚迪代表行业"]},
            {"name": "渗透率高=无增长空间的误判", "keywords": ["行业无增长空间", "已经饱和无空间"]},
        ],
        min_direction_coverage=0.4,
    ),
]
