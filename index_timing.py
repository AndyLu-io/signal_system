#!/usr/bin/env python3
"""
宽基指数+海外ETF择时系统
09:25→14:50 每10分钟分析12只ETF（A股宽基+港股+日股+美股）
推送至专属飞书频道，不经 notifier.py
"""
from __future__ import annotations
import json
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).parent))

import data_fetcher as df_api
from data_fetcher import _tencent_kline, _klines_to_df, _market  # 复用底层接口
from config import FEISHU_INDEX_WEBHOOK as FEISHU_WEBHOOK
from feishu_pusher import post_card

# ─── 配置 ──────────────────────────────────────────────────────────────────────

INDEX_UNIVERSE: dict[str, dict] = {
    # ── A股宽基（大→中→小→微，全市值覆盖）─────────────────────
    "510050": {"name": "上证50",     "region": "A股", "risk": "低"},
    "563080": {"name": "中证A50",    "region": "A股", "risk": "中"},
    "510300": {"name": "沪深300",    "region": "A股", "risk": "中"},
    "510500": {"name": "中证500",    "region": "A股", "risk": "中高"},
    "512100": {"name": "中证1000",   "region": "A股", "risk": "高"},
    "159915": {"name": "创业板",     "region": "A股", "risk": "高"},
    "588080": {"name": "科创50",     "region": "A股", "risk": "高"},
    "159902": {"name": "中小100",    "region": "A股", "risk": "中高"},
    # ── 港股 ────────────────────────────────────────────────────
    "159920": {"name": "恒生指数",   "region": "港股", "risk": "中"},
    "513180": {"name": "恒生科技",   "region": "港股", "risk": "高"},
    # ── 美股 ────────────────────────────────────────────────────
    "513500": {"name": "标普500",    "region": "美股", "risk": "中"},
    "513400": {"name": "道琼斯",     "region": "美股", "risk": "中"},
    "159501": {"name": "纳斯达克",   "region": "美股", "risk": "高"},
    # ── 日股 ────────────────────────────────────────────────────
    "513520": {"name": "日经225",    "region": "日股", "risk": "中高"},
    # ── 商品（对冲参考）────────────────────────────────────────────
    "518880": {"name": "黄金",       "region": "商品", "risk": "中"},
}

# CSI300 基准代码（用于超额收益计算）
CSI300_CODE = "510300"

# 信号定义：score 阈值 / 展示 emoji / 操作建议仓位区间
TIMING_LEVELS = [
    {"signal": "STRONG_BUY", "min": 72, "emoji": "🚀", "label": "强势买入", "pos": "15-20%"},
    {"signal": "BUY",        "min": 60, "emoji": "✅", "label": "积极参与", "pos": "10-15%"},
    {"signal": "HOLD",       "min": 45, "emoji": "🔵", "label": "持仓观察", "pos": "5-10%"},
    {"signal": "REDUCE",     "min": 32, "emoji": "🟡", "label": "减仓等待", "pos": "2-5%"},
    {"signal": "AVOID",      "min":  0, "emoji": "🔴", "label": "空仓观望", "pos": "0%"},
]

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"index_timing_{datetime.today().strftime('%Y%m')}.log",
                            encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("index_timing")


# ─── 数据获取 ──────────────────────────────────────────────────────────────────

def _fetch_etf_df(code: str, days: int = 300):
    """用腾讯K线获取ETF日线数据（sh/sz前缀由 _market() 自动检测）"""
    prefix = _market(code)
    sym = f"{prefix}{code}"
    klines = _tencent_kline(sym, days + 5)
    if klines:
        return _klines_to_df(klines).tail(days)
    logger.warning(f"ETF {code} 数据获取失败")
    return None


# ─── 评分计算 ──────────────────────────────────────────────────────────────────

def _calc_rsi(closes: np.ndarray, period: int = 14) -> float:
    if len(closes) < period + 1:
        return 50.0
    deltas = np.diff(closes[-(period + 1):])
    gains  = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains.mean()
    avg_loss = losses.mean()
    if avg_loss < 1e-9:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - 100.0 / (1.0 + rs), 1)


def _compute_macd_boll(closes: np.ndarray) -> dict:
    """MACD(12,26,9) + Bollinger(20,2σ) from a closes array."""
    if len(closes) < 35:
        return {}

    def _ema(arr: np.ndarray, period: int) -> np.ndarray:
        result = np.empty(len(arr))
        k = 2.0 / (period + 1)
        result[0] = arr[0]
        for i in range(1, len(arr)):
            result[i] = arr[i] * k + result[i - 1] * (1 - k)
        return result

    ema12 = _ema(closes, 12)
    ema26 = _ema(closes, 26)
    dif   = ema12 - ema26
    dea   = _ema(dif, 9)
    bar   = (dif - dea) * 2

    d, e   = float(dif[-1]),  float(dea[-1])
    dp, ep = float(dif[-2]),  float(dea[-2])
    bar_now  = float(bar[-1])
    bar_prev = float(bar[-2])

    cross = None
    if dp < ep and d >= e:
        cross = "golden"
    elif dp > ep and d <= e:
        cross = "death"

    pre_golden_cross = d < e and d > dp and (e - d) < (ep - dp)

    boll_upper = boll_lower = boll_pct = None
    if len(closes) >= 20:
        ma20  = float(closes[-20:].mean())
        std20 = float(closes[-20:].std())
        boll_upper = ma20 + 2 * std20
        boll_lower = ma20 - 2 * std20
        width = boll_upper - boll_lower
        boll_pct = float((closes[-1] - boll_lower) / width) if width > 0 else 0.5

    return {
        "dif": d, "dea": e,
        "bar_now": bar_now, "bar_prev": bar_prev,
        "cross": cross,
        "pre_golden_cross": pre_golden_cross,
        "boll_upper": boll_upper, "boll_lower": boll_lower, "boll_pct": boll_pct,
    }


