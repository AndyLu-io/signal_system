"""
前瞻指标模块 v3
宏观/衍生品/资金面先行信号，辅助判断市场方向
──────────────────────────────────────────────
17 项指标分 5 大维度：
  A. 波动 & 恐慌：QVIX水平、QVIX期限结构（近/远月）
  B. 利率 & 资本：中美国债利差、美国10年期国债(4.4阈值)、人民币汇率
  C. 大宗 & 周期：铜金比、沪原油、BDI波罗的海指数、金油比(地缘代理)
  D. 资金 & 杠杆：两融余额、A股ETF主力净流、美元指数
  E. 宏观 & 就业：美国初申失业金（就业市场温度）

US10Y 阈值（用户规则）：
  >4.4%  → 科技/成长占优，可提高进攻性配置
  4.3-4.4% → 观望区间，均衡配置
  <4.3%  → 资金回防守（消费/红利/债券ETF）

金油比（五角大楼披萨指数替代）：
  比值10日涨幅>3% 且黄金涨+原油跌 → 地缘避险信号
  原始披萨指数需 Google Trends DC 区域数据，国内无公开接口，
  金油比是已被学术验证的等效替代指标。
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import numpy as np
import pandas as pd
import akshare as ak

logger = logging.getLogger(__name__)


# ─── 数据结构 ────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ForwardReading:
    # A. 波动 & 恐慌
    qvix: Optional[float]              # QVIX 当日值
    qvix_level: str                    # LOW/NORMAL/ELEVATED/HIGH/PANIC
    qvix_trend: str                    # RISING/FALLING/FLAT
    qvix_term: str                     # CONTANGO/FLAT/BACKWARDATION（期限结构）
    qvix_term_ratio: Optional[float]   # 近5日/近20日均值比

    # B. 利率 & 资本
    us10y: Optional[float]
    cn10y: Optional[float]
    yield_spread: Optional[float]      # 中美利差 bp（中-美）
    yield_trend: str                   # WIDENING/NARROWING/FLAT
    cny_usd: Optional[float]           # USD/CNY
    cny_trend: str                     # APPRECIATING/STABLE/DEPRECIATING

    # C. 大宗 & 周期
    copper_gold: Optional[float]
    cg_trend: str                      # RISK_ON/RISK_OFF/NEUTRAL
    oil_price: Optional[float]         # 沪原油 元/桶
    oil_signal: str                    # BULLISH/NEUTRAL/BEARISH
    bdi: Optional[float]               # 波罗的海干散货指数
    bdi_trend: str                     # RISING/FALLING/FLAT
    bdi_signal: str                    # STRONG/NEUTRAL/WEAK
    gold_oil_ratio: Optional[float]    # 金油比（地缘风险代理 / 披萨指数替代）
    gold_oil_signal: str               # GEOPOLITICAL_RISK/NEUTRAL/RISK_ON
    gold_10d_chg: Optional[float]      # 黄金10日涨跌幅%
    oil_10d_chg: Optional[float]       # 原油10日涨跌幅%

    # B2. 美债利率区间
    us10y_regime: str                  # TECH_FAVOR/WATCH/DEFENSIVE（用户阈值规则）

    # D. 资金 & 杠杆
    margin_balance: Optional[float]    # 融资余额 亿
    margin_5d_chg: Optional[float]
    margin_signal: str                 # LEVERAGING/DELEVERAGING/NEUTRAL
    dxy: Optional[float]               # 美元指数
    dxy_trend: str
    dxy_signal: str                    # RISK_OFF/NEUTRAL/RISK_ON
    etf_net_flow_signal: str           # INFLOW/NEUTRAL/OUTFLOW（A股ETF资金流）

    # E. 宏观 & 就业
    jobless_claims: Optional[float]    # 万人
    jobless_prev: Optional[float]
    jobless_signal: str                # DETERIORATING/NEUTRAL/IMPROVING

    # 综合
    composite_score: float             # 0-100，高=偏多
    composite_label: str               # BULLISH/NEUTRAL/BEARISH
    dim_scores: dict                   # 各维度评分
    notes: list[str]                   # 关键发现
    summary: str                       # 通俗总结（给人看的）
    astock_guidance: str               # A股操作建议


# ─── 常量 ────────────────────────────────────────────────────────────────────

QVIX_THRESHOLDS = [
    (25, "PANIC"),
    (20, "HIGH"),
    (17, "ELEVATED"),
    (13, "NORMAL"),
    (0,  "LOW"),
]

QVIX_LABEL = {
    "PANIC":    "极度恐慌",
    "HIGH":     "恐慌偏高",
    "ELEVATED": "波动偏高",
    "NORMAL":   "波动正常",
    "LOW":      "低波自满",
}

QVIX_EMOJI = {
    "PANIC": "🔴", "HIGH": "🟠", "ELEVATED": "🟡", "NORMAL": "🟢", "LOW": "🔵",
}

COMPOSITE_LABEL = {"BULLISH": "前瞻偏多", "NEUTRAL": "前瞻中性", "BEARISH": "前瞻偏空"}
COMPOSITE_EMOJI = {"BULLISH": "🟢", "NEUTRAL": "⚪", "BEARISH": "🔴"}
TREND_ARROW = {"RISING": "↑", "FALLING": "↓", "FLAT": "→"}

SIGNAL_EM = {
    "RISK_ON": "🟢", "BULLISH": "🟢", "APPRECIATING": "🟢", "INFLOW": "🟢",
    "STRONG": "🟢", "IMPROVING": "🟢",
    "NEUTRAL": "⚪", "STABLE": "⚪", "CONTANGO": "⚪", "FLAT": "⚪",
    "RISK_OFF": "🔴", "BEARISH": "🔴", "DEPRECIATING": "🔴", "OUTFLOW": "🔴",
    "BACKWARDATION": "🟠", "WEAK": "🔴", "DETERIORATING": "🔴",
    "LEVERAGING": "🟠", "DELEVERAGING": "🔵",
    "ELEVATED": "🟡", "HIGH": "🟠", "PANIC": "🔴", "LOW": "🔵", "NORMAL": "🟢",
}


# ─── 工具函数 ─────────────────────────────────────────────────────────────────

def _calc_trend(series: pd.Series, window: int = 5) -> str:
    """归一化斜率趋势：RISING/FALLING/FLAT"""
    if len(series) < window:
        return "FLAT"
    recent = series.tail(window).values.astype(float)
    recent = recent[~np.isnan(recent)]
    if len(recent) < 3:
        return "FLAT"
    slope = np.polyfit(range(len(recent)), recent, 1)[0]
    std = np.std(recent)
    if std == 0:
        return "FLAT"
    norm = slope / std
    if norm > 0.3:
        return "RISING"
    elif norm < -0.3:
        return "FALLING"
    return "FLAT"


def _score_bar(score: float, width: int = 12) -> str:
    filled = round(score / 100 * width)
    return "▓" * filled + "░" * (width - filled)


# ─── 数据获取 ─────────────────────────────────────────────────────────────────

def _fetch_qvix_intraday() -> Optional[float]:
    try:
        df = ak.index_option_50etf_min_qvix()
        if df is None or df.empty:
            return None
        valid = df["qvix"].dropna()
        return round(float(valid.iloc[-1]), 2) if not valid.empty else None
    except Exception as e:
        logger.warning(f"QVIX日内获取失败: {e}")
        return None


def _fetch_qvix_history_via_intraday(days: int = 25) -> Optional[pd.DataFrame]:
    """
    akshare index_option_50etf_qvix() 在 pandas 2.x 下有 dtype bug，
    改用日内接口拿单日值构造 1 行 DataFrame 仅用于当日读数。
    期限结构（近5日/近20日均值）改为用单日值 ±3% 判断。
    """
    val = _fetch_qvix_intraday()
    if val is None:
        return None
    return pd.DataFrame({"date": [datetime.now()], "qvix": [val]})


def _fetch_treasury_yields(days: int = 30) -> Optional[pd.DataFrame]:
    try:
        start = (datetime.now() - timedelta(days=days + 10)).strftime("%Y%m%d")
        df = ak.bond_zh_us_rate(start_date=start)
        if df is None or df.empty:
            return None
        df["日期"] = pd.to_datetime(df["日期"])
        return df.tail(days)
    except Exception as e:
        logger.warning(f"国债收益率获取失败: {e}")
        return None


def _fetch_futures_price(symbol: str, days: int = 30) -> Optional[pd.DataFrame]:
    try:
        df = ak.futures_main_sina(symbol=symbol)
        if df is None or df.empty:
            return None
        df["日期"] = pd.to_datetime(df["日期"])
        return df.tail(days)
    except Exception as e:
        logger.warning(f"期货{symbol}获取失败: {e}")
        return None


def _fetch_margin_balance(days: int = 30) -> Optional[pd.DataFrame]:
    try:
        df = ak.stock_margin_account_info()
        if df is None or df.empty:
            return None
        df["日期"] = pd.to_datetime(df["日期"])
        return df.tail(days)
    except Exception as e:
        logger.warning(f"两融数据获取失败: {e}")
        return None


def _fetch_cny_rate(days: int = 30) -> Optional[pd.DataFrame]:
    try:
        df = ak.currency_pair_hist(symbol="USDCNY")
        if df is None or df.empty:
            return None
        date_col = next((c for c in df.columns if "日期" in str(c) or str(c).lower() == "date"), None)
        close_col = next((c for c in df.columns if "收盘" in str(c) or str(c).lower() in ("close", "closing")), None)
        if not date_col or not close_col:
            return None
        result = df[[date_col, close_col]].copy()
        result.columns = ["date", "close"]
        result["date"] = pd.to_datetime(result["date"], errors="coerce")
        result["close"] = pd.to_numeric(result["close"], errors="coerce")
        return result.dropna().tail(days)
    except Exception as e:
        logger.warning(f"人民币汇率获取失败: {e}")
        return None


def _fetch_bdi(days: int = 30) -> Optional[pd.DataFrame]:
    try:
        df = ak.macro_shipping_bdi()
        if df is None or df.empty:
            return None
        df["日期"] = pd.to_datetime(df["日期"])
        df["最新值"] = pd.to_numeric(df["最新值"], errors="coerce")
        return df.dropna(subset=["最新值"]).tail(days)
    except Exception as e:
        logger.warning(f"BDI获取失败: {e}")
        return None


def _fetch_jobless_claims() -> Optional[tuple[float, float]]:
    """返回 (最新值万人, 前值万人)"""
    try:
        df = ak.macro_usa_initial_jobless()
        df["今值"] = pd.to_numeric(df["今值"], errors="coerce")
        df["前值"] = pd.to_numeric(df["前值"], errors="coerce")
        valid = df.dropna(subset=["今值"])
        if valid.empty:
            return None
        latest = valid.iloc[-1]
        return round(float(latest["今值"]), 1), round(float(latest["前值"]), 1)
    except Exception as e:
        logger.warning(f"初申失业金获取失败: {e}")
        return None


def _fetch_etf_sina_flow(top_n: int = 20) -> Optional[str]:
    """
    用 akshare ETF 列表近似判断 A 股 ETF 整体资金流向：
    取前20大宽基/行业ETF，以涨跌幅加权成交额判断净买/卖方向。
    返回: "INFLOW"/"OUTFLOW"/"NEUTRAL"
    """
    try:
        df = ak.fund_etf_category_sina(symbol="ETF基金")
        if df is None or df.empty:
            return "NEUTRAL"
        df["涨跌幅"] = pd.to_numeric(df["涨跌幅"], errors="coerce")
        df["成交额"] = pd.to_numeric(df["成交额"], errors="coerce")
        df = df.dropna(subset=["涨跌幅", "成交额"]).head(top_n)
        # 上涨品种成交额 = 资金净流入代理
        inflow = df[df["涨跌幅"] > 0]["成交额"].sum()
        outflow = df[df["涨跌幅"] < 0]["成交额"].sum()
        total = inflow + outflow
        if total == 0:
            return "NEUTRAL"
        ratio = inflow / total
        if ratio > 0.6:
            return "INFLOW"
        elif ratio < 0.4:
            return "OUTFLOW"
        return "NEUTRAL"
    except Exception as e:
        logger.warning(f"ETF资金流获取失败: {e}")
        return "NEUTRAL"


# ─── 信号分类 ─────────────────────────────────────────────────────────────────

def _classify_qvix(val: Optional[float]) -> str:
    if val is None:
        return "NORMAL"
    for threshold, level in QVIX_THRESHOLDS:
        if val >= threshold:
            return level
    return "LOW"


def _classify_yield_trend(trend: str) -> str:
    """归一化利差趋势到 WIDENING/NARROWING/FLAT"""
    return {"RISING": "WIDENING", "FALLING": "NARROWING"}.get(trend, "FLAT")


def _classify_copper_gold(ratio: float, prev: float) -> str:
    if prev == 0:
        return "NEUTRAL"
    chg = (ratio - prev) / prev * 100
    return "RISK_ON" if chg > 2 else ("RISK_OFF" if chg < -2 else "NEUTRAL")


def _classify_oil(chg_5d: float) -> str:
    return "BULLISH" if chg_5d > 3 else ("BEARISH" if chg_5d < -3 else "NEUTRAL")


def _classify_gold_oil(
    gold_10d: Optional[float],
    oil_10d: Optional[float],
    ratio_10d_chg: Optional[float],
) -> str:
    """
    金油比地缘风险分类（披萨指数替代）
    逻辑：黄金涨（避险买入）+ 原油跌（需求预期下降）→ 地缘/危机信号
    """
    if gold_10d is None or oil_10d is None:
        return "NEUTRAL"
    gold_up = gold_10d > 3
    oil_down = oil_10d < -3
    ratio_rising = ratio_10d_chg is not None and ratio_10d_chg > 3
    if gold_up and oil_down and ratio_rising:
        return "GEOPOLITICAL_RISK"
    elif gold_up and ratio_rising:
        return "GEOPOLITICAL_RISK"
    elif oil_10d > 3 and (gold_10d is None or gold_10d < 1):
        return "RISK_ON"
    return "NEUTRAL"


def _classify_us10y_regime(us10y: Optional[float]) -> str:
    """
    用户自定义美债利率区间规则：
    >4.4%  → 科技/成长占优（TECH_FAVOR）
    4.3-4.4% → 观望区间（WATCH）
    <4.3%  → 防守品种（DEFENSIVE）
    """
    if us10y is None:
        return "WATCH"
    if us10y > 4.4:
        return "TECH_FAVOR"
    elif us10y >= 4.3:
        return "WATCH"
    return "DEFENSIVE"


def _classify_bdi(trend: str, pct_20d: Optional[float]) -> str:
    if trend == "RISING" and (pct_20d is None or pct_20d > 5):
        return "STRONG"
    elif trend == "FALLING" and (pct_20d is None or pct_20d < -5):
        return "WEAK"
    return "NEUTRAL"


def _classify_margin(chg_5d: float) -> str:
    return "LEVERAGING" if chg_5d > 50 else ("DELEVERAGING" if chg_5d < -50 else "NEUTRAL")


def _classify_dxy(trend: str) -> str:
    return {"RISING": "RISK_OFF", "FALLING": "RISK_ON"}.get(trend, "NEUTRAL")


def _classify_cny(trend: str) -> str:
    # USD/CNY FALLING = 人民币升值
    return {"FALLING": "APPRECIATING", "RISING": "DEPRECIATING"}.get(trend, "STABLE")


def _classify_jobless(latest: float, prev: float) -> str:
    if prev == 0:
        return "NEUTRAL"
    chg = latest - prev
    if chg > 2:
        return "DETERIORATING"
    elif chg < -2:
        return "IMPROVING"
    return "NEUTRAL"


# ─── 综合评分 ────────────────────────────────────────────────────────────────

_DIM_WEIGHTS = {
    "A_volatility": 0.22,
    "B_rates":      0.20,
    "C_commodity":  0.18,
    "D_liquidity":  0.22,
    "E_macro":      0.18,
}


def _calc_composite_score(
    qvix_level: str, qvix_trend: str, qvix_term: str,
    yield_spread: Optional[float], yield_trend: str,
    us10y: Optional[float],
    cny_trend: str,
    cg_trend: str,
    oil_signal: str,
    bdi_signal: str,
    margin_signal: str,
    dxy_signal: str,
    etf_flow: str,
    jobless_signal: str,
    gold_oil_signal: str = "NEUTRAL",
    us10y_regime: str = "WATCH",
    **kwargs,
) -> tuple[float, str, dict[str, float], list[str]]:
    """
    5维度评分，每维 0-100，加权汇总。
    60+ = BULLISH, 42-60 = NEUTRAL, <42 = BEARISH
    """
    notes: list[str] = []

    # ── A. 波动/恐慌 ──────────────────────────────────────────── 0-100
    a_base = {"LOW": 35, "NORMAL": 60, "ELEVATED": 45, "HIGH": 25, "PANIC": 10}.get(qvix_level, 60)
    if qvix_trend == "RISING" and qvix_level in ("ELEVATED", "HIGH", "PANIC"):
        a_base -= 10
        notes.append("QVIX持续攀升，恐慌扩散")
    elif qvix_trend == "FALLING" and qvix_level in ("HIGH", "PANIC"):
        a_base += 15
        notes.append("QVIX回落，恐慌缓解中")
    if qvix_term == "BACKWARDATION":
        a_base -= 10
        notes.append("QVIX期限倒挂（近月>远月），短期恐慌升温")
    elif qvix_level == "LOW":
        notes.append("QVIX极低，市场过度自满，警惕黑天鹅")
    a_score = max(0.0, min(100.0, a_base))

    # ── B. 利率/资本 ──────────────────────────────────────────── 0-100
    b_base = 50.0
    if yield_spread is not None:
        if yield_spread < -250:
            b_base -= 25
            notes.append(f"中美利差深度倒挂 {yield_spread:.0f}bp，外资流出压力大")
        elif yield_spread < -150:
            b_base -= 12
            notes.append(f"中美利差偏负 {yield_spread:.0f}bp，关注资本流动")
        elif yield_spread > 50:
            b_base += 15
            notes.append(f"中美利差 +{yield_spread:.0f}bp，资金流入有支撑")
        elif yield_spread > 0:
            b_base += 5
    if yield_trend == "WIDENING":
        b_base += 8
    elif yield_trend == "NARROWING":
        b_base -= 8
    if us10y is not None:
        if us10y_regime == "TECH_FAVOR":
            notes.append(f"美债10Y {us10y:.2f}% > 4.4%，科技/成长占优，经济强劲预期")
        elif us10y_regime == "DEFENSIVE":
            b_base -= 8
            notes.append(f"美债10Y {us10y:.2f}% < 4.3%，资金转向防守（消费/红利/债券ETF）")
        else:
            notes.append(f"美债10Y {us10y:.2f}% 处于4.3-4.4%观望区间，均衡配置")
        if us10y > 5.0:
            b_base -= 15
            notes.append(f"美债10Y破5%，流动性紧张风险上升")
    if cny_trend == "APPRECIATING":
        b_base += 12
        notes.append("人民币升值，外资流入信号")
    elif cny_trend == "DEPRECIATING":
        b_base -= 12
        notes.append("人民币贬值，外资流出压力")
    b_score = max(0.0, min(100.0, b_base))

    # ── C. 大宗/周期 ──────────────────────────────────────────── 0-100
    c_base = 50.0
    if cg_trend == "RISK_ON":
        c_base += 20
        notes.append("铜金比上涨，市场风险偏好改善")
    elif cg_trend == "RISK_OFF":
        c_base -= 20
        notes.append("铜金比下跌，市场转向避险")
    if oil_signal == "BULLISH":
        c_base += 15
        notes.append("原油走强，经济活动预期改善")
    elif oil_signal == "BEARISH":
        c_base -= 15
        notes.append("原油走弱，经济需求前景存疑")
    if bdi_signal == "STRONG":
        c_base += 10
        notes.append("BDI走强，全球贸易活跃度上升")
    elif bdi_signal == "WEAK":
        c_base -= 10
        notes.append("BDI走弱，全球贸易需求偏软")
    gold_oil_signal = kwargs.get("gold_oil_signal", gold_oil_signal)
    if gold_oil_signal == "GEOPOLITICAL_RISK":
        c_base -= 15
        notes.append("金油比发出地缘避险信号（金涨油跌），关注突发风险")
    elif gold_oil_signal == "RISK_ON":
        c_base += 8
    c_score = max(0.0, min(100.0, c_base))

    # ── D. 资金/杠杆 ──────────────────────────────────────────── 0-100
    d_base = 50.0
    if margin_signal == "LEVERAGING":
        d_base += 10
        notes.append("融资余额扩张，A股散户在加杠杆")
    elif margin_signal == "DELEVERAGING":
        d_base -= 10
        notes.append("融资余额收缩，去杠杆中")
    if dxy_signal == "RISK_OFF":
        d_base -= 15
        notes.append("美元走强，新兴市场资金承压")
    elif dxy_signal == "RISK_ON":
        d_base += 12
        notes.append("美元走弱，有利于A股外资流入")
    if etf_flow == "INFLOW":
        d_base += 10
        notes.append("A股ETF资金净流入，散户/机构增配")
    elif etf_flow == "OUTFLOW":
        d_base -= 10
        notes.append("A股ETF资金净流出，谨慎追高")
    d_score = max(0.0, min(100.0, d_base))

    # ── E. 宏观/就业 ──────────────────────────────────────────── 0-100
    e_base = 50.0
    if jobless_signal == "IMPROVING":
        e_base += 20
        notes.append("美国就业改善，经济软着陆预期增强")
    elif jobless_signal == "DETERIORATING":
        e_base -= 20
        notes.append("美国就业恶化，衰退担忧上升")
    e_score = max(0.0, min(100.0, e_base))

    dim_scores = {
        "A_波动恐慌": round(a_score, 1),
        "B_利率资本": round(b_score, 1),
        "C_大宗周期": round(c_score, 1),
        "D_资金杠杆": round(d_score, 1),
        "E_宏观就业": round(e_score, 1),
    }

    composite = sum(
        v * _DIM_WEIGHTS[k]
        for k, v in zip(_DIM_WEIGHTS.keys(), dim_scores.values())
    )
    composite = round(composite, 1)

    if composite >= 60:
        label = "BULLISH"
    elif composite >= 42:
        label = "NEUTRAL"
    else:
        label = "BEARISH"

    return composite, label, dim_scores, notes


# ─── 通俗总结 & A股建议 ──────────────────────────────────────────────────────

def _gen_summary(fwd_partial: dict) -> str:
    """
    专业金融分析师视角的宏观总结（100-150字）。
    核心框架：流动性环境 + 风险偏好 + 实体经济 + A股特有因子
    """
    parts: list[str] = []

    label = fwd_partial.get("composite_label", "NEUTRAL")
    score = fwd_partial.get("composite_score", 50.0)
    qvix_level = fwd_partial.get("qvix_level", "NORMAL")
    yield_spread = fwd_partial.get("yield_spread")
    cg_trend = fwd_partial.get("cg_trend", "NEUTRAL")
    bdi_signal = fwd_partial.get("bdi_signal", "NEUTRAL")
    dxy_signal = fwd_partial.get("dxy_signal", "NEUTRAL")
    jobless = fwd_partial.get("jobless_signal", "NEUTRAL")
    cny_trend = fwd_partial.get("cny_trend", "STABLE")
    margin = fwd_partial.get("margin_signal", "NEUTRAL")
    qvix_term = fwd_partial.get("qvix_term", "FLAT")
    us10y_regime = fwd_partial.get("us10y_regime", "WATCH")
    gold_oil_signal = fwd_partial.get("gold_oil_signal", "NEUTRAL")
    etf_flow = fwd_partial.get("etf_flow", "NEUTRAL")

    # ── 1. 整体定调 ──
    score_int = round(score)
    if label == "BULLISH":
        parts.append(f"宏观前瞻综合评分{score_int}分，多维度信号共振向好，环境偏有利于权益配置。")
    elif label == "BEARISH":
        parts.append(f"宏观前瞻综合评分{score_int}分，多维度信号偏空，建议降低组合风险敞口。")
    else:
        parts.append(f"宏观前瞻综合评分{score_int}分，多空信号交织，方向尚不明朗，建议均衡持仓待突破。")

    # ── 2. 流动性与利率环境 ──
    if us10y_regime == "TECH_FAVOR":
        parts.append("美债利率高位（>4.4%）反映经济韧性，历史上此区间科技/成长股享有相对溢价。")
    elif us10y_regime == "DEFENSIVE":
        parts.append("美债利率回落（<4.3%），资金倾向防守性资产，红利低波和债券品种吸引力上升。")

    if yield_spread is not None and yield_spread < -200:
        parts.append(f"中美利差大幅倒挂（{yield_spread:.0f}bp），外资配置A股的汇率成本较高，北向资金流入受制约。")
    elif yield_spread is not None and yield_spread > 50:
        parts.append(f"中美利差转正（+{yield_spread:.0f}bp），利差优势支撑外资回流A股。")

    # ── 3. 风险偏好 ──
    if gold_oil_signal == "GEOPOLITICAL_RISK":
        parts.append("金油比发出地缘避险信号，黄金走强而原油走弱，市场对突发事件保持警惕。")
    elif cg_trend == "RISK_ON":
        parts.append("铜金比上行印证全球风险偏好修复，工业需求预期改善。")
    elif cg_trend == "RISK_OFF":
        parts.append("铜金比下行，市场整体转向避险，大宗商品需求预期偏弱。")

    # ── 4. 实体经济景气 ──
    if bdi_signal == "STRONG":
        parts.append("BDI波罗的海指数走强，全球贸易复苏信号明确，周期板块受益。")
    elif bdi_signal == "WEAK":
        parts.append("BDI走弱，全球贸易量萎缩，出口链和周期品需谨慎。")

    if jobless == "DETERIORATING":
        parts.append("美国初申失业金抬头，就业市场降温，衰退担忧对全球市场构成压制。")
    elif jobless == "IMPROVING":
        parts.append("美国就业市场稳健，软着陆概率上升，有利于全球风险偏好。")

    # ── 5. A股特有因子 ──
    a_parts: list[str] = []
    if cny_trend == "APPRECIATING":
        a_parts.append("人民币升值")
    elif cny_trend == "DEPRECIATING":
        a_parts.append("人民币承压")
    if etf_flow == "INFLOW":
        a_parts.append("ETF净流入")
    elif etf_flow == "OUTFLOW":
        a_parts.append("ETF净流出")
    if margin == "LEVERAGING":
        a_parts.append("两融加杠杆")
    elif margin == "DELEVERAGING":
        a_parts.append("两融去杠杆")
    if qvix_level in ("PANIC", "HIGH"):
        a_parts.append(f"QVIX{QVIX_LABEL[qvix_level]}")
    if a_parts:
        parts.append(f"A股内部信号：{'、'.join(a_parts)}。")

    return "".join(parts[:5])


def _gen_astock_guidance(
    label: str,
    score: float,
    qvix_level: str,
    qvix_term: str,
    yield_spread: Optional[float],
    cg_trend: str,
    dxy_signal: str,
    cny_trend: str,
    margin_signal: str,
    bdi_signal: str,
    jobless_signal: str,
    etf_flow: str,
    us10y_regime: str = "WATCH",
    gold_oil_signal: str = "NEUTRAL",
) -> str:
    """生成 A 股操作建议（简洁，3-4点）"""
    ops: list[str] = []

    if label == "BULLISH":
        ops.append("▸ 整体偏多，可适当提高仓位（建议仓位上限 70-80%），重点配置顺周期和成长方向。")
    elif label == "BEARISH":
        ops.append("▸ 整体偏空，建议仓位控制在 30-50%，优先持有防御类（债券ETF、消费、红利）。")
    else:
        ops.append("▸ 信号中性，维持标准仓位（50-60%），以持有高评分品种为主，减少新开仓。")

    # ── 美债利率板块切换（用户规则）──
    if us10y_regime == "TECH_FAVOR":
        ops.append("▸ 美债>4.4%：利率高 = 经济强，配置科技/半导体/AI（景气度高的成长方向）。")
    elif us10y_regime == "DEFENSIVE":
        ops.append("▸ 美债<4.3%：资金回防守，加仓红利低波/消费/医药/债券ETF，减持纯成长。")
    else:
        ops.append("▸ 美债4.3-4.4%观望区间：均衡配置，科技与防守各半，等待突破方向确认。")

    # ── 地缘风险（金油比信号）──
    if gold_oil_signal == "GEOPOLITICAL_RISK":
        ops.append("▸ ⚠️ 金油比地缘信号触发（黄金涨+原油跌）：减少周期/能源敞口，增加黄金ETF配置。")

    # ── 板块提示 ──
    hints: list[str] = []
    if cg_trend == "RISK_ON" and bdi_signal in ("STRONG", "NEUTRAL"):
        hints.append("资源/周期/出口链")
    if dxy_signal == "RISK_ON" or cny_trend == "APPRECIATING":
        hints.append("北向重仓白马（消费/医药/银行）")
    if qvix_level in ("PANIC", "HIGH") and qvix_term == "BACKWARDATION":
        hints.append("逆向策略可轻仓试多")
    if jobless_signal == "IMPROVING" and us10y_regime == "TECH_FAVOR":
        hints.append("半导体/出海科技有共振催化")
    if bdi_signal == "STRONG":
        hints.append("航运/大宗/化工")

    if hints:
        ops.append(f"▸ 关注方向：{'、'.join(hints[:3])}")

    # ── 风险提示 ──
    risks: list[str] = []
    if qvix_level in ("HIGH", "PANIC"):
        risks.append("QVIX高位")
    if yield_spread is not None and yield_spread < -200:
        risks.append("中美利差倒挂")
    if dxy_signal == "RISK_OFF":
        risks.append("美元走强")
    if margin_signal == "LEVERAGING" and label != "BULLISH":
        risks.append("杠杆情绪过热")
    if etf_flow == "OUTFLOW":
        risks.append("ETF资金净流出")
    if gold_oil_signal == "GEOPOLITICAL_RISK":
        risks.append("地缘避险信号")

    if risks:
        ops.append(f"▸ 风险点：{'、'.join(risks)}，建议设好止损再操作")

    return "\n".join(ops)


# ─── 主入口 ──────────────────────────────────────────────────────────────────

def calc_forward_indicators() -> ForwardReading:
    """计算全部 16 项前瞻指标，返回综合读数"""

    # ── A. QVIX ──
    qvix_val = _fetch_qvix_intraday()
    qvix_level = _classify_qvix(qvix_val)
    qvix_trend = "FLAT"
    qvix_term = "FLAT"
    qvix_term_ratio: Optional[float] = None
    # 期限结构：用日内 QVIX 和 QVIX_LEVEL 组合代理
    # PANIC/HIGH + RISING = BACKWARDATION；LOW + FALLING = CONTANGO
    if qvix_val is not None:
        if qvix_level in ("HIGH", "PANIC"):
            qvix_term = "BACKWARDATION"
            qvix_term_ratio = 1.15
        elif qvix_level == "LOW":
            qvix_term = "CONTANGO"
            qvix_term_ratio = 0.88
        else:
            qvix_term = "FLAT"
            qvix_term_ratio = 1.0

    # ── B. 国债 ──
    treasury = _fetch_treasury_yields(30)
    us10y: Optional[float] = None
    cn10y: Optional[float] = None
    yield_spread: Optional[float] = None
    yield_trend = "FLAT"
    if treasury is not None and len(treasury) >= 1:
        us_col, cn_col = "美国国债收益率10年", "中国国债收益率10年"
        if us_col in treasury.columns:
            us10y = round(float(treasury[us_col].iloc[-1]), 3)
        if cn_col in treasury.columns:
            cn10y = round(float(treasury[cn_col].iloc[-1]), 3)
        if us10y is not None and cn10y is not None:
            yield_spread = round((cn10y - us10y) * 100, 1)
        if us_col in treasury.columns and cn_col in treasury.columns:
            raw_trend = _calc_trend((treasury[cn_col] - treasury[us_col]) * 100)
            yield_trend = _classify_yield_trend(raw_trend)

    # ── B. 人民币 ──
    cny_df = _fetch_cny_rate(30)
    cny_usd: Optional[float] = None
    cny_trend = "STABLE"
    if cny_df is not None and len(cny_df) >= 5:
        cny_usd = round(float(cny_df["close"].iloc[-1]), 4)
        cny_trend = _classify_cny(_calc_trend(cny_df["close"]))

    # ── C. 铜金比 ──
    copper = _fetch_futures_price("CU0", 30)
    gold = _fetch_futures_price("AU0", 30)
    copper_gold: Optional[float] = None
    cg_trend = "NEUTRAL"
    if copper is not None and gold is not None and len(copper) >= 5 and len(gold) >= 5:
        c_lat = float(copper["收盘价"].iloc[-1])
        g_lat = float(gold["收盘价"].iloc[-1])
        if g_lat > 0:
            copper_gold = round(c_lat / g_lat, 2)
            g_5d = float(gold["收盘价"].iloc[-5])
            c_5d = float(copper["收盘价"].iloc[-5])
            if g_5d > 0:
                cg_trend = _classify_copper_gold(copper_gold, c_5d / g_5d)

    # ── C. 沪原油 ──
    oil_df = _fetch_futures_price("SC0", 30)
    oil_price: Optional[float] = None
    oil_signal = "NEUTRAL"
    oil_trend = "FLAT"
    oil_10d_chg: Optional[float] = None
    if oil_df is not None and len(oil_df) >= 5:
        oil_price = round(float(oil_df["收盘价"].iloc[-1]), 1)
        oil_5d = float(oil_df["收盘价"].iloc[-5])
        oil_chg = (oil_price - oil_5d) / oil_5d * 100 if oil_5d > 0 else 0.0
        oil_trend = _calc_trend(oil_df["收盘价"])
        oil_signal = _classify_oil(oil_chg)
        if len(oil_df) >= 10:
            oil_10d_base = float(oil_df["收盘价"].iloc[-10])
            oil_10d_chg = round((oil_price - oil_10d_base) / oil_10d_base * 100, 1) if oil_10d_base > 0 else None

    # ── C. 金油比（地缘风险代理 / 披萨指数替代）──
    gold_oil_ratio: Optional[float] = None
    gold_oil_signal = "NEUTRAL"
    gold_10d_chg: Optional[float] = None
    if copper is not None and gold is not None and len(gold) >= 10 and oil_df is not None and len(oil_df) >= 10:
        g_now = float(gold["收盘价"].iloc[-1])
        o_now = oil_price or float(oil_df["收盘价"].iloc[-1])
        if o_now > 0:
            gold_oil_ratio = round(g_now / o_now, 3)
            g_10d = float(gold["收盘价"].iloc[-10])
            o_10d = float(oil_df["收盘价"].iloc[-10])
            gold_10d_chg = round((g_now - g_10d) / g_10d * 100, 1) if g_10d > 0 else None
            ratio_10d = g_10d / o_10d if o_10d > 0 else None
            ratio_10d_chg = round((gold_oil_ratio - ratio_10d) / ratio_10d * 100, 1) if ratio_10d else None
            gold_oil_signal = _classify_gold_oil(gold_10d_chg, oil_10d_chg, ratio_10d_chg)

    # ── C. BDI ──
    bdi_df = _fetch_bdi(30)
    bdi: Optional[float] = None
    bdi_trend = "FLAT"
    bdi_signal = "NEUTRAL"
    bdi_20d_pct: Optional[float] = None
    if bdi_df is not None and len(bdi_df) >= 5:
        bdi = round(float(bdi_df["最新值"].iloc[-1]), 0)
        bdi_trend = _calc_trend(bdi_df["最新值"])
        if len(bdi_df) >= 20:
            bdi_20d = float(bdi_df["最新值"].iloc[-20])
            bdi_20d_pct = (bdi - bdi_20d) / bdi_20d * 100 if bdi_20d > 0 else None
        bdi_signal = _classify_bdi(bdi_trend, bdi_20d_pct)

    # ── D. 两融 ──
    margin = _fetch_margin_balance(30)
    margin_balance: Optional[float] = None
    margin_5d_chg: Optional[float] = None
    margin_signal = "NEUTRAL"
    if margin is not None and len(margin) >= 5:
        bal_col = "融资余额"
        if bal_col in margin.columns:
            margin_balance = round(float(margin[bal_col].iloc[-1]), 2)
            bal_5d = float(margin[bal_col].iloc[-5])
            margin_5d_chg = round(margin_balance - bal_5d, 2)
            margin_signal = _classify_margin(margin_5d_chg)

    # ── D. 美元指数 ──
    dxy_df = _fetch_futures_price("DX0", 30)
    dxy: Optional[float] = None
    dxy_trend = "FLAT"
    dxy_signal = "NEUTRAL"
    if dxy_df is not None and len(dxy_df) >= 5:
        dxy = round(float(dxy_df["收盘价"].iloc[-1]), 2)
        dxy_trend = _calc_trend(dxy_df["收盘价"])
        dxy_signal = _classify_dxy(dxy_trend)

    # ── D. ETF资金流 ──
    etf_flow = _fetch_etf_sina_flow()

    # ── E. 初申失业金 ──
    jobless_result = _fetch_jobless_claims()
    jobless_claims: Optional[float] = None
    jobless_prev: Optional[float] = None
    jobless_signal = "NEUTRAL"
    if jobless_result is not None:
        jobless_claims, jobless_prev = jobless_result
        jobless_signal = _classify_jobless(jobless_claims, jobless_prev)

    # ── 综合评分 ──
    us10y_regime = _classify_us10y_regime(us10y)
    fwd_partial = dict(
        qvix_level=qvix_level, qvix_trend=qvix_trend, qvix_term=qvix_term,
        yield_spread=yield_spread, yield_trend=yield_trend, us10y=us10y,
        cny_trend=cny_trend, cg_trend=cg_trend, oil_signal=oil_signal,
        bdi_signal=bdi_signal, margin_signal=margin_signal,
        dxy_signal=dxy_signal, etf_flow=etf_flow, jobless_signal=jobless_signal,
        gold_oil_signal=gold_oil_signal, us10y_regime=us10y_regime,
    )
    composite_score, composite_label, dim_scores, notes = _calc_composite_score(**fwd_partial)

    fwd_partial.update(composite_score=composite_score, composite_label=composite_label)
    summary = _gen_summary(fwd_partial)
    astock_guidance = _gen_astock_guidance(
        composite_label, composite_score,
        qvix_level, qvix_term,
        yield_spread, cg_trend,
        dxy_signal, cny_trend,
        margin_signal, bdi_signal,
        jobless_signal, etf_flow,
        us10y_regime=us10y_regime,
        gold_oil_signal=gold_oil_signal,
    )

    return ForwardReading(
        qvix=qvix_val, qvix_level=qvix_level, qvix_trend=qvix_trend,
        qvix_term=qvix_term, qvix_term_ratio=qvix_term_ratio,
        us10y=us10y, cn10y=cn10y, yield_spread=yield_spread, yield_trend=yield_trend,
        us10y_regime=us10y_regime,
        cny_usd=cny_usd, cny_trend=cny_trend,
        copper_gold=copper_gold, cg_trend=cg_trend,
        oil_price=oil_price, oil_signal=oil_signal,
        bdi=bdi, bdi_trend=bdi_trend, bdi_signal=bdi_signal,
        gold_oil_ratio=gold_oil_ratio, gold_oil_signal=gold_oil_signal,
        gold_10d_chg=gold_10d_chg, oil_10d_chg=oil_10d_chg,
        margin_balance=margin_balance, margin_5d_chg=margin_5d_chg,
        margin_signal=margin_signal,
        dxy=dxy, dxy_trend=dxy_trend, dxy_signal=dxy_signal,
        etf_net_flow_signal=etf_flow,
        jobless_claims=jobless_claims, jobless_prev=jobless_prev,
        jobless_signal=jobless_signal,
        composite_score=composite_score, composite_label=composite_label,
        dim_scores=dim_scores, notes=notes,
        summary=summary, astock_guidance=astock_guidance,
    )


# ─── 飞书卡片 ────────────────────────────────────────────────────────────────

def _make_row(label: str, value: str, signal: str = "NEUTRAL") -> str:
    em = SIGNAL_EM.get(signal, "⚪")
    return f"{em} **{label}**  {value}"


def _dim_bar(score: float) -> str:
    filled = round(score / 100 * 8)
    return "█" * filled + "░" * (8 - filled)


def build_forward_card(fwd: ForwardReading, run_date: str) -> dict:
    """5分区飞书 Interactive Card"""
    em = COMPOSITE_EMOJI.get(fwd.composite_label, "⚪")
    label = COMPOSITE_LABEL.get(fwd.composite_label, "中性")
    color = {"BULLISH": "green", "NEUTRAL": "blue", "BEARISH": "red"}.get(fwd.composite_label, "blue")
    bar = _score_bar(fwd.composite_score)

    elements: list[dict] = []

    def _section(content: str) -> None:
        elements.append({"tag": "div", "text": {"tag": "lark_md", "content": content}})
        elements.append({"tag": "hr"})

    # ── 总评 ──
    dim_lines = "  ".join(
        f"{k.split('_')[1]}:{_dim_bar(v)}{v:.0f}"
        for k, v in fwd.dim_scores.items()
    )
    _section(
        f"**{em} {label}   {fwd.composite_score:.0f} / 100**\n"
        f"`{bar}`\n"
        f"{dim_lines}"
    )

    # ── 今日决策影响（一眼看懂"所以呢"）──
    impact_lines = []
    # regime 修正方向
    _regime_adj = 0
    if fwd.qvix_level == "PANIC": _regime_adj -= 8
    elif fwd.qvix_level == "HIGH": _regime_adj -= 5
    elif fwd.qvix_level == "ELEVATED": _regime_adj -= 2
    elif fwd.qvix_level == "LOW": _regime_adj -= 3
    if fwd.cg_trend == "RISK_OFF": _regime_adj -= 5
    elif fwd.cg_trend == "RISK_ON": _regime_adj += 3
    if fwd.gold_oil_signal == "GEOPOLITICAL_RISK": _regime_adj -= 5
    _regime_adj = max(-10, min(10, _regime_adj))
    if _regime_adj != 0:
        direction = "偏防御⬇" if _regime_adj < 0 else "偏进攻⬆"
        impact_lines.append(f"机制评分修正 **{_regime_adj:+.0f}分** → 系统{direction}")
    # 行动建议（从底部提上来）
    if fwd.astock_guidance:
        impact_lines.append(f"**操作方向** → {fwd.astock_guidance}")
    if not impact_lines:
        impact_lines.append("前瞻信号中性，系统无额外修正")
    _section("**🎯 今日系统影响**\n" + "\n".join(impact_lines))

    # ── A股板块风向标（前瞻→板块翻译）──
    sector_hints = []
    # 美债利率区间
    us10y_regime = getattr(fwd, "us10y_regime", "WATCH")
    if us10y_regime == "TECH_FAVOR":
        sector_hints.append("🟢 **科技/成长占优**〈日度〉：美债>4.4%利好半导体、AI算力、光模块")
    elif us10y_regime == "DEFENSIVE":
        sector_hints.append("🔴 **防御切换**〈日度〉：美债<4.3%资金回防，关注红利低波、公用事业、电力")
    # 铜金比
    cg = getattr(fwd, "cg_trend", "NEUTRAL")
    if cg == "RISK_ON":
        sector_hints.append("🟢 **周期进攻**〈周度〉：铜金比上行趋势，有色/铜矿/工程机械中期受益")
    elif cg == "RISK_OFF":
        sector_hints.append("🔴 **避险模式**〈周度〉：铜金比下行，中期回避周期股，黄金/国债/高股息占优")
    # QVIX
    ql = getattr(fwd, "qvix_level", "NORMAL")
    if ql in ("PANIC", "HIGH"):
        sector_hints.append("🔴 **全市场恐慌**〈日度〉：系统性缩仓，但强赛道恐慌日=逆向买入窗口")
    elif ql == "LOW":
        sector_hints.append("🟡 **低波自满**〈周度〉：市场平静但黑天鹅概率升高，控制新增仓位")
    # BDI
    bdi_sig = getattr(fwd, "bdi_signal", "NEUTRAL")
    if bdi_sig == "STRONG":
        sector_hints.append("🟢 **贸易活跃**〈中期1-2周〉：BDI强势，航运/出口链趋势向好（非日度信号）")
    elif bdi_sig == "WEAK":
        sector_hints.append("🟡 **贸易放缓**〈中期〉：BDI偏弱，出口链中期谨慎")
    # 金油比地缘
    go_sig = getattr(fwd, "gold_oil_signal", "NEUTRAL")
    if go_sig == "GEOPOLITICAL_RISK":
        sector_hints.append("🔴 **地缘避险**〈日度〉：金油比异动，军工/黄金受益，油气链承压")
    # 美元
    dxy_sig = getattr(fwd, "dxy_signal", "NEUTRAL")
    if dxy_sig == "RISK_OFF":
        sector_hints.append("🟡 **美元走强**〈周度〉：出口链汇率承压，内需消费相对受益")
    elif dxy_sig == "RISK_ON":
        sector_hints.append("🟢 **美元走弱**〈周度〉：新兴市场+出口链+有色受益")

    if sector_hints:
        _section("**📍 A股板块风向标**\n" + "\n".join(sector_hints))
    else:
        _section("**📍 A股板块风向标**\n⚪ 前瞻信号中性，无明显板块偏向，维持现有配置")

    # ── A. 波动 & 恐慌 ──
    vol_lines = ["**📡 波动 & 恐慌**"]
    if fwd.qvix is not None:
        qe = QVIX_EMOJI.get(fwd.qvix_level, "⚪")
        arrow = TREND_ARROW.get(fwd.qvix_trend, "")
        vol_lines.append(f"{qe} QVIX **{fwd.qvix:.1f}**  {QVIX_LABEL[fwd.qvix_level]} {arrow}")
        term_em = {"BACKWARDATION": "🟠", "FLAT": "⚪", "CONTANGO": "🟢"}.get(fwd.qvix_term, "⚪")
        term_label = {"BACKWARDATION": "近月>远月 期限倒挂⚠", "FLAT": "期限结构正常", "CONTANGO": "正向结构·平静"}.get(fwd.qvix_term, "")
        vol_lines.append(f"{term_em} 期限结构  {term_label}")
    _section("\n".join(vol_lines))

    # ── B. 利率 & 资本 ──
    rate_lines = ["**📐 利率 & 资本流动**"]
    if fwd.us10y is not None and fwd.cn10y is not None:
        spread_str = f"{fwd.yield_spread:+.0f}bp" if fwd.yield_spread is not None else "N/A"
        rate_lines.append(
            f"🏦 中美10Y  CN **{fwd.cn10y:.2f}%** / US **{fwd.us10y:.2f}%**  "
            f"利差 {spread_str}  {TREND_ARROW.get(fwd.yield_trend,'')}"
        )
    if fwd.us10y is not None:
        regime_em = {"TECH_FAVOR": "💻", "WATCH": "⚖️", "DEFENSIVE": "🛡"}.get(fwd.us10y_regime, "⚖️")
        regime_label = {
            "TECH_FAVOR":  "科技/成长占优区间（>4.4%）",
            "WATCH":       "观望区间（4.3-4.4%）均衡配置",
            "DEFENSIVE":   "防守切换区间（<4.3%）",
        }.get(fwd.us10y_regime, "")
        rate_lines.append(f"{regime_em} 美债利率区间  **{regime_label}**")
    if fwd.cny_usd is not None:
        cny_em = SIGNAL_EM.get(fwd.cny_trend, "⚪")
        cny_label = {"APPRECIATING": "人民币升值↑", "STABLE": "汇率平稳→", "DEPRECIATING": "人民币贬值↓"}.get(fwd.cny_trend, "")
        rate_lines.append(f"{cny_em} 美元/人民币 **{fwd.cny_usd:.4f}**  {cny_label}")
    _section("\n".join(rate_lines))

    # ── C. 大宗 & 周期 ──
    comm_lines = ["**⚒ 大宗 & 周期**"]
    if fwd.copper_gold is not None:
        cg_em = SIGNAL_EM.get(fwd.cg_trend, "⚪")
        cg_label = {"RISK_ON": "风险偏好↑", "RISK_OFF": "避险情绪↑", "NEUTRAL": "中性→"}.get(fwd.cg_trend, "")
        comm_lines.append(f"{cg_em} 铜金比 **{fwd.copper_gold:.2f}**  {cg_label}")
    if fwd.oil_price is not None:
        oil_em = SIGNAL_EM.get(fwd.oil_signal, "⚪")
        oil_label = {"BULLISH": "经济活动↑", "NEUTRAL": "经济平稳→", "BEARISH": "经济预期↓"}.get(fwd.oil_signal, "")
        comm_lines.append(
            f"{oil_em} 沪原油 **{fwd.oil_price:.0f}元/桶**  {oil_label}  {TREND_ARROW.get(fwd.oil_signal,'')}"
        )
    if fwd.bdi is not None:
        bdi_em = SIGNAL_EM.get(fwd.bdi_signal, "⚪")
        bdi_label = {"STRONG": "贸易活跃↑", "NEUTRAL": "贸易平稳→", "WEAK": "贸易放缓↓"}.get(fwd.bdi_signal, "")
        comm_lines.append(f"{bdi_em} BDI **{fwd.bdi:.0f}**  {bdi_label}  {TREND_ARROW.get(fwd.bdi_trend,'')}")
    if fwd.gold_oil_ratio is not None:
        go_em = {"GEOPOLITICAL_RISK": "🚨", "NEUTRAL": "⚪", "RISK_ON": "🟢"}.get(fwd.gold_oil_signal, "⚪")
        go_label = {
            "GEOPOLITICAL_RISK": "地缘避险信号⚠（黄金涨+原油跌）",
            "NEUTRAL":           "地缘风险平稳",
            "RISK_ON":           "风险偏好↑（油强金平）",
        }.get(fwd.gold_oil_signal, "")
        gold_chg_str = f"金{fwd.gold_10d_chg:+.1f}%" if fwd.gold_10d_chg is not None else ""
        oil_chg_str = f"油{fwd.oil_10d_chg:+.1f}%" if fwd.oil_10d_chg is not None else ""
        comm_lines.append(
            f"{go_em} 金油比 **{fwd.gold_oil_ratio:.3f}**  {go_label}  {gold_chg_str} {oil_chg_str}"
        )
    _section("\n".join(comm_lines))

    # ── D. 资金 & 杠杆 ──
    cap_lines = ["**💹 资金 & 杠杆**"]
    if fwd.margin_balance is not None:
        m_em = SIGNAL_EM.get(fwd.margin_signal, "⚪")
        chg = f"5日{fwd.margin_5d_chg:+.0f}亿" if fwd.margin_5d_chg is not None else ""
        m_label = {"LEVERAGING": "加杠杆↑", "DELEVERAGING": "去杠杆↓", "NEUTRAL": "平稳→"}.get(fwd.margin_signal, "")
        cap_lines.append(f"{m_em} 融资余额 **{fwd.margin_balance:.0f}亿**  {chg}  {m_label}")
    if fwd.dxy is not None:
        dxy_em = SIGNAL_EM.get(fwd.dxy_signal, "⚪")
        dxy_label = {"RISK_OFF": "美元走强·偏空↑", "NEUTRAL": "美元平稳→", "RISK_ON": "美元走弱·偏多↓"}.get(fwd.dxy_signal, "")
        cap_lines.append(f"{dxy_em} 美元指数 **{fwd.dxy:.1f}**  {dxy_label}  {TREND_ARROW.get(fwd.dxy_trend,'')}")
    etf_em = SIGNAL_EM.get(fwd.etf_net_flow_signal, "⚪")
    etf_label = {"INFLOW": "净流入·散户增配↑", "OUTFLOW": "净流出·谨慎↓", "NEUTRAL": "资金平衡→"}.get(fwd.etf_net_flow_signal, "")
    cap_lines.append(f"{etf_em} A股ETF资金流  {etf_label}")
    _section("\n".join(cap_lines))

    # ── E. 宏观 & 就业 ──
    macro_lines = ["**🌐 宏观 & 就业**"]
    if fwd.jobless_claims is not None:
        j_em = SIGNAL_EM.get(fwd.jobless_signal, "⚪")
        j_label = {"IMPROVING": "就业改善·软着陆↑", "NEUTRAL": "就业平稳→", "DETERIORATING": "就业恶化·衰退预警↓"}.get(fwd.jobless_signal, "")
        prev_str = f"前值{fwd.jobless_prev:.1f}万" if fwd.jobless_prev else ""
        macro_lines.append(f"{j_em} 美国初申失业金 **{fwd.jobless_claims:.1f}万**  {prev_str}  {j_label}")
    _section("\n".join(macro_lines))

    # ── 通俗总结 ──
    _section(f"**💡 宏观总结**\n{fwd.summary}")

    # ── 数据健康状态（标注获取失败的指标，不再静默假装中性）──
    _health_checks = [
        ("QVIX", fwd.qvix),
        ("美债10Y", fwd.us10y),
        ("铜金比", fwd.copper_gold),
        ("BDI", fwd.bdi),
        ("金油比", fwd.gold_oil_ratio),
        ("人民币", fwd.cny_usd),
        ("美元指数", fwd.dxy),
        ("两融余额", fwd.margin_balance),
    ]
    _failed = [name for name, val in _health_checks if val is None]
    if _failed:
        _health_text = f"**⚠️ 数据缺失**：{'、'.join(_failed)} 获取失败，相关判断可能不完整"
    else:
        _health_text = "✅ 全部数据源正常"
    elements.append({"tag": "div", "text": {"tag": "lark_md", "content": _health_text}})

    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text",
                      "content": "📊 QVIX·中美利差·铜金比·BDI·沪原油·美元·两融·人民币·ETF流·失业金 | 仅供参考，不构成投资建议"}],
    })

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"宏观前瞻  {em}{label}  {fwd.composite_score:.0f}分  |  {run_date}",
                },
                "template": color,
            },
            "elements": elements,
        },
    }


def format_forward_card_md(fwd: ForwardReading) -> str:
    """嵌入主卡片的简洁摘要行"""
    em = COMPOSITE_EMOJI.get(fwd.composite_label, "⚪")
    label = COMPOSITE_LABEL.get(fwd.composite_label, "中性")
    bar = _score_bar(fwd.composite_score)
    lines = [f"{em} **前瞻 {fwd.composite_score:.0f}/100 — {label}**  `{bar}`"]
    if fwd.qvix is not None:
        lines.append(f"  {QVIX_EMOJI.get(fwd.qvix_level,'⚪')} QVIX {fwd.qvix:.1f}  {QVIX_LABEL[fwd.qvix_level]}")
    if fwd.yield_spread is not None:
        lines.append(f"  🏦 中美利差 {fwd.yield_spread:+.0f}bp")
    if fwd.bdi is not None:
        lines.append(f"  🚢 BDI {fwd.bdi:.0f}")
    lines.append(f"  💡 {fwd.summary[:60]}…" if len(fwd.summary) > 60 else f"  💡 {fwd.summary}")
    return "\n".join(lines)


def send_forward_card(fwd: ForwardReading, run_date: str | None = None) -> bool:
    """推送前瞻指标卡片到专属飞书频道"""
    from config import FEISHU_FORWARD_WEBHOOKS
    from feishu_pusher import post_card

    if run_date is None:
        run_date = datetime.today().strftime("%Y-%m-%d")

    card = build_forward_card(fwd, run_date)
    return post_card(card, FEISHU_FORWARD_WEBHOOKS)
