"""
五因子量化评分引擎
F_Policy(静态) + F_Momentum + F_Flow + F_Liquidity + F_Earnings(静态)
→ 综合评分 → 信念等级（S/A/B/C/D）

F_Momentum: 5日涨幅×0.40 + 20日涨幅×0.25 + 5日超额×0.20 + 均线结构×0.15
F_Flow:     北向代理×0.30 + 主力分位×0.40 + 量价方向信号×0.30
F_Liquidity: 绝对分档×0.50 + 相对分位×0.50（今日量 vs 20日历史）
"""

from __future__ import annotations
import logging
from typing import Optional
import numpy as np
import pandas as pd

from config import (ETF_UNIVERSE, FACTOR_WEIGHTS_BY_REGIME,
                    CONVICTION_TIERS, SIGNAL_AVOID)

logger = logging.getLogger(__name__)


# ─── F_Momentum：价格动量因子（0-100） ────────────────────────────────────────

def _calc_momentum(code: str,
                   etf_prices: dict[str, Optional[pd.DataFrame]]) -> float:
    """5日涨幅×0.40 + 20日涨幅×0.25 + 5日超额×0.20 + 均线结构×0.15"""
    df = etf_prices.get(code)
    csi300 = etf_prices.get("000300")

    if df is None or len(df) < 22:
        return 50.0

    closes = df["close"].values

    # 5日/20日动量
    ret5  = (closes[-1] / closes[-6]  - 1) * 100 if len(closes) >= 6  else 0
    ret20 = (closes[-1] / closes[-21] - 1) * 100 if len(closes) >= 21 else 0

    # 5日超额收益（vs CSI300）
    excess5 = ret5
    if csi300 is not None and len(csi300) >= 6:
        idx5 = (csi300["close"].values[-1] / csi300["close"].values[-6] - 1) * 100
        excess5 = ret5 - idx5

    def _rank_score(v: float, thresholds: list) -> float:
        for score, threshold in thresholds:
            if v >= threshold:
                return score
        return thresholds[-1][0]

    mom5_score  = _rank_score(ret5,    [(95,6),(80,3),(65,1),(50,-1),(35,-3),(20,-5),(5,-99)])
    mom20_score = _rank_score(ret20,   [(95,12),(80,6),(65,2),(50,-2),(35,-6),(20,-12),(5,-99)])
    exc5_score  = _rank_score(excess5, [(95,3),(70,1),(50,-1),(30,-3),(5,-99)])

    # 均线结构：5MA > 20MA > 60MA 趋势一致性
    ma_structure = 50.0
    ma5  = closes[-5:].mean()
    ma20 = closes[-20:].mean()
    if len(closes) >= 61:
        ma60 = closes[-60:].mean()
        if closes[-1] > ma5 > ma20 > ma60:
            ma_structure = 95.0   # 多头完美排列
        elif closes[-1] > ma5 > ma20:
            ma_structure = 80.0   # 短中期多头
        elif closes[-1] > ma20:
            ma_structure = 62.0   # 站上20MA，结构转好
        elif closes[-1] < ma5 < ma20 < ma60:
            ma_structure = 10.0   # 空头完美排列
        elif closes[-1] < ma20:
            ma_structure = 30.0   # 跌破20MA
    else:
        if closes[-1] > ma5 > ma20:
            ma_structure = 80.0
        elif closes[-1] > ma20:
            ma_structure = 62.0
        elif closes[-1] < ma20:
            ma_structure = 30.0

    return round(0.40 * mom5_score + 0.25 * mom20_score + 0.20 * exc5_score + 0.15 * ma_structure, 1)


def _rank_percentile(value: float, all_values: list[float]) -> float:
    """返回 value 在 all_values 中的百分位（0-100）"""
    if not all_values:
        return 50.0
    arr = np.array(all_values)
    return round(float((arr < value).mean() * 100), 1)


def calc_momentum_scores(codes: list[str],
                         etf_prices: dict[str, Optional[pd.DataFrame]]) -> dict[str, float]:
    """计算所有ETF的F_Momentum分位排名得分"""
    raw = {code: _calc_momentum(code, etf_prices) for code in codes}
    all_vals = list(raw.values())
    return {code: _rank_percentile(v, all_vals) for code, v in raw.items()}


# ─── F_Flow：资金流入因子（0-100） ────────────────────────────────────────────

