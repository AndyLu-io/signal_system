"""
通用技术指标库：EMA / RSI / MACD / Bollinger / 周月线重采样 / 渲染条。

把原本散落在 sentiment_gauge / stock_timing / index_timing / etf_reversal /
tail_market_scanner 里的重复实现合并到一处。
"""

from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd


# ─── 基础 ──────────────────────────────────────────────────────────────────────

def ema_series(s: pd.Series, span: int) -> pd.Series:
    """Pandas Series 的 EMA。"""
    return s.ewm(span=span, adjust=False).mean()


def ema_array(arr: np.ndarray, period: int) -> np.ndarray:
    """numpy 数组上的 EMA（递推法，与 pandas adjust=False 等价）。"""
    out = np.empty(len(arr))
    if len(arr) == 0:
        return out
    k = 2.0 / (period + 1)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = arr[i] * k + out[i - 1] * (1 - k)
    return out


# ─── RSI ──────────────────────────────────────────────────────────────────────

def rsi(closes: np.ndarray | pd.Series, period: int = 14) -> float:
    """简易 RSI（最近 period 条数据），返回 0-100 的最新值。"""
    arr = np.asarray(closes, dtype=float)
    if len(arr) < period + 1:
        return 50.0
    deltas = np.diff(arr[-period - 1:])
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = float(gains.mean())
    avg_loss = float(losses.mean())
    if avg_loss < 1e-9:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100.0 - 100.0 / (1.0 + rs), 2)


def rsi_full_series(closes: pd.Series, period: int = 14) -> pd.Series:
    """返回完整 RSI 序列（用于需要逐日比较的场景）。"""
    delta = closes.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = (-delta.clip(upper=0)).rolling(period).mean()
    return 100 - 100 / (1 + gain / loss.replace(0, 1e-9))


# ─── MACD ─────────────────────────────────────────────────────────────────────

def macd_state(closes: np.ndarray | pd.Series) -> dict:
    """
    返回 MACD 当下状态字典，包含：
      dif/dea/bar_now/bar_prev/cross/pre_golden_cross
    cross: 'golden' / 'death' / None
    pre_golden_cross: DIF<DEA 但缺口正在收窄且 DIF 上扬（即将金叉）
    """
    arr = np.asarray(closes, dtype=float)
    if len(arr) < 35:
        return {}

    ema12 = ema_array(arr, 12)
    ema26 = ema_array(arr, 26)
    dif = ema12 - ema26
    dea = ema_array(dif, 9)
    bar = (dif - dea) * 2

    d, e = float(dif[-1]), float(dea[-1])
    dp, ep = float(dif[-2]), float(dea[-2])

    cross: Optional[str] = None
    if dp < ep and d >= e:
        cross = "golden"
    elif dp > ep and d <= e:
        cross = "death"
    pre_golden_cross = d < e and d > dp and (e - d) < (ep - dp)

    return {
        "dif": d,
        "dea": e,
        "bar_now": float(bar[-1]),
        "bar_prev": float(bar[-2]),
        "cross": cross,
        "pre_golden_cross": pre_golden_cross,
    }


# ─── Bollinger Bands ──────────────────────────────────────────────────────────

def bollinger(closes: np.ndarray | pd.Series, period: int = 20, std_mult: float = 2.0) -> dict:
    """返回 {upper, lower, pct}（pct = 当前价在通道中位置 0..1）。"""
    arr = np.asarray(closes, dtype=float)
    if len(arr) < period:
        return {"upper": None, "lower": None, "pct": 0.5}
    ma = float(arr[-period:].mean())
    std = float(arr[-period:].std())
    upper = ma + std_mult * std
    lower = ma - std_mult * std
    width = upper - lower
    pct = float((arr[-1] - lower) / width) if width > 0 else 0.5
    return {"upper": upper, "lower": lower, "pct": pct}


# ─── 周/月重采样 ──────────────────────────────────────────────────────────────

def resample_period(df: pd.DataFrame, freq: str) -> Optional[pd.DataFrame]:
    """
    将日线 DataFrame(date, close, volume) 按 freq 重采样。
    freq: 'W' 周 / 'ME' 月末。
    """
    if df is None or "date" not in df.columns or "close" not in df.columns:
        return None
    try:
        idx = df.set_index("date")
        closes = idx["close"].resample(freq).last().dropna()
        out = closes.to_frame()
        if "volume" in idx.columns:
            out["volume"] = idx["volume"].resample(freq).sum()
        return out.reset_index()
    except Exception:
        return None


def weekly_macd_state(df: pd.DataFrame) -> dict:
    """周线 MACD 状态。需要 ≥30 根周K（约 7 个月日线）。"""
    df_w = resample_period(df, "W")
    if df_w is None or len(df_w) < 30:
        return {"ok": False}
    state = macd_state(df_w["close"].astype(float).values)
    if not state:
        return {"ok": False}
    bar_now = state["bar_now"]
    bar_prev = state["bar_prev"]
    return {
        "ok": True,
        "golden": state["cross"] == "golden",
        "pre_cross": state["pre_golden_cross"],
        "above_zero": state["dif"] > 0,
        "bar_rising": bar_now > bar_prev,
        "dif": round(state["dif"], 4),
        "dea": round(state["dea"], 4),
    }


def monthly_trend_state(df: pd.DataFrame) -> dict:
    """月线趋势（用 MA3/MA6 替代 MACD，避免月K数量不足）。"""
    df_m = resample_period(df, "ME")
    if df_m is None or len(df_m) < 6:
        return {"ok": False}
    close = df_m["close"].astype(float)
    ma3 = close.rolling(3).mean()
    ma6 = close.rolling(6).mean()
    cur = float(close.iloc[-1])
    ma3_v = float(ma3.iloc[-1])
    ma6_v = float(ma6.iloc[-1])
    ma3_p = float(ma3.iloc[-2]) if len(ma3.dropna()) >= 2 else ma3_v
    dif_proxy = ma3_v - ma6_v
    dif_p = float(ma3.iloc[-2]) - float(ma6.iloc[-2]) if len(ma6.dropna()) >= 2 else dif_proxy
    return {
        "ok": True,
        "above_ma6": cur > ma6_v,
        "ma3_rising": ma3_v > ma3_p,
        "dif_positive": dif_proxy > 0,
        "dif_rising": dif_proxy > dif_p,
        "ma3": round(ma3_v, 4),
        "ma6": round(ma6_v, 4),
    }


# ─── 渲染：维度进度条 ─────────────────────────────────────────────────────────

def dim_bar(score: int, max_score: int, label: str) -> str:
    """label[████░░]score/max — 5 格"""
    filled = round(score / max_score * 5) if max_score > 0 else 0
    filled = max(0, min(5, filled))
    bar = "█" * filled + "░" * (5 - filled)
    return f"{label}[{bar}]{score}/{max_score}"
