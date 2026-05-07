#!/usr/bin/env python3
"""
每日08:50 自动生成操作指导 + 推送飞书
策略框架：V4.0 + 用户策略（业绩景气度 + 龙头驱动 + 银行券商节奏信号）
"""
import json
import re
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from typing import TypeVar, Callable

import requests

from config import FEISHU_WEBHOOK, FEISHU_WEBHOOKS, REGIME_UP_CONFIRM, REGIME_DOWN_CONFIRM, DEFENSIVE_ROTATION_POOL, ETF_UNIVERSE, STOCK_UNIVERSE
from rotation_advisor import calc_rotation_signal, DIM_EMOJI

T = TypeVar("T")


def _retry(fn: Callable[[], T], attempts: int = 3, delays: tuple = (2, 5)) -> T:
    last_exc: Exception = RuntimeError("unknown")
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if i < len(delays):
                print(f"  [重试] 第{i+1}次失败({e})，{delays[i]}s后重试...")
                time.sleep(delays[i])
    raise last_exc

# ─── 交易日历（简化判断：周一至周五，节假日人工维护排除）─────────────────────
HOLIDAY_BLACKLIST = {
    # 2026年主要节假日（需每年更新）
    "2026-01-01", "2026-01-02",
    "2026-02-17", "2026-02-18", "2026-02-19", "2026-02-20",
    "2026-02-23", "2026-02-24",
    "2026-04-03", "2026-04-04", "2026-04-05", "2026-04-06",
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    "2026-06-19", "2026-06-20", "2026-06-21",
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
    "2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08",
}


def is_trading_day() -> bool:
    today = date.today()
    if today.weekday() >= 5:
        return False
    if today.isoformat() in HOLIDAY_BLACKLIST:
        return False
    return True


# ─── 外部宏观快速抓取 ─────────────────────────────────────────────────────────

def fetch_us_indices() -> dict:
    """Tencent Finance获取美股三大指数"""
    def _fetch():
        # 腾讯实时行情：usDJI=道琼斯, usNDX=纳指100, usINX=标普500
        codes = "usDJI,usNDX,usINX"
        url = f"https://qt.gtimg.cn/q={codes}"
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        results = {}
        for line in r.text.strip().split("\n"):
            # 格式: v_usDJI="200~道琼斯~.DJI~price~prev~...~pct~..."
            if "=" not in line:
                continue
            val = line.split("=", 1)[1].strip().strip('"').strip(";")
            parts = val.split("~")
            if len(parts) > 32 and parts[0] == "200":
                name = parts[1]
                pct_raw = parts[32]
                results[name] = float(pct_raw) if pct_raw else 0.0
        if not results:
            raise ValueError("美股数据为空")
        return results
    try:
        return _retry(_fetch)
    except Exception as e:
        print(f"  [警告] 美股数据获取失败: {e}")
        return {}


def fetch_hk_indices() -> dict:
    """港股恒生指数"""
    def _fetch():
        r = requests.get("https://qt.gtimg.cn/q=hkHSI", timeout=10)
        r.raise_for_status()
        parts = r.text.split("~")
        if len(parts) > 32:
            return {"恒生指数": float(parts[32])}
        raise ValueError("恒生数据为空")
    try:
        return _retry(_fetch)
    except Exception as e:
        print(f"  [警告] 港股数据获取失败: {e}")
        return {}


def fetch_gold_oil() -> dict:
    """BTC/ETH 加密货币行情（CoinGecko）"""
    def _fetch():
        r = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "bitcoin,ethereum", "vs_currencies": "usd", "include_24hr_change": "true"},
            timeout=12,
        )
        r.raise_for_status()
        data = r.json()
        results = {}
        if "bitcoin" in data:
            results["BTC"] = {"price": data["bitcoin"]["usd"], "pct": data["bitcoin"].get("usd_24h_change", 0)}
        if "ethereum" in data:
            results["ETH"] = {"price": data["ethereum"]["usd"], "pct": data["ethereum"].get("usd_24h_change", 0)}
        if not results:
            raise ValueError("加密货币数据为空")
        return results
    try:
        return _retry(_fetch, attempts=3, delays=(3, 8))
    except Exception as e:
        print(f"  [警告] 加密货币数据获取失败: {e}")
        return {}


def fetch_bank_broker_signal() -> dict:
    """实时抓 银行/券商/科技ETF 涨跌幅，返回节奏结论"""
    default = {"bank_pct": None, "broker_pct": None, "tech_pct": None,
               "conclusion": "数据获取失败，请手动判断", "detail": "", "signal": "UNKNOWN"}
    try:
        r = requests.get("https://qt.gtimg.cn/q=sh512800,sh512880,sz159995", timeout=8)
        r.raise_for_status()
        pcts: dict[str, float] = {}
        for line in r.text.strip().split("\n"):
            if "=" not in line:
                continue
            code = line.split("=")[0].replace("v_sh", "").replace("v_sz", "")
            parts = line.split("=", 1)[1].strip().strip('"').strip(";").split("~")
            if len(parts) > 32 and parts[32]:
                pcts[code] = float(parts[32])
        bank_pct = pcts.get("512800")
        broker_pct = pcts.get("512880")
        tech_pct = pcts.get("159995")
        if bank_pct is None or broker_pct is None or tech_pct is None:
            return default
        bank_strong = bank_pct >= 0.3
        tech_strong = tech_pct >= 0.5
        broker_strong = broker_pct >= 0.3
        if bank_strong and broker_strong:
            conclusion, signal = "银行强+券商强 → 全面护盘/做多，跟随主线板块", "STRONG"
        elif bank_strong and not broker_strong:
            conclusion, signal = "银行强+券商弱 → 护盘行情，今日谨慎，不追高", "CAUTION"
        elif not bank_strong and tech_strong:
            conclusion, signal = "银行弱+科技强 → 增量资金入场，可以交易", "ACTIVE"
        elif not bank_strong and not broker_strong and not tech_strong:
            conclusion, signal = "银行弱+全线弱 → 观望，今日不操作", "WAIT"
        else:
            conclusion, signal = "混合信号，谨慎观察，等方向明确再动", "MIXED"
        detail = f"银行ETF(512800) {bank_pct:+.2f}% | 券商ETF(512880) {broker_pct:+.2f}% | 芯片ETF(159995) {tech_pct:+.2f}%"
        return {"bank_pct": bank_pct, "broker_pct": broker_pct, "tech_pct": tech_pct,
                "conclusion": conclusion, "detail": detail, "signal": signal}
    except Exception as e:
        print(f"  [警告] 银行/券商信号获取失败: {e}")
        return default


