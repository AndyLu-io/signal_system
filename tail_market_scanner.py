"""
尾盘择时扫描器 — 每日 14:45 执行
核心逻辑：恐惧是最好的朋友
当日杀跌恐慌 + 技术面关键支撑 = 尾盘低吸最佳时机
"""

from __future__ import annotations
import logging
import re
from dataclasses import dataclass, field
from typing import Optional
import requests
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0"}

STAR_MAP = {3: "★★★ 绝佳", 2: "★★  较好", 1: "★    一般", 0: "— 暂无机会"}


@dataclass
class TailSignal:
    code: str
    name: str
    current_price: float
    change_pct: float          # 今日涨跌幅(%)
    vol_ratio: float           # 今日量/5日均量
    ma5_dev_pct: float         # 距MA5偏离(%)
    ma20_dev_pct: float        # 距MA20偏离(%)
    rsi6: float
    score: float               # 0-100 综合得分
    stars: int                 # 0-3星
    triggers: list[str] = field(default_factory=list)   # 触发原因列表
    cautions: list[str] = field(default_factory=list)   # 注意事项
    ma60_trend_pct: float = 0.0       # MA60近5日斜率(%)，正=上升趋势
    main_force_flow: float = 0.0      # 主力净流入(亿元)，正=流入
    consec_down_days: int = 0         # 历史连续收跌天数


@dataclass
class TailMarketReading:
    market_panic: bool          # 大盘是否出现恐慌
    market_change_pct: float    # 大盘今日涨跌幅
    market_note: str
    signals: list[TailSignal]   # 有机会的品种，按评分降序


# ─── 实时行情（腾讯） ──────────────────────────────────────────────────────────

def _fetch_realtime_quotes(codes: list[str]) -> dict[str, dict]:
    """
    腾讯实时行情接口，获取当前价格、涨跌幅、今日成交量
    返回 {code: {price, change_pct, volume, high, low, open}}
    """
    mkt_map = {}
    for c in codes:
        prefix = "sz" if (c.startswith("15") or c.startswith("562") or c.startswith("563")) else "sh"
        mkt_map[c] = f"{prefix}{c}"

    symbols = ",".join(mkt_map.values())
    url = f"http://qt.gtimg.cn/q={symbols}"
    result = {}
    try:
        r = requests.get(url, headers=_HEADERS, timeout=8)
        for code, sym in mkt_map.items():
            pattern = rf'v_{re.escape(sym)}="([^"]+)"'
            m = re.search(pattern, r.text)
            if not m:
                continue
            parts = m.group(1).split("~")
            if len(parts) < 32:
                continue
            try:
                result[code] = {
                    "price":      float(parts[3]),
                    "prev_close": float(parts[4]),
                    "open":       float(parts[5]),
                    "volume":     float(parts[6]),    # 手（100股）
                    "change_pct": float(parts[32]) if len(parts) > 32 else 0.0,
                    "high":       float(parts[33]) if len(parts) > 33 else 0.0,
                    "low":        float(parts[34]) if len(parts) > 34 else 0.0,
                }
            except (ValueError, IndexError):
                pass
    except Exception as e:
        logger.warning(f"实时行情获取失败: {e}")
    return result


def _calc_ma60_slope(closes: np.ndarray, window: int = 5) -> float:
    """MA60近window个交易日斜率(%)，正=上升，负=下降"""
    if len(closes) < 60 + window:
        return 0.0
    ma60_now  = closes[-60:].mean()
    ma60_prev = closes[-(60 + window):-window].mean()
    if ma60_prev == 0:
        return 0.0
    return round((ma60_now - ma60_prev) / ma60_prev * 100, 3)


def _count_consec_down(closes: np.ndarray) -> int:
    """统计历史收盘价连续收跌天数"""
    count = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] < closes[i - 1]:
            count += 1
        else:
            break
    return count


def _calc_rsi(closes: np.ndarray, period: int = 6) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes[-period - 1:])
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains.mean()
    avg_loss = losses.mean()
    if avg_loss == 0:
        return 100.0
    return round(100 - 100 / (1 + avg_gain / avg_loss), 2)


# ─── 单品种尾盘评分 ────────────────────────────────────────────────────────────

