#!/usr/bin/env python3
"""
个股研究池 · 盘中择时信号
每个交易日 09:45 起每隔 1 小时推送一次（09:45 / 10:45 / 11:45 / 12:45 / 13:45 / 14:45）。
盘中使用腾讯财经实时 K 线（含当日实时价格）。

用法:
    python3 signal_system/stock_timing.py          # 正常运行 + 推送飞书
    python3 signal_system/stock_timing.py --dry    # 只打印，不推送
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import requests

_DIR = Path(__file__).parent
sys.path.insert(0, str(_DIR))

from config import (  # noqa: E402
    STOCK_UNIVERSE,
    STOCK_CLUSTER_MAX_WEIGHT,
    DEFENSIVE_ROTATION_POOL,
    FEISHU_STOCK_WEBHOOKS as FEISHU_WEBHOOKS,
)
from data_fetcher import (  # noqa: E402
    get_north_flow,
    get_market_breadth,
    get_index_prices,
    get_etf_main_force_flow,
    market_prefix as _market_prefix,
)
from sentiment_gauge import calc_market_sentiment  # noqa: E402
from rotation_advisor import calc_rotation_signal, RotationSignal, DIM_EMOJI, STRENGTH_LABEL  # noqa: E402
from tail_market_scanner import detect_cluster_panic, fmt_cluster_panic_block, detect_offense_surge, fmt_offense_surge_block  # noqa: E402
from utils import is_trading_day as _utils_is_trading_day  # noqa: E402
from feishu_pusher import post_card as _post_card  # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────────────────────────────────────
STATE_FILE = _DIR / "state" / "regime_state.json"
LOG_FILE   = _DIR / "logs" / f"stock_timing_{date.today():%Y%m}.log"

REGIME_LABEL = {"R1": "趋势牛市", "R2": "震荡市", "R3": "轮动市", "R4": "风险市"}
REGIME_COLOR = {"R1": "green",   "R2": "blue",   "R3": "yellow", "R4": "red"}

# 机制 → 个股单仓上限（%）
REGIME_STOCK_MAX = {"R1": 8, "R2": 6, "R3": 4, "R4": 0}
# pool → 仓位系数
POOL_FACTOR = {"core": 1.0, "candidate": 0.6, "watch": 0.0}

SCORE_BUY_STRONG = 6   # 3月29样本: ★★★/sc=5超额+2.67%胜率78%; sc=6非★★★超额-4~-6%（见过滤规则）
SCORE_BUY_WATCH  = 5   # 3月83样本: BUY_WATCH超额+1.45%胜率59%; sc=4均收-2.12%超额-3.33%
SCORE_HOLD       = -2  # 全年回测: sc=-2超额+2.69%胜率63%, sc=-3超额-0.47%均为误减仓
SCORE_REDUCE     = -4  # 全年回测: sc=-4超额-4.36%胜率11%, sc=-5/-6强烈负超额

# watch池高风险cluster，买入阈值提高+1（回测: chemical/food_bev/consumer T+5超额持续-2~-5%）
_HIGH_RISK_CLUSTERS = frozenset({"chemical", "food_bev", "consumer"})

# ── 动态板块动量门（#2 行业开关层）─────────────────────────────────────────────
# 用板块代理ETF的MA20斜率(%/5日，见 ctx['cluster_trend'])动态判断板块趋势。
# 设计依据：组合回测显示买入信号超额几乎全部来自强势赛道(optics/semicon)，
# 弱势赛道(chemical/new_energy)持续负超额——砍掉逆势赛道是稳健加分项，
# 且基于实时板块趋势而非历史名单拟合，能自动适配赛道轮动。
SECTOR_MOM_VETO_SLOPE  = -1.0   # MA20斜率 < -1.0%：板块明确下行，买入信号一票否决降HOLD
SECTOR_MOM_WEAK_SLOPE  = -0.3   # MA20斜率 ∈ [-1.0,-0.3)：板块走弱，买入门槛+1（需更强信号）

# ── 绝对止损闸门（②不可被回测降级覆盖的硬风控）──────────────────────────────
# A股个股特征是"闷杀"——一字跌停、连续阴跌、业绩暴雷。任何"暂缓止损降为减仓"的
# 回测优化规则都不应覆盖这条底线，否则黑天鹅来时会扛着不动。触发任一即无条件 SELL_STOP。
HARD_STOP_DAY_DROP   = -9.0    # 单日跌幅 <= -9%（近跌停/重大利空）
HARD_STOP_3D_DROP    = -15.0   # 3日累计跌幅 <= -15%（连续闷杀）
HARD_STOP_CONSEC_DOWN = 5      # 连续收阴 >= 5日（阴跌不止，趋势性破位）

# cluster → 代理ETF（sh/sz前缀），用于板块趋势判断
# 规则：所在板块ETF的MA20斜率为负时，尾盘翻转信号不成立
_CLUSTER_PROXY_ETF: dict[str, str] = {
    "optics":          "sh515880",   # 通信ETF
    "semicon":         "sh512480",   # 半导体ETF
    "pcb":             "sh512480",   # 半导体ETF近似
    "power_equip":     "sz159326",   # 电网设备ETF
    "new_energy":      "sh516850",   # 新能源ETF
    "battery":         "sh516850",
    "battery_materials": "sh516850",
    "industrial_auto": "sz562500",   # 机器人ETF
    "defense":         "sz512660",   # 国防ETF
    "chemical":        "sz159870",   # 化工ETF — 万华化学所在板块
    "commodity":       "sh512400",   # 有色金属ETF
    "consumer":        "sh515030",   # 消费ETF
    "food_bev":        "sh515030",
    "pharma":          "sh512010",   # 医疗ETF
    "agriculture":     "sz159275",   # 农牧渔ETF — 牧原股份所在板块
    "finance":         "sh515850",   # 证券ETF
    "consumer_elec":   "sz159768",   # 消费电子ETF
    "software":        "sz159522",   # 软件ETF
    "machinery":       "sz562500",
    "shipping":        "sh510170",   # 交运ETF近似
}

_TENCENT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://finance.qq.com/",
}

# 节假日黑名单 — 统一从 config.HOLIDAY_BLACKLIST 读取（避免与 daily_guidance 不一致）

# ─────────────────────────────────────────────────────────────────────────────
# 日志
# ─────────────────────────────────────────────────────────────────────────────
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
# 交易日判断
# ─────────────────────────────────────────────────────────────────────────────
def is_trading_day(today: str | None = None) -> bool:
    """委托给 utils.is_trading_day（共享 config.HOLIDAY_BLACKLIST）。"""
    return _utils_is_trading_day(today)


# ─────────────────────────────────────────────────────────────────────────────
# K 线获取（腾讯财经，与主系统一致，含盘中实时价格）
# ─────────────────────────────────────────────────────────────────────────────


def _fetch_tencent_kline(code: str, count: int = 120) -> list | None:
    sym = f"{_market_prefix(code)}{code}"
    url = (
        f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        f"?_var=kline_dayqfq&param={sym},day,,,{count},qfq"
    )
    for attempt in range(3):
        try:
            r = requests.get(url, headers=_TENCENT_HEADERS, timeout=10)
            r.raise_for_status()
            raw = r.text.replace("kline_dayqfq=", "")
            data = json.loads(raw)
            inner = data.get("data", {}).get(sym, {})
            return inner.get("day") or inner.get("qfqday") or []
        except Exception as e:
            if attempt < 2:
                time.sleep(1.5)
            else:
                log.warning(f"腾讯K线 {code} 失败: {e}")
    return None


def fetch_kline(code: str, count: int = 300) -> pd.DataFrame | None:
    """
    返回 DataFrame(date, open, close, high, low, volume)。
    腾讯接口在交易时段自动包含今日实时价格作为最后一行。
    count=300 约覆盖14个月日线，足够重采样出60根周K和12根月K。
    """
    klines = _fetch_tencent_kline(code, count=count)
    if not klines or len(klines) < 30:
        return None
    rows = []
    for k in klines:
        try:
            rows.append({
                "date":   pd.to_datetime(k[0]),
                "open":   float(k[1]),
                "close":  float(k[2]),
                "high":   float(k[3]),
                "low":    float(k[4]),
                "volume": float(k[5]),
            })
        except (IndexError, ValueError):
            continue
    if not rows:
        return None
    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    return df


def fetch_klines_parallel(
    codes: list[str], count: int = 300, max_workers: int = 10
) -> dict[str, pd.DataFrame | None]:
    """并行抓取多只代码的日线，避免主流程串行 ~25 次 HTTP。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    result: dict[str, pd.DataFrame | None] = {}
    if not codes:
        return result
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        fmap = {pool.submit(fetch_kline, c, count): c for c in codes}
        for fut in as_completed(fmap):
            code = fmap[fut]
            try:
                result[code] = fut.result()
            except Exception as e:
                log.warning(f"K线 {code} 异常: {e}")
                result[code] = None
    return result


# ─────────────────────────────────────────────────────────────────────────────
# 技术指标
# ─────────────────────────────────────────────────────────────────────────────
def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def compute_indicators(df: pd.DataFrame) -> dict | None:
    if len(df) < 62:
        return None

    close  = df["close"].astype(float)
    volume = df["volume"].astype(float)

    ma5  = close.rolling(5).mean()
    ma20 = close.rolling(20).mean()
    ma60 = close.rolling(60).mean()

    dif = _ema(close, 12) - _ema(close, 26)
    dea = _ema(dif, 9)
    bar = 2 * (dif - dea)

    delta = close.diff()
    gain  = delta.clip(lower=0).rolling(14).mean()
    loss  = (-delta.clip(upper=0)).rolling(14).mean()
    rsi   = 100 - 100 / (1 + gain / loss.replace(0, 1e-9))

    vol_ma20  = volume.rolling(20).mean()
    vol_ratio = (volume / vol_ma20.replace(0, 1)).iloc[-1]

    chg3 = (
        (close.iloc[-1] / close.iloc[-4] - 1) * 100
        if len(close) >= 4 else 0.0
    )

    # 单日涨跌幅（用于绝对止损闸门：识别跌停/闷杀）
    chg1 = (
        (close.iloc[-1] / close.iloc[-2] - 1) * 100
        if len(close) >= 2 else 0.0
    )

    if dif.iloc[-2] < dea.iloc[-2] and dif.iloc[-1] > dea.iloc[-1]:
        cross = "golden"
    elif dif.iloc[-2] > dea.iloc[-2] and dif.iloc[-1] < dea.iloc[-1]:
        cross = "death"
    else:
        cross = "none"

    data_date = str(df["date"].iloc[-1])[:10]

    # MA60斜率（近5日变化率%），需要65条数据
    ma60_slope_5d = 0.0
    if len(close) >= 65:
        ma60_5d_ago = float(close.iloc[-(60 + 5):-5].mean())
        ma60_now_val = float(ma60.iloc[-1])
        if ma60_5d_ago > 0:
            ma60_slope_5d = round((ma60_now_val - ma60_5d_ago) / ma60_5d_ago * 100, 3)

    # 历史连续收跌天数（去除今日实时行，基于确认收盘）
    consec_down_days = 0
    hist_closes = close.values[:-1]
    for i in range(len(hist_closes) - 1, 0, -1):
        if hist_closes[i] < hist_closes[i - 1]:
            consec_down_days += 1
        else:
            break

    # 连续跌破MA60天数（含今日实时行，用于SELL_STOP确认）
    consec_below_ma60 = 0
    all_closes = close.values
    all_ma60 = ma60.values
    for i in range(len(all_closes) - 1, -1, -1):
        if all_ma60[i] > 0 and all_closes[i] < all_ma60[i]:
            consec_below_ma60 += 1
        else:
            break

    # 连续收涨天数（含今日实时行，用于主题过热判断）
    consec_up_days = 0
    for i in range(len(all_closes) - 1, 0, -1):
        if all_closes[i] > all_closes[i - 1]:
            consec_up_days += 1
        else:
            break

    # 均线偏离度（用于个股/主题追高风险）
    ma5_dev_pct = round((float(close.iloc[-1]) - float(ma5.iloc[-1])) / float(ma5.iloc[-1]) * 100, 2)
    ma20_dev_pct = round((float(close.iloc[-1]) - float(ma20.iloc[-1])) / float(ma20.iloc[-1]) * 100, 2)

    # 布林带 (20, 2σ)
    boll_std   = close.rolling(20).std()
    boll_upper = float((ma20 + 2 * boll_std).iloc[-1])
    boll_lower = float((ma20 - 2 * boll_std).iloc[-1])
    boll_width = boll_upper - boll_lower
    boll_pct   = float((close.iloc[-1] - boll_lower) / boll_width) if boll_width > 0 else 0.5

    # 日线 MACD 即将金叉：DIF < DEA 但差距正在缩小且 DIF 在上升
    _d, _e   = float(dif.iloc[-1]),  float(dea.iloc[-1])
    _dp, _ep = float(dif.iloc[-2]),  float(dea.iloc[-2])
    pre_golden_cross = (
        _d < _e and
        _d > _dp and
        (_e - _d) < (_ep - _dp)
    )

    return {
        "close":           float(close.iloc[-1]),
        "ma5":             float(ma5.iloc[-1]),
        "ma20":            float(ma20.iloc[-1]),
        "ma60":            float(ma60.iloc[-1]),
        "dif":             float(dif.iloc[-1]),
        "dea":             float(dea.iloc[-1]),
        "macd_bar":        float(bar.iloc[-1]),
        "macd_bar_p":      float(bar.iloc[-2]),
        "cross":           cross,
        "rsi":             float(rsi.iloc[-1]),
        "vol_ratio":       float(vol_ratio),
        "chg3":            float(chg3),
        "chg1":            float(chg1),
        "data_date":       data_date,
        "ma60_slope_5d":   ma60_slope_5d,
        "consec_down_days": consec_down_days,
        "consec_below_ma60":  consec_below_ma60,
        "consec_up_days":     consec_up_days,
        "ma5_dev_pct":        ma5_dev_pct,
        "ma20_dev_pct":       ma20_dev_pct,
        "boll_upper":         boll_upper,
        "boll_lower":         boll_lower,
        "boll_pct":           round(boll_pct, 3),
        "pre_golden_cross":   pre_golden_cross,
        "main_force_flow":    0.0,   # 在主循环中由 get_etf_main_force_flow 填充
    }