def _weekly_macd_state_from_closes(closes: np.ndarray) -> dict:
    """Resample daily closes to weekly (last of each 5-day window) and return MACD state."""
    if len(closes) < 60:
        return {"ok": False}
    weekly = closes[::-1][::5][::-1]
    if len(weekly) < 35:
        return {"ok": False}
    mb = _compute_macd_boll(weekly)
    if not mb:
        return {"ok": False}
    return {
        "ok":         True,
        "golden":     mb.get("cross") == "golden",
        "pre_cross":  mb.get("pre_golden_cross", False),
        "above_zero": mb.get("dif", 0) > 0,
        "bar_rising": mb.get("bar_now", 0) > mb.get("bar_prev", 0),
        "dif":        mb.get("dif", 0),
        "dea":        mb.get("dea", 0),
    }


def _dim_bar(score: int, max_score: int, label: str) -> str:
    filled = round(score / max_score * 5) if max_score > 0 else 0
    bar = "█" * filled + "░" * (5 - filled)
    return f"{label}[{bar}]{score}/{max_score}"


def _score_trend(closes: np.ndarray) -> tuple[float, str, dict]:
    """
    趋势得分（0-100）
    维度: MA5>MA20>MA60排列 + MA20斜率方向 + 价格距MA20距离
    """
    if len(closes) < 22:
        return 50.0, "数据不足", {}

    ma5  = closes[-5:].mean()
    ma20 = closes[-20:].mean()
    ma60 = closes[-60:].mean() if len(closes) >= 60 else None
    ma20_5d_ago = closes[-25:-5].mean() if len(closes) >= 25 else None
    current = closes[-1]

    # MA排列基础分
    if ma60 is not None:
        if current > ma5 > ma20 > ma60:
            base, label = 90, "多头完美排列"
        elif current > ma5 > ma20:
            base, label = 78, "短中期多头"
        elif current > ma20 > ma60:
            base, label = 65, "站上MA20+MA60"
        elif current > ma20:
            base, label = 55, "站上MA20"
        elif current > ma60:
            base, label = 40, "跌破MA20但上MA60"
        elif current > ma5:
            base, label = 35, "弱势反弹"
        else:
            base = 15 if (ma5 < ma20 and (ma60 is None or ma20 < ma60)) else 28
            label = "空头排列" if base == 15 else "弱势整理"
    else:
        if current > ma5 > ma20:
            base, label = 78, "短中期多头"
        elif current > ma20:
            base, label = 58, "站上MA20"
        else:
            base, label = 28, "跌破MA20"

    # MA20斜率修正（趋势方向确认）
    slope_bonus = 0
    if ma20_5d_ago is not None:
        ma20_slope = (ma20 - ma20_5d_ago) / ma20_5d_ago * 100
        if   ma20_slope >= 0.5:  slope_bonus = 8
        elif ma20_slope >= 0.1:  slope_bonus = 4
        elif ma20_slope <= -0.5: slope_bonus = -8
        elif ma20_slope <= -0.1: slope_bonus = -4
    else:
        ma20_slope = 0.0

    # MA60斜率修正（中期趋势方向确认）
    ma60_slope = 0.0
    ma60_slope_bonus = 0
    if ma60 is not None and len(closes) >= 65:
        ma60_5d_ago = closes[-65:-5].mean()
        ma60_slope = (float(ma60) - float(ma60_5d_ago)) / float(ma60_5d_ago) * 100
        if   ma60_slope >= 0.3:  ma60_slope_bonus = 5
        elif ma60_slope >= 0.1:  ma60_slope_bonus = 2
        elif ma60_slope <= -0.5: ma60_slope_bonus = -6
        elif ma60_slope <= -0.2: ma60_slope_bonus = -3

    dev_from_ma20 = float((current - ma20) / ma20 * 100)
    if   dev_from_ma20 >= 15: dev_penalty = -22
    elif dev_from_ma20 >= 10: dev_penalty = -15
    elif dev_from_ma20 >= 7:  dev_penalty = -8
    elif dev_from_ma20 >= 4:  dev_penalty = -4
    else:                      dev_penalty = 0

    score = float(min(max(base + slope_bonus + ma60_slope_bonus + dev_penalty, 5), 98))
    details = {
        "ma5": round(ma5, 2), "ma20": round(ma20, 2),
        "ma60": round(ma60, 2) if ma60 else None,
        "ma20_slope": round(ma20_slope, 3) if ma20_5d_ago else 0.0,
        "ma60_slope": round(ma60_slope, 3),
        "dev_from_ma20": round(dev_from_ma20, 1),
        "label": label,
    }
    return round(score, 1), label, details


