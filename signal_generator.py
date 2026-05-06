"""
信号生成器 + 风险门控
输入：机制、因子评分、ETF价格
输出：每只ETF的操作信号 + 建议仓位
"""

from __future__ import annotations
import logging
from typing import Optional
import pandas as pd

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from sentiment_gauge import SentimentReading

from config import (
    ETF_UNIVERSE, REGIME_PARAMS, CLUSTER_MAX_WEIGHT,
    CONVICTION_TIERS, ACCOUNT_NET_VALUE,
    SIGNAL_BUY_STRONG, SIGNAL_BUY_WATCH, SIGNAL_HOLD,
    SIGNAL_REDUCE, SIGNAL_SELL_STOP, SIGNAL_AVOID,
)
from factor_scorer import get_tier_info

logger = logging.getLogger(__name__)


# ─── 风险预算仓位计算 ──────────────────────────────────────────────────────────

VOLATILITY_FACTOR = {
    "low":    0.8,   # 电网、红利
    "medium": 1.0,   # 芯片、电力
    "high":   1.3,   # 机器人、卫星
}

HIGH_VOL_ETFS  = {"562500", "159206", "159715"}
LOW_VOL_ETFS   = {"159326", "159611", "562960", "515450", "159667"}


def _vol_factor(code: str) -> float:
    if code in HIGH_VOL_ETFS:
        return VOLATILITY_FACTOR["high"]
    if code in LOW_VOL_ETFS:
        return VOLATILITY_FACTOR["low"]
    return VOLATILITY_FACTOR["medium"]


def calc_risk_budget_weight(code: str, regime: str, tier: str) -> float:
    """
    资金仓位 = 桶内风险预算份额 / (止损幅度 × 波动系数)
    返回建议仓位占总资产比例（0-1）
    """
    params = REGIME_PARAMS[regime]
    info = ETF_UNIVERSE.get(code, {})
    bucket = info.get("bucket", "tactical")
    max_single = info.get("max_weight", 0.10)

    # 确定日风险预算
    daily_loss_budget = ACCOUNT_NET_VALUE * params["daily_loss_rate"]

    # 按桶分配风险预算
    bucket_risk_share = 0.65 if bucket == "core_alpha" else (0.25 if bucket == "tactical" else 0.10)
    bucket_budget = daily_loss_budget * bucket_risk_share

    # 单品风险预算份额（假设该桶平均3个品种）
    single_budget = bucket_budget / 3

    # 止损幅度
    if bucket == "core_alpha":
        stop_pct = params["stop_loss_core"]
    elif bucket == "tactical":
        stop_pct = params["stop_loss_tactical"]
    else:
        stop_pct = 0.04

    if stop_pct == 0:
        return 0.0

    vol = _vol_factor(code)
    raw_weight = single_budget / (ACCOUNT_NET_VALUE * stop_pct * vol)

    # 信念等级系数
    tier_info = get_tier_info(tier)
    tier_factor = tier_info.get("max_weight_factor", 0.5)

    weight = raw_weight * tier_factor
    weight = min(weight, max_single, params["max_single_position"])
    return round(weight, 3)


# ─── 择时门控：情绪过热 + 接盘风险 ───────────────────────────────────────────