def _score_etf_tail(
    code: str,
    name: str,
    quote: dict,
    hist: Optional[pd.DataFrame],
    main_force_flow: float = 0.0,
    vol_factor: float = 0.80,
) -> Optional[TailSignal]:
    """
    计算单只ETF的尾盘买入吸引力得分（0-100）
    必要条件：今日至少下跌0.5%（有恐慌/回调）
    """
    change_pct = quote.get("change_pct", 0.0)
    current    = quote.get("price", 0.0)
    today_vol  = quote.get("volume", 0.0)

    if current <= 0:
        return None

    # 必要条件：今日必须有回调（买在恐惧时）
    if change_pct > 0.5:
        return None

    triggers = []
    cautions = []
    score = 0.0

    # 高开低走预判（在所有评分之前，防止出货形态被误判为买点）
    today_high = quote.get("high", current)
    if today_high > 0 and current > 0:
        intraday_fall = (today_high - current) / current * 100
        if intraday_fall >= 3.0:
            cautions.append(f"高开冲高回落{intraday_fall:.1f}%（出货形态风险）")
            score -= 10
        elif intraday_fall >= 1.5:
            cautions.append(f"今日冲高回落{intraday_fall:.1f}%，形态偏弱")

    # ── S1: 今日跌幅（恐惧程度）────────────────────────────────────────────
    if change_pct <= -5:
        score += 20; triggers.append(f"今日暴跌{change_pct:.1f}%（极度恐慌）")
    elif change_pct <= -3:
        score += 18; triggers.append(f"今日大跌{change_pct:.1f}%（恐慌出逃）")
    elif change_pct <= -2:
        score += 14; triggers.append(f"今日下跌{change_pct:.1f}%（恐惧情绪）")
    elif change_pct <= -1:
        score += 8;  triggers.append(f"今日跌{change_pct:.1f}%（轻度回调）")
    else:
        score += 2   # 0~-0.5% 微幅下跌

    ma5_dev = ma20_dev = rsi6 = vol_ratio = 0.0

    if hist is not None and len(hist) >= 22:
        closes = hist["close"].values

        # 把今日实时价格拼入用于计算（避免仅看昨收）
        all_closes = np.append(closes, current)

        ma5  = closes[-5:].mean()
        ma20 = closes[-20:].mean()
        ma60 = closes[-min(60, len(closes)):].mean() if len(closes) >= 10 else ma20

        ma5_dev  = (current - ma5)  / ma5  * 100
        ma20_dev = (current - ma20) / ma20 * 100
        ma60_dev = (current - ma60) / ma60 * 100
        rsi6 = _calc_rsi(all_closes, 6)

        # ── S2: 均线支撑（技术面绝佳买点）────────────────────────────────
        if -2.0 <= ma20_dev <= 1.0:
            score += 20; triggers.append(f"回踩MA20支撑（偏离{ma20_dev:+.1f}%）")
        elif -1.5 <= ma5_dev <= 0.5:
            score += 15; triggers.append(f"回踩MA5支撑（偏离{ma5_dev:+.1f}%）")
        elif -4.0 <= ma20_dev < -2.0:
            score += 10; triggers.append(f"跌破MA20下方（{ma20_dev:+.1f}%，或超跌机会）")
        elif ma20_dev < -4:
            cautions.append(f"深跌MA20 {ma20_dev:+.1f}%，需确认止跌信号")

        if -5 <= ma60_dev <= 1:
            score += 8; triggers.append(f"MA60长期支撑附近（{ma60_dev:+.1f}%）")

        # ── S3: RSI 超卖（逆向买点）───────────────────────────────────────
        if rsi6 <= 20:
            score += 22; triggers.append(f"RSI(6)={rsi6:.0f}（极度超卖）")
        elif rsi6 <= 30:
            score += 18; triggers.append(f"RSI(6)={rsi6:.0f}（超卖区）")
        elif rsi6 <= 40:
            score += 10; triggers.append(f"RSI(6)={rsi6:.0f}（偏低区间）")
        elif rsi6 >= 70:
            score -= 10; cautions.append(f"RSI(6)={rsi6:.0f}（偏高，非超卖）")

        # ── S4: 量能萎缩（卖压减弱 = 底部信号）────────────────────────────
        # vol_factor < 0.2 说明是早盘/竞价，量数据无参考意义，跳过
        if vol_factor >= 0.2 and "volume" in hist.columns and len(hist) >= 6:
            avg5_vol = hist["volume"].values[-5:].mean()
            est_full_vol = today_vol / vol_factor
            if avg5_vol > 0:
                vol_ratio = est_full_vol / avg5_vol
                if vol_ratio <= 0.6:
                    score += 15; triggers.append(f"量能萎缩{vol_ratio:.1f}x（抛压衰竭）")
                elif vol_ratio <= 0.8:
                    score += 8;  triggers.append(f"量能收缩{vol_ratio:.1f}x（卖压减弱）")
                elif vol_ratio >= 2.0:
                    cautions.append(f"放量下跌{vol_ratio:.1f}x（主力出货？谨慎）")

        # ── S6: 中期趋势方向（MA60斜率）─────────────────────────────────
        ma60_slope = _calc_ma60_slope(closes)
        if ma60_slope > 0.5:
            score += 12; triggers.append(f"MA60趋势向上（+{ma60_slope:.2f}%/5日），回调低吸")
        elif ma60_slope > 0.2:
            score += 5;  triggers.append(f"MA60温和上行（+{ma60_slope:.2f}%/5日）")
        elif ma60_slope < -0.5:
            score -= 12; cautions.append(f"MA60趋势下行（{ma60_slope:.2f}%/5日），谨防下跌趋势接刀")
        elif ma60_slope < -0.2:
            score -= 5;  cautions.append(f"MA60偏弱（{ma60_slope:.2f}%/5日），中期趋势存隐患")

        # ── S7: 连续下跌天数 ──────────────────────────────────────────────
        consec_down = _count_consec_down(closes)
        if consec_down >= 5:
            score -= 8; cautions.append(f"历史连续{consec_down}日收跌，下跌趋势明显，小心接刀")
        elif consec_down >= 3:
            score -= 3; cautions.append(f"连续{consec_down}日调整，注意是否止跌企稳")

    else:
        ma60_slope = 0.0
        consec_down = 0

    # ── S5: 今日低点与前低支撑 ──────────────────────────────────────────
    today_low = quote.get("low", current)
    if hist is not None and len(hist) >= 5:
        prev_lows = hist["low"].values[-5:]
        near_low = any(abs(today_low - l) / l < 0.015 for l in prev_lows)
        if near_low:
            score += 8; triggers.append("触及近期支撑低点区域")

    # ── S8: ETF主力净流入（今日实时） ────────────────────────────────────────
    if main_force_flow > 1.0:
        score += 15; triggers.append(f"主力今日净流入{main_force_flow:.2f}亿（资金积极进场）")
    elif main_force_flow > 0.3:
        score += 8;  triggers.append(f"主力今日净流入{main_force_flow:.2f}亿（资金回流）")
    elif main_force_flow > 0:
        score += 3
    elif main_force_flow < -1.0:
        score -= 12; cautions.append(f"主力今日净流出{abs(main_force_flow):.2f}亿（资金撤离，慎买）")
    elif main_force_flow < -0.3:
        score -= 6;  cautions.append(f"主力今日净流出{abs(main_force_flow):.2f}亿，需观察")

    stars = 3 if score >= 70 else (2 if score >= 50 else (1 if score >= 30 else 0))

    if not triggers and stars == 0:
        return None

    return TailSignal(
        code=code, name=name, current_price=current,
        change_pct=change_pct, vol_ratio=vol_ratio,
        ma5_dev_pct=round(ma5_dev, 2), ma20_dev_pct=round(ma20_dev, 2),
        rsi6=rsi6, score=round(score, 1), stars=stars,
        triggers=triggers, cautions=cautions,
        ma60_trend_pct=round(ma60_slope if hist is not None and len(hist) >= 65 else 0.0, 3),
        main_force_flow=round(main_force_flow, 4),
        consec_down_days=consec_down if hist is not None and len(hist) >= 22 else 0,
    )