def _score_momentum(closes: np.ndarray, csi300_closes: Optional[np.ndarray]) -> tuple[float, str, dict]:
    """
    动量得分（0-100）
    维度: 5日涨幅 + 20日涨幅 + 5日超额 + RSI健康度
    """
    if len(closes) < 22:
        return 50.0, "数据不足", {}

    ret5  = (closes[-1] / closes[-6]  - 1) * 100
    ret20 = (closes[-1] / closes[-21] - 1) * 100
    rsi   = _calc_rsi(closes)

    # 5日超额（vs 沪深300）
    excess5 = ret5
    if csi300_closes is not None and len(csi300_closes) >= 6:
        idx_ret5 = (csi300_closes[-1] / csi300_closes[-6] - 1) * 100
        excess5 = ret5 - idx_ret5

    # 5日涨幅评分（激进品种允许更大波动）
    def _ret_score(r: float, t: list) -> float:
        for s, th in t:
            if r >= th: return s
        return t[-1][0]

    s5  = _ret_score(ret5,    [(90,5),(75,2),(60,0.5),(50,-0.5),(38,-2),(25,-5),(10,-99)])
    s20 = _ret_score(ret20,   [(90,10),(75,5),(60,1),(50,-1),(38,-5),(25,-10),(10,-99)])
    se  = _ret_score(excess5, [(90,2),(70,0.5),(55,-0.5),(40,-2),(20,-99)])

    # RSI健康度（过热/超卖均调整）
    if   rsi >= 80: rsi_score = 30   # 严重过热
    elif rsi >= 72: rsi_score = 50   # 偏热
    elif rsi >= 55: rsi_score = 80   # 动量强但未过热
    elif rsi >= 40: rsi_score = 65   # 正常区间
    elif rsi >= 30: rsi_score = 55   # 弱势但未极度超卖
    else:           rsi_score = 70   # 超卖反弹机会

    score = round(0.35 * s5 + 0.25 * s20 + 0.20 * se + 0.20 * rsi_score, 1)
    score = float(min(max(score, 5), 95))

    if   score >= 75: mom_label = "动量强劲"
    elif score >= 60: mom_label = "动量偏强"
    elif score >= 45: mom_label = "动量平稳"
    elif score >= 30: mom_label = "动量偏弱"
    else:             mom_label = "动量低迷"

    details = {
        "ret5": round(ret5, 2), "ret20": round(ret20, 2),
        "excess5": round(excess5, 2), "rsi": rsi, "label": mom_label,
    }
    return score, mom_label, details


def _score_volume(amounts: np.ndarray, closes: np.ndarray) -> tuple[float, str]:
    """
    量能得分（0-100）
    放量上涨=买盘确认; 缩量上涨=可持续性存疑; 放量下跌=卖盘出逃
    """
    if len(amounts) < 22 or amounts[-1] <= 0:
        return 50.0, "数据不足"

    avg20 = float(amounts[-21:-1].mean())
    if avg20 <= 0:
        return 50.0, "成交额为零"

    surge = amounts[-1] / avg20
    price_up = closes[-1] > closes[-2]

    if price_up:
        if   surge >= 2.5: score, label = 92, f"巨量上涨{surge:.1f}x"
        elif surge >= 2.0: score, label = 82, f"放量上涨{surge:.1f}x"
        elif surge >= 1.5: score, label = 72, f"温和放量{surge:.1f}x"
        elif surge >= 1.2: score, label = 62, f"量配合{surge:.1f}x"
        elif surge >= 0.8: score, label = 52, f"量平稳{surge:.1f}x"
        else:              score, label = 38, f"缩量上涨{surge:.1f}x"
    else:
        if   surge >= 2.5: score, label = 8,  f"巨量下跌{surge:.1f}x"
        elif surge >= 2.0: score, label = 18, f"放量下跌{surge:.1f}x"
        elif surge >= 1.5: score, label = 28, f"温和放量跌{surge:.1f}x"
        elif surge >= 1.2: score, label = 38, f"量放跌{surge:.1f}x"
        elif surge >= 0.8: score, label = 48, f"量平稳跌{surge:.1f}x"
        else:              score, label = 58, f"缩量下跌{surge:.1f}x"

    return float(score), label


def _score_macro(north_flow, margin, breadth: dict) -> tuple[float, str]:
    """
    宏观资金得分（0-100）—— 所有指数共享同一宏观评分
    北向5日净买/成交额 + 两融余额变化 + 市场宽度
    """
    # 北向得分（优先净买入，降级到成交额活跃度）
    north_score = 50.0
    if north_flow is not None:
        net_s = north_flow.get("net_buy_billion", north_flow["net_buy_billion"]
                               if "net_buy_billion" in north_flow.columns else None)
        if hasattr(north_flow, "columns"):
            net_s = north_flow["net_buy_billion"].dropna()
            if len(net_s) >= 1:
                net5 = float(net_s.tail(5).sum())
                if   net5 >= 150: north_score = 90
                elif net5 >= 80:  north_score = 78
                elif net5 >= 30:  north_score = 65
                elif net5 >= 5:   north_score = 55
                elif net5 >= -10: north_score = 48
                elif net5 >= -40: north_score = 35
                else:             north_score = 20
            else:
                deal_s = north_flow["deal_amt_billion"].dropna()
                if len(deal_s) >= 1:
                    deal_avg = float(deal_s.tail(5).mean())
                    north_score = 65 if deal_avg >= 300 else (52 if deal_avg >= 200 else 40)

    # 两融余额变化得分
    margin_score = 50.0
    if margin is not None and len(margin) >= 5:
        bal = margin["balance_billion"].dropna()
        if len(bal) >= 5:
            chg5 = float(bal.iloc[-1] - bal.iloc[-5])
            if   chg5 >= 300: margin_score = 82
            elif chg5 >= 100: margin_score = 70
            elif chg5 >= 20:  margin_score = 58
            elif chg5 >= -20: margin_score = 50
            elif chg5 >= -100:margin_score = 38
            else:             margin_score = 25

    # 市场宽度（涨停/跌停比）
    lu = breadth.get("limit_up", 0)
    ld = breadth.get("limit_down", 1)
    ratio = lu / max(ld, 1)
    if   ratio >= 10: breadth_score = 90
    elif ratio >= 5:  breadth_score = 78
    elif ratio >= 3:  breadth_score = 65
    elif ratio >= 1.5:breadth_score = 55
    elif ratio >= 0.8:breadth_score = 42
    elif ratio >= 0.3:breadth_score = 28
    else:             breadth_score = 15

    score = round(0.40 * north_score + 0.30 * margin_score + 0.30 * breadth_score, 1)

    if   score >= 72: label = f"资金流入 涨{lu}/跌{ld}"
    elif score >= 55: label = f"资金中性 涨{lu}/跌{ld}"
    else:             label = f"资金流出 涨{lu}/跌{ld}"

    return score, label


# ─── 择时结果数据类 ─────────────────────────────────────────────────────────────