# ─────────────────────────────────────────────────────────────────────────────
# 信号评分
# ─────────────────────────────────────────────────────────────────────────────
def score_stock(ind: dict, info: dict) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    close, ma5, ma20, ma60 = ind["close"], ind["ma5"], ind["ma20"], ind["ma60"]

    # ── 维度1: 趋势（合并均线排列+MA60方向+连跌，消除共线性，单一分 ∈ [-2,+3]）
    trend_score = 0
    if close > ma5 > ma20 > ma60:
        trend_score = 2; reasons.append("多头排列")
    elif close > ma20 > ma60:
        trend_score = 1; reasons.append("站20/60线")
    elif close < ma60:
        trend_score = -2; reasons.append("跌破60线")
    elif close < ma20:
        trend_score = -1; reasons.append("跌破20线")
    # MA60方向只在趋势中性/正面时追加（避免与"跌破60线"重复扣分）
    ma60_slope = ind.get("ma60_slope_5d", 0.0)
    if trend_score >= 0 and ma60_slope > 0.5:
        trend_score += 1; reasons.append(f"MA60上行(+{ma60_slope:.1f}%/5日)")
    elif trend_score >= -1 and ma60_slope < -0.8:
        trend_score -= 1; reasons.append(f"MA60下行趋势({ma60_slope:.1f}%/5日)")
    # 连跌只在已偏弱时追加（避免多头排列+连跌矛盾打分）
    consec = ind.get("consec_down_days", 0)
    if consec >= 5 and trend_score <= 0:
        trend_score -= 1; reasons.append(f"连跌{consec}日")
    trend_score = max(-2, min(3, trend_score))
    score += trend_score

    # ── 维度2: MACD 动量（独立于均线，看金叉/死叉/柱体方向）
    cross = ind["cross"]
    if cross == "golden":
        score += 2; reasons.append("MACD金叉")
    elif cross == "death":
        score -= 2; reasons.append("MACD死叉")
    elif ind["dif"] > ind["dea"]:
        if ind["macd_bar"] > ind["macd_bar_p"]:
            score += 1; reasons.append("红柱扩张")
    else:
        if ind.get("pre_golden_cross"):
            score += 1; reasons.append("MACD即将金叉🔔")
        elif ind["macd_bar"] < ind["macd_bar_p"]:
            score -= 1; reasons.append("绿柱扩张")

    # ── 维度3: 过热/追高（合并RSI/偏离MA20/连涨，取最严重惩罚，单一分 ∈ [-3,+2]）
    overheat_score = 0
    rsi = ind["rsi"]
    dev_ma20 = (close - ma20) / ma20 * 100 if ma20 > 0 else 0
    consec_up = ind.get("consec_up_days", 0)
    # 超卖加分（只取一次）
    if rsi < 25:
        overheat_score = 2; reasons.append(f"RSI超卖{rsi:.0f}")
    elif rsi < 35:
        overheat_score = 1; reasons.append(f"RSI偏低{rsi:.0f}")
    else:
        # 过热：取 RSI/MA20偏离/连涨 中最严重的单一惩罚（不累加）
        penalties = []
        if rsi > 78:
            penalties.append((-2, f"RSI超买{rsi:.0f}"))
        elif rsi > 68:
            penalties.append((-1, f"RSI偏高{rsi:.0f}"))
        if dev_ma20 >= 20:
            penalties.append((-2, f"严重偏离MA20+{dev_ma20:.0f}%"))
        elif dev_ma20 >= 12:
            penalties.append((-1, f"偏离MA20+{dev_ma20:.0f}%"))
        if consec_up >= 5:
            penalties.append((-2, f"连涨{consec_up}日追高风险"))
        elif consec_up >= 3 and close > ma5 * 1.04:
            penalties.append((-1, f"连涨{consec_up}日+偏MA5"))
        if penalties:
            worst = min(penalties, key=lambda x: x[0])
            overheat_score = worst[0]
            reasons.append(worst[1])
            # RSI+连涨联合额外-1（两个独立维度同时触发才追加）
            has_rsi_hot = any(p[0] <= -1 and "RSI" in p[1] for p in penalties)
            has_consec_hot = any(p[0] <= -1 and "连涨" in p[1] for p in penalties)
            if has_rsi_hot and has_consec_hot:
                overheat_score -= 1
                reasons.append(f"RSI+连涨联合过热")
    overheat_score = max(-3, min(2, overheat_score))
    score += overheat_score

    # ── 维度4: 量能 & 资金（独立信息维度）
    vr = ind["vol_ratio"]
    if vr >= 1.5 and close >= ma5:
        score += 1; reasons.append(f"放量{vr:.1f}x")
    elif vr < 0.6 and close < ma5:
        score -= 1; reasons.append(f"缩量弱势{vr:.1f}x")

    flow = ind.get("main_force_flow", 0.0)
    if flow > 0.5:
        score += 1; reasons.append(f"主力净流入{flow:.1f}亿")
    elif flow < -0.5:
        score -= 1; reasons.append(f"主力净流出{abs(flow):.1f}亿")

    # 量价背离（独立信号：趋势向好但量能背离）
    if (close > ma5 and consec_up >= 2 and vr < 0.7 and flow < -0.3):
        score -= 1; reasons.append(f"量价背离(新高缩量{vr:.1f}x+主力流出{abs(flow):.1f}亿)")

    # ── 维度5: 三维信号强度（基本面/资金面/ETF联动，独立于技术面）
    sig3d = info.get("signal_3d", "★☆☆")
    if score > 0:
        if sig3d == "★★★":
            score += 1
        elif sig3d == "★☆☆":
            score -= 1

    # MACD红柱连续缩短（趋势疲劳）
    bar_now = ind.get("macd_bar", 0)
    bar_prev = ind.get("macd_bar_p", 0)
    if bar_now > 0 and bar_prev > 0 and bar_now < bar_prev * 0.85:
        if rsi > 60:
            score -= 1; reasons.append(f"红柱缩短({bar_prev:.3f}→{bar_now:.3f})+RSI偏高，动量衰竭")

    # 回踩确认加分：价格从高位回踩至MA5/MA20附近获得支撑
    boll_pct = ind.get("boll_pct", 0.5)
    if (0.25 <= boll_pct <= 0.55
        and close <= ma5 * 1.02
        and close >= ma20 * 0.98
        and ind.get("ma60_slope_5d", 0) > 0.2
        and ind.get("consec_down_days", 0) >= 2):
        score += 1; reasons.append("回踩MA5/MA20支撑确认✅")

    # 板块ETF联动验证：板块当日跌>1%时个股买入信号不可信
    cluster_etf_chg = ind.get("cluster_etf_chg")
    if cluster_etf_chg is not None and cluster_etf_chg < -1.0:
        score -= 2; reasons.append(f"板块ETF今跌{cluster_etf_chg:.1f}%，逆板块风险")

    return score, reasons


def signal_type(score: int) -> str:
    if score >= SCORE_BUY_STRONG: return "BUY_STRONG"
    if score >= SCORE_BUY_WATCH:  return "BUY_WATCH"
    if score >= SCORE_HOLD:       return "HOLD"
    if score >= SCORE_REDUCE:     return "REDUCE"
    return "SELL_STOP"


# ─────────────────────────────────────────────────────────────────────────────
# 周/月K 分析（从日线 DataFrame 重采样，无需额外 HTTP 请求）
# ─────────────────────────────────────────────────────────────────────────────

def _resample_period(df: pd.DataFrame, freq: str) -> pd.DataFrame | None:
    try:
        idx = df.set_index("date")
        closes = idx["close"].resample(freq).last().dropna()
        vols   = idx["volume"].resample(freq).sum()
        out = closes.to_frame()
        out["volume"] = vols
        return out.reset_index()
    except Exception:
        return None


def _weekly_macd_state(df: pd.DataFrame) -> dict:
    """周K MACD 状态（需要 ≥30 根周K，即约 7 个月日线数据）。"""
    df_w = _resample_period(df, "W")
    if df_w is None or len(df_w) < 30:
        return {"ok": False}
    close = df_w["close"].astype(float)
    dif = _ema(close, 12) - _ema(close, 26)
    dea = _ema(dif, 9)
    d, e   = float(dif.iloc[-1]),  float(dea.iloc[-1])
    dp, ep = float(dif.iloc[-2]),  float(dea.iloc[-2])
    pre_cross = d < e and d > dp and (e - d) < (ep - dp)
    bar_now  = float(2 * (dif - dea).iloc[-1])
    bar_prev = float(2 * (dif - dea).iloc[-2])
    return {
        "ok":         True,
        "golden":     d > e,
        "pre_cross":  pre_cross,
        "above_zero": d > 0,
        "bar_rising": bar_now > bar_prev,
        "dif":        round(d, 4),
        "dea":        round(e, 4),
    }


def _monthly_trend_state(df: pd.DataFrame) -> dict:
    """月K 趋势状态（用 MA3/MA6 判断，不依赖 MACD 避免月K数量不足）。"""
    df_m = _resample_period(df, "ME")
    if df_m is None or len(df_m) < 6:
        return {"ok": False}
    close  = df_m["close"].astype(float)
    ma3    = close.rolling(3).mean()
    ma6    = close.rolling(6).mean()
    cur    = float(close.iloc[-1])
    ma3_v  = float(ma3.iloc[-1])
    ma6_v  = float(ma6.iloc[-1])
    ma3_p  = float(ma3.iloc[-2]) if len(ma3.dropna()) >= 2 else ma3_v
    # 月线MACD（简版，用MA3-MA6代替DIF）
    dif_proxy = ma3_v - ma6_v
    dif_p     = float(ma3.iloc[-2]) - float(ma6.iloc[-2]) if len(ma6.dropna()) >= 2 else dif_proxy
    return {
        "ok":           True,
        "above_ma6":    cur > ma6_v,
        "ma3_rising":   ma3_v > ma3_p,
        "dif_positive": dif_proxy > 0,
        "dif_rising":   dif_proxy > dif_p,
        "ma3":          round(ma3_v, 2),
        "ma6":          round(ma6_v, 2),
    }


def check_reversal(
    ind: dict,
    df: pd.DataFrame,
    info: dict | None = None,
    ctx: dict | None = None,
) -> dict | None:
    """
    底部反转候选筛选（满分22分，≥10分入选）。

    硬门槛（必须全部满足才进入评分）：
      ① 价格在布林带中位以下（boll_pct < 0.50），即处于调整/底部区域
      ② MACD 有改善迹象（日线pre_golden_cross / 刚金叉 / 绿柱收缩）
      ③ MA60趋势非陡峭下行（ma60_slope_5d ≥ -0.8），排除结构性下跌（如通威股份光伏产能过剩）
      ④ 价格不低于MA60的-13%（close/ma60 ≥ 0.87），深度破位≠反转
      ⑤ 成交量≥0.8倍均值（vol_ratio ≥ 0.8），无量的反弹不可信
      ⑥ 盈利质量底线（f_earnings ≥ 60），基本面支撑反转

    技术面（满分6）：
      日线MACD即将金叉  +2 ｜ 已金叉/绿柱缩  +1
      Boll ≤15%位       +2 ｜ ≤30%位         +1
      RSI < 35          +2 ｜ < 45            +1
    周期面（满分4）：
      周线MACD金叉/即将  +2 ｜ 零轴以上/柱改善 +1
      月线站MA6且上升    +2 ｜ 任满足1          +1
    资金面（满分2）：
      主力净流入>0.5亿  +2 ｜ 中性             +1
    政策面（满分6）：
      f_policy ≥ 85     +2 ｜ ≥ 70            +1
      f_earnings ≥ 80   +2 ｜ ≥ 75            +1（盈利强可补偿政策弱势，如万华化学/中国神华）
      北向5日净流入>50亿 +1
    情绪面（满分2）：
      市场情绪COLD/NORMAL +2（最佳买入窗口）
      市场情绪WARM        +1
    量价确认（满分2）：
      量比≥1.5（强量反转）+2 ｜ 量比≥1.2       +1
    """
    # ── 硬门槛 ──────────────────────────────────────────────────────────
    bp = ind.get("boll_pct", 0.5)
    if bp >= 0.50:           # 价格在布林带中位以上 → 不是底部
        return None

    bar_now  = ind.get("macd_bar", 0)
    bar_prev = ind.get("macd_bar_p", 0)
    macd_improving = (
        ind.get("pre_golden_cross", False) or
        ind.get("cross") == "golden" or
        (bar_now < 0 and bar_now > bar_prev)  # 负柱缩小
    )
    if not macd_improving:   # MACD 没有改善迹象 → 仍在下跌中段
        return None

    # MA60趋势方向：陡峭下行=结构性下跌，不是反转（通威光伏产能过剩典型案例）
    ma60_slope = ind.get("ma60_slope_5d", 0.0)
    if ma60_slope < -0.8:
        return None

    # 价格与MA60距离：深度破位(>-13%)不是反转，是加速下跌
    close_val = ind.get("close", 0)
    ma60_val  = ind.get("ma60", 0)
    if ma60_val > 0 and close_val < ma60_val * 0.87:
        return None

    # 盈利质量底线：基本面无支撑的"反转"多为技术假信号（通威f_earnings=58被此门槛拦截）
    _info_gate = info or {}
    if _info_gate.get("f_earnings", 60) < 60:
        return None

    # 周线趋势门槛：周线DIF在零轴以下且MACD柱不改善 → 大趋势仍空，拒绝反转
    # 万华化学/牧原股份亏损案例：日线技术触底但周线仍在空头延续段
    wk_gate = _weekly_macd_state(df)
    if wk_gate["ok"]:
        wk_dif_below = not wk_gate.get("above_zero", False)
        wk_bar_bad   = not wk_gate.get("bar_rising", False) and not wk_gate.get("golden", False) and not wk_gate.get("pre_cross", False)
        if wk_dif_below and wk_bar_bad:
            return None   # 周线空头延续，日线反弹无效

    # 板块趋势门槛：cluster代理ETF的MA20斜率持续为负 → 板块趋势向下，个股反转大概率失败
    # 比亚迪（new_energy）、万华化学（chemical）、牧原股份（agriculture）均在下行板块中触发
    _cluster = (_info_gate).get("cluster", "")
    _cluster_trend = (ctx or {}).get("cluster_trend", {})
    if _cluster and _cluster in _cluster_trend:
        slope = _cluster_trend[_cluster]
        if slope is not None and slope < -0.3:
            return None   # 板块ETF MA20持续下行，逆势抄底胜率低
    vr = ind.get("vol_ratio", 0.5)

    pts      = 0
    details  = []
    dim_tech = dim_cycle = dim_fund = dim_policy = dim_senti = dim_vol = 0

    # ── 维度1: 技术面 ─────────────────────────────────────────────────
    if ind.get("pre_golden_cross"):
        pts += 2; dim_tech += 2; details.append("日线MACD即将金叉🔔")
    elif ind.get("cross") == "golden":
        pts += 1; dim_tech += 1; details.append("日线MACD刚金叉")
    else:
        pts += 1; dim_tech += 1; details.append("日线MACD绿柱收缩")

    if bp <= 0.15:
        pts += 2; dim_tech += 2; details.append(f"Boll触底({bp:.0%})")
    elif bp <= 0.30:
        pts += 1; dim_tech += 1; details.append(f"Boll接近下轨({bp:.0%})")

    rsi = ind.get("rsi", 50)
    if rsi < 35:
        pts += 2; dim_tech += 2; details.append(f"RSI超卖{rsi:.0f}")
    elif rsi < 45:
        pts += 1; dim_tech += 1; details.append(f"RSI偏低{rsi:.0f}")

    # ── 维度2: 周期面 ─────────────────────────────────────────────────
    wk = _weekly_macd_state(df)
    if wk["ok"]:
        if wk.get("golden") or wk.get("pre_cross"):
            pts += 2; dim_cycle += 2
            # 区分零轴上方真金叉 vs 零轴下方弱金叉（DIF/DEA均负，仅相对位置改善）
            if wk.get("golden") and wk.get("above_zero"):
                lbl = "周线MACD金叉✅"
            elif wk.get("golden"):
                lbl = f"周线MACD弱金叉(DIF={wk['dif']:+.2f}仍在零轴下)"
            else:
                lbl = "周线MACD即将金叉🔔"
            details.append(lbl)
        elif wk.get("above_zero"):
            pts += 1; dim_cycle += 1; details.append("周线DIF零轴以上")
        elif wk.get("bar_rising"):
            pts += 1; dim_cycle += 1; details.append("周线MACD柱改善")

    mk = _monthly_trend_state(df)
    if mk["ok"]:
        above    = mk.get("above_ma6", False)
        rising   = mk.get("ma3_rising", False)
        dif_pos  = mk.get("dif_positive", False)
        dif_rise = mk.get("dif_rising", False)
        if above and rising:
            pts += 2; dim_cycle += 2; details.append("月线站MA6且上升✅")
        elif above or dif_pos:
            pts += 1; dim_cycle += 1
            details.append("月线MA6支撑" if above else "月线趋势向好")
        elif dif_rise:
            pts += 1; dim_cycle += 1; details.append("月线趋势改善中")

    # ── 维度3: 资金面 ─────────────────────────────────────────────────
    flow = ind.get("main_force_flow", 0.0)
    if flow > 0.5:
        pts += 2; dim_fund += 2; details.append(f"主力净流入{flow:.1f}亿💰")
    elif flow >= -0.3:
        pts += 1; dim_fund += 1; details.append("主力资金中性")

    # ── 维度4: 政策面 ─────────────────────────────────────────────────
    _info = info or {}
    f_policy   = _info.get("f_policy", 60)
    f_earnings = _info.get("f_earnings", 60)
    if f_policy >= 85:
        pts += 2; dim_policy += 2; details.append(f"强政策支撑(f_policy={f_policy})")
    elif f_policy >= 70:
        pts += 1; dim_policy += 1; details.append(f"政策评分{f_policy}")
    if f_earnings >= 80:
        pts += 2; dim_policy += 2; details.append(f"盈利质量强(f_earnings={f_earnings})🛡️")
    elif f_earnings >= 75:
        pts += 1; dim_policy += 1; details.append(f"盈利质量佳(f_earnings={f_earnings})")

    north_5d = (ctx or {}).get("north_5d")
    if north_5d is not None and north_5d > 50:
        pts += 1; dim_policy += 1; details.append(f"北向5日净流入{north_5d:.0f}亿")

    # ── 维度5: 情绪面 ─────────────────────────────────────────────────
    senti = (ctx or {}).get("sentiment")
    if senti is not None:
        slevel = getattr(senti, "level", "NORMAL")
        if slevel in ("COLD", "NORMAL"):
            pts += 2; dim_senti += 2; details.append(f"市场情绪{slevel}(最佳买点窗口)🟢")
        elif slevel == "WARM":
            pts += 1; dim_senti += 1; details.append("市场情绪温和，可介入")
        else:
            details.append(f"市场情绪{slevel}⚠️，反转需等回落")

    # ── 维度6: 量价确认 ─────────────────────────────────────────────────
    if vr >= 1.5:
        pts += 2; dim_vol += 2; details.append(f"放量反转(量比{vr:.1f}x)")
    elif vr >= 1.2:
        pts += 1; dim_vol += 1; details.append(f"量价配合(量比{vr:.1f}x)")

    # 情绪冰点时降低反转门槛：COLD=最佳逆向窗口，7分即入选
    _senti_ctx = (ctx or {}).get("sentiment")
    _senti_level = getattr(_senti_ctx, "level", "NORMAL") if _senti_ctx else "NORMAL"
    _rev_threshold = 7 if _senti_level == "COLD" else 9
    if pts < _rev_threshold:
        return None

    return {
        "rev_pts":     pts,
        "rev_details": details,
        "boll_pct":    bp,
        "wk":          wk,
        "mk":          mk,
        "dim_tech":    dim_tech,
        "dim_cycle":   dim_cycle,
        "dim_fund":    dim_fund,
        "dim_policy":  dim_policy,
        "dim_senti":   dim_senti,
        "dim_vol":     dim_vol,
    }


