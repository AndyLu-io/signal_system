"""
V4.0 信号系统配置
静态参数：ETF池、政策评分、因子权重、机制阈值
"""

import os

# ─── 飞书 Webhook ────────────────────────────────────────────────────────────
# 优先从环境变量读取（推荐 .env 或 launchd plist 注入）；
# 未设置时退回历史硬编码值，保证 launchd 现网无需立刻改配置。
def _env_webhook(key: str, default: str) -> str:
    return os.environ.get(key, default)


def _env_webhooks(key: str, defaults: list[str]) -> list[str]:
    raw = os.environ.get(key)
    if raw:
        return [u.strip() for u in raw.split(",") if u.strip()]
    return defaults


FEISHU_WEBHOOK = _env_webhook(
    "FEISHU_WEBHOOK",
    "https://open.feishu.cn/open-apis/bot/v2/hook/9dafed62-6b03-42d8-a802-32906592db68",
)
FEISHU_WEBHOOKS = _env_webhooks(
    "FEISHU_WEBHOOKS",
    [
        FEISHU_WEBHOOK,
        "https://open.feishu.cn/open-apis/bot/v2/hook/a3523361-87bf-470c-821d-1c30cd2f6588",
    ],
)
# ETF尾盘/早盘/竞价择时专用推送列表（仅发送到专属频道）
FEISHU_TAIL_WEBHOOKS = _env_webhooks(
    "FEISHU_TAIL_WEBHOOKS",
    ["https://open.feishu.cn/open-apis/bot/v2/hook/d74bc3bd-2cc6-4126-ade2-4625dd7d4854"],
)
# 个股盘中择时（stock_timing.py）专用频道
FEISHU_STOCK_WEBHOOKS = _env_webhooks(
    "FEISHU_STOCK_WEBHOOKS",
    [
        "https://open.feishu.cn/open-apis/bot/v2/hook/077c6eb2-14ae-4736-8b9b-56d444082da6",
        "https://open.feishu.cn/open-apis/bot/v2/hook/d7bf66ce-e368-4718-a00e-753fc1f1f5dc",
    ],
)
# 宽基/海外指数择时（index_timing.py）专用频道
FEISHU_INDEX_WEBHOOK = _env_webhook(
    "FEISHU_INDEX_WEBHOOK",
    "https://open.feishu.cn/open-apis/bot/v2/hook/2a6bbc1c-91cd-4cb6-963e-7b965999ea89",
)


# ─── 节假日黑名单（统一来源） ─────────────────────────────────────────────────
# 所有 is_trading_day() 调用都从这里读，避免 daily_guidance / stock_timing 等
# 各处分别维护导致不一致。每年元旦前需更新。
HOLIDAY_BLACKLIST: set[str] = {
    "2026-01-01", "2026-01-02",
    "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",
    "2026-02-23", "2026-02-24",
    "2026-04-03", "2026-04-04", "2026-04-05", "2026-04-06",
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    "2026-06-19", "2026-06-20", "2026-06-21",
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
    "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
}

# ─── 账户基础参数 ─────────────────────────────────────────────────────────────
ACCOUNT_NET_VALUE = 300_000  # 账户净值（元），每月手动更新