@dataclass
class IndexTiming:
    code: str
    name: str
    region: str
    current_price: float
    composite: float
    signal: str
    trend_score: float
    trend_label: str
    momentum_score: float
    momentum_label: str
    volume_score: float
    volume_label: str
    macro_score: float
    macro_label: str
    details: dict = field(default_factory=dict)

    @property
    def signal_info(self) -> dict:
        for lvl in TIMING_LEVELS:
            if self.composite >= lvl["min"]:
                return lvl
        return TIMING_LEVELS[-1]

    def one_line(self) -> str:
        info = self.signal_info
        d = self.details
        ret5 = f"{d.get('ret5', 0):+.1f}%"
        rsi  = d.get("rsi", 0)
        ma_label = self.trend_label
        return (
            f"{info['emoji']} **{self.name}**({self.code}) "
            f"{info['label']} {self.composite:.0f}分 "
            f"| 5日{ret5} RSI={rsi:.0f} | {ma_label} | {self.volume_label}"
        )

    def detail_block(self) -> str:
        info = self.signal_info
        d = self.details
        ma20 = d.get("ma20", 0)
        ma60 = d.get("ma60")
        stop = f"MA60={ma60:.0f}" if ma60 else f"MA20={ma20:.0f}"
        return (
            f"{info['emoji']} **{self.name}**（ETF: {self.code}）\n"
            f"  综合 **{self.composite:.0f}** | 趋势 {self.trend_score:.0f} "
            f"| 动量 {self.momentum_score:.0f} | 量能 {self.volume_score:.0f} "
            f"| 资金 {self.macro_score:.0f}\n"
            f"  {self.trend_label} | {self.momentum_label} | {self.volume_label}\n"
            f"  建议仓位 **{info['pos']}** | 止损参考 {stop}跌破"
        )


# ─── 单指数综合计算 ─────────────────────────────────────────────────────────────

def _get_signal(score: float) -> str:
    for lvl in TIMING_LEVELS:
        if score >= lvl["min"]:
            return lvl["signal"]
    return "AVOID"


def calc_index_timing(
    code: str,
    info: dict,
    df,
    csi300_closes: Optional[np.ndarray],
    macro_score: float,
    macro_label: str,
) -> Optional[IndexTiming]:
    if df is None or len(df) < 22:
        logger.warning(f"{code} 数据不足，跳过")
        return None

    closes  = df["close"].values.astype(float)
    amounts = df["amount"].values.astype(float)

    t_score, t_label, t_details = _score_trend(closes)
    m_score, m_label, m_details = _score_momentum(closes, csi300_closes)
    v_score, v_label             = _score_volume(amounts, closes)

    composite = round(
        0.35 * t_score + 0.25 * m_score + 0.15 * v_score + 0.25 * macro_score, 1
    )
    # 宏观情绪过热门控：macro≥70(HOT)时不发STRONG_BUY，最高为BUY（上限71）
    if macro_score >= 70:
        composite = min(composite, 71.0)
    # RSI极度过热门控（主要保护海外ETF无宏观门控时）
    if m_details.get("rsi", 50) >= 80:
        composite = min(composite, 68.0)
    signal = _get_signal(composite)

    combined_details = {**t_details, **m_details}

    return IndexTiming(
        code=code,
        name=info["name"],
        region=info["region"],
        current_price=round(float(closes[-1]), 3),
        composite=composite,
        signal=signal,
        trend_score=t_score,
        trend_label=t_label,
        momentum_score=m_score,
        momentum_label=m_label,
        volume_score=v_score,
        volume_label=v_label,
        macro_score=macro_score,
        macro_label=macro_label,
        details=combined_details,
    )


# ─── 飞书推送 ──────────────────────────────────────────────────────────────────

def _read_regime() -> str:
    state_file = Path(__file__).parent / "state" / "regime_state.json"
    if state_file.exists():
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            return data.get("current_regime", "R2")
        except Exception:
            pass
    return "R2"

REGIME_LABEL = {"R1": "趋势牛市", "R2": "震荡市", "R3": "轮动市", "R4": "风险市"}
REGIME_COLOR = {"R1": "green", "R2": "blue", "R3": "yellow", "R4": "red"}