def _calc_vol_inflow_proxy(code: str,
                           etf_prices: dict[str, Optional[pd.DataFrame]]) -> float:
    """
    量价方向信号：今日成交额 vs 20日均值 × 涨跌方向
    放量上涨 = 主动买入；放量下跌 = 主动卖出；缩量上涨 = 可持续性存疑
    """
    df = etf_prices.get(code)
    if df is None or len(df) < 22 or "amount" not in df.columns:
        return 50.0

    amounts = df["amount"].values
    avg20 = float(amounts[-21:-1].mean())
    if avg20 <= 0:
        return 50.0

    today_amt = float(amounts[-1])
    surge = today_amt / avg20
    price_up = float(df["close"].values[-1]) > float(df["close"].values[-2])

    if price_up:
        # 放量上涨：主动买盘确认
        if   surge >= 2.5: return 92.0
        elif surge >= 2.0: return 82.0
        elif surge >= 1.5: return 72.0
        elif surge >= 1.2: return 62.0
        elif surge >= 0.8: return 52.0
        else:              return 38.0   # 缩量上涨，持续性存疑
    else:
        # 放量下跌：主动卖盘出逃
        if   surge >= 2.5: return 8.0
        elif surge >= 2.0: return 18.0
        elif surge >= 1.5: return 28.0
        elif surge >= 1.2: return 38.0
        elif surge >= 0.8: return 48.0
        else:              return 55.0   # 缩量下跌，抛压轻


def calc_flow_scores(codes: list[str],
                     north_flow: Optional[pd.DataFrame],
                     main_force: dict[str, float],
                     etf_prices: Optional[dict] = None) -> dict[str, float]:
    """
    F_Flow = 北向代理×0.30 + 主力分位×0.40 + 量价方向信号×0.30
    北向：宏观方向代理；主力：今日净流向分位；量价：放量方向确认
    """
    # 北向5日合计
    north_5d = 0.0
    if north_flow is not None and "net_buy_billion" in north_flow.columns:
        net_s = north_flow["net_buy_billion"].dropna()
        if len(net_s) >= 1:
            north_5d = float(net_s.tail(5).sum())

    def _north_score(flow: float) -> float:
        if   flow >= 100: return 90
        elif flow >= 50:  return 75
        elif flow >= 10:  return 60
        elif flow >= 0:   return 50
        elif flow >= -30: return 35
        elif flow >= -50: return 20
        return 10

    north_base = _north_score(north_5d)

    mf_vals = list(main_force.values())
    result = {}
    for code in codes:
        mf     = main_force.get(code, 0.0)
        mf_pct = _rank_percentile(mf, mf_vals) if mf_vals else 50.0
        vol_proxy = _calc_vol_inflow_proxy(code, etf_prices or {})

        flow_score = round(0.30 * north_base + 0.40 * mf_pct + 0.30 * vol_proxy, 1)
        result[code] = flow_score
    return result


# ─── F_Liquidity：流动性因子（0-100） ─────────────────────────────────────────

def calc_liquidity_scores(codes: list[str],
                          etf_prices: dict[str, Optional[pd.DataFrame]]) -> dict[str, float]:
    """
    绝对分档×0.50 + 相对分位×0.50
    绝对：5日均成交额分档（保证可交易性下限）
    相对：今日量 vs 20日历史百分位（活跃度是否异常放量）
    """
    result = {}
    for code in codes:
        df = etf_prices.get(code)
        if df is None or len(df) < 5 or "amount" not in df.columns:
            result[code] = 40.0
            continue

        amounts = df["amount"].values
        avg5_100m = float(amounts[-5:].mean()) / 1e8

        # 绝对流动性分档（可交易性）
        if   avg5_100m >= 20: abs_score = 100.0
        elif avg5_100m >= 10: abs_score = 80.0
        elif avg5_100m >= 5:  abs_score = 60.0
        elif avg5_100m >= 1:  abs_score = 40.0
        else:                  abs_score = 0.0   # 流动性陷阱

        # 相对活跃度：今日量 vs 20日历史分位
        if len(amounts) >= 22:
            hist20 = amounts[-21:-1]
            today  = amounts[-1]
            rel_score = _rank_percentile(float(today), [float(v) for v in hist20])
        else:
            rel_score = 50.0

        result[code] = round(0.50 * abs_score + 0.50 * rel_score, 1)
    return result


