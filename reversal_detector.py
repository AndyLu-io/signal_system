"""
通用底部反转候选检测器（满分 18 分，≥9 分入选）。

把原本散落在 etf_reversal / stock_timing / index_timing 三处几乎一致的
反转评分逻辑合并到一处。资金面/政策面这两个维度可被调用方"喂"任意指标，
因此用回调（Protocol）实现，让 ETF / 个股 / 宽基指数三个调用方共用。

硬门槛（必须同时满足）：
  ① 价格在布林带中位以下（boll_pct < 0.50）
  ② MACD 有改善迹象（即将金叉 / 刚金叉 / 绿柱收缩）

5 维度满分：技术 6 + 周期 4 + 资金 X + 政策 Y + 情绪 Z = 18
（X+Y+Z 默认 2/4/2，调用方可重新分配权重）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import pandas as pd

from indicators import (
    bollinger,
    macd_state,
    rsi,
    weekly_macd_state,
    monthly_trend_state,
)


# ─── 输入回调：资金面、政策面 ─────────────────────────────────────────────────

# 返回 (score_inc, dim_inc, detail_text) 列表，可触发多个加分项。
DimScorer = Callable[[], list[tuple[int, int, str]]]


@dataclass(frozen=True)
class ReversalReport:
    pts: int
    details: list[str]
    boll_pct: float
    boll_upper: Optional[float]
    boll_lower: Optional[float]
    rsi: float
    weekly: dict
    monthly: dict
    dim_tech: int
    dim_cycle: int
    dim_fund: int
    dim_policy: int
    dim_senti: int

    def to_dict(self) -> dict:
        return {
            "rev_pts": self.pts,
            "rev_details": self.details,
            "boll_pct": self.boll_pct,
            "boll_upper": self.boll_upper,
            "boll_lower": self.boll_lower,
            "rsi": self.rsi,
            "wk": self.weekly,
            "mk": self.monthly,
            "dim_tech": self.dim_tech,
            "dim_cycle": self.dim_cycle,
            "dim_fund": self.dim_fund,
            "dim_policy": self.dim_policy,
            "dim_senti": self.dim_senti,
        }


# ─── 内部评分组件 ──────────────────────────────────────────────────────────────

def _score_technical(boll_pct: float, mb: dict, rsi_val: float) -> tuple[int, int, list[str]]:
    """技术面（满分 6）：日线 MACD + Boll + RSI"""
    pts = 0
    details: list[str] = []
    dim = 0

    if mb.get("pre_golden_cross"):
        pts += 2; dim += 2; details.append("日线MACD即将金叉🔔")
    elif mb.get("cross") == "golden":
        pts += 1; dim += 1; details.append("日线MACD刚金叉")
    else:
        pts += 1; dim += 1; details.append("日线MACD绿柱收缩")

    if boll_pct <= 0.15:
        pts += 2; dim += 2; details.append(f"Boll触底({boll_pct:.0%})")
    elif boll_pct <= 0.30:
        pts += 1; dim += 1; details.append(f"Boll接近下轨({boll_pct:.0%})")

    if rsi_val < 35:
        pts += 2; dim += 2; details.append(f"RSI超卖{rsi_val:.0f}")
    elif rsi_val < 45:
        pts += 1; dim += 1; details.append(f"RSI偏低{rsi_val:.0f}")

    return pts, dim, details


def _score_cycle(wk: dict, mk: dict) -> tuple[int, int, list[str]]:
    """周期面（满分 4）：周线 MACD + 月线趋势"""
    pts = 0
    details: list[str] = []
    dim = 0

    if wk.get("ok"):
        if wk.get("golden") or wk.get("pre_cross"):
            pts += 2; dim += 2
            details.append("周线MACD" + ("金叉✅" if wk.get("golden") else "即将金叉🔔"))
        elif wk.get("above_zero"):
            pts += 1; dim += 1; details.append("周线DIF零轴以上")
        elif wk.get("bar_rising"):
            pts += 1; dim += 1; details.append("周线MACD柱改善")

    if mk.get("ok"):
        above = mk.get("above_ma6", False)
        rising = mk.get("ma3_rising", False)
        dif_pos = mk.get("dif_positive", False)
        dif_rise = mk.get("dif_rising", False)
        if above and rising:
            pts += 2; dim += 2; details.append("月线站MA6且上升✅")
        elif above or dif_pos:
            pts += 1; dim += 1
            details.append("月线MA6支撑" if above else "月线趋势向好")
        elif dif_rise:
            pts += 1; dim += 1; details.append("月线趋势改善中")

    return pts, dim, details


def _score_fund_main_force(main_force_flow: float) -> list[tuple[int, int, str]]:
    """默认资金面评分（满分 2）：基于主力净流入。"""
    if main_force_flow > 0.5:
        return [(2, 2, f"主力净流入{main_force_flow:.1f}亿💰")]
    if main_force_flow >= -0.3:
        return [(1, 1, "主力资金中性")]
    return []


def _score_policy_static(
    f_policy: float,
    f_earnings: float,
    north_5d: Optional[float],
) -> list[tuple[int, int, str]]:
    """默认政策面评分（满分 4）：静态评分 + 北向 5 日合计。"""
    out: list[tuple[int, int, str]] = []
    if f_policy >= 85:
        out.append((2, 2, f"强政策支撑(f_policy={f_policy:.0f})"))
    elif f_policy >= 70:
        out.append((1, 1, f"政策评分{f_policy:.0f}"))
    if f_earnings >= 75:
        out.append((1, 1, f"盈利质量佳(f_earnings={f_earnings:.0f})"))
    if north_5d is not None and north_5d > 50:
        out.append((1, 1, f"北向5日净流入{north_5d:.0f}亿"))
    return out


def _score_sentiment_level(level: str) -> list[tuple[int, int, str]]:
    """默认情绪面评分（满分 2）：来自 sentiment_gauge.level。"""
    if level in ("COLD", "NORMAL", "NEUTRAL"):
        return [(2, 2, f"市场情绪{level}(最佳买点窗口)🟢")]
    if level == "WARM":
        return [(1, 1, "市场情绪温和，可介入")]
    # HOT / OVERHEATED：给提示但不加分
    return [(0, 0, f"市场情绪{level}⚠️，反转需等回落")]


# ─── 主入口 ───────────────────────────────────────────────────────────────────

def detect(
    closes: np.ndarray | pd.Series,
    daily_df: pd.DataFrame,
    *,
    min_pts: int = 9,
    fund_scorer: DimScorer | None = None,
    policy_scorer: DimScorer | None = None,
    sentiment_scorer: DimScorer | None = None,
) -> Optional[ReversalReport]:
    """
    通用反转检测。

    closes      : 日线收盘价数组 / Series
    daily_df    : 日线 DataFrame（含 date/close/volume）—— 用于周/月重采样
    fund_scorer : 资金面评分函数（不传则跳过该维度）
    policy_scorer / sentiment_scorer 同理

    返回 ReversalReport 或 None（未通过硬门槛 / 未达 min_pts）。
    """
    arr = np.asarray(closes, dtype=float)
    if len(arr) < 35:
        return None

    # ── 硬门槛 1：Boll 在中位以下 ───────────────────────────────────────────
    boll = bollinger(arr, period=20, std_mult=2.0)
    bp = boll["pct"]
    if bp >= 0.50:
        return None

    # ── 硬门槛 2：MACD 改善 ─────────────────────────────────────────────────
    mb = macd_state(arr)
    if not mb:
        return None

    macd_improving = (
        mb.get("pre_golden_cross", False)
        or mb.get("cross") == "golden"
        or (mb["bar_now"] < 0 and mb["bar_now"] > mb["bar_prev"])
    )
    if not macd_improving:
        return None

    rsi_val = rsi(arr, period=14)
    wk = weekly_macd_state(daily_df)
    mk = monthly_trend_state(daily_df)

    # ── 维度 1: 技术 ────────────────────────────────────────────────────
    pts, dim_tech, details = _score_technical(bp, mb, rsi_val)

    # ── 维度 2: 周期 ────────────────────────────────────────────────────
    p2, dim_cycle, d2 = _score_cycle(wk, mk)
    pts += p2
    details.extend(d2)

    # ── 维度 3: 资金 ────────────────────────────────────────────────────
    dim_fund = 0
    if fund_scorer:
        for inc, d_inc, txt in fund_scorer():
            pts += inc
            dim_fund += d_inc
            if txt:
                details.append(txt)

    # ── 维度 4: 政策 ────────────────────────────────────────────────────
    dim_policy = 0
    if policy_scorer:
        for inc, d_inc, txt in policy_scorer():
            pts += inc
            dim_policy += d_inc
            if txt:
                details.append(txt)

    # ── 维度 5: 情绪 ────────────────────────────────────────────────────
    dim_senti = 0
    if sentiment_scorer:
        for inc, d_inc, txt in sentiment_scorer():
            pts += inc
            dim_senti += d_inc
            if txt:
                details.append(txt)

    if pts < min_pts:
        return None

    return ReversalReport(
        pts=pts,
        details=details,
        boll_pct=bp,
        boll_upper=boll["upper"],
        boll_lower=boll["lower"],
        rsi=rsi_val,
        weekly=wk,
        monthly=mk,
        dim_tech=dim_tech,
        dim_cycle=dim_cycle,
        dim_fund=dim_fund,
        dim_policy=dim_policy,
        dim_senti=dim_senti,
    )


# ─── 便捷构造器：常见调用方组合 ───────────────────────────────────────────────

def make_main_force_fund_scorer(main_force_flow: float) -> DimScorer:
    """ETF / 个股使用：主力净流入"""
    return lambda: _score_fund_main_force(main_force_flow)


def make_static_policy_scorer(
    f_policy: float,
    f_earnings: float,
    north_5d: Optional[float],
) -> DimScorer:
    """ETF / 个股使用：静态政策 + 北向 5 日"""
    return lambda: _score_policy_static(f_policy, f_earnings, north_5d)


def make_level_sentiment_scorer(level: str) -> DimScorer:
    """根据 sentiment_gauge.level 给情绪分"""
    return lambda: _score_sentiment_level(level)
