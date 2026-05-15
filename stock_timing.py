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

SCORE_BUY_STRONG = 6   # 2026-05回测: score=5 BUY_STRONG T+5超额-1.07%, T+1胜率40%; 提至6需更强共振
SCORE_BUY_WATCH  = 5   # 2026-05回测: score=4 BUY_WATCH T+5均收-2.12%超额-3.33%，提至5使其不再触发
SCORE_HOLD       = -3  # 2026-05回测: SELL_STOP后T+5超额+3.34%，止损普遍过早；-3减少误降REDUCE
SCORE_REDUCE     = -5  # 2026-05回测: REDUCE后均反弹+2.60%~+6.04%，放宽至-5降低误止损频率

# watch池高风险cluster，买入阈值提高+1（回测: chemical/food_bev/consumer T+5超额持续-2~-5%）
_HIGH_RISK_CLUSTERS = frozenset({"chemical", "food_bev", "consumer"})

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

    # 趋势
    if close > ma5 > ma20 > ma60:
        score += 2; reasons.append("多头排列")
    elif close > ma20 > ma60:
        score += 1; reasons.append("站20/60线")
    elif close < ma60:
        score -= 2; reasons.append("跌破60线")
    elif close < ma20:
        score -= 1; reasons.append("跌破20线")

    # MACD
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

    # RSI(14)
    rsi = ind["rsi"]
    if rsi > 78:
        score -= 2; reasons.append(f"RSI超买{rsi:.0f}")
    elif rsi > 68:
        score -= 1; reasons.append(f"RSI偏高{rsi:.0f}")
    elif rsi < 25:
        score += 2; reasons.append(f"RSI超卖{rsi:.0f}")
    elif rsi < 35:
        score += 1; reasons.append(f"RSI偏低{rsi:.0f}")

    # 量比
    vr = ind["vol_ratio"]
    if vr >= 1.5 and close >= ma5:
        score += 1; reasons.append(f"放量{vr:.1f}x")
    elif vr < 0.6 and close < ma5:
        score -= 1; reasons.append(f"缩量弱势{vr:.1f}x")

    # 三维信号强度加成
    sig3d = info.get("signal_3d", "★☆☆")
    if score > 0:
        if sig3d == "★★★":
            score += 1
        elif sig3d == "★☆☆":
            score -= 1

    # MA60趋势方向（中期趋势过滤）
    ma60_slope = ind.get("ma60_slope_5d", 0.0)
    if ma60_slope > 0.5:
        score += 1; reasons.append(f"MA60上行(+{ma60_slope:.1f}%/5日)")
    elif ma60_slope < -0.8:
        score -= 2; reasons.append(f"MA60下行趋势({ma60_slope:.1f}%/5日)")
    elif ma60_slope < -0.3:
        score -= 1; reasons.append(f"MA60偏弱({ma60_slope:.1f}%/5日)")

    # 连续下跌天数
    consec = ind.get("consec_down_days", 0)
    if consec >= 5:
        score -= 1; reasons.append(f"连跌{consec}日")
    elif consec >= 3 and ma60_slope < -0.2:
        score -= 1; reasons.append(f"连跌{consec}日且MA60偏弱")

    # 主力资金净流向
    flow = ind.get("main_force_flow", 0.0)
    if flow > 0.5:
        score += 1; reasons.append(f"主力净流入{flow:.1f}亿")
    elif flow < -0.5:
        score -= 1; reasons.append(f"主力净流出{abs(flow):.1f}亿")

    # 偏离MA20（追高风险，阈值比ETF系统宽松以适应个股波动）
    dev_ma20 = (close - ma20) / ma20 * 100
    if dev_ma20 >= 20:
        score -= 2; reasons.append(f"严重偏离MA20+{dev_ma20:.0f}%")
    elif dev_ma20 >= 12:
        score -= 1; reasons.append(f"偏离MA20+{dev_ma20:.0f}%")

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

    # 低量提示（不作为硬门槛，改为评分维度扣分，万华化学量比0.53不应被直接淘汰）
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
            details.append("周线MACD" + ("金叉✅" if wk.get("golden") else "即将金叉🔔"))
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

    if pts < 9:              # 至少9分入选；4个新增硬门槛(MA60slope/close/MA60/f_earnings≥60)已把通威等结构性下跌股拦住
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


def calc_position(sig: str, info: dict, regime: str) -> int:
    if sig not in ("BUY_STRONG", "BUY_WATCH"):
        return 0
    cap    = REGIME_STOCK_MAX.get(regime, 0)
    factor = POOL_FACTOR.get(info.get("pool", "watch"), 0.0)
    base   = cap * factor
    if sig == "BUY_WATCH":
        base *= 0.6
    return max(0, round(base))


