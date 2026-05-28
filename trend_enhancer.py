"""
趋势加仓 + 分批建仓 + 止损延迟确认 + 异常熔断
独立模块，被 stock_timing.py 和 main.py 调用

核心功能：
1. 趋势加仓（TREND_ADD）：HOLD品种突破20日高点+放量+周MACD金叉 → 加仓信号
2. 分批建仓建议：BUY信号附加"首仓50%+T+1回调后补仓50%"
3. 止损延迟确认：破位后需连续3日未收回才确认止损
4. 异常熔断：单品种5日内跌幅>20% → 强制降级+预警
"""

from __future__ import annotations

import logging
import numpy as np
import pandas as pd
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


# ─── 1. 趋势加仓信号 ─────────────────────────────────────────────────────────

@dataclass
class TrendAddSignal:
    code: str
    name: str
    close: float
    high_20d: float           # 20日最高价
    breakout_pct: float       # 突破幅度%
    vol_ratio: float          # 量比（当日/5日均）
    weekly_macd_golden: bool  # 周线MACD是否金叉
    rsi14: float
    score: int                # 0-5 加仓评分
    reasons: list[str] = field(default_factory=list)
    cluster: str = ""
    signal_3d: str = ""


def scan_trend_add(
    stock_prices: dict[str, Optional[pd.DataFrame]],
    universe: dict,
    current_holds: set[str] | None = None,
) -> list[TrendAddSignal]:
    """
    扫描HOLD品种中的趋势加仓机会。
    触发条件（同时满足）：
    1. 品种当前处于HOLD状态（score在-2~4之间）或已持仓
    2. 收盘价突破20日最高价
    3. 量比>=1.3（放量确认）
    4. RSI 50-75（趋势中段，非超买）
    5. 加分项：周MACD金叉/三维共振★★★/MA60上行
    """
    results: list[TrendAddSignal] = []
    holds = current_holds or set()

    for code, info in universe.items():
        pool = info.get("pool", "watch")
        if pool == "watch":
            continue

        df = stock_prices.get(code)
        if df is None or len(df) < 25:
            continue

        closes = df["close"].values.astype(float)
        current = closes[-1]

        # 20日最高价（不含今日）
        high_20d = closes[-21:-1].max() if len(closes) >= 22 else closes[:-1].max()

        # 必须突破20日高点
        if current <= high_20d:
            continue

        breakout_pct = (current / high_20d - 1) * 100

        # 量比 >= 1.3
        if "volume" in df.columns and len(df) >= 6:
            vols = df["volume"].values.astype(float)
            vol_today = vols[-1]
            vol_5d = vols[-6:-1].mean()
            vol_ratio = vol_today / vol_5d if vol_5d > 0 else 1.0
        else:
            vol_ratio = 1.0

        if vol_ratio < 1.3:
            continue

        # RSI(14) 在 50-75
        if len(closes) >= 15:
            deltas = np.diff(closes[-15:])
            gains = np.where(deltas > 0, deltas, 0.0).mean()
            losses = np.where(deltas < 0, -deltas, 0.0).mean()
            rsi14 = 100 - 100 / (1 + gains / (losses + 1e-10))
        else:
            rsi14 = 50.0

        if rsi14 < 45 or rsi14 > 78:
            continue

        # 评分
        score = 0
        reasons = []

        reasons.append(f"突破20日高点+{breakout_pct:.1f}%")
        score += 1

        if vol_ratio >= 2.0:
            score += 2; reasons.append(f"放量{vol_ratio:.1f}x（强确认）")
        elif vol_ratio >= 1.5:
            score += 1; reasons.append(f"放量{vol_ratio:.1f}x")
        else:
            reasons.append(f"量比{vol_ratio:.1f}x")

        # 周线MACD金叉
        weekly_golden = False
        if len(closes) >= 60:
            # 简化周线判断：用5周均线斜率
            w5 = np.mean([closes[i*5:(i+1)*5].mean() for i in range(len(closes)//5 - 4, len(closes)//5)])
            w5_prev = np.mean([closes[i*5:(i+1)*5].mean() for i in range(len(closes)//5 - 5, len(closes)//5 - 1)])
            if w5 > w5_prev:
                weekly_golden = True
                score += 1; reasons.append("周线趋势向上")

        # MA60上行
        if len(closes) >= 65:
            ma60_now = closes[-60:].mean()
            ma60_prev = closes[-65:-5].mean()
            if ma60_now > ma60_prev * 1.005:
                score += 1; reasons.append(f"MA60上行+{(ma60_now/ma60_prev-1)*100:.1f}%")

        # 三维共振加分
        signal_3d = info.get("signal_3d", "★☆☆")
        if signal_3d == "★★★":
            score += 1; reasons.append("三维共振★★★")

        if score < 2:
            continue

        results.append(TrendAddSignal(
            code=code, name=info["name"],
            close=round(current, 2),
            high_20d=round(high_20d, 2),
            breakout_pct=round(breakout_pct, 2),
            vol_ratio=round(vol_ratio, 2),
            weekly_macd_golden=weekly_golden,
            rsi14=round(rsi14, 1),
            score=score, reasons=reasons,
            cluster=info.get("cluster", ""),
            signal_3d=signal_3d,
        ))

    results.sort(key=lambda s: (-s.score, -s.breakout_pct))
    return results


# ─── 2. 分批建仓建议 ─────────────────────────────────────────────────────────

def calc_batch_entry(close: float, ma5: float, signal: str) -> dict:
    """
    计算分批建仓方案。
    回测：BUY后T+1均亏-2.17%，分两笔可减少冲击。
    """
    if signal not in ("BUY_STRONG", "BUY_WATCH"):
        return {}

    # 首笔50%在信号日，补仓50%在回调到MA5附近
    entry1_pct = 50
    entry2_price = round(min(close * 0.985, ma5 * 1.005), 2)
    entry2_pct = 50

    return {
        "batch_entry": True,
        "entry1_pct": entry1_pct,
        "entry1_price": round(close, 2),
        "entry2_pct": entry2_pct,
        "entry2_price": entry2_price,
        "entry2_note": f"次日回调至{entry2_price}附近补仓（MA5={ma5:.2f}）",
    }


# ─── 3. 止损延迟确认 ─────────────────────────────────────────────────────────

def check_delayed_stop(
    code: str,
    df: pd.DataFrame,
    stop_price: float,
    confirm_days: int = 3,
) -> dict:
    """
    破位后需连续N日收盘价都在止损线下方才确认止损。
    回测：SELL_STOP后T+5=+2.81%（触发太早），延迟3日可过滤假破位。
    返回 {"confirmed": bool, "days_below": int, "should_stop": bool}
    """
    if df is None or len(df) < confirm_days + 1:
        return {"confirmed": False, "days_below": 0, "should_stop": False}

    closes = df["close"].values.astype(float)

    # 检查最近 confirm_days 天是否都在止损线下方
    days_below = 0
    for i in range(1, confirm_days + 1):
        if closes[-i] <= stop_price:
            days_below += 1
        else:
            break

    confirmed = days_below >= confirm_days
    # 特殊情况：单日暴跌>8%直接止损（不等确认）
    single_day_crash = (closes[-1] / closes[-2] - 1) < -0.08 if len(closes) >= 2 else False

    return {
        "confirmed": confirmed,
        "days_below": days_below,
        "should_stop": confirmed or single_day_crash,
        "note": f"破位{days_below}/{confirm_days}日确认" if not confirmed else "破位确认，执行止损",
    }


# ─── 4. 异常波动熔断 ─────────────────────────────────────────────────────────

@dataclass
class CircuitBreaker:
    code: str
    name: str
    drop_5d: float          # 5日跌幅%
    drop_10d: float         # 10日跌幅%
    level: str              # "WARNING" / "CRITICAL"
    note: str


def scan_circuit_breakers(
    stock_prices: dict[str, Optional[pd.DataFrame]],
    universe: dict,
) -> list[CircuitBreaker]:
    """
    扫描异常波动品种。
    回测：源杰科技连续HOLD但实际暴跌-30~-38%，系统未预警。
    规则：
    - WARNING: 5日跌幅 >= 15%
    - CRITICAL: 5日跌幅 >= 20% 或 10日跌幅 >= 25%
    """
    results: list[CircuitBreaker] = []

    for code, info in universe.items():
        pool = info.get("pool", "watch")
        if pool == "watch":
            continue

        df = stock_prices.get(code)
        if df is None or len(df) < 11:
            continue

        closes = df["close"].values.astype(float)
        current = closes[-1]

        # 5日跌幅
        drop_5d = (current / closes[-6] - 1) * 100 if len(closes) >= 6 else 0
        # 10日跌幅
        drop_10d = (current / closes[-11] - 1) * 100 if len(closes) >= 11 else 0

        level = None
        if drop_5d <= -20 or drop_10d <= -25:
            level = "CRITICAL"
        elif drop_5d <= -15:
            level = "WARNING"

        if level:
            results.append(CircuitBreaker(
                code=code, name=info["name"],
                drop_5d=round(drop_5d, 2),
                drop_10d=round(drop_10d, 2),
                level=level,
                note=f"5日{drop_5d:+.1f}% 10日{drop_10d:+.1f}% → 建议降级或止损",
            ))

    results.sort(key=lambda x: x.drop_5d)
    return results


# ─── 5. 飞书卡片构建 ─────────────────────────────────────────────────────────

def build_trend_add_card(signals: list[TrendAddSignal]) -> dict | None:
    """构建趋势加仓飞书卡片。"""
    if not signals:
        return None

    from datetime import datetime
    now = datetime.now().strftime("%m-%d %H:%M")

    lines = []
    for sig in signals[:10]:
        s3d = sig.signal_3d or ""
        lines.append(
            f"{'🔥' * min(sig.score, 3)} **{sig.name}**({sig.code}) {s3d} "
            f"突破+{sig.breakout_pct:.1f}% "
            f"量{sig.vol_ratio:.1f}x "
            f"RSI={sig.rsi14:.0f}"
        )
        lines.append(f"  └ {'; '.join(sig.reasons)}")

    elements = [
        {"tag": "markdown", "content": "**策略**：HOLD品种突破20日高点 + 放量≥1.3x + RSI中段 → 趋势加仓\n**仓位**：现有持仓追加50%，总仓位不超配置上限"},
        {"tag": "hr"},
        {"tag": "markdown", "content": "\n".join(lines)},
        {"tag": "hr"},
        {"tag": "markdown", "content": "⚠️ 加仓≠追高 · 必须已持有底仓 · 突破失败次日止盈离场"},
    ]

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"📈 趋势加仓信号 [{len(signals)}只] {now}"},
                "template": "green",
            },
            "elements": elements,
        },
    }


def build_circuit_breaker_card(breakers: list[CircuitBreaker]) -> dict | None:
    """构建异常熔断预警卡片。"""
    if not breakers:
        return None

    from datetime import datetime
    now = datetime.now().strftime("%m-%d %H:%M")

    lines = []
    for b in breakers:
        emoji = "🚨" if b.level == "CRITICAL" else "⚠️"
        lines.append(f"{emoji} **{b.name}**({b.code}) 5日{b.drop_5d:+.1f}% 10日{b.drop_10d:+.1f}%")
        lines.append(f"  └ {b.note}")

    elements = [
        {"tag": "markdown", "content": "**异常波动熔断**：以下品种近期跌幅异常，建议立即审视持仓"},
        {"tag": "hr"},
        {"tag": "markdown", "content": "\n".join(lines)},
    ]

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"🚨 异常熔断预警 [{len(breakers)}只] {now}"},
                "template": "red",
            },
            "elements": elements,
        },
    }