def fetch_north_flow_summary() -> dict:
    """
    读取北向资金近况（优先调 data_fetcher 刷新今日 Tier-2 净买入，失败则降级读缓存）。
    返回包含 text（飞书 lark_md）、signal、summary 的字典。
    """
    df = None
    try:
        from data_fetcher import get_north_flow
        df = get_north_flow(days=10)
    except Exception as e:
        print(f"  [警告] get_north_flow失败({e})，降级读缓存")

    if df is None:
        cache_path = Path(__file__).parent / "state" / "north_flow_cache.json"
        try:
            import pandas as pd
            records = json.loads(cache_path.read_text(encoding="utf-8"))
            df = pd.DataFrame(records)
            df["date"] = pd.to_datetime(df["date"])
            for col in ("deal_amt_billion", "net_buy_billion"):
                if col not in df.columns:
                    df[col] = None
            df = df.sort_values("date").tail(10).reset_index(drop=True)
        except Exception as e2:
            print(f"  [警告] 北向缓存读取失败: {e2}")
            return {"text": "北向资金：数据获取失败", "signal": "UNKNOWN",
                    "summary": "数据获取失败", "today_net": None, "deal_today": None}

    recent = df.tail(5)

    # 今日净买入（精确，Tier-2 自动或 Tier-3 手动）
    today_net: float | None = None
    net_5d: float | None = None
    if "net_buy_billion" in recent.columns:
        net_series = recent["net_buy_billion"].dropna()
        if len(net_series) > 0:
            today_net = round(float(net_series.iloc[-1]), 2)
            net_5d = round(float(net_series.sum()), 2)

    # 成交总额（活跃度代理，Tier-1 自动）
    deal_today: float | None = None
    deal_avg_5d: float | None = None
    deal_trend_pct: float | None = None
    if "deal_amt_billion" in recent.columns:
        deal_series = recent["deal_amt_billion"].dropna()
        if len(deal_series) >= 1:
            deal_today = round(float(deal_series.iloc[-1]), 2)
        if len(deal_series) >= 3:
            deal_avg_5d = round(float(deal_series.mean()), 2)
            if deal_today and deal_avg_5d > 0:
                deal_trend_pct = round((deal_today / deal_avg_5d - 1) * 100, 1)

    # 信号评估（优先净买入方向，降级为活跃度）
    if today_net is not None:
        if today_net >= 20:
            signal, emoji = "STRONG_IN", "🟢"
            conclusion = f"大幅净流入 +{today_net:.2f}亿，外资积极做多"
        elif today_net >= 5:
            signal, emoji = "IN", "🟢"
            conclusion = f"净流入 +{today_net:.2f}亿，外资偏多"
        elif today_net >= -5:
            signal, emoji = "NEUTRAL", "⚪"
            conclusion = f"小幅波动 {today_net:+.2f}亿，方向中性"
        elif today_net >= -20:
            signal, emoji = "OUT", "🟡"
            conclusion = f"净流出 {today_net:.2f}亿，外资偏空"
        else:
            signal, emoji = "STRONG_OUT", "🔴"
            conclusion = f"大幅净流出 {today_net:.2f}亿，外资撤退"
    elif deal_today is not None:
        signal, emoji = "ACTIVITY_ONLY", "🔵"
        avg_ref = f"较5日均 {deal_avg_5d:.1f}亿" if deal_avg_5d else ""
        conclusion = f"成交总额 {deal_today:.1f}亿（{avg_ref}），净方向待精确数据"
    else:
        signal, emoji = "UNKNOWN", "❓"
        conclusion = "数据不足，无法评估"

    # 组装飞书 lark_md 文本
    net_days = len(recent["net_buy_billion"].dropna()) if "net_buy_billion" in recent.columns else 0
    lines: list[str] = []
    if today_net is not None:
        net_5d_str = f" | {net_days}日累计 **{net_5d:+.2f}亿**" if net_5d is not None and net_days > 1 else ""
        lines.append(f"今日净买入 **{today_net:+.2f}亿**（实时）{net_5d_str}")
    if deal_today is not None:
        trend_str = ""
        if deal_trend_pct is not None:
            arrow = "↑" if deal_trend_pct > 0 else "↓"
            trend_str = f"（{arrow}{abs(deal_trend_pct):.0f}%较均值）"
        avg_str = f" | 5日均 **{deal_avg_5d:.1f}亿**" if deal_avg_5d else ""
        lines.append(f"成交总额 **{deal_today:.1f}亿**{trend_str}{avg_str}")
    if not lines:
        lines.append("精确净买入：待 Tier-2 自动刷新")
    lines.append(f"{emoji} **{conclusion}**")

    return {
        "today_net": today_net, "net_5d": net_5d,
        "deal_today": deal_today, "deal_avg_5d": deal_avg_5d, "deal_trend_pct": deal_trend_pct,
        "signal": signal, "emoji": emoji, "summary": conclusion,
        "text": "\n".join(lines),
    }


def fetch_policy_sector_avg_chg() -> float | None:
    """
    政策核心板块平均当日涨跌幅（政策面代理指标）。
    抓取：电网设备ETF(159326) / 半导体ETF(512480) / 光通信ETF(515880)
    """
    codes = {"电网设备": "sz159326", "半导体": "sh512480", "光通信": "sh515880"}
    chgs: list[float] = []
    for name, sym in codes.items():
        url = (
            f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
            f"?_var=kline_dayqfq&param={sym},day,,,5,qfq"
        )
        try:
            r = requests.get(url, timeout=8,
                             headers={"User-Agent": "Mozilla/5.0"})
            r.raise_for_status()
            raw  = r.text.replace("kline_dayqfq=", "")
            data = json.loads(raw)
            inner = data.get("data", {}).get(sym, {})
            klines = inner.get("day") or inner.get("qfqday") or []
            if len(klines) >= 2:
                prev  = float(klines[-2][2])
                close = float(klines[-1][2])
                chgs.append((close / prev - 1) * 100)
        except Exception as e:
            print(f"  [警告] 政策板块{name}数据失败: {e}")
    return round(sum(chgs) / len(chgs), 2) if chgs else None


def fetch_a_share_futures() -> dict:
    """沪深300期货（通过腾讯实时行情，09:30后才有数据）"""
    try:
        # IF主力合约腾讯代码：IF主力=jIF0001（开盘后才有数据）
        r = requests.get("https://qt.gtimg.cn/q=jIF0001", timeout=8)
        val = r.text.split("=", 1)[-1].strip().strip('"').strip(";")
        parts = val.split("~")
        if len(parts) > 32 and parts[0] == "200":
            return {"IF主力": float(parts[32])}
    except Exception:
        pass
    return {}  # 开盘前无数据属正常，静默降级


# ─── 实时ETF扫描（09:50后，每小时运行一次）───────────────────────────────────

def _intraday_vol_factor() -> float:
    h, m = datetime.now().hour, datetime.now().minute
    if h < 9 or (h == 9 and m < 30):
        return 0.0
    if h == 9:
        return 0.25
    if h == 10:
        return 0.40
    if h in (11, 12):
        return 0.50
    if h == 13:
        return 0.65
    return 0.80


def fetch_realtime_scan() -> "tuple[object | None, dict, dict]":
    """
    实时跑 ETF 扫描器，返回 (TailMarketReading | None, etf_prices, flow_detail)。
    flow_detail: {code: {today, yesterday, delta, history}}
    市场未开时（08:50）返回 (None, {}, {})。
    """
    vol_factor = _intraday_vol_factor()
    if vol_factor < 0.1:
        return None, {}, {}
    try:
        import data_fetcher as df_api
        import tail_market_scanner as scanner
        codes = list(ETF_UNIVERSE.keys())
        etf_prices = df_api.get_etf_prices(codes, days=65)
        flow_detail = df_api.get_etf_main_force_flow_detail(codes)
        flows_simple = {k: v["today"] for k, v in flow_detail.items()}
        reading = scanner.scan_tail_market(
            ETF_UNIVERSE, etf_prices,
            main_force_flows=flows_simple,
            vol_factor=vol_factor,
        )
        return reading, etf_prices, flow_detail
    except Exception as e:
        print(f"  [警告] 实时ETF扫描失败: {e}")
        return None, {}, {}