def calc_position(sig: str, info: dict, regime: str, sector_rank: str = "neutral") -> int:
    """仓位计算：基础仓位(regime×pool×信号) × 板块动量排名系数。
    sector_rank = 'strong' 1.5×满配 / 'neutral' 1.0×标准 / 'weak' 0（零配）/ 'unknown' 不调整。
    """
    if sig not in ("BUY_STRONG", "BUY_WATCH"):
        return 0
    cap    = REGIME_STOCK_MAX.get(regime, 0)
    factor = POOL_FACTOR.get(info.get("pool", "watch"), 0.0)
    base   = cap * factor
    if sig == "BUY_WATCH":
        base *= 0.6
    # 板块动量排名加权：把"赛道选择力"这一唯一证实的alpha来源体现在仓位上
    if sector_rank == "strong":
        base *= 1.5
    elif sector_rank == "weak":
        base = 0
    return max(0, round(base))


# cluster → 止损乘数（回测5月: 50样本SELL_STOP后T+5均+2.81%，止损偏紧）
_CLUSTER_STOP_MULT: dict[str, float] = {
    "semicon": 0.93, "optics": 0.93, "defense": 0.93,
    "pcb": 0.93, "industrial_auto": 0.93, "new_energy": 0.93,
    "battery": 0.94, "commodity": 0.94, "machinery": 0.94,
    "consumer": 0.96, "food_bev": 0.96, "finance": 0.96,
}


def calc_stop(ind: dict, info: dict | None = None) -> float:
    cluster = (info or {}).get("cluster", "")
    mult = _CLUSTER_STOP_MULT.get(cluster, 0.95)
    return round(max(ind["ma20"], ind["close"] * mult), 2)


# ─────────────────────────────────────────────────────────────────────────────
# 主题/集群过热检测
# 连涨后谁来接盘？同一集群多只品种同时大涨 → 次日冲高回落概率高
# ─────────────────────────────────────────────────────────────────────────────

# 过热判定阈值
_CLUSTER_OVERHEAT_CONSEC = 3       # 集群内≥3只连涨≥3日 → 过热预警
_CLUSTER_OVERHEAT_DEV    = 4.0     # 集群均偏离MA5≥4% → 过热确认
_CLUSTER_DANGER_CONSEC   = 4       # 集群内≥2只连涨≥4日 → 接盘危险


def calc_theme_overheat(results: list[dict]) -> dict[str, dict]:
    """
    对每个 cluster 计算过热状态，返回 {cluster: overheat_info}。

    overheat_info 包含:
      level: "SAFE" / "CAUTION" / "DANGER"
      consec_up_stocks: 连涨≥3日的品种数
      avg_ma5_dev: 集群平均MA5偏离度(%)
      avg_rsi: 集群平均RSI
      avg_consec: 集群平均连涨天数
      stock_names: 过热品种名称列表
      note: 描述文字
    """
    cluster_data: dict[str, list[dict]] = {}
    for r in results:
        cluster = r["info"].get("cluster", "")
        if not cluster:
            continue
        if cluster not in cluster_data:
            cluster_data[cluster] = []
        cluster_data[cluster].append(r)

    overheat_map: dict[str, dict] = {}

    for cluster, stocks in cluster_data.items():
        if len(stocks) < 2:
            continue

        consec_list = [s["ind"].get("consec_up_days", 0) for s in stocks]
        dev_list    = [s["ind"].get("ma5_dev_pct", 0.0) for s in stocks]
        rsi_list    = [s["ind"].get("rsi", 50.0) for s in stocks]

        avg_consec = sum(consec_list) / len(consec_list)
        avg_dev    = sum(dev_list) / len(dev_list)
        avg_rsi    = sum(rsi_list) / len(rsi_list)

        consec_up_3 = sum(1 for c in consec_list if c >= 3)
        consec_up_4 = sum(1 for c in consec_list if c >= 4)

        hot_names = [s["name"] for s in stocks if s["ind"].get("consec_up_days", 0) >= 3]

        # 判定等级
        level = "SAFE"
        note_parts = []

        if consec_up_4 >= 2:
            level = "DANGER"
            note_parts.append(f"≥2只连涨4日+，接盘真空风险极高")
        elif consec_up_3 >= _CLUSTER_OVERHEAT_CONSEC and avg_dev >= _CLUSTER_OVERHEAT_DEV:
            level = "DANGER"
            note_parts.append(f"≥{consec_up_3}只连涨3日+ 均偏MA5+{avg_dev:.1f}%，追高危险")
        elif consec_up_3 >= 2 or avg_dev >= _CLUSTER_OVERHEAT_DEV:
            level = "CAUTION"
            note_parts.append(f"连涨品种增多(≥{consec_up_3}只) 或偏离MA5+{avg_dev:.1f}%")
        elif avg_consec >= 2 and avg_rsi >= 60:
            level = "CAUTION"
            note_parts.append(f"集群均连涨{avg_consec:.0f}日 RSI={avg_rsi:.0f}偏热")

        note = "; ".join(note_parts) if note_parts else "集群热度中性"
        if level != "SAFE" and hot_names:
            note += f" — 过热品种: {', '.join(hot_names[:4])}"

        theme_names = [s["info"].get("theme", "") for s in stocks[:3]]
        cluster_label = cluster + "(" + "/".join(theme_names) + ")"

        overheat_map[cluster] = {
            "level":          level,
            "cluster_label":  cluster_label,
            "consec_up_3":    consec_up_3,
            "consec_up_4":    consec_up_4,
            "avg_consec":     round(avg_consec, 1),
            "avg_ma5_dev":    round(avg_dev, 1),
            "avg_rsi":        round(avg_rsi, 0),
            "n_stocks":       len(stocks),
            "hot_names":      hot_names,
            "note":           note,
        }

    return overheat_map


def _fmt_theme_overheat_section(overheat_map: dict[str, dict]) -> str:
    """将集群过热状态渲染为飞书 markdown 段落。"""
    danger_clusters  = [v for v in overheat_map.values() if v["level"] == "DANGER"]
    caution_clusters = [v for v in overheat_map.values() if v["level"] == "CAUTION"]

    if not danger_clusters and not caution_clusters:
        return ""

    lines = []
    if danger_clusters:
        lines.append("🔥 **集群过热 · 次日接盘风险高 · 暂停追高**")
        for v in danger_clusters:
            lines.append(
                f"  🔴 **{v['cluster_label']}** "
                f"连涨≥3日:{v['consec_up_3']}/{v['n_stocks']}只 "
                f"均偏MA5+{v['avg_ma5_dev']:.1f}% RSI={v['avg_rsi']:.0f}"
            )
            lines.append(f"     *{v['note']}*")

    if caution_clusters:
        lines.append("⚠️ **集群偏热 · 关注回落风险**")
        for v in caution_clusters:
            lines.append(
                f"  🟡 {v['cluster_label']} "
                f"连涨≥3日:{v['consec_up_3']}/{v['n_stocks']}只 "
                f"均偏MA5+{v['avg_ma5_dev']:.1f}% RSI={v['avg_rsi']:.0f}"
            )
            lines.append(f"     *{v['note']}*")

    return "\n".join(lines)

_RANK_LABEL = {1: "🛡️ 极防御（公用/货币）", 2: "🔵 高股息蓝筹", 3: "🟡 消费/医药防御"}


def fetch_and_score_defensive() -> list[dict]:
    """扫描防御轮动池，返回按防御等级+趋势排序的结果列表。"""
    results: list[dict] = []
    codes = list(DEFENSIVE_ROTATION_POOL.keys())
    klines = fetch_klines_parallel(codes, count=300)
    for code, info in DEFENSIVE_ROTATION_POOL.items():
        df = klines.get(code)
        if df is None:
            log.debug(f"防御池 {code} {info['name']} K线获取失败")
            continue
        ind = compute_indicators(df)
        if ind is None:
            log.debug(f"防御池 {code} {info['name']} 数据不足")
            continue
        trend_ok = ind["close"] > ind["ma20"]
        macd_ok  = ind["dif"] > ind["dea"]
        results.append({
            "code":         code,
            "name":         info["name"],
            "defense_rank": info["defense_rank"],
            "note":         info["note"],
            "close":        ind["close"],
            "ma5":          ind["ma5"],
            "ma20":         ind["ma20"],
            "rsi":          ind["rsi"],
            "chg3":         ind["chg3"],
            "trend_ok":     trend_ok,
            "macd_ok":      macd_ok,
        })
    results.sort(key=lambda x: (x["defense_rank"], not x["trend_ok"], not x["macd_ok"]))
    return results


def _fmt_defensive_rotation(
    defensive: list[dict],
    rotation: "RotationSignal | None" = None,
) -> str:
    if not defensive:
        return "⚠️ 防御池数据获取失败，请手动参考防御品种"

    header_lines = ["**🔄 攻守切换建议**"]
    if rotation is not None:
        header_lines.append(f"**{rotation.dim_line()}**  →  {rotation.label()}")
        header_lines.append(rotation.detail_block())
    else:
        header_lines.append("> 情绪过热 — 降低进攻型仓位，择机切换至以下防御品种")
    header_lines.append("")

    body_lines: list[str] = []
    current_rank = None
    for item in defensive:
        rank = item["defense_rank"]
        if rank != current_rank:
            body_lines.append(f"**{_RANK_LABEL.get(rank, '')}**")
            current_rank = rank
        trend_icon = "✅" if item["trend_ok"] else "⚠️"
        macd_tag   = "MACD↑" if item["macd_ok"] else "MACD↓"
        body_lines.append(
            f"{trend_icon} **{item['name']}**（{item['code']}）"
            f"  {item['close']:.2f}  MA20{'✅' if item['trend_ok'] else '❌'}"
            f"  {macd_tag}  RSI={item['rsi']:.0f}  3日{item['chg3']:+.1f}%\n"
            f"   → {item['note']}"
        )
    return "\n".join(header_lines + body_lines)


# ─────────────────────────────────────────────────────────────────────────────
# 宏观上下文（大盘 + 资金面 + 情绪 + 板块流向）
# ─────────────────────────────────────────────────────────────────────────────

# 代表性板块 ETF（用于板块轮动快照，存全符号 sh/sz + 代码）
_SECTOR_ETFS: dict[str, str] = {
    "光通信": "sh515880",
    "半导体": "sh512480",
    "电网设备": "sz159326",
    "新能源": "sh516850",
    "券商":   "sh515850",
    "有色金属": "sh512400",
}

_SENTIMENT_LEVEL_LABEL: dict[str, str] = {
    "OVERHEATED": "💥 过热",
    "HOT":        "🔥 亢奋",
    "WARM":       "🙂 偏暖",
    "NORMAL":     "😐 中性",
    "COLD":       "🥶 偏冷",
}

# 大盘指数（全符号）
_INDICES: dict[str, str] = {
    "沪深300": "sh000300",
    "中证500": "sh000905",
    "创业板":  "sz399006",
}


def _price_snapshot(sym: str, count: int = 65) -> dict | None:
    """
    通用腾讯日K快照。sym 已包含 sh/sz 前缀（如 sh000300 / sz159326）。
    返回 {close, chg_pct, vs_ma5, vs_ma20}，失败返回 None。

    保留单 sym 接口以兼容老调用方；新调用方建议使用 _price_snapshots_batch。
    """
    res = _price_snapshots_batch([sym], count=count)
    return res.get(sym)


