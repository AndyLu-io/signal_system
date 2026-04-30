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

from config import STOCK_UNIVERSE, STOCK_CLUSTER_MAX_WEIGHT, DEFENSIVE_ROTATION_POOL  # noqa: E402
from data_fetcher import (  # noqa: E402
    get_north_flow,
    get_market_breadth,
    get_index_prices,
    get_etf_main_force_flow,
)
from sentiment_gauge import calc_market_sentiment  # noqa: E402
from rotation_advisor import calc_rotation_signal, RotationSignal, DIM_EMOJI, STRENGTH_LABEL  # noqa: E402

# ─────────────────────────────────────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────────────────────────────────────
FEISHU_WEBHOOKS = [
    "https://open.feishu.cn/open-apis/bot/v2/hook/077c6eb2-14ae-4736-8b9b-56d444082da6",
    "https://open.feishu.cn/open-apis/bot/v2/hook/d7bf66ce-e368-4718-a00e-753fc1f1f5dc",
]
STATE_FILE = _DIR / "state" / "regime_state.json"
LOG_FILE   = _DIR / "logs" / f"stock_timing_{date.today():%Y%m}.log"

REGIME_LABEL = {"R1": "趋势牛市", "R2": "震荡市", "R3": "轮动市", "R4": "风险市"}
REGIME_COLOR = {"R1": "green",   "R2": "blue",   "R3": "yellow", "R4": "red"}

# 机制 → 个股单仓上限（%）
REGIME_STOCK_MAX = {"R1": 8, "R2": 6, "R3": 4, "R4": 0}
# pool → 仓位系数
POOL_FACTOR = {"core": 1.0, "candidate": 0.6, "watch": 0.0}

SCORE_BUY_STRONG = 5
SCORE_BUY_WATCH  = 3
SCORE_HOLD       = -1
SCORE_REDUCE     = -3

_TENCENT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Referer": "https://finance.qq.com/",
}

# 公休假日黑名单（补充节假日，格式 YYYY-MM-DD）
HOLIDAY_BLACKLIST: set[str] = {
    "2026-01-01", "2026-01-26", "2026-01-27", "2026-01-28",
    "2026-01-29", "2026-01-30", "2026-02-02",
    "2026-04-03", "2026-04-06",
    "2026-05-01", "2026-05-04", "2026-05-05",
    "2026-06-19",
    "2026-10-01", "2026-10-02", "2026-10-05", "2026-10-06",
    "2026-10-07", "2026-10-08",
}

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
    d = today or date.today().isoformat()
    if datetime.strptime(d, "%Y-%m-%d").weekday() >= 5:
        return False
    return d not in HOLIDAY_BLACKLIST


# ─────────────────────────────────────────────────────────────────────────────
# K 线获取（腾讯财经，与主系统一致，含盘中实时价格）
# ─────────────────────────────────────────────────────────────────────────────
def _market_prefix(code: str) -> str:
    if code.startswith("0") or code.startswith("3"):
        return "sz"
    return "sh"


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


def fetch_kline(code: str) -> pd.DataFrame | None:
    """
    返回 DataFrame(date, open, close, high, low, volume)。
    腾讯接口在交易时段自动包含今日实时价格作为最后一行。
    """
    klines = _fetch_tencent_kline(code, count=120)
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
        "consec_below_ma60": consec_below_ma60,
        "main_force_flow": 0.0,   # 在主循环中由 get_etf_main_force_flow 填充
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
        if ind["macd_bar"] < ind["macd_bar_p"]:
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
    elif vr < 0.6:
        score -= 1; reasons.append(f"缩量{vr:.1f}x")

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

    return score, reasons


def signal_type(score: int) -> str:
    if score >= SCORE_BUY_STRONG: return "BUY_STRONG"
    if score >= SCORE_BUY_WATCH:  return "BUY_WATCH"
    if score >= SCORE_HOLD:       return "HOLD"
    if score >= SCORE_REDUCE:     return "REDUCE"
    return "SELL_STOP"


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
# 攻守切换 · 防御轮动池
# ─────────────────────────────────────────────────────────────────────────────

_RANK_LABEL = {1: "🛡️ 极防御（公用/货币）", 2: "🔵 高股息蓝筹", 3: "🟡 消费/医药防御"}


