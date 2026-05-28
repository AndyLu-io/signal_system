"""
回踩买入扫描器 — ETF + 个股
核心逻辑：好品种+回调到支撑位 = 最佳买入时机

触发条件（同时满足）：
1. 品种质量：tier in (S, A) 或 composite >= 60 或 signal_3d in (★★★, ★★☆)
2. 近期涨过：过去10日最高点距当前回撤 >= 3%
3. 回踩支撑：当前价格在 MA20 ± 2% 范围内
4. 动能未崩：RSI(14) 在 30-50 之间（超卖但非崩盘）
5. 量能萎缩：近3日量比 < 0.8（抛压衰竭）

输出：独立飞书卡片，与主信号分离
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))

from config import (
    ETF_UNIVERSE, STOCK_UNIVERSE,
    FEISHU_WEBHOOKS, FEISHU_STOCK_WEBHOOKS,
)
from feishu_pusher import post_card as _post_card

logger = logging.getLogger(__name__)


@dataclass
class PullbackSignal:
    code: str
    name: str
    source: str                 # "etf" / "stock"
    close: float
    ma20: float
    ma20_dev_pct: float         # 距MA20偏离%
    rsi14: float
    pullback_pct: float         # 从10日高点回撤幅度%
    vol_ratio_3d: float         # 近3日均量/前20日均量
    score: float                # 综合质量得分
    tier: str = ""              # S/A/B/C/D
    composite: float = 0.0     # 五因子综合分
    signal_3d: str = ""        # ★★★/★★☆/★☆☆
    cluster: str = ""
    reasons: list[str] = field(default_factory=list)
    stars: int = 0              # 1-3 星


def _calc_rsi14(closes: np.ndarray) -> float:
    if len(closes) < 15:
        return 50.0
    deltas = np.diff(closes[-15:])
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)
    avg_gain = gains.mean()
    avg_loss = losses.mean()
    if avg_loss == 0:
        return 100.0
    return round(100 - 100 / (1 + avg_gain / avg_loss), 2)


def scan_etf_pullbacks(
    etf_prices: dict[str, Optional[pd.DataFrame]],
    factor_scores: dict[str, dict],
    regime: str,
) -> list[PullbackSignal]:
    """扫描ETF池中的回踩买入机会。"""
    from config import REGIME_PARAMS
    results: list[PullbackSignal] = []
    allowed_tiers = REGIME_PARAMS[regime]["allowed_tiers"]

    for code, info in ETF_UNIVERSE.items():
        df = etf_prices.get(code)
        if df is None or len(df) < 22:
            continue

        pool = info.get("pool", "watch")
        if pool == "watch":
            continue

        scores = factor_scores.get(code, {})
        tier = scores.get("tier", "D")
        composite = scores.get("composite", 0)

        # 品种质量门槛：tier S/A 或 composite >= 60
        if tier not in ("S", "A") and composite < 60:
            continue

        closes = df["close"].values.astype(float)
        current = closes[-1]

        # MA20
        ma20 = closes[-20:].mean()
        ma20_dev = (current - ma20) / ma20 * 100

        # 回踩支撑：在 MA20 上下 2% 范围内
        if ma20_dev < -4.0 or ma20_dev > 2.0:
            continue

        # 从10日高点回撤 >= 3%
        high_10d = closes[-10:].max()
        pullback = (high_10d - current) / high_10d * 100
        if pullback < 3.0:
            continue

        # RSI(14) 在 30-55 之间
        rsi14 = _calc_rsi14(closes)
        if rsi14 < 25 or rsi14 > 55:
            continue

        # 量能萎缩：近3日均量 / 前20日均量
        vol_ratio_3d = 1.0
        if "volume" in df.columns and len(df) >= 22:
            vols = df["volume"].values.astype(float)
            vol_3d = vols[-3:].mean()
            vol_20d = vols[-20:-3].mean()
            if vol_20d > 0:
                vol_ratio_3d = round(vol_3d / vol_20d, 2)

        # 量能不能太大（放量下跌=出货）
        if vol_ratio_3d > 1.3:
            continue

        # 构建信号
        reasons = []
        reasons.append(f"10日高点回撤{pullback:.1f}%")
        reasons.append(f"回踩MA20({ma20_dev:+.1f}%)")
        reasons.append(f"RSI(14)={rsi14:.0f}(偏低区间)")
        if vol_ratio_3d <= 0.7:
            reasons.append(f"缩量{vol_ratio_3d:.1f}x(抛压衰竭)")
        else:
            reasons.append(f"量比{vol_ratio_3d:.1f}x")

        # 星级评定
        star_score = 0
        if pullback >= 5:
            star_score += 1
        if rsi14 <= 40:
            star_score += 1
        if vol_ratio_3d <= 0.7:
            star_score += 1
        if tier in ("S", "A") and tier in allowed_tiers:
            star_score += 1
        if ma20_dev >= -1.5 and ma20_dev <= 0.5:
            star_score += 1

        stars = min(3, max(1, star_score - 1))

        results.append(PullbackSignal(
            code=code, name=info["name"], source="etf",
            close=round(current, 3), ma20=round(ma20, 3),
            ma20_dev_pct=round(ma20_dev, 2),
            rsi14=rsi14, pullback_pct=round(pullback, 2),
            vol_ratio_3d=vol_ratio_3d,
            score=composite, tier=tier, composite=composite,
            cluster=info.get("cluster", ""),
            reasons=reasons, stars=stars,
        ))

    results.sort(key=lambda s: (-s.stars, -s.pullback_pct))
    return results


def scan_stock_pullbacks(
    stock_prices: dict[str, Optional[pd.DataFrame]],
    regime: str,
) -> list[PullbackSignal]:
    """扫描个股池中的回踩买入机会。"""
    results: list[PullbackSignal] = []

    for code, info in STOCK_UNIVERSE.items():
        pool = info.get("pool", "watch")
        if pool == "watch":
            continue

        signal_3d = info.get("signal_3d", "★☆☆")
        # 品种质量门槛
        if signal_3d == "★☆☆" and info.get("f_policy", 0) < 80:
            continue

        df = stock_prices.get(code)
        if df is None or len(df) < 22:
            continue

        closes = df["close"].values.astype(float)
        current = closes[-1]

        ma20 = closes[-20:].mean()
        ma20_dev = (current - ma20) / ma20 * 100

        # 回踩支撑：MA20 ± 3%（个股波动大于ETF）
        if ma20_dev < -5.0 or ma20_dev > 3.0:
            continue

        # 从10日高点回撤 >= 5%（个股波动大，阈值宽于ETF）
        high_10d = closes[-10:].max()
        pullback = (high_10d - current) / high_10d * 100
        if pullback < 5.0:
            continue

        # RSI(14)
        rsi14 = _calc_rsi14(closes)
        if rsi14 < 20 or rsi14 > 50:
            continue

        # 量能
        vol_ratio_3d = 1.0
        if "volume" in df.columns and len(df) >= 22:
            vols = df["volume"].values.astype(float)
            vol_3d = vols[-3:].mean()
            vol_20d = vols[-20:-3].mean()
            if vol_20d > 0:
                vol_ratio_3d = round(vol_3d / vol_20d, 2)

        if vol_ratio_3d > 1.2:
            continue

        # MA60 趋势必须向上（只在上升趋势中回踩才买）
        if len(closes) >= 65:
            ma60_now = closes[-60:].mean()
            ma60_prev = closes[-65:-5].mean()
            ma60_slope = (ma60_now - ma60_prev) / ma60_prev * 100
            if ma60_slope < -0.2:
                continue
        else:
            ma60_slope = 0.0

        reasons = []
        reasons.append(f"10日回撤{pullback:.1f}%")
        reasons.append(f"回踩MA20({ma20_dev:+.1f}%)")
        reasons.append(f"RSI={rsi14:.0f}")
        if vol_ratio_3d <= 0.7:
            reasons.append(f"缩量{vol_ratio_3d:.1f}x")
        if ma60_slope > 0.5:
            reasons.append(f"MA60上行+{ma60_slope:.1f}%")

        # 星级
        star_score = 0
        if pullback >= 8:
            star_score += 1
        if rsi14 <= 35:
            star_score += 1
        if vol_ratio_3d <= 0.7:
            star_score += 1
        if signal_3d == "★★★":
            star_score += 1
        if pool == "core":
            star_score += 1

        stars = min(3, max(1, star_score - 1))

        results.append(PullbackSignal(
            code=code, name=info["name"], source="stock",
            close=round(current, 2), ma20=round(ma20, 2),
            ma20_dev_pct=round(ma20_dev, 2),
            rsi14=rsi14, pullback_pct=round(pullback, 2),
            vol_ratio_3d=vol_ratio_3d,
            score=info.get("f_policy", 50),
            tier="", composite=0,
            signal_3d=signal_3d,
            cluster=info.get("cluster", ""),
            reasons=reasons, stars=stars,
        ))

    results.sort(key=lambda s: (-s.stars, -s.pullback_pct))
    return results


# ─── 飞书卡片 ────────────────────────────────────────────────────────────────

_STAR_EMOJI = {3: "🌟🌟🌟", 2: "🌟🌟", 1: "🌟"}


def _build_etf_card(signals: list[PullbackSignal]) -> dict:
    """构建 ETF 回踩买入飞书卡片。"""
    from datetime import datetime
    now = datetime.now().strftime("%m-%d %H:%M")

    elements = []
    elements.append({
        "tag": "markdown",
        "content": (
            "**策略**：优质ETF(S/A级) + 10日高点回撤≥3% + 回踩MA20支撑 + RSI偏低 + 缩量\n"
            "**止损**：跌破MA20下方3%即止损（结构破坏）"
        ),
    })
    elements.append({"tag": "hr"})

    if signals:
        lines = []
        for sig in signals[:8]:
            star = _STAR_EMOJI.get(sig.stars, "🌟")
            lines.append(
                f"{star} **{sig.name}**({sig.code}) "
                f"回撤{sig.pullback_pct:.1f}% "
                f"MA20偏{sig.ma20_dev_pct:+.1f}% "
                f"RSI={sig.rsi14:.0f} "
                f"量比{sig.vol_ratio_3d:.1f}x "
                f"[{sig.tier}级/{sig.composite:.0f}分]"
            )
            lines.append(f"  └ {'; '.join(sig.reasons)}")
        elements.append({"tag": "markdown", "content": "\n".join(lines)})
    else:
        elements.append({"tag": "markdown", "content": "今日无符合条件的ETF回踩品种"})

    elements.append({"tag": "hr"})
    elements.append({
        "tag": "markdown",
        "content": "⚠️ 回踩≠抄底 · 确认MA60向上 · 止损MA20下方3% · 单品种≤5%",
    })

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"📉 ETF回踩买入窗口 [{len(signals)}只] {now}"},
                "template": "wathet",
            },
            "elements": elements,
        },
    }


def _build_stock_card(signals: list[PullbackSignal]) -> dict:
    """构建个股回踩买入飞书卡片。"""
    from datetime import datetime
    now = datetime.now().strftime("%m-%d %H:%M")

    elements = []
    elements.append({
        "tag": "markdown",
        "content": (
            "**策略**：优质个股(★★★/core) + 10日高点回撤≥5% + 回踩MA20 + RSI偏低 + 缩量 + MA60向上\n"
            "**止损**：跌破MA20下方3%即止损（结构破坏）"
        ),
    })
    elements.append({"tag": "hr"})

    if signals:
        lines = []
        for sig in signals[:12]:
            star = _STAR_EMOJI.get(sig.stars, "🌟")
            s3d = sig.signal_3d or ""
            lines.append(
                f"{star} **{sig.name}**({sig.code}) {s3d} "
                f"回撤{sig.pullback_pct:.1f}% "
                f"MA20偏{sig.ma20_dev_pct:+.1f}% "
                f"RSI={sig.rsi14:.0f} "
                f"量比{sig.vol_ratio_3d:.1f}x"
            )
            lines.append(f"  └ {'; '.join(sig.reasons)}")
        elements.append({"tag": "markdown", "content": "\n".join(lines)})
    else:
        elements.append({"tag": "markdown", "content": "今日无符合条件的个股回踩品种"})

    elements.append({"tag": "hr"})
    elements.append({
        "tag": "markdown",
        "content": "⚠️ 回踩≠抄底 · 确认MA60向上 · 止损MA20下方3% · 单品种≤5%",
    })

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"📉 个股回踩买入窗口 [{len(signals)}只] {now}"},
                "template": "wathet",
            },
            "elements": elements,
        },
    }


def push_pullback_card(
    etf_signals: list[PullbackSignal],
    stock_signals: list[PullbackSignal],
    regime: str,
    dry: bool = False,
) -> bool:
    """分别推送 ETF/个股 回踩买入卡片到对应频道。"""
    if not etf_signals and not stock_signals:
        logger.info("回踩买入：无信号，跳过推送")
        return False

    ok = False

    # ETF 卡片 → ETF 频道
    if etf_signals:
        etf_card = _build_etf_card(etf_signals)
        if dry:
            import json
            print("=== ETF PULLBACK CARD ===")
            print(json.dumps(etf_card, ensure_ascii=False, indent=2))
        else:
            if _post_card(etf_card, FEISHU_WEBHOOKS):
                ok = True

    # 个股卡片 → 个股频道
    if stock_signals:
        stock_card = _build_stock_card(stock_signals)
        if dry:
            import json
            print("=== STOCK PULLBACK CARD ===")
            print(json.dumps(stock_card, ensure_ascii=False, indent=2))
        else:
            if _post_card(stock_card, FEISHU_STOCK_WEBHOOKS):
                ok = True

    return ok or dry
