"""
信号后验复盘脚本

Phase 1: ETF 主信号   (--type etf)    来源: signal_detail_*.json
Phase 2: 个股择时     (--type stock)  来源: stock_timing_*.json
Phase 3: 尾盘买点     (--type tail)   来源: tail_detail_*.json

Usage:
    python3 signal_system/signal_review.py
    python3 signal_system/signal_review.py --from 2026-04-20 --to 2026-04-29
    python3 signal_system/signal_review.py --type etf
    python3 signal_system/signal_review.py --type stock
    python3 signal_system/signal_review.py --type tail
    python3 signal_system/signal_review.py --type all
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
from collections import defaultdict
from dataclasses import asdict, dataclass, field, fields as dc_fields
from datetime import datetime
from pathlib import Path
from typing import Optional

_ROOT     = Path(__file__).parent
_LOG_DIR  = _ROOT / "logs"
_REVIEW_DIR = _ROOT / "review"
_CACHE_DIR  = _ROOT / "state" / "price_cache"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ─── 数据类 ───────────────────────────────────────────────────────────────────

@dataclass
class SignalRecord:
    source: str                          # main / tail / stock
    signal_date: str                     # YYYY-MM-DD
    code: str
    name: str
    signal: str                          # BUY_STRONG / BUY_WATCH / HOLD / REDUCE / SELL_STOP / AVOID / TAIL_Nstar
    score: Optional[float] = None        # composite score 或 尾盘 score
    tier: Optional[str] = None           # ETF 信念等级
    stars: Optional[int] = None          # 尾盘星级
    regime: Optional[str] = None         # R1/R2/R3/R4
    sentiment_level: Optional[str] = None
    position_pct: Optional[float] = None
    stop_loss_pct: Optional[float] = None
    theme: Optional[str] = None
    cluster: Optional[str] = None
    pool: Optional[str] = None
    signal_3d: Optional[str] = None
    market_panic: Optional[bool] = None  # 尾盘恐慌日
    triggers: Optional[str] = None
    cautions: Optional[str] = None
    note: Optional[str] = None
    # ── 后验字段 ──
    close_t: Optional[float] = None
    close_t1: Optional[float] = None
    close_t3: Optional[float] = None
    close_t5: Optional[float] = None
    close_t10: Optional[float] = None
    ret_1d: Optional[float] = None
    ret_3d: Optional[float] = None
    ret_5d: Optional[float] = None
    ret_10d: Optional[float] = None
    max_gain_10d: Optional[float] = None
    max_drawdown_10d: Optional[float] = None
    ret_csi300_1d: Optional[float] = None
    ret_csi300_3d: Optional[float] = None
    ret_csi300_5d: Optional[float] = None
    ret_csi300_10d: Optional[float] = None
    excess_1d: Optional[float] = None
    excess_3d: Optional[float] = None
    excess_5d: Optional[float] = None
    excess_10d: Optional[float] = None
    hit_stop_10d: Optional[bool] = None
    review_status: str = "pending"       # complete / pending / missing_price


# ─── 价格缓存 ──────────────────────────────────────────────────────────────────

def _cache_load(code: str) -> dict:
    """Return {"updated": "YYYY-MM-DD", "prices": {"YYYY-MM-DD": {"close":, "high":, "low":}}}"""
    f = _CACHE_DIR / f"{code}.json"
    if f.exists():
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"updated": "", "prices": {}}


def _cache_save(code: str, data: dict) -> None:
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (_CACHE_DIR / f"{code}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


_INDEX_SH = {"000300", "000905", "000016", "000001", "000852", "000688"}  # 上证系指数
_INDEX_SZ = {"399001", "399006", "399300"}                               # 深证系指数


def _code_symbol(code: str) -> str:
    """为腾讯 K线接口组装正确的 symbol（区分指数/ETF/个股）。"""
    from data_fetcher import _market  # noqa
    if code in _INDEX_SH:
        return f"sh{code}"
    if code in _INDEX_SZ:
        return f"sz{code}"
    return f"{_market(code)}{code}"


def fetch_ohlc(codes: list[str], days: int = 55) -> dict[str, dict[str, dict]]:
    """
    获取每个 code 的 OHLC 数据，优先使用缓存（当天已拉取则跳过）。
    返回 {code: {date_str: {"close": float, "high": float, "low": float}}}
    """
    from data_fetcher import _klines_to_df, _tencent_kline  # noqa

    today = datetime.today().strftime("%Y-%m-%d")
    result: dict[str, dict[str, dict]] = {}

    for code in codes:
        cached = _cache_load(code)
        if cached["updated"] == today and cached["prices"]:
            result[code] = cached["prices"]
            continue

        sym = _code_symbol(code)
        klines = _tencent_kline(sym, days)
        if not klines:
            logger.warning(f"无法获取 {code} K线，使用缓存（{len(cached['prices'])} 条）")
            result[code] = cached["prices"]
            continue

        df = _klines_to_df(klines)
        new_prices = {
            row["date"].strftime("%Y-%m-%d"): {
                "close": float(row["close"]),
                "high":  float(row["high"]),
                "low":   float(row["low"]),
            }
            for _, row in df.iterrows()
        }

        # 价格合理性校验：若新旧数据最新价差距超过3倍，丢弃旧缓存（防止复权混用）
        if cached["prices"] and new_prices:
            old_latest = sorted(cached["prices"])[-1]
            new_latest = sorted(new_prices)[-1]
            if old_latest in new_prices:
                old_c = cached["prices"][old_latest]["close"]
                new_c = new_prices[old_latest]["close"]
                if old_c > 0 and (new_c / old_c > 3 or new_c / old_c < 0.33):
                    logger.warning(f"{code} 缓存价格({old_c:.2f})与新拉价格({new_c:.2f})差距超3倍，清除旧缓存")
                    cached["prices"] = {}

        merged = {**cached["prices"], **new_prices}
        _cache_save(code, {"updated": today, "prices": merged})
        result[code] = merged

    return result


def _trading_calendar(ohlc_map: dict[str, dict]) -> list[str]:
    """从所有 code 的价格数据中提取去重后升序排列的交易日列表。"""
    all_dates: set[str] = set()
    for prices in ohlc_map.values():
        all_dates.update(prices.keys())
    return sorted(all_dates)


def _future_date(calendar: list[str], signal_date: str, n: int) -> Optional[str]:
    """返回 signal_date 之后第 n 个交易日，若不在日历内则返回 None（pending）。"""
    # 找 signal_date 在 calendar 中的位置（或下一个最近交易日）
    idx: Optional[int] = None
    try:
        idx = calendar.index(signal_date)
    except ValueError:
        for i, d in enumerate(calendar):
            if d >= signal_date:
                idx = i
                break

    if idx is None:
        return None
    if idx + n < len(calendar):
        return calendar[idx + n]
    return None


# ─── 信号提取 ──────────────────────────────────────────────────────────────────

def extract_etf_signals(from_date: str, to_date: str) -> list[SignalRecord]:
    records: list[SignalRecord] = []

    for path in sorted(_LOG_DIR.glob("signal_detail_*.json")):
        raw = path.stem.replace("signal_detail_", "")          # YYYYMMDD
        date_fmt = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"           # YYYY-MM-DD
        if date_fmt < from_date or date_fmt > to_date:
            continue

        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"读取 {path.name} 失败: {e}")
            continue

        regime = d.get("regime", "")
        sentiment_level = (d.get("sentiment") or {}).get("level") or ""

        for sig in d.get("signals", []):
            records.append(SignalRecord(
                source="main",
                signal_date=date_fmt,
                code=str(sig.get("code", "")),
                name=sig.get("name", ""),
                signal=sig.get("signal", ""),
                score=sig.get("composite"),
                tier=sig.get("tier"),
                regime=regime or None,
                sentiment_level=sentiment_level or None,
                position_pct=sig.get("weight_pct"),
                stop_loss_pct=sig.get("stop_loss_pct"),
                note=sig.get("note"),
            ))

    logger.info(f"ETF 主信号: 提取 {len(records)} 条（{from_date} ~ {to_date}）")
    return records


def extract_stock_signals(from_date: str, to_date: str) -> list[SignalRecord]:
    records: list[SignalRecord] = []

    for path in sorted(_LOG_DIR.glob("stock_timing_*.json")):
        date_str = path.stem.replace("stock_timing_", "")       # YYYY-MM-DD
        if date_str < from_date or date_str > to_date:
            continue

        try:
            items = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(items, list):
                continue
        except Exception as e:
            logger.warning(f"读取 {path.name} 失败: {e}")
            continue

        for item in items:
            raw_code = str(item.get("code", ""))
            close_val = item.get("close")
            stop_val  = item.get("stop_price")
            stop_pct: Optional[float] = None
            if close_val and stop_val and close_val > 0:
                stop_pct = round((1 - stop_val / close_val) * 100, 2)

            reasons = item.get("reasons", [])
            records.append(SignalRecord(
                source="stock",
                signal_date=date_str,
                code=raw_code,
                name=item.get("name", ""),
                signal=item.get("signal", ""),
                score=item.get("score"),
                position_pct=item.get("position_pct"),
                stop_loss_pct=stop_pct,
                pool=item.get("pool"),
                signal_3d=item.get("signal_3d"),
                theme=item.get("theme"),
                cluster=item.get("cluster"),
                note="; ".join(reasons) if reasons else None,
                close_t=float(close_val) if close_val is not None else None,
            ))

    logger.info(f"个股择时: 提取 {len(records)} 条（{from_date} ~ {to_date}）")
    return records


def extract_tail_signals(from_date: str, to_date: str) -> list[SignalRecord]:
    records: list[SignalRecord] = []

    for path in sorted(_LOG_DIR.glob("tail_detail_*.json")):
        raw = path.stem.replace("tail_detail_", "")             # YYYYMMDD
        date_fmt = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
        if date_fmt < from_date or date_fmt > to_date:
            continue

        try:
            d = json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"读取 {path.name} 失败: {e}")
            continue

        panic = bool(d.get("market_panic", False))

        for sig in d.get("signals", []):
            stars = sig.get("stars", 0)
            triggers = sig.get("triggers", [])
            cautions = sig.get("cautions", [])
            records.append(SignalRecord(
                source="tail",
                signal_date=date_fmt,
                code=str(sig.get("code", "")),
                name=sig.get("name", ""),
                signal=f"TAIL_{stars}STAR",
                score=sig.get("score"),
                stars=stars,
                market_panic=panic,
                triggers="; ".join(triggers) if triggers else None,
                cautions="; ".join(cautions) if cautions else None,
                note=f"market_panic={panic}",
            ))

    logger.info(f"尾盘买点: 提取 {len(records)} 条（{from_date} ~ {to_date}）")
    return records


# ─── 后验收益计算 ──────────────────────────────────────────────────────────────

_HORIZONS = (1, 3, 5, 10)


def enrich_returns(
    records: list[SignalRecord],
    ohlc_map: dict[str, dict[str, dict]],
    csi300: dict[str, dict],
    calendar: list[str],
) -> None:
    """原地填写每条记录的后验收益字段。"""
    for rec in records:
        code_prices = ohlc_map.get(rec.code, {})

        # 信号当日收盘价（若 extract 阶段已填则保留，并与缓存交叉验证）
        if rec.close_t is None:
            t_data = code_prices.get(rec.signal_date)
            if t_data is None:
                rec.review_status = "missing_price"
                continue
            rec.close_t = t_data["close"]
        else:
            t_data = code_prices.get(rec.signal_date) or {"close": rec.close_t, "high": rec.close_t, "low": rec.close_t}
            cached_close = t_data.get("close") or rec.close_t
            if rec.close_t > 0 and cached_close > 0:
                ratio = cached_close / rec.close_t
                if ratio > 3 or ratio < 0.33:
                    logger.warning(f"价格不一致 {rec.code} {rec.signal_date}: 信号记录={rec.close_t:.2f} 缓存={cached_close:.2f}，标记 missing_price")
                    rec.review_status = "missing_price"
                    continue
                # 除权修正：偏差>20%时用前复权缓存价作为基准（避免未复权vs前复权导致假暴跌）
                if ratio < 0.8 or ratio > 1.2:
                    logger.info(f"除权修正 {rec.code} {rec.signal_date}: 信号价{rec.close_t:.2f}→缓存价{cached_close:.2f}")
                    rec.close_t = cached_close

        base = rec.close_t
        if base is None or base <= 0:
            rec.review_status = "missing_price"
            continue

        # 止损价（绝对价格，用于 hit_stop）
        stop_price: Optional[float] = None
        if rec.stop_loss_pct is not None:
            stop_price = base * (1 - rec.stop_loss_pct / 100)

        csi300_t = (csi300.get(rec.signal_date) or {}).get("close")

        all_complete = True
        for n in _HORIZONS:
            fd = _future_date(calendar, rec.signal_date, n)
            if fd is None:
                all_complete = False
                continue
            future = code_prices.get(fd)
            if future is None:
                all_complete = False
                continue
            close_n = future["close"]
            ret = (close_n / base) - 1
            setattr(rec, f"close_t{n}", round(close_n, 4))
            setattr(rec, f"ret_{n}d", round(ret, 6))

            csi300_n = (csi300.get(fd) or {}).get("close")
            if csi300_t and csi300_n:
                csi300_ret = (csi300_n / csi300_t) - 1
                setattr(rec, f"ret_csi300_{n}d", round(csi300_ret, 6))
                setattr(rec, f"excess_{n}d", round(ret - csi300_ret, 6))

        # 10 日内最大涨幅 / 最大回撤
        window_highs: list[float] = []
        window_lows:  list[float] = []
        for n in range(1, 11):
            fd = _future_date(calendar, rec.signal_date, n)
            if fd and fd in code_prices:
                window_highs.append(code_prices[fd]["high"])
                window_lows.append(code_prices[fd]["low"])

        if window_highs:
            rec.max_gain_10d     = round(max(window_highs) / base - 1, 6)
            rec.max_drawdown_10d = round(min(window_lows)  / base - 1, 6)

        if stop_price is not None and window_lows:
            rec.hit_stop_10d = any(lo <= stop_price for lo in window_lows)

        rec.review_status = "complete" if all_complete else "pending"

    complete = sum(1 for r in records if r.review_status == "complete")
    pending  = sum(1 for r in records if r.review_status == "pending")
    missing  = sum(1 for r in records if r.review_status == "missing_price")
    logger.info(f"  后验状态: complete={complete}, pending={pending}, missing_price={missing}")


# ─── 统计 ──────────────────────────────────────────────────────────────────────

def _win_rate(values: list[float]) -> Optional[float]:
    return round(sum(1 for v in values if v > 0) / len(values), 4) if values else None


def _avg(values: list[float]) -> Optional[float]:
    return round(sum(values) / len(values), 6) if values else None


def sample_label(n: int) -> str:
    if n < 10:
        return "极少(不评价)"
    if n < 30:
        return "初步观察"
    if n < 100:
        return "弱结论"
    return "较可靠"


def compute_stats(records: list[SignalRecord], group_field: str = "signal") -> list[dict]:
    groups: dict[str, list[SignalRecord]] = defaultdict(list)
    for rec in records:
        key = str(getattr(rec, group_field, None) or "unknown")
        groups[key].append(rec)

    rows = []
    for key in sorted(groups):
        grp = groups[key]
        # 使用有价格数据的记录（pending 只意味着 T+10 未到，T+1~T+5 可能已有）
        done = [r for r in grp if r.review_status != "missing_price"]

        def _vals(attr: str, _done: list = done) -> list[float]:
            return [v for r in _done if (v := getattr(r, attr)) is not None]

        full_done = sum(1 for r in grp if r.review_status == "complete")
        rows.append({
            "group":          key,
            "samples":        len(grp),
            "complete":       len(done),       # 有任意后验数据的记录数
            "full_complete":  full_done,        # T+10 全齐的记录数
            "sample_quality": sample_label(len(done)),
            "win_1d":         _win_rate(_vals("ret_1d")),
            "win_3d":         _win_rate(_vals("ret_3d")),
            "win_5d":         _win_rate(_vals("ret_5d")),
            "win_10d":        _win_rate(_vals("ret_10d")),
            "avg_1d":         _avg(_vals("ret_1d")),
            "avg_3d":         _avg(_vals("ret_3d")),
            "avg_5d":         _avg(_vals("ret_5d")),
            "avg_10d":        _avg(_vals("ret_10d")),
            "avg_excess_5d":  _avg(_vals("excess_5d")),
            "avg_excess_10d": _avg(_vals("excess_10d")),
            "max_gain":       max(_vals("ret_5d"), default=None),
            "max_loss":       min(_vals("ret_5d"), default=None),
        })
    return rows


# ─── 报告生成 ──────────────────────────────────────────────────────────────────

def _pct(v: Optional[float]) -> str:
    return f"{v * 100:+.2f}%" if v is not None else "—"


def _rate(v: Optional[float]) -> str:
    return f"{v * 100:.1f}%" if v is not None else "—"


def _stat_row(s: dict) -> str:
    return (
        f"| {s['group']} | {s['samples']} | {s['complete']} | {s['sample_quality']} "
        f"| {_rate(s['win_1d'])} | {_rate(s['win_3d'])} | {_rate(s['win_5d'])} "
        f"| {_pct(s['avg_5d'])} | {_pct(s['avg_excess_5d'])} |"
    )


def _diag_buy(etf_stats: list[dict]) -> list[str]:
    """生成买入信号诊断文本。"""
    lines: list[str] = []
    bs = next((s for s in etf_stats if s["group"] == "BUY_STRONG"), None)
    bw = next((s for s in etf_stats if s["group"] == "BUY_WATCH"), None)

    if bs:
        if bs["complete"] < 10:
            lines.append(f"🔍 BUY_STRONG 样本不足（完整={bs['complete']}），不能下结论")
        else:
            avg5, win5 = bs["avg_5d"], bs["win_5d"]
            if avg5 is not None and avg5 > 0 and win5 is not None and win5 >= 0.55:
                lines.append(f"✅ BUY_STRONG 有效：T+5 胜率 {_rate(win5)}，均收 {_pct(avg5)}")
            elif avg5 is not None and avg5 <= 0:
                lines.append(f"⚠️ BUY_STRONG 疑似失效：T+5 均收 {_pct(avg5)}（负收益）")
            else:
                lines.append(f"📊 BUY_STRONG 表现一般：T+5 胜率 {_rate(win5)}，均收 {_pct(avg5)}")
    else:
        lines.append("🔍 BUY_STRONG 本期无样本")

    if bs and bw and bs["complete"] >= 5 and bw["complete"] >= 5:
        diff = (bs["avg_5d"] or 0) - (bw["avg_5d"] or 0)
        if diff > 0.005:
            lines.append(f"✅ BUY_STRONG 明显优于 BUY_WATCH（T+5 差距 {_pct(diff)}）")
        else:
            lines.append(f"⚠️ BUY_STRONG 未明显优于 BUY_WATCH（T+5 差距 {_pct(diff)}），建议检查分级逻辑")

    return lines


def _diag_sell(etf_stats: list[dict]) -> list[str]:
    lines: list[str] = []

    reduce = next((s for s in etf_stats if s["group"] == "REDUCE"), None)
    if reduce:
        if reduce["complete"] < 5:
            lines.append(f"🔍 REDUCE 样本不足（有后验={reduce['complete']}），不能下结论")
        elif reduce.get("avg_5d") is not None:
            avg5 = reduce["avg_5d"]
            if avg5 < -0.005:
                lines.append(f"✅ REDUCE 有效：T+5 均收 {_pct(avg5)}（减仓后继续下跌）")
            elif avg5 > 0.01:
                lines.append(f"⚠️ REDUCE 疑似过严：T+5 均收 {_pct(avg5)}（减仓后反弹，可能减仓过早）")
            else:
                lines.append(f"📊 REDUCE 中性：T+5 均收 {_pct(avg5)}（接近零，减仓效果不明显）")
        else:
            lines.append(f"🔍 REDUCE 有记录但 T+5 尚未到期，暂无结论")
    else:
        lines.append("🔍 REDUCE 本期无样本")

    ss = next((s for s in etf_stats if s["group"] == "SELL_STOP"), None)
    if ss:
        if ss["complete"] < 5:
            lines.append(f"🔍 SELL_STOP 样本不足（有后验={ss['complete']}），不能下结论")
        elif ss.get("avg_5d") is not None:
            avg5 = ss["avg_5d"]
            if avg5 > 0.01:
                lines.append(f"⚠️ SELL_STOP 卖飞风险：T+5 均收 {_pct(avg5)}（止损后快速反弹）")
            elif avg5 < -0.005:
                lines.append(f"✅ SELL_STOP 有效：T+5 均收 {_pct(avg5)}（止损后继续下跌）")
            else:
                lines.append(f"📊 SELL_STOP 中性：T+5 均收 {_pct(avg5)}（接近零）")
        else:
            lines.append(f"🔍 SELL_STOP 有记录但 T+5 尚未到期")
    else:
        lines.append("🔍 SELL_STOP 本期无样本")

    return lines


def _diag_avoid(etf_stats: list[dict]) -> list[str]:
    lines: list[str] = []
    avoid = next((s for s in etf_stats if s["group"] == "AVOID"), None)
    buy_stats = [s for s in etf_stats if s["group"] in ("BUY_STRONG", "BUY_WATCH") and s.get("avg_5d") is not None]

    if avoid and avoid["complete"] >= 5:
        avoid_5d = avoid["avg_5d"]
        if avoid_5d is None:
            lines.append(f"🔍 AVOID {avoid['complete']} 条有记录但 T+5 尚未到期")
        elif buy_stats:
            best_buy = max(s["avg_5d"] for s in buy_stats)
            if (avoid_5d or 0) > best_buy:
                lines.append(f"⚠️ AVOID 疑似过严：AVOID 后均收 {_pct(avoid_5d)} 优于买入信号（最高 {_pct(best_buy)}），考虑放松过滤条件")
            else:
                lines.append(f"✅ AVOID 过滤有效：AVOID 后均收 {_pct(avoid_5d)} 低于买入信号（最高 {_pct(best_buy)}）")
        else:
            # No buy signals this period for comparison
            if avoid_5d is not None and avoid_5d > 0.01:
                lines.append(f"⚠️ AVOID 后均收 {_pct(avoid_5d)}（正收益）——本期无买入信号，无法比较，但需关注是否错过机会")
            else:
                lines.append(f"📊 AVOID 后均收 {_pct(avoid_5d)}——本期无买入信号可对比")
    elif avoid:
        lines.append(f"🔍 AVOID 有后验={avoid['complete']} 条，样本不足，不能下结论")
    else:
        lines.append("🔍 AVOID 本期无样本")

    return lines


def generate_etf_section(records: list[SignalRecord], etf_stats: list[dict], month_tag: str) -> str:
    lines: list[str] = []

    total    = len(records)
    complete = sum(1 for r in records if r.review_status == "complete")
    pending  = sum(1 for r in records if r.review_status == "pending")
    missing  = sum(1 for r in records if r.review_status == "missing_price")

    lines.append(f"### ETF 主信号（{month_tag}）\n")
    lines.append(f"- 样本数：{total} | 完整：{complete} | 待观察：{pending} | 缺失行情：{missing}\n")

    # 买入信号
    lines.append("#### 买入信号表现\n")
    buy_stats = [s for s in etf_stats if s["group"] in ("BUY_STRONG", "BUY_WATCH")]
    if buy_stats:
        lines.append("| 信号 | 样本 | 完整 | 质量 | 胜率T+1 | 胜率T+3 | 胜率T+5 | 均收T+5 | 超额T+5 |")
        lines.append("|------|------|------|------|---------|---------|---------|---------|---------|")
        for s in buy_stats:
            lines.append(_stat_row(s))
    else:
        lines.append("*本期无买入信号样本*")
    lines.append("")

    # 减仓/止损
    lines.append("#### 减仓/止损信号表现\n")
    sell_stats = [s for s in etf_stats if s["group"] in ("REDUCE", "SELL_STOP")]
    if sell_stats:
        lines.append("| 信号 | 样本 | 完整 | 质量 | 续跌比例T+5 | 均收T+5 | 均收T+10 |")
        lines.append("|------|------|------|------|-------------|---------|----------|")
        for s in sell_stats:
            down_5d = (1 - s["win_5d"]) if s["win_5d"] is not None else None
            lines.append(
                f"| {s['group']} | {s['samples']} | {s['complete']} | {s['sample_quality']} "
                f"| {_rate(down_5d)} | {_pct(s['avg_5d'])} | {_pct(s['avg_10d'])} |"
            )
    else:
        lines.append("*本期无减仓/止损样本*")
    lines.append("")

    # HOLD / AVOID
    lines.append("#### HOLD / AVOID 信号表现\n")
    other_stats = [s for s in etf_stats if s["group"] in ("HOLD", "AVOID")]
    if other_stats:
        lines.append("| 信号 | 样本 | 完整 | 质量 | 均收T+5 | 超额T+5 |")
        lines.append("|------|------|------|------|---------|---------|")
        for s in other_stats:
            lines.append(
                f"| {s['group']} | {s['samples']} | {s['complete']} | {s['sample_quality']} "
                f"| {_pct(s['avg_5d'])} | {_pct(s['avg_excess_5d'])} |"
            )
    else:
        lines.append("*本期无 HOLD/AVOID 样本*")
    lines.append("")

    return "\n".join(lines)


def generate_stock_section(records: list[SignalRecord], month_tag: str) -> str:
    lines: list[str] = []

    by_signal  = compute_stats(records, "signal")
    by_theme   = compute_stats(records, "theme")
    by_3d      = compute_stats(records, "signal_3d")

    lines.append(f"### 个股择时（{month_tag}）\n")
    total    = len(records)
    complete = sum(1 for r in records if r.review_status == "complete")
    lines.append(f"- 样本数：{total} | 完整：{complete}\n")

    def _table(stats: list[dict], title: str) -> None:
        lines.append(f"#### {title}\n")
        if stats:
            lines.append("| 分组 | 样本 | 完整 | 质量 | 胜率T+1 | 胜率T+3 | 胜率T+5 | 均收T+5 | 超额T+5 |")
            lines.append("|------|------|------|------|---------|---------|---------|---------|---------|")
            for s in stats:
                lines.append(_stat_row(s))
        else:
            lines.append("*无数据*")
        lines.append("")

    _table(by_signal, "按信号类型")
    _table(by_3d,     "按 signal_3d 三维评级")
    _table(by_theme,  "按主题")

    return "\n".join(lines)


def generate_tail_section(records: list[SignalRecord], month_tag: str) -> str:
    lines: list[str] = []

    by_stars  = compute_stats(records, "stars")

    lines.append(f"### 尾盘买点（{month_tag}）\n")
    total    = len(records)
    complete = sum(1 for r in records if r.review_status == "complete")
    lines.append(f"- 样本数：{total} | 完整：{complete}\n")

    lines.append("#### 按星级\n")
    if by_stars:
        lines.append("| 星级 | 样本 | 完整 | 质量 | 胜率T+3 | 胜率T+5 | 均收T+5 | 最大回撤10d均值 |")
        lines.append("|------|------|------|------|---------|---------|---------|----------------|")
        for s in by_stars:
            done = [r for r in records if str(r.stars) == str(s["group"]) and r.review_status == "complete"]
            avg_dd = _avg([r.max_drawdown_10d for r in done if r.max_drawdown_10d is not None])
            lines.append(
                f"| {s['group']}★ | {s['samples']} | {s['complete']} | {s['sample_quality']} "
                f"| {_rate(s['win_3d'])} | {_rate(s['win_5d'])} | {_pct(s['avg_5d'])} | {_pct(avg_dd)} |"
            )
    else:
        lines.append("*无数据*")
    lines.append("")

    # 恐慌日 vs 普通日
    panic_recs  = [r for r in records if r.market_panic is True]
    normal_recs = [r for r in records if r.market_panic is False]
    if panic_recs or normal_recs:
        lines.append("#### 恐慌日 vs 普通日\n")
        for label, group in [("恐慌日", panic_recs), ("普通日", normal_recs)]:
            done = [r for r in group if r.review_status == "complete"]
            ret5 = [r.ret_5d for r in done if r.ret_5d is not None]
            lines.append(f"- **{label}**：样本={len(group)}, 完整={len(done)}, 均收T+5={_pct(_avg(ret5))}")
        lines.append("")

    return "\n".join(lines)


def generate_report(
    etf_records: list[SignalRecord],
    stock_records: list[SignalRecord],
    tail_records: list[SignalRecord],
    etf_stats: list[dict],
    month_tag: str,
) -> str:
    lines: list[str] = []
    year  = month_tag[:4]
    month = month_tag[4:6]
    lines.append(f"# {year}-{month} 信号后验复盘\n")
    lines.append(f"生成时间：{datetime.today().strftime('%Y-%m-%d %H:%M')}\n")

    # 总览
    all_recs = etf_records + stock_records + tail_records
    lines.append("## 总览\n")
    lines.append(f"| 来源 | 样本数 | 有后验数据 | T+10完整 | 缺失行情 |")
    lines.append(f"|------|--------|-----------|----------|----------|")

    for label, recs in [("ETF 主信号", etf_records), ("个股择时", stock_records), ("尾盘买点", tail_records)]:
        has_data = sum(1 for r in recs if r.review_status != "missing_price")
        full = sum(1 for r in recs if r.review_status == "complete")
        m = sum(1 for r in recs if r.review_status == "missing_price")
        lines.append(f"| {label} | {len(recs)} | {has_data} | {full} | {m} |")

    has_data = sum(1 for r in all_recs if r.review_status != "missing_price")
    full = sum(1 for r in all_recs if r.review_status == "complete")
    m = sum(1 for r in all_recs if r.review_status == "missing_price")
    lines.append(f"| **合计** | **{len(all_recs)}** | **{has_data}** | **{full}** | **{m}** |")
    lines.append("")

    # 分模块
    lines.append("## 详细表现\n")
    if etf_records:
        lines.append(generate_etf_section(etf_records, etf_stats, f"{year}-{month}"))
    if stock_records:
        lines.append(generate_stock_section(stock_records, f"{year}-{month}"))
    if tail_records:
        lines.append(generate_tail_section(tail_records, f"{year}-{month}"))

    # 规则诊断（仅 ETF 部分，样本足够后生效）
    if etf_records:
        lines.append("## 规则诊断\n")
        diags = _diag_buy(etf_stats) + _diag_sell(etf_stats) + _diag_avoid(etf_stats)
        if not diags:
            diags = ["*样本量不足，本期无法生成诊断结论*"]
        for d in diags:
            lines.append(f"- {d}")
        lines.append("")

    # 核心问题
    lines.append("## 本月核心问题\n")
    bs = next((s for s in etf_stats if s["group"] == "BUY_STRONG"), None)
    bw = next((s for s in etf_stats if s["group"] == "BUY_WATCH"), None)
    reduce = next((s for s in etf_stats if s["group"] == "REDUCE"), None)
    ss = next((s for s in etf_stats if s["group"] == "SELL_STOP"), None)

    lines.append(
        "1. **BUY_STRONG 是否值得执行？** " +
        (f"是（T+5 胜率 {_rate(bs['win_5d'])}，均收 {_pct(bs['avg_5d'])}）" if bs and bs["complete"] >= 5 and (bs.get("avg_5d") or 0) > 0
         else "待观察（样本不足或负收益）")
    )
    lines.append(
        "2. **BUY_WATCH 是否只是噪音？** " +
        (f"否（T+5 均收 {_pct(bw['avg_5d'])}）" if bw and bw["complete"] >= 5 and (bw.get("avg_5d") or 0) > 0
         else "待观察（样本不足）")
    )
    lines.append(
        "3. **REDUCE 是否真的减少亏损？** " +
        (f"{'是' if (reduce.get('avg_5d') or 0) < 0 else '待验证'}（T+5 均收 {_pct(reduce.get('avg_5d'))}）" if reduce and reduce["complete"] >= 5
         else "待观察（样本不足）")
    )
    lines.append(
        "4. **SELL_STOP 是否经常卖飞？** " +
        (f"{'是（后续反弹）' if (ss.get('avg_5d') or 0) > 0.01 else '否（止损后续跌）'}（T+5 均收 {_pct(ss.get('avg_5d'))}）" if ss and ss["complete"] >= 5
         else "待观察（样本不足）")
    )
    lines.append("5. **AVOID 是否过滤掉太多机会？** 见规则诊断")
    lines.append("6. **哪个主题/cluster 信号最有效？** 见个股择时章节（Phase 2）")
    lines.append("7. **下月调整建议：** 样本持续积累中，建议 1~2 个月后再调整权重")
    lines.append("")

    return "\n".join(lines)


# ─── CSV 保存 ──────────────────────────────────────────────────────────────────

def _save_csv(records: list[SignalRecord], path: Path) -> None:
    if not records:
        return
    _REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    field_names = [f.name for f in dc_fields(SignalRecord)]
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=field_names)
        w.writeheader()
        for rec in records:
            w.writerow(asdict(rec))
    logger.info(f"CSV: {path} （{len(records)} 条）")


# ─── 主入口 ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="信号后验复盘")
    parser.add_argument("--from", dest="from_date", default=None, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--to",   dest="to_date",   default=None, help="结束日期 YYYY-MM-DD")
    parser.add_argument(
        "--type", dest="signal_type", default="etf",
        choices=["etf", "stock", "tail", "all"],
        help="信号类型（默认 etf）",
    )
    args = parser.parse_args()

    today     = datetime.today().strftime("%Y-%m-%d")
    from_date = args.from_date or "2026-01-01"
    to_date   = args.to_date   or today
    month_tag = from_date.replace("-", "")[:6]   # YYYYMM

    _REVIEW_DIR.mkdir(parents=True, exist_ok=True)

    do_etf   = args.signal_type in ("etf", "all")
    do_stock = args.signal_type in ("stock", "all")
    do_tail  = args.signal_type in ("tail", "all")

    etf_records:   list[SignalRecord] = []
    stock_records: list[SignalRecord] = []
    tail_records:  list[SignalRecord] = []
    etf_stats:     list[dict]        = []

    # ── ETF 主信号 ──────────────────────────────────────────────────────────────
    if do_etf:
        logger.info("=== Phase 1: ETF 主信号复盘 ===")
        etf_records = extract_etf_signals(from_date, to_date)
        if etf_records:
            codes = list({r.code for r in etf_records if r.code})
            logger.info(f"拉取价格: {len(codes)} 个 ETF + CSI300...")
            ohlc = fetch_ohlc(codes + ["000300"])
            cal  = _trading_calendar(ohlc)
            enrich_returns(etf_records, ohlc, ohlc.get("000300", {}), cal)
            _save_csv(etf_records, _REVIEW_DIR / f"signals_etf_{month_tag}.csv")
            etf_stats = compute_stats(etf_records, "signal")

    # ── 个股择时 ────────────────────────────────────────────────────────────────
    if do_stock:
        logger.info("=== Phase 2: 个股择时复盘 ===")
        stock_records = extract_stock_signals(from_date, to_date)
        if stock_records:
            codes = list({r.code for r in stock_records if r.code})
            logger.info(f"拉取价格: {len(codes)} 只个股 + CSI300...")
            ohlc  = fetch_ohlc(codes + ["000300"])
            cal   = _trading_calendar(ohlc)
            enrich_returns(stock_records, ohlc, ohlc.get("000300", {}), cal)
            _save_csv(stock_records, _REVIEW_DIR / f"signals_stock_{month_tag}.csv")

    # ── 尾盘买点 ────────────────────────────────────────────────────────────────
    if do_tail:
        logger.info("=== Phase 3: 尾盘买点复盘 ===")
        tail_records = extract_tail_signals(from_date, to_date)
        if tail_records:
            codes = list({r.code for r in tail_records if r.code})
            logger.info(f"拉取价格: {len(codes)} 个 ETF + CSI300...")
            ohlc  = fetch_ohlc(codes + ["000300"])
            cal   = _trading_calendar(ohlc)
            enrich_returns(tail_records, ohlc, ohlc.get("000300", {}), cal)
            _save_csv(tail_records, _REVIEW_DIR / f"signals_tail_{month_tag}.csv")

    # ── 生成汇总报告 ─────────────────────────────────────────────────────────────
    if etf_records or stock_records or tail_records:
        report = generate_report(etf_records, stock_records, tail_records, etf_stats, month_tag)

        report_file = _REVIEW_DIR / f"review_report_{month_tag}.md"
        report_file.write_text(report, encoding="utf-8")
        logger.info(f"报告: {report_file}")

        summary = {
            "generated":    today,
            "from_date":    from_date,
            "to_date":      to_date,
            "etf_stats":    etf_stats,
            "stock_stats":  compute_stats(stock_records, "signal") if stock_records else [],
            "tail_stats":   compute_stats(tail_records, "stars")   if tail_records  else [],
        }
        (_REVIEW_DIR / f"review_summary_{month_tag}.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        print(report)
    else:
        logger.warning("未找到任何信号，请检查日期范围和日志目录")


if __name__ == "__main__":
    main()