# ─── ETF 宇宙 ─────────────────────────────────────────────────────────────────
# f_policy: 政策刚性评分(0-100)，每月人工审视更新
# f_earnings: 盈利修正评分(0-100)，每季度财报后更新
# pool: "core"=核心池, "candidate"=候选池, "watch"=观察池
# bucket: "core_alpha", "tactical", "defensive"
# cluster: 相关性集群，同集群合计仓位有上限
ETF_UNIVERSE = {
    # ── 电力/电网主线 ──────────────────────────────────────────────────────────
    "159326": {"name": "电网设备ETF",   "f_policy": 90, "f_earnings": 82, "pool": "core",      "bucket": "core_alpha", "cluster": "power_export", "max_weight": 0.15,
        "constituent_count": 80, "top5": "特变电工(10.94%), 思源电气(10.28%), 国电南瑞(8.53%), 亨通光电(8.49%), 中天科技(6.71%)"},
    "562960": {"name": "绿色电力ETF",   "f_policy": 85, "f_earnings": 76, "pool": "core",      "bucket": "core_alpha", "cluster": "power_export", "max_weight": 0.10,
        "constituent_count": 10, "top5": "中国核电(9.06%), 长江电力(9.04%), 三峡能源(7.41%), 国电电力(5.23%), 国投电力(4.14%)"},
    "159611": {"name": "电力ETF",       "f_policy": 80, "f_earnings": 76, "pool": "core",      "bucket": "core_alpha", "cluster": "power_export", "max_weight": 0.10,
        "constituent_count": 38, "top5": "长江电力(9.01%), 中国核电(7.92%), 三峡能源(6.47%), 国电电力(4.59%), 永泰能源(4.18%)"},
    "600900": {"name": "长江电力",      "f_policy": 78, "f_earnings": 82, "pool": "candidate", "bucket": "defensive",  "cluster": "power_export", "max_weight": 0.08,
        "fund_delta": -157,  "fund_pct": -22.0},

    # ── 卫星/机器人/先进制造 ──────────────────────────────────────────────────
    "159206": {"name": "卫星ETF",       "f_policy": 95, "f_earnings": 70, "pool": "core",      "bucket": "core_alpha", "cluster": "space_robot",  "max_weight": 0.12,
        "constituent_count": 51, "top5": "航天电子(9.11%), 中国卫星(7.84%), 睿创微纳(5.99%), 信维通信(4.53%), ST臻镭(3.77%)"},
    "562500": {"name": "机器人ETF",     "f_policy": 85, "f_earnings": 68, "pool": "core",      "bucket": "core_alpha", "cluster": "space_robot",  "max_weight": 0.12,
        "constituent_count": 66, "top5": "科大讯飞(9.96%), 汇川技术(9.59%), 大族激光(6.62%), 拓普集团(6.47%), 中控技术(5.35%)"},
    "159667": {"name": "工业母机ETF",   "f_policy": 75, "f_earnings": 65, "pool": "core",      "bucket": "core_alpha", "cluster": "space_robot",  "max_weight": 0.08,
        "constituent_count": 16, "top5": "华工科技(17.1%), 大族激光(8.82%), 中钨高新(7.52%), 厦门钨业(6.51%), 豪迈科技(4.63%)"},

    # ── AI算力/半导体 ─────────────────────────────────────────────────────────
    # Q1 2026季报更新：芯片链业绩大幅超预期，f_policy/f_earnings全面上调
    "002371": {"name": "北方华创",      "f_policy": 95, "f_earnings": 86, "pool": "core",      "bucket": "core_alpha", "cluster": "ai_compute",   "max_weight": 0.10,
        "fund_delta": -41,  "fund_pct": -7.8},
    "512480": {"name": "半导体ETF",     "f_policy": 90, "f_earnings": 85, "pool": "core",      "bucket": "core_alpha", "cluster": "ai_compute",   "max_weight": 0.10,
        "constituent_count": 27, "top5": "寒武纪(6.64%), 海光信息(6.24%), 北方华创(6.19%), 中芯国际(6.01%), 兆易创新(5.08%)"},
    "159995": {"name": "芯片ETF",       "f_policy": 88, "f_earnings": 85, "pool": "core",      "bucket": "core_alpha", "cluster": "ai_compute",   "max_weight": 0.10,
        "constituent_count": 22, "top5": "海光信息(10.09%), 北方华创(9.12%), 中芯国际(8.5%), 兆易创新(8.0%), 寒武纪(7.34%)"},
    "561980": {"name": "半导体设备ETF", "f_policy": 92, "f_earnings": 88, "pool": "core",      "bucket": "core_alpha", "cluster": "ai_compute",   "max_weight": 0.10,
        "constituent_count": 15, "top5": "中微公司(15.8%), 北方华创(13.79%), 拓荆科技(7.35%), 寒武纪(6.61%), 长川科技(6.33%)"},
    "515880": {"name": "通信ETF",       "f_policy": 78, "f_earnings": 80, "pool": "core",      "bucket": "core_alpha", "cluster": "ai_compute",   "max_weight": 0.08,
        "constituent_count": 36, "top5": "新易盛(15.0%), 中际旭创(13.66%), 工业富联(10.42%), 天孚通信(7.14%), 中兴通讯(5.35%)"},
    "588080": {"name": "科创50ETF",     "f_policy": 80, "f_earnings": 72, "pool": "candidate", "bucket": "tactical",   "cluster": "ai_compute",   "max_weight": 0.08,
        "constituent_count": 30, "top5": "寒武纪(9.37%), 海光信息(8.84%), 中芯国际(8.5%), 澜起科技(6.49%), 中微公司(6.07%)"},
    "159909": {"name": "TMT50ETF",      "f_policy": 78, "f_earnings": 72, "pool": "candidate", "bucket": "tactical",   "cluster": "ai_compute",   "max_weight": 0.08,
        "constituent_count": 15, "top5": "新易盛(6.62%), 中际旭创(6.02%), 北方华创(5.37%), 立讯精密(4.96%), 胜宏科技(4.77%)"},

    # ── 新能源（光伏/储能/电池） ──────────────────────────────────────────────
    "300274": {"name": "阳光电源",      "f_policy": 82, "f_earnings": 72, "pool": "core",      "bucket": "core_alpha", "cluster": "new_energy",   "max_weight": 0.10,
        "fund_delta": 383,  "fund_pct": 108.2},
    "516850": {"name": "新能源ETF",     "f_policy": 80, "f_earnings": 65, "pool": "candidate", "bucket": "tactical",   "cluster": "new_energy",   "max_weight": 0.08,
        "constituent_count": 10, "top5": "宁德时代(9.71%), 阳光电源(7.6%), 特变电工(4.67%), 隆基绿能(3.7%), 华友钴业(3.11%)"},
    "515790": {"name": "光伏ETF",       "f_policy": 80, "f_earnings": 62, "pool": "candidate", "bucket": "tactical",   "cluster": "new_energy",   "max_weight": 0.08,
        "constituent_count": 36, "top5": "特变电工(10.8%), 隆基绿能(8.55%), 阳光电源(7.51%), TCL科技(7.19%), 德业股份(3.86%)"},
    "561910": {"name": "电池ETF",       "f_policy": 80, "f_earnings": 65, "pool": "candidate", "bucket": "tactical",   "cluster": "new_energy",   "max_weight": 0.08,
        "constituent_count": 17, "top5": "宁德时代(10.22%), 阳光电源(8.3%), 三花智控(6.5%), 亿纬锂能(5.3%), 天赐材料(4.48%)"},

    # ── 防御/红利 ─────────────────────────────────────────────────────────────
    "515450": {"name": "红利低波50",    "f_policy": 80, "f_earnings": 80, "pool": "core",      "bucket": "defensive",  "cluster": "defensive",    "max_weight": 0.10,
        "constituent_count": 23, "top5": "格力电器(3.7%), 浙能电力(3.39%), 申能股份(3.36%), 雅戈尔(3.31%), 中国神华(3.3%)"},

    # ── 金融（银行/券商） ─────────────────────────────────────────────────────
    "515850": {"name": "证券ETF",       "f_policy": 65, "f_earnings": 60, "pool": "candidate", "bucket": "tactical",   "cluster": "finance",      "max_weight": 0.08,
        "constituent_count": 15, "top5": "东方财富(13.53%), 中信证券(13.28%), 国泰海通(10.62%), 华泰证券(5.89%), 招商证券(3.24%)"},
    "600036": {"name": "招商银行",      "f_policy": 70, "f_earnings": 78, "pool": "candidate", "bucket": "defensive",  "cluster": "finance",      "max_weight": 0.08,
        "fund_delta": -76,  "fund_pct": -8.8},
    "601398": {"name": "工商银行",      "f_policy": 62, "f_earnings": 72, "pool": "watch",     "bucket": "defensive",  "cluster": "finance",      "max_weight": 0.05,
        "fund_delta": -75,  "fund_pct": -18.2},

    # ── 宽基指数 ──────────────────────────────────────────────────────────────
    "510500": {"name": "中证500ETF",    "f_policy": 65, "f_earnings": 65, "pool": "candidate", "bucket": "tactical",   "cluster": "broad_market", "max_weight": 0.08,
        "constituent_count": 187, "top5": "亨通光电(1.05%), 赤峰黄金(0.73%), 佰维存储(0.69%), 金风科技(0.64%), 信维通信(0.63%)"},
    "510050": {"name": "上证50ETF",     "f_policy": 65, "f_earnings": 72, "pool": "candidate", "bucket": "tactical",   "cluster": "broad_market", "max_weight": 0.08,
        "constituent_count": 18, "top5": "贵州茅台(9.92%), 中国平安(6.63%), 紫金矿业(5.9%), 招商银行(5.33%), 长江电力(3.62%)"},
    "159915": {"name": "创业板ETF",     "f_policy": 68, "f_earnings": 67, "pool": "candidate", "bucket": "tactical",   "cluster": "broad_market", "max_weight": 0.08,
        "constituent_count": 67, "top5": "宁德时代(19.69%), 中际旭创(9.3%), 新易盛(7.53%), 东方财富(4.64%), 阳光电源(4.27%)"},

    # ── 港股/海外 ─────────────────────────────────────────────────────────────
    "159567": {"name": "港股创新药ETF", "f_policy": 72, "f_earnings": 52, "pool": "candidate", "bucket": "tactical",   "cluster": "overseas",     "max_weight": 0.06,
        "constituent_count": 10, "top5": "康方生物(11.86%), 信达生物(9.81%), 石药集团(9.4%), 百济神州(9.06%), 中国生物制药(9.02%)"},
    "513180": {"name": "恒生科技ETF",   "f_policy": 62, "f_earnings": 62, "pool": "candidate", "bucket": "tactical",   "cluster": "overseas",     "max_weight": 0.06,
        "constituent_count": 10, "top5": "比亚迪股份(9.43%), 腾讯控股(9.27%), 美团-W(9.04%), 阿里巴巴-W(8.44%), 小米集团-W(8.25%)"},
    "159941": {"name": "纳指ETF",       "f_policy": 52, "f_earnings": 75, "pool": "candidate", "bucket": "tactical",   "cluster": "overseas",     "max_weight": 0.06,
        "constituent_count": 11, "top5": "英伟达(7.88%), 苹果(7.04%), 微软(5.17%), 亚马逊(4.14%), 特斯拉(3.41%)"},

    # ── 大宗商品/周期 ─────────────────────────────────────────────────────────
    "159715": {"name": "稀土ETF",       "f_policy": 70, "f_earnings": 60, "pool": "candidate", "bucket": "tactical",   "cluster": "commodity",    "max_weight": 0.06,
        "constituent_count": 10, "top5": "北方稀土(14.26%), 金风科技(7.76%), 厦门钨业(7.05%), 中国稀土(4.92%), 格林美(4.92%)"},
    "512400": {"name": "有色金属ETF",   "f_policy": 65, "f_earnings": 62, "pool": "candidate", "bucket": "tactical",   "cluster": "commodity",    "max_weight": 0.06,
        "constituent_count": 46, "top5": "紫金矿业(9.44%), 洛阳钼业(6.8%), 北方稀土(5.48%), 中国铝业(4.1%), 华友钴业(4.05%)"},
    "518880": {"name": "黄金ETF",       "f_policy": 62, "f_earnings": 52, "pool": "candidate", "bucket": "defensive",  "cluster": "commodity",    "max_weight": 0.06},
    "515220": {"name": "煤炭ETF",       "f_policy": 45, "f_earnings": 58, "pool": "watch",     "bucket": "none",       "cluster": "commodity",    "max_weight": 0.05,
        "constituent_count": 35, "top5": "中国神华(10.07%), 兖矿能源(10.04%), 陕西煤业(10.02%), 中煤能源(9.13%), 山西焦煤(5.48%)"},
    "600989": {"name": "宝丰能源",      "f_policy": 50, "f_earnings": 62, "pool": "watch",     "bucket": "none",       "cluster": "commodity",    "max_weight": 0.05,
        "fund_delta": 69,  "fund_pct": 84.1},
    "159870": {"name": "化工ETF",       "f_policy": 45, "f_earnings": 50, "pool": "watch",     "bucket": "none",       "cluster": "commodity",    "max_weight": 0.05,
        "constituent_count": 51, "top5": "万华化学(9.98%), 盐湖股份(7.85%), 天赐材料(4.38%), 宝丰能源(4.28%), 藏格矿业(4.16%)"},

    # ── 农业/其他 ─────────────────────────────────────────────────────────────
    "159275": {"name": "农牧渔ETF",     "f_policy": 65, "f_earnings": 55, "pool": "watch",     "bucket": "none",       "cluster": "agriculture",  "max_weight": 0.05,
        "constituent_count": 15, "top5": "温氏股份(15.01%), 牧原股份(13.33%), 海大集团(7.68%), 正邦科技(4.17%), 梅花生物(4.16%)"},
}

# ─── 相关性集群仓位上限 ────────────────────────────────────────────────────────
CLUSTER_MAX_WEIGHT = {
    "power_export": 0.30,
    "ai_compute":   0.25,
    "space_robot":  0.20,
    "new_energy":   0.20,
    "finance":      0.15,
    "broad_market": 0.20,
    "overseas":     0.15,
    "commodity":    0.15,
    "defensive":    0.20,
    "agriculture":  0.08,
    "materials":    0.10,
}