# ─── 综合评分 ─────────────────────────────────────────────────────────────────

def calc_composite_scores(
    codes: list[str],
    regime: str,
    etf_prices: dict[str, Optional[pd.DataFrame]],
    north_flow: Optional[pd.DataFrame],
    main_force: dict[str, float],
) -> dict[str, dict]:
    """
    返回 {code: {composite, tier, f_policy, f_momentum, f_flow, f_liquidity, f_earnings}}
    """
    weights = FACTOR_WEIGHTS_BY_REGIME[regime]

    momentum_scores   = calc_momentum_scores(codes, etf_prices)
    flow_scores       = calc_flow_scores(codes, north_flow, main_force, etf_prices)
    liquidity_scores  = calc_liquidity_scores(codes, etf_prices)

    results = {}
    for code in codes:
        info = ETF_UNIVERSE.get(code, {})
        f_policy   = float(info.get("f_policy", 50))
        f_earnings = float(info.get("f_earnings", 50))
        f_momentum = momentum_scores.get(code, 50.0)
        f_flow     = flow_scores.get(code, 50.0)
        f_liquidity = liquidity_scores.get(code, 40.0)

        composite = round(
            f_policy   * weights["f_policy"]   +
            f_momentum * weights["f_momentum"] +
            f_flow     * weights["f_flow"]     +
            f_liquidity * weights["f_liquidity"] +
            f_earnings * weights["f_earnings"],
            1,
        )

        tier = _get_tier(composite)
        results[code] = {
            "composite": composite,
            "tier": tier,
            "f_policy": f_policy,
            "f_momentum": round(f_momentum, 1),
            "f_flow": round(f_flow, 1),
            "f_liquidity": round(f_liquidity, 1),
            "f_earnings": f_earnings,
        }
    return results


def _get_tier(score: float) -> str:
    for item in CONVICTION_TIERS:
        if score >= item["min_score"]:
            return item["tier"]
    return "D"


def get_tier_info(tier: str) -> dict:
    for item in CONVICTION_TIERS:
        if item["tier"] == tier:
            return item
    return CONVICTION_TIERS[-1]


# ─── ETF 接盘风险（均线偏离 + 连续上涨） ──────────────────────────────────────

def calc_etf_timing_risks(
    codes: list[str],
    etf_prices: dict[str, Optional[pd.DataFrame]],
) -> dict[str, dict]:
    """
    计算每只ETF的「接盘真空」风险
    今天买入 → 明日谁来接盘？
    连续大涨 + 均线偏移大 → 追高风险极高

    返回 {code: {consec_up, ma5_dev_pct, ma20_dev_pct, risk_level}}
    risk_level: SAFE / CAUTION / DANGER
    """
    result = {}
    for code in codes:
        df = etf_prices.get(code)
        if df is None or len(df) < 22:
            result[code] = {
                "consec_up": 0, "ma5_dev_pct": 0.0,
                "ma20_dev_pct": 0.0, "risk_level": "SAFE",
            }
            continue

        closes = df["close"].values
        current = closes[-1]
        ma5 = closes[-5:].mean()
        ma20 = closes[-20:].mean()

        ma5_dev = (current - ma5) / ma5 * 100
        ma20_dev = (current - ma20) / ma20 * 100

        consec_up = 0
        for i in range(len(closes) - 1, 0, -1):
            if closes[i] > closes[i - 1]:
                consec_up += 1
            else:
                break

        # 风险积分：偏离度和连涨天数各自贡献
        risk_pts = 0
        if ma5_dev >= 5:   risk_pts += 3
        elif ma5_dev >= 3: risk_pts += 2
        elif ma5_dev >= 2: risk_pts += 1

        if ma20_dev >= 10:  risk_pts += 3
        elif ma20_dev >= 6: risk_pts += 2
        elif ma20_dev >= 3: risk_pts += 1

        if consec_up >= 5:   risk_pts += 3
        elif consec_up >= 4: risk_pts += 2
        elif consec_up >= 3: risk_pts += 1

        if risk_pts >= 5:
            risk_level = "DANGER"
        elif risk_pts >= 2:
            risk_level = "CAUTION"
        else:
            risk_level = "SAFE"

        result[code] = {
            "consec_up": consec_up,
            "ma5_dev_pct": round(ma5_dev, 2),
            "ma20_dev_pct": round(ma20_dev, 2),
            "risk_level": risk_level,
        }
    return result