def fetch_position_alerts(etf_prices: dict) -> list:
    """
    检查 positions.json 中的持仓，返回止损/减仓预警列表。
    cost==0 的条目视为空仓，跳过。
    """
    state_path = Path(__file__).parent / "state" / "positions.json"
    try:
        raw = json.loads(state_path.read_text())
        positions = {k: float(v) for k, v in raw.items() if float(v) > 0}
    except Exception:
        return []

    all_names = {k: v.get("name", k) for k, v in {**ETF_UNIVERSE, **STOCK_UNIVERSE}.items()}
    alerts = []

    for code, cost in positions.items():
        df = etf_prices.get(code)
        if df is None or len(df) < 6:
            continue
        close = df["close"]
        current = float(close.iloc[-1])
        prev_close = float(close.iloc[-2])
        today_chg = (current / prev_close - 1) * 100
        loss_pct = (current / cost - 1) * 100
        ma5 = float(close.tail(5).mean())
        ma20 = float(close.tail(20).mean()) if len(df) >= 20 else ma5
        ma5_dev = (current / ma5 - 1) * 100

        # RSI(6) 简算
        delta = close.diff().dropna()
        avg_g = delta.clip(lower=0).tail(6).mean()
        avg_l = (-delta.clip(upper=0)).tail(6).mean()
        rsi = 100.0 if avg_l == 0 else round(100 - 100 / (1 + avg_g / avg_l), 1)

        if loss_pct <= -8.5:
            alert_type = "STOP_LOSS"
        elif loss_pct <= -5.0 and today_chg <= -1.5:
            alert_type = "WARN_LOSS"
        elif ma5_dev >= 4.5 and rsi >= 76:
            alert_type = "REDUCE"
        else:
            continue

        alerts.append({
            "code": code,
            "name": all_names.get(code, code),
            "cost": cost,
            "current": current,
            "today_chg": today_chg,
            "loss_pct": loss_pct,
            "ma5_dev": ma5_dev,
            "ma20_dev": (current / ma20 - 1) * 100 if ma20 > 0 else 0.0,
            "rsi": rsi,
            "alert_type": alert_type,
            "stop_price": round(cost * 0.915, 3),
        })
    return alerts


def _format_realtime_sections(reading, position_alerts: list) -> list:
    """
    生成实时操作信号的飞书 card sections。
    优先级：止损 > 买点 > 无信号。
    """
    def md(content: str) -> dict:
        return {"tag": "div", "text": {"tag": "lark_md", "content": content}}

    sections: list = []

    # ── 持仓预警（最高优先级）──────────────────────────────────────────────
    if position_alerts:
        lines = []
        for a in position_alerts:
            if a["alert_type"] == "STOP_LOSS":
                lines.append(
                    f"🔴 **{a['name']}**（{a['code']}）**止损警报！**\n"
                    f"    成本 {a['cost']:.3f} → 现价 **{a['current']:.3f}**"
                    f"（亏损 **{a['loss_pct']:+.1f}%**）今日{a['today_chg']:+.1f}%\n"
                    f"    → 建议：收盘前清仓，止损线 **{a['stop_price']:.3f}**"
                )
            elif a["alert_type"] == "WARN_LOSS":
                lines.append(
                    f"🟠 **{a['name']}**（{a['code']}）亏损预警\n"
                    f"    成本 {a['cost']:.3f} → 现价 {a['current']:.3f}"
                    f"（{a['loss_pct']:+.1f}%）今日{a['today_chg']:+.1f}%\n"
                    f"    → 若再跌破 **{a['stop_price']:.3f}** 立即止损"
                )
            else:
                lines.append(
                    f"🟡 **{a['name']}**（{a['code']}）高位减仓建议\n"
                    f"    MA5偏 **{a['ma5_dev']:+.1f}%** RSI={a['rsi']:.0f}"
                    f"    → 可减仓30–50%，等回调MA5后再加"
                )
        sections += [md("**🚨 持仓预警（优先处理）**\n\n" + "\n\n".join(lines)), {"tag": "hr"}]

    # ── 实时买入机会 ────────────────────────────────────────────────────────
    if reading is None:
        sections += [md("**⚪ 实时扫描数据获取失败，请手动查看行情**"), {"tag": "hr"}]
        return sections

    strong = [s for s in reading.signals if s.stars >= 2]
    weak   = [s for s in reading.signals if s.stars == 1]

    if strong:
        panic_prefix = ""
        if reading.market_panic:
            panic_prefix = f"😱 **{reading.market_note}** — 恐慌低吸窗口！\n\n"

        lines = []
        for s in strong:
            # 从 MA20 偏离推算关键价位
            ma20 = s.current_price / (1 + s.ma20_dev_pct / 100) if s.ma20_dev_pct != -100 else s.current_price
            entry_lo = round(ma20 * 0.992, 3)
            entry_hi = round(ma20 * 1.008, 3)
            stop_p   = round(ma20 * 0.91,  3)
            target   = round(ma20 * 1.08,  3)

            icon = "🟢" if s.stars == 3 else "🔵"
            flow_str = ""
            if abs(s.main_force_flow) >= 0.1:
                arrow = "↑" if s.main_force_flow > 0 else "↓"
                flow_str = f" 主力{arrow}{abs(s.main_force_flow):.2f}亿"

            trigger_str = " | ".join(s.triggers[:2]) if s.triggers else ""
            lines.append(
                f"{icon} **{s.name}**（{s.code}）\n"
                f"    现价 {s.current_price:.3f} 今日{s.change_pct:+.1f}%"
                f" RSI={s.rsi6:.0f} MA20偏{s.ma20_dev_pct:+.1f}%{flow_str}\n"
                f"    📌 买入区 **{entry_lo}–{entry_hi}** | 止损 **{stop_p}** | 目标 {target}"
                + (f"\n    *{trigger_str}*" if trigger_str else "")
            )

        # 一般机会（★）也完整展示，只是换图标和标注"轻度"
        weak_lines = []
        for s in weak:
            ma20 = s.current_price / (1 + s.ma20_dev_pct / 100) if s.ma20_dev_pct != -100 else s.current_price
            entry_lo = round(ma20 * 0.992, 3)
            entry_hi = round(ma20 * 1.008, 3)
            stop_p   = round(ma20 * 0.91,  3)
            target   = round(ma20 * 1.08,  3)
            flow_str = ""
            if abs(s.main_force_flow) >= 0.1:
                arrow = "↑" if s.main_force_flow > 0 else "↓"
                flow_str = f" 主力{arrow}{abs(s.main_force_flow):.2f}亿"
            trigger_str = " | ".join(s.triggers[:2]) if s.triggers else ""
            weak_lines.append(
                f"🟡 **{s.name}**（{s.code}）\n"
                f"    现价 {s.current_price:.3f} 今日{s.change_pct:+.1f}%"
                f" RSI={s.rsi6:.0f} MA20偏{s.ma20_dev_pct:+.1f}%{flow_str}\n"
                f"    📌 买入区 **{entry_lo}–{entry_hi}** | 止损 **{stop_p}** | 目标 {target}"
                + (f"\n    *{trigger_str}*" if trigger_str else "")
            )

        weak_section = ("\n\n**⬇️ 一般机会（轻度超卖，可小仓观察）**\n\n" + "\n\n".join(weak_lines)) if weak_lines else ""
        header = f"**⚡ 实时买入机会**\n\n{panic_prefix}"
        sections += [md(header + "\n\n".join(lines) + weak_section), {"tag": "hr"}]

    elif reading.market_panic:
        # 大盘恐慌但无强信号 → 展示 weak 完整详情
        panic_lines = [f"**😱 大盘恐慌：{reading.market_note}**\n等待量能止稳后的低吸机会"]
        if weak:
            panic_lines.append("**⬇️ 一般机会（轻度超卖，可小仓关注）**")
            for s in weak:
                ma20 = s.current_price / (1 + s.ma20_dev_pct / 100) if s.ma20_dev_pct != -100 else s.current_price
                entry_lo = round(ma20 * 0.992, 3)
                entry_hi = round(ma20 * 1.008, 3)
                stop_p   = round(ma20 * 0.91,  3)
                target   = round(ma20 * 1.08,  3)
                flow_str = ""
                if abs(s.main_force_flow) >= 0.1:
                    arrow = "↑" if s.main_force_flow > 0 else "↓"
                    flow_str = f" 主力{arrow}{abs(s.main_force_flow):.2f}亿"
                trigger_str = " | ".join(s.triggers[:2]) if s.triggers else ""
                panic_lines.append(
                    f"🟡 **{s.name}**（{s.code}）\n"
                    f"    现价 {s.current_price:.3f} 今日{s.change_pct:+.1f}%"
                    f" RSI={s.rsi6:.0f} MA20偏{s.ma20_dev_pct:+.1f}%{flow_str}\n"
                    f"    📌 买入区 **{entry_lo}–{entry_hi}** | 止损 **{stop_p}** | 目标 {target}"
                    + (f"\n    *{trigger_str}*" if trigger_str else "")
                )
        sections += [md("\n\n".join(panic_lines)), {"tag": "hr"}]

    else:
        if weak:
            # 无强信号但有 weak → 也完整展示
            weak_only_lines = ["**⚪ 暂无强买入机会**\n\n**⬇️ 一般机会（轻度超卖，可小仓观察）**"]
            for s in weak:
                ma20 = s.current_price / (1 + s.ma20_dev_pct / 100) if s.ma20_dev_pct != -100 else s.current_price
                entry_lo = round(ma20 * 0.992, 3)
                entry_hi = round(ma20 * 1.008, 3)
                stop_p   = round(ma20 * 0.91,  3)
                target   = round(ma20 * 1.08,  3)
                flow_str = ""
                if abs(s.main_force_flow) >= 0.1:
                    arrow = "↑" if s.main_force_flow > 0 else "↓"
                    flow_str = f" 主力{arrow}{abs(s.main_force_flow):.2f}亿"
                trigger_str = " | ".join(s.triggers[:2]) if s.triggers else ""
                weak_only_lines.append(
                    f"🟡 **{s.name}**（{s.code}）\n"
                    f"    现价 {s.current_price:.3f} 今日{s.change_pct:+.1f}%"
                    f" RSI={s.rsi6:.0f} MA20偏{s.ma20_dev_pct:+.1f}%{flow_str}\n"
                    f"    📌 买入区 **{entry_lo}–{entry_hi}** | 止损 **{stop_p}** | 目标 {target}"
                    + (f"\n    *{trigger_str}*" if trigger_str else "")
                )
            sections += [md("\n\n".join(weak_only_lines)), {"tag": "hr"}]
        else:
            sections += [md("**⚪ 暂无买入机会**\n股池品种未到技术低点，继续等待回调"), {"tag": "hr"}]

    return sections