def check_etf_reversal(
    timing: "IndexTiming",
    closes: np.ndarray,
    north_flow,
    margin,
    breadth: dict,
    macro_score: float,
) -> dict | None:
    """
    ETF底部反转候选筛选（满分18分，≥9分入选）。

    硬门槛：① boll_pct < 0.50  ② MACD有改善迹象

    技术面（max 6）: 日线MACD + Boll位置 + RSI
    周期面（max 4）: 周线MACD + 趋势强度
    资金面（max 4）: 北向5日净流入 + 两融余额变化
    情绪面（max 4）: 宏观评分→情绪温度 + 涨跌停宽度
    """
    mb = _compute_macd_boll(closes)
    if not mb:
        return None

    bp = mb.get("boll_pct")
    if bp is None or bp >= 0.50:
        return None

    bar_now  = mb.get("bar_now", 0)
    bar_prev = mb.get("bar_prev", 0)
    macd_improving = (
        mb.get("pre_golden_cross", False) or
        mb.get("cross") == "golden" or
        (bar_now < 0 and bar_now > bar_prev)
    )
    if not macd_improving:
        return None

    pts      = 0
    details  = []
    dim_tech = dim_cycle = dim_fund = dim_senti = 0

    # ── 技术面 ────────────────────────────────────────────────────
    if mb.get("pre_golden_cross"):
        pts += 2; dim_tech += 2; details.append("日线MACD即将金叉🔔")
    elif mb.get("cross") == "golden":
        pts += 1; dim_tech += 1; details.append("日线MACD刚金叉")
    else:
        pts += 1; dim_tech += 1; details.append("日线MACD绿柱收缩")

    if bp <= 0.15:
        pts += 2; dim_tech += 2; details.append(f"Boll触底({bp:.0%})")
    elif bp <= 0.30:
        pts += 1; dim_tech += 1; details.append(f"Boll接近下轨({bp:.0%})")

    rsi = _calc_rsi(closes)
    if rsi < 35:
        pts += 2; dim_tech += 2; details.append(f"RSI超卖{rsi:.0f}")
    elif rsi < 45:
        pts += 1; dim_tech += 1; details.append(f"RSI偏低{rsi:.0f}")

    # ── 周期面 ────────────────────────────────────────────────────
    wk = _weekly_macd_state_from_closes(closes)
    if wk.get("ok"):
        if wk.get("golden") or wk.get("pre_cross"):
            pts += 2; dim_cycle += 2
            details.append("周线MACD" + ("金叉✅" if wk.get("golden") else "即将金叉🔔"))
        elif wk.get("above_zero"):
            pts += 1; dim_cycle += 1; details.append("周线DIF零轴以上")
        elif wk.get("bar_rising"):
            pts += 1; dim_cycle += 1; details.append("周线MACD柱改善")

    ts = timing.trend_score
    if ts >= 60:
        pts += 2; dim_cycle += 2; details.append(f"趋势支撑({ts:.0f}分)")
    elif ts >= 45:
        pts += 1; dim_cycle += 1; details.append(f"趋势中性({ts:.0f}分)")

    # ── 资金面 ────────────────────────────────────────────────────
    if north_flow is not None and hasattr(north_flow, "columns"):
        net_s = north_flow["net_buy_billion"].dropna()
        if len(net_s) >= 1:
            north_5d = float(net_s.tail(5).sum())
            if north_5d > 50:
                pts += 2; dim_fund += 2; details.append(f"北向5日净流入{north_5d:.0f}亿🟢")
            elif north_5d > 10:
                pts += 1; dim_fund += 1; details.append(f"北向5日{north_5d:.0f}亿偏正")
            elif north_5d < -30:
                details.append(f"北向5日净流出{abs(north_5d):.0f}亿⚠️")

    if margin is not None and len(margin) >= 5:
        bal = margin["balance_billion"].dropna()
        if len(bal) >= 5:
            chg5 = float(bal.iloc[-1] - bal.iloc[-5])
            if chg5 > 100:
                pts += 2; dim_fund += 2; details.append(f"两融增加{chg5:.0f}亿💰")
            elif chg5 > 20:
                pts += 1; dim_fund += 1; details.append(f"两融小幅增加{chg5:.0f}亿")

    # ── 情绪面 ────────────────────────────────────────────────────
    # 宏观评分映射为情绪温度
    if macro_score < 40:
        senti_level = "COLD"
        pts += 2; dim_senti += 2; details.append(f"宏观评分{macro_score:.0f}(COLD，最佳买点窗口)🟢")
    elif macro_score < 55:
        senti_level = "NORMAL"
        pts += 2; dim_senti += 2; details.append(f"宏观评分{macro_score:.0f}(NORMAL)🟢")
    elif macro_score < 65:
        senti_level = "WARM"
        pts += 1; dim_senti += 1; details.append(f"宏观评分{macro_score:.0f}(WARM)")
    else:
        senti_level = "HOT"
        details.append(f"宏观评分{macro_score:.0f}(HOT)⚠️反转需等回落")

    lu = breadth.get("limit_up", 0)
    ld = breadth.get("limit_down", 1)
    ratio = lu / max(ld, 1)
    if ratio >= 5:
        pts += 2; dim_senti += 2; details.append(f"市场宽度强({lu}/{ld})✅")
    elif ratio >= 2:
        pts += 1; dim_senti += 1; details.append(f"市场宽度{lu}/{ld}")

    if pts < 9:
        return None

    return {
        "rev_pts":    pts,
        "rev_details": details,
        "boll_pct":   bp,
        "boll_upper": mb.get("boll_upper"),
        "boll_lower": mb.get("boll_lower"),
        "rsi":        rsi,
        "wk":         wk,
        "senti_level": senti_level,
        "dim_tech":   dim_tech,
        "dim_cycle":  dim_cycle,
        "dim_fund":   dim_fund,
        "dim_senti":  dim_senti,
    }


