#!/usr/bin/env python3
"""
产业链补涨标的挖掘模块

核心逻辑：
  1. 龙头疲劳检测：主线龙头连续上涨后动力衰减（RSI高位+红柱缩短+成交萎缩）
  2. 链内涨幅差计算：同一产业链内标的按近20日涨幅排序，找出显著滞涨品种
  3. 补涨评分：基本面底线 + 技术启动信号 + 资金异动 + 估值洼地
  4. 推送飞书卡片

典型案例：
  光模块(中际旭创/新易盛)涨幅耗尽 → MLCC(风华高科)/PCB(生益科技)/光芯片(源杰)补涨
  半导体设备(北方华创)高位 → 半导体材料(江丰电子/广钢气体)补涨

用法:
    python3 signal_system/chain_catchup.py          # 正常运行+推送
    python3 signal_system/chain_catchup.py --dry    # 只打印不推送
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd

_DIR = Path(__file__).parent
sys.path.insert(0, str(_DIR))

from config import STOCK_UNIVERSE, ETF_UNIVERSE
from data_fetcher import market_prefix as _market_prefix
from feishu_pusher import post_card as _post_card
from utils import is_trading_day as _utils_is_trading_day

# ─────────────────────────────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────────────────────────────
FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/2335ea51-ea2b-4050-8ac0-cd18f7e66dbb"

# 产业链图谱：核心链 → {龙头标的, 补涨候选池}
# 定义哪些链有"龙头→补涨"的传导关系
CHAIN_MAP: dict[str, dict] = {
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # AI 产业链全景图（英伟达GB200/NVL72机柜为核心，从上到下完整覆盖）
    #
    # 层级结构：
    #   L1 AI芯片/GPU → L2 光互联 → L3 铜缆短距连接
    #        ↓                              ↓
    #   L4 PCB/基板 → L5 被动元器件 → L6 服务器电源
    #        ↓                              ↓
    #   L7 散热/液冷 → L8 服务器整机 → L9 交换机/网络
    #        ↓
    #   L10 AI应用/推理部署
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    # ── L1: AI芯片/GPU（国产算力） ───────────────────────────────────────────
    # 逻辑：海光/中微/北方华创是国产替代主线龙头，涨幅最大最先见顶
    #       补涨路径 → 二线设备/IP/EDA/材料
    "L1_AI芯片设备": {
        "leaders": ["688012", "002371", "688041"],  # 中微公司、北方华创、海光信息
        "catchup_pool": [
            "688256",  # 寒武纪 — AI推理芯片
            "688521",  # 芯原股份 — 半导体IP平台("中国ARM")
            "688072",  # 拓荆科技 — CVD/ALD薄膜沉积设备
            "688120",  # 华海清科 — CMP研磨设备(国内唯一)
            "300666",  # 江丰电子 — 高纯溅射靶材
            "688409",  # 富创精密 — 半导体精密零部件
            "603929",  # 亚翔集成 — 半导体洁净室工程
            "688548",  # 广钢气体 — 半导体特种气体(NF3/WF6)
            "603986",  # 兆易创新 — 存储芯片(NOR/MCU)
            "300782",  # 卓胜微 — 射频前端芯片
        ],
        "proxy_etf": "512480",  # 半导体ETF
    },

    # ── L2: 光模块/光互联（AI算力核心带宽瓶颈）──────────────────────────────
    # 逻辑：800G/1.6T光模块是GB200机柜的带宽瓶颈，涨幅最大
    #       补涨路径 → 光芯片/光器件/光纤光缆
    "L2_光模块光通信": {
        "leaders": ["300308", "300502", "300394"],  # 中际旭创、新易盛、天孚通信
        "catchup_pool": [
            "000988",  # 华工科技 — 激光+光模块(华工正源)
            "688498",  # 源杰科技 — 磷化铟光芯片衬底
            "002384",  # 东山精密 — 光模块FPC封装
            "601869",  # 长飞光纤 — 光纤光缆全球前三
            "600522",  # 中天科技 — 海缆+光纤+电力电缆
            "600487",  # 亨通光电 — 光纤光缆+海底电缆
            "688048",  # 长光华芯 — 半导体激光芯片(光模块最上游)
            "688195",  # 腾景科技 — 精密光学元件
            "600105",  # 永鼎股份 — 光纤光缆+超导
            "600498",  # 烽火通信 — 光通信设备(国家队)
            "688167",  # 炬光科技 — 高功率半导体激光器
        ],
        "proxy_etf": "515880",  # 通信ETF
    },

    # ── L3: 铜缆高速连接（AEC/ACC，机柜内短距互联）─────────────────────────
    # 逻辑：NVL72机柜内GPU间用铜缆(AEC)而非光纤，成本低延迟小
    #       光模块涨完 → 铜缆连接是下一个爆发点
    "L3_铜缆高速连接": {
        "leaders": ["300308", "300502"],  # 光模块龙头代理整体算力景气
        "catchup_pool": [
            "002130",  # 沃尔核材 — 高速铜缆线束(安费诺供应商)
            "300548",  # 博创科技 — 高速光电连接器/AOC
            "300252",  # 金信诺 — 射频连接器/高速线缆
            "002475",  # 立讯精密 — 精密连接器(苹果+服务器)
            "601138",  # 工业富联 — AI服务器连接器/整机
            "002916",  # 深南电路 — 高速连接器基板
        ],
        "proxy_etf": "515880",
    },

    # ── L4: PCB/高速基板（AI服务器核心载体）─────────────────────────────────
    # 逻辑：GPU卡的16层+PCB、交换机背板PCB需求暴增
    #       生益/沪电涨完 → 二线PCB/基板材料补涨
    "L4_PCB高速基板": {
        "leaders": ["600183", "002463"],  # 生益科技(覆铜板)、沪电股份(高多层PCB)
        "catchup_pool": [
            "300476",  # 胜宏科技 — 服务器PCB
            "603256",  # 宏和科技 — 高端电子玻纤布(PCB原材料)
            "002916",  # 深南电路 — IC载板+高多层PCB(华为链)
            "002436",  # 兴森科技 — IC载板+样板快件
            "300749",  # 顶固集创 — PCB钻头(微型工具)
        ],
        "proxy_etf": "515880",
    },

    # ── L5: 被动元器件/MLCC（AI服务器用量10x提升）─────────────────────────
    # 逻辑：单张GPU卡MLCC用量~1000颗，NVL72机柜=72卡=7.2万颗MLCC
    #       光模块/芯片确认出货 → 被动元器件确定性补涨
    "L5_MLCC被动元器件": {
        "leaders": ["300308", "300502", "300394"],  # 光通信龙头(需求确认源)
        "catchup_pool": [
            "000636",  # 风华高科 — 国产MLCC龙头
            "300408",  # 三环集团 — MLCC陶瓷基板/封装基座
            "002138",  # 顺络电子 — 片式电感(VRM模块核心)
            "002859",  # 洁美科技 — 电子元器件载带
            "600563",  # 法拉电子 — 薄膜电容(电源滤波)
            "603989",  # 艾华集团 — 铝电解电容
            "603738",  # 泰晶科技 — 石英晶振(时钟信号)
            "603290",  # 斯达半导 — IGBT功率半导体(电源模块)
        ],
        "proxy_etf": "515880",
    },

    # ── L6: AI服务器电源（GPU功耗暴增→电源价值量翻倍）───────────────────────
    # 逻辑：B200单卡1000W，NVL72机柜总功耗~120kW，电源从$500→$2000+
    #       算力确认 → 电源是最确定的"卖铲子"环节
    "L6_服务器电源": {
        "leaders": ["300308", "688012"],  # 光模块+芯片龙头代理景气
        "catchup_pool": [
            "002851",  # 麦格米特 — AI服务器电源(台达对标)
            "002364",  # 中恒电气 — 数据中心电源/储能
            "300870",  # 欧陆通 — 服务器电源(国内份额最高)
            "603063",  # 禾望电气 — 大功率电源模块
            "688677",  # 海泰新光 — 电源管理芯片
        ],
        "proxy_etf": "515880",
    },

    # ── L7: 散热/液冷（GPU功耗→散热方案从风冷到液冷，价值量10x）────────────
    # 逻辑：H100风冷→B200/GB200必须液冷，单柜散热方案从$3k→$30k
    "L7_散热液冷": {
        "leaders": ["300308", "300502"],  # 光模块龙头代理AI算力景气
        "catchup_pool": [
            "002837",  # 英维克 — 精密温控/数据中心液冷系统
            "300602",  # 飞荣达 — 散热模组/导热界面材料/EMI
            "300684",  # 中石科技 — 导热硅脂/石墨散热
            "301487",  # 高澜股份 — 液冷温控设备(IGBT+数据中心)
            "831834",  # 曙光数创 — AI算力液冷(中科曙光子公司)
            "301018",  # 申菱环境 — 数据中心精密空调/液冷
            "603912",  # 佳力图 — 数据中心精密温控
            "002530",  # 丰东股份 — 热处理设备(散热零件加工)
        ],
        "proxy_etf": "515880",
    },

    # ── L8: 服务器整机/ODM（算力基础设施集成商）──────────────────────────────
    # 逻辑：芯片/光模块都要装进整机，ODM厂商是确定性受益方
    "L8_服务器整机": {
        "leaders": ["300308", "688041"],  # 光模块+海光(国产算力)
        "catchup_pool": [
            "601138",  # 工业富联 — 全球最大AI服务器ODM
            "000977",  # 浪潮信息 — AI服务器(国内份额第一)
            "603019",  # 中科曙光 — 国产算力整机+液冷
            "000938",  # 紫光股份 — 服务器+交换机(新华三)
        ],
        "proxy_etf": "515880",
    },

    # ── L9: 交换机/网络设备（AI集群内部互联）─────────────────────────────────
    # 逻辑：NVL72需要高性能InfiniBand/以太网交换机，国产替代空间大
    "L9_交换机网络": {
        "leaders": ["300308", "300502"],  # 光模块涨=交换机需求确认
        "catchup_pool": [
            "301165",  # 锐捷网络 — 数据中心交换机(CPO方案)
            "000063",  # 中兴通讯 — 5G+数据中心交换机
            "688702",  # 盛科通信 — 交换芯片(国产替代博通)
            "002396",  # 星网锐捷 — 网络设备(锐捷母公司)
            "300502",  # (新易盛也做CPO光引擎，已在龙头)
        ],
        "proxy_etf": "515880",
    },

    # ── L10: AI应用/推理部署（算力建设完→应用爆发）─────────────────────────
    # 逻辑：基础设施建完后，应用层是最后一波但弹性最大
    "L10_AI应用推理": {
        "leaders": ["688041", "688256"],  # 海光信息+寒武纪(算力芯片确认)
        "catchup_pool": [
            "688111",  # 金山办公 — WPS AI(大模型应用标杆)
            "002230",  # 科大讯飞 — 星火大模型/AI教育
            "300229",  # 拓尔思 — 大模型+政务AI
            "688327",  # 云从科技 — AI视觉/大模型
            "301236",  # 软通动力 — 华为生态AI服务
        ],
        "proxy_etf": "515880",
    },
}

# 补涨池中不在STOCK_UNIVERSE的标的基本面信息（补涨评分需要）
CATCHUP_EXTRA_INFO: dict[str, dict] = {
    # ── L1 AI芯片补充 ──
    "300782": {"name": "卓胜微", "f_policy": 70, "f_earnings": 68, "signal_3d": "★☆☆",
               "note": "射频前端芯片，5G+AI终端受益"},
    # ── L3 铜缆连接 ──
    "002130": {"name": "沃尔核材", "f_policy": 78, "f_earnings": 72, "signal_3d": "★★☆",
               "note": "高速铜缆线束，安费诺/豪利士供应商，AEC核心受益"},
    "300548": {"name": "博创科技", "f_policy": 75, "f_earnings": 68, "signal_3d": "★★☆",
               "note": "高速光电连接器/AOC有源光缆，CPO方向"},
    "300252": {"name": "金信诺", "f_policy": 72, "f_earnings": 65, "signal_3d": "★☆☆",
               "note": "射频连接器+高速线缆，军工+AI双线"},
    # ── L4 PCB补充 ──
    "002916": {"name": "深南电路", "f_policy": 82, "f_earnings": 78, "signal_3d": "★★☆",
               "note": "IC载板+高多层PCB，华为/服务器核心供应商"},
    "002436": {"name": "兴森科技", "f_policy": 72, "f_earnings": 68, "signal_3d": "★☆☆",
               "note": "IC载板+PCB样板快件，AI芯片封装配套"},
    "300749": {"name": "顶固集创", "f_policy": 65, "f_earnings": 60, "signal_3d": "★☆☆",
               "note": "PCB微钻/精密工具，PCB产量增→钻头耗材受益"},
    # ── L5 MLCC/被动元器件 ──
    "000636": {"name": "风华高科", "f_policy": 78, "f_earnings": 72, "signal_3d": "★★☆",
               "note": "国产MLCC龙头，AI服务器单卡MLCC用量10x"},
    "300408": {"name": "三环集团", "f_policy": 75, "f_earnings": 78, "signal_3d": "★★☆",
               "note": "MLCC陶瓷基板+电子陶瓷封装基座"},
    "002138": {"name": "顺络电子", "f_policy": 72, "f_earnings": 70, "signal_3d": "★★☆",
               "note": "片式电感龙头，服务器VRM模块核心器件"},
    "002859": {"name": "洁美科技", "f_policy": 70, "f_earnings": 65, "signal_3d": "★☆☆",
               "note": "电子元器件载带，MLCC/芯片封装物流配套"},
    "600563": {"name": "法拉电子", "f_policy": 72, "f_earnings": 75, "signal_3d": "★★☆",
               "note": "薄膜电容龙头，新能源+服务器电源双受益"},
    "603989": {"name": "艾华集团", "f_policy": 68, "f_earnings": 68, "signal_3d": "★☆☆",
               "note": "铝电解电容，服务器电源模块用量大"},
    "603738": {"name": "泰晶科技", "f_policy": 70, "f_earnings": 65, "signal_3d": "★☆☆",
               "note": "石英晶振，服务器/通信基站时钟信号"},
    "603290": {"name": "斯达半导", "f_policy": 78, "f_earnings": 75, "signal_3d": "★★☆",
               "note": "IGBT功率半导体，服务器电源+新能源"},
    # ── L6 服务器电源 ──
    "002851": {"name": "麦格米特", "f_policy": 80, "f_earnings": 78, "signal_3d": "★★☆",
               "note": "AI服务器电源(对标台达)，AIDC业务爆发"},
    "002364": {"name": "中恒电气", "f_policy": 75, "f_earnings": 70, "signal_3d": "★★☆",
               "note": "数据中心电源+储能，IDC供电核心"},
    "300870": {"name": "欧陆通", "f_policy": 78, "f_earnings": 75, "signal_3d": "★★☆",
               "note": "服务器电源国内份额最高，AI算力直接受益"},
    "603063": {"name": "禾望电气", "f_policy": 72, "f_earnings": 68, "signal_3d": "★☆☆",
               "note": "大功率电源模块/变频器，数据中心供电"},
    "688677": {"name": "海泰新光", "f_policy": 70, "f_earnings": 65, "signal_3d": "★☆☆",
               "note": "医疗光学+电源管理"},
    # ── L7 散热/液冷 ──
    "002837": {"name": "英维克", "f_policy": 82, "f_earnings": 75, "signal_3d": "★★☆",
               "note": "精密温控/数据中心液冷系统，AI算力散热核心"},
    "300602": {"name": "飞荣达", "f_policy": 80, "f_earnings": 72, "signal_3d": "★★☆",
               "note": "散热模组+EMI屏蔽，GPU液冷方案供应商"},
    "300684": {"name": "中石科技", "f_policy": 78, "f_earnings": 70, "signal_3d": "★★☆",
               "note": "导热界面材料/石墨散热片，AI服务器热管理"},
    "301487": {"name": "高澜股份", "f_policy": 80, "f_earnings": 72, "signal_3d": "★★☆",
               "note": "液冷温控设备(IGBT+数据中心)，AI液冷核心标的"},
    "831834": {"name": "曙光数创", "f_policy": 82, "f_earnings": 70, "signal_3d": "★★☆",
               "note": "AI算力液冷(中科曙光子公司)，国产算力液冷第一"},
    "301018": {"name": "申菱环境", "f_policy": 78, "f_earnings": 68, "signal_3d": "★★☆",
               "note": "数据中心精密空调/液冷，IDC温控龙头"},
    "603912": {"name": "佳力图", "f_policy": 72, "f_earnings": 65, "signal_3d": "★☆☆",
               "note": "数据中心精密温控/微模块"},
    "002530": {"name": "丰东股份", "f_policy": 65, "f_earnings": 60, "signal_3d": "★☆☆",
               "note": "热处理设备，散热零部件加工"},
    # ── L8 服务器整机 ──
    "000977": {"name": "浪潮信息", "f_policy": 82, "f_earnings": 78, "signal_3d": "★★☆",
               "note": "AI服务器国内份额第一，国产算力整机核心"},
    "603019": {"name": "中科曙光", "f_policy": 85, "f_earnings": 78, "signal_3d": "★★☆",
               "note": "国产算力整机(海光生态)+液冷方案"},
    "000938": {"name": "紫光股份", "f_policy": 78, "f_earnings": 72, "signal_3d": "★★☆",
               "note": "新华三(服务器+交换机)，数据中心全栈"},
    # ── L9 交换机/网络 ──
    "301165": {"name": "锐捷网络", "f_policy": 78, "f_earnings": 72, "signal_3d": "★★☆",
               "note": "数据中心交换机，CPO光引擎方案先行者"},
    "000063": {"name": "中兴通讯", "f_policy": 80, "f_earnings": 75, "signal_3d": "★★☆",
               "note": "5G+数据中心交换机+光模块，全栈通信"},
    "688702": {"name": "盛科通信", "f_policy": 82, "f_earnings": 65, "signal_3d": "★★☆",
               "note": "交换芯片(国产替代博通)，AI网络核心"},
    "002396": {"name": "星网锐捷", "f_policy": 72, "f_earnings": 68, "signal_3d": "★☆☆",
               "note": "网络设备(锐捷母公司)，IDC网络配套"},
    # ── L10 AI应用 ──
    "002230": {"name": "科大讯飞", "f_policy": 82, "f_earnings": 68, "signal_3d": "★★☆",
               "note": "星火大模型/AI教育，国产AI应用标杆"},
    "300229": {"name": "拓尔思", "f_policy": 72, "f_earnings": 62, "signal_3d": "★☆☆",
               "note": "大模型+政务AI，NLP龙头"},
    "688327": {"name": "云从科技", "f_policy": 75, "f_earnings": 55, "signal_3d": "★☆☆",
               "note": "AI视觉/大模型，行业AI解决方案"},
    "301236": {"name": "软通动力", "f_policy": 72, "f_earnings": 65, "signal_3d": "★☆☆",
               "note": "华为生态AI服务，IT外包+AI赋能"},
}
LEADER_FATIGUE_RSI = 65          # RSI高于此值
LEADER_FATIGUE_GAIN_20D = 15.0   # 近20日涨幅超过此值(%)
LEADER_FATIGUE_VOL_DECAY = 0.8   # 量比低于此值（缩量）

# 补涨候选评分参数
CATCHUP_MIN_SCORE = 60           # 最低入选分（满分100）
# 分层阈值（20日涨幅）
CATCHUP_TIER_READY = 8.0        # ≤8%: 待启动（最佳买点）
CATCHUP_TIER_ACCEL = 25.0       # 8-25%: 已启动加速中（追高风险，标注警示）
# >25%: 补涨完成，不入选

LOG_FILE = _DIR / "logs" / f"chain_catchup_{date.today():%Y%m}.log"
LOG_FILE.parent.mkdir(exist_ok=True)
logging.basicConfig(
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# K线获取（复用stock_timing的逻辑）
# ─────────────────────────────────────────────────────────────────────────────
import requests as _requests

_TENCENT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://finance.qq.com/",
}


def _fetch_kline(code: str, count: int = 120) -> pd.DataFrame | None:
    sym = f"{_market_prefix(code)}{code}"
    url = (
        f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        f"?_var=kline_dayqfq&param={sym},day,,,{count},qfq"
    )
    for attempt in range(3):
        try:
            r = _requests.get(url, headers=_TENCENT_HEADERS, timeout=10)
            r.raise_for_status()
            raw = r.text.replace("kline_dayqfq=", "")
            data = json.loads(raw)
            inner = data.get("data", {}).get(sym, {})
            klines = inner.get("day") or inner.get("qfqday") or []
            if len(klines) < 30:
                return None
            rows = []
            for k in klines:
                try:
                    rows.append({
                        "date": pd.to_datetime(k[0]),
                        "open": float(k[1]),
                        "close": float(k[2]),
                        "high": float(k[3]),
                        "low": float(k[4]),
                        "volume": float(k[5]),
                    })
                except (IndexError, ValueError):
                    continue
            if not rows:
                return None
            return pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
        except Exception as e:
            if attempt < 2:
                time.sleep(1.5)
            else:
                log.warning(f"K线 {code} 失败: {e}")
    return None


def _fetch_klines_batch(codes: list[str], count: int = 120) -> dict[str, pd.DataFrame | None]:
    from concurrent.futures import ThreadPoolExecutor, as_completed
    result: dict[str, pd.DataFrame | None] = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        fmap = {pool.submit(_fetch_kline, c, count): c for c in codes}
        for fut in as_completed(fmap):
            code = fmap[fut]
            try:
                result[code] = fut.result()
            except Exception:
                result[code] = None
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 技术指标计算
# ─────────────────────────────────────────────────────────────────────────────
def _calc_metrics(df: pd.DataFrame) -> dict | None:
    if len(df) < 60:
        return None
    close = df["close"].astype(float)
    volume = df["volume"].astype(float)

    ma5 = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()

    # RSI(14)
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rsi = 100 - 100 / (1 + gain / loss.replace(0, 1e-9))

    # MACD
    dif = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    dea = dif.ewm(span=9, adjust=False).mean()
    bar = 2 * (dif - dea)

    # 量比
    vol_ma20 = volume.rolling(20).mean()
    vol_ratio = float((volume / vol_ma20.replace(0, 1)).iloc[-1])

    # 涨幅
    gain_5d = (float(close.iloc[-1]) / float(close.iloc[-6]) - 1) * 100 if len(close) >= 6 else 0
    gain_20d = (float(close.iloc[-1]) / float(close.iloc[-21]) - 1) * 100 if len(close) >= 21 else 0
    gain_60d = (float(close.iloc[-1]) / float(close.iloc[-min(61, len(close))]) - 1) * 100

    # MA20偏离
    dev_ma20 = (float(close.iloc[-1]) - float(ma20.iloc[-1])) / float(ma20.iloc[-1]) * 100

    # MACD状态
    bar_now = float(bar.iloc[-1])
    bar_prev = float(bar.iloc[-2])
    dif_v = float(dif.iloc[-1])
    dea_v = float(dea.iloc[-1])

    # 金叉检测
    cross = "none"
    if float(dif.iloc[-2]) < float(dea.iloc[-2]) and dif_v > dea_v:
        cross = "golden"
    elif float(dif.iloc[-2]) > float(dea.iloc[-2]) and dif_v < dea_v:
        cross = "death"

    # 放量突破检测：今日量>1.5倍均量 + 收阳
    breakout = (vol_ratio >= 1.5 and float(close.iloc[-1]) > float(close.iloc[-2])
                and float(close.iloc[-1]) > float(ma5.iloc[-1]))

    return {
        "close": float(close.iloc[-1]),
        "ma5": float(ma5.iloc[-1]),
        "ma20": float(ma20.iloc[-1]),
        "ma60": float(ma60.iloc[-1]),
        "rsi": float(rsi.iloc[-1]),
        "vol_ratio": vol_ratio,
        "gain_5d": gain_5d,
        "gain_20d": gain_20d,
        "gain_60d": gain_60d,
        "dev_ma20": dev_ma20,
        "bar_now": bar_now,
        "bar_prev": bar_prev,
        "dif": dif_v,
        "dea": dea_v,
        "cross": cross,
        "bar_shrinking": bar_now > 0 and bar_now < bar_prev * 0.85,
        "breakout": breakout,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 龙头疲劳检测 + 存活度判定
# ─────────────────────────────────────────────────────────────────────────────
def detect_leader_fatigue(chain_name: str, leader_codes: list[str],
                          klines: dict, flows: dict | None = None) -> dict | None:
    """
    检测龙头是否进入疲劳期 + 判断龙头存活状态。

    疲劳条件（满足2/3即判定）：
      1. 平均RSI > 65
      2. 平均近20日涨幅 > 15%
      3. 量比 < 0.8（缩量）或 MACD红柱缩短

    存活度（决定补涨是否有效）：
      ALIVE: 龙头仍在MA20上方，补涨逻辑完全有效
      WEAKENING: 龙头跌破MA20但MA60仍撑，补涨窗口缩短(加警告)
      DEAD: 龙头跌破MA60或MACD死叉，取消该链补涨推荐
    """
    metrics_list = []
    for code in leader_codes:
        df = klines.get(code)
        if df is None:
            continue
        m = _calc_metrics(df)
        if m is None:
            continue
        metrics_list.append({"code": code, **m})

    if not metrics_list:
        return None

    avg_rsi = sum(m["rsi"] for m in metrics_list) / len(metrics_list)
    avg_gain_20d = sum(m["gain_20d"] for m in metrics_list) / len(metrics_list)
    avg_vol = sum(m["vol_ratio"] for m in metrics_list) / len(metrics_list)
    bar_shrink_count = sum(1 for m in metrics_list if m["bar_shrinking"])

    fatigue_signals = 0
    reasons = []

    if avg_rsi > LEADER_FATIGUE_RSI:
        fatigue_signals += 1
        reasons.append(f"RSI偏高({avg_rsi:.0f})")
    if avg_gain_20d > LEADER_FATIGUE_GAIN_20D:
        fatigue_signals += 1
        reasons.append(f"20日涨幅+{avg_gain_20d:.1f}%")
    if avg_vol < LEADER_FATIGUE_VOL_DECAY:
        fatigue_signals += 1
        reasons.append(f"缩量({avg_vol:.2f}x)")
    elif bar_shrink_count >= len(metrics_list) // 2 + 1:
        fatigue_signals += 1
        reasons.append(f"红柱缩短({bar_shrink_count}/{len(metrics_list)}只)")

    if fatigue_signals < 2:
        return None

    # ── 龙头存活度判定 ────────────────────────────────────────────────────
    dead_count = 0
    weak_count = 0
    for m in metrics_list:
        if m["close"] < m["ma60"] or m["cross"] == "death":
            dead_count += 1
        elif m["close"] < m["ma20"]:
            weak_count += 1

    if dead_count >= len(metrics_list) // 2 + 1:
        alive_status = "DEAD"
    elif weak_count + dead_count >= len(metrics_list) // 2 + 1:
        alive_status = "WEAKENING"
    else:
        alive_status = "ALIVE"

    # ── 龙头资金流出确认 ─────────────────────────────────────────────────
    leader_flow_total = 0.0
    if flows:
        for code in leader_codes:
            leader_flow_total += flows.get(code, 0.0)

    leader_names = []
    for code in leader_codes:
        info = STOCK_UNIVERSE.get(code) or ETF_UNIVERSE.get(code) or CATCHUP_EXTRA_INFO.get(code) or {}
        leader_names.append(info.get("name", code))

    if alive_status == "DEAD":
        reasons.append("⚠️龙头见顶(破MA60/死叉)")
    elif alive_status == "WEAKENING":
        reasons.append("龙头转弱(破MA20)")

    if leader_flow_total < -1.0:
        reasons.append(f"龙头资金净流出{abs(leader_flow_total):.1f}亿")

    return {
        "chain": chain_name,
        "leaders": leader_names,
        "alive_status": alive_status,
        "avg_rsi": round(avg_rsi, 1),
        "avg_gain_20d": round(avg_gain_20d, 1),
        "avg_vol": round(avg_vol, 2),
        "leader_flow": round(leader_flow_total, 2),
        "fatigue_reasons": reasons,
        "leader_metrics": metrics_list,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 补涨候选评分
# ─────────────────────────────────────────────────────────────────────────────
def score_catchup_candidate(code: str, metrics: dict, leader_avg_gain: float,
                            info: dict, flow: float = 0.0,
                            leader_flow: float = 0.0) -> dict | None:
    """
    评分维度（满分100）：
      涨幅差(30分)：与龙头涨幅差越大分越高
      技术启动(25分)：MACD金叉/即将金叉 + 放量突破 + 站上MA20
      基本面(20分)：f_policy + f_earnings + signal_3d
      估值洼地(15分)：偏离MA20越低分越高（但不低于-15%避免破位）
      资金启动(10分)：量比>1.2 + 主力资金流入确认
    """
    gain_20d = metrics["gain_20d"]

    # 分层判定
    if gain_20d > CATCHUP_TIER_ACCEL:
        return None  # 补涨完成，不再推荐
    tier = "待启动" if gain_20d <= CATCHUP_TIER_READY else "已启动"

    # 排除深度破位的（>10%跌破MA60 = 结构性弱势）
    if metrics["close"] < metrics["ma60"] * 0.90:
        return None
    # 轻度破位(5-10%)需MACD改善才可入选
    if metrics["close"] < metrics["ma60"] * 0.95:
        macd_improving = (
            metrics["cross"] == "golden" or
            (metrics["bar_now"] < 0 and metrics["bar_now"] > metrics["bar_prev"])
        )
        if not macd_improving:
            return None

    score = 0
    details = []

    # ── 涨幅差(30分) ──────────────────────────────────────────
    gap = leader_avg_gain - gain_20d
    if gap >= 20:
        score += 30; details.append(f"涨幅差{gap:.0f}%(极大)")
    elif gap >= 15:
        score += 25; details.append(f"涨幅差{gap:.0f}%(大)")
    elif gap >= 10:
        score += 20; details.append(f"涨幅差{gap:.0f}%(中)")
    elif gap >= 5:
        score += 12; details.append(f"涨幅差{gap:.0f}%(小)")
    elif gap >= 3 and tier == "已启动":
        score += 8; details.append(f"涨幅差{gap:.0f}%(已启动缩小)")
    else:
        return None  # 差距太小，不算补涨

    # ── 技术启动(25分) ────────────────────────────────────────
    if metrics["cross"] == "golden":
        score += 10; details.append("MACD金叉")
    elif metrics["dif"] < metrics["dea"] and metrics["bar_now"] > metrics["bar_prev"]:
        score += 5; details.append("MACD绿柱收缩")

    if metrics["breakout"]:
        score += 8; details.append("放量突破")

    if metrics["close"] > metrics["ma20"]:
        score += 5; details.append("站上MA20")
    elif metrics["close"] > metrics["ma20"] * 0.98:
        score += 3; details.append("贴近MA20")

    if metrics["rsi"] < 50:
        score += 2; details.append(f"RSI低位{metrics['rsi']:.0f}")

    # ── 基本面(20分) ──────────────────────────────────────────
    f_policy = info.get("f_policy", 50)
    f_earnings = info.get("f_earnings", 50)
    signal_3d = info.get("signal_3d", "★☆☆")

    if f_policy >= 85:
        score += 8; details.append(f"强政策({f_policy})")
    elif f_policy >= 70:
        score += 5

    if f_earnings >= 80:
        score += 7; details.append(f"强盈利({f_earnings})")
    elif f_earnings >= 65:
        score += 4

    if signal_3d == "★★★":
        score += 5; details.append("三维共振")
    elif signal_3d == "★★☆":
        score += 3

    # ── 估值洼地(15分) ────────────────────────────────────────
    dev = metrics["dev_ma20"]
    if -10 <= dev <= -3:
        score += 15; details.append(f"MA20下方{dev:.1f}%(洼地)")
    elif -3 < dev <= 0:
        score += 10; details.append(f"MA20附近({dev:.1f}%)")
    elif 0 < dev <= 5:
        score += 5
    # dev < -15% 说明破位，不加分

    # ── 资金启动(10分) ────────────────────────────────────────
    if metrics["vol_ratio"] >= 2.0:
        score += 7; details.append(f"强放量({metrics['vol_ratio']:.1f}x)")
    elif metrics["vol_ratio"] >= 1.5:
        score += 5; details.append(f"放量({metrics['vol_ratio']:.1f}x)")
    elif metrics["vol_ratio"] >= 1.2:
        score += 3

    # 资金搬家确认：候选净流入 + 龙头净流出 = 最强信号
    if flow > 0.3 and leader_flow < -0.5:
        score += 10; details.append(f"💰资金搬家确认(流入{flow:.1f}亿,龙头流出{abs(leader_flow):.1f}亿)")
    elif flow > 0.3:
        score += 5; details.append(f"主力净流入{flow:.1f}亿")
    elif flow > 0.1:
        score += 2

    # 已启动标的追高惩罚
    if tier == "已启动":
        score -= 10
        details.append(f"⚠️已启动+{gain_20d:.0f}%，追高风险")

    if score < CATCHUP_MIN_SCORE:
        return None

    # ── 操作价位计算 ──────────────────────────────────────────────────────
    close_v = metrics["close"]
    ma5_v = metrics["ma5"]
    ma20_v = metrics["ma20"]
    entry_price = round(max(ma5_v, close_v * 0.985), 2)
    stop_price = round(min(ma20_v * 0.97, close_v * 0.95), 2)
    target_price = round(close_v * (1 + gap * 0.003), 2)  # 补涨预期填补30%的差

    return {
        "code": code,
        "name": info.get("name", code),
        "score": score,
        "tier": tier,
        "gain_20d": round(gain_20d, 1),
        "gap_vs_leader": round(gap, 1),
        "details": details,
        "metrics": metrics,
        "info": info,
        "entry_price": entry_price,
        "stop_price": stop_price,
        "target_price": target_price,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 飞书卡片构建
# ─────────────────────────────────────────────────────────────────────────────
def _build_card(fatigue_chains: list[dict], catchup_results: dict[str, list[dict]],
                ts: str) -> dict:
    elements: list[dict] = []

    # 总览
    active_chains = [f for f in fatigue_chains]
    chains_with_candidates = sum(1 for f in active_chains
                                  if catchup_results.get(f["chain"]) or f.get("alive_status") == "DEAD")
    if not active_chains:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
            "content": "当前无龙头进入疲劳期，暂无补涨机会"}})
    else:
        summary = (
            f"检测到 **{len(active_chains)}** 条链龙头疲劳"
            f"，其中 **{chains_with_candidates}** 条有可操作标的"
        )
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": summary}})
        elements.append({"tag": "hr"})

    for fatigue in active_chains:
        chain = fatigue["chain"]
        candidates = catchup_results.get(chain, [])
        alive = fatigue.get("alive_status", "ALIVE")

        # P1: 只展示有候选的链 或 DEAD警告链（减少噪音）
        if not candidates and alive != "DEAD":
            continue
        leaders_str = "、".join(fatigue["leaders"])
        reasons_str = " + ".join(fatigue["fatigue_reasons"])
        alive_icon = {"ALIVE": "🟢", "WEAKENING": "🟡", "DEAD": "🔴"}[alive]
        flow_str = ""
        if fatigue.get("leader_flow", 0) < -0.5:
            flow_str = f"  资金流出{abs(fatigue['leader_flow']):.1f}亿"

        # 疲劳天数 → 阶段标签
        fdays = fatigue.get("fatigue_days", 1)
        if fdays <= 2:
            phase_tag = f"⏳预警期(第{fdays}天)"
        elif fdays <= 7:
            phase_tag = f"✅最佳窗口(第{fdays}天)"
        else:
            phase_tag = f"⚠️晚期(第{fdays}天，补涨或近尾声)"

        # 龙头疲劳信息
        elements.append({"tag": "div", "text": {"tag": "lark_md",
            "content": (
                f"**🔗 {chain}** {alive_icon}龙头{alive}  {phase_tag}\n"
                f"龙头：{leaders_str}\n"
                f"疲劳信号：{reasons_str}{flow_str}\n"
                f"  RSI={fatigue['avg_rsi']:.0f}  20日涨幅+{fatigue['avg_gain_20d']:.1f}%  量比={fatigue['avg_vol']:.2f}x"
            )}})

        # DEAD链直接标红跳过
        if alive == "DEAD":
            elements.append({"tag": "div", "text": {"tag": "lark_md",
                "content": "🚫 **龙头已死（破MA60/MACD死叉）— 补涨逻辑失效，回避该链！**"}})
            elements.append({"tag": "hr"})
            continue

        if alive == "WEAKENING":
            elements.append({"tag": "div", "text": {"tag": "lark_md",
                "content": "⚠️ 龙头转弱（破MA20），补涨窗口缩短，快进快出"}})

        # 补涨候选
        candidates = catchup_results.get(chain, [])
        if candidates:
            # 仓位建议：ALIVE=5%, WEAKENING=3%
            pos_pct = 5 if alive == "ALIVE" else 3
            lines = []
            for i, c in enumerate(candidates[:5], 1):
                stars = "⭐" * min(c["score"] // 20, 5)
                m = c["metrics"]
                tier_tag = "🟢待启动" if c.get("tier") == "待启动" else "🟡已启动"
                exit_w = c.get("exit_warning", "")
                exit_line = f"\n   {exit_w}" if exit_w else ""
                loss_pct = round((c['stop_price'] / m['close'] - 1) * 100, 1)
                gain_pct = round((c['target_price'] / m['close'] - 1) * 100, 1)
                lines.append(
                    f"{i}. {stars} **{c['name']}**（{c['code']}）{tier_tag}"
                    f"  评分 **{c['score']}/100**\n"
                    f"   20日涨幅 {c['gain_20d']:+.1f}%  vs龙头差 **{c['gap_vs_leader']:.0f}%**\n"
                    f"   价格{m['close']:.2f}  RSI={m['rsi']:.0f}  量比={m['vol_ratio']:.1f}x  偏MA20={m['dev_ma20']:+.1f}%\n"
                    f"   📌 介入≤**{c['entry_price']}**  止损≤{c['stop_price']}({loss_pct:+.1f}%)"
                    f"  目标{c['target_price']}({gain_pct:+.1f}%)  仓位{pos_pct}%\n"
                    f"   ✅ {'、'.join(c['details'][:5])}{exit_line}"
                )
            elements.append({"tag": "div", "text": {"tag": "lark_md",
                "content": "**补涨候选（按评分排序）**\n" + "\n\n".join(lines)}})
        else:
            elements.append({"tag": "div", "text": {"tag": "lark_md",
                "content": "*该链暂无合格补涨标的（均已跟涨或破位）*"}})

        elements.append({"tag": "hr"})

    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text",
            "content": "产业链补涨扫描 ｜ 补涨窗口通常3-5日 ｜ 龙头不死补涨不止 ｜ 严守止损"}],
    })

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text",
                    "content": f"🔗 产业链补涨挖掘 ｜ {ts}"},
                "template": "orange",
            },
            "elements": elements,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────────────────────
def main(dry: bool = False, force: bool = False) -> None:
    today = date.today().isoformat()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    log.info(f"=== 产业链补涨扫描 {ts} ===")

    if not force and not _utils_is_trading_day(today):
        log.info("非交易日，退出")
        return

    # 收集所有需要的代码
    all_codes: set[str] = set()
    for chain_cfg in CHAIN_MAP.values():
        all_codes.update(chain_cfg["leaders"])
        all_codes.update(chain_cfg["catchup_pool"])

    log.info(f"拉取 {len(all_codes)} 只标的K线...")
    klines = _fetch_klines_batch(list(all_codes), count=120)
    ok_count = sum(1 for v in klines.values() if v is not None)
    log.info(f"K线完成: {ok_count}/{len(all_codes)}")

    # 拉取主力资金流向（用于资金搬家确认）
    log.info("获取主力资金流向...")
    from data_fetcher import get_etf_main_force_flow
    all_flows = get_etf_main_force_flow(list(all_codes))
    flow_active = {k: v for k, v in all_flows.items() if abs(v) >= 0.1}
    log.info(f"资金流向有效值: {len(flow_active)} 只")

    # 读取昨日快照（用于补涨持续性追踪）
    yesterday_snap: dict = {}
    from datetime import timedelta
    for days_back in range(1, 4):
        prev_date = (date.today() - timedelta(days=days_back)).isoformat()
        prev_file = _DIR / "logs" / f"chain_catchup_{prev_date}.json"
        if prev_file.exists():
            try:
                yesterday_snap = json.loads(prev_file.read_text(encoding="utf-8"))
                log.info(f"加载前次快照: {prev_file.name}")
            except Exception:
                pass
            break

    # ── P1: 大盘熔断检查 ──────────────────────────────────────────────────
    market_suspended = False
    tech_suspended = False
    try:
        from data_fetcher import get_index_prices
        csi300_df = get_index_prices("000300", days=3)
        if csi300_df is not None and len(csi300_df) >= 2:
            csi_prev = float(csi300_df["close"].iloc[-2])
            csi_now = float(csi300_df["close"].iloc[-1])
            csi_chg = (csi_now / csi_prev - 1) * 100
            log.info(f"沪深300今日: {csi_chg:+.2f}%")
            if csi_chg < -1.5:
                market_suspended = True
                log.warning(f"⚠️ 沪深300跌{csi_chg:.2f}%，系统性风险压制，全部暂停")
        cyb_df = get_index_prices("399006", days=3)
        if cyb_df is not None and len(cyb_df) >= 2:
            cyb_prev = float(cyb_df["close"].iloc[-2])
            cyb_now = float(cyb_df["close"].iloc[-1])
            cyb_chg = (cyb_now / cyb_prev - 1) * 100
            if cyb_chg < -2.0:
                tech_suspended = True
                log.warning(f"⚠️ 创业板跌{cyb_chg:.2f}%，科技链(L1-L5)暂停")
    except Exception as e:
        log.debug(f"大盘检查失败: {e}")

    if market_suspended:
        card = {
            "msg_type": "interactive",
            "card": {
                "header": {"title": {"tag": "plain_text",
                    "content": f"🔗 产业链补涨 ｜ {ts} ｜ 暂停"}, "template": "red"},
                "elements": [{"tag": "div", "text": {"tag": "lark_md",
                    "content": f"🚫 **沪深300跌超1.5%，系统性风险压制所有Alpha**\n\n"
                               f"补涨逻辑在大盘普跌日完全失效，今日不做任何推荐。\n"
                               f"等大盘企稳后补涨机会会更安全。"}}],
            },
        }
        if dry:
            print("大盘熔断，暂停推送")
        else:
            _post_card(card, [FEISHU_WEBHOOK])
        return

    # 科技链暂停列表
    _TECH_CHAINS = {"L1_AI芯片设备", "L2_光模块光通信", "L3_铜缆高速连接",
                    "L4_PCB高速基板", "L5_MLCC被动元器件"}

    # 检测各链龙头疲劳
    fatigue_chains: list[dict] = []
    catchup_results: dict[str, list[dict]] = {}

    for chain_name, cfg in CHAIN_MAP.items():
        # P1: 创业板暴跌时科技链暂停
        if tech_suspended and chain_name in _TECH_CHAINS:
            log.info(f"  {chain_name}: 创业板暴跌，科技链暂停")
            continue

        fatigue = detect_leader_fatigue(chain_name, cfg["leaders"], klines, flows=all_flows)
        if fatigue is None:
            log.info(f"  {chain_name}: 龙头未疲劳，跳过")
            continue

        # P0: 龙头存活度判定 — DEAD则跳过该链
        alive = fatigue.get("alive_status", "ALIVE")
        if alive == "DEAD":
            log.warning(f"  {chain_name}: ❌龙头已死(破MA60/死叉)，取消补涨推荐")
            fatigue_chains.append(fatigue)
            catchup_results[chain_name] = []
            continue

        fatigue_chains.append(fatigue)
        alive_tag = "⚠️转弱" if alive == "WEAKENING" else "✅存活"
        log.info(f"  {chain_name}: 龙头疲劳[{alive_tag}] {fatigue['fatigue_reasons']}")

        leader_avg_gain = fatigue["avg_gain_20d"]
        leader_flow_total = fatigue.get("leader_flow", 0.0)

        # 扫描补涨候选
        candidates: list[dict] = []
        for code in cfg["catchup_pool"]:
            df = klines.get(code)
            if df is None:
                continue
            m = _calc_metrics(df)
            if m is None:
                continue
            info = STOCK_UNIVERSE.get(code) or ETF_UNIVERSE.get(code) or CATCHUP_EXTRA_INFO.get(code) or {"name": code}
            stock_flow = all_flows.get(code, 0.0)
            result = score_catchup_candidate(
                code, m, leader_avg_gain, info,
                flow=stock_flow, leader_flow=leader_flow_total,
            )
            if result:
                # P1补涨持续性：检查该标的是否昨日已在推荐中（连续推荐=启动确认）
                prev_chain_results = yesterday_snap.get("catchup_results", {}).get(chain_name, [])
                prev_codes = {r["code"] for r in prev_chain_results}
                if code in prev_codes:
                    result["continuation_days"] = 2
                    result["details"].append("📈连续2日入选(启动确认)")
                    result["score"] += 5
                else:
                    result["continuation_days"] = 1

                # ── P0: 补涨结束预警 ─────────────────────────────────
                exit_warning = None
                rsi_c = result["metrics"]["rsi"]
                gap_c = result["gap_vs_leader"]
                gain_c = result["gain_20d"]
                cont_days = result.get("continuation_days", 1)

                if cont_days >= 3 and rsi_c > 60:
                    exit_warning = f"🔴连续{cont_days}日+RSI={rsi_c:.0f}，建议止盈"
                elif gap_c < 5:
                    exit_warning = f"🔴涨幅差仅{gap_c:.0f}%，补涨空间耗尽"
                elif rsi_c > 70:
                    exit_warning = f"🟡RSI={rsi_c:.0f}超买，注意冲高回落"
                elif result["metrics"]["vol_ratio"] < 0.7 and gain_c > 5:
                    exit_warning = f"🟡缩量滞涨(量比{result['metrics']['vol_ratio']:.1f}x)，动量衰竭"

                if exit_warning:
                    result["exit_warning"] = exit_warning
                    result["details"].append(exit_warning)

                candidates.append(result)

        candidates.sort(key=lambda x: -x["score"])
        catchup_results[chain_name] = candidates
        log.info(f"    补涨候选: {len(candidates)} 只 — {[c['name'] for c in candidates[:5]]}")

    # ── P1: 龙头疲劳天数计算（回溯历史快照）─────────────────────────────────
    for fatigue in fatigue_chains:
        chain = fatigue["chain"]
        consec_days = 1  # 今天算第1天
        for days_back in range(1, 11):
            prev_d = (date.today() - timedelta(days=days_back)).isoformat()
            prev_f = _DIR / "logs" / f"chain_catchup_{prev_d}.json"
            if not prev_f.exists():
                continue
            try:
                prev_data = json.loads(prev_f.read_text(encoding="utf-8"))
                prev_chains = [f["chain"] for f in prev_data.get("fatigue_chains", [])]
                if chain in prev_chains:
                    consec_days += 1
                else:
                    break
            except Exception:
                break
        fatigue["fatigue_days"] = consec_days
        if consec_days >= 3:
            log.info(f"  {chain}: 连续疲劳{consec_days}天")

    if not fatigue_chains:
        log.info("所有链龙头仍强势，本次无补涨信号")
        return

    # ── P1: 信号变化检测（无变化不推送，减少噪音）─────────────────────────
    today_codes: set[str] = set()
    for cands in catchup_results.values():
        today_codes.update(c["code"] for c in cands)
    prev_codes_all: set[str] = set()
    for cands in yesterday_snap.get("catchup_results", {}).values():
        prev_codes_all.update(r["code"] for r in cands)

    has_change = (
        not yesterday_snap  # 首次运行必推
        or today_codes != prev_codes_all  # 有新增或移除
        or any(f.get("alive_status") == "DEAD" for f in fatigue_chains)  # 有龙头死亡警告
    )
    if not has_change and not dry:
        log.info(f"补涨列表无变化({len(today_codes)}只)，跳过推送")
        # 仍保存快照以便追踪持续天数
    else:
        log.info(f"信号变化: 昨日{len(prev_codes_all)}只→今日{len(today_codes)}只")

    # 构建并推送卡片（仅信号有变化时推送）
    if has_change or dry:
        card = _build_card(fatigue_chains, catchup_results, ts)
        if dry:
            for el in card["card"]["elements"]:
                if el.get("tag") == "div":
                    txt = el.get("text", {}).get("content", "")
                    if txt:
                        print(txt[:500])
                        print()
        else:
            _post_card(card, [FEISHU_WEBHOOK])
            log.info("飞书推送完成")

    # 保存快照
    snap = _DIR / "logs" / f"chain_catchup_{today}.json"
    snap.write_text(json.dumps({
        "ts": ts,
        "fatigue_chains": [{k: v for k, v in f.items() if k != "leader_metrics"} for f in fatigue_chains],
        "catchup_results": {
            chain: [{"code": c["code"], "name": c["name"], "score": c["score"],
                     "gain_20d": c["gain_20d"], "gap_vs_leader": c["gap_vs_leader"]}
                    for c in cands[:5]]
            for chain, cands in catchup_results.items()
        },
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"快照: {snap}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只打印不推送")
    ap.add_argument("--force", action="store_true", help="跳过交易日检查")
    args = ap.parse_args()
    main(dry=args.dry, force=args.force)