# ─── 主力资金意图分析 ────────────────────────────────────────────────────────────

def _flow_intent(today: float, yesterday: float, price_chg: float) -> str:
    """根据资金流向+价格行为推断主力意图，返回简短中文标签。"""
    delta = today - yesterday
    significant = abs(today) >= 0.08

    if not significant:
        return "主力观望"

    reversal = (today > 0.1 and yesterday < -0.1) or (today < -0.1 and yesterday > 0.1)

    if today > 0:
        if reversal:
            return "主力回头建仓"
        if delta >= 0.3 and today >= 0.3:
            return "主力加速买入"
        if price_chg <= -1.5:
            return "主力逆势抄底"
        if price_chg >= 2.5:
            return "主力追涨（注意追高风险）"
        return "主力稳步建仓"
    else:
        if reversal:
            return "主力转向出货 ⚠️"
        if delta <= -0.3 and abs(today) >= 0.3:
            return "主力加速撤退"
        if price_chg >= 1.5:
            return "拉高出货 ⚠️"
        if price_chg <= -2.0:
            return "主力止损出逃"
        return "主力逐步减仓"


def _format_flow_intent_section(
    flow_detail: dict, etf_prices: dict, north_signal: str = "UNKNOWN"
) -> list:
    """
    生成主力资金意图分析的飞书 card sections。
    只展示今日流向绝对值 >= 0.12亿 的品种，按|today|降序，最多12只。
    """
    def md(content: str) -> dict:
        return {"tag": "div", "text": {"tag": "lark_md", "content": content}}

    if not flow_detail:
        return []

    etf_names = {k: v.get("name", k) for k, v in ETF_UNIVERSE.items()}

    # 计算今日价格涨跌幅（从 etf_prices）
    price_chg: dict[str, float] = {}
    for code, df in etf_prices.items():
        if df is not None and len(df) >= 2:
            try:
                price_chg[code] = (float(df["close"].iloc[-1]) / float(df["close"].iloc[-2]) - 1) * 100
            except Exception:
                pass

    # 过滤 + 排序
    items = [
        (code, d) for code, d in flow_detail.items()
        if abs(d.get("today", 0)) >= 0.12
    ]
    items.sort(key=lambda x: abs(x[1]["today"]), reverse=True)
    items = items[:12]

    if not items:
        return []

    # 分组：态度转变 / 净流入 / 净流出
    reversals, inflows, outflows = [], [], []
    for code, d in items:
        today, yest = d["today"], d["yesterday"]
        delta = d["delta"]
        pct = price_chg.get(code, 0.0)
        intent = _flow_intent(today, yest, pct)
        is_reversal = (today > 0.1 and yest < -0.1) or (today < -0.1 and yest > 0.1)
        delta_str = f"{'↑' if delta > 0 else '↓'}{abs(delta):.2f}亿较昨"
        name = etf_names.get(code, code)
        row = (
            f"• **{name}**（{code}）"
            f"今日{'**+' if today > 0 else '**'}{today:.2f}亿**"
            f"  {delta_str}  {intent}"
        )
        if is_reversal:
            reversals.append(row)
        elif today > 0:
            inflows.append(row)
        else:
            outflows.append(row)

    # 整体背景判断
    total_today = sum(d["today"] for _, d in items)
    total_yest  = sum(d["yesterday"] for _, d in items)
    net_arrow = "↑" if total_today > total_yest else "↓"
    context_parts = [
        f"追踪品种净额  今日 **{total_today:+.2f}亿**  昨日 {total_yest:+.2f}亿  {net_arrow}变化{abs(total_today-total_yest):.2f}亿"
    ]
    # 简单背景解读
    if total_today > 1.0 and north_signal in ("IN", "STRONG_IN"):
        context_parts.append("北向与主力同向流入，多头共识较强")
    elif total_today > 0 and north_signal in ("OUT", "STRONG_OUT"):
        context_parts.append("主力流入但北向流出，内外资存在分歧")
    elif total_today < -1.0 and north_signal in ("OUT", "STRONG_OUT"):
        context_parts.append("内外资同步流出，回避情绪较重")
    elif total_today < 0 and north_signal in ("IN", "STRONG_IN"):
        context_parts.append("北向流入但主力流出，机构换手调仓中")

    lines = ["**📈 主力资金意图**\n", "\n".join(context_parts)]

    if reversals:
        lines.append("\n🔄 **态度转变（重点关注）**\n" + "\n".join(reversals))
    if inflows:
        lines.append("\n🟢 **净流入**\n" + "\n".join(inflows))
    if outflows:
        lines.append("\n🔴 **净流出**\n" + "\n".join(outflows))

    return [md("\n".join(lines)), {"tag": "hr"}]


# ─── 机制状态读取（从 main.py 08:30 持久化结果中读，避免重复计算）────────────

_REGIME_DESC = {
    "R1": "趋势偏多，顺势操作",
    "R2": "震荡市，结构性机会",
    "R3": "轮动市，跟强势板块",
    "R4": "风险偏高，谨慎防御",
}
_REGIMES_ORDER = ["R1", "R2", "R3", "R4"]


