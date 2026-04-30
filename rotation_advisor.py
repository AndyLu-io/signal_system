"""
攻守切换三维复合强度
三个维度独立评色，加权得出切换强度建议。

维度：
  情绪面 — sentiment_gauge 输出（HOT/OVERHEATED → 🔴）
  资金面 — 北向近5日净流向（流出 → 🔴）
  政策面 — 政策核心板块平均涨跌幅代理（跌 → 🔴）

强度映射（总分0-6，RED=2 YELLOW=1 GREEN=0）：
  4-6 → STRONG_SWITCH   三维共振，立即行动
  2-3 → SUGGEST_REDUCE  两维偏空，逐步移仓
  1   → CAUTION         单维警示，控制新增
  0   → NORMAL          无异常，正常操作
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sentiment_gauge import SentimentReading

# ─────────────────────────────────────────────────────────────────────────────
# 常量
# ─────────────────────────────────────────────────────────────────────────────

_DIM_SCORE = {"RED": 2, "YELLOW": 1, "GREEN": 0}

_STRENGTH_MAP = [
    (5, "STRONG_SWITCH"),   # ≥2 RED 或 3 RED
    (3, "SUGGEST_REDUCE"),  # 1 RED + 1 YELLOW 或 2 RED + GREEN
    (1, "CAUTION"),         # 单维警示（1 RED 或 ≥1 YELLOW）
    (0, "NORMAL"),
]

DIM_EMOJI = {"RED": "🔴", "YELLOW": "🟡", "GREEN": "🟢"}

STRENGTH_LABEL = {
    "STRONG_SWITCH":  "强切换 ▶ 三维共振，立即降低进攻仓位",
    "SUGGEST_REDUCE": "建议减仓 ▶ 两维偏空，逐步移仓防御",
    "CAUTION":        "谨慎观察 ▶ 单维警示，控制新增",
    "NORMAL":         "无异常 ▶ 正常操作",
}

STRENGTH_COLOR = {
    "STRONG_SWITCH":  "red",
    "SUGGEST_REDUCE": "orange",
    "CAUTION":        "yellow",
    "NORMAL":         "green",
}


# ─────────────────────────────────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RotationSignal:
    strength: str        # STRONG_SWITCH / SUGGEST_REDUCE / CAUTION / NORMAL
    sentiment_dim: str   # RED / YELLOW / GREEN
    capital_dim: str     # RED / YELLOW / GREEN
    policy_dim: str      # RED / YELLOW / GREEN
    total_score: int     # 0-6
    sentiment_reason: str
    capital_reason: str
    policy_reason: str

    @property
    def should_switch(self) -> bool:
        return self.strength in ("STRONG_SWITCH", "SUGGEST_REDUCE")

    def dim_line(self) -> str:
        """三维状态一行摘要，用于卡片标题区"""
        return (
            f"情绪{DIM_EMOJI[self.sentiment_dim]}  "
            f"资金{DIM_EMOJI[self.capital_dim]}  "
            f"政策{DIM_EMOJI[self.policy_dim]}"
        )

    def detail_block(self) -> str:
        """三维详细理由，用于卡片正文区"""
        lines = [
            f"情绪{DIM_EMOJI[self.sentiment_dim]} {self.sentiment_reason}",
            f"资金{DIM_EMOJI[self.capital_dim]} {self.capital_reason}",
            f"政策{DIM_EMOJI[self.policy_dim]} {self.policy_reason}",
        ]
        return "\n".join(lines)

    def label(self) -> str:
        return STRENGTH_LABEL.get(self.strength, self.strength)


# ─────────────────────────────────────────────────────────────────────────────
# 各维度评色
# ─────────────────────────────────────────────────────────────────────────────

def _sentiment_dim(senti: SentimentReading) -> tuple[str, str]:
    if senti.level in ("OVERHEATED", "HOT"):
        return "RED", (
            f"{senti.level}（{senti.score:.0f}/100）"
            f"  连涨{senti.consec_up_days}日  RSI(6)={senti.rsi6:.0f}"
        )
    if senti.level == "WARM":
        return "YELLOW", f"偏暖（{senti.score:.0f}/100）"
    if senti.level == "COLD":
        return "GREEN", f"市场恐惧（{senti.score:.0f}/100）— 逢低布局机会"
    return "GREEN", f"中性（{senti.score:.0f}/100）"


def _capital_dim(
    north_5d_billion: Optional[float],
    north_5d_deal_avg: Optional[float] = None,
    north_today_deal: Optional[float] = None,
) -> tuple[str, str]:
    """
    资金面维度。
    north_5d_billion      : 近5日净买入合计（亿元），有值时为精确信号
    north_5d_deal_avg     : 近5日成交总额均值（亿元），仅有活跃度时使用
    north_today_deal      : 今日成交总额（亿元），仅有活跃度时使用
    """
    # ── 精确净买入（手动数据） ──────────────────────────────────────────────
    if north_5d_billion is not None:
        if north_5d_billion < -30:
            return "RED",    f"北向近5日净流出 {north_5d_billion:.1f}亿（外资撤退）"
        if north_5d_billion < 10:
            return "YELLOW", f"北向近5日 {north_5d_billion:+.1f}亿（资金观望）"
        return "GREEN",      f"北向近5日净流入 {north_5d_billion:.1f}亿"

    # ── 活跃度代理（东方财富成交总额，无方向） ─────────────────────────────
    if north_today_deal is not None and north_5d_deal_avg is not None and north_5d_deal_avg > 0:
        dev = (north_today_deal - north_5d_deal_avg) / north_5d_deal_avg * 100
        label = f"今日{north_today_deal:.1f}亿 vs 5日均{north_5d_deal_avg:.1f}亿（偏离{dev:+.0f}%）"
        if dev > 20:
            return "GREEN",  f"北向活跃↑ — {label}（净方向未知，仅供参考）"
        if dev < -20:
            return "YELLOW", f"北向缩量↓ — {label}（净方向未知，仅供参考）"
        return "YELLOW",     f"北向成交正常 — {label}（净方向未知，仅供参考）"

    return "YELLOW", "北向数据：仅成交总额，无净买入（废额度后接口失效）"


def _policy_dim(policy_sector_avg_chg: Optional[float]) -> tuple[str, str]:
    """
    政策面代理：政策核心板块（电网/半导体/光通信）平均当日涨跌幅。
    连续下跌说明政策增量预期减弱，资金在兑现。
    """
    if policy_sector_avg_chg is None:
        return "YELLOW", "政策板块数据缺失"
    if policy_sector_avg_chg < -0.5:
        return "RED", f"政策板块平均 {policy_sector_avg_chg:.1f}%（资金兑现，动能减弱）"
    if policy_sector_avg_chg < 0.3:
        return "YELLOW", f"政策板块平均 {policy_sector_avg_chg:+.1f}%（无增量催化）"
    return "GREEN", f"政策板块平均 {policy_sector_avg_chg:+.1f}%（资金维持）"


# ─────────────────────────────────────────────────────────────────────────────
# 主函数
# ─────────────────────────────────────────────────────────────────────────────

def calc_rotation_signal(
    sentiment: SentimentReading,
    north_5d_billion: Optional[float],
    policy_sector_avg_chg: Optional[float],
    north_5d_deal_avg: Optional[float] = None,
    north_today_deal: Optional[float] = None,
) -> RotationSignal:
    """
    三维综合评判，返回 RotationSignal。

    Args:
        sentiment            : 情绪温度计读数
        north_5d_billion     : 北向近5日净买入（亿元），手动数据有值时精确；None 则用活跃度代理
        policy_sector_avg_chg: 政策核心板块平均当日涨跌幅(%)
        north_5d_deal_avg    : 近5日成交总额均值（亿元），活跃度代理用
        north_today_deal     : 今日成交总额（亿元），活跃度代理用
    """
    s_dim, s_reason = _sentiment_dim(sentiment)
    c_dim, c_reason = _capital_dim(north_5d_billion, north_5d_deal_avg, north_today_deal)
    p_dim, p_reason = _policy_dim(policy_sector_avg_chg)

    total = _DIM_SCORE[s_dim] + _DIM_SCORE[c_dim] + _DIM_SCORE[p_dim]
    strength = next(
        label for threshold, label in _STRENGTH_MAP if total >= threshold
    )

    return RotationSignal(
        strength=strength,
        sentiment_dim=s_dim,
        capital_dim=c_dim,
        policy_dim=p_dim,
        total_score=total,
        sentiment_reason=s_reason,
        capital_reason=c_reason,
        policy_reason=p_reason,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 飞书卡片格式化（供各 notifier 复用）
# ─────────────────────────────────────────────────────────────────────────────

def format_rotation_card_md(signal: RotationSignal, pool: dict) -> str:
    """
    生成攻守切换飞书卡片段落（lark_md 格式）。
    pool 格式：{code: {"name": str, "defense_rank": int, "note": str}}
    """
    icon_map = {"STRONG_SWITCH": "🔴", "SUGGEST_REDUCE": "🟠", "CAUTION": "🟡"}
    icon = icon_map.get(signal.strength, "🟢")
    s_label = STRENGTH_LABEL.get(signal.strength, signal.strength)

    lines = [
        f"**{icon} 攻守切换 — {s_label}**",
        f"{signal.dim_line()}　总分 {signal.total_score}/6",
        "",
        signal.detail_block(),
        "",
        "**↓ 推荐防御标的（按防御等级排序）**",
    ]
    by_rank: dict[int, list] = {}
    for code, info in pool.items():
        by_rank.setdefault(info["defense_rank"], []).append((code, info))
    rank_labels = {1: "极防御", 2: "高股息蓝筹", 3: "消费/医药"}
    for rank in sorted(by_rank):
        lines.append(f"**{rank_labels.get(rank, f'R{rank}')}**")
        for code, info in by_rank[rank]:
            lines.append(f"• {info['name']}（{code}）— {info['note']}")
    return "\n".join(lines)