# ─── 机制判别参数 ─────────────────────────────────────────────────────────────
REGIME_WEIGHTS = {
    "s1_trend":    0.25,  # 沪深300趋势强度
    "s2_volume":   0.20,  # 成交额动能
    "s3_north":    0.20,  # 北向5日净流向
    "s4_rotation": 0.15,  # 板块轮动速度
    "s5_margin":   0.10,  # 两融余额变化
    "s6_vol":      0.10,  # 波动率压力
}

REGIME_THRESHOLDS = {
    "R1": (70, 100),   # 趋势牛市
    "R2": (45, 69),    # 震荡市
    "R3": (30, 44),    # 板块轮动市
    "R4": (0,  29),    # 系统性风险市
}

# 机制切换：向下需连续2日，向上需连续3日，R4立即触发
REGIME_DOWN_CONFIRM = 2
REGIME_UP_CONFIRM   = 3

# ─── 机制条件化参数 ────────────────────────────────────────────────────────────
REGIME_PARAMS = {
    "R1": {
        "max_total_position":   0.90,
        "max_leverage_ratio":   0.30,
        "max_single_position":  0.15,
        "stop_loss_core":       0.08,
        "stop_loss_tactical":   0.06,
        "max_holding_days":     3,
        "min_defensive_ratio":  0.05,
        "daily_loss_rate":      0.015,
        "allowed_tiers":        ["S", "A", "B"],
    },
    "R2": {
        "max_total_position":   0.75,
        "max_leverage_ratio":   0.15,
        "max_single_position":  0.12,
        "stop_loss_core":       0.06,
        "stop_loss_tactical":   0.05,
        "max_holding_days":     2,
        "min_defensive_ratio":  0.10,
        "daily_loss_rate":      0.012,
        "allowed_tiers":        ["S", "A"],
    },
    "R3": {
        "max_total_position":   0.65,
        "max_leverage_ratio":   0.10,
        "max_single_position":  0.10,
        "stop_loss_core":       0.05,
        "stop_loss_tactical":   0.04,
        "max_holding_days":     1,
        "min_defensive_ratio":  0.15,
        "daily_loss_rate":      0.010,
        "allowed_tiers":        ["S", "A"],
    },
    "R4": {
        "max_total_position":   0.40,
        "max_leverage_ratio":   0.00,
        "max_single_position":  0.08,
        "stop_loss_core":       0.04,
        "stop_loss_tactical":   0.00,
        "max_holding_days":     0,
        "min_defensive_ratio":  0.35,
        "daily_loss_rate":      0.006,
        "allowed_tiers":        ["S"],
    },
}

# ─── 五因子权重（按机制） ──────────────────────────────────────────────────────
FACTOR_WEIGHTS_BY_REGIME = {
    "R1": {"f_policy": 0.30, "f_momentum": 0.30, "f_flow": 0.20, "f_liquidity": 0.10, "f_earnings": 0.10},
    "R2": {"f_policy": 0.35, "f_momentum": 0.20, "f_flow": 0.25, "f_liquidity": 0.10, "f_earnings": 0.10},
    "R3": {"f_policy": 0.25, "f_momentum": 0.35, "f_flow": 0.30, "f_liquidity": 0.10, "f_earnings": 0.00},
    "R4": {"f_policy": 0.40, "f_momentum": 0.00, "f_flow": 0.30, "f_liquidity": 0.20, "f_earnings": 0.10},
}

# ─── 信念等级映射 ─────────────────────────────────────────────────────────────
CONVICTION_TIERS = [
    {"tier": "S", "min_score": 85, "max_weight_factor": 1.00, "bucket": "core_alpha"},
    {"tier": "A", "min_score": 70, "max_weight_factor": 0.80, "bucket": "core_alpha"},
    {"tier": "B", "min_score": 55, "max_weight_factor": 0.50, "bucket": "tactical"},
    {"tier": "C", "min_score": 40, "max_weight_factor": 0.00, "bucket": None},
    {"tier": "D", "min_score":  0, "max_weight_factor": 0.00, "bucket": None},
]

# ─── 信号类型 ─────────────────────────────────────────────────────────────────
SIGNAL_BUY_STRONG  = "BUY_STRONG"   # S级，三因子全通
SIGNAL_BUY_WATCH   = "BUY_WATCH"    # A级，可择机建仓
SIGNAL_HOLD        = "HOLD"         # 维持持仓
SIGNAL_REDUCE      = "REDUCE"       # 条件性减仓
SIGNAL_SELL_STOP   = "SELL_STOP"    # 触止损，立即清
SIGNAL_AVOID       = "AVOID"        # 禁止配置

# 主要基准指数代码
INDEX_CSI300  = "000300"  # 沪深300
INDEX_CSI500  = "000905"  # 中证500

