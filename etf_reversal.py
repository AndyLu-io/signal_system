"""
ETF底部反转候选检测（供 main.py / notifier.py 调用）

与个股版本（stock_timing.check_reversal）使用相同的5维度评分：
  技术（max 6）+ 周期（max 4）+ 资金（max 2）+ 政策（max 4）+ 情绪（max 2）= 满分18，≥9入选
"""
from __future__ import annotations
import pandas as pd
from typing import Optional


# ── 基础计算 ──────────────────────────────────────────────────────────────────

def _ema(s: pd.Series, span: int) -> pd.Series:
    return s.ewm(span=span, adjust=False).mean()


def _resample_period(df: pd.DataFrame, freq: str) -> pd.DataFrame | None:
    try:
        idx    = df.set_index("date")
        closes = idx["close"].resample(freq).last().dropna()
        vols   = idx["volume"].resample(freq).sum()
        out    = closes.to_frame()
        out["volume"] = vols
        return out.reset_index()
    except Exception:
        return None


def _weekly_macd_state(df: pd.DataFrame) -> dict:
    df_w = _resample_period(df, "W")
    if df_w is None or len(df_w) < 30:
        return {"ok": False}
    close = df_w["close"].astype(float)
    dif   = _ema(close, 12) - _ema(close, 26)
    dea   = _ema(dif, 9)
    d, e  = float(dif.iloc[-1]),  float(dea.iloc[-1])
    dp,ep = float(dif.iloc[-2]),  float(dea.iloc[-2])
    bar_now  = float(2 * (dif - dea).iloc[-1])
    bar_prev = float(2 * (dif - dea).iloc[-2])
    return {
        "ok":         True,
        "golden":     d > e,
        "pre_cross":  d < e and d > dp and (e - d) < (ep - dp),
        "above_zero": d > 0,
        "bar_rising": bar_now > bar_prev,
        "dif":        round(d, 4),
        "dea":        round(e, 4),
    }


def _monthly_trend_state(df: pd.DataFrame) -> dict:
    df_m = _resample_period(df, "ME")
    if df_m is None or len(df_m) < 6:
        return {"ok": False}
    close     = df_m["close"].astype(float)
    ma3       = close.rolling(3).mean()
    ma6       = close.rolling(6).mean()
    cur       = float(close.iloc[-1])
    ma3_v     = float(ma3.iloc[-1])
    ma6_v     = float(ma6.iloc[-1])
    ma3_p     = float(ma3.iloc[-2]) if len(ma3.dropna()) >= 2 else ma3_v
    dif_proxy = ma3_v - ma6_v
    dif_p     = float(ma3.iloc[-2]) - float(ma6.iloc[-2]) if len(ma6.dropna()) >= 2 else dif_proxy
    return {
        "ok":           True,
        "above_ma6":    cur > ma6_v,
        "ma3_rising":   ma3_v > ma3_p,
        "dif_positive": dif_proxy > 0,
        "dif_rising":   dif_proxy > dif_p,
        "ma3":          round(ma3_v, 4),
        "ma6":          round(ma6_v, 4),
    }


def _dim_bar(score: int, max_score: int, label: str) -> str:
    filled = round(score / max_score * 5) if max_score > 0 else 0
    bar    = "█" * filled + "░" * (5 - filled)
    return f"{label}[{bar}]{score}/{max_score}"


# ── 核心评分 ──────────────────────────────────────────────────────────────────

