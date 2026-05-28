"""
模拟盘绩效报告 — 周度/月度汇总报告，详细交易复盘。

用法：
  python3 signal_system/paper_trade_report.py              # 周报
  python3 signal_system/paper_trade_report.py --monthly    # 月报
  python3 signal_system/paper_trade_report.py --full       # 完整报告
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from paper_trader import load_state, calc_performance, _get_current_price, INITIAL_CAPITAL
from utils import webhooks_from_env
from feishu_pusher import post_card

logger = logging.getLogger(__name__)

_FEISHU_WEBHOOKS_DEFAULT = [
    "https://open.feishu.cn/open-apis/bot/v2/hook/5506bc09-083a-4daa-9285-cb1677d83fbd"
]


# ─── 周期筛选 ──────────────────────────────────────────────────────────────────

def _filter_period(history: list[dict], days: int) -> list[dict]:
    """取最近N天的记录。"""
    if not history:
        return []
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    return [r for r in history if r["date"] >= cutoff]


def _filter_trades(trades: list[dict], days: int) -> list[dict]:
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    return [t for t in trades if t["date"] >= cutoff]


# ─── 周期绩效计算 ──────────────────────────────────────────────────────────────

def calc_period_stats(state: dict, days: int) -> dict:
    """计算指定周期内的绩效指标。"""
    history = state["nav_history"]
    period_history = _filter_period(history, days)

    if len(period_history) < 2:
        return {"period_days": days, "data_points": len(period_history)}

    start_nav = period_history[0]["nav"]
    end_nav = period_history[-1]["nav"]
    period_return = (end_nav / start_nav - 1) * 100

    # 周期最大回撤
    peak = start_nav
    max_dd = 0.0
    for r in period_history:
        if r["nav"] > peak:
            peak = r["nav"]
        dd = (r["nav"] / peak - 1) * 100
        if dd < max_dd:
            max_dd = dd

    # 周期基准
    bench_return = 0
    b0 = period_history[0].get("benchmark_close")
    b1 = period_history[-1].get("benchmark_close")
    if b0 and b1 and b0 > 0:
        bench_return = (b1 / b0 - 1) * 100

    # 周期交易
    period_trades = _filter_trades(state["trades"], days)
    buy_count = sum(1 for t in period_trades if t["action"] == "BUY")
    sell_count = sum(1 for t in period_trades if t["action"] == "SELL")
    sell_trades = [t for t in period_trades if t["action"] == "SELL"]
    wins = [t for t in sell_trades if t.get("pnl_pct", 0) > 0]
    losses = [t for t in sell_trades if t.get("pnl_pct", 0) < 0]

    win_rate = len(wins) / len(sell_trades) * 100 if sell_trades else 0
    avg_win = sum(t["pnl_pct"] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t["pnl_pct"] for t in losses) / len(losses) if losses else 0

    # 日均收益波动
    daily_returns = []
    for i in range(1, len(period_history)):
        prev = period_history[i - 1]["nav"]
        curr = period_history[i]["nav"]
        if prev > 0:
            daily_returns.append((curr / prev - 1) * 100)

    avg_daily = sum(daily_returns) / len(daily_returns) if daily_returns else 0
    std_daily = (
        (sum((r - avg_daily) ** 2 for r in daily_returns) / len(daily_returns)) ** 0.5
        if daily_returns else 0
    )
    sharpe = (avg_daily / std_daily * (252 ** 0.5)) if std_daily > 0 else 0

    return {
        "period_days": days,
        "data_points": len(period_history),
        "period_return_pct": round(period_return, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "benchmark_return_pct": round(bench_return, 2),
        "excess_return_pct": round(period_return - bench_return, 2),
        "buy_count": buy_count,
        "sell_count": sell_count,
        "win_rate_pct": round(win_rate, 1),
        "avg_win_pct": round(avg_win, 2),
        "avg_loss_pct": round(avg_loss, 2),
        "sharpe_ratio": round(sharpe, 2),
        "daily_volatility_pct": round(std_daily, 3),
    }


# ─── 持仓分析 ──────────────────────────────────────────────────────────────────

def position_analysis(state: dict) -> list[dict]:
    """当前持仓的详细分析。"""
    result = []
    for code, pos in state["positions"].items():
        price = _get_current_price(code) or pos["cost_price"]
        pnl_pct = (price / pos["cost_price"] - 1) * 100
        market_value = price * pos["shares"]
        hold_days = (date.today() - date.fromisoformat(pos["buy_date"])).days

        result.append({
            "code": code,
            "name": pos["name"],
            "shares": pos["shares"],
            "cost_price": pos["cost_price"],
            "current_price": price,
            "pnl_pct": round(pnl_pct, 2),
            "market_value": round(market_value, 2),
            "hold_days": hold_days,
            "stop_pct": pos["stop_pct"],
            "distance_to_stop_pct": round(pnl_pct + pos["stop_pct"], 2),
        })

    return sorted(result, key=lambda x: x["pnl_pct"], reverse=True)


# ─── 交易复盘 ──────────────────────────────────────────────────────────────────

def trade_review(state: dict, days: int = 7) -> dict:
    """最近N天的交易复盘。"""
    trades = _filter_trades(state["trades"], days)
    if not trades:
        return {"period_days": days, "trades": []}

    # 最佳/最差交易
    sell_trades = [t for t in trades if t["action"] == "SELL" and "pnl_pct" in t]
    best = max(sell_trades, key=lambda t: t["pnl_pct"]) if sell_trades else None
    worst = min(sell_trades, key=lambda t: t["pnl_pct"]) if sell_trades else None

    # 止损统计
    stop_trades = [t for t in sell_trades if "止损" in t.get("reason", "")]

    return {
        "period_days": days,
        "total_trades": len(trades),
        "best_trade": best,
        "worst_trade": worst,
        "stop_loss_count": len(stop_trades),
        "recent_trades": trades[-10:],  # 最近10笔
    }


# ─── 报告生成 ──────────────────────────────────────────────────────────────────

def generate_report_text(state: dict, period: str = "weekly") -> str:
    """生成文本报告。"""
    days = {"weekly": 7, "monthly": 30, "full": 9999}[period]
    period_label = {"weekly": "周", "monthly": "月", "full": "全部"}[period]

    perf = calc_performance(state)
    period_stats = calc_period_stats(state, days)
    positions = position_analysis(state)
    review = trade_review(state, days)

    lines = [
        f"═══ 模拟盘{period_label}报 ═══",
        f"日期: {date.today().isoformat()}",
        f"初始资金: {INITIAL_CAPITAL:,.0f}",
        f"当前净值: {perf.get('current_nav', 0):,.0f}",
        "",
        f"── 累计绩效 ──",
        f"  总收益率:   {perf.get('total_return_pct', 0):+.2f}%",
        f"  最大回撤:   {perf.get('max_drawdown_pct', 0):.2f}%",
        f"  沪深300:    {perf.get('benchmark_return_pct', 0):+.2f}%",
        f"  超额收益:   {perf.get('excess_return_pct', 0):+.2f}%",
        f"  胜率:       {perf.get('win_rate_pct', 0):.0f}%",
        f"  盈亏比:     {perf.get('profit_factor', 0):.2f}",
        f"  跟踪天数:   {perf.get('days_tracked', 0)}",
    ]

    if period_stats.get("data_points", 0) >= 2:
        lines += [
            "",
            f"── 本{period_label}绩效 ──",
            f"  收益率:     {period_stats.get('period_return_pct', 0):+.2f}%",
            f"  最大回撤:   {period_stats.get('max_drawdown_pct', 0):.2f}%",
            f"  超额:       {period_stats.get('excess_return_pct', 0):+.2f}%",
            f"  Sharpe:     {period_stats.get('sharpe_ratio', 0):.2f}",
            f"  买入:       {period_stats.get('buy_count', 0)}笔",
            f"  卖出:       {period_stats.get('sell_count', 0)}笔",
            f"  胜率:       {period_stats.get('win_rate_pct', 0):.0f}%",
        ]

    if positions:
        lines += ["", f"── 当前持仓 ({len(positions)}只) ──"]
        for p in positions:
            flag = "+" if p["pnl_pct"] > 0 else ""
            lines.append(
                f"  {p['name']:8s} {p['code']}  {flag}{p['pnl_pct']:.1f}%  "
                f"持{p['hold_days']}天  距止损{p['distance_to_stop_pct']:.1f}%"
            )

    if review.get("best_trade"):
        lines += ["", f"── 本{period_label}最佳/最差 ──"]
        bt = review["best_trade"]
        lines.append(f"  最佳: {bt['name']} +{bt['pnl_pct']:.1f}%")
        if review.get("worst_trade"):
            wt = review["worst_trade"]
            lines.append(f"  最差: {wt['name']} {wt['pnl_pct']:.1f}%")
        lines.append(f"  止损次数: {review.get('stop_loss_count', 0)}")

    return "\n".join(lines)


def push_weekly_report(state: dict, dry: bool = False) -> None:
    """推送周报到飞书。"""
    perf = calc_performance(state)
    week_stats = calc_period_stats(state, 7)
    positions = position_analysis(state)

    pos_text = ""
    for p in positions[:8]:
        marker = "🟢" if p["pnl_pct"] > 0 else "🔴"
        pos_text += f"{marker} {p['name']} {p['pnl_pct']:+.1f}% (持{p['hold_days']}天)\n"

    week_ret = week_stats.get("period_return_pct", 0)
    total_ret = perf.get("total_return_pct", 0)

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"📈 模拟盘周报 {date.today().isoformat()}"},
                "template": "blue",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**本周收益**: {week_ret:+.2f}%  |  "
                            f"**累计收益**: {total_ret:+.2f}%\n"
                            f"**最大回撤**: {perf.get('max_drawdown_pct', 0):.2f}%  |  "
                            f"**Sharpe**: {week_stats.get('sharpe_ratio', 0):.2f}\n"
                            f"**超额(vs沪深300)**: {perf.get('excess_return_pct', 0):+.2f}%\n"
                            f"**胜率**: {perf.get('win_rate_pct', 0):.0f}%  |  "
                            f"**盈亏比**: {perf.get('profit_factor', 0):.1f}  |  "
                            f"**交易**: {week_stats.get('buy_count', 0)}买 "
                            f"{week_stats.get('sell_count', 0)}卖"
                        ),
                    },
                },
                {"tag": "hr"},
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"**持仓** ({len(positions)}只)\n{pos_text}" if pos_text else "空仓",
                    },
                },
            ],
        },
    }

    if dry:
        logger.info(f"[DRY] 周报卡片:\n{json.dumps(card, ensure_ascii=False, indent=2)}")
        return

    webhooks = webhooks_from_env("PAPER_TRADE_WEBHOOK", _FEISHU_WEBHOOKS_DEFAULT)
    post_card(card, webhooks)


# ─── 主入口 ────────────────────────────────────────────────────────────────────

def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="模拟盘绩效报告")
    parser.add_argument("--weekly", action="store_true", help="周报（默认）")
    parser.add_argument("--monthly", action="store_true", help="月报")
    parser.add_argument("--full", action="store_true", help="完整报告")
    parser.add_argument("--push", action="store_true", help="推送到飞书")
    parser.add_argument("--dry", action="store_true", help="不推送")
    args = parser.parse_args()

    state = load_state()

    if args.monthly:
        period = "monthly"
    elif args.full:
        period = "full"
    else:
        period = "weekly"

    report = generate_report_text(state, period)
    print(report)

    if args.push:
        push_weekly_report(state, dry=args.dry)


if __name__ == "__main__":
    main()