def _etf_reversal_line(timing: "IndexTiming", rev: dict) -> str:
    pts  = rev["rev_pts"]
    bp   = rev.get("boll_pct", 0.5)
    rsi  = rev.get("rsi", 50)
    wk   = rev.get("wk", {})

    stars = "⭐" * min(pts // 3, 5)

    radar = (
        f"{_dim_bar(rev.get('dim_tech',  0), 6, '技术')}  "
        f"{_dim_bar(rev.get('dim_cycle', 0), 4, '周期')}  "
        f"{_dim_bar(rev.get('dim_fund',  0), 4, '资金')}  "
        f"{_dim_bar(rev.get('dim_senti', 0), 4, '情绪')}"
    )

    bu, bl = rev.get("boll_upper"), rev.get("boll_lower")
    boll_str = (
        f"Boll下轨={bl:.3f}  上轨={bu:.3f}  位于{bp:.0%}位"
        if bu and bl else f"Boll位于{bp:.0%}位"
    )

    weekly_str = (
        "金叉✅" if wk.get("golden") else
        "即将金叉🔔" if wk.get("pre_cross") else
        "零轴↑" if wk.get("above_zero") else
        "偏弱⚠️"
    ) if wk.get("ok") else "—"

    details_str = "、".join(rev["rev_details"])

    return (
        f"🔄 **{timing.name}**（{timing.code}）｜{timing.region}  "
        f"{stars}  **{pts}/18分**\n"
        f"  {radar}\n"
        f"  价格 **{timing.current_price:.3f}**  RSI={rsi:.0f}  综合{timing.composite:.0f}分\n"
        f"  {boll_str}\n"
        f"  周线MACD: {weekly_str}\n"
        f"  ✅ {details_str}"
    )


def build_card(timings: list[IndexTiming], run_date: str, regime: str, run_time: str,
               reversals: list[dict] | None = None,
               pullbacks: list[IndexPullback] | None = None,
               trend_adds: list[IndexTrendAdd] | None = None) -> dict:
    regime_label = REGIME_LABEL.get(regime, regime)
    color = REGIME_COLOR.get(regime, "blue")

    # 按综合分排序
    sorted_t = sorted(timings, key=lambda x: x.composite, reverse=True)

    # ── 按地区分组 ───────────────────────────────────────────────
    regions_order = ["A股", "港股", "日股", "美股"]
    grouped: dict[str, list[IndexTiming]] = {}
    for t in sorted_t:
        r = t.region
        grouped.setdefault(r, []).append(t)

    region_sections = []
    for r in regions_order:
        items = grouped.get(r, [])
        if not items:
            continue
        lines = [t.one_line() for t in items]
        region_sections.append(f"**{r}宽基**\n\n" + "\n\n".join(lines))
    summary_md = "\n\n---\n\n".join(region_sections)

    # ── 宏观资金（共享）──────────────────────────────────────────
    a_stock_timings = [t for t in timings if t.region == "A股"]
    if a_stock_timings:
        macro_line = f"资金面：{a_stock_timings[0].macro_label}（权重25%，仅A股适用）"
    else:
        macro_line = ""

    # ── 重点品种详情（评分≥60，最多6个）──────────────────────────
    detail_items = [t for t in sorted_t if t.composite >= 60][:6]
    detail_md = "\n\n".join(t.detail_block() for t in detail_items) if detail_items else "暂无强势品种"

    # ── 评分说明 ─────────────────────────────────────────────────
    score_note = "评分构成：趋势35% + 动量25% + 量能15% + 资金25%（海外ETF资金项=50中性基准）"
    signal_note = "🚀≥72强买 | ✅≥60积极 | 🔵≥45观察 | 🟡≥32等待 | 🔴空仓"

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"指数ETF择时 | {regime_label} | {run_date} {run_time}",
                },
                "template": color,
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**机制: {regime} {regime_label}**\n"
                            f"{score_note}\n{signal_note}"
                        ),
                    },
                },
                {"tag": "hr"},
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": summary_md,
                    },
                },
                {"tag": "hr"},
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**重点品种详情**\n\n{detail_md}",
                    },
                },
                {"tag": "hr"},
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": macro_line,
                    },
                },
            ],
        },
    }

    # ── 底部反转候选 ─────────────────────────────────────────────
    if reversals:
        rev_body = "\n\n".join(
            _etf_reversal_line(r["timing"], r["rev"]) for r in reversals
        )
        card["card"]["elements"] += [
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {
                    "tag": "lark_md",
                    "content": (
                        "**🔄 ━━ ETF底部反转候选（MACD/Boll/周线多维确认）━━**\n\n"
                        + rev_body
                    ),
                },
            },
        ]

    # ── 回踩低吸 + 趋势加仓 ─────────────────────────────────────
    pb_elements = _build_pullback_section(pullbacks or [], trend_adds or [])
    if pb_elements:
        card["card"]["elements"] += pb_elements

    card["card"]["elements"].append({
        "tag": "note",
        "elements": [
            {
                "tag": "plain_text",
                "content": "ETF择时仅供参考，海外ETF受T+0及汇率影响，仓位需结合个人风险承受力。止损线触及请严格执行。",
            }
        ],
    })
    return card


def send_to_feishu(card: dict, webhook: str = FEISHU_WEBHOOK) -> bool:
    return post_card(card, [webhook])


# ─── 主流程 ────────────────────────────────────────────────────────────────────

# ─── 指数回踩低吸 + 趋势加仓 ──────────────────────────────────────────────────

@dataclass
class IndexPullback:
    code: str
    name: str
    close: float
    ma20: float
    ma20_dev_pct: float
    pullback_pct: float       # 从10日高点回撤
    rsi14: float
    ma60_slope: float         # MA60斜率(%)
    vol_shrink: float         # 量能萎缩比
    entry_price: float        # 建议分批入场价
    reasons: list[str] = field(default_factory=list)


@dataclass
class IndexTrendAdd:
    code: str
    name: str
    close: float
    breakout_pct: float       # 突破20日高点幅度
    vol_ratio: float          # 量比
    weekly_up: bool           # 周线趋势向上
    reasons: list[str] = field(default_factory=list)