def load_regime_from_state() -> tuple[str, str]:
    """读取 regime_state.json，返回 (机制代码, 描述含待切换信息)"""
    state_path = Path(__file__).parent / "state" / "regime_state.json"
    try:
        state = json.loads(state_path.read_text())
        regime = state.get("current_regime", "R2")
        desc = _REGIME_DESC.get(regime, "")
        pending = state.get("pending_regime")
        pending_days = state.get("pending_days", 0)
        if pending and pending in _REGIMES_ORDER and regime in _REGIMES_ORDER:
            going_down = _REGIMES_ORDER.index(pending) > _REGIMES_ORDER.index(regime)
            needed = REGIME_DOWN_CONFIRM if going_down else REGIME_UP_CONFIRM
            desc += f"（待切换→{pending}，{pending_days}/{needed}日）"
        return regime, desc
    except Exception as e:
        print(f"  [警告] 读取机制状态失败({e})，默认R2")
        return "R2", "震荡市，结构性机会"


# ─── 今日信号详情读取 ─────────────────────────────────────────────────────────

def load_signal_detail() -> dict:
    """读取 main.py 今日生成的信号快照（08:30后可用）"""
    path = Path(__file__).parent / "logs" / f"signal_detail_{date.today().strftime('%Y%m%d')}.json"
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _format_etf_guidance(detail: dict) -> str:
    """
    飞书 lark_md 格式的 ETF 操作指导。
    布局：置信度 → 不可操作区 → 可操作区 → 机制限流区 → 一般观察
    """
    if not detail:
        return "（信号数据未生成，等待08:30 main.py运行后可用）"

    signals      = detail.get("signals", [])
    timing_risks = detail.get("timing_risks", {})
    timing_block = detail.get("sentiment", {}).get("timing_block", False)
    senti_level  = detail.get("sentiment", {}).get("level", "")
    senti_score  = detail.get("sentiment", {}).get("score", 0)
    regime       = detail.get("regime", "?")

    dh           = detail.get("data_health", {})
    confidence   = dh.get("confidence", "?")
    conf_icon    = {"HIGH": "✅", "MEDIUM": "⚠️", "LOW": "❌"}.get(confidence, "❓")

    rotation_strength = detail.get("rotation_strength", "NORMAL")
    rotation_switch   = rotation_strength in ("STRONG_SWITCH", "SUGGEST_REDUCE")

    # ── 分组 ──────────────────────────────────────────────────────────────────
    sell_sigs:      list[tuple] = []   # SELL_STOP — 真止损
    regime_blocked: list[tuple] = []   # REDUCE 因机制等级限制（强势但暂不可买）
    real_reduce:    list[tuple] = []   # REDUCE 真正减仓建议
    buy_actives:    list[tuple] = []
    danger_zone:    list[tuple] = []
    watch_list:     list[tuple] = []
    other_holds:    list[tuple] = []   # HOLD tier C/D
    avoid_list:     list[tuple] = []   # AVOID

    for s in signals:
        sig  = s["signal"]
        tier = s["tier"]
        note = s.get("note", "")
        tr   = timing_risks.get(s["code"], {})
        risk = tr.get("risk_level", "SAFE")

        if sig == "SELL_STOP":
            sell_sigs.append((s, tr))
        elif sig == "REDUCE":
            if "不支持" in note:
                regime_blocked.append((s, tr))
            else:
                real_reduce.append((s, tr))
        elif sig in ("BUY_STRONG", "BUY_WATCH"):
            buy_actives.append((s, tr))
        elif risk == "DANGER":
            danger_zone.append((s, tr))
        elif sig == "HOLD" and tier in ("S", "A", "B"):
            watch_list.append((s, tr))
        elif sig == "HOLD":
            other_holds.append((s, tr))
        else:
            avoid_list.append((s, tr))

    # ── 顶部：置信度 + 机制摘要 ───────────────────────────────────────────────
    lines = [
        f"{conf_icon} 置信度 **{confidence}** ｜ **{regime}** {senti_level} {senti_score:.0f}/100"
    ]

    # ── 不可操作区（始终优先展示）────────────────────────────────────────────
    has_restriction = bool(sell_sigs) or bool(real_reduce) or timing_block or bool(danger_zone) or rotation_switch
    if has_restriction:
        lines.append("\n**━━━ ⛔ 不可操作区 ━━━**")

        if sell_sigs:
            lines.append("**🔴 今日只允许减仓·止损（当日必须执行）**")
            for s, tr in sell_sigs:
                stop = s.get("stop_loss_pct", 8)
                lines.append(f"• **{s['name']}**（{s['code']}）止损 -{stop:.0f}%")

        if real_reduce:
            lines.append("**🟡 今日只允许减仓·逐步压缩**")
            for s, tr in real_reduce:
                wt = s.get("weight_pct", 0)
                lines.append(f"• {s['name']}（{s['code']}）建议仓位→{wt:.1f}%")

        if timing_block and buy_actives:
            lines.append("**🔥 今日禁止追高·情绪过热·所有买入信号降级**")
            for s, tr in buy_actives:
                ma5 = tr.get("ma5_dev_pct", 0.0)
                lines.append(f"• {s['name']}（{s['code']}）等回调 MA5偏{ma5:+.1f}%")
        elif timing_block:
            lines.append("**🔥 今日禁止追高·情绪过热·暂无新买点**")

        if danger_zone:
            lines.append("**⛔ 今日禁止追高·高位风险区**")
            for s, tr in danger_zone:
                ma5    = tr.get("ma5_dev_pct", 0.0)
                consec = tr.get("consec_up", 0)
                lines.append(f"• {s['name']}（{s['code']}）MA5偏 **{ma5:+.1f}%**  连涨{consec}日")

        if rotation_switch:
            label = {
                "STRONG_SWITCH":  "强切换·三维共振·立即降进攻仓位",
                "SUGGEST_REDUCE": "建议减仓·两维偏空·逐步移仓防御",
            }.get(rotation_strength, rotation_strength)
            lines.append(f"**🔄 今日适合防御切换 → {label}**")

    # ── 可操作区 ──────────────────────────────────────────────────────────────
    has_actionable = (buy_actives and not timing_block) or watch_list
    if has_actionable:
        lines.append("\n**━━━ ✅ 可操作区 ━━━**")

        if buy_actives and not timing_block:
            lines.append("**🟢 今日可买**")
            for s, tr in buy_actives:
                ma5  = tr.get("ma5_dev_pct", 0.0)
                wt   = s.get("weight_pct", 0)
                stop = s.get("stop_loss_pct", 8)
                label = "强买" if s["signal"] == "BUY_STRONG" else "关注买"
                zone  = "✓ MA5区间" if abs(ma5) <= 1.0 else f"MA5偏{ma5:+.1f}%"
                lines.append(
                    f"• **{s['name']}**（{s['code']}）{s['tier']}/{int(s['composite'])}分"
                    f"　{label}　建仓 **{wt:.1f}%**　{zone}　止损 -{stop:.0f}%"
                )

        if watch_list:
            lines.append("**🔵 今日只观察·等回调MA5附近**")
            for s, tr in watch_list:          # 移除 [:8] 限制，全量展示
                ma5  = tr.get("ma5_dev_pct", 0.0)
                risk = tr.get("risk_level", "SAFE")
                wt   = s.get("weight_pct", 0)
                zone = "✓ 入场区" if abs(ma5) <= 1.0 else f"距MA5 {ma5:+.1f}%"
                flag = " ⚡偏高" if risk == "CAUTION" else ""
                top2 = sorted(s.get("factors", {}).items(), key=lambda x: -x[1])[:2]
                factor_str = " ".join(f"{k}{int(v)}" for k, v in top2) if top2 else ""
                lines.append(
                    f"• **{s['name']}**（{s['code']}）{s['tier']}/{int(s['composite'])}分"
                    f"　{zone}{flag}　上限{wt:.1f}%　[{factor_str}]"
                )

    elif not has_restriction:
        lines.append("\n⚪ 暂无买点，全仓持仓观察")

    # ── 机制限流区（强势但当前机制等级受限，非卖出信号）────────────────────────
    if regime_blocked:
        lines.append(f"\n**━━━ 🔒 机制限流（{regime}不开放买入，非卖出）━━━**")
        for s, tr in regime_blocked:
            ma5    = tr.get("ma5_dev_pct", 0.0)
            ma20   = tr.get("ma20_dev_pct", 0.0)
            lines.append(
                f"• {s['name']}（{s['code']}）{s['tier']}/{int(s['composite'])}分"
                f"  MA5偏{ma5:+.1f}% MA20偏{ma20:+.1f}%  *等{regime}→R1或升A级解锁*"
            )

    # ── ETF全景扫描（C级：评分+趋势，让用户看清全池状态）────────────────────────
    c_show = [(s, tr) for s, tr in other_holds if s["composite"] >= 44]
    c_show.sort(key=lambda x: -x[0]["composite"])
    if c_show or avoid_list:
        lines.append("\n**━━━ 📡 ETF全景扫描 ━━━**")

    if c_show:
        lines.append("**C级·持仓观察（暂不加仓，跟踪趋势）**")
        for s, tr in c_show:
            ma5  = tr.get("ma5_dev_pct", 0.0)
            risk = tr.get("risk_level", "SAFE")
            consec = tr.get("consec_up", 0)
            trend_icon = "📈" if consec >= 2 else ("📉" if ma5 < -3 else "➡️")
            risk_tag = " ⚠️高位" if risk in ("CAUTION", "DANGER") else ""
            lines.append(
                f"  {trend_icon} {s['name']}（{s['code']}）{int(s['composite'])}分"
                f"  MA5偏{ma5:+.1f}%{risk_tag}"
            )

    if avoid_list:
        lines.append("**D级·禁配品种（不操作）**")
        names = "  ".join(
            f"{s['name']}({int(s['composite'])})" for s, _ in avoid_list
        )
        lines.append(f"  {names}")

    return "\n".join(lines)


