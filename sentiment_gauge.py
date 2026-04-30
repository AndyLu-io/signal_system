"""
市场情绪温度计
核心哲学：别人贪婪我恐惧，别人恐惧我贪婪
当日情绪过热 → 次日低开下探概率大 → 禁止追高
"""

from __future__ import annotations
import logging
from dataclasses import dataclass
from typing import Optional
import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SentimentReading:
    score: float          # 0-100，越高越热
    level: str            # COLD / NEUTRAL / WARM / HOT / OVERHEATED
    consec_up_days: int   # 大盘连续上涨天数
    ma5_dev_pct: float    # CSI300 距 MA5 偏离度(%)
    ma20_dev_pct: float   # CSI300 距 MA20 偏离度(%)
    rsi6: float           # CSI300 RSI(6)
    limit_up: int         # 今日涨停家数
    limit_down: int       # 今日跌停家数
    breadth_ratio: float  # 涨停/跌停比
    timing_block: bool    # True = 禁止追高
    note: str             # 诊断描述


LEVEL_THRESHOLDS = [
    (80, "OVERHEATED"),  # 严重过热：禁止追高，等待回落
    (65, "HOT"),          # 偏热：信号降级，控制仓位
    (45, "WARM"),         # 偏暖：正常但保持谨慎
    (25, "NEUTRAL"),      # 中性：可正常操作
    (0,  "COLD"),         # 冷场：恐惧期，逢低布局机会
]

LEVEL_LABEL = {
    "OVERHEATED": "严重过热",
    "HOT":        "情绪偏热",
    "WARM":       "偏暖谨慎",
    "NEUTRAL":    "情绪中性",
    "COLD":       "市场恐惧",
}


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


def _count_consecutive_up(closes: np.ndarray) -> int:
    count = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] > closes[i - 1]:
            count += 1
        else:
            break
    return count


def _score_breadth(ratio: float) -> float:
    """涨停/跌停比 → 情绪得分"""
    if ratio >= 5:   return 95
    if ratio >= 3:   return 78
    if ratio >= 2:   return 62
    if ratio >= 1.2: return 48
    if ratio >= 0.7: return 32
    if ratio >= 0.3: return 16
    return 5


def _score_consec_up(days: int) -> float:
    """连续上涨天数 → 情绪得分（接盘真空风险）"""
    if days >= 7: return 98
    if days >= 5: return 85
    if days >= 4: return 72
    if days >= 3: return 58
    if days >= 2: return 40
    if days == 1: return 28
    return 15


def _score_ma_dev(dev_pct: float, ma_type: str) -> float:
    """均线偏离度 → 情绪得分"""
    thresholds = {
        "ma5":  [(5, 95), (3, 80), (2, 65), (1, 50), (0, 38), (-2, 22), (-99, 8)],
        "ma20": [(8, 95), (5, 80), (3, 63), (0, 46), (-3, 30), (-99, 10)],
    }
    for threshold, score in thresholds.get(ma_type, []):
        if dev_pct >= threshold:
            return float(score)
    return 8.0


def _score_rsi(rsi: float) -> float:
    if rsi >= 85: return 98
    if rsi >= 75: return 82
    if rsi >= 65: return 63
    if rsi >= 55: return 48
    if rsi >= 45: return 35
    if rsi >= 35: return 20
    return 8


def _level_from_score(score: float) -> str:
    for threshold, level in LEVEL_THRESHOLDS:
        if score >= threshold:
            return level
    return "COLD"


def calc_market_sentiment(
    csi300: Optional[pd.DataFrame],
    breadth: dict,
) -> SentimentReading:
    """
    五项指标加权合成情绪温度

    权重设计原则：宽度数据最直接反映参与情绪；
    连涨天数衡量"接盘真空"风险；RSI捕捉短期超买。
    """
    limit_up = breadth.get("limit_up", 0)
    limit_down = breadth.get("limit_down", 0)
    breadth_ratio = breadth.get("ratio", 1.0)

    consec_up = 0
    ma5_dev = 0.0
    ma20_dev = 0.0
    rsi6 = 50.0

    if csi300 is not None and len(csi300) >= 22:
        closes = csi300["close"].values
        current = closes[-1]
        ma5 = closes[-5:].mean()
        ma20 = closes[-20:].mean()

        ma5_dev = round((current - ma5) / ma5 * 100, 2)
        ma20_dev = round((current - ma20) / ma20 * 100, 2)
        rsi6 = _calc_rsi(closes, 6)
        consec_up = _count_consecutive_up(closes)

    s_breadth  = _score_breadth(breadth_ratio)
    s_consec   = _score_consec_up(consec_up)
    s_ma5_dev  = _score_ma_dev(ma5_dev, "ma5")
    s_ma20_dev = _score_ma_dev(ma20_dev, "ma20")
    s_rsi      = _score_rsi(rsi6)

    score = round(
        s_breadth  * 0.30 +
        s_consec   * 0.22 +
        s_ma5_dev  * 0.16 +
        s_ma20_dev * 0.12 +
        s_rsi      * 0.20,
        1,
    )

    level = _level_from_score(score)
    timing_block = level in ("OVERHEATED", "HOT")

    notes = []
    if consec_up >= 3:
        notes.append(f"大盘已连涨{consec_up}日（谁来接盘？）")
    if ma5_dev >= 2:
        notes.append(f"距MA5偏离{ma5_dev:+.1f}%")
    if breadth_ratio >= 2:
        notes.append(f"涨停/跌停={breadth_ratio:.1f}x（情绪亢奋）")
    if rsi6 >= 70:
        notes.append(f"RSI(6)={rsi6:.0f}（超买）")
    if level == "COLD":
        notes.append("市场恐惧，可逢低关注核心品种")
    elif not notes:
        notes.append("情绪中性，正常操作")

    return SentimentReading(
        score=score,
        level=level,
        consec_up_days=consec_up,
        ma5_dev_pct=ma5_dev,
        ma20_dev_pct=ma20_dev,
        rsi6=rsi6,
        limit_up=limit_up,
        limit_down=limit_down,
        breadth_ratio=breadth_ratio,
        timing_block=timing_block,
        note="；".join(notes),
    )