def calc_stop(ind: dict) -> float:
    return round(max(ind["ma20"], ind["close"] * 0.95), 2)


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
                return sym, {
                    "close":   close,
                    "chg_pct": chg,
                    "vs_ma5":  "↑" if close > ma5 else "↓",
                    "vs_ma20": "↑" if close > ma20 else "↓",
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
    """返回 (建议买入参考价, 简短提示)。"""
    close, ma5, ma20 = ind["close"], ind["ma5"], ind["ma20"]
    if sig == "BUY_STRONG":
        if close > ma5 * 1.03:
            return round(ma5 * 1.01, 2), "等回踩MA5再分批入场"
        return round(close, 2), "当前价位可即刻分批建仓"
    # BUY_WATCH
    if close > ma5 * 1.02:
        return round(ma5 * 1.01, 2), "建议等回踩MA5附近再入场"
    return round(close, 2), "当前价位可小仓试探建仓"


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
               overheat_map: dict[str, dict] | None = None) -> dict:
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

    # ── 底部反转候选 ────────────────────────────────────────────────────
    if reversals:
        rev_body = "\n\n".join(_reversal_line(r) for r in reversals)
        elements += [
            {"tag": "div", "text": {"tag": "lark_md",
                                     "content": f"**🔄 ━━ 底部反转候选（MACD/Boll/周月K多维确认）━━**\n{rev_body}"}},
            {"tag": "hr"},
        ]

    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text",
                      "content": "个股池择时 ｜ 止损触及须当日执行 ｜ 单股仓位≤集群上限 ｜ 反转候选仅供参考，需等日线信号确认"}],
    })

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text",
                           "content": f"📊 个股研究池择时 ｜ {ts}"},
                "template": color,
            },
            "elements": elements,
        },
    }


# ─────────────────────────────────────────────────────────────────────────────
# 推送
# ─────────────────────────────────────────────────────────────────────────────
def push_feishu(card: dict, dry: bool = False) -> None:
    if dry:
        import pprint
        log.info("[dry-run] 卡片预览（前 3000 字）:")
        pprint.pprint(card, width=120)
        for el in card["card"]["elements"]:
            if el.get("tag") == "div":
                txt = el.get("text", {}).get("content", "")
                if txt:
                    print(txt[:400])
                    print()
        return
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
        kline_surge = detect_offense_surge(STOCK_UNIVERSE, kline_chg_pcts)
        if kline_surge:
            ctx["offense_surge"] = kline_surge
            log.info(f"进攻信号: {', '.join(f'{n}({v:+.1f}%)' for n,v in kline_surge)}")
    except Exception as e:
        log.debug(f"K线集群恐慌检测: {e}")

    results:   list[dict] = []
    reversals: list[dict] = []

    for code, info in STOCK_UNIVERSE.items():
        df = all_klines.get(code)
        if df is None:
            log.warning(f"{code} {info['name']} K线获取失败")
            continue

        ind = compute_indicators(df)
        if ind is None:
            log.warning(f"{code} {info['name']} 数据不足({len(df)}条)")
            continue

        ind["main_force_flow"] = stock_flows.get(code, 0.0)

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

        # ── 减仓/止损过滤 ──────────────────────────────────────────────────
        # SELL_STOP 降级规则（回测: SELL_STOP后T+5超额+3.34%，大量误杀）
        rsi_now = ind.get("rsi", 50)
        consec = ind.get("consec_below_ma60", 0)
        cluster_now = info.get("cluster", "")
        if sig == "SELL_STOP":
            # watch池禁止SELL_STOP（回测: watch/★☆☆ SELL_STOP后T+5超额+4.21%）
            if pool == "watch":
                sig = "REDUCE"
                reasons.append("观察池暂缓止损，降为减仓（watch池止损成本高）")
            elif pool == "core" and info.get("signal_3d") == "★★★" and score >= -5:
                sig = "REDUCE"
                reasons.append("★★★核心池score>=-5暂缓止损，降为减仓观察")
            elif rsi_now < 25:
                sig = "REDUCE"
                reasons.append(f"RSI深度超卖({rsi_now:.0f}<25)，暂缓止损降为减仓")
            elif consec < 4:
                sig = "REDUCE"
                reasons.append(f"MA60跌破{consec}日未满4日确认，降级为减仓")

        pos  = calc_position(sig, info, regime)
        stop = calc_stop(ind)

        results.append({
            "code":         code,
            "name":         info["name"],
            "info":         info,
            "score":        score,
            "signal":       sig,
            "position_pct": pos,
            "stop_price":   stop,
            "reasons":      reasons,
            "ind":          ind,
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
            r["position_pct"] = calc_position("BUY_WATCH", r["info"], regime)
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

    reversals.sort(key=lambda r: -r["rev_pts"])
    log.info(f"底部反转候选: {len(reversals)} 只 — {[r['name'] for r in reversals]}")

    card = build_card(results, regime, ts, ctx=ctx, reversals=reversals, overheat_map=overheat_map)
    push_feishu(card, dry=dry)

    snap = _DIR / "logs" / f"stock_timing_{today}.json"
    snap.write_text(
        json.dumps(
            [
                {k: v for k, v in r.items() if k not in ("ind", "info")}
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