def fetch_and_score_defensive() -> list[dict]:
    """扫描防御轮动池，返回按防御等级+趋势排序的结果列表。"""
    results: list[dict] = []
    for code, info in DEFENSIVE_ROTATION_POOL.items():
        df = fetch_kline(code)
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
    """
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
                return None
            closes = [float(k[2]) for k in klines]
            close  = closes[-1]
            prev   = closes[-2]
            chg    = round((close / prev - 1) * 100, 2)
            ma5    = sum(closes[-5:]) / 5
            ma20   = sum(closes[-20:]) / 20 if len(closes) >= 20 else close
            return {
                "close":   close,
                "chg_pct": chg,
                "vs_ma5":  "↑" if close > ma5  else "↓",
                "vs_ma20": "↑" if close > ma20 else "↓",
            }
        except Exception:
            if attempt == 0:
                time.sleep(1)
    return None


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

    # ── 1. 大盘指数 ────────────────────────────────────────────────────────
    indices: dict[str, dict] = {}
    for name, sym in _INDICES.items():
        snap = _price_snapshot(sym)
        if snap:
            indices[name] = snap
    ctx["indices"] = indices

    # ── 2. 北向资金 ────────────────────────────────────────────────────────
    try:
        north_df = get_north_flow(days=8)
        if north_df is not None and len(north_df) >= 1:
            # 精确净买入（手动数据）
            net_s = north_df["net_buy_billion"].dropna()
            if len(net_s) >= 1:
                ctx["north_today"] = round(float(net_s.iloc[-1]), 2)
                ctx["north_5d"]    = round(float(net_s.tail(5).sum()), 2)
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

    # ── 4. 板块轮动（ETF 当日涨跌幅，腾讯 K 线，无代理依赖）──────────────
    sector_snaps: dict[str, dict] = {}
    for name, sym in _SECTOR_ETFS.items():
        snap = _price_snapshot(sym, count=10)
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


def _signal_line(r: dict) -> str:
    ind   = r["ind"]
    info  = r["info"]
    sig   = r["signal"]
    score = r["score"]
    pos   = r["position_pct"]
    stop  = r["stop_price"]
    text  = "、".join(r["reasons"]) if r["reasons"] else "技术中性"

    emoji, label = _signal_label(sig, score, pos)

    price_line = (
        f"价格 **{ind['close']:.2f}**"
        f"  MA5={ind['ma5']:.2f}  MA20={ind['ma20']:.2f}  MA60={ind['ma60']:.2f}"
    )
    ma60_slope = ind.get("ma60_slope_5d", 0.0)
    ma60_arr = "↑" if ma60_slope > 0.3 else ("↓" if ma60_slope < -0.3 else "→")
    flow = ind.get("main_force_flow", 0.0)
    flow_str = (f"  主力{'↑' if flow > 0 else '↓'}{abs(flow):.1f}亿" if abs(flow) >= 0.3 else "")
    tech_line = (
        f"{_rsi_tag(ind['rsi'])}  量比={ind['vol_ratio']:.1f}x"
        f"  3日{ind['chg3']:+.1f}%  MA60{ma60_arr}  得分{score:+d}"
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


def build_card(results: list[dict], regime: str, ts: str, ctx: dict | None = None) -> dict:
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
    near_buys  = [r for r in results if r["signal"] == "HOLD" and r["score"] >= 2]
    holds      = [r for r in results if r["signal"] == "HOLD" and 0 <= r["score"] < 2]
    weak_holds = [r for r in results if r["signal"] == "HOLD" and r["score"] < 0]

    # 情绪过热时降级买入信号（不执行操作）
    if timing_blocked:
        near_buys  = near_buys + buys_s + buys_w
        buys_s     = []
        buys_w     = []

    elements: list[dict] = []

    # ── 宏观摘要（顶部） ──────────────────────────────────────────────────
    if ctx:
        macro_text = _fmt_macro_section(ctx)
        if macro_text:
            elements += [
                {"tag": "div", "text": {"tag": "lark_md", "content": macro_text}},
                {"tag": "hr"},
            ]

    elements += [
        {"tag": "div", "text": {"tag": "lark_md", "content": gate_tip}},
        {"tag": "hr"},
    ]

    # ── 攻守切换区块（情绪过热时插入）─────────────────────────────────────
    if timing_blocked:
        log.info("情绪过热，扫描防御轮动池...")
        defensive_list = fetch_and_score_defensive()
        rotation_text  = _fmt_defensive_rotation(defensive_list, rotation=rotation)
        elements += [
            {"tag": "div", "text": {"tag": "lark_md", "content": rotation_text}},
            {"tag": "hr"},
        ]

    def _sec(title: str, items: list[dict]) -> list[dict]:
        if not items:
            return []
        body = "\n\n".join(_signal_line(r) for r in items)
        return [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**{title}**\n{body}"}},
            {"tag": "hr"},
        ]

    def _name_sec(title: str, items: list[dict]) -> list[dict]:
        if not items:
            return []
        names = "　".join(f"{r['name']}({r['code']}) {r['score']:+d}分" for r in items)
        return [
            {"tag": "div", "text": {"tag": "lark_md", "content": f"**{title}**\n{names}"}},
            {"tag": "hr"},
        ]

    elements += _sec("🚀 ━━ 强买入信号 ━━", buys_s)
    elements += _sec("🔵 ━━ 观察建仓 · 今日机会 ━━", buys_w)
    elements += _sec("🟡 接近买入线（可小仓关注）", near_buys)
    elements += _sec("🔴 止损（立即执行）", sell_stops)
    elements += _sec("📉 减仓 / 回避", reduces)
    elements += _name_sec("⚪ 技术中性（持仓不动）", holds)
    elements += _name_sec("🟠 弱势持观（偏弱未破位）", weak_holds)

    if not (sell_stops or reduces or buys_s or buys_w or near_buys or holds or weak_holds):
        elements.append({"tag": "div", "text": {"tag": "lark_md",
                                                  "content": "暂无有效信号"}})

    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text",
                      "content": "个股池择时 ｜ 止损触及须当日执行 ｜ 单股仓位≤集群上限"}],
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
        # 同时用简洁文本打印关键信号
        for el in card["card"]["elements"]:
            if el.get("tag") == "div":
                txt = el.get("text", {}).get("content", "")
                if txt:
                    print(txt[:400])
                    print()
        return
    try:
        for url in FEISHU_WEBHOOKS:
            r = requests.post(
                url,
                headers={"Content-Type": "application/json"},
                data=json.dumps(card, ensure_ascii=False),
                timeout=10,
            )
            res = r.json()
            if res.get("code") == 0 or res.get("StatusCode") == 0:
                log.info(f"飞书推送成功: {url[-8:]}")
            else:
                log.warning(f"飞书返回({url[-8:]}): {res}")
    except Exception as e:
        log.error(f"飞书推送失败: {e}")


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

    results: list[dict] = []

    for code, info in STOCK_UNIVERSE.items():
        df = fetch_kline(code)
        if df is None:
            log.warning(f"{code} {info['name']} K线获取失败")
            continue

        ind = compute_indicators(df)
        if ind is None:
            log.warning(f"{code} {info['name']} 数据不足({len(df)}条)")
            continue

        ind["main_force_flow"] = stock_flows.get(code, 0.0)
        score, reasons = score_stock(ind, info)

        # R4：禁止个股新多
        if regime == "R4" and score > 0:
            score = 0

        sig  = signal_type(score)

        # SELL_STOP 2日确认：连续跌破MA60不满2日时降级为REDUCE
        if sig == "SELL_STOP" and ind.get("consec_below_ma60", 0) < 2:
            sig = "REDUCE"
            reasons.append("MA60跌破未满2日确认，降级为减仓")

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

    order = {"SELL_STOP": 0, "REDUCE": 1, "BUY_STRONG": 2, "BUY_WATCH": 3, "HOLD": 4}
    results.sort(key=lambda r: (order.get(r["signal"], 9), -r["score"]))

    card = build_card(results, regime, ts, ctx=ctx)
    push_feishu(card, dry=dry)

    snap = _DIR / "logs" / f"stock_timing_{today}.json"
    snap.write_text(
        json.dumps(
            [
                {k: v for k, v in r.items() if k not in ("ind", "info")}
                | {"close": r["ind"]["close"], "rsi": r["ind"]["rsi"],
                   "vol_ratio": r["ind"]["vol_ratio"], "data_date": r["ind"]["data_date"],
                   "consec_below_ma60": r["ind"]["consec_below_ma60"],
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