def _price_snapshots_batch(syms: list[str], count: int = 65) -> dict[str, dict]:
    """
    并行批量抓取多个 sym 的日K快照。腾讯日K接口本身一次只能查一个 sym，
    但用 ThreadPool 并行就能把 9 次串行 (~3s) 压到 ~0.5s。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _one(sym: str) -> tuple[str, dict | None]:
        url = (
            f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            f"?_var=kline_dayqfq&param={sym},day,,,{count},qfq"
        )
        for attempt in range(2):
            try:
                r = requests.get(url, headers=_TENCENT_HEADERS, timeout=8)
                r.raise_for_status()
                raw = r.text.replace("kline_dayqfq=", "")
                data = json.loads(raw)
                inner = data.get("data", {}).get(sym, {})
                klines = inner.get("day") or inner.get("qfqday") or []
                if len(klines) < 5:
                    return sym, None
                closes = [float(k[2]) for k in klines]
                close = closes[-1]
                prev = closes[-2]
                chg = round((close / prev - 1) * 100, 2)
                ma5 = sum(closes[-5:]) / 5
                ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else close
                ma20_slope: float | None = None
                if len(closes) >= 25:
                    ma20_prev = sum(closes[-25:-5]) / 20
                    ma20_slope = round((ma20 - ma20_prev) / ma20_prev * 100, 3) if ma20_prev > 0 else None
                # 多周期动量（5/20/60日收益率%）
                mom_5d = round((close / closes[-6] - 1) * 100, 2) if len(closes) >= 6 else 0.0
                mom_20d = round((close / closes[-21] - 1) * 100, 2) if len(closes) >= 21 else 0.0
                mom_60d = round((close / closes[-min(61, len(closes))] - 1) * 100, 2) if len(closes) >= 10 else 0.0
                # 三周期加权合成（短0.2+中0.5+长0.3）
                _w5, _w20, _w60 = 0.2, 0.5, 0.3
                _total_w = (_w5 if len(closes) >= 6 else 0) + (_w20 if len(closes) >= 21 else 0) + (_w60 if len(closes) >= 10 else 0)
                mom_composite = round(
                    (mom_5d * _w5 * (1 if len(closes) >= 6 else 0) +
                     mom_20d * _w20 * (1 if len(closes) >= 21 else 0) +
                     mom_60d * _w60 * (1 if len(closes) >= 10 else 0)) / _total_w, 3
                ) if _total_w > 0 else 0.0
                return sym, {
                    "close":      close,
                    "chg_pct":    chg,
                    "vs_ma5":     "↑" if close > ma5 else "↓",
                    "vs_ma20":    "↑" if close > ma20 else "↓",
                    "ma20_slope": ma20_slope,
                    "mom_5d":     mom_5d,
                    "mom_20d":    mom_20d,
                    "mom_60d":    mom_60d,
                    "mom_composite": mom_composite,
                }
            except Exception:
                if attempt == 0:
                    time.sleep(1)
        return sym, None

    out: dict[str, dict] = {}
    if not syms:
        return out
    with ThreadPoolExecutor(max_workers=min(10, len(syms))) as pool:
        for fut in as_completed(pool.submit(_one, s) for s in syms):
            sym, snap = fut.result()
            if snap is not None:
                out[sym] = snap
    return out


def fetch_macro_context() -> dict:
    """
    并行拉取：
      - 三大指数（沪深300 / 中证500 / 创业板）
      - 北向资金（今日 + 近5日累计）
      - 市场情绪（SentimentReading）
      - 板块主力净流向（6个代表性 ETF）
    返回 dict，全部容错（字段缺失不影响主流程）。
    """
    ctx: dict = {}

    # ── 1. 大盘指数（批量并行） ────────────────────────────────────────────
    idx_snaps = _price_snapshots_batch(list(_INDICES.values()))
    indices: dict[str, dict] = {}
    for name, sym in _INDICES.items():
        snap = idx_snaps.get(sym)
        if snap:
            indices[name] = snap
    ctx["indices"] = indices

    # ── 2. 北向资金 ────────────────────────────────────────────────────────
    try:
        north_df = get_north_flow(days=8)
        if north_df is not None and len(north_df) >= 1:
            # 今日净买入（最后一行实时值）
            net_s = north_df["net_buy_billion"].dropna()
            if len(net_s) >= 1:
                ctx["north_today"] = round(float(net_s.iloc[-1]), 2)
            # 5日合计：优先用接口直接返回的多周期合计（attrs），废额度后缓存累加不可靠
            north_5d = north_df.attrs.get("net_buy_5d")
            if north_5d is not None:
                ctx["north_5d"] = north_5d
            # 成交总额（活跃度代理，东方财富自动）
            deal_s = north_df["deal_amt_billion"].dropna()
            if len(deal_s) >= 1:
                ctx["north_today_deal"] = round(float(deal_s.iloc[-1]), 2)
            if len(deal_s) >= 5:
                ctx["north_5d_deal_avg"] = round(float(deal_s.tail(5).mean()), 2)
    except Exception as e:
        log.debug(f"北向数据: {e}")

    # ── 3. 情绪面 ──────────────────────────────────────────────────────────
    try:
        csi300_df = get_index_prices("000300", days=40)
        breadth   = get_market_breadth()
        ctx["sentiment"] = calc_market_sentiment(csi300_df, breadth)
    except Exception as e:
        log.debug(f"情绪数据: {e}")

    # ── 4. 板块轮动（批量并行）─────────────────────────────────────────────
    sec_raw = _price_snapshots_batch(list(_SECTOR_ETFS.values()), count=10)
    sector_snaps: dict[str, dict] = {}
    for name, sym in _SECTOR_ETFS.items():
        snap = sec_raw.get(sym)
        if snap:
            sector_snaps[name] = snap
    ctx["sector_snaps"] = sector_snaps

    # ── 5. 三维攻守切换强度 ───────────────────────────────────────────────
    senti = ctx.get("sentiment")
    if senti is not None:
        policy_chgs = [
            sector_snaps[s]["chg_pct"]
            for s in ("电网设备", "半导体", "光通信")
            if s in sector_snaps
        ]
        policy_avg = sum(policy_chgs) / len(policy_chgs) if policy_chgs else None
        ctx["rotation"] = calc_rotation_signal(
            sentiment=senti,
            north_5d_billion=ctx.get("north_5d"),
            policy_sector_avg_chg=policy_avg,
            north_5d_deal_avg=ctx.get("north_5d_deal_avg"),
            north_today_deal=ctx.get("north_today_deal"),
        )

    # ── 6. 集群恐慌检测（用板块ETF快照的今日涨跌幅） ─────────────────────────
    # sector_snaps 已在步骤4拉取，key是中文名；这里用STOCK_UNIVERSE做集群映射
    # 同时从all_klines（主流程传入后会有）或sector_snaps补充涨跌幅
    try:
        # 用 sector_snaps 里的行情构造 {code: chg_pct}（按code反查）
        _sec_snaps = ctx.get("sector_snaps", {})
        # 同时尝试从indices拿大盘级别
        change_pcts: dict[str, float] = {}
        # sector_snaps key是中文名，需要反查_SECTOR_ETFS获取code
        for sec_name, sym in _SECTOR_ETFS.items():
            snap = _sec_snaps.get(sec_name)
            if snap and snap.get("chg_pct") is not None:
                # sym格式 sh515880 → code 515880
                code = sym[2:]
                change_pcts[code] = snap["chg_pct"]
        panic_clusters = detect_cluster_panic(STOCK_UNIVERSE, change_pcts)
        # 补充：用ETF行情覆盖/补充STOCK_UNIVERSE里匹配cluster的品种
        if panic_clusters:
            ctx["panic_clusters"] = panic_clusters
    except Exception as e:
        log.debug(f"集群恐慌检测: {e}")

    # ── 7. 板块趋势（多周期动量合成，用于买入门控+仓位分配） ────────────
    try:
        proxy_syms = list(set(_CLUSTER_PROXY_ETF.values()))
        proxy_raw  = _price_snapshots_batch(proxy_syms, count=65)
        cluster_trend: dict[str, float | None] = {}
        for cluster, sym in _CLUSTER_PROXY_ETF.items():
            snap = proxy_raw.get(sym)
            # 优先用三周期合成动量，fallback 到 ma20_slope
            cluster_trend[cluster] = (
                snap.get("mom_composite") or snap.get("ma20_slope")
            ) if snap else None
        ctx["cluster_trend"] = cluster_trend
    except Exception as e:
        log.debug(f"板块趋势获取失败: {e}")

    return ctx


def _fmt_macro_section(ctx: dict) -> str:
    """将宏观上下文格式化为飞书 lark_md 文本块"""
    lines: list[str] = []

    # 大盘
    idx = ctx.get("indices", {})
    if idx:
        parts = []
        for name, snap in idx.items():
            sign  = "📈" if snap["chg_pct"] > 0 else ("📉" if snap["chg_pct"] < 0 else "➡️")
            color = "**" if abs(snap["chg_pct"]) > 0.5 else ""
            parts.append(
                f"{name} {color}{snap['chg_pct']:+.2f}%{color}"
                f" MA5{snap['vs_ma5']} MA20{snap['vs_ma20']}"
            )
        lines.append("🌐 **大盘**  " + "　｜　".join(parts))

    # 资金面
    capital_parts: list[str] = []
    north_today     = ctx.get("north_today")       # 精确净买入（手动数据）
    north_5d        = ctx.get("north_5d")
    north_deal      = ctx.get("north_today_deal")  # 成交总额（东方财富）
    north_deal_avg  = ctx.get("north_5d_deal_avg")
    if north_today is not None:
        sign  = "+" if north_today >= 0 else ""
        trend = "净流入🟢" if north_today > 5 else ("净流出🔴" if north_today < -5 else "平衡")
        capital_parts.append(f"北向今日净买入 **{sign}{north_today:.1f}亿**（{trend}）")
        if north_5d is not None:
            sign5 = "+" if north_5d >= 0 else ""
            capital_parts.append(f"近5日合计 {sign5}{north_5d:.1f}亿")
    elif north_deal is not None:
        capital_parts.append(f"北向成交 {north_deal:.1f}亿（仅活跃度，净方向未知）")
        if north_deal_avg is not None:
            dev = (north_deal - north_deal_avg) / north_deal_avg * 100
            capital_parts.append(f"5日均{north_deal_avg:.1f}亿 偏离{dev:+.0f}%")
    else:
        capital_parts.append("北向资金：暂无数据")
    lines.append("💰 **资金面**  " + "　".join(capital_parts))

    # 板块轮动（ETF 涨跌幅）
    sector_snaps = ctx.get("sector_snaps", {})
    if sector_snaps:
        flow_parts = []
        for name, snap in sector_snaps.items():
            chg = snap["chg_pct"]
            ma5_tag = snap["vs_ma5"]
            if chg > 0.5:
                flow_parts.append(f"▲**{name}**{chg:+.1f}%{ma5_tag}")
            elif chg < -0.5:
                flow_parts.append(f"▼{name}{chg:+.1f}%{ma5_tag}")
            else:
                flow_parts.append(f"→{name}{chg:+.1f}%")
        lines.append("🔄 **板块轮动**  " + "  ".join(flow_parts))

    # 情绪面
    senti = ctx.get("sentiment")
    if senti is not None:
        level_label = _SENTIMENT_LEVEL_LABEL.get(senti.level, senti.level)
        block_warn  = "  ⚠️ **情绪过热，暂停新买**" if senti.timing_block else ""
        lines.append(
            f"🧭 **情绪**  {level_label} {senti.score:.0f}分"
            f"  涨/跌停={senti.limit_up}/{senti.limit_down}"
            f"  RSI(6)={senti.rsi6:.0f}"
            f"  连涨{senti.consec_up_days}日"
            f"{block_warn}"
        )
        if senti.note:
            lines.append(f"   *{senti.note}*")

    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# 机制读取
# ─────────────────────────────────────────────────────────────────────────────
def current_regime() -> str:
    try:
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return state.get("current_regime", "R2")
    except Exception:
        return "R2"


# ─────────────────────────────────────────────────────────────────────────────
# 飞书卡片
# ─────────────────────────────────────────────────────────────────────────────
def _signal_label(sig: str, score: int, pos: int) -> tuple[str, str]:
    """返回 (emoji, 操作标签)，按信号类型 + 得分 + 是否持仓细分"""
    held = pos > 0
    if sig == "BUY_STRONG":
        return "🟢", f"强买入 — 建仓{pos}%"
    if sig == "BUY_WATCH":
        if held:
            return "🔵", f"关注买点 — 建仓{pos}%"
        return "🔵", "关注买点 — 等回调入场"
    if sig == "HOLD":
        if score >= 2:
            return "🟡", "接近买入线 — 可小仓关注"
        if score >= 0:
            return "⚪", "持仓不动 — 技术中性"
        return "🟠", "弱势持观 — 偏弱未破位"
    if sig == "REDUCE":
        if held:
            return "📉", "建议减仓 — 技术走弱"
        if score >= -2:
            return "🟠", "暂观望 — 动能偏弱"
        return "📍", "回避 — 技术偏空"
    if sig == "SELL_STOP":
        return "🔴", "止损清仓 — 立即执行"
    return "⚪", "观察"


def _rsi_tag(rsi: float) -> str:
    if rsi >= 78: return f"RSI={rsi:.0f}🔴超买"
    if rsi >= 68: return f"RSI={rsi:.0f}⚠️偏高"
    if rsi <= 25: return f"RSI={rsi:.0f}🟢超卖"
    if rsi <= 35: return f"RSI={rsi:.0f}🟡偏低"
    return f"RSI={rsi:.0f}"


def _suggest_entry(ind: dict, sig: str) -> tuple[float, str]:
    """返回 (建议买入参考价, 简短提示)。回测:BUY后T+1均亏-2.17%，分批建仓减少冲击。"""
    close, ma5, ma20 = ind["close"], ind["ma5"], ind["ma20"]
    entry2 = round(min(close * 0.985, ma5 * 1.005), 2)
    if sig == "BUY_STRONG":
        if close > ma5 * 1.03:
            return round(ma5 * 1.01, 2), f"⚡首仓50%≤{round(ma5*1.01,2)} + 补仓50%≤{entry2}"
        return round(close, 2), f"⚡首仓50%现价 + 次日回调至{entry2}补仓50%"
    # BUY_WATCH
    if close > ma5 * 1.02:
        return round(ma5 * 1.01, 2), f"首仓50%≤{round(ma5*1.01,2)} + 次日补仓50%≤{entry2}"
    return round(close, 2), f"首仓50%现价 + 次日回调至{entry2}补仓50%"


def _score100(score: int) -> int:
    """将原始得分（约 -10~+10）映射到 0-100 评分，便于直观判断。"""
    return min(100, max(0, round((score + 10) * 5)))


def _score100_bar(score100: int) -> str:
    """生成可视化评分条，每10分一格，共10格。"""
    filled = score100 // 10
    bar = "█" * filled + "░" * (10 - filled)
    if score100 >= 75:
        level = "强"
    elif score100 >= 60:
        level = "中"
    elif score100 >= 40:
        level = "弱"
    else:
        level = "差"
    return f"{bar} **{score100}/100**（{level}）"


def _signal_line(r: dict) -> str:
    ind   = r["ind"]
    info  = r["info"]
    sig   = r["signal"]
    score = r["score"]
    pos   = r["position_pct"]
    stop  = r["stop_price"]
    text  = "、".join(r["reasons"]) if r["reasons"] else "技术中性"

    emoji, label = _signal_label(sig, score, pos)
    s100 = _score100(score)

    price_line = (
        f"价格 **{ind['close']:.2f}**"
        f"  MA5={ind['ma5']:.2f}  MA20={ind['ma20']:.2f}  MA60={ind['ma60']:.2f}"
    )
    ma60_slope = ind.get("ma60_slope_5d", 0.0)
    ma60_arr = "↑" if ma60_slope > 0.3 else ("↓" if ma60_slope < -0.3 else "→")
    flow = ind.get("main_force_flow", 0.0)
    flow_str = (f"  主力{'↑' if flow > 0 else '↓'}{abs(flow):.1f}亿" if abs(flow) >= 0.3 else "")
    score_bar = _score100_bar(s100)
    tech_line = (
        f"📊 评分 {score_bar}\n"
        f"  {_rsi_tag(ind['rsi'])}  量比={ind['vol_ratio']:.1f}x"
        f"  3日{ind['chg3']:+.1f}%  MA60{ma60_arr}"
        f"{flow_str}  ｜ {text}"
    )
    if sig in ("BUY_STRONG", "BUY_WATCH") and pos > 0:
        entry, hint = _suggest_entry(ind, sig)
        loss_pct = (stop / ind["close"] - 1) * 100
        op = (
            f"📌 **买入参考价 ≤{entry}**  建仓 **{pos}%**  止损≤{stop}（{loss_pct:.1f}%）\n"
            f"   💡 {hint}"
        )
    elif sig == "SELL_STOP":
        op = f"🔴 **立即止损**  参考价 ≤{stop}  ({(stop/ind['close']-1)*100:.1f}%)"
    elif sig == "REDUCE" and pos > 0:
        op = f"减仓操作  止损参考 {stop}"
    else:
        op = "无持仓 — 纯观察，不操作"

    return (
        f"{emoji} **{r['name']}**（{r['code']}）"
        f"｜{info.get('signal_3d','—')} {info.get('theme','')}｜{label}\n"
        f"  {price_line}\n"
        f"  {tech_line}\n"
        f"  {op}"
    )


def _dim_bar(score: int, max_score: int, label: str) -> str:
    """单维度得分进度条：label [████░░] n/max"""
    filled = round(score / max_score * 5) if max_score > 0 else 0
    bar = "█" * filled + "░" * (5 - filled)
    return f"{label}[{bar}]{score}/{max_score}"


def _reversal_line(r: dict) -> str:
    ind  = r["ind"]
    info = r["info"]
    pts  = r["rev_pts"]
    wk   = r.get("wk", {})
    mk   = r.get("mk", {})
    bp   = r.get("boll_pct", 0.5)

    # 综合星级（满分22，每4分一星，最多5星）
    stars = "⭐" * min(pts // 4, 5)

    # 5维度得分条
    d_tech   = r.get("dim_tech",   0)
    d_cycle  = r.get("dim_cycle",  0)
    d_fund   = r.get("dim_fund",   0)
    d_policy = r.get("dim_policy", 0)
    d_senti  = r.get("dim_senti",  0)
    d_vol    = r.get("dim_vol",    0)
    radar = (
        f"{_dim_bar(d_tech,  6, '技术')}  "
        f"{_dim_bar(d_cycle, 4, '周期')}  "
        f"{_dim_bar(d_fund,  2, '资金')}  "
        f"{_dim_bar(d_policy,6, '政策')}  "
        f"{_dim_bar(d_senti, 2, '情绪')}  "
        f"{_dim_bar(d_vol,   2, '量价')}"
    )

    boll_line = (
        f"Boll下轨={ind['boll_lower']:.2f}  上轨={ind['boll_upper']:.2f}"
        f"  价格位于{bp:.0%}位"
    )
    weekly_str = (
        "金叉✅" if wk.get("golden") else
        "即将金叉🔔" if wk.get("pre_cross") else
        "零轴↑" if wk.get("above_zero") else
        "偏弱⚠️"
    ) if wk.get("ok") else "—"
    monthly_str = (
        "站MA6✅" if mk.get("above_ma6") else
        "MA6支撑" if mk.get("dif_positive") else
        "改善中" if mk.get("dif_rising") else "偏弱"
    ) if mk.get("ok") else "—"

    flow = ind.get("main_force_flow", 0.0)
    flow_str = f"{'净流入💰' if flow>0 else '净流出'}{abs(flow):.1f}亿" if abs(flow) >= 0.1 else "中性"

    details_str = "、".join(r["rev_details"])

    return (
        f"🔄 **{r['name']}**（{r['code']}）｜{info.get('signal_3d','—')} {info.get('theme','')}  "
        f"{stars}  **{pts}/22分**\n"
        f"  {radar}\n"
        f"  价格 **{ind['close']:.2f}**  RSI={ind['rsi']:.0f}  量比={ind['vol_ratio']:.1f}x  主力{flow_str}\n"
        f"  {boll_line}\n"
        f"  周线: {weekly_str}  ｜  月线: {monthly_str}\n"
        f"  ✅ {details_str}"
    )


def _triple_align_watch_line(r: dict) -> str:
    """三线共振候观区格式化：日线即将金叉+周线零轴上金叉+月线站MA6，偏离MA20过高暂不买。"""
    ind  = r["ind"]
    info = r["info"]
    close   = ind["close"]
    ma20    = ind["ma20"]
    dev_ma20 = (close - ma20) / ma20 * 100
    entry_lo = round(ma20 * 0.99, 2)
    entry_hi = round(ma20 * 1.02, 2)
    wk  = r.get("wk", {})
    mk  = r.get("mk", {})
    wk_str = f"DIF={wk.get('dif',0):+.2f}" if wk.get("ok") else "—"
    mk_str = "站MA6✅" if mk.get("above_ma6") else "MA3↑"
    return (
        f"📌 **{info['name']}**（{r['code']}）｜{info.get('signal_3d','—')} "
        f"{info.get('theme','')}  [{info.get('pool','')}]\n"
        f"  三线共振：日线即将金叉🔔  周线{wk_str}零轴上✅  月线{mk_str}\n"
        f"  当前价 **{close:.2f}**  偏MA20 **{dev_ma20:+.1f}%**  RSI={ind['rsi']:.0f}\n"
        f"  ⏳ 建议等回踩 **{entry_lo}～{entry_hi}** 附近再介入"
    )


def _ma5_hug_watch_line(r: dict) -> str:
    """MA5贴线上攻候观区：沿MA5一路上攻，当前偏离过高等回踩，或大跌日不破MA5的强势股。"""
    ind  = r["ind"]
    info = r["info"]
    close  = ind["close"]
    ma5    = ind["ma5"]
    ma20   = ind["ma20"]
    dev_ma5  = (close - ma5)  / ma5  * 100
    dev_ma20 = (close - ma20) / ma20 * 100
    entry = round(ma5 * 1.005, 2)   # 贴MA5买入参考：MA5 + 0.5%
    tag = r.get("ma5_hug_tag", "")
    tag_str = "  🛡️ 大跌日护盘" if tag == "resilient" else ""
    ma5_slope = r.get("ma5_slope_5d", 0)
    return (
        f"🚀 **{info['name']}**（{r['code']}）｜{info.get('signal_3d','—')} "
        f"{info.get('theme','')}  [{info.get('pool','')}]{tag_str}\n"
        f"  MA5贴线上攻：近15日站MA5 {r.get('above_count',0)}/15天  MA5近5日涨幅{ma5_slope:+.1f}%\n"
        f"  当前价 **{close:.2f}**  偏MA5 **{dev_ma5:+.1f}%**  偏MA20 {dev_ma20:+.1f}%  RSI={ind['rsi']:.0f}\n"
        f"  ⏳ 回踩 MA5({ma5:.2f}) 附近 ≤**{entry}** 时介入"
    )


def _scan_ma5_hug(results: list[dict], index_chg_yesterday: float) -> list[dict]:
    """
    扫描两类MA5上攻形态并合并返回：
    A. 持续贴MA5上攻（近15日站MA5≥11天 + MA5_5d涨≥1.5%）
    B. 大盘单边大跌日（index_chg < -0.8%）中，MA5几乎未破的强势个股
       条件：昨日大盘跌>0.8%，个股昨日偏MA5跌幅 < -0.5%（未真正破线）
    """
    import pandas as pd

    found = {}  # code → entry

    for r in results:
        info_r = r["info"]
        ind_r  = r["ind"]
        df_r   = r.get("df")
        if df_r is None:
            continue
        if info_r.get("pool") not in ("core", "candidate"):
            continue
        if info_r.get("signal_3d") not in ("★★★", "★★☆"):
            continue

        df2 = df_r.copy()
        df2["ma5"]  = df2["close"].rolling(5).mean()
        df2["ma20"] = df2["close"].rolling(20).mean()
        df2["ma60"] = df2["close"].rolling(60).mean()
        df2["dev_ma5"] = (df2["close"] - df2["ma5"]) / df2["ma5"] * 100
        df2 = df2.dropna()
        if len(df2) < 15:
            continue

        tail15 = df2.tail(15)
        above_count  = int(tail15["close"].gt(tail15["ma5"]).sum())
        dev_min      = float(tail15["dev_ma5"].min())
        dev_now      = float(df2["dev_ma5"].iloc[-1])
        ma5_now      = float(df2["ma5"].iloc[-1])
        ma60_slope   = float(df2["ma60"].iloc[-1] / df2["ma60"].iloc[-5] - 1) * 100
        ma5_slope_5d = float(df2["ma5"].iloc[-1] / df2["ma5"].iloc[-5] - 1) * 100

        # ── A. 持续贴线上攻 ──
        is_hug = (
            above_count >= 11 and
            ma5_slope_5d >= 1.5 and
            0 <= dev_now <= 15 and
            dev_min >= -2.0 and
            ma60_slope >= 0
        )

        # ── B. 大跌日不破MA5的强势股 ──
        is_resilient = False
        if index_chg_yesterday < -0.8 and len(df2) >= 2:
            # 昨日（倒数第2行，今日为最新行）
            yesterday_row = df2.iloc[-2]
            yesterday_dev = float(yesterday_row["dev_ma5"])
            yesterday_chg = float(
                (yesterday_row["close"] - df2.iloc[-3]["close"]) / df2.iloc[-3]["close"] * 100
            ) if len(df2) >= 3 else 0
            is_resilient = (
                yesterday_dev >= -0.5 and        # 昨日未破MA5（允许轻贴-0.5%以内）
                yesterday_chg > index_chg_yesterday + 0.5 and  # 显著跑赢大盘
                ma60_slope >= 0                  # 大趋势向上
            )

        if not (is_hug or is_resilient):
            continue

        # 当前偏离MA5 > 15% 说明今日大涨后过热，不重复（等后续自然回踩）
        if dev_now > 15:
            continue

        tag = "resilient" if (is_resilient and not is_hug) else "hug"
        found[r["code"]] = {
            **r,
            "above_count":   above_count,
            "ma5_slope_5d":  round(ma5_slope_5d, 2),
            "dev_now":       round(dev_now, 2),
            "ma5_hug_tag":   tag,
        }

    return sorted(found.values(), key=lambda x: (
        x["info"].get("pool") != "core",
        x["info"].get("signal_3d") != "★★★",
        -x["ma5_slope_5d"],
    ))


def _is_coiling(r: dict) -> bool:
    """蓄势待发：中期结构健康（MA60向上、价格站稳MA20、RSI未过热），但缺短期确认信号。"""
    ind = r.get("ind", {})
    return (
        r["signal"] == "HOLD"
        and r["score"] >= 3
        and ind.get("ma60_slope_5d", 0.0) > 0.3
        and ind.get("close", 0) > ind.get("ma20", 0)
        and ind.get("rsi", 50) < 72
    )


def _coiling_trigger_hint(ind: dict) -> str:
    """自动分析缺哪个短期信号才能触发 BUY_STRONG。"""
    hints = []
    close, ma5, ma20, ma60 = ind["close"], ind["ma5"], ind["ma20"], ind["ma60"]
    cross = ind.get("cross", "")
    if cross != "golden":
        if ind.get("dif", 0) < ind.get("dea", 0):
            hints.append("MACD金叉")
        elif not (ind.get("macd_bar", 0) > ind.get("macd_bar_p", 0)):
            hints.append("红柱扩张")
    if not (close > ma5 > ma20 > ma60):
        hints.append("突破MA5(多头排列)")
    if ind.get("vol_ratio", 1.0) < 1.5:
        hints.append("放量>1.5x")
    return "⏳ 等待触发：" + " | ".join(hints[:3]) if hints else "⏳ 触发条件接近满足"


def _coiling_line(r: dict) -> str:
    ind        = r["ind"]
    info       = r["info"]
    close      = ind["close"]
    ma60_slope = ind.get("ma60_slope_5d", 0.0)
    flow       = ind.get("main_force_flow", 0.0)
    text       = "、".join(r["reasons"]) if r["reasons"] else "技术面改善中"
    flow_str   = (f"  主力{'↑' if flow > 0 else '↓'}{abs(flow):.1f}亿" if abs(flow) >= 0.3 else "")
    trigger    = _coiling_trigger_hint(ind)
    return (
        f"🎯 **{r['name']}**（{r['code']}）"
        f"｜{info.get('signal_3d','—')} {info.get('theme','')}  评分{_score100(r['score'])}/100\n"
        f"  价格 **{close:.2f}**  MA60↑+{ma60_slope:.1f}%/5日"
        f"  {_rsi_tag(ind['rsi'])}{flow_str}\n"
        f"  📋 {text}\n"
        f"  {trigger}"
    )


def build_card(results: list[dict], regime: str, ts: str,
               ctx: dict | None = None, reversals: list[dict] | None = None,
               overheat_map: dict[str, dict] | None = None,
               triple_watch: list[dict] | None = None,
               ma5_hug_watch: list[dict] | None = None) -> list[dict]:
    """返回卡片列表：[卡片1-操作信号, 卡片2-候观雷达]"""
    color       = REGIME_COLOR.get(regime, "blue")
    regime_desc = REGIME_LABEL.get(regime, regime)
    max_pos     = REGIME_STOCK_MAX.get(regime, 0)

    # 情绪过热追加门控
    senti    = (ctx or {}).get("sentiment")
    rotation = (ctx or {}).get("rotation")
    timing_blocked = senti is not None and senti.timing_block

    if max_pos == 0:
        gate_tip = f"⚠️ {regime} 风险市 — 个股买入全部禁用，仅处理止损信号"
    elif timing_blocked and rotation is not None:
        gate_tip = (
            f"🔥 **{regime} {regime_desc}**  |  {rotation.dim_line()}\n"
            f"**{rotation.label()}**  —  暂停新买，见下方攻守切换建议"
        )
    elif timing_blocked:
        gate_tip = f"🔥 {regime} {regime_desc} ｜ 情绪过热 — 暂停新买，持仓观察"
    else:
        gate_tip = f"{regime} {regime_desc} ｜ 个股单仓上限 **{max_pos}%**"

    sell_stops = [r for r in results if r["signal"] == "SELL_STOP"]
    reduces    = [r for r in results if r["signal"] == "REDUCE"]
    buys_s     = [r for r in results if r["signal"] == "BUY_STRONG"]
    buys_w     = [r for r in results if r["signal"] == "BUY_WATCH"]
    _near_all  = [r for r in results if r["signal"] == "HOLD" and r["score"] >= 2]
    coiling    = [r for r in _near_all if _is_coiling(r)]
    near_buys  = [r for r in _near_all if not _is_coiling(r)]
    holds      = [r for r in results if r["signal"] == "HOLD" and 0 <= r["score"] < 2]
    weak_holds = [r for r in results if r["signal"] == "HOLD" and r["score"] < 0]

    # 情绪过热时：BUY_WATCH 降级为观察，BUY_STRONG 保留置顶（加警示标题）
    if timing_blocked:
        near_buys = sorted(near_buys + buys_w, key=lambda r: -r["score"])
        buys_w    = []

    def _sec(title: str, items: list[dict]) -> list[dict]:
        if not items:
            return []
        body = "\n\n".join(_signal_line(r) for r in items)
        return [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**{title}**\n{body}"}},
            {"tag": "hr"},
        ]

    def _compact_sec(title: str, items: list[dict]) -> list[dict]:
        """技术中性/弱势持观：每股一行，评分格栅+关键指标+趋势标，按评分降序。"""
        if not items:
            return []

        def _mini_bar(s100: int) -> str:
            """3格迷你评分条，均匀分段：<34=░░░ 34-50=█░░ 51-66=██░ 67+=███"""
            if s100 >= 67:   return "███"
            if s100 >= 51:   return "██░"
            if s100 >= 34:   return "█░░"
            return "░░░"

        sorted_items = sorted(items, key=lambda r: r["score"], reverse=True)
        lines = []
        for r in sorted_items:
            ind    = r["ind"]
            s100   = _score100(r["score"])
            rsi    = ind.get("rsi", 50)
            chg3   = ind.get("chg3", 0.0)
            close  = ind.get("close", 0)
            ma20   = ind.get("ma20", 0)
            flow   = ind.get("main_force_flow", 0.0)

            bar    = _mini_bar(s100)
            ma20_pct = (close / ma20 - 1) * 100 if ma20 > 0 else 0
            ma20_tag = f"↑MA20+{ma20_pct:.1f}%" if close >= ma20 else f"↓MA20{ma20_pct:.1f}%"
            chg3_tag = f"▲{chg3:.1f}%" if chg3 >= 0 else f"▼{abs(chg3):.1f}%"
            rsi_tag  = _rsi_tag(rsi)
            flow_tag = (f" 主力{'↑' if flow > 0 else '↓'}{abs(flow):.1f}亿" if abs(flow) >= 0.3 else "")

            lines.append(
                f"`{bar}` **{r['name']}**（{r['code']}）{s100}/100"
                f"  {rsi_tag}  {ma20_tag}  {chg3_tag}{flow_tag}"
            )

        body = "\n".join(lines)
        return [
            {"tag": "div", "text": {"tag": "lark_md",
                                     "content": f"**{title}**（{len(items)}只）\n{body}"}},
            {"tag": "hr"},
        ]

    def _coiling_sec(items: list[dict]) -> list[dict]:
        if not items:
            return []
        body = "\n\n".join(_coiling_line(r) for r in items)
        return [
            {"tag": "div", "text": {"tag": "lark_md",
                                     "content": f"**🎯 ━━ 蓄势待发 · 买点前夕 ━━**（{len(items)}只）\n{body}"}},
            {"tag": "hr"},
        ]

    elements: list[dict] = []

    # ── 宏观摘要 ──────────────────────────────────────────────────────────
    if ctx:
        macro_text = _fmt_macro_section(ctx)
        if macro_text:
            elements += [
                {"tag": "div", "text": {"tag": "lark_md", "content": macro_text}},
                {"tag": "hr"},
            ]

    # ── 跨模块方向暴露预警 ─────────────────────────────────────────────────
    _exp_warns = (ctx or {}).get("exposure_warnings", [])
    if _exp_warns:
        _ew_text = "**🚨 方向集中度预警（个股+ETF合计）**\n" + "\n".join(_exp_warns)
        elements += [
            {"tag": "div", "text": {"tag": "lark_md", "content": _ew_text}},
            {"tag": "hr"},
        ]

    # ── 集群过热警告 ──────────────────────────────────────────────────────
    if overheat_map:
        oh_text = _fmt_theme_overheat_section(overheat_map)
        if oh_text:
            elements += [
                {"tag": "div", "text": {"tag": "lark_md", "content": oh_text}},
                {"tag": "hr"},
            ]

    # ── 板块恐慌 → 高低切换提示 ───────────────────────────────────────────
    panic_clusters = (ctx or {}).get("panic_clusters", [])
    offense_surge  = (ctx or {}).get("offense_surge", [])
    if panic_clusters:
        panic_text = fmt_cluster_panic_block(panic_clusters)
        elements += [
            {"tag": "div", "text": {"tag": "lark_md", "content": panic_text}},
            {"tag": "hr"},
        ]
        defensive_list = fetch_and_score_defensive()
        rotation_text  = _fmt_defensive_rotation(defensive_list, rotation=rotation)
        elements += [
            {"tag": "div", "text": {"tag": "lark_md", "content": rotation_text}},
            {"tag": "hr"},
        ]
    elif offense_surge:
        surge_text = fmt_offense_surge_block(offense_surge)
        elements += [
            {"tag": "div", "text": {"tag": "lark_md", "content": surge_text}},
            {"tag": "hr"},
        ]

    elements += [
        {"tag": "div", "text": {"tag": "lark_md", "content": gate_tip}},
        {"tag": "hr"},
    ]

    # ── 强买入信号 —— 机制行下方 ──────────────────────────────────────────
    if buys_s:
        buy_title = (
            "🚀 ━━ 强买入信号（⚠️情绪过热，等回落确认后入场）━━"
            if timing_blocked else
            "🚀 ━━ 强买入信号 ━━"
        )
        elements += _sec(buy_title, buys_s)

    # ── 攻守切换区块（情绪过热时插入）─────────────────────────────────────
    if timing_blocked:
        log.info("情绪过热，扫描防御轮动池...")
        defensive_list = fetch_and_score_defensive()
        rotation_text  = _fmt_defensive_rotation(defensive_list, rotation=rotation)
        elements += [
            {"tag": "div", "text": {"tag": "lark_md", "content": rotation_text}},
            {"tag": "hr"},
        ]

    elements += _coiling_sec(coiling)
    elements += _sec("🔵 ━━ 观察建仓 · 今日机会 ━━", buys_w)
    elements += _sec("🟡 接近买入线（可小仓关注）", near_buys)
    elements += _sec("🔴 止损（立即执行）", sell_stops)
    elements += _sec("📉 减仓 / 回避", reduces)
    elements += _compact_sec("⚪ 技术中性（持仓不动）", holds)
    elements += _compact_sec("🟠 弱势持观（偏弱未破位）", weak_holds)

    if not (sell_stops or reduces or buys_s or buys_w or coiling or near_buys or holds or weak_holds):
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                                                  "content": "暂无有效信号"}})

    # ── 近期信号战绩（飞轮记分牌：让你知道系统最近准不准）─────────────────────
    try:
        _snap_files = sorted(Path(__file__).parent.glob("logs/stock_timing_*.json"))
        _today_snap_map = {r["code"]: r.get("close", 0) for r in results}
        _scorecard_lines = []
        if len(_snap_files) >= 4:
            # 取5-7天前的快照
            _old_file = _snap_files[-min(6, len(_snap_files))]
            _old_data = json.loads(_old_file.read_text(encoding="utf-8"))
            _old_buys = [r for r in _old_data if r.get("signal") in ("BUY_STRONG", "BUY_WATCH")]
            _tracked = []
            for ob in _old_buys:
                c = ob["code"]
                old_p = ob.get("close", 0)
                new_p = _today_snap_map.get(c, 0)
                if old_p > 0 and new_p > 0:
                    _tracked.append({"name": ob["name"], "ret": (new_p/old_p-1)*100})
            if _tracked:
                _wins = sum(1 for t in _tracked if t["ret"] > 0)
                _avg = sum(t["ret"] for t in _tracked) / len(_tracked)
                _days = _old_file.stem.replace("stock_timing_", "")
                _scorecard_lines.append(f"**📋 近期战绩**（{_days}至今，{len(_tracked)}笔）")
                _scorecard_lines.append(f"  胜率 **{_wins/len(_tracked)*100:.0f}%**  均收 **{_avg:+.1f}%**")
                _sorted = sorted(_tracked, key=lambda x: -x["ret"])
                if len(_sorted) >= 2:
                    _scorecard_lines.append(f"  最佳: {_sorted[0]['name']}{_sorted[0]['ret']:+.1f}%  最差: {_sorted[-1]['name']}{_sorted[-1]['ret']:+.1f}%")
        if _scorecard_lines:
            elements.append({"tag": "hr"})
            elements.append({"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(_scorecard_lines)}})
    except Exception:
        pass

    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text",
                      "content": "个股池择时 ｜ 止损触及须当日执行 ｜ 单股仓位≤集群上限"}],
    })

    card1 = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text",
                           "content": f"📊 个股择时·操作信号 ｜ {ts}"},
                "template": color,
            },
            "elements": elements,
        },
    }

    # ── 卡片2：候观雷达 ───────────────────────────────────────────────────
    el2: list[dict] = []

    # 标题说明
    el2.append({"tag": "div", "text": {"tag": "lark_md",
        "content": f"**🔭 候观雷达 ｜ {regime_desc} ｜ {ts}**\n强势股尚未回踩 / 底部待确认，等信号再上车"}})
    el2.append({"tag": "hr"})

    # ── 三线共振候观区 + MA5贴线上攻候观区 ──────────────────────────────
    watch_lines = []
    if triple_watch:
        watch_lines += [_triple_align_watch_line(r) for r in triple_watch]
    if ma5_hug_watch:
        watch_lines += [_ma5_hug_watch_line(r) for r in ma5_hug_watch]
    if watch_lines:
        el2 += [
            {"tag": "div", "text": {"tag": "lark_md",
                                     "content": "**📌 ━━ 强势股候观区（等回踩再买）━━**\n" + "\n\n".join(watch_lines)}},
            {"tag": "hr"},
        ]

    # ── 底部反转候选 ────────────────────────────────────────────────────
    if reversals:
        rev_body = "\n\n".join(_reversal_line(r) for r in reversals)
        el2 += [
            {"tag": "div", "text": {"tag": "lark_md",
                                     "content": f"**🔄 ━━ 底部反转候选（MACD/Boll/周月K多维确认）━━**\n{rev_body}"}},
            {"tag": "hr"},
        ]

    if not watch_lines and not reversals:
        el2.append({"tag": "div", "text": {"tag": "lark_md", "content": "暂无候观/反转标的"}})

    el2.append({
        "tag": "note",
        "elements": [{"tag": "plain_text",
                      "content": "候观区仅供参考，需等日线信号确认后再介入 ｜ 反转候选须等量价验证"}],
    })

    card2 = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text",
                           "content": f"🔭 候观雷达·强势股跟踪 ｜ {ts}"},
                "template": "wathet",
            },
            "elements": el2,
        },
    }

    return [card1, card2]


# ─────────────────────────────────────────────────────────────────────────────
# 推送
# ─────────────────────────────────────────────────────────────────────────────
def push_feishu(cards: list[dict] | dict, dry: bool = False) -> None:
    if isinstance(cards, dict):
        cards = [cards]
    if dry:
        import pprint
        for i, card in enumerate(cards, 1):
            log.info(f"[dry-run] 卡片{i} 预览:")
            for el in card["card"]["elements"]:
                if el.get("tag") == "div":
                    txt = el.get("text", {}).get("content", "")
                    if txt:
                        print(txt[:400])
                        print()
        return
    for card in cards:
        _post_card(card, FEISHU_WEBHOOKS)


# ─────────────────────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────────────────────
def main(dry: bool = False, force: bool = False) -> None:
    today = date.today().isoformat()
    ts    = datetime.now().strftime("%Y-%m-%d %H:%M")
    log.info(f"=== 个股择时 {ts} ===")

    if not force and not is_trading_day(today):
        log.info("今日非交易日，退出")
        return

    # ── 尾盘买点次日验证（仅首次运行09:45触发）────────────────────────────
    _now_hour = datetime.now().hour
    if _now_hour < 10:
        try:
            from datetime import timedelta as _td
            for _back in range(1, 4):
                _prev_d = (date.today() - _td(days=_back)).strftime("%Y%m%d")
                _tail_file = _DIR / "logs" / f"tail_detail_{_prev_d}.json"
                if _tail_file.exists():
                    _tail_data = json.loads(_tail_file.read_text(encoding="utf-8"))
                    _tail_sigs = _tail_data.get("signals", [])
                    if not _tail_sigs:
                        break
                    _tail_codes = [str(s.get("code", "")) for s in _tail_sigs if s.get("stars", 0) >= 2]
                    if _tail_codes:
                        log.info(f"尾盘验证: 检查{len(_tail_codes)}只昨日推荐标的开盘表现")
                        _tail_klines = fetch_klines_parallel(_tail_codes, count=5, max_workers=5)
                        _bad_opens: list[str] = []
                        for _tc in _tail_codes:
                            _tdf = _tail_klines.get(_tc)
                            if _tdf is not None and len(_tdf) >= 2:
                                _prev_close = float(_tdf["close"].iloc[-2])
                                _today_open = float(_tdf["open"].iloc[-1])
                                _gap = (_today_open / _prev_close - 1) * 100
                                if _gap < -1.5:
                                    _tname = next((s.get("name", _tc) for s in _tail_sigs if str(s.get("code")) == _tc), _tc)
                                    _bad_opens.append(f"{_tname}({_tc}) 低开{_gap:.1f}%")
                                    log.warning(f"  尾盘验证: {_tname} 今日低开{_gap:.1f}%")
                        if _bad_opens:
                            _alert_card = {
                                "msg_type": "interactive",
                                "card": {
                                    "header": {"title": {"tag": "plain_text",
                                        "content": f"⚠️ 昨日尾盘买点次日验证 ｜ {ts}"}, "template": "red"},
                                    "elements": [{"tag": "div", "text": {"tag": "lark_md",
                                        "content": "**昨日尾盘推荐标的今日低开预警**\n" + "\n".join(f"🔴 {b}" for b in _bad_opens)
                                        + "\n\n建议：低开>1.5%属于追尾失败，考虑早盘止损或等反抽减仓"}}],
                                },
                            }
                            _post_card(_alert_card, FEISHU_WEBHOOKS)
                    break
        except Exception as e:
            log.debug(f"尾盘验证异常: {e}")

    regime = current_regime()
    log.info(f"机制: {regime}")

    log.info("拉取宏观上下文（大盘/北向/情绪/板块）...")
    ctx = fetch_macro_context()
    senti = ctx.get("sentiment")
    if senti:
        log.info(
            f"情绪: {senti.level} {senti.score:.0f}分  "
            f"涨/跌停={senti.limit_up}/{senti.limit_down}  "
            f"北向今日={ctx.get('north_today', 'N/A')}亿"
        )

    log.info("获取个股主力资金流向...")
    stock_codes = list(STOCK_UNIVERSE.keys())
    stock_flows = get_etf_main_force_flow(stock_codes)
    flow_summary = {k: v for k, v in stock_flows.items() if abs(v) >= 0.3}
    log.info(f"个股主力流向（有效值）: {flow_summary}")

    log.info(f"并行抓取 {len(stock_codes)} 只个股 K 线...")
    all_klines = fetch_klines_parallel(stock_codes, count=300)
    ok_count = sum(1 for v in all_klines.values() if v is not None)
    log.info(f"K 线抓取完成：{ok_count}/{len(stock_codes)} 成功")

    # ── 前瞻验证闭环：读昨日预判→比对今日实际板块涨跌→记录准确度 ──────────────
    try:
        from datetime import timedelta
        _yesterday = (datetime.today() - timedelta(days=1)).strftime("%Y-%m-%d")
        _pred_file = Path(__file__).parent / "state" / f"forward_pred_{_yesterday}.json"
        if _pred_file.exists():
            _pred = json.loads(_pred_file.read_text(encoding="utf-8"))
            # 算今日各cluster均涨跌
            _today_chgs: dict[str, list[float]] = {}
            for code, df in all_klines.items():
                if df is not None and len(df) >= 2:
                    chg = (float(df["close"].iloc[-1]) / float(df["close"].iloc[-2]) - 1) * 100
                    c = STOCK_UNIVERSE.get(code, {}).get("cluster", "")
                    if c:
                        _today_chgs.setdefault(c, []).append(chg)
            _cluster_avg = {c: sum(v)/len(v) for c, v in _today_chgs.items() if v}
            # 验证
            _hits, _total = 0, 0
            if _pred.get("us10y_regime") == "DEFENSIVE":
                _total += 1
                _tech = sum(_cluster_avg.get(c, 0) for c in ("optics","semicon","pcb")) / 3
                _def = sum(_cluster_avg.get(c, 0) for c in ("commodity","finance")) / 2
                if _def > _tech:
                    _hits += 1
                    log.info(f"前瞻验证✅ 美债DEFENSIVE：防御({_def:+.1f}%)>科技({_tech:+.1f}%)")
                else:
                    log.info(f"前瞻验证❌ 美债DEFENSIVE：防御({_def:+.1f}%)<科技({_tech:+.1f}%)")
            elif _pred.get("us10y_regime") == "TECH_FAVOR":
                _total += 1
                _tech = sum(_cluster_avg.get(c, 0) for c in ("optics","semicon","pcb")) / 3
                _def = sum(_cluster_avg.get(c, 0) for c in ("commodity","finance")) / 2
                if _tech > _def:
                    _hits += 1
                    log.info(f"前瞻验证✅ 美债TECH_FAVOR：科技({_tech:+.1f}%)>防御({_def:+.1f}%)")
                else:
                    log.info(f"前瞻验证❌ 美债TECH_FAVOR：科技({_tech:+.1f}%)<防御({_def:+.1f}%)")
            if _total > 0:
                log.info(f"前瞻验证摘要: {_hits}/{_total} 命中")
    except Exception as _e:
        log.debug(f"前瞻验证: {_e}")

    # 用 K 线最后两行计算今日涨跌幅，做集群恐慌检测（覆盖 fetch_macro_context 的 sector_snaps 结果）
    try:
        kline_chg_pcts: dict[str, float] = {}
        for code, df in all_klines.items():
            if df is not None and len(df) >= 2:
                prev, cur = float(df["close"].iloc[-2]), float(df["close"].iloc[-1])
                if prev > 0:
                    kline_chg_pcts[code] = (cur - prev) / prev * 100
        kline_panic = detect_cluster_panic(STOCK_UNIVERSE, kline_chg_pcts)
        if kline_panic:
            ctx["panic_clusters"] = kline_panic
            log.info(f"板块恐慌: {', '.join(f'{n}({v:+.1f}%)' for n,v in kline_panic)}")
        # 构造恐慌 cluster 英文key集合（供恐慌反转买点使用）
        _panic_en_keys: set[str] = set()
        _cluster_avgs: dict[str, list[float]] = {}
        for code, info in STOCK_UNIVERSE.items():
            c = info.get("cluster", "")
            chg = kline_chg_pcts.get(code)
            if c and chg is not None:
                _cluster_avgs.setdefault(c, []).append(chg)
        for c, chgs in _cluster_avgs.items():
            if sum(chgs) / len(chgs) <= -2.0:
                _panic_en_keys.add(c)
        ctx["panic_cluster_keys"] = _panic_en_keys
        kline_surge = detect_offense_surge(STOCK_UNIVERSE, kline_chg_pcts)
        if kline_surge:
            ctx["offense_surge"] = kline_surge
            log.info(f"进攻信号: {', '.join(f'{n}({v:+.1f}%)' for n,v in kline_surge)}")
    except Exception as e:
        log.debug(f"K线集群恐慌检测: {e}")

    results:   list[dict] = []
    reversals: list[dict] = []

    # ── 读取 cluster 健康度（周度自动复盘结果，连续负超额的赛道被降级）────────
    _degraded_clusters: set[str] = set()
    _health_file = Path(__file__).parent / "state" / "cluster_health.json"
    if _health_file.exists():
        try:
            _health = json.loads(_health_file.read_text(encoding="utf-8"))
            _degraded_clusters = {c for c, h in _health.items() if h.get("status") == "degraded"}
            if _degraded_clusters:
                log.info(f"周度降级赛道: {_degraded_clusters}")
        except Exception:
            pass

    # ── 板块动量排名分档（决定仓位权重：强赛道满配、中性标准、弱势零配）────────
    _cluster_trend = ctx.get("cluster_trend", {})
    _sorted_clusters = sorted(
        _cluster_trend.items(),
        key=lambda x: x[1] if x[1] is not None else 0.0,
        reverse=True,
    )
    n_clusters = len(_sorted_clusters)
    _cluster_rank: dict[str, str] = {}  # cluster -> 'strong'/'neutral'/'weak'
    if n_clusters > 0:
        cut_top = n_clusters // 3
        cut_bot = n_clusters - n_clusters // 3
        for i, (cname, slope) in enumerate(_sorted_clusters):
            if i < cut_top:
                _cluster_rank[cname] = "strong"
            elif i >= cut_bot:
                _cluster_rank[cname] = "weak"
            else:
                _cluster_rank[cname] = "neutral"

    # ── 读取盘中预警快照（用于信号一致性标注）────────────────────────────────
    _intraday_snap_path = Path(__file__).parent / "state" / f"intraday_alerts_{datetime.today().strftime('%Y-%m-%d')}.json"
    _intraday_alerts: dict[str, str] = {}
    if _intraday_snap_path.exists():
        try:
            _intraday_alerts = json.loads(_intraday_snap_path.read_text(encoding="utf-8"))
            log.info(f"盘中预警快照: {len(_intraday_alerts)} 条")
        except Exception:
            pass

    # ── 前日止损未执行追踪（③执行闭环）─────────────────────────────────────
    _pending_stops: set[str] = set()
    _prev_snaps = sorted(Path(__file__).parent.glob("logs/stock_timing_*.json"))
    if len(_prev_snaps) >= 2:
        try:
            _prev_data = json.loads(_prev_snaps[-2].read_text(encoding="utf-8"))
            _pending_stops = {r["code"] for r in _prev_data if r.get("signal") == "SELL_STOP"}
            if _pending_stops:
                log.info(f"前日止损追踪: {len(_pending_stops)} 只待确认执行")
        except Exception:
            pass

    for code, info in STOCK_UNIVERSE.items():
        df = all_klines.get(code)
        if df is None:
            log.warning(f"{code} {info['name']} K线获取失败")
            continue

        ind = compute_indicators(df)
        if ind is None:
            log.warning(f"{code} {info['name']} 数据不足({len(df)}条)")
            continue

        _today_date_str = datetime.today().strftime("%Y-%m-%d")
        if ind.get("data_date", "")[:10] != _today_date_str:
            log.warning(f"{code} {info['name']} K线过期({ind.get('data_date')}≠{_today_date_str})，跳过")
            continue

        ind["main_force_flow"] = stock_flows.get(code, 0.0)

        # 注入板块ETF今日涨跌幅（用于板块联动验证）
        _cluster = info.get("cluster", "")
        _proxy_sym = _CLUSTER_PROXY_ETF.get(_cluster, "")
        _sec_snaps = ctx.get("sector_snaps", {})
        _cluster_chg = None
        if _proxy_sym:
            for _sn, _snap in _sec_snaps.items():
                if _SECTOR_ETFS.get(_sn) == _proxy_sym:
                    _cluster_chg = _snap.get("chg_pct")
                    break
            if _cluster_chg is None and _proxy_sym in kline_chg_pcts:
                _cluster_chg = kline_chg_pcts.get(_proxy_sym[2:])
        ind["cluster_etf_chg"] = _cluster_chg

        # 底部反转候选检测（传入info/ctx以启用政策+情绪维度）
        rev = check_reversal(ind, df, info=info, ctx=ctx)
        if rev is not None:
            reversals.append({
                "code":  code,
                "name":  info["name"],
                "info":  info,
                "ind":   ind,
                **rev,
            })

        score, reasons = score_stock(ind, info)

        # R4：禁止个股新多
        if regime == "R4" and score > 0:
            score = 0

        sig  = signal_type(score)

        # ── 买入信号质量过滤 ──────────────────────────────────────────────────
        pool = info.get("pool", "watch")

        # watch池禁止生成买入信号（回测: watch池BUY超额-4.41%）
        if sig in ("BUY_STRONG", "BUY_WATCH") and pool == "watch":
            sig = "HOLD"
            reasons.append("观察池(watch)，等待进入核心/候选池后再买入")

        # BUY_WATCH ★☆☆ 单维共振不够强，降级为 HOLD
        if sig == "BUY_WATCH" and info.get("signal_3d", "★☆☆") == "★☆☆":
            sig = "HOLD"
            reasons.append("单维共振(★☆☆)，等待信号强化")

        # BUY_STRONG score=6 且非★★★ → 降为BUY_WATCH
        # 回测(3月/29样本): ★★★/sc=6 T+5超额-4.61%胜率0%，★★☆/sc=6超额-6.09%
        # score=6往往对应单日强共振但缺乏持续性，非★★★时易高位假突破
        if sig == "BUY_STRONG" and score == SCORE_BUY_STRONG and info.get("signal_3d") != "★★★":
            sig = "BUY_WATCH"
            reasons.append(f"score={score}仅单日强共振非三维共振(★★★)，降为观察买")

        # ★★★ core BUY追高过滤：偏离MA20>=8%时降级为HOLD观察
        # 回测: ★★★ core BUY_WATCH T+1胜率仅29.6%，追高是核心原因
        if sig in ("BUY_STRONG", "BUY_WATCH") and pool == "core" and info.get("signal_3d") == "★★★":
            close = ind.get("close", 0)
            ma20  = ind.get("ma20", 0)
            if ma20 > 0 and (close - ma20) / ma20 * 100 >= 8:
                sig = "HOLD"
                dev_pct = (close - ma20) / ma20 * 100
                reasons.append(f"★★★core偏离MA20+{dev_pct:.0f}%，追高风险降为观察")

        # 高风险cluster买入门槛提升（回测: chemical/food_bev/consumer T+5超额-2~-5%，连续负向）
        cluster_now = info.get("cluster", "")
        if sig in ("BUY_STRONG", "BUY_WATCH") and cluster_now in _HIGH_RISK_CLUSTERS:
            if score < SCORE_BUY_STRONG + 1:
                sig = "HOLD"
                reasons.append(f"高风险板块({cluster_now})，买入阈值+1，等待更强信号")

        # 周度降级cluster拦截（飞轮自动反馈：连续2周负超额的赛道自动提高门槛）
        if sig in ("BUY_STRONG", "BUY_WATCH") and cluster_now in _degraded_clusters:
            sig = "HOLD"
            reasons.append(f"📉赛道降级({cluster_now})：近期连续负超额，暂停买入等待恢复")

        # ── 动态板块动量门（#2 行业开关层）──────────────────────────────────
        # 组合回测：买入超额几乎全来自强势赛道，逆势赛道持续负超额。
        # 用板块代理ETF的MA20斜率实时判断，下行赛道否决买入，走弱赛道抬门槛。
        if sig in ("BUY_STRONG", "BUY_WATCH"):
            _sector_slope = ctx.get("cluster_trend", {}).get(cluster_now)
            if _sector_slope is not None:
                if _sector_slope < SECTOR_MOM_VETO_SLOPE:
                    sig = "HOLD"
                    reasons.append(f"板块趋势下行(MA20斜率{_sector_slope:+.1f}%)，逆势暂不买入")
                elif _sector_slope < SECTOR_MOM_WEAK_SLOPE and score < SCORE_BUY_STRONG:
                    sig = "HOLD"
                    reasons.append(f"板块走弱(MA20斜率{_sector_slope:+.1f}%)，非强共振降为观察")

        # MA60陡峭上行+RSI高位 = 动量衰竭区（回测: 德业+源杰+罗博特科等MA60>3.5%+RSI>68均大亏-11~-27%）
        ma60_slope = ind.get("ma60_slope_5d", 0.0)
        rsi_now = ind.get("rsi", 50)
        if sig in ("BUY_STRONG", "BUY_WATCH") and ma60_slope > 3.5 and rsi_now > 68:
            sig = "HOLD"
            reasons.append(f"MA60陡峭+{ma60_slope:.1f}%且RSI={rsi_now:.0f}，动量衰竭区不追高")

        # ── 减仓/止损过滤 ──────────────────────────────────────────────────
        # ② 绝对止损闸门（最高优先级，不可被任何回测降级规则覆盖）
        # A股闷杀特征：一字跌停/连续阴跌/业绩暴雷。触发即无条件清仓，保命底线。
        hard_stop = False
        _chg1 = ind.get("chg1", 0.0)
        _chg3 = ind.get("chg3", 0.0)
        _consec_down = ind.get("consec_down_days", 0)
        _hard_reason = ""
        if _chg1 <= HARD_STOP_DAY_DROP:
            hard_stop = True; _hard_reason = f"单日暴跌{_chg1:.1f}%(近跌停)"
        elif _chg3 <= HARD_STOP_3D_DROP:
            hard_stop = True; _hard_reason = f"3日累计暴跌{_chg3:.1f}%(连续闷杀)"
        elif _consec_down >= HARD_STOP_CONSEC_DOWN:
            hard_stop = True; _hard_reason = f"连续收阴{_consec_down}日(趋势破位)"
        if hard_stop:
            sig = "SELL_STOP"
            reasons.append(f"🛑绝对止损闸门：{_hard_reason}，无条件清仓(不可降级)")

        # SELL_STOP 降级规则（回测: SELL_STOP后T+5超额+3.34%，大量误杀）
        # 仅对"非硬止损"触发的普通 SELL_STOP 生效；硬止损绝不降级。
        rsi_now = ind.get("rsi", 50)
        consec = ind.get("consec_below_ma60", 0)
        cluster_now = info.get("cluster", "")
        if sig == "SELL_STOP" and not hard_stop:
            # watch池禁止SELL_STOP（回测: watch/★☆☆ SELL_STOP后T+5超额+4.21%）
            if pool == "watch":
                sig = "REDUCE"
                reasons.append("观察池暂缓止损，降为减仓（watch池止损成本高）")
            elif pool == "core" and info.get("signal_3d") == "★★★":
                # 3月29样本: ★★★/core sc=-4超额+5.06%, sc=-5超额+4.75%, sc=-6超额+7.66%
                # ★★★核心池全档位SELL_STOP均为卖飞，取消score门槛限制
                sig = "REDUCE"
                reasons.append("★★★核心池暂缓止损（各分数档回测均为卖飞），降为减仓观察")
            elif rsi_now < 25:
                sig = "REDUCE"
                reasons.append(f"RSI深度超卖({rsi_now:.0f}<25)，暂缓止损降为减仓")
            elif consec < 4:
                sig = "REDUCE"
                reasons.append(f"MA60跌破{consec}日未满4日确认，降级为减仓")
            # 止损延迟确认：近3日均价仍在止损价上方 → 假破位，降级观察
            elif len(df) >= 4:
                recent_3d_avg = df["close"].values[-3:].mean()
                stop_ref = ind["close"] * _CLUSTER_STOP_MULT.get(cluster_now, 0.95)
                if recent_3d_avg > stop_ref:
                    sig = "REDUCE"
                    reasons.append(f"3日均价{recent_3d_avg:.2f}仍高于止损{stop_ref:.2f}，疑似假破位")

        # ── 恐慌反转买入（④强赛道恐慌日逆向策略）─────────────────────────────
        # 数据验证：强赛道恐慌日 T+3 均收+5.36% 胜率67%，比正常买入更优。
        # 条件：信号偏弱(HOLD/REDUCE) + 强赛道今日恐慌 + RSI超卖 + MA60仍向上
        # 排除：板块趋势已明确下行(mom_composite<VETO)时不抄底，防止下降趋势中逆势
        _panic_keys = ctx.get("panic_cluster_keys", set())
        _PANIC_STRONG = {"optics", "semicon", "pcb", "industrial_auto"}
        _sector_mom = ctx.get("cluster_trend", {}).get(cluster_now)
        if (sig in ("HOLD", "REDUCE")
            and not hard_stop
            and cluster_now in _panic_keys
            and cluster_now in _PANIC_STRONG
            and ind.get("rsi", 50) <= 40
            and ind.get("ma60_slope_5d", 0) > 0
            and pool != "watch"
            and (_sector_mom is None or _sector_mom >= SECTOR_MOM_VETO_SLOPE)):
            sig = "BUY_WATCH"
            reasons.append(f"🔥恐慌反转：强赛道({cluster_now})恐慌日+RSI{ind['rsi']:.0f}超卖+MA60向上，逆向买入")
            if regime != "R1":
                reasons.append(f"⚠️当前{regime}(非牛市)，此为防御中的逆向机会，建议半仓试探")

        # ── 止盈兑现（浮盈丰厚+动量衰减→主动保利润）──────────────────────────
        # 强赛道趋势强但波动大，3日涨>12%+RSI>65时主动兑现部分利润，防止回吐。
        _chg3_val = ind.get("chg3", 0.0)
        if (sig == "HOLD"
            and _chg3_val >= 12.0
            and ind.get("rsi", 50) > 65
            and pool in ("core", "candidate")):
            sig = "REDUCE"
            reasons.append(f"💰止盈兑现：3日涨{_chg3_val:+.1f}%+RSI{ind['rsi']:.0f}，主动保利润")

        _cur_cluster = info.get("cluster", "")
        _cur_rank = _cluster_rank.get(_cur_cluster, "neutral")
        pos  = calc_position(sig, info, regime, sector_rank=_cur_rank)
        stop = calc_stop(ind, info)

        # 盘中信号一致性对比
        _intra = _intraday_alerts.get(code)
        if _intra and _intra in ("REDUCE", "STOP_LOSS", "WARN_LOSS") and sig in ("BUY_STRONG", "BUY_WATCH", "HOLD"):
            reasons.append(f"⚡盘中曾发{_intra}，收盘信号{sig}方向变化，谨慎对待")

        # 前日止损未执行升级提醒（③执行闭环）
        if code in _pending_stops and sig != "SELL_STOP":
            reasons.append("🚨前日止损未执行！如仍持有请立即处理（信号已转为非止损，但风险未消除）")

        results.append({
            "code":         code,
            "name":         info["name"],
            "info":         info,
            "score":        score,
            "signal":       sig,
            "position_pct": pos,
            "stop_price":   stop,
            "intraday_alert": _intra,
            "reasons":      reasons,
            "ind":          ind,
            "df":           df,
        })
        log.info(
            f"  {code} {info['name']:6s}  "
            f"收{ind['close']:.2f} RSI={ind['rsi']:.0f} 量比={ind['vol_ratio']:.1f}x  "
            f"得分{score:+d} → {sig}  仓位={pos}%  [{ind['data_date']}]"
        )

    if not results:
        log.info("无有效结果，跳过推送")
        return

    results.sort(key=lambda r: -r["score"])

    # ── 集群/主题过热检测 ──────────────────────────────────────────────────
    overheat_map = calc_theme_overheat(results)

    # 二次过滤：过热集群中的买入信号降级
    for r in results:
        cluster = r["info"].get("cluster", "")
        if cluster not in overheat_map:
            continue
        oh = overheat_map[cluster]
        sig = r["signal"]
        if sig not in ("BUY_STRONG", "BUY_WATCH"):
            continue

        consec_self = r["ind"].get("consec_up_days", 0)
        dev_ma5     = r["ind"].get("ma5_dev_pct", 0.0)

        # DANGER: 集群严重过热 + 自身连涨≥3 → 降为HOLD观察
        if oh["level"] == "DANGER" and consec_self >= 3:
            r["signal"] = "HOLD"
            r["position_pct"] = 0
            r["reasons"].append(
                f"集群过热({oh['cluster_label']})+连涨{consec_self}日，等回踩再介入"
            )
            log.info(f"  集群过热拦截: {r['name']} {cluster} DANGER→HOLD")
        # CAUTION: 集群偏热 + 自身偏离MA5≥4% → 强买降为观察
        elif oh["level"] == "CAUTION" and sig == "BUY_STRONG" and dev_ma5 >= 4:
            r["signal"] = "BUY_WATCH"
            _oh_cluster = r["info"].get("cluster", "")
            r["position_pct"] = calc_position("BUY_WATCH", r["info"], regime,
                                              sector_rank=_cluster_rank.get(_oh_cluster, "neutral"))
            r["reasons"].append(f"集群偏热+偏离MA5+{dev_ma5:.1f}%，强买降为观察")
            log.info(f"  集群偏热降级: {r['name']} {cluster} CAUTION→BUY_WATCH")
        # CAUTION + 自身连涨≥4 → 降为HOLD观察
        elif oh["level"] == "CAUTION" and consec_self >= 4:
            r["signal"] = "HOLD"
            r["position_pct"] = 0
            r["reasons"].append(f"集群偏热+自身连涨{consec_self}日，等回踩")
            log.info(f"  集群偏热拦截: {r['name']} {cluster} CAUTION→HOLD")

    danger_count = sum(1 for v in overheat_map.values() if v["level"] == "DANGER")
    caution_count = sum(1 for v in overheat_map.values() if v["level"] == "CAUTION")
    log.info(f"集群过热: DANGER={danger_count} CAUTION={caution_count} SAFE={len(overheat_map)-danger_count-caution_count}")

    # ── 同产业链集中度限制：同一cluster最多2只BUY信号同时存在 ──────────────────
    _MAX_BUY_PER_CLUSTER = 2
    cluster_buys: dict[str, list[dict]] = {}
    for r in results:
        if r["signal"] in ("BUY_STRONG", "BUY_WATCH"):
            c = r["info"].get("cluster", "")
            if c:
                cluster_buys.setdefault(c, []).append(r)
    for cluster, buys in cluster_buys.items():
        if len(buys) <= _MAX_BUY_PER_CLUSTER:
            continue
        buys.sort(key=lambda x: -x["score"])
        for r in buys[_MAX_BUY_PER_CLUSTER:]:
            r["signal"] = "HOLD"
            r["position_pct"] = 0
            r["reasons"].append(f"同链({cluster})已有{_MAX_BUY_PER_CLUSTER}只买入，集中度降级")
            log.info(f"  集中度限制: {r['name']} {cluster} → HOLD（同链已满）")

    # ── 全局每日买入上限：100万集中出击，每日最多3只新仓（每只20-30万有力度）
    _MAX_BUY_PER_DAY = 3
    all_buys = [r for r in results if r["signal"] in ("BUY_STRONG", "BUY_WATCH")]
    if len(all_buys) > _MAX_BUY_PER_DAY:
        # 排序：恐慌逆向优先 > score 高分
        def _buy_priority(r):
            is_panic = any("恐慌反转" in reason for reason in r.get("reasons", []))
            return (-int(is_panic), -r["score"])
        all_buys.sort(key=_buy_priority)
        for r in all_buys[_MAX_BUY_PER_DAY:]:
            r["signal"] = "HOLD"
            r["position_pct"] = 0
            r["reasons"].append(f"今日已有{_MAX_BUY_PER_DAY}只买入，资金集中不分散")
            log.info(f"  集中出击: {r['name']} → HOLD（每日上限{_MAX_BUY_PER_DAY}只）")

    # ── 跨模块方向暴露预警 ─────────────────────────────────────────────────
    exposure_warnings: list[str] = []
    try:
        _today_str = date.today().strftime("%Y%m%d")
        _etf_sig_file = _DIR / "logs" / f"signal_detail_{_today_str}.json"
        _cross_exposure: dict[str, float] = {}

        # 统计本模块买入信号的cluster仓位
        for r in results:
            if r["signal"] in ("BUY_STRONG", "BUY_WATCH"):
                c = r["info"].get("cluster", "")
                if c:
                    _cross_exposure[c] = _cross_exposure.get(c, 0) + r["position_pct"]

        # 叠加ETF系统今日买入信号的cluster仓位
        if _etf_sig_file.exists():
            _etf_data = json.loads(_etf_sig_file.read_text(encoding="utf-8"))
            for sig in _etf_data.get("signals", []):
                if sig.get("signal") in ("BUY_STRONG", "BUY_WATCH"):
                    from config import ETF_UNIVERSE
                    etf_info = ETF_UNIVERSE.get(str(sig.get("code", "")), {})
                    c = etf_info.get("cluster", "")
                    w = sig.get("weight_pct", 0) or 0
                    if c and w > 0:
                        _cross_exposure[c] = _cross_exposure.get(c, 0) + w

        for c, total in _cross_exposure.items():
            if total > 25:
                exposure_warnings.append(f"⚠️ {c} 合计仓位{total:.0f}%（个股+ETF），超25%集中度上限")
                log.warning(f"方向暴露: {c} = {total:.0f}%")

        # ETF↔个股信号方向冲突检测
        if _etf_sig_file.exists():
            _etf_data = json.loads(_etf_sig_file.read_text(encoding="utf-8"))
            from config import ETF_UNIVERSE
            _etf_cluster_signals: dict[str, str] = {}
            for sig in _etf_data.get("signals", []):
                etf_info = ETF_UNIVERSE.get(str(sig.get("code", "")), {})
                c = etf_info.get("cluster", "")
                s = sig.get("signal", "")
                if c and s in ("BUY_STRONG", "BUY_WATCH", "REDUCE", "SELL_STOP", "AVOID"):
                    _etf_cluster_signals.setdefault(c, []).append(s)
            # 本模块各cluster方向
            _stock_cluster_dir: dict[str, list[str]] = {}
            for r in results:
                c = r["info"].get("cluster", "")
                s = r["signal"]
                if c and s in ("BUY_STRONG", "BUY_WATCH", "REDUCE", "SELL_STOP"):
                    _stock_cluster_dir.setdefault(c, []).append(s)
            # 检测冲突
            for c in set(_etf_cluster_signals) & set(_stock_cluster_dir):
                etf_buys = sum(1 for s in _etf_cluster_signals[c] if "BUY" in s)
                etf_sells = sum(1 for s in _etf_cluster_signals[c] if s in ("REDUCE","SELL_STOP","AVOID"))
                stk_buys = sum(1 for s in _stock_cluster_dir[c] if "BUY" in s)
                stk_sells = sum(1 for s in _stock_cluster_dir[c] if s in ("REDUCE","SELL_STOP"))
                if etf_buys > 0 and stk_sells > stk_buys:
                    exposure_warnings.append(f"🔀 {c}: ETF看多(BUY) vs 个股偏空(多数REDUCE)——方向冲突，谨慎")
                elif etf_sells > 0 and stk_buys > stk_sells:
                    exposure_warnings.append(f"🔀 {c}: ETF看空 vs 个股看多(BUY)——方向冲突，以个股技术面为准")
    except Exception as e:
        log.debug(f"跨模块暴露检查: {e}")

    reversals.sort(key=lambda r: -r["rev_pts"])
    log.info(f"底部反转候选: {len(reversals)} 只 — {[r['name'] for r in reversals]}")

    # ── 三线共振候观区：core/★★★ 日+周+月三线共振，当前偏离MA20>5% 暂不买 ──
    triple_watch: list[dict] = []
    for r in results:
        info_r = r["info"]
        ind_r  = r["ind"]
        if info_r.get("pool") != "core":
            continue
        if info_r.get("signal_3d") != "★★★":
            continue
        if r["signal"] not in ("HOLD", "BUY_WATCH"):
            continue
        df_r = r.get("df")
        if df_r is None:
            continue
        wk_r = _weekly_macd_state(df_r)
        mk_r = _monthly_trend_state(df_r)
        day_align  = ind_r.get("pre_golden_cross", False) or ind_r.get("cross") == "golden"
        week_align = wk_r.get("golden", False) and wk_r.get("above_zero", False)
        month_align = mk_r.get("ma3_rising", False) and (mk_r.get("above_ma6", False) or mk_r.get("dif_positive", False))
        if not (day_align and week_align and month_align):
            continue
        close_r = ind_r["close"]
        ma20_r  = ind_r.get("ma20", 0)
        dev = (close_r - ma20_r) / ma20_r * 100 if ma20_r else 0
        # 偏离MA20 > 5% 说明短期偏高，作为候观提示；已回踩则直接产生BUY信号，不重复
        if dev > 5:
            triple_watch.append({**r, "wk": wk_r, "mk": mk_r})
            log.info(f"  三线共振候观: {r['name']} 偏MA20={dev:+.1f}% 等回踩")

    log.info(f"三线共振候观: {len(triple_watch)} 只 — {[r['name'] for r in triple_watch]}")

    # ── MA5贴线上攻 + 大跌日护盘强势股扫描 ──
    from data_fetcher import get_index_prices
    index_chg_yesterday = 0.0
    try:
        idx_df = get_index_prices("000300", 5)
        if idx_df is not None and len(idx_df) >= 2:
            c0 = float(idx_df["close"].iloc[-2])
            c1 = float(idx_df["close"].iloc[-3])
            index_chg_yesterday = (c0 - c1) / c1 * 100
    except Exception:
        pass
    log.info(f"昨日沪深300涨跌: {index_chg_yesterday:+.2f}%")

    ma5_hug_watch = _scan_ma5_hug(results, index_chg_yesterday)
    log.info(f"MA5上攻候观: {len(ma5_hug_watch)} 只 — {[r['name'] for r in ma5_hug_watch]}")

    if exposure_warnings:
        ctx["exposure_warnings"] = exposure_warnings

    card = build_card(results, regime, ts, ctx=ctx, reversals=reversals,
                      overheat_map=overheat_map, triple_watch=triple_watch,
                      ma5_hug_watch=ma5_hug_watch)
    push_feishu(card, dry=dry)

    snap = _DIR / "logs" / f"stock_timing_{today}.json"
    snap.write_text(
        json.dumps(
            [
                {k: v for k, v in r.items() if k not in ("ind", "info", "df")}
                | {"close": r["ind"]["close"], "rsi": r["ind"]["rsi"],
                   "vol_ratio": r["ind"]["vol_ratio"], "data_date": r["ind"]["data_date"],
                   "consec_below_ma60": r["ind"]["consec_below_ma60"],
                   "consec_up_days": r["ind"].get("consec_up_days", 0),
                   "ma5_dev_pct": r["ind"].get("ma5_dev_pct", 0),
                   "pool": r["info"].get("pool", ""), "signal_3d": r["info"].get("signal_3d", ""),
                   "theme": r["info"].get("theme", ""), "cluster": r["info"].get("cluster", "")}
                for r in results
            ],
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    log.info(f"快照: {snap}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry",   action="store_true", help="只打印，不推送飞书")
    ap.add_argument("--force", action="store_true", help="跳过交易日检查（调试用）")
    args = ap.parse_args()
    main(dry=args.dry, force=args.force)