# ─── 个股研究池 ────────────────────────────────────────────────────────────────
# 来源：公募基金季报持仓分析 × 北向资金 × ETF成分三维交叉验证
# signal_3d : ★★★=三重共振 ★★☆=双维共振 ★☆☆=单维/分歧
# fund_delta : Q4 2024→Q4 2025 公募增持只数（年度全披露口径）
# fund_pct   : 对应增幅 %
# nb_delta   : Q4 2025→Q1 2026 北向持仓占流通股比变化（百分点）
# q1_yoy     : Q1 2025→Q1 2026 同口径同比变化（季报前十大口径）
# etf_overlap: 相关 ETF（可用于联动验证）
# discovered : 首次纳入分析的季度
STOCK_UNIVERSE = {
    # ── 光通信链条（三维信号最强主线）─────────────────────────────────────────
    "300308": {
        "name": "中际旭创",
        "theme": "光模块",
        "pool": "core",      "cluster": "optics",
        "f_policy": 78,      "f_earnings": 85,
        "signal_3d": "★★★",
        "fund_delta": -721,  "fund_pct": 816.3,
        "nb_delta": 1.414,   "q1_yoy": 1860,
        "etf_overlap": ["515880", "159909", "159915"],
        "discovered": "2026Q1",
        "note": "光模块龙头；Q1净利+262%营收+192%ROE=17.5%，公募季节性获利了结-721家，非基本面恶化",
    },
    "300502": {
        "name": "新易盛",
        "theme": "光模块",
        "pool": "core",      "cluster": "optics",
        "f_policy": 75,      "f_earnings": 70,
        "signal_3d": "★★★",
        "fund_delta": 1193,   "fund_pct": 410.0,
        "nb_delta": 1.835,   "q1_yoy": 1193,
        "etf_overlap": ["515880", "159909", "159915"],
        "discovered": "2026Q1",
        "note": "800G光模块，北向增持最高+1.84pp",
    },
    "300394": {
        "name": "天孚通信",
        "theme": "光通信器件",
        "pool": "core",      "cluster": "optics",
        "f_policy": 82,      "f_earnings": 70,
        "signal_3d": "★★☆",
        "fund_delta": 363,   "fund_pct": 526.1,
        "nb_delta": 3.559,   "q1_yoy": 432,
        "etf_overlap": ["515880", "159909", "159915"],
        "discovered": "2026Q1",
        "note": "北向Q1增持+3.56pp（链条内最高），无源光器件龙头",
    },
    "688498": {
        "name": "源杰科技",
        "theme": "光芯片基材",
        "pool": "candidate", "cluster": "optics",
        "f_policy": 80,      "f_earnings": 65,
        "signal_3d": "★★☆",
        "fund_delta": 343,     "fund_pct": 5716.7,
        "nb_delta": 4.713,   "q1_yoy": 349,
        "etf_overlap": [],
        "discovered": "2026Q2",
        "note": "Q2新变量；磷化铟衬底基片，北向Q1+4.71pp（新进最强）",
    },
    "002384": {
        "name": "东山精密",
        "theme": "PCB/光模块封装",
        "pool": "candidate", "cluster": "optics",
        "f_policy": 72,      "f_earnings": 68,
        "signal_3d": "★★☆",
        "fund_delta": 246,   "fund_pct": 139.8,
        "nb_delta": 2.026,   "q1_yoy": 245,
        "etf_overlap": ["159909"],
        "discovered": "2026Q1",
        "note": "光模块FPC封装，北向+2.03pp",
    },

    # ── 半导体设备/芯片 ──────────────────────────────────────────────────────
    "688012": {
        "name": "中微公司",
        "theme": "半导体设备",
        "pool": "core",      "cluster": "semicon",
        "f_policy": 92,      "f_earnings": 92,
        "signal_3d": "★★★",
        "fund_delta": 245,   "fund_pct": 102.9,
        "nb_delta": 1.707,   "q1_yoy": 245,
        "etf_overlap": ["512480", "159995", "561980", "588080"],
        "discovered": "2026Q1",
        "note": "刻蚀设备龙头；Q1净利同比+245%，北向+1.71pp，国产替代加速",
    },
    "603986": {
        "name": "兆易创新",
        "theme": "存储芯片",
        "pool": "candidate", "cluster": "semicon",
        "f_policy": 78,      "f_earnings": 80,
        "signal_3d": "★★☆",
        "fund_delta": 96,   "fund_pct": 36.4,
        "nb_delta": 0.006,   "q1_yoy": 95,
        "etf_overlap": ["512480", "159995"],
        "discovered": "2026Q1",
        "note": "NOR Flash/MCU；Q1净利同比+95%，公募强增，北向持平",
    },
    "688256": {
        "name": "寒武纪",
        "theme": "AI推理芯片",
        "pool": "candidate", "cluster": "semicon",
        "f_policy": 90,      "f_earnings": 78,
        "signal_3d": "★☆☆",
        "fund_delta": 276,   "fund_pct": 72.1,
        "nb_delta": -0.580,  "q1_yoy": 276,
        "etf_overlap": ["512480", "159995", "561980", "588080"],
        "discovered": "2026Q1",
        "note": "AI推理芯片；Q1营收同比+276%，公募大增，北向减持-0.58pp；回测T+5超额+31.68%，上调f_earnings",
    },

    # ── 新能源（个股，与ETF池形成联动验证）────────────────────────────────────
    "300274": {
        "name": "阳光电源",
        "theme": "逆变器",
        "pool": "core",      "cluster": "new_energy",
        "f_policy": 82,      "f_earnings": 72,
        "signal_3d": "★★★",
        "fund_delta": 690,   "fund_pct": 53.8,
        "nb_delta": 0.544,   "q1_yoy": 381,
        "etf_overlap": ["516850", "515790", "561910"],
        "discovered": "2026Q1",
        "note": "逆变器出货全球第一，三维共振；已在ETF_UNIVERSE，此处作个股跟踪",
    },

    # ── 大宗资源/周期 ─────────────────────────────────────────────────────────
    "601899": {
        "name": "紫金矿业",
        "theme": "铜金矿",
        "pool": "watch",     "cluster": "commodity",
        "f_policy": 65,      "f_earnings": 72,
        "signal_3d": "★☆☆",
        "fund_delta": 280,   "fund_pct": 23.5,
        "nb_delta": -0.404,  "q1_yoy": 280,
        "etf_overlap": ["510050", "512400"],
        "discovered": "2026Q1",
        "note": "公募大增但北向在减仓；铜价高位，外资获利回吐",
    },

    # ── 工程机械 ─────────────────────────────────────────────────────────────
    "600031": {
        "name": "三一重工",
        "theme": "工程机械",
        "pool": "watch",     "cluster": "machinery",
        "f_policy": 68,      "f_earnings": 65,
        "signal_3d": "★★☆",
        "fund_delta": -53,   "fund_pct": -22.7,
        "nb_delta": 1.478,   "q1_yoy": -53,
        "etf_overlap": ["510050"],
        "discovered": "2026Q1",
        "note": "北向持仓最重13.9%+Q1增持，但Q1同比基金数-53只；关税压出口链",
    },

    # ── 医药/CXO（信号分歧，存档备查）────────────────────────────────────────
    "603259": {
        "name": "药明康德",
        "theme": "CXO",
        "pool": "watch",     "cluster": "pharma",  # 已是watch★☆☆，回测超额-3.72%
        "f_policy": 40,      "f_earnings": 60,  # f_policy 55→40，CXO行业承压
        "signal_3d": "★☆☆",
        "fund_delta": 298,   "fund_pct": 78.8,
        "nb_delta": -0.911,  "q1_yoy": 298,
        "etf_overlap": ["510050"],
        "discovered": "2026Q1",
        "note": "北向减持-0.91pp（链条内最大分歧）；监管风险压制外资",
    },
    "600276": {
        "name": "恒瑞医药",
        "theme": "创新药",
        "pool": "watch",     "cluster": "pharma",
        "f_policy": 55,      "f_earnings": 50,
        "signal_3d": "★☆☆",
        "fund_delta": -50,   "fund_pct": -9.4,
        "nb_delta": -0.346,  "q1_yoy": -50,
        "etf_overlap": ["510050"],
        "discovered": "2026Q1",
        "note": "Q1同比-50只，公募和北向双减信号，已降级观察",
    },

    # ── 公募基金Top30信赖标的（双期持续入选×季报全披露口径）────────────────────
    # fund_delta: Q1-2025→Q1-2026 同口径公募基金数变化（季报前十大口径，≈真实资金流向）
    # nb_delta: 北向资金占流通股比变化（百分点，0.0=本期未单独拉取，待补充）
    # q1_yoy: Q1 2025→Q1 2026 同比（季报前十大口径，0=未单独核验）

    # ── 动力电池 ─────────────────────────────────────────────────────────────
    "300750": {
        "name": "宁德时代",
        "theme": "动力电池",
        "pool": "core",      "cluster": "battery",
        "f_policy": 85,      "f_earnings": 78,
        "signal_3d": "★★★",
        "fund_delta": 477,   "fund_pct": 25.6,
        "nb_delta": -0.512,  "q1_yoy": 520,
        "etf_overlap": ["516850", "561910", "159915"],
        "discovered": "2026Q2",
        "note": "动力电池全球第一；公募持有基金数最多，北向小幅减仓但绝对持仓仍最重",
    },

    # ── 消费电子供应链 ────────────────────────────────────────────────────────
    "002475": {
        "name": "立讯精密",
        "theme": "消费电子精密制造",
        "pool": "core",      "cluster": "consumer_elec",
        "f_policy": 72,      "f_earnings": 78,
        "signal_3d": "★★★",
        "fund_delta": -295,   "fund_pct": -29.9,
        "nb_delta": 0.892,   "q1_yoy": 445,
        "etf_overlap": ["159909"],
        "discovered": "2026Q2",
        "note": "苹果供应链核心；公募双期稳增，北向Q1显著回流",
    },
    "601138": {
        "name": "工业富联",
        "theme": "AI服务器/精密制造",
        "pool": "candidate", "cluster": "consumer_elec",
        "f_policy": 72,      "f_earnings": 78,
        "signal_3d": "★★☆",
        "fund_delta": 282,   "fund_pct": 486.2,
        "nb_delta": 0.0,     "q1_yoy": 0,
        "etf_overlap": ["515880"],
        "discovered": "2026Q2",
        "note": "AI服务器出货受益，富士康工业核心；公募双期持续",
    },

    # ── 大消费（品牌消费品） ──────────────────────────────────────────────────
    "000333": {
        "name": "美的集团",
        "theme": "家电/工业机器人",
        "pool": "core",      "cluster": "consumer",
        "f_policy": 75,      "f_earnings": 78,
        "signal_3d": "★★★",
        "fund_delta": -417,   "fund_pct": -31.3,
        "nb_delta": 0.648,   "q1_yoy": 478,
        "etf_overlap": ["510050"],
        "discovered": "2026Q2",
        "note": "白电+工业机器人双主线；公募双期稳增，北向回流",
    },
    "600519": {
        "name": "贵州茅台",
        "theme": "高端白酒",
        "pool": "candidate",  "cluster": "consumer",  # 回测超额-6.68%，core→candidate
        "f_policy": 58,      "f_earnings": 75,  # f_policy 72→58，回测avg T+1=-3.71%
        "signal_3d": "★★☆",  # ★★★→★★☆，白酒下行期单维共振不足
        "fund_delta": 158,   "fund_pct": 13.2,
        "nb_delta": 1.205,   "q1_yoy": 420,
        "etf_overlap": ["510050"],
        "discovered": "2026Q2",
        "note": "白酒龙头/消费核心资产；公募和北向双双增持",
    },
    "000858": {
        "name": "五粮液",
        "theme": "高端白酒",
        "pool": "candidate", "cluster": "consumer",
        "f_policy": 65,      "f_earnings": 70,
        "signal_3d": "★★☆",
        "fund_delta": -330,   "fund_pct": -59.9,
        "nb_delta": 0.0,     "q1_yoy": 0,
        "etf_overlap": ["515450"],
        "discovered": "2026Q2",
        "note": "白酒第二梯队龙头；公募双期稳增",
    },
    "600887": {
        "name": "伊利股份",
        "theme": "乳制品/食品",
        "pool": "candidate", "cluster": "consumer",
        "f_policy": 72,      "f_earnings": 75,
        "signal_3d": "★★☆",
        "fund_delta": -88,   "fund_pct": -27.9,
        "nb_delta": 0.0,     "q1_yoy": 0,
        "etf_overlap": [],
        "discovered": "2026Q2",
        "note": "乳品双寡头之一；公募双期持续入选，消费升级受益",
    },
    "601888": {
        "name": "中国中免",
        "theme": "免税零售",
        "pool": "candidate", "cluster": "consumer",
        "f_policy": 78,      "f_earnings": 65,
        "signal_3d": "★★☆",
        "fund_delta": 64,   "fund_pct": 133.3,
        "nb_delta": 0.0,     "q1_yoy": 0,
        "etf_overlap": [],
        "discovered": "2026Q2",
        "note": "免税专营牌照稀缺；公募双期持续，免税政策扩张预期",
    },
    "000651": {
        "name": "格力电器",
        "theme": "家电",
        "pool": "candidate", "cluster": "consumer",
        "f_policy": 68,      "f_earnings": 78,
        "signal_3d": "★★☆",
        "fund_delta": -346,   "fund_pct": -57.5,
        "nb_delta": 0.0,     "q1_yoy": 0,
        "etf_overlap": ["515450"],
        "discovered": "2026Q2",
        "note": "空调龙头，高分红；公募双期持续，估值低位",
    },

    # ── 半导体（扩展） ────────────────────────────────────────────────────────
    "688041": {
        "name": "海光信息",
        "theme": "国产CPU/GPU",
        "pool": "core",      "cluster": "semicon",
        "f_policy": 92,      "f_earnings": 92,
        "signal_3d": "★★★",
        "fund_delta": 158,   "fund_pct": 34.1,
        "nb_delta": 1.124,   "q1_yoy": 580,
        "etf_overlap": ["512480", "159995", "561980", "588080"],
        "discovered": "2026Q2",
        "note": "国产替代GPU/CPU核心；Q1净利同比+580%（链条最高），自主可控政策加持",
    },
    "688981": {
        "name": "中芯国际",
        "theme": "半导体代工",
        "pool": "core",      "cluster": "semicon",
        "f_policy": 95,      "f_earnings": 88,
        "signal_3d": "★★★",
        "fund_delta": -147,   "fund_pct": -29.3,
        "nb_delta": 0.958,   "q1_yoy": 495,
        "etf_overlap": ["512480", "159995", "561980", "588080"],
        "discovered": "2026Q2",
        "note": "中国最先进晶圆代工；Q1净利同比+495%，自主可控战略核心，北向回流",
    },

    # ── PCB供应链（AI数据中心驱动） ──────────────────────────────────────────
    "600183": {
        "name": "生益科技",
        "theme": "PCB基板/覆铜板",
        "pool": "core",      "cluster": "pcb",
        "f_policy": 75,      "f_earnings": 72,
        "signal_3d": "★★★",
        "fund_delta": 60,   "fund_pct": 61.9,
        "nb_delta": 2.156,   "q1_yoy": 480,
        "etf_overlap": [],
        "discovered": "2026Q2",
        "note": "AI服务器高速覆铜板龙头；公募+北向双双大幅增持",
    },
    "002463": {
        "name": "沪电股份",
        "theme": "高多层PCB",
        "pool": "core",      "cluster": "pcb",
        "f_policy": 75,      "f_earnings": 70,
        "signal_3d": "★★★",
        "fund_delta": 185,   "fund_pct": 171.3,
        "nb_delta": 1.782,   "q1_yoy": 420,
        "etf_overlap": ["159909"],
        "discovered": "2026Q2",
        "note": "AI数据中心高多层PCB龙头；公募+北向三维共振",
    },
    "300476": {
        "name": "胜宏科技",
        "theme": "PCB",
        "pool": "candidate", "cluster": "pcb",
        "f_policy": 70,      "f_earnings": 68,
        "signal_3d": "★★☆",
        "fund_delta": -17,   "fund_pct": -7.0,
        "nb_delta": 0.0,     "q1_yoy": 0,
        "etf_overlap": ["159909", "159915"],
        "discovered": "2026Q2",
        "note": "中小盘PCB成长股；公募双期持续入选，北向待分析",
    },

    # ── 化工（特种化学品） ────────────────────────────────────────────────────
    "600309": {
        "name": "万华化学",
        "theme": "MDI/聚氨酯",
        "pool": "candidate",  "cluster": "chemical",  # 回测超额-3.90%，core→candidate
        "f_policy": 55,      "f_earnings": 80,  # f_policy 70→55，回测买入T+5全亏
        "signal_3d": "★★★",
        "fund_delta": 151,   "fund_pct": 61.1,
        "nb_delta": 0.524,   "q1_yoy": 408,
        "etf_overlap": ["159870"],
        "discovered": "2026Q2",
        "note": "MDI全球第一，聚氨酯产业链龙头；公募双期稳增，盈利稳健",
    },
    "600426": {
        "name": "华鲁恒升",
        "theme": "煤化工/化肥",
        "pool": "candidate", "cluster": "chemical",
        "f_policy": 65,      "f_earnings": 72,
        "signal_3d": "★★☆",
        "fund_delta": 167,   "fund_pct": 149.1,
        "nb_delta": 0.0,     "q1_yoy": 0,
        "etf_overlap": ["159870"],
        "discovered": "2026Q2",
        "note": "煤化工低成本优势明显；公募双期稳增，高分红特征",
    },

    # ── 电池材料 ─────────────────────────────────────────────────────────────
    "603799": {
        "name": "华友钴业",
        "theme": "钴/三元前驱体",
        "pool": "candidate", "cluster": "battery_materials",
        "f_policy": 72,      "f_earnings": 65,
        "signal_3d": "★★☆",
        "fund_delta": 100,   "fund_pct": 88.5,
        "nb_delta": 0.0,     "q1_yoy": 0,
        "etf_overlap": ["516850", "512400"],
        "discovered": "2026Q2",
        "note": "钴锂产业链核心，前驱体一体化；公募双期持续入选",
    },

    # ── 大宗商品（扩展） ─────────────────────────────────────────────────────
    "603993": {
        "name": "洛阳钼业",
        "theme": "铜/钴矿",
        "pool": "core",      "cluster": "commodity",
        "f_policy": 68,      "f_earnings": 75,
        "signal_3d": "★★★",
        "fund_delta": 58,   "fund_pct": 36.2,
        "nb_delta": 1.312,   "q1_yoy": 465,
        "etf_overlap": ["510050", "512400"],
        "discovered": "2026Q2",
        "note": "铜钴矿全球布局；公募和北向双增，铜价高位受益",
    },
    "601088": {
        "name": "中国神华",
        "theme": "煤炭/电力",
        "pool": "watch",     "cluster": "commodity",  # 已是watch，回测超额-8.67%
        "f_policy": 45,      "f_earnings": 80,  # f_policy 60→45，煤炭周期下行
        "signal_3d": "★★☆",
        "fund_delta": 32,   "fund_pct": 12.2,
        "nb_delta": 0.0,     "q1_yoy": 0,
        "etf_overlap": ["515450", "515220"],
        "discovered": "2026Q2",
        "note": "央企高股息防御标的；公募双期稳增，能源安全战略加持",
    },

    # ── 新能源整车/制造（扩展） ──────────────────────────────────────────────
    "002594": {
        "name": "比亚迪",
        "theme": "电动车/刀片电池",
        "pool": "candidate",  "cluster": "new_energy",  # 回测超额-7.12%，core→candidate
        "f_policy": 72,      "f_earnings": 82,  # f_policy 88→72，电动车竞争加剧
        "signal_3d": "★★★",
        "fund_delta": -651,   "fund_pct": -64.1,
        "nb_delta": 0.748,   "q1_yoy": 620,
        "etf_overlap": ["516850", "561910"],
        "discovered": "2026Q2",
        "note": "EV+电池+ADAS一体化龙头；公募大幅增持，北向Q1回流",
    },
    "600438": {
        "name": "通威股份",
        "theme": "光伏电池/硅料",
        "pool": "candidate", "cluster": "new_energy",
        "f_policy": 80,      "f_earnings": 58,
        "signal_3d": "★★☆",
        "fund_delta": -41,   "fund_pct": -48.8,
        "nb_delta": 0.0,     "q1_yoy": 0,
        "etf_overlap": ["515790"],
        "discovered": "2026Q2",
        "note": "多晶硅+电池片双龙头；底部持续布局，政策催化预期",
    },
    "601012": {
        "name": "隆基绿能",
        "theme": "光伏组件",
        "pool": "watch",     "cluster": "new_energy",
        "f_policy": 65,      "f_earnings": 42,
        "signal_3d": "★☆☆",
        "fund_delta": -13,   "fund_pct": -10.2,
        "nb_delta": 0.0,     "q1_yoy": 0,
        "etf_overlap": ["516850", "515790", "510050"],
        "discovered": "2026Q2",
        "note": "光伏组件全球第一，行业产能过剩压利润；等待右侧信号",
    },

    # ── 电力设备 ─────────────────────────────────────────────────────────────
    "002028": {
        "name": "思源电气",
        "theme": "特高压/变电设备",
        "pool": "watch",      "cluster": "power_equip",  # 回测超额-5.77%，降为watch
        "f_policy": 55,      "f_earnings": 65,  # f_policy 75→55，回测avg T+1=-7.96%
        "signal_3d": "★★☆",  # ★★★→★★☆，单维共振不足
        "fund_delta": 296,   "fund_pct": 271.6,
        "nb_delta": 1.568,   "q1_yoy": 412,
        "etf_overlap": ["159326"],
        "discovered": "2026Q2",
        "note": "特高压变电设备核心供应商；电网投资加速受益，公募+北向双共振",
    },

    # ── 工程机械（扩展） ─────────────────────────────────────────────────────
    "000338": {
        "name": "潍柴动力",
        "theme": "重型发动机/氢燃料",
        "pool": "candidate", "cluster": "machinery",
        "f_policy": 75,      "f_earnings": 78,
        "signal_3d": "★★☆",
        "fund_delta": 208,   "fund_pct": 171.9,
        "nb_delta": 0.0,     "q1_yoy": 0,
        "etf_overlap": [],
        "discovered": "2026Q2",
        "note": "柴油机+氢燃料电池双轮驱动；出口亮眼，公募双期稳增",
    },

    # ── 农业养殖 ─────────────────────────────────────────────────────────────
    "002714": {
        "name": "牧原股份",
        "theme": "生猪养殖",
        "pool": "candidate", "cluster": "agriculture",
        "f_policy": 70,      "f_earnings": 72,
        "signal_3d": "★★☆",
        "fund_delta": 121,   "fund_pct": 97.6,
        "nb_delta": 0.0,     "q1_yoy": 0,
        "etf_overlap": ["159275"],
        "discovered": "2026Q2",
        "note": "生猪养殖成本最低龙头；猪周期上行，公募双期稳增",
    },

    # ── 金融（扩展） ─────────────────────────────────────────────────────────
    "600030": {
        "name": "中信证券",
        "theme": "综合券商",
        "pool": "candidate", "cluster": "finance",
        "f_policy": 65,      "f_earnings": 70,
        "signal_3d": "★★☆",
        "fund_delta": -7,   "fund_pct": -3.0,
        "nb_delta": 0.0,     "q1_yoy": 0,
        "etf_overlap": ["515850", "510050"],
        "discovered": "2026Q2",
        "note": "券商龙头，资本市场政策受益；行情启动时弹性大",
    },
    "601318": {
        "name": "中国平安",
        "theme": "保险/金融科技",
        "pool": "candidate", "cluster": "finance",
        "f_policy": 70,      "f_earnings": 72,
        "signal_3d": "★★☆",
        "fund_delta": 453,   "fund_pct": 76.0,
        "nb_delta": 0.0,     "q1_yoy": 0,
        "etf_overlap": ["510050"],
        "discovered": "2026Q2",
        "note": "保险龙头，高股息+科技转型；公募双期持续入选",
    },

    # ── 医疗器械（扩展） ─────────────────────────────────────────────────────
    "300760": {
        "name": "迈瑞医疗",
        "theme": "医疗器械",
        "pool": "candidate", "cluster": "pharma",
        "f_policy": 75,      "f_earnings": 78,
        "signal_3d": "★★☆",
        "fund_delta": -151,   "fund_pct": -47.9,
        "nb_delta": 0.0,     "q1_yoy": 0,
        "etf_overlap": ["159915"],
        "discovered": "2026Q2",
        "note": "医疗器械进口替代龙头；出海+国内配置双受益，公募双期稳增",
    },

    # ── 食品饮料 ─────────────────────────────────────────────────────────────
    "000568": {
        "name": "泸州老窖",
        "theme": "高端白酒",
        "pool": "candidate", "cluster": "food_bev",
        "f_policy": 62,      "f_earnings": 68,
        "signal_3d": "★★☆",
        "fund_delta": -127,   "fund_pct": -51.6,
        "nb_delta": 0.0,     "q1_yoy": 0,
        "etf_overlap": [],
        "discovered": "2026Q2",
        "note": "国窖1573，高端白酒第三极；公募双期稳增",
    },
    "603288": {
        "name": "海天味业",
        "theme": "调味品",
        "pool": "watch",     "cluster": "food_bev",
        "f_policy": 68,      "f_earnings": 70,
        "signal_3d": "★☆☆",
        "fund_delta": 61,   "fund_pct": 129.8,
        "nb_delta": 0.0,     "q1_yoy": 0,
        "etf_overlap": [],
        "discovered": "2026Q2",
        "note": "调味品龙头，防御属性；公募双期持续但增速放缓",
    },

    # ── 光通信第二梯队（Top300 Q1 2026 增量最强方向）──────────────────────────
    # fund_delta: Q1-2025→Q1-2026 同口径增减数；fund_pct: 同口径增幅%(%)
    "601869": {
        "name": "长飞光纤",
        "theme": "光纤光缆",
        "pool": "core",      "cluster": "optics",
        "f_policy": 85,      "f_earnings": 80,
        "signal_3d": "★★★",
        "fund_delta": -76,   "fund_pct": 1637.5,
        "nb_delta": 0.0,     "q1_yoy": 139,
        "etf_overlap": ["159206"],
        "discovered": "2026Q2",
        "note": "光纤光缆全球前三；Q1净利+226%营收+28%，筹码最稳（-76家），Top200唯一A类标的",
    },
    "600522": {
        "name": "中天科技",
        "theme": "海缆/光纤/电力电缆",
        "pool": "core",      "cluster": "optics",
        "f_policy": 85,      "f_earnings": 72,
        "signal_3d": "★★★",
        "fund_delta": 12,   "fund_pct": 9.0,
        "nb_delta": 0.0,     "q1_yoy": 145,
        "etf_overlap": ["159326", "515880"],
        "discovered": "2026Q2",
        "note": "海缆+光纤+电力电缆三线；Q1季度环比+66%、持仓增量+53亿，AI算力+能源双主线",
    },
    "600498": {
        "name": "烽火通信",
        "theme": "光通信设备",
        "pool": "candidate", "cluster": "optics",
        "f_policy": 82,      "f_earnings": 68,
        "signal_3d": "★★☆",
        "fund_delta": 94,   "fund_pct": 376.0,
        "nb_delta": 0.0,     "q1_yoy": 119,
        "etf_overlap": ["515880"],
        "discovered": "2026Q2",
        "note": "烽火+武汉邮科国家队背景，光通信设备主力；Q1季度环比+57%、持仓增量+22亿",
    },
    "688048": {
        "name": "长光华芯",
        "theme": "半导体激光芯片",
        "pool": "candidate", "cluster": "optics",
        "f_policy": 88,      "f_earnings": 65,
        "signal_3d": "★★☆",
        "fund_delta": 65,    "fund_pct": 0,
        "nb_delta": 0.0,     "q1_yoy": 65,
        "etf_overlap": [],
        "discovered": "2026Q2",
        "note": "半导体激光芯片国产龙头（光模块最上游芯片）；Q1季度环比+64%、持仓增量+15亿",
    },
    "688195": {
        "name": "腾景科技",
        "theme": "精密光学元件",
        "pool": "candidate", "cluster": "optics",
        "f_policy": 80,      "f_earnings": 65,
        "signal_3d": "★★☆",
        "fund_delta": 84,    "fund_pct": 0,
        "nb_delta": 0.0,     "q1_yoy": 84,
        "etf_overlap": [],
        "discovered": "2026Q2",
        "note": "精密光学元件+激光光路核心器件，AI光通信/激光传感双受益；Q1季度环比+60%、持仓增量+10亿",
    },

    # ── 半导体材料/设备（国产替代第二梯队，Q1 Top300 新入）──────────────────
    "688120": {
        "name": "华海清科",
        "theme": "CMP研磨设备",
        "pool": "candidate", "cluster": "semicon",
        "f_policy": 92,      "f_earnings": 68,
        "signal_3d": "★★☆",
        "fund_delta": 23,   "fund_pct": 24.0,
        "nb_delta": 0.0,     "q1_yoy": 119,
        "etf_overlap": ["561980"],
        "discovered": "2026Q2",
        "note": "CMP化学机械研磨设备国内唯一量产厂商；公募流通占比20.6%（高集中度），国产替代卡脖子",
    },
    "300666": {
        "name": "江丰电子",
        "theme": "高纯溅射靶材",
        "pool": "candidate", "cluster": "semicon",
        "f_policy": 88,      "f_earnings": 70,
        "signal_3d": "★★☆",
        "fund_delta": 56,    "fund_pct": 155.6,
        "nb_delta": 0.0,     "q1_yoy": 92,
        "etf_overlap": [],
        "discovered": "2026Q2",
        "note": "半导体溅射靶材国内最高纯度龙头，关键卡脖子材料；Q1季度环比+51%、持仓增量+17亿",
    },

    # ── 工业自动化（Q1 Top300 新发现，与AI制造业升级共振）──────────────────
    "300757": {
        "name": "罗博特科",
        "theme": "光伏/新能源自动化装备",
        "pool": "candidate", "cluster": "industrial_auto",
        "f_policy": 82,      "f_earnings": 68,
        "signal_3d": "★★☆",
        "fund_delta": 77,    "fund_pct": 513.3,
        "nb_delta": 0.0,     "q1_yoy": 92,
        "etf_overlap": ["515790"],
        "discovered": "2026Q2",
        "note": "光伏&新能源自动化装备龙头，出海拓展；Q1季度环比+63%、持仓增量+18亿",
    },
    "688777": {
        "name": "中控技术",
        "theme": "工业自动化DCS",
        "pool": "candidate", "cluster": "industrial_auto",
        "f_policy": 85,      "f_earnings": 78,
        "signal_3d": "★★☆",
        "fund_delta": 39,    "fund_pct": 78.0,
        "nb_delta": 0.0,     "q1_yoy": 89,
        "etf_overlap": ["562500", "588080"],
        "discovered": "2026Q2",
        "note": "化工/能源行业DCS控制系统龙头，工业互联网+自主可控双逻辑；Q1季度环比+31%、持仓增量+17亿",
    },

    # ── 公募×ETF联动新发现（2026Q2）────────────────────────────────────────────
    # B区：公募环比>15% + ETF成分双向加仓；C区：ETF盲区纯Alpha

    # 亨通光电：B区最强信号，公募#35 + 电网设备ETF+通信ETF+中证500，三重共振
    "600487": {
        "name": "亨通光电",
        "theme": "光纤光缆/海底电缆",
        "pool": "core",      "cluster": "optics",
        "f_policy": 88,      "f_earnings": 75,
        "signal_3d": "★★★",
        "fund_delta": 202,   "fund_pct": 178.8,
        "nb_delta": 0.0,     "q1_yoy": 315,
        "etf_overlap": ["159326", "515880", "510500"],
        "discovered": "2026Q2",
        "note": "光纤光缆+海底电缆龙头；公募#35+ETF三重覆盖，环比+113%、增量+147亿，联动最强信号",
    },
    # 芯原股份：公募#25 + 半导体ETF+芯片ETF+科创50三重ETF，半导体IP"中国ARM"
    "688521": {
        "name": "芯原股份",
        "theme": "半导体IP/芯片设计平台",
        "pool": "candidate", "cluster": "semicon",
        "f_policy": 88,      "f_earnings": 62,
        "signal_3d": "★★☆",
        "fund_delta": 191,   "fund_pct": 104.9,
        "nb_delta": 0.0,     "q1_yoy": 373,
        "etf_overlap": ["512480", "159995", "588080"],
        "discovered": "2026Q2",
        "note": "芯片IP授权平台，被动+主动同向加仓；公募#25+三只ETF覆盖，增量+49亿",
    },
    # 德业股份：公募#44 + 光伏ETF+电池ETF，户用储能逆变器出口弹性
    "605117": {
        "name": "德业股份",
        "theme": "储能逆变器/户用储能",
        "pool": "candidate", "cluster": "new_energy",
        "f_policy": 82,      "f_earnings": 72,
        "signal_3d": "★★☆",
        "fund_delta": 109,   "fund_pct": 63.0,
        "nb_delta": 0.0,     "q1_yoy": 281,
        "etf_overlap": ["515790", "561910"],
        "discovered": "2026Q2",
        "note": "户用储能逆变器出口龙头；公募+光伏ETF+电池ETF双向买入，环比+53%、增量+26亿",
    },
    # 大族激光：公募#100 + 机器人ETF+工业母机ETF，激光龙头
    "002008": {
        "name": "大族激光",
        "theme": "工业激光设备",
        "pool": "candidate", "cluster": "industrial_auto",
        "f_policy": 82,      "f_earnings": 72,
        "signal_3d": "★★☆",
        "fund_delta": 129,   "fund_pct": 477.8,
        "nb_delta": 0.0,     "q1_yoy": 156,
        "etf_overlap": ["562500", "159667"],
        "discovered": "2026Q2",
        "note": "工业激光设备全球龙头；公募+机器人ETF+工业母机ETF三向共振，环比+49%、增量+22亿",
    },
    # 荣昌生物：C区ETF盲区纯Alpha，ADC抗体偶联药物RC48国际授权
    "688331": {
        "name": "荣昌生物",
        "theme": "ADC创新药",
        "pool": "candidate", "cluster": "pharma",
        "f_policy": 62,      "f_earnings": 50,
        "signal_3d": "★★☆",
        "fund_delta": 154,   "fund_pct": 570.4,
        "nb_delta": 0.0,     "q1_yoy": 180,
        "etf_overlap": [],
        "discovered": "2026Q2",
        "note": "ADC抗体偶联药RC48已获美国加速审评；C区纯Alpha，ETF买不到，增量+14亿，公募独家布局",
    },
    # 招商轮船：C区ETF盲区纯Alpha，VLCC油轮龙头，环比+82%增量+31亿最大纯Alpha
    "601872": {
        "name": "招商轮船",
        "theme": "VLCC油轮/干散货",
        "pool": "candidate", "cluster": "shipping",
        "f_policy": 65,      "f_earnings": 72,
        "signal_3d": "★★☆",
        "fund_delta": 111,   "fund_pct": 170.8,
        "nb_delta": 0.0,     "q1_yoy": 176,
        "etf_overlap": [],
        "discovered": "2026Q2",
        "note": "VLCC油轮最大运营商；C区ETF盲区，公募独家布局，增量+31亿为C区最大绝对增量",
    },
    # 赤峰黄金：公募#42 + 有色金属ETF+中证500，避险+黄金涨价弹性
    "600988": {
        "name": "赤峰黄金",
        "theme": "黄金矿采选",
        "pool": "candidate", "cluster": "commodity",
        "f_policy": 65,      "f_earnings": 75,
        "signal_3d": "★★☆",
        "fund_delta": 54,   "fund_pct": 23.1,
        "nb_delta": 0.0,     "q1_yoy": 288,
        "etf_overlap": ["510500", "512400"],
        "discovered": "2026Q2",
        "note": "中国黄金矿产第二大上市公司；公募+有色金属ETF双向，环比+38%、增量+17亿",
    },
    # 广钢气体：C区ETF盲区，半导体特种气体国产替代，环比+53%真实增量+4亿
    "688548": {
        "name": "广钢气体",
        "theme": "半导体特种气体",
        "pool": "watch",     "cluster": "semicon",
        "f_policy": 68,      "f_earnings": 48,
        "signal_3d": "★☆☆",
        "fund_delta": 40,    "fund_pct": 105.3,
        "nb_delta": 0.0,     "q1_yoy": 78,
        "etf_overlap": [],
        "discovered": "2026Q2",
        "note": "半导体特种气体（NF3/WF6）国产替代；C区纯Alpha，ETF无覆盖，环比+53%、增量+4亿",
    },
    # 炬光科技：C区ETF盲区，高功率半导体激光器，环比+86%增量+5.2亿
    "688167": {
        "name": "炬光科技",
        "theme": "高功率半导体激光器",
        "pool": "watch",     "cluster": "optics",
        "f_policy": 90,      "f_earnings": 72,
        "signal_3d": "★☆☆",
        "fund_delta": 62,    "fund_pct": 1033.3,
        "nb_delta": 0.0,     "q1_yoy": 68,
        "etf_overlap": [],
        "discovered": "2026Q2",
        "note": "高功率半导体激光器国产龙头，应用于光通信/泵浦源/激光雷达；C区纯Alpha，增量+5.2亿",
    },
    # 永鼎股份：C区ETF盲区，光纤光缆+新能源电缆，+7.5亿稳健增量
    "600105": {
        "name": "永鼎股份",
        "theme": "光纤光缆/新能源电缆",
        "pool": "watch",     "cluster": "optics",
        "f_policy": 80,      "f_earnings": 65,
        "signal_3d": "★☆☆",
        "fund_delta": 88,    "fund_pct": 2933.3,
        "nb_delta": 0.0,     "q1_yoy": 91,
        "etf_overlap": [],
        "discovered": "2026Q2",
        "note": "光纤光缆+新能源特种电缆；C区纯Alpha，ETF无覆盖，增量+7.5亿，光通信链条补充",
    },

    # ── Top500 Q1 新发现（301-500区间，watch级别，基金数35-55，需验证）────────
    "688409": {
        "name": "富创精密",
        "theme": "半导体精密零部件",
        "pool": "watch",     "cluster": "semicon",
        "f_policy": 90,      "f_earnings": 65,
        "signal_3d": "★☆☆",
        "fund_delta": 24,    "fund_pct": 133.3,
        "nb_delta": 0.0,     "q1_yoy": 41,
        "etf_overlap": ["512480"],
        "discovered": "2026Q2",
        "note": "CVD/ALD设备腔体/管路精密零件国产替代；流通占比13.5%，Q1增量+6.2亿，Top500真实增量",
    },
    "603929": {
        "name": "亚翔集成",
        "theme": "半导体洁净室工程",
        "pool": "watch",     "cluster": "semicon",
        "f_policy": 88,      "f_earnings": 62,
        "signal_3d": "★☆☆",
        "fund_delta": 47,    "fund_pct": 1566.7,
        "nb_delta": 0.0,     "q1_yoy": 49,
        "etf_overlap": [],
        "discovered": "2026Q2",
        "note": "FAB洁净室系统集成工程商，半导体建厂直接受益；Q1真实增量+1.2亿，环比+88%",
    },
    "603256": {
        "name": "宏和科技",
        "theme": "高端电子玻纤布",
        "pool": "watch",     "cluster": "pcb",
        "f_policy": 88,      "f_earnings": 80,
        "signal_3d": "★☆☆",
        "fund_delta": 37,    "fund_pct": 3700.0,
        "nb_delta": 0.0,     "q1_yoy": 38,
        "etf_overlap": [],
        "discovered": "2026Q2",
        "note": "高端电子级玻纤布龙头，AI服务器高速PCB/半导体基板原材料；Q1增量+4.4亿",
    },
    "002865": {
        "name": "钧达股份",
        "theme": "TOPCon光伏电池片",
        "pool": "watch",     "cluster": "new_energy",
        "f_policy": 80,      "f_earnings": 55,
        "signal_3d": "★☆☆",
        "fund_delta": 36,    "fund_pct": 200.0,
        "nb_delta": 0.0,     "q1_yoy": 54,
        "etf_overlap": ["515790"],
        "discovered": "2026Q2",
        "note": "专注TOPCon电池片的纯电池厂，301-500中增量最大+9亿；光伏底部布局，等待行业拐点",
    },
    "301200": {
        "name": "大族数控",
        "theme": "激光/数控切割设备",
        "pool": "watch",     "cluster": "industrial_auto",
        "f_policy": 80,      "f_earnings": 65,
        "signal_3d": "★☆☆",
        "fund_delta": 49,    "fund_pct": 2450.0,
        "nb_delta": 0.0,     "q1_yoy": 51,
        "etf_overlap": [],
        "discovered": "2026Q2",
        "note": "大族激光拆分子公司，激光+数控精密切割设备，工业自动化受益；Q1增量+1.4亿",
    },

    # ── 军工电子（Q1 Top300 稳健增量，低调建仓信号）──────────────────────────
    "600879": {
        "name": "航天电子",
        "theme": "军工电子/惯导",
        "pool": "candidate", "cluster": "defense",
        "f_policy": 90,      "f_earnings": 72,
        "signal_3d": "★★☆",
        "fund_delta": 94,   "fund_pct": 313.3,
        "nb_delta": 0.0,     "q1_yoy": 124,
        "etf_overlap": ["159206", "510500"],
        "discovered": "2026Q2",
        "note": "航天科技集团旗下，惯导/控制/通信军用电子核心；Q1持仓增量+16亿，低调稳健建仓",
    },
    "600893": {
        "name": "航发动力",
        "theme": "航空发动机",
        "pool": "candidate", "cluster": "defense",
        "f_policy": 95,      "f_earnings": 70,
        "signal_3d": "★★☆",
        "fund_delta": 75,   "fund_pct": 111.9,
        "nb_delta": 0.0,     "q1_yoy": 142,
        "etf_overlap": [],
        "discovered": "2026Q2",
        "note": "航空发动机国内唯一量产主机厂，国产大飞机/战机核心；Q1季度环比+20%，战略稀缺",
    },
    # ── 2026Q2 季报研究新增 ─────────────────────────────────────────────────────
    "688111": {
        "name": "金山办公",
        "theme": "AI办公软件",
        "pool": "candidate", "cluster": "software",
        "f_policy": 82,      "f_earnings": 85,
        "signal_3d": "★★☆",
        "fund_delta": -913,  "fund_pct": 180.0,
        "nb_delta": 0.0,     "q1_yoy": 180,
        "etf_overlap": [],
        "discovered": "2026Q2",
        "note": "WPS AI商业化落地；Q1净利+445%营收+24%ROE=15.7%，公募减持系高位兑现，非基本面恶化",
    },
    "688072": {
        "name": "拓荆科技",
        "theme": "半导体薄膜设备",
        "pool": "candidate", "cluster": "semicon",
        "f_policy": 88,      "f_earnings": 82,
        "signal_3d": "★★☆",
        "fund_delta": -651,  "fund_pct": 373.0,
        "nb_delta": 0.0,     "q1_yoy": 373,
        "etf_overlap": ["159995"],
        "discovered": "2026Q2",
        "note": "CVD/ALD薄膜设备国产替代核心；Q1净利+488%营收+57%，公募高位减持，国产设备景气持续",
    },
    "688002": {
        "name": "睿创微纳",
        "theme": "红外热成像/军工电子",
        "pool": "candidate", "cluster": "defense",
        "f_policy": 88,      "f_earnings": 80,
        "signal_3d": "★★☆",
        "fund_delta": -663,  "fund_pct": 208.0,
        "nb_delta": 0.0,     "q1_yoy": 208,
        "etf_overlap": ["512660"],
        "discovered": "2026Q2",
        "note": "红外热成像芯片及探测器龙头；Q1净利+228%营收+71%ROE=6.8%，军工+消费双线驱动",
    },
}