def check_reversal(
    code: str,
    df: pd.DataFrame,
    info: dict,
    main_force_flow: float = 0.0,
    north_5d: Optional[float] = None,
    sentiment: Optional[object] = None,
) -> dict | None:
    """
    ETF底部反转候选筛选（满分18，≥9入选）。

    硬门槛（两条必须同时满足）：
      ① boll_pct < 0.50（价格在布林中线以下）
      ② MACD有改善迹象（即将金叉 / 刚金叉 / 绿柱收缩）

    软条件5维度：
      技术（max 6）: 日线MACD + Boll位置 + RSI
      周期（max 4）: 周线MACD + 月线MA3/MA6趋势
      资金（max 2）: 主力净流入
      政策（max 4）: f_policy + f_earnings + 北向5日净流入
      情绪（max 2）: SentimentReading level
    """
    if df is None or len(df) < 35:
        return None

    close = df["close"].astype(float)

    # ── 日线 MACD ──
    dif = _ema(close, 12) - _ema(close, 26)
    dea = _ema(dif, 9)
    bar = (dif - dea) * 2

    d, e   = float(dif.iloc[-1]),  float(dea.iloc[-1])
    dp, ep = float(dif.iloc[-2]),  float(dea.iloc[-2])
    bar_now  = float(bar.iloc[-1])
    bar_prev = float(bar.iloc[-2])

    cross = None
    if dp < ep and d >= e:
        cross = "golden"
    elif dp > ep and d <= e:
        cross = "death"
    pre_golden_cross = d < e and d > dp and (e - d) < (ep - dp)

    # ── Bollinger Bands (20, 2σ) ──
    ma20       = close.rolling(20).mean()
    std20      = close.rolling(20).std()
    boll_upper = float((ma20 + 2 * std20).iloc[-1])
    boll_lower = float((ma20 - 2 * std20).iloc[-1])
    width      = boll_upper - boll_lower
    boll_pct   = float((close.iloc[-1] - boll_lower) / width) if width > 0 else 0.5

    # ── RSI (14) ──
    delta    = close.diff()
    avg_gain = delta.clip(lower=0).rolling(14).mean()
    avg_loss = (-delta).clip(lower=0).rolling(14).mean()
    rsi      = float(100 - 100 / (1 + avg_gain.iloc[-1] / (avg_loss.iloc[-1] + 1e-9)))

    # ── 硬门槛 ──
    if boll_pct >= 0.50:
        return None

    macd_improving = (
        pre_golden_cross or
        cross == "golden" or
        (bar_now < 0 and bar_now > bar_prev)
    )
    if not macd_improving:
        return None

    pts      = 0
    details  = []
    dim_tech = dim_cycle = dim_fund = dim_policy = dim_senti = 0

    # ── 维度1: 技术面 ────────────────────────────────────────────────
    if pre_golden_cross:
        pts += 2; dim_tech += 2; details.append("日线MACD即将金叉🔔")
    elif cross == "golden":
        pts += 1; dim_tech += 1; details.append("日线MACD刚金叉")
    else:
        pts += 1; dim_tech += 1; details.append("日线MACD绿柱收缩")

    if boll_pct <= 0.15:
        pts += 2; dim_tech += 2; details.append(f"Boll触底({boll_pct:.0%})")
    elif boll_pct <= 0.30:
        pts += 1; dim_tech += 1; details.append(f"Boll接近下轨({boll_pct:.0%})")

    if rsi < 35:
        pts += 2; dim_tech += 2; details.append(f"RSI超卖{rsi:.0f}")
    elif rsi < 45:
        pts += 1; dim_tech += 1; details.append(f"RSI偏低{rsi:.0f}")

    # ── 维度2: 周期面 ────────────────────────────────────────────────
    wk = _weekly_macd_state(df)
    if wk["ok"]:
        if wk.get("golden") or wk.get("pre_cross"):
            pts += 2; dim_cycle += 2
            details.append("周线MACD" + ("金叉✅" if wk.get("golden") else "即将金叉🔔"))
        elif wk.get("above_zero"):
            pts += 1; dim_cycle += 1; details.append("周线DIF零轴以上")
        elif wk.get("bar_rising"):
            pts += 1; dim_cycle += 1; details.append("周线MACD柱改善")

    mk = _monthly_trend_state(df)
    if mk["ok"]:
        above    = mk.get("above_ma6", False)
        rising   = mk.get("ma3_rising", False)
        dif_pos  = mk.get("dif_positive", False)
        dif_rise = mk.get("dif_rising", False)
        if above and rising:
            pts += 2; dim_cycle += 2; details.append("月线站MA6且上升✅")
        elif above or dif_pos:
            pts += 1; dim_cycle += 1
            details.append("月线MA6支撑" if above else "月线趋势向好")
        elif dif_rise:
            pts += 1; dim_cycle += 1; details.append("月线趋势改善中")

    # ── 维度3: 资金面 ────────────────────────────────────────────────
    if main_force_flow > 0.5:
        pts += 2; dim_fund += 2; details.append(f"主力净流入{main_force_flow:.1f}亿💰")
    elif main_force_flow >= -0.3:
        pts += 1; dim_fund += 1; details.append("主力资金中性")

    # ── 维度4: 政策面 ────────────────────────────────────────────────
    f_policy   = info.get("f_policy", 60)
    f_earnings = info.get("f_earnings", 60)
    if f_policy >= 85:
        pts += 2; dim_policy += 2; details.append(f"强政策支撑(f_policy={f_policy})")
    elif f_policy >= 70:
        pts += 1; dim_policy += 1; details.append(f"政策评分{f_policy}")
    if f_earnings >= 75:
        pts += 1; dim_policy += 1; details.append(f"盈利质量佳(f_earnings={f_earnings})")
    if north_5d is not None and north_5d > 50:
        pts += 1; dim_policy += 1; details.append(f"北向5日净流入{north_5d:.0f}亿")

    # ── 维度5: 情绪面 ────────────────────────────────────────────────
    if sentiment is not None:
        slevel = getattr(sentiment, "level", "NORMAL")
        if slevel in ("COLD", "NORMAL"):
            pts += 2; dim_senti += 2; details.append(f"市场情绪{slevel}(最佳买点窗口)🟢")
        elif slevel == "WARM":
            pts += 1; dim_senti += 1; details.append("市场情绪温和，可介入")
        else:
            details.append(f"市场情绪{slevel}⚠️，反转需等回落")

    if pts < 9:
        return None

    return {
        "code":            code,
        "name":            info["name"],
        "info":            info,
        "close":           float(close.iloc[-1]),
        "rsi":             rsi,
        "boll_pct":        boll_pct,
        "boll_upper":      boll_upper,
        "boll_lower":      boll_lower,
        "main_force_flow": main_force_flow,
        "rev_pts":         pts,
        "rev_details":     details,
        "wk":              wk,
        "mk":              mk,
        "dim_tech":        dim_tech,
        "dim_cycle":       dim_cycle,
        "dim_fund":        dim_fund,
        "dim_policy":      dim_policy,
        "dim_senti":       dim_senti,
    }