def _format_board_heatmap(detail: dict) -> str:
    """板块热力表：每板块一行，按最高评分排序，始终显示（早盘+盘中均适用）"""
    if not detail:
        return ""
    try:
        from config import ETF_UNIVERSE as _EU
    except ImportError:
        return ""

    _CLUSTER_SHORT = {
        "ai_compute":   "🤖 AI算力",
        "power_export": "⚡ 电力设备",
        "space_robot":  "🚀 卫星机器人",
        "new_energy":   "🌱 新能源",
        "optics":       "💡 光模块",
        "semicon":      "💾 半导体设计",
        "defensive":    "🛡️ 红利防御",
        "finance":      "🏦 金融",
        "broad_market": "📊 宽基",
        "overseas":     "🌏 海外",
        "commodity":    "⛏️ 大宗商品",
        "agriculture":  "🌾 农业",
    }
    _SIG_ORDER = ["BUY_STRONG", "BUY_WATCH", "HOLD", "REDUCE", "SELL_STOP", "AVOID"]

    timing_block = detail.get("sentiment", {}).get("timing_block", False)
    senti_level  = detail.get("sentiment", {}).get("level", "")

    def _bar(score: float) -> str:
        filled = round((max(float(score), 40.0) - 40.0) / 60.0 * 6)
        filled = max(0, min(6, filled))
        return "█" * filled + "░" * (6 - filled)

    def _action(sig: str, tier: str) -> str:
        if sig == "BUY_STRONG":
            return "🟢**可买入**" if not timing_block else "⏳**持仓等回调**"
        if sig == "BUY_WATCH":
            return "🔵**可试仓**" if not timing_block else "⏳**持仓等回调**"
        if sig == "HOLD":
            if tier in ("S", "A"):
                return "⏳**持仓等加仓**" if timing_block else "🔵**等MA5可加**"
            if tier == "B":
                return "⏳持仓等回踩" if timing_block else "🔵等回踩关注"
            return "🔍C级跟踪"
        if sig == "REDUCE":
            return "🟡减仓"
        if sig == "SELL_STOP":
            return "🔴止损"
        return "⛔回避"

    signals = detail.get("signals", [])
    cmap: dict[str, list[dict]] = {}
    for s in signals:
        cl = _EU.get(s["code"], {}).get("cluster", "")
        if cl:
            cmap.setdefault(cl, []).append(s)

    if not cmap:
        return ""

    rows: list[tuple[int, str, str, dict, str]] = []
    for cl, cs in cmap.items():
        best_s     = max(cs, key=lambda x: x["composite"])
        best_score = int(best_s["composite"])
        if best_score < 40:
            continue
        best_sig = next((p for p in _SIG_ORDER if p in {x["signal"] for x in cs}), "AVOID")
        rows.append((-best_score, cl, best_sig, best_s, ""))

    if not rows:
        return ""

    rows.sort(key=lambda x: x[0])

    if timing_block:
        state_line = f"🔥 {senti_level}·买入拦截  A/B持仓等回调，C跟踪，有仓位不追高"
    else:
        state_line = "✅ 正常窗口·按信号操作"

    lines = [f"**🗺️ 板块热力表**\n{state_line}"]
    for neg_score, cl, best_sig, best_s, _ in rows:
        score  = -neg_score
        bar    = _bar(score)
        label  = _CLUSTER_SHORT.get(cl, cl)
        action = _action(best_sig, best_s["tier"])
        lines.append(f"{label}  `{bar}`  {best_s['tier']}/{score}  {best_s['name']}  {action}")

    return "\n".join(lines)


# ─── 集群板块策略（动态，用于 generate_guidance 第五节）────────────────────────

_CLUSTER_LABELS: dict[str, str] = {
    "ai_compute":   "🤖 AI算力·半导体",
    "power_export": "⚡ 电力设备·电网",
    "space_robot":  "🚀 卫星·机器人·工业母机",
    "new_energy":   "🌱 新能源·光伏·储能",
    "optics":       "💡 光模块·光器件",
    "semicon":      "💾 半导体设计",
    "defensive":    "🛡️ 红利·防御",
    "finance":      "🏦 金融·银行·券商",
    "broad_market": "📊 宽基指数",
    "overseas":     "🌏 海外·港股",
    "commodity":    "⛏️ 大宗商品·资源",
    "agriculture":  "🌾 农业",
}

_SIG_PRIORITY = ["BUY_STRONG", "BUY_WATCH", "HOLD", "REDUCE", "SELL_STOP", "AVOID"]


def _cluster_note(sigs: list[dict], timing_block: bool) -> str:
    sig_set = {s["signal"] for s in sigs}
    best_sig = next((p for p in _SIG_PRIORITY if p in sig_set), "AVOID")
    best_score = max(s["composite"] for s in sigs)
    if best_sig == "BUY_STRONG":
        return "🟢 强势板块，今日可建仓"
    if best_sig == "BUY_WATCH":
        return "🔵 今日可小仓试探"
    if best_sig == "HOLD":
        if timing_block:
            return "⏳ 情绪过热，等回调5日线再介入"
        if best_score >= 70:
            return "🔵 高分板块，等MA5回踩即可建仓"
        if best_score >= 55:
            return "⏳ B级板块，耐心等待回踩"
        return "⚪ C级·暂未启动，跟踪观察"
    if best_sig in ("REDUCE", "SELL_STOP"):
        return "🟡 持仓减仓，不加新仓"
    return "⛔ 空仓回避"