# ─── 个股集群仓位上限（单独管理，不与ETF集群合并计算）─────────────────────────
STOCK_CLUSTER_MAX_WEIGHT = {
    # ── 原始研究池集群 ──────────────────────────────────────────────────────
    "optics":           0.20,   # 光通信链（中际旭创+新易盛+天孚+东山+源杰）
    "semicon":          0.18,   # 半导体设备/芯片（中微+兆易+寒武纪+海光+中芯）
    "new_energy":       0.15,   # 新能源（阳光电源+比亚迪+通威+隆基）
    "commodity":        0.12,   # 大宗商品（紫金+洛阳钼业+神华）
    "machinery":        0.10,   # 工程机械（三一+潍柴）
    "pharma":           0.10,   # 医药（药明+恒瑞+迈瑞）
    # ── Top30信赖股新增集群 ──────────────────────────────────────────────────
    "battery":          0.15,   # 动力电池（宁德时代）
    "consumer_elec":    0.12,   # 消费电子供应链（立讯+工业富联）
    "consumer":         0.18,   # 大消费（美的+茅台+五粮液+伊利+中免+格力）
    "pcb":              0.12,   # PCB供应链（生益+沪电+胜宏）
    "chemical":         0.10,   # 化工（万华+华鲁恒升）
    "battery_materials": 0.08,  # 电池材料（华友钴业）
    "power_equip":      0.12,   # 电力设备（思源电气）
    "agriculture":      0.08,   # 农业（牧原股份）
    "finance":          0.12,   # 金融（中信证券+中国平安）
    "food_bev":         0.08,   # 食品饮料（泸州老窖+海天味业）
    "industrial_auto":  0.10,   # 工业自动化（罗博特科+中控技术）
    "defense":          0.12,   # 军工电子（航天电子+航发动力+睿创微纳）
    "shipping":         0.08,   # 航运（招商轮船）
    "software":         0.08,   # 软件（金山办公）
}