# ── 飞书格式化 ────────────────────────────────────────────────────────────────

def _reversal_line(r: dict) -> str:
    info = r["info"]
    pts  = r["rev_pts"]
    bp   = r["boll_pct"]
    wk   = r.get("wk", {})
    mk   = r.get("mk", {})

    stars = "⭐" * min(pts // 3, 5)
    radar = (
        f"{_dim_bar(r.get('dim_tech',   0), 6, '技术')}  "
        f"{_dim_bar(r.get('dim_cycle',  0), 4, '周期')}  "
        f"{_dim_bar(r.get('dim_fund',   0), 2, '资金')}  "
        f"{_dim_bar(r.get('dim_policy', 0), 4, '政策')}  "
        f"{_dim_bar(r.get('dim_senti',  0), 2, '情绪')}"
    )
    boll_str = (
        f"Boll下轨={r['boll_lower']:.3f}  上轨={r['boll_upper']:.3f}"
        f"  价格位于{bp:.0%}位"
    )
    weekly_str = (
        "金叉✅" if wk.get("golden") else
        "即将金叉🔔" if wk.get("pre_cross") else
        "零轴↑" if wk.get("above_zero") else
        "偏弱⚠️"
    ) if wk.get("ok") else "—"
    monthly_str = (
        "站MA6✅" if mk.get("above_ma6") else
        "MA6支撑" if mk.get("dif_positive") else
        "改善中"  if mk.get("dif_rising")  else "偏弱"
    ) if mk.get("ok") else "—"

    flow     = r.get("main_force_flow", 0.0)
    flow_str = f"{'净流入💰' if flow > 0 else '净流出'}{abs(flow):.1f}亿" if abs(flow) >= 0.1 else "中性"
    details_str = "、".join(r["rev_details"])

    return (
        f"🔄 **{r['name']}**（{r['code']}）｜{info.get('cluster', '')}  "
        f"{stars}  **{pts}/18分**\n"
        f"  {radar}\n"
        f"  价格 **{r['close']:.3f}**  RSI={r['rsi']:.0f}  主力{flow_str}\n"
        f"  {boll_str}\n"
        f"  周线: {weekly_str}  ｜  月线: {monthly_str}\n"
        f"  ✅ {details_str}"
    )


def build_reversal_section(reversals: list[dict]) -> list[dict]:
    """返回可直接插入 Feishu card elements 的区块列表。"""
    if not reversals:
        return []
    body = "\n\n".join(_reversal_line(r) for r in reversals)
    return [
        {"tag": "hr"},
        {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": (
                    "**🔄 ━━ ETF底部反转候选（MACD/Boll/周月K多维确认）━━**\n\n"
                    + body
                ),
            },
        },
    ]