def _format_cluster_strategy(detail: dict) -> str:
    """按 cluster 分组，生成动态板块策略（用于每日操作指导 section 五）"""
    if not detail:
        return "（信号数据未生成，待08:30 main.py运行后可用）"
    try:
        from config import ETF_UNIVERSE as _EU
    except ImportError:
        return "（config.ETF_UNIVERSE 未找到）"

    signals      = detail.get("signals", [])
    timing_risks = detail.get("timing_risks", {})
    timing_block = detail.get("sentiment", {}).get("timing_block", False)

    cluster_map: dict[str, list[dict]] = {}
    for s in signals:
        cl = _EU.get(s["code"], {}).get("cluster", "")
        if cl:
            cluster_map.setdefault(cl, []).append(s)

    if not cluster_map:
        return "（集群配置未找到，请检查 ETF_UNIVERSE cluster 字段）"

    sorted_clusters = sorted(
        cluster_map.items(),
        key=lambda x: -max(s["composite"] for s in x[1]),
    )

    _sig_tag = {
        "BUY_STRONG": "🟢强买",  "BUY_WATCH": "🔵关注买",
        "HOLD": "持仓观察",       "REDUCE": "🟡减仓",
        "SELL_STOP": "🔴止损",    "AVOID": "⛔回避",
    }

    lines: list[str] = []
    alpha_idx = 0
    for cluster, sigs in sorted_clusters:
        best_score = max(s["composite"] for s in sigs)
        if best_score < 40:
            continue
        best_tier = max(sigs, key=lambda s: s["composite"])["tier"]
        label = _CLUSTER_LABELS.get(cluster, cluster)
        prefix = chr(ord("A") + alpha_idx) if alpha_idx < 26 else str(alpha_idx + 1)
        alpha_idx += 1
        lines.append(f"\n### {prefix}. {label}（{best_tier}级·{best_score:.0f}分）")
        for s in sorted(sigs, key=lambda x: -x["composite"])[:6]:
            tr   = timing_risks.get(s["code"], {})
            ma5  = tr.get("ma5_dev_pct", 0.0)
            tag  = _sig_tag.get(s["signal"], s["signal"])
            ma5_str = f"  MA5偏{ma5:+.1f}%" if abs(ma5) >= 0.5 else ""
            lines.append(
                f"- {s['name']}（{s['code']}）{s['tier']}/{int(s['composite'])}分"
                f"  {tag}{ma5_str}"
            )
        lines.append(f"> {_cluster_note(sigs, timing_block)}")

    return "\n".join(lines) if lines else "（今日无有效板块信号）"


# ─── 生成操作指导 ─────────────────────────────────────────────────────────────

def generate_guidance(
    macro: dict, regime: str, regime_desc: str, bank_signal: dict,
    signal_detail: dict, north_summary: dict | None = None,
) -> str:
    today_str = date.today().isoformat()

    us_summary = "外部数据获取失败"
    if macro.get("us"):
        us_lines = [f"{k}: {v:+.1f}%" for k, v in macro["us"].items()]
        us_summary = " | ".join(us_lines) if us_lines else "数据异常"

    btc_info = ""
    if macro.get("crypto") and "BTC" in macro["crypto"]:
        b = macro["crypto"]["BTC"]
        btc_info = f"BTC: ${b['price']:,.0f}（{b['pct']:+.1f}%）"

    pos_limit = {"R1": "≤70%", "R2": "≤50%", "R3": "≤55%", "R4": "≤30%"}.get(regime, "≤50%")

    # 北向资金章节
    if north_summary:
        north_section = north_summary["text"].replace("**", "")  # MD 不需要 lark_md 加粗
    else:
        north_section = "北向资金：数据获取失败"

    guidance = f"""# 今日操作指导 — {today_str}

> 自动生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}
> 策略版本：V4.0 + 业绩景气度优先框架

---

## 一、外部宏观快览

- 美股：{us_summary}
- 加密：{btc_info or '数据获取失败'}
- 市场机制：**{regime}** — {regime_desc}
- 仓位上限：{pos_limit}，现金储备：≥30%

---

## 二、银行/券商节奏信号

{bank_signal['detail']}

> **结论：{bank_signal['conclusion']}**

---

## 三、北向资金

{north_section}

---

## 四、股池操作指导

{_format_etf_guidance(signal_detail)}

---

## 五、主线板块策略

{_format_cluster_strategy(signal_detail)}

---

## 六、行为护栏（开盘前必做）

```
[ ] 今日止损上限：_____ 元
[ ] 昨日亏损状态：_____ （亏损>1%则今日仓位自动降50%）
[ ] "赢回来"心理检测：有→强制休市半天
[ ] 无龙头信号不建仓
[ ] 两融亏损仓位禁止加仓
```

---

## 七、执行时序

| 时间 | 动作 |
|------|------|
| 09:15 | 看竞价，判断银行/券商 |
| 09:30-09:45 | 确认板块龙头是否起 |
| 10:00 | 有信号才建仓 |
| 14:00 | 尾盘方向确认 |
| 14:55 | 截止操作 |

---

> 今日红线：无龙头不入场 ｜ 两融不加仓 ｜ 高位不追 ｜ 止损严格止盈随时
"""
    return guidance


# ─── 飞书推送 ─────────────────────────────────────────────────────────────────

_SIGNAL_HEADER_COLOR = {
    "STRONG": "blue",
    "ACTIVE": "turquoise",
    "CAUTION": "yellow",
    "WAIT": "grey",
    "MIXED": "orange",
    "UNKNOWN": "indigo",
}