def scan_index_pullbacks(etf_dfs: dict, timings: list[IndexTiming]) -> list[IndexPullback]:
    """
    扫描指数ETF回踩低吸窗口。
    条件：MA60上行 + 回踩MA20附近(-3%~+1%) + RSI 30-50 + 近3日量能萎缩
    """
    results: list[IndexPullback] = []
    for t in timings:
        df = etf_dfs.get(t.code)
        if df is None or len(df) < 65:
            continue

        closes = df["close"].values.astype(float)
        current = closes[-1]
        ma20 = closes[-20:].mean()
        ma60 = closes[-60:].mean()

        # MA60 必须上行
        if len(closes) >= 65:
            ma60_prev = closes[-65:-5].mean()
            ma60_slope = (ma60 - ma60_prev) / ma60_prev * 100
        else:
            continue
        if ma60_slope < 0.1:
            continue

        # 回踩MA20: 偏离在 -3% ~ +1.5%
        ma20_dev = (current - ma20) / ma20 * 100
        if ma20_dev < -3.5 or ma20_dev > 1.5:
            continue

        # 10日高点回撤 >= 2.5%
        high_10d = closes[-10:].max()
        pullback = (high_10d - current) / high_10d * 100
        if pullback < 2.5:
            continue

        # RSI 30-52
        rsi14 = _calc_rsi(closes)
        if rsi14 < 28 or rsi14 > 52:
            continue

        # 量能萎缩
        vol_shrink = 1.0
        if "amount" in df.columns and len(df) >= 22:
            amounts = df["amount"].values.astype(float)
            vol_3d = amounts[-3:].mean()
            vol_20d = amounts[-20:-3].mean()
            vol_shrink = vol_3d / vol_20d if vol_20d > 0 else 1.0

        if vol_shrink > 1.2:
            continue

        reasons = []
        reasons.append(f"MA60上行+{ma60_slope:.1f}%（中期趋势完好）")
        reasons.append(f"回踩MA20({ma20_dev:+.1f}%)")
        reasons.append(f"10日回撤{pullback:.1f}%")
        reasons.append(f"RSI={rsi14:.0f}")
        if vol_shrink <= 0.7:
            reasons.append(f"缩量{vol_shrink:.1f}x（抛压衰竭）")

        # 分批入场价：MA20附近
        entry = round(min(current * 0.99, ma20 * 1.005), 3)

        results.append(IndexPullback(
            code=t.code, name=t.name, close=round(current, 3),
            ma20=round(ma20, 3), ma20_dev_pct=round(ma20_dev, 2),
            pullback_pct=round(pullback, 2), rsi14=round(rsi14, 1),
            ma60_slope=round(ma60_slope, 2), vol_shrink=round(vol_shrink, 2),
            entry_price=entry, reasons=reasons,
        ))

    results.sort(key=lambda s: -s.pullback_pct)
    return results


def scan_index_trend_add(etf_dfs: dict, timings: list[IndexTiming]) -> list[IndexTrendAdd]:
    """
    扫描指数趋势加仓信号：突破20日高点 + 放量。
    """
    results: list[IndexTrendAdd] = []
    for t in timings:
        df = etf_dfs.get(t.code)
        if df is None or len(df) < 25:
            continue

        closes = df["close"].values.astype(float)
        current = closes[-1]
        high_20d = closes[-21:-1].max()

        if current <= high_20d:
            continue

        breakout = (current / high_20d - 1) * 100

        vol_ratio = 1.0
        if "amount" in df.columns and len(df) >= 6:
            amounts = df["amount"].values.astype(float)
            vol_ratio = amounts[-1] / amounts[-6:-1].mean() if amounts[-6:-1].mean() > 0 else 1.0

        if vol_ratio < 1.2:
            continue

        # 周线趋势向上
        weekly_up = False
        if len(closes) >= 30:
            w5 = closes[-5:].mean()
            w10 = closes[-10:].mean()
            weekly_up = w5 > w10

        reasons = []
        reasons.append(f"突破20日高点+{breakout:.1f}%")
        reasons.append(f"量比{vol_ratio:.1f}x")
        if weekly_up:
            reasons.append("周趋势向上")

        results.append(IndexTrendAdd(
            code=t.code, name=t.name, close=round(current, 3),
            breakout_pct=round(breakout, 2), vol_ratio=round(vol_ratio, 2),
            weekly_up=weekly_up, reasons=reasons,
        ))

    results.sort(key=lambda s: -s.breakout_pct)
    return results


def _build_pullback_section(pullbacks: list[IndexPullback], trend_adds: list[IndexTrendAdd]) -> list[dict]:
    """构建回踩+趋势加仓的飞书卡片 elements。"""
    elements = []
    if pullbacks:
        lines = ["**📉 指数回踩低吸窗口**\n"]
        for p in pullbacks[:6]:
            lines.append(
                f"🌟 **{p.name}**({p.code}) "
                f"回撤{p.pullback_pct:.1f}% MA20偏{p.ma20_dev_pct:+.1f}% "
                f"RSI={p.rsi14:.0f} 量{p.vol_shrink:.1f}x"
            )
            lines.append(f"  └ {'; '.join(p.reasons)}")
            lines.append(f"  └ 建议入场: ⚡首仓50%现价 + 回调至**{p.entry_price}**补50%")
        elements.append({"tag": "markdown", "content": "\n".join(lines)})
        elements.append({"tag": "hr"})

    if trend_adds:
        lines = ["**📈 指数趋势加仓**\n"]
        for ta in trend_adds[:6]:
            wk = "✅周线↑" if ta.weekly_up else ""
            lines.append(
                f"🔥 **{ta.name}**({ta.code}) "
                f"突破+{ta.breakout_pct:.1f}% 量{ta.vol_ratio:.1f}x {wk}"
            )
            lines.append(f"  └ {'; '.join(ta.reasons)}")
        elements.append({"tag": "markdown", "content": "\n".join(lines)})
        elements.append({"tag": "hr"})

    return elements


