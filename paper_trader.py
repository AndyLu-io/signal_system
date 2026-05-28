"""
三账户模拟盘追踪系统。

三个独立 100 万账户，各消费不同信号源：
  - ETF 账户：signal_detail + tail_detail 中的 ETF 信号
  - 个股账户：stock_timing 个股择时信号
  - 指数账户：index_timing 宽基指数信号

每日收盘后推送三账户对比卡片到飞书。

用法：
  python3 signal_system/paper_trader.py [--dry]     # 每日运行
  python3 signal_system/paper_trader.py --reset     # 重置全部账户
  python3 signal_system/paper_trader.py --status    # 查看状态
  python3 signal_system/paper_trader.py --force     # 跳过交易日检查
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import ACCOUNT_NET_VALUE, REGIME_PARAMS
from data_fetcher import market_prefix, get_etf_prices
from utils import atomic_write_json, is_trading_day, webhooks_from_env
from feishu_pusher import post_card

logger = logging.getLogger(__name__)

# ─── 常量 ──────────────────────────────────────────────────────────────────────

_STATE_DIR = Path(__file__).parent / "state"
_LOGS_DIR = Path(__file__).parent / "logs"

INITIAL_CAPITAL = 1_000_000.0

ACCOUNTS = {
    "etf": {"label": "ETF账户", "state_file": "paper_trade_etf.json"},
    "stock": {"label": "个股账户", "state_file": "paper_trade_stock.json"},
    "index": {"label": "指数账户", "state_file": "paper_trade_index.json"},
}

_FEISHU_WEBHOOKS_DEFAULT = [
    "https://open.feishu.cn/open-apis/bot/v2/hook/5506bc09-083a-4daa-9285-cb1677d83fbd"
]

COMMISSION_RATE = 0.0003
SLIPPAGE_RATE = 0.001
MAX_POSITIONS = 5  # 每个账户最多持仓数
MIN_WEIGHT_PCT = 15.0  # 单只最低仓位%（确保资金充分利用）


# ─── 状态管理 ──────────────────────────────────────────────────────────────────

def _default_state(account_name: str) -> dict:
    return {
        "account": account_name,
        "initial_capital": INITIAL_CAPITAL,
        "cash": INITIAL_CAPITAL,
        "positions": {},
        "nav_history": [],
        "trades": [],
        "created_at": date.today().isoformat(),
        "last_update": None,
    }


def load_state(account: str) -> dict:
    path = _STATE_DIR / ACCOUNTS[account]["state_file"]
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return _default_state(account)


def save_state(state: dict) -> None:
    account = state["account"]
    path = _STATE_DIR / ACCOUNTS[account]["state_file"]
    state["last_update"] = datetime.now().isoformat(timespec="seconds")
    atomic_write_json(path, state)


def reset_all() -> None:
    for name in ACCOUNTS:
        state = _default_state(name)
        save_state(state)
    logger.info(f"三账户已重置，各 {INITIAL_CAPITAL:,.0f}")


# ─── 价格获取 ──────────────────────────────────────────────────────────────────

_price_cache: dict[str, float] = {}


def _fetch_single_price(code: str) -> float | None:
    import requests
    symbol = f"{market_prefix(code)}{code}"
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?_var=kline_dayqfq&param={symbol},day,,,3,qfq")
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        r.raise_for_status()
        raw = r.text.replace("kline_dayqfq=", "")
        data = json.loads(raw)
        inner = data.get("data", {}).get(symbol, {})
        klines = inner.get("day") or inner.get("qfqday") or []
        if klines:
            return float(klines[-1][2])
    except Exception as e:
        logger.warning(f"获取 {code} 价格失败: {e}")
    return None


def _get_current_price(code: str) -> float | None:
    if code in _price_cache:
        return _price_cache[code]
    price = _fetch_single_price(code)
    if price and price > 0:
        _price_cache[code] = price
        return price
    return None


def preload_prices(codes: list[str]) -> None:
    result = get_etf_prices(codes, days=3)
    for code, df in result.items():
        if df is not None and not df.empty:
            _price_cache[code] = float(df.iloc[-1]["close"])


# ─── 交易执行 ──────────────────────────────────────────────────────────────────

def execute_buy(state: dict, code: str, name: str, weight_pct: float,
                stop_pct: float, price: float, today: str, reason: str,
                note: str = "") -> bool:
    nav = _calc_nav(state)
    target_amount = nav * (weight_pct / 100.0)

    effective_price = price * (1 + SLIPPAGE_RATE)
    cost_with_fee = target_amount * (1 + COMMISSION_RATE)

    if cost_with_fee > state["cash"]:
        target_amount = state["cash"] / (1 + COMMISSION_RATE)
        if target_amount < 1000:
            return False

    shares = int(target_amount / effective_price / 100) * 100
    if shares <= 0:
        return False

    actual_amount = shares * effective_price
    commission = actual_amount * COMMISSION_RATE
    total_cost = actual_amount + commission
    state["cash"] -= total_cost

    if code in state["positions"]:
        pos = state["positions"][code]
        old_shares = pos["shares"]
        old_cost = pos["cost_price"] * old_shares
        new_total_shares = old_shares + shares
        pos["cost_price"] = (old_cost + actual_amount) / new_total_shares
        pos["shares"] = new_total_shares
        pos["buy_date"] = today
    else:
        state["positions"][code] = {
            "name": name,
            "shares": shares,
            "cost_price": effective_price,
            "buy_date": today,
            "stop_pct": stop_pct,
            "reason": reason,
            "note": note,
        }

    state["trades"].append({
        "date": today, "code": code, "name": name, "action": "BUY",
        "price": round(effective_price, 4), "shares": shares,
        "amount": round(total_cost, 2), "reason": reason,
    })
    return True


def execute_sell(state: dict, code: str, price: float, today: str,
                 reason: str, partial_ratio: float = 1.0) -> bool:
    if code not in state["positions"]:
        return False
    pos = state["positions"][code]
    if pos["buy_date"] == today:
        return False

    sell_shares = int(pos["shares"] * partial_ratio / 100) * 100
    if sell_shares <= 0:
        sell_shares = pos["shares"]

    effective_price = price * (1 - SLIPPAGE_RATE)
    amount = sell_shares * effective_price
    commission = amount * COMMISSION_RATE
    net_amount = amount - commission
    state["cash"] += net_amount

    pnl_pct = (effective_price / pos["cost_price"] - 1) * 100
    state["trades"].append({
        "date": today, "code": code, "name": pos["name"], "action": "SELL",
        "price": round(effective_price, 4), "shares": sell_shares,
        "amount": round(net_amount, 2), "pnl_pct": round(pnl_pct, 2),
        "reason": reason,
    })

    if sell_shares >= pos["shares"]:
        del state["positions"][code]
    else:
        pos["shares"] -= sell_shares
    return True


# ─── 止损 + 信号消费 ──────────────────────────────────────────────────────────

def check_stop_loss(state: dict, today: str) -> int:
    triggered = 0
    for code in list(state["positions"].keys()):
        pos = state["positions"][code]
        if pos["buy_date"] == today:
            continue
        price = _get_current_price(code)
        if not price:
            continue
        loss_pct = (price / pos["cost_price"] - 1)
        if loss_pct <= -pos["stop_pct"] / 100.0:
            execute_sell(state, code, price, today, f"止损({loss_pct*100:.1f}%)")
            triggered += 1
    return triggered


def veteran_filter(signals: list[dict], account: str, state: dict) -> list[dict]:
    """
    资深投资者决策层 — 在系统信号基础上做二次过滤。

    核心原则：
      - 宁可错过，不可做错（只做高确定性机会）
      - 顺势而为，不抄底摸顶
      - 已满仓时只进不出（等信号换仓）
      - 连续下跌的不接飞刀
    """
    filtered = []

    for sig in signals:
        signal_type = sig.get("signal", "")
        composite = sig.get("composite", 0)
        code = sig.get("code", "")

        # 卖出/减仓信号直接放行
        if signal_type in ("SELL_STOP", "REDUCE"):
            filtered.append(sig)
            continue

        # 非买入信号跳过
        if signal_type not in ("BUY_STRONG", "BUY_WATCH", "BUY"):
            continue

        # ── 个股账户：score是0-10尺度，用信号类型判断 ──
        if account == "stock":
            if signal_type == "BUY_STRONG":
                sig = {**sig, "composite": 90}  # 确保排序靠前
                filtered.append(sig)
            elif signal_type == "BUY_WATCH" and composite >= 4:
                sig = {**sig, "composite": 70}
                filtered.append(sig)
            continue

        # ── ETF/指数账户：composite 是 0-100 尺度 ──

        # 1. composite 太低不做
        if composite < 55:
            continue

        # 2. 尾盘信号只做高分的（>=65）
        if sig.get("stars") is not None and sig.get("score", 0) < 65:
            continue

        # 3. 底部翻转：资深投资者只在高确定性时布局（>=12/18分 → composite>=79）
        if sig.get("source") == "reversal" and composite < 75:
            continue

        # 4. 指数账户：只做趋势明确的
        if account == "index" and composite < 60:
            continue

        # 4. 已持仓的不重复买
        if code in state.get("positions", {}):
            continue

        # 5. BUY_STRONG 加排序权重
        if signal_type == "BUY_STRONG":
            sig = {**sig, "composite": composite + 20}

        filtered.append(sig)

    return filtered


def consume_signals(state: dict, signals: list[dict], today: str, account: str = "etf") -> dict:
    summary = {"buys": 0, "sells": 0, "skipped": 0, "stop_losses": 0}

    # 资深投资者决策过滤
    signals = veteran_filter(signals, account, state)

    summary["stop_losses"] = check_stop_loss(state, today)

    for sig in signals:
        if sig.get("signal") in ("SELL_STOP", "REDUCE"):
            code = sig["code"]
            if code in state["positions"]:
                price = _get_current_price(code)
                if price:
                    ratio = 0.5 if sig["signal"] == "REDUCE" else 1.0
                    if execute_sell(state, code, price, today, sig["signal"], ratio):
                        summary["sells"] += 1

    buy_signals = [s for s in signals if s.get("signal") in ("BUY_STRONG", "BUY_WATCH", "BUY")]
    best_buys: dict[str, dict] = {}
    for sig in buy_signals:
        code = sig["code"]
        if code in best_buys:
            existing = best_buys[code]
            if sig.get("signal") == "BUY_STRONG" and existing.get("signal") != "BUY_STRONG":
                best_buys[code] = sig
            elif sig.get("weight_pct", 0) > existing.get("weight_pct", 0):
                best_buys[code] = sig
        else:
            best_buys[code] = sig

    # 按 composite 分数排序，优先买入最强信号
    sorted_buys = sorted(best_buys.values(), key=lambda s: s.get("composite", 0), reverse=True)

    for sig in sorted_buys:
        code = sig["code"]
        if code in state["positions"]:
            continue
        if len(state["positions"]) >= MAX_POSITIONS:
            break  # 已满仓

        price = _get_current_price(code)
        if not price:
            summary["skipped"] += 1
            continue
        weight = max(sig.get("weight_pct", 0), MIN_WEIGHT_PCT)
        stop = sig.get("stop_loss_pct", 6.0)
        if weight > 0:
            note = sig.get("note", "") or sig.get("reasons", "")
            if isinstance(note, list):
                note = "; ".join(note)
            if execute_buy(state, code, sig["name"], weight, stop, price, today,
                          sig.get("signal", "BUY"), note=note):
                summary["buys"] += 1
            else:
                summary["skipped"] += 1

    return summary


# ─── NAV 计算 ─────────────────────────────────────────────────────────────────

def _calc_nav(state: dict) -> float:
    market_value = 0.0
    for code, pos in state["positions"].items():
        price = _get_current_price(code) or pos["cost_price"]
        market_value += price * pos["shares"]
    return state["cash"] + market_value


def update_nav(state: dict, today: str) -> dict:
    market_value = 0.0
    for code, pos in state["positions"].items():
        price = _get_current_price(code) or pos["cost_price"]
        market_value += price * pos["shares"]
    nav = state["cash"] + market_value
    benchmark = _get_current_price("510300") or 0
    record = {
        "date": today,
        "nav": round(nav, 2),
        "cash": round(state["cash"], 2),
        "market_value": round(market_value, 2),
        "position_count": len(state["positions"]),
        "benchmark_close": round(benchmark, 4) if benchmark else None,
    }
    state["nav_history"].append(record)
    return record


def calc_performance(state: dict) -> dict:
    history = state["nav_history"]
    if not history:
        return {"total_return_pct": 0, "max_drawdown_pct": 0, "benchmark_return_pct": 0,
                "excess_return_pct": 0, "win_rate_pct": 0, "profit_factor": 0,
                "current_nav": state["cash"], "current_positions": 0, "days_tracked": 0,
                "total_trades": 0, "sell_trades": 0}

    initial = state["initial_capital"]
    current_nav = history[-1]["nav"]
    total_return = (current_nav / initial - 1) * 100

    peak = initial
    max_dd = 0.0
    for r in history:
        if r["nav"] > peak:
            peak = r["nav"]
        dd = (r["nav"] / peak - 1) * 100
        if dd < max_dd:
            max_dd = dd

    trades = state["trades"]
    sell_trades = [t for t in trades if t["action"] == "SELL"]
    wins = [t for t in sell_trades if t.get("pnl_pct", 0) > 0]
    losses = [t for t in sell_trades if t.get("pnl_pct", 0) < 0]
    win_rate = len(wins) / len(sell_trades) * 100 if sell_trades else 0
    avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0
    profit_factor = abs(avg_win / avg_loss) if avg_loss != 0 else 0

    benchmark_return = 0
    if len(history) >= 2 and history[0].get("benchmark_close") and history[-1].get("benchmark_close"):
        b0, b1 = history[0]["benchmark_close"], history[-1]["benchmark_close"]
        if b0 > 0:
            benchmark_return = (b1 / b0 - 1) * 100

    return {
        "total_return_pct": round(total_return, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "benchmark_return_pct": round(benchmark_return, 2),
        "excess_return_pct": round(total_return - benchmark_return, 2),
        "win_rate_pct": round(win_rate, 1),
        "profit_factor": round(profit_factor, 2),
        "current_nav": round(current_nav, 2),
        "current_positions": len(state["positions"]),
        "days_tracked": len(history),
        "total_trades": len(trades),
        "sell_trades": len(sell_trades),
    }


# ─── 信号加载（按账户类型筛选） ────────────────────────────────────────────────

def load_signals_for_account(account: str, today: str, session: str = "all") -> list[dict]:
    """
    按账户类型和时段加载对应信号。
    session: "morning"=早盘(09:30), "afternoon"=尾盘(14:45), "all"=全天合并
    """
    date_compact = today.replace("-", "")
    signals = []

    if account == "etf":
        # 早盘: 先加载周报推荐信号（周五生成→下周一执行）
        if session in ("morning", "all"):
            market_sig_file = _STATE_DIR / "market_signals.json"
            if market_sig_file.exists():
                try:
                    ms = json.loads(market_sig_file.read_text(encoding="utf-8"))
                    expires = ms.get("expires", "")
                    if today <= expires:
                        for sig in ms.get("signals", []):
                            sig["composite"] = sig.get("composite", 70)
                            signals.append(sig)
                        if ms.get("signals"):
                            logger.info(f"[ETF] 加载周报信号 {len(ms['signals'])}条 (有效至{expires})")
                except Exception as e:
                    logger.warning(f"读取 market_signals.json 失败: {e}")

            # ETF主信号（08:30生成）
            f1 = _LOGS_DIR / f"signal_detail_{date_compact}.json"
            if f1.exists():
                data = json.loads(f1.read_text(encoding="utf-8"))
                for sig in data.get("signals", []):
                    signals.append(sig)

            # 底部翻转检测 — 资深投资者视角：只做高分翻转（>=12/18）
            try:
                import etf_reversal
                from config import ETF_UNIVERSE
                reversal_codes = list(ETF_UNIVERSE.keys())
                rev_prices = get_etf_prices(reversal_codes, days=60)
                for code, df in rev_prices.items():
                    if df is None or df.empty:
                        continue
                    info = ETF_UNIVERSE.get(code, {})
                    if not info:
                        continue
                    rev = etf_reversal.check_reversal(
                        code=code, df=df, info=info,
                    )
                    if rev and rev["rev_pts"] >= 12:
                        details = rev.get("rev_details", "")
                        signals.append({
                            "code": code,
                            "name": rev["name"],
                            "signal": "BUY_WATCH",
                            "weight_pct": 5.0,
                            "stop_loss_pct": 8.0,
                            "composite": min(95, 55 + rev["rev_pts"] * 2),
                            "note": f"底部翻转{rev['rev_pts']}/18分; {details[:30]}",
                            "source": "reversal",
                        })
            except Exception as e:
                logger.warning(f"底部翻转检测异常: {e}")

        # 尾盘: tail_detail（14:45生成）
        if session in ("afternoon", "all"):
            f2 = _LOGS_DIR / f"tail_detail_{date_compact}.json"
            if f2.exists():
                data = json.loads(f2.read_text(encoding="utf-8"))
                for s in data.get("signals", []):
                    if s.get("stars", 0) < 2:
                        continue
                    signals.append({
                        "code": s["code"], "name": s["name"], "signal": "BUY_WATCH",
                        "weight_pct": {3: 3.0, 2: 2.0}.get(s.get("stars", 1), 1.5),
                        "stop_loss_pct": 6.0,
                        "composite": s.get("score", 50),
                        "note": "; ".join(s.get("triggers", [])[:3]),
                    })

    elif account == "stock":
        # 个股择时（盘中每小时运行，取最新一份）
        if session in ("morning", "all"):
            f1 = _LOGS_DIR / f"stock_timing_{today}.json"
            if f1.exists():
                stocks = json.loads(f1.read_text(encoding="utf-8"))
                for s in stocks:
                    if s.get("signal") not in ("BUY_STRONG", "BUY_WATCH", "SELL_STOP"):
                        continue
                    if s.get("position_pct", 0) <= 0 and "BUY" in s.get("signal", ""):
                        continue
                    stop_price = s.get("stop_price", 0)
                    close = s.get("close", 0)
                    stop_pct = round(abs(1 - stop_price / close) * 100, 1) if stop_price and close else 6.0
                    signals.append({
                        "code": s["code"], "name": s["name"], "signal": s["signal"],
                        "weight_pct": s.get("position_pct", 3.0),
                        "stop_loss_pct": stop_pct,
                        "composite": s.get("score", 50),
                        "note": "; ".join(s.get("reasons", [])[:3]),
                    })

    elif account == "index":
        f1 = _LOGS_DIR / f"index_timing_{today}.json"
        if f1.exists():
            data = json.loads(f1.read_text(encoding="utf-8"))
            runs = data.get("runs", [])
            if not runs:
                return signals

            # 早盘: 取 09:25-09:35 的第一个 run
            # 尾盘: 取 14:40-15:00 的最后一个 run
            # all: 取收盘后最后一个 run
            if session == "morning":
                target_runs = [r for r in runs if "09:" in r.get("time", "")]
            elif session == "afternoon":
                target_runs = [r for r in runs if r.get("time", "") >= "14:40"]
            else:
                target_runs = runs

            if not target_runs:
                target_runs = runs

            # 用最后一个匹配的 run（最新信号）
            latest_run = target_runs[-1]
            for e in latest_run.get("etfs", []):
                sig_type = e.get("signal", "HOLD")
                if sig_type == "BUY":
                    weight = min(15.0, max(5.0, e.get("composite", 50) / 10))
                    signals.append({
                        "code": e["code"], "name": e["name"], "signal": "BUY_WATCH",
                        "weight_pct": round(weight, 1),
                        "stop_loss_pct": 5.0,
                        "composite": e.get("composite", 50),
                        "note": e.get("details", {}).get("label", "指数择时信号"),
                    })
                elif sig_type == "SELL":
                    signals.append({
                        "code": e["code"], "name": e["name"], "signal": "SELL_STOP",
                        "weight_pct": 0, "stop_loss_pct": 0, "composite": 0,
                    })
                elif sig_type == "REDUCE":
                    signals.append({
                        "code": e["code"], "name": e["name"], "signal": "REDUCE",
                        "weight_pct": 0, "stop_loss_pct": 0, "composite": 0,
                    })

    return signals


# ─── 飞书推送（三账户对比卡片） ────────────────────────────────────────────────

def push_combined_summary(results: dict[str, dict], today: str, dry: bool = False) -> None:
    """推送三张独立账户卡片到飞书。"""
    webhooks = webhooks_from_env("PAPER_TRADE_WEBHOOK", _FEISHU_WEBHOOKS_DEFAULT)

    for acct in ("etf", "stock", "index"):
        r = results[acct]
        perf = r["perf"]
        summary = r["summary"]
        state = r["state"]
        label = ACCOUNTS[acct]["label"]
        nav = perf["current_nav"]
        ret = perf["total_return_pct"]
        dd = perf["max_drawdown_pct"]
        excess = perf["excess_return_pct"]
        bench = perf["benchmark_return_pct"]
        cash = state["cash"]
        positions = state["positions"]

        history = state["nav_history"]
        daily_pnl = 0
        daily_pct = 0
        if len(history) >= 2:
            daily_pnl = history[-1]["nav"] - history[-2]["nav"]
            daily_pct = (history[-1]["nav"] / history[-2]["nav"] - 1) * 100

        # ── 头部颜色 ──
        template = "green" if daily_pnl > 0 else "red" if daily_pnl < 0 else "blue"
        pnl_icon = "🚀" if daily_pct > 1 else "✨" if daily_pnl > 0 else "💧" if daily_pnl < 0 else "⏸️"

        # ── 账户概览 ──
        ops = []
        if summary["buys"]:
            ops.append(f"买入{summary['buys']}笔")
        if summary["sells"]:
            ops.append(f"卖出{summary['sells']}笔")
        if summary["stop_losses"]:
            ops.append(f"止损{summary['stop_losses']}笔")
        ops_str = "  ".join(ops) if ops else "无操作"

        position_ratio = (1 - cash / nav) * 100 if nav > 0 else 0

        overview = (
            f"{pnl_icon} **今日盈亏  {daily_pnl:+,.0f} 元  ({daily_pct:+.2f}%)**\n\n"
            f"💰 净值 **{nav:,.0f}**  ▸  现金 **{cash:,.0f}**  ▸  仓位 **{position_ratio:.0f}%**\n"
            f"📈 累计 **{ret:+.2f}%**  ▸  回撤 {dd:.2f}%  ▸  超额 {excess:+.2f}%\n"
            f"🏦 沪深300 {bench:+.2f}%  ▸  {ops_str}"
        )

        # ── 持仓明细 ──
        if not positions:
            pos_content = "**当前空仓**，等待信号入场"
        else:
            pos_lines = []
            sorted_pos = sorted(
                positions.items(),
                key=lambda x: ((_get_current_price(x[0]) or x[1]["cost_price"]) * x[1]["shares"]),
                reverse=True,
            )
            for i, (code, pos) in enumerate(sorted_pos, 1):
                price = _get_current_price(code) or pos["cost_price"]
                pnl = (price / pos["cost_price"] - 1) * 100
                mv = price * pos["shares"]
                stop_pct = pos.get("stop_pct", 6.0)
                stop_price = pos["cost_price"] * (1 - stop_pct / 100)
                distance_to_stop = pnl + stop_pct
                weight = mv / nav * 100 if nav > 0 else 0

                marker = "🟩" if pnl > 0.05 else "🟥" if pnl < -0.05 else "🟨"

                # 明日计划
                if pnl >= 8:
                    plan = "💰 止盈减仓，落袋为安"
                elif pnl >= 5:
                    plan = "🛡️ 上移止损至成本价，锁定利润"
                elif distance_to_stop <= 1.5:
                    plan = "🔥 逼近止损线！严格执行纪律"
                elif pnl < -stop_pct:
                    plan = "🚨 已触发止损，明日开盘卖出"
                elif pnl > 0:
                    plan = "💎 持有，趋势良好"
                else:
                    plan = "⏳ 持有等待，未触发止损"

                # 买入理由
                reason = pos.get("note", "") or pos.get("reason", "")
                if len(reason) > 35:
                    reason = reason[:35] + "…"
                if not reason:
                    reason = "系统择时信号"

                pos_lines.append(
                    f"**{i}. {marker} {pos['name']}**\n"
                    f"    🎯 {pos['shares']}股  ▸  占仓 {weight:.0f}%  ▸  市值 {mv:,.0f}\n"
                    f"    💵 现价 {price:.2f}  ▸  成本 {pos['cost_price']:.2f}  ▸  盈亏 **{pnl:+.2f}%**\n"
                    f"    ⛔ 止损 {stop_price:.2f}(-{stop_pct:.0f}%)  ▸  距止损 {distance_to_stop:.1f}%\n"
                    f"    💡 {reason}\n"
                    f"    ⚡ {plan}"
                )

            pos_content = "\n\n".join(pos_lines)

        # ── 今日交易记录 ──
        today_trades = [t for t in state["trades"] if t["date"] == today]
        if today_trades:
            trade_lines = ["**📋 今日交易记录**"]
            for t in today_trades:
                if t["action"] == "BUY":
                    trade_lines.append(
                        f"    🟢 **买入** {t['name']}  ▸  {t['shares']}股  ▸  "
                        f"价格 {t['price']:.2f}  ▸  金额 {t['amount']:,.0f}\n"
                        f"    　　信号: {t.get('reason', '')}"
                    )
                else:
                    pnl = t.get("pnl_pct", 0)
                    pnl_icon = "🟢" if pnl > 0 else "🔴"
                    trade_lines.append(
                        f"    {pnl_icon} **卖出** {t['name']}  ▸  {t['shares']}股  ▸  "
                        f"价格 {t['price']:.2f}  ▸  金额 {t['amount']:,.0f}\n"
                        f"    　　盈亏 **{pnl:+.2f}%**  ▸  原因: {t.get('reason', '')}"
                    )
            trade_content = "\n\n".join(trade_lines)
        else:
            trade_content = ""

        # ── 组装卡片 ──
        card_elements = [
            {"tag": "div", "text": {"tag": "lark_md", "content": overview}},
            {"tag": "hr"},
            {"tag": "div", "text": {"tag": "lark_md", "content": pos_content}},
        ]
        if trade_content:
            card_elements.append({"tag": "hr"})
            card_elements.append({"tag": "div", "text": {"tag": "lark_md", "content": trade_content}})

        card = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {
                        "tag": "plain_text",
                        "content": f"{label} ┆ 模拟盘战报 {today}",
                    },
                    "template": template,
                },
                "elements": card_elements,
            },
        }

        if dry:
            logger.info(f"[DRY] {label} 卡片:\n{json.dumps(card, ensure_ascii=False, indent=2)}")
            continue

        post_card(card, webhooks)
        import time
        time.sleep(3)  # 避免连续推送触发飞书频率限制


# ─── 主运行逻辑 ────────────────────────────────────────────────────────────────

def run_all(today: str, dry: bool = False, session: str = "all") -> None:
    """
    运行三个账户并推送对比卡片。
    session: "morning"=早盘操作, "afternoon"=尾盘操作+推送日报, "all"=全天
    """
    results = {}

    for acct in ("etf", "stock", "index"):
        state = load_state(acct)
        signals = load_signals_for_account(acct, today, session=session)

        all_codes = list(state["positions"].keys()) + [s["code"] for s in signals]
        if all_codes:
            preload_prices(list(set(all_codes)))

        summary = consume_signals(state, signals, today, account=acct)
        update_nav(state, today)
        perf = calc_performance(state)
        save_state(state)

        results[acct] = {"state": state, "summary": summary, "perf": perf}

        label = ACCOUNTS[acct]["label"]
        logger.info(
            f"[{label}] {session} | NAV={perf['current_nav']:,.0f} "
            f"买{summary['buys']} 卖{summary['sells']} 止损{summary['stop_losses']}"
        )

    # 推送卡片：尾盘和 all 模式推送，早盘仅有操作时推送
    morning_has_action = session == "morning" and any(
        r["summary"]["buys"] + r["summary"]["sells"] + r["summary"]["stop_losses"] > 0
        for r in results.values()
    )

    if session in ("afternoon", "all") or morning_has_action:
        push_combined_summary(results, today, dry=dry)


# ─── CLI ───────────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="三账户A股模拟盘")
    parser.add_argument("--dry", action="store_true", help="不推送飞书")
    parser.add_argument("--reset", action="store_true", help="重置全部账户")
    parser.add_argument("--status", action="store_true", help="查看状态")
    parser.add_argument("--force", action="store_true", help="跳过交易日检查")
    parser.add_argument("--session", choices=["morning", "afternoon", "all"], default="all",
                        help="运行时段: morning=早盘, afternoon=尾盘, all=全天")
    args = parser.parse_args()

    if args.reset:
        reset_all()
        print(f"✓ 三账户已重置，各 {INITIAL_CAPITAL:,.0f}")
        return

    if args.status:
        for acct in ("etf", "stock", "index"):
            state = load_state(acct)
            perf = calc_performance(state)
            label = ACCOUNTS[acct]["label"]
            nav = _calc_nav(state)
            print(f"\n── {label} ──")
            print(f"  净值: {nav:,.0f}  收益: {perf['total_return_pct']:+.2f}%  "
                  f"回撤: {perf['max_drawdown_pct']:.2f}%  持仓: {perf['current_positions']}只  "
                  f"交易: {perf['total_trades']}笔")
            for code, pos in state["positions"].items():
                price = _get_current_price(code) or pos["cost_price"]
                pnl = (price / pos["cost_price"] - 1) * 100
                print(f"    {pos['name']:8s} {code} {pos['shares']:>6}股 {pnl:+.1f}%")
        return

    today = date.today().isoformat()

    if not args.force and not is_trading_day():
        logger.info(f"{today} 非交易日，跳过")
        return

    run_all(today, dry=args.dry, session=args.session)


if __name__ == "__main__":
    main()