def send_feishu(
    macro: dict, regime: str, regime_desc: str, bank_signal: dict,
    signal_detail: dict, rotation=None, north_summary: dict | None = None,
    realtime_reading=None, position_alerts: list | None = None,
    flow_detail: dict | None = None, etf_prices: dict | None = None,
):
    pos_limit = {"R1": "≤70%", "R2": "≤50%", "R3": "≤55%", "R4": "≤30%"}.get(regime, "≤50%")
    today = date.today().isoformat()
    now_hm = datetime.now().strftime("%H:%M")
    hour = datetime.now().hour
    is_morning = hour <= 8

    _pa = position_alerts or []
    _has_stop  = any(a["alert_type"] == "STOP_LOSS" for a in _pa)
    _has_buy   = realtime_reading is not None and any(s.stars >= 2 for s in realtime_reading.signals)
    _is_panic  = realtime_reading is not None and realtime_reading.market_panic

    if _has_stop:
        header_color, title_icon = "red", "🚨"
    elif _has_buy or _is_panic:
        header_color, title_icon = "green", "⚡"
    elif _pa:
        header_color, title_icon = "yellow", "⚠️"
    else:
        header_color = _SIGNAL_HEADER_COLOR.get(bank_signal.get("signal", "UNKNOWN"), "indigo")
        title_icon = "📋" if is_morning else "📊"

    title_str = f"{title_icon} 操作指导 {now_hm} | {today}"

    def md(content: str) -> dict:
        return {"tag": "div", "text": {"tag": "lark_md", "content": content}}

    # ── 宏观快览（压缩3行）───────────────────────────────────────────────────
    _abbrev = {"道琼斯": "道指", "纳斯达克100": "纳指", "纳指100": "纳指", "标普500": "标普"}
    us_parts: list[str] = []
    if macro.get("us"):
        for k, v in list(macro["us"].items())[:3]:
            us_parts.append(f"{_abbrev.get(k, k)}{v:+.1f}%")
    us_str = " / ".join(us_parts) if us_parts else "获取失败"

    north_short = "数据待刷新"
    if north_summary:
        nt = north_summary.get("today_net")
        n5 = north_summary.get("net_5d")
        em = north_summary.get("emoji", "")
        if nt is not None:
            north_short = f"{em} 净买 **{nt:+.1f}亿**"
            if n5 is not None:
                north_short += f"（5日{n5:+.1f}亿）"
        elif north_summary.get("deal_today") is not None:
            north_short = f"成交 {north_summary['deal_today']:.0f}亿"

    bank_pct = bank_signal.get("bank_pct")
    broker_pct = bank_signal.get("broker_pct")
    if bank_pct is not None and broker_pct is not None:
        bank_str = f"银行{bank_pct:+.1f}%/券商{broker_pct:+.1f}% → {bank_signal.get('conclusion', '—')}"
    else:
        bank_str = bank_signal.get("conclusion", "数据获取失败")

    macro_block = (
        f"**📊 市场快览**\n"
        f"美股：{us_str}　｜　北向：{north_short}\n"
        f"{bank_str}\n"
        f"机制：**{regime}** {regime_desc}　仓位上限 **{pos_limit}**"
    )

    # ── 攻守切换（仅触发时显示）─────────────────────────────────────────────
    rotation_elements: list[dict] = []
    if rotation is not None and rotation.should_switch:
        rotation_elements = [
            {"tag": "hr"},
            md(
                f"**🔄 攻守切换  {rotation.dim_line()}**\n"
                f"**{rotation.label()}**\n"
                f"{rotation.detail_block()}"
            ),
        ]

    # ── 正文组装 ─────────────────────────────────────────────────────────────
    elements: list[dict] = []
    if is_morning:
        # 08:50 开盘前：显示 08:30 生成的信号快照
        elements += [
            md("**📋 今日操作（08:30快照）**\n" + _format_etf_guidance(signal_detail)),
            {"tag": "hr"},
        ]
    else:
        # 盘中：显示实时扫描结果 + 持仓预警
        elements += _format_realtime_sections(realtime_reading, _pa)
        # 主力资金意图（盘中才有当日流向数据）
        north_sig = (north_summary or {}).get("signal", "UNKNOWN")
        elements += _format_flow_intent_section(
            flow_detail or {}, etf_prices or {}, north_signal=north_sig
        )

    # 板块热力表（早盘+盘中均显示）
    heatmap_text = _format_board_heatmap(signal_detail)
    if heatmap_text:
        elements += [{"tag": "hr"}, md(heatmap_text)]

    elements += [
        {"tag": "hr"},
        md(macro_block),
        *rotation_elements,
        {
            "tag": "note",
            "elements": [{"tag": "plain_text", "content": (
                "红线：无龙头不追 | 两融不加仓 | 高位不买 | 止损到价立即执行 | 现金≥30%"
            )}],
        },
    ]

    data = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title_str},
                "template": header_color,
            },
            "elements": elements,
        },
    }

    primary_result = None
    for i, url in enumerate(FEISHU_WEBHOOKS):
        resp = requests.post(url, json=data, timeout=10)
        result = resp.json()
        if i == 0:
            primary_result = result
            if result.get("code") != 0:
                raise ValueError(f"code={result.get('code')} msg={result.get('msg')}")
        else:
            if result.get("code") != 0:
                import logging as _lg
                _lg.getLogger(__name__).warning(f"副 webhook 推送失败({url[-8:]}): {result}")
    return primary_result


# ─── 保存MD文件 ───────────────────────────────────────────────────────────────

def save_md(content: str):
    import os
    now = datetime.now()
    today_str = now.strftime("%Y%m%d")
    hm_str = now.strftime("%H%M")
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "交易日志",
        f"{today_str}_{hm_str}_操作指导.md",
    )
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[OK] 已保存: {path}")
    return path


# ─── 主流程 ───────────────────────────────────────────────────────────────────

def main():
    if not is_trading_day():
        print(f"[跳过] {date.today()} 非交易日")
        return

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 开始生成今日操作指导...")

    # 抓取宏观数据
    macro = {
        "us": fetch_us_indices(),
        "hk": fetch_hk_indices(),
        "crypto": fetch_gold_oil(),
        "futures": fetch_a_share_futures(),
    }

    # 从持久化状态读取机制（main.py 08:30 已完整六指标计算）
    regime, regime_desc = load_regime_from_state()
    print(f"[机制] {regime}: {regime_desc}")

    # 银行/券商节奏信号
    bank_signal = fetch_bank_broker_signal()
    print(f"[节奏] {bank_signal['detail']} → {bank_signal['conclusion']}")

    # 北向资金评价
    north_summary = fetch_north_flow_summary()
    today_net_str = f"{north_summary['today_net']:+.2f}亿" if north_summary.get("today_net") is not None else "仅活跃度"
    deal_str = f" 成交{north_summary['deal_today']:.1f}亿" if north_summary.get("deal_today") else ""
    print(f"[北向] 净买入 {today_net_str}{deal_str} → {north_summary['summary']}")

    # 今日信号快照（main.py 08:30 生成）
    signal_detail = load_signal_detail()
    etf_count = len(signal_detail.get("signals", []))
    print(f"[信号] 加载 {etf_count} 只ETF信号{'（无数据）' if not etf_count else ''}")

    # 三维攻守切换强度（仅在情绪过热时计算，避免额外API开销）
    rotation = None
    timing_block = signal_detail.get("sentiment", {}).get("timing_block", False)
    if timing_block:
        from sentiment_gauge import SentimentReading, calc_market_sentiment
        from data_fetcher import get_index_prices, get_market_breadth
        try:
            csi300_df = get_index_prices("000300", days=40)
            breadth   = get_market_breadth()
            fresh_senti = calc_market_sentiment(csi300_df, breadth)
            north_5d = signal_detail.get("north_5d_billion")
            policy_avg = fetch_policy_sector_avg_chg()
            rotation = calc_rotation_signal(fresh_senti, north_5d, policy_avg)
            print(f"[切换] {rotation.dim_line()} → {rotation.strength}")
        except Exception as e:
            print(f"  [警告] 三维切换信号计算失败: {e}")

    # 实时ETF扫描（09:50+ 生效，08:50 市场未开时跳过）
    realtime_reading, etf_prices, flow_detail = fetch_realtime_scan()
    position_alerts = fetch_position_alerts(etf_prices)
    if realtime_reading:
        strong_cnt = sum(1 for s in realtime_reading.signals if s.stars >= 2)
        print(f"[实时] {'恐慌！' if realtime_reading.market_panic else '平稳'} "
              f"强信号 {strong_cnt} 个 | 全部 {len(realtime_reading.signals)} 个")
    if flow_detail:
        sig_flows = {k: v["today"] for k, v in flow_detail.items() if abs(v["today"]) >= 0.12}
        print(f"[资金] 主力流向有效品种 {len(sig_flows)} 个: "
              f"流入{sum(1 for v in sig_flows.values() if v>0)}只 "
              f"流出{sum(1 for v in sig_flows.values() if v<0)}只")
    if position_alerts:
        print(f"[持仓] {len(position_alerts)} 个预警: {[a['code'] for a in position_alerts]}")

    # 保存每日操作指导 Markdown
    try:
        md_content = generate_guidance(macro, regime, regime_desc, bank_signal, signal_detail, north_summary)
        save_md(md_content)
    except Exception as e:
        print(f"[WARN] 操作指导保存失败: {e}")

    # 推送飞书（遇限流最多重试3次，间隔30s/60s）
    try:
        _retry(
            lambda: send_feishu(
                macro, regime, regime_desc, bank_signal, signal_detail,
                rotation=rotation, north_summary=north_summary,
                realtime_reading=realtime_reading, position_alerts=position_alerts,
                flow_detail=flow_detail, etf_prices=etf_prices,
            ),
            attempts=3, delays=(30, 60),
        )
        print("[OK] 飞书推送成功")
    except Exception as e:
        print(f"[ERR] 飞书推送最终失败: {e}")

    print(f"[完成] {datetime.now().strftime('%H:%M:%S')}")


if __name__ == "__main__":
    main()