def _apply_timing_gate(
    signal: str,
    note: str,
    code: str,
    sentiment: Optional[object],
    timing_risks: Optional[dict],
) -> tuple[str, str]:
    """
    别人贪婪我恐惧——
    市场情绪过热或个股均线偏离过大时，降级/拦截买入信号。
    连续大涨后谁来接盘？不做最后的接盘侠。
    """
    if signal not in (SIGNAL_BUY_STRONG, SIGNAL_BUY_WATCH):
        return signal, note

    market_hot = sentiment is not None and getattr(sentiment, "timing_block", False)
    etf_risk = (timing_risks or {}).get(code, {})
    risk_level = etf_risk.get("risk_level", "SAFE")
    consec_up = etf_risk.get("consec_up", 0)
    ma5_dev = etf_risk.get("ma5_dev_pct", 0.0)

    # 双重过热：市场情绪热 + 个股自身偏离大 → 直接降至观望
    if market_hot and risk_level == "DANGER":
        sentiment_level = getattr(sentiment, "level", "HOT")
        gate_note = (
            f"择时拦截：市场{sentiment_level}+连涨{consec_up}日"
            f"+距MA5偏离{ma5_dev:+.1f}%，等回落再介入"
        )
        return SIGNAL_HOLD, gate_note

    # 市场情绪过热（单独）→ 强买降为观察，观察降为观望
    if market_hot:
        sentiment_level = getattr(sentiment, "level", "HOT")
        gate_note = f"市场情绪{sentiment_level}，信号降级，等待回落"
        if signal == SIGNAL_BUY_STRONG:
            return SIGNAL_BUY_WATCH, gate_note
        return SIGNAL_HOLD, gate_note

    # 个股接盘风险高（市场未整体过热）→ 强买降级，附加警告
    if risk_level == "DANGER":
        gate_note = f"连涨{consec_up}日+距MA5偏{ma5_dev:+.1f}%，追高风险，等待回踩MA"
        if signal == SIGNAL_BUY_STRONG:
            return SIGNAL_BUY_WATCH, gate_note
        return SIGNAL_HOLD, gate_note

    if risk_level == "CAUTION":
        note = f"{note} ⚠️连涨{consec_up}日({ma5_dev:+.1f}%)"

    return signal, note


# ─── 技术面入场确认 ────────────────────────────────────────────────────────────

def _check_technical_entry(code: str,
                            etf_prices: dict[str, Optional[pd.DataFrame]]) -> bool:
    """关三：价格在5日均线上方 + 成交量正常"""
    df = etf_prices.get(code)
    if df is None or len(df) < 6:
        return False
    closes = df["close"].values
    ma5 = closes[-5:].mean()
    current = closes[-1]
    # 未大幅追高（不超过5日均线+2%）
    if current > ma5 * 1.02:
        return False
    if current < ma5 * 0.97:
        return False
    # 成交量不萎缩
    if "volume" in df.columns and len(df) >= 6:
        vol_ratio = df["volume"].values[-1] / (df["volume"].values[-6:-1].mean() + 1)
        if vol_ratio < 0.6:
            return False
    return True


def _check_stop_loss(code: str,
                     etf_prices: dict[str, Optional[pd.DataFrame]],
                     cost_price: Optional[float],
                     regime: str) -> bool:
    """判断当前价格是否触及止损线"""
    if cost_price is None:
        return False
    df = etf_prices.get(code)
    if df is None or len(df) == 0:
        return False
    current = df["close"].values[-1]
    info = ETF_UNIVERSE.get(code, {})
    bucket = info.get("bucket", "tactical")
    params = REGIME_PARAMS[regime]
    stop_pct = params["stop_loss_core"] if bucket == "core_alpha" else params["stop_loss_tactical"]
    return current <= cost_price * (1 - stop_pct)


# ─── 主信号生成 ────────────────────────────────────────────────────────────────

