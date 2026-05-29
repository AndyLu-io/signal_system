"""
市场机制识别引擎
6指标加权评分 → R1/R2/R3/R4 机制判断
支持机制切换延迟确认（防止频繁误切换）
"""

from __future__ import annotations
import json
import logging
from pathlib import Path
from typing import Optional
import numpy as np
import pandas as pd

from config import (REGIME_WEIGHTS, REGIME_THRESHOLDS,
                    REGIME_DOWN_CONFIRM, REGIME_UP_CONFIRM,
                    REGIME_PARAMS, INDEX_CSI300, INDEX_CSI500)
import data_fetcher as df_api

logger = logging.getLogger(__name__)
STATE_FILE = Path(__file__).parent / "state" / "regime_state.json"


# ─── 六个子指标评分（0-10分） ────────────────────────────────────────────────

def _score_s1_trend(csi300: Optional[pd.DataFrame]) -> float:
    """沪深300趋势强度：20日均线斜率 + 价格与均线的关系"""
    if csi300 is None or len(csi300) < 22:
        return 5.0
    close = csi300["close"].values
    ma20 = close[-20:].mean()
    current = close[-1]
    # 斜率：最近5日 vs 前15日均值
    slope_sign = 1 if close[-5:].mean() > close[-20:-5].mean() else -1
    above_ma = current > ma20

    if above_ma and slope_sign > 0:
        # 在均线上方且上升 → 8-10分
        strength = min((current - ma20) / ma20 * 100, 5)  # 偏离度
        return round(8 + strength * 0.4, 1)
    elif above_ma and slope_sign < 0:
        # 在均线上方但下降 → 5-7分
        return 6.0
    elif not above_ma and slope_sign > 0:
        # 在均线下方但回升 → 4-5分
        return 4.5
    else:
        # 在均线下方且下降 → 0-3分
        drop = min((ma20 - current) / ma20 * 100, 5)
        return round(max(0, 3 - drop * 0.6), 1)


def _score_s2_volume(csi300: Optional[pd.DataFrame]) -> float:
    """成交额动能：当日/近5日成交额 vs 30日均值"""
    if csi300 is None or len(csi300) < 32:
        return 5.0
    if "amount" not in csi300.columns:
        return 5.0
    amounts = csi300["amount"].values
    avg30 = amounts[-32:-2].mean()
    recent5 = amounts[-5:].mean()
    if avg30 == 0:
        return 5.0
    ratio = recent5 / avg30
    if ratio >= 1.3:
        return 9.0
    elif ratio >= 1.1:
        return 7.5
    elif ratio >= 0.9:
        return 5.5
    elif ratio >= 0.7:
        return 3.5
    else:
        return 1.5


def _score_s3_north(north_flow: Optional[pd.DataFrame]) -> float:
    """
    北向资金评分（0-10）。
    优先使用 net_buy_billion（精确净买入），若全为 null 则用成交总额活跃度偏离代理。
    无数据返回中性 5.0。
    """
    if north_flow is None or len(north_flow) < 2:
        return 5.0

    # ── 优先：精确净买入 ────────────────────────────────────────────────────
    if "net_buy_billion" in north_flow.columns:
        net_series = north_flow["net_buy_billion"].dropna()
        if len(net_series) >= 2:
            flow5 = net_series.tail(5).sum()
            if flow5 >= 100: return 9.5
            if flow5 >= 50:  return 8.0
            if flow5 >= 10:  return 6.5
            if flow5 >= 0:   return 5.0
            if flow5 >= -30: return 3.5
            if flow5 >= -50: return 2.0
            return 0.5

    # ── 降级：成交总额活跃度偏离（方向未知，保守给分）───────────────────────
    if "deal_amt_billion" in north_flow.columns:
        amt_series = north_flow["deal_amt_billion"].dropna()
        if len(amt_series) >= 5:
            recent = float(amt_series.iloc[-1])
            avg5   = float(amt_series.tail(5).mean())
            if avg5 > 0:
                deviation = (recent - avg5) / avg5  # 相对偏离
                # 活跃度高 → 略偏积极；萎缩 → 略偏消极；方向不确定，不给极端分
                if deviation > 0.3:   return 6.0   # 成交明显放量
                if deviation > 0.1:   return 5.5
                if deviation > -0.1:  return 5.0   # 正常
                if deviation > -0.3:  return 4.5
                return 4.0                           # 成交明显萎缩

    return 5.0


def _score_s4_rotation(etf_prices: dict[str, Optional[pd.DataFrame]]) -> float:
    """
    板块轮动速度：用核心ETF的动量一致性衡量
    各ETF 5日涨幅的方差越小 → 趋势越一致 → 分数越高
    """
    returns_5d = []
    for code, df in etf_prices.items():
        if df is not None and len(df) >= 6:
            r = (df["close"].iloc[-1] / df["close"].iloc[-6] - 1) * 100
            returns_5d.append(r)
    if len(returns_5d) < 3:
        return 5.0
    arr = np.array(returns_5d)
    positive_ratio = (arr > 0).mean()
    std = arr.std()
    # 正向ETF比例高、分歧小 → 趋势清晰
    if positive_ratio >= 0.7 and std < 3:
        return 8.5
    elif positive_ratio >= 0.6 and std < 5:
        return 6.5
    elif positive_ratio >= 0.5:
        return 5.0
    elif positive_ratio >= 0.3:
        return 3.5
    else:
        return 1.5