def run() -> None:
    now      = datetime.today()
    today_str = now.strftime("%Y-%m-%d")
    time_str  = now.strftime("%H:%M")
    from utils import is_trading_day as _it
    if not _it():
        logger.info("非交易日，跳过")
        return

    logger.info(f"=== 指数ETF择时启动 {today_str} {time_str} ===")

    # ── 数据获取 ─────────────────────────────────────────────────
    logger.info("获取ETF K线数据...")
    etf_dfs = {}
    for code, info in INDEX_UNIVERSE.items():
        etf_dfs[code] = _fetch_etf_df(code, days=120)
        status = "OK" if etf_dfs[code] is not None else "FAIL"
        logger.info(f"  {info['name']}({code}): {status}")

    # CSI300 ETF收盘序列（用于A股超额收益计算）
    csi300_df = etf_dfs.get(CSI300_CODE)
    csi300_closes = csi300_df["close"].values.astype(float) if csi300_df is not None else None

    logger.info("获取宏观数据（北向/两融/宽度）...")
    north_flow = df_api.get_north_flow(days=10)
    margin     = df_api.get_margin_balance(days=10)
    breadth    = df_api.get_market_breadth()
    logger.info(f"  北向: {'OK' if north_flow is not None else '降级'} | "
                f"两融: {'OK' if margin is not None else '降级'} | "
                f"涨停{breadth.get('limit_up',0)}/跌停{breadth.get('limit_down',0)}")

    # ── 宏观评分（共享） ─────────────────────────────────────────
    macro_score, macro_label = _score_macro(north_flow, margin, breadth)
    logger.info(f"宏观资金评分: {macro_score:.1f} — {macro_label}")

    # ── 各ETF评分 ────────────────────────────────────────────────
    regime = _read_regime()
    timings:   list[IndexTiming] = []
    reversals: list[dict] = []
    for code, info in INDEX_UNIVERSE.items():
        # 海外/商品ETF宏观资金用50中性基准（北向/两融/宽度不适用）
        m_score  = macro_score if info["region"] == "A股" else 50.0
        m_label  = macro_label if info["region"] == "A股" else "中性基准(非A股)"

        # 海外ETF超额收益基准为自身（无需CSI300对比）
        benchmark = csi300_closes if info["region"] == "A股" else None

        t = calc_index_timing(
            code, info, etf_dfs.get(code),
            benchmark, m_score, m_label,
        )
        if t:
            timings.append(t)
            logger.info(f"  {t.name}({t.region}): {t.composite:.1f} [{t.signal}] "
                        f"趋势{t.trend_score:.0f} 动量{t.momentum_score:.0f} "
                        f"量能{t.volume_score:.0f} 资金{t.macro_score:.0f}")

            # 底部反转候选检测（仅A股ETF有完整宏观数据）
            df_etf = etf_dfs.get(code)
            if df_etf is not None and len(df_etf) >= 35:
                closes_full = df_etf["close"].values.astype(float)
                rev = check_etf_reversal(
                    t, closes_full,
                    north_flow if info["region"] == "A股" else None,
                    margin     if info["region"] == "A股" else None,
                    breadth    if info["region"] == "A股" else {},
                    m_score,
                )
                if rev is not None:
                    reversals.append({"timing": t, "rev": rev})
                    logger.info(f"    └─ 底部反转候选: {t.name} {rev['rev_pts']}/18分")

    if not timings:
        logger.error("无有效ETF数据，退出")
        return

    reversals.sort(key=lambda r: -r["rev"]["rev_pts"])
    logger.info(f"ETF底部反转候选: {len(reversals)} 只 — {[r['timing'].name for r in reversals]}")

    # ── 回踩低吸 + 趋势加仓扫描 ─────────────────────────────────
    pullbacks = scan_index_pullbacks(etf_dfs, timings)
    trend_adds = scan_index_trend_add(etf_dfs, timings)
    logger.info(f"指数回踩低吸: {len(pullbacks)} 只 — {[p.name for p in pullbacks]}")
    logger.info(f"指数趋势加仓: {len(trend_adds)} 只 — {[t.name for t in trend_adds]}")

    # ── 推送飞书（仅信号变化 或 关键时点推送）──────────────────────
    current_signals = {t.code: t.signal for t in timings}
    should_push = False

    # 关键时点必推（开盘/午盘/尾盘各一次）
    _KEY_TIMES = {"09:25", "09:30", "09:35", "11:25", "11:30", "14:25", "14:30", "14:45", "14:50"}
    if time_str in _KEY_TIMES:
        should_push = True
        logger.info(f"关键时点 {time_str}，必推")

    # 对比上一次运行的信号，有变化则推
    if not should_push:
        snap_file_check = LOG_DIR / f"index_timing_{today_str}.json"
        if snap_file_check.exists():
            try:
                prev_data = json.loads(snap_file_check.read_text(encoding="utf-8"))
                prev_runs = prev_data.get("runs", [])
                if prev_runs:
                    last_run = prev_runs[-1]
                    prev_signals = {e["code"]: e["signal"] for e in last_run.get("etfs", [])}
                    changed = [code for code, sig in current_signals.items()
                               if prev_signals.get(code) != sig]
                    if changed:
                        should_push = True
                        changed_names = [next((t.name for t in timings if t.code == c), c) for c in changed[:3]]
                        logger.info(f"信号变化: {', '.join(changed_names)} 等{len(changed)}只，推送")
                    else:
                        logger.info("信号无变化，跳过推送")
                else:
                    should_push = True  # 今天首次
            except Exception:
                should_push = True
        else:
            should_push = True  # 今天首次

    if should_push:
        card = build_card(timings, today_str, regime, time_str, reversals=reversals,
                          pullbacks=pullbacks, trend_adds=trend_adds)
        send_to_feishu(card)

    # ── 保存快照 ─────────────────────────────────────────────────
    snapshot = {
        "date": today_str,
        "time": time_str,
        "regime": regime,
        "macro_score": macro_score,
        "etfs": [
            {
                "code": t.code, "name": t.name, "region": t.region,
                "composite": t.composite, "signal": t.signal,
                "trend": t.trend_score, "momentum": t.momentum_score,
                "volume": t.volume_score, "macro": t.macro_score,
                "details": t.details,
            }
            for t in timings
        ],
    }
    snap_file = LOG_DIR / f"index_timing_{today_str}.json"
    existing = json.loads(snap_file.read_text(encoding="utf-8")) if snap_file.exists() else {}
    runs = existing.get("runs", [])
    runs.append(snapshot)
    existing["date"] = today_str
    existing["regime"] = regime
    existing["runs"] = runs
    snap_file.write_text(json.dumps(existing, ensure_ascii=False, indent=2))
    logger.info(f"快照已追加: {snap_file} (第{len(runs)}次)")
    logger.info("=== 指数ETF择时完成 ===")


if __name__ == "__main__":
    run()