def generate_signals(
    regime: str,
    factor_scores: dict[str, dict],
    etf_prices: dict[str, Optional[pd.DataFrame]],
    current_positions: Optional[dict] = None,
    sentiment: Optional[object] = None,    # SentimentReading
    timing_risks: Optional[dict] = None,   # {code: timing_risk_dict}
) -> list[dict]:
    """
    返回信号列表，每条：
    {code, name, signal, tier, composite, weight, stop_loss_pct, note}
    """
    if current_positions is None:
        current_positions = {}

    params = REGIME_PARAMS[regime]
    allowed_tiers = params["allowed_tiers"]
    signals = []

    # 集群仓位追踪（防止集群超仓）
    cluster_used: dict[str, float] = {}

    for code, scores in factor_scores.items():
        info = ETF_UNIVERSE.get(code, {})
        name = info.get("name", code)
        tier = scores["tier"]
        composite = scores["composite"]
        pool = info.get("pool", "watch")
        bucket = info.get("bucket", "none")
        cluster = info.get("cluster", "")

        # D级/观察池/bucket空 → AVOID；C级 → HOLD观察（不配置但不禁止）
        if pool == "watch" or tier == "D" or bucket == "none":
            signals.append(_make_signal(code, name, SIGNAL_AVOID, tier, composite,
                                        0, 0, "政策/综合评分不足，禁止配置"))
            continue
        if tier == "C":
            signals.append(_make_signal(code, name, SIGNAL_HOLD, tier, composite,
                                        0, 0, f"C级品种，综合分{composite:.0f}，观察但不配置"))
            continue

        # R4 机制下 Tactical 全清
        if regime == "R4" and bucket == "tactical":
            signals.append(_make_signal(code, name, SIGNAL_SELL_STOP, tier, composite,
                                        0, 0, "R4风险机制：Tactical仓强制清仓"))
            continue

        # 信念等级不在机制允许范围
        # 回测显示REDUCE后T+5均涨+3.81%，未持仓时不强制减仓，改为观望
        if tier not in allowed_tiers:
            if code in current_positions:
                signals.append(_make_signal(code, name, SIGNAL_REDUCE, tier, composite,
                                            0, params["stop_loss_core"],
                                            f"已持仓，{regime}不支持{tier}级，逐步减仓"))
            else:
                signals.append(_make_signal(code, name, SIGNAL_HOLD, tier, composite,
                                            0, params["stop_loss_core"],
                                            f"{tier}级，{regime}暂不追加，持续观察"))
            continue

        # 止损检查（已持仓品种）
        cost = current_positions.get(code)
        if cost and _check_stop_loss(code, etf_prices, cost, regime):
            signals.append(_make_signal(code, name, SIGNAL_SELL_STOP, tier, composite,
                                        0, params["stop_loss_core"],
                                        f"触及止损线（成本{cost:.3f}）"))
            continue

        # 计算建议仓位
        weight = calc_risk_budget_weight(code, regime, tier)

        # 集群仓位约束
        cluster_limit = CLUSTER_MAX_WEIGHT.get(cluster, 0.30)
        already = cluster_used.get(cluster, 0)
        if already + weight > cluster_limit:
            weight = max(0, cluster_limit - already)

        # 生成买入/持有信号
        if code in current_positions:
            signal = SIGNAL_HOLD
            note = "维持持仓"
        else:
            tech_ok = _check_technical_entry(code, etf_prices)
            if tier == "S" and tech_ok:
                signal = SIGNAL_BUY_STRONG
                note = "S级强信念，三因子确认"
            elif tier in ("S", "A") and tech_ok:
                signal = SIGNAL_BUY_WATCH
                note = f"{tier}级，可择机建仓"
            elif tier in ("S", "A") and not tech_ok:
                signal = SIGNAL_BUY_WATCH
                note = f"{tier}级，等待技术面确认（均线/量能）"
            else:
                signal = SIGNAL_HOLD
                note = "观察中，暂不建仓"

        # 择时门控：情绪过热 / 接盘风险拦截
        signal, note = _apply_timing_gate(signal, note, code, sentiment, timing_risks)

        cluster_used[cluster] = cluster_used.get(cluster, 0) + weight
        stop_pct = params["stop_loss_core"] if bucket == "core_alpha" else params["stop_loss_tactical"]
        signals.append(_make_signal(code, name, signal, tier, composite, weight, stop_pct, note, scores))

    # 按综合评分降序排列
    signals.sort(key=lambda x: x["composite"], reverse=True)
    return signals


def _make_signal(code, name, signal, tier, composite, weight, stop_pct, note, scores=None) -> dict:
    base = {
        "code": code,
        "name": name,
        "signal": signal,
        "tier": tier,
        "composite": composite,
        "weight_pct": round(weight * 100, 1),
        "stop_loss_pct": round(stop_pct * 100, 1),
        "note": note,
    }
    if scores:
        base["factors"] = {
            "政策": scores.get("f_policy", "-"),
            "动量": scores.get("f_momentum", "-"),
            "资金": scores.get("f_flow", "-"),
            "流动性": scores.get("f_liquidity", "-"),
            "盈利": scores.get("f_earnings", "-"),
        }
    return base


# ─── 每日风险摘要 ─────────────────────────────────────────────────────────────

def calc_daily_risk_summary(regime: str) -> dict:
    params = REGIME_PARAMS[regime]
    net = ACCOUNT_NET_VALUE
    return {
        "max_position_pct": int(params["max_total_position"] * 100),
        "max_leverage_pct": int(params["max_leverage_ratio"] * 100),
        "daily_loss_limit": int(net * params["daily_loss_rate"]),
        "min_defensive_pct": int(params["min_defensive_ratio"] * 100),
        "max_holding_days": params["max_holding_days"],
        "allowed_tiers": params["allowed_tiers"],
    }