def _score_s5_margin(margin: Optional[pd.DataFrame]) -> float:
    """两融余额5日变化率"""
    if margin is None or len(margin) < 6:
        return 5.0
    bal = margin["balance_billion"].values
    change_rate = (bal[-1] - bal[-6]) / abs(bal[-6]) * 100 if bal[-6] != 0 else 0
    if change_rate >= 2:
        return 9.0
    elif change_rate >= 0.5:
        return 7.0
    elif change_rate >= -0.5:
        return 5.5
    elif change_rate >= -2:
        return 3.5
    else:
        return 1.0


def _score_s6_volatility(csi500: Optional[pd.DataFrame]) -> float:
    """波动率压力：中证500日内振幅 vs 30日均值（振幅小=压力小=高分）"""
    if csi500 is None or len(csi500) < 32:
        return 5.0
    if "high" not in csi500.columns or "low" not in csi500.columns:
        return 5.0
    ranges = ((csi500["high"] - csi500["low"]) / csi500["low"] * 100).values
    avg30 = ranges[-32:-2].mean()
    recent = ranges[-1]
    if avg30 == 0:
        return 5.0
    ratio = recent / avg30
    if ratio <= 0.7:
        return 9.0
    elif ratio <= 0.9:
        return 7.5
    elif ratio <= 1.1:
        return 5.5
    elif ratio <= 1.5:
        return 3.0
    else:
        return 1.0


# ─── 机制评分汇总 ─────────────────────────────────────────────────────────────

def calculate_regime_score(
    csi300: Optional[pd.DataFrame],
    csi500: Optional[pd.DataFrame],
    north_flow: Optional[pd.DataFrame],
    margin: Optional[pd.DataFrame],
    etf_prices: dict[str, Optional[pd.DataFrame]],
) -> dict:
    s1 = _score_s1_trend(csi300)
    s2 = _score_s2_volume(csi300)
    s3 = _score_s3_north(north_flow)
    s4 = _score_s4_rotation(etf_prices)
    s5 = _score_s5_margin(margin)
    s6 = _score_s6_volatility(csi500)

    w = REGIME_WEIGHTS
    raw = (s1 * w["s1_trend"] + s2 * w["s2_volume"] + s3 * w["s3_north"] +
           s4 * w["s4_rotation"] + s5 * w["s5_margin"] + s6 * w["s6_vol"])
    score = round(raw * 10, 1)  # 转为 0-100 分

    # ── 极端事件快速降级：单日暴跌直接压入R4区间 ──────────────────────────
    extreme_event = False
    if csi300 is not None and len(csi300) >= 2:
        _c_prev = float(csi300["close"].values[-2])
        _c_now = float(csi300["close"].values[-1])
        _day_chg = (_c_now / _c_prev - 1) * 100 if _c_prev > 0 else 0
        if _day_chg < -3.0:
            score = min(score, 25.0)
            extreme_event = True
            logger.warning(f"极端事件: 沪深300单日跌{_day_chg:.1f}%，强制降级R4")
    if not extreme_event and csi500 is not None and len(csi500) >= 2:
        _c5_prev = float(csi500["close"].values[-2])
        _c5_now = float(csi500["close"].values[-1])
        _day_chg5 = (_c5_now / _c5_prev - 1) * 100 if _c5_prev > 0 else 0
        if _day_chg5 < -4.0:
            score = min(score, 25.0)
            extreme_event = True
            logger.warning(f"极端事件: 中证500单日跌{_day_chg5:.1f}%，强制降级R4")

    breakdown = {
        "s1_trend": round(s1, 1),
        "s2_volume": round(s2, 1),
        "s3_north": round(s3, 1),
        "s4_rotation": round(s4, 1),
        "s5_margin": round(s5, 1),
        "s6_volatility": round(s6, 1),
        "total": score,
    }
    return breakdown


def _score_to_raw_regime(score: float) -> str:
    for regime, (low, high) in REGIME_THRESHOLDS.items():
        if low <= score <= high:
            return regime
    return "R4"


# ─── 状态持久化（切换延迟确认） ───────────────────────────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"current_regime": "R2", "pending_regime": None, "pending_days": 0}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def determine_regime(score: float) -> tuple[str, dict]:
    """
    考虑切换延迟后，返回 (confirmed_regime, state_info)
    R4 立即触发，其他需连续确认
    """
    raw = _score_to_raw_regime(score)
    state = _load_state()
    current = state["current_regime"]

    # R4 立即切换
    if raw == "R4":
        state.update({"current_regime": "R4", "pending_regime": None, "pending_days": 0})
        _save_state(state)
        return "R4", state

    if raw == current:
        # 没有切换需求，重置 pending
        state.update({"pending_regime": None, "pending_days": 0})
        _save_state(state)
        return current, state

    # 判断切换方向
    regimes_order = ["R1", "R2", "R3", "R4"]
    current_idx = regimes_order.index(current)
    raw_idx = regimes_order.index(raw)
    going_down = raw_idx > current_idx
    needed = REGIME_DOWN_CONFIRM if going_down else REGIME_UP_CONFIRM

    if state.get("pending_regime") == raw:
        state["pending_days"] += 1
    else:
        state["pending_regime"] = raw
        state["pending_days"] = 1

    if state["pending_days"] >= needed:
        state.update({"current_regime": raw, "pending_regime": None, "pending_days": 0})
        logger.info(f"机制切换确认：{current} → {raw}（连续{needed}日）")
    else:
        logger.info(f"机制切换待确认：{current} → {raw}（{state['pending_days']}/{needed}日）")

    _save_state(state)
    return state["current_regime"], state


def get_regime_params(regime: str) -> dict:
    return REGIME_PARAMS[regime]