# ─── 大盘情绪判断 ──────────────────────────────────────────────────────────────

def _check_market_panic(csi300_quote: Optional[dict]) -> tuple[bool, float, str]:
    """判断大盘是否出现恐慌，返回 (is_panic, change_pct, note)"""
    if not csi300_quote:
        return False, 0.0, "大盘数据获取失败"
    chg = csi300_quote.get("change_pct", 0.0)
    if chg <= -2.5:
        return True, chg, f"大盘暴跌{chg:.1f}%，市场极度恐慌，逢低布局机会"
    if chg <= -1.5:
        return True, chg, f"大盘大跌{chg:.1f}%，情绪偏悲观，关注超跌品种"
    if chg <= -0.8:
        return False, chg, f"大盘下跌{chg:.1f}%，情绪偏冷，谨慎观察"
    if chg >= 1.5:
        return False, chg, f"大盘上涨{chg:.1f}%，情绪偏热，追高需谨慎"
    return False, chg, f"大盘涨跌{chg:+.1f}%，情绪平稳"


# ─── 主扫描入口 ────────────────────────────────────────────────────────────────

def scan_tail_market(
    etf_universe: dict,
    etf_prices: dict[str, Optional[pd.DataFrame]],
    main_force_flows: dict[str, float] | None = None,
    vol_factor: float = 0.80,
) -> TailMarketReading:
    """
    扫描股池中尾盘买入机会
    etf_universe: {code: {name, ...}} from config
    etf_prices: 日K线数据（来自早盘数据获取）
    main_force_flows: 主力净流向 {code: 亿元}（来自 data_fetcher）
    """
    codes = list(etf_universe.keys())
    all_codes = codes + ["000300"]
    quotes = _fetch_realtime_quotes(all_codes)

    # 大盘恐慌判断
    csi300_q = quotes.get("000300")
    is_panic, mkt_chg, mkt_note = _check_market_panic(csi300_q)

    flows = main_force_flows or {}
    signals = []
    for code in codes:
        info = etf_universe.get(code, {})
        name = info.get("name", code)
        pool = info.get("pool", "watch")

        # 只扫描核心池和候选池
        if pool == "watch":
            continue

        quote = quotes.get(code)
        if not quote:
            logger.debug(f"  {name}({code}) 实时行情获取失败，跳过")
            continue

        hist = etf_prices.get(code)
        sig = _score_etf_tail(code, name, quote, hist, main_force_flow=flows.get(code, 0.0), vol_factor=vol_factor)
        if sig and sig.stars >= 1:
            signals.append(sig)

    signals.sort(key=lambda s: s.score, reverse=True)

    return TailMarketReading(
        market_panic=is_panic,
        market_change_pct=mkt_chg,
        market_note=mkt_note,
        signals=signals,
    )