# ─── 攻守切换 · 防御轮动池 ────────────────────────────────────────────────────
# 情绪过热（HOT / OVERHEATED）时推荐切换至此池
# defense_rank: 1=极防御（货币/公用事业），2=高股息蓝筹，3=消费/医药防御
DEFENSIVE_ROTATION_POOL = {
    "511880": {"name": "银华日利ETF",   "defense_rank": 1, "note": "货币基金ETF，日度申赎，近零风险"},
    "515450": {"name": "红利低波50ETF", "defense_rank": 1, "note": "高股息+低波动，防御首选"},
    "600900": {"name": "长江电力",      "defense_rank": 1, "note": "公用事业+高分红，稳定防御"},
    "601088": {"name": "中国神华",      "defense_rank": 2, "note": "央企高股息，煤炭+电力双轮"},
    "601398": {"name": "工商银行",      "defense_rank": 2, "note": "国有大行，高股息低波动"},
    "600919": {"name": "江苏银行",      "defense_rank": 2, "note": "城商行高股息，估值合理"},
    "600036": {"name": "招商银行",      "defense_rank": 2, "note": "银行龙头，高ROE防御"},
    "600941": {"name": "中国移动",      "defense_rank": 2, "note": "5G运营商，高股息稳定"},
    "600519": {"name": "贵州茅台",      "defense_rank": 3, "note": "白酒核心资产，抗跌性强"},
    "512690": {"name": "酒ETF鹏华",    "defense_rank": 3, "note": "白酒ETF，消费蓝筹防御"},
    "600276": {"name": "恒瑞医药",      "defense_rank": 3, "note": "医药龙头，估值回落后的防御"},
}
