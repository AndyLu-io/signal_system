"""
组合回测引擎

不同于 signal_review.py（只算单条信号的裸前向收益），本引擎把信号序列
重放成一个真实组合：按 position_pct 建仓、扣交易成本、按 stop_loss 止损、
到期/反向信号平仓，输出资金曲线、最大回撤、夏普，以及对基准的超额。

目的：在调任何买卖阈值之前，先有一个能区分「赛道 beta」与「择时 alpha」、
且不只看牛市裸收益的评估底座。

数据来源：signal_review.py 生成的 review/signals_{type}_{tag}.csv
（已含 signal_date / code / signal / position_pct / stop_loss_pct /
 close_t / close_t{1,3,5,10} / ret_*d / ret_csi300_*d / hit_stop_10d）

用法：
    python3 signal_system/backtest_engine.py --type stock
    python3 signal_system/backtest_engine.py --type etf --hold 5
    python3 signal_system/backtest_engine.py --type stock --cost 0.0015 --hold 5
    python3 signal_system/backtest_engine.py --type stock --group cluster
"""

from __future__ import annotations

import argparse
import csv
import logging
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

_ROOT = Path(__file__).parent
_REVIEW_DIR = _ROOT / "review"

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# 买入类信号才建仓；其余（HOLD/REDUCE/SELL_STOP/AVOID）不主动开新仓
_BUY_SIGNALS = {"BUY_STRONG", "BUY_WATCH", "TAIL_1STAR", "TAIL_2STAR", "TAIL_3STAR"}
# 单笔默认仓位（当 CSV 无 position_pct 时的兜底）
_DEFAULT_POS_PCT = 10.0
# 单笔交易往返成本（佣金+滑点+印花，双边）默认 15bp
_DEFAULT_COST = 0.0015
# 年化无风险利率（算夏普用）
_RF_ANNUAL = 0.018
# A股年交易日
_TRADING_DAYS = 244


# ─── 数据结构 ────────────────────────────────────────────────────────────────

@dataclass
class Trade:
    code: str
    name: str
    signal_date: str
    signal: str
    group_key: str          # 用于分组归因（cluster/theme/signal...）
    weight: float           # 实际占组合权重（0~1）
    ret: Optional[float]    # 持有期裸收益（已扣成本）
    ret_bench: Optional[float]  # 同期基准收益
    excess: Optional[float]     # 超额
    hit_stop: bool


@dataclass
class GroupStat:
    key: str
    n: int = 0
    rets: list = field(default_factory=list)
    excess: list = field(default_factory=list)
    stops: int = 0


# ─── 读取 CSV ────────────────────────────────────────────────────────────────

def _to_float(v: str) -> Optional[float]:
    if v is None or v == "":
        return None
    try:
        return float(v)
    except ValueError:
        return None


def load_signals(signal_type: str, tag: str) -> list[dict]:
    """读取 review CSV。tag 形如 202601；找不到精确 tag 时取该 type 最新一份。"""
    exact = _REVIEW_DIR / f"signals_{signal_type}_{tag}.csv"
    if exact.exists():
        path = exact
    else:
        candidates = sorted(_REVIEW_DIR.glob(f"signals_{signal_type}_*.csv"))
        if not candidates:
            raise FileNotFoundError(f"未找到 {signal_type} 的 review CSV，请先运行 signal_review.py")
        path = candidates[-1]
        logger.info(f"未找到 tag={tag}，使用最新文件 {path.name}")

    rows: list[dict] = []
    with path.open(encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            rows.append(row)
    logger.info(f"读取 {path.name}：{len(rows)} 条信号")
    return rows


# ─── 组合回测核心 ─────────────────────────────────────────────────────────────

def run_backtest(
    rows: list[dict],
    hold_days: int,
    cost: float,
    group_field: str,
    next_day_fill: bool = False,
    liq_penalty: float = 0.0,
) -> tuple[list[Trade], dict[str, list[float]]]:
    """
    把买入信号重放为组合。

    简化假设（透明声明，便于后续升级）：
    - 默认在 signal_date 收盘建仓；next_day_fill=True 时改用次日收盘价 close_t1
      建仓（模拟 A股 T+1：信号日收盘到次日才能买，隔夜跳空全暴露），收益相应改为
      close_t{hold}/close_t1 - 1，更贴近实盘可执行收益。
    - 持有 hold_days 个交易日后平仓；hold_days==10 且触止损时按 -stop_pct 截断。
    - 往返成本 cost 从每笔收益扣除；liq_penalty 对低价/小盘(watch池)标的叠加额外
      单边滑点惩罚（冲击成本），300万账户打中小盘滑点远超指数。
    - 组合权重 = position_pct/100；同日多信号按权重并行持有。
    - 日度资金曲线按"建仓日归因"近似，用于回撤/夏普。

    返回 (trades, equity_by_date)
    """
    ret_field = f"ret_{hold_days}d"
    bench_field = f"ret_csi300_{hold_days}d"

    trades: list[Trade] = []
    # 日度组合收益贡献：date -> [加权收益片段]
    daily_port: dict[str, float] = defaultdict(float)
    daily_bench: dict[str, float] = defaultdict(float)

    for r in rows:
        signal = r.get("signal", "")
        if signal not in _BUY_SIGNALS:
            continue

        # ── 建仓基准：默认信号日收盘；T+1 模式用次日收盘 close_t1 ──────────────
        if next_day_fill:
            base = _to_float(r.get("close_t1"))
            close_n = _to_float(r.get(f"close_t{hold_days}"))
            if base is None or base <= 0 or close_n is None:
                continue  # 次日或持有期价格缺失，跳过
            ret = close_n / base - 1
        else:
            ret = _to_float(r.get(ret_field))
            if ret is None:
                continue  # 持有期未到，跳过（不计入已实现）

        bench = _to_float(r.get(bench_field))
        pos_pct = _to_float(r.get("position_pct"))
        weight = (pos_pct if pos_pct and pos_pct > 0 else _DEFAULT_POS_PCT) / 100.0

        stop_pct = _to_float(r.get("stop_loss_pct"))
        hit_stop = (r.get("hit_stop_10d", "").lower() == "true")
        # 止损截断只在持有期与止损窗口口径一致时应用：
        # hit_stop_10d 是 10 日窗口事件，用它截断 <10 日的持有收益会高估止损损失，
        # 因此仅当 hold_days==10 时才把触止损的收益截断到 -stop_pct。
        if hold_days == 10 and hit_stop and stop_pct is not None:
            ret = min(ret, -stop_pct / 100.0)

        # ── 流动性惩罚：低价/小盘(watch池)叠加额外单边滑点 ────────────────────
        trade_cost = cost
        if liq_penalty > 0:
            close_t = _to_float(r.get("close_t")) or 0.0
            pool = str(r.get("pool") or "")
            if pool == "watch" or (0 < close_t < 20):
                trade_cost += liq_penalty

        net_ret = ret - trade_cost  # 扣往返成本（含流动性惩罚）

        gkey = str(r.get(group_field) or "unknown")
        excess = (net_ret - bench) if bench is not None else None

        trades.append(Trade(
            code=r.get("code", ""),
            name=r.get("name", ""),
            signal_date=r.get("signal_date", ""),
            signal=signal,
            group_key=gkey,
            weight=weight,
            ret=net_ret,
            ret_bench=bench,
            excess=excess,
            hit_stop=hit_stop,
        ))

        # 资金曲线归因：把这笔加权收益记在建仓日
        sd = r.get("signal_date", "")
        daily_port[sd] += weight * net_ret
        if bench is not None:
            daily_bench[sd] += weight * bench

    # 构造日度资金曲线：加权(按weight归一化) vs 等权(简单平均)，同口径可公平对比
    all_dates = sorted(set(daily_port) | set(daily_bench))
    equity = {"dates": all_dates, "port": [], "bench": [], "naive": []}
    cum_p, cum_b, cum_n = 1.0, 1.0, 1.0

    # 按建仓日聚合：加权用 weight 归一化，等权用算术平均（同口径=日度组合收益率）
    day_weight: dict[str, float] = defaultdict(float)   # 当日权重之和（用于归一化）
    day_wret: dict[str, float] = defaultdict(float)     # 当日 sum(weight*ret)
    day_nret: dict[str, float] = defaultdict(float)     # 当日 sum(ret)，等权用
    day_count: dict[str, int] = defaultdict(int)
    day_bench: dict[str, float] = defaultdict(float)    # 当日基准（每笔相同，取一次即可）
    for t in trades:
        sd = t.signal_date
        if t.ret is None:
            continue
        day_weight[sd] += t.weight
        day_wret[sd]   += t.weight * t.ret
        day_nret[sd]   += t.ret
        day_count[sd]  += 1
        if t.ret_bench is not None:
            day_bench[sd] = t.ret_bench  # 同日基准一致

    for d in all_dates:
        cnt = day_count.get(d, 0)
        if cnt == 0:
            continue
        # 加权组合日收益率：sum(w*ret)/sum(w)（权重归一化，避免被仓位绝对值缩放）
        wsum = day_weight.get(d, 0.0)
        rp = (day_wret[d] / wsum) if wsum > 0 else 0.0
        # 等权组合日收益率：算术平均
        rn = day_nret[d] / cnt
        rb = day_bench.get(d, 0.0)
        cum_p *= (1 + rp)
        cum_n *= (1 + rn)
        cum_b *= (1 + rb)
        equity["port"].append(cum_p)
        equity["naive"].append(cum_n)
        equity["bench"].append(cum_b)

    return trades, equity


# ─── 指标计算 ─────────────────────────────────────────────────────────────────

def _max_drawdown(curve: list[float]) -> float:
    peak = -math.inf
    mdd = 0.0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            mdd = min(mdd, v / peak - 1)
    return mdd


def _sharpe(daily_rets: list[float]) -> Optional[float]:
    if len(daily_rets) < 2:
        return None
    mean = sum(daily_rets) / len(daily_rets)
    var = sum((x - mean) ** 2 for x in daily_rets) / (len(daily_rets) - 1)
    sd = math.sqrt(var)
    if sd == 0:
        return None
    rf_daily = _RF_ANNUAL / _TRADING_DAYS
    return (mean - rf_daily) / sd * math.sqrt(_TRADING_DAYS)


def _daily_returns(curve: list[float]) -> list[float]:
    out = []
    prev = 1.0
    for v in curve:
        out.append(v / prev - 1)
        prev = v
    return out


def summarize(trades: list[Trade], equity: dict) -> dict:
    rets = [t.ret for t in trades if t.ret is not None]
    excess = [t.excess for t in trades if t.excess is not None]
    n = len(rets)

    port_curve = equity["port"]
    bench_curve = equity["bench"]

    total_ret = (port_curve[-1] - 1) if port_curve else 0.0
    bench_ret = (bench_curve[-1] - 1) if bench_curve else 0.0

    # 朴素等权对照曲线（同口径日度复利），weight_edge = 加权 - 等权，正值=加权有增量价值
    naive_curve = equity.get("naive", [])
    naive_ret = (naive_curve[-1] - 1) if naive_curve else 0.0
    weight_edge = total_ret - naive_ret

    return {
        "n_trades": n,
        "win_rate": (sum(1 for x in rets if x > 0) / n) if n else None,
        "avg_ret": (sum(rets) / n) if n else None,
        "avg_excess": (sum(excess) / len(excess)) if excess else None,
        "naive_total_ret": naive_ret if naive_curve else None,
        "weight_edge": weight_edge if naive_curve else None,
        "port_total_ret": total_ret,
        "bench_total_ret": bench_ret,
        "alpha_total": total_ret - bench_ret,
        "max_drawdown": _max_drawdown(port_curve) if port_curve else None,
        "bench_max_dd": _max_drawdown(bench_curve) if bench_curve else None,
        "sharpe": _sharpe(_daily_returns(port_curve)) if port_curve else None,
        "stop_hits": sum(1 for t in trades if t.hit_stop),
    }


def group_attribution(trades: list[Trade]) -> list[GroupStat]:
    groups: dict[str, GroupStat] = {}
    for t in trades:
        g = groups.setdefault(t.group_key, GroupStat(key=t.group_key))
        g.n += 1
        if t.ret is not None:
            g.rets.append(t.ret)
        if t.excess is not None:
            g.excess.append(t.excess)
        if t.hit_stop:
            g.stops += 1
    return sorted(groups.values(), key=lambda x: (sum(x.excess) / len(x.excess)) if x.excess else 0, reverse=True)


# ─── 输出 ────────────────────────────────────────────────────────────────────

def _pct(v: Optional[float]) -> str:
    return f"{v * 100:+.2f}%" if v is not None else "—"


def _rate(v: Optional[float]) -> str:
    return f"{v * 100:.1f}%" if v is not None else "—"


def print_report(signal_type: str, hold: int, cost: float, s: dict,
                 groups: list[GroupStat], group_field: str) -> None:
    print(f"\n{'='*64}")
    print(f"组合回测：{signal_type}  持有{hold}日  往返成本{cost*100:.2f}%")
    print(f"{'='*64}")
    print("⚠️ 口径声明：研究池(STOCK_UNIVERSE)由季报事后选出的赢家构成，存在")
    print("   幸存者偏差——回测胜率/超额天然虚高，真实环境无法提前一季持有这批票。")
    print("   结论仅供相对比较(信号A vs B/赛道间)，绝对收益不可外推为实盘预期。")
    print(f"{'-'*64}")
    print(f"成交笔数        : {s['n_trades']}")
    print(f"胜率            : {_rate(s['win_rate'])}")
    print(f"单笔均收(扣费)  : {_pct(s['avg_ret'])}")
    print(f"单笔均超额      : {_pct(s['avg_excess'])}")
    print(f"止损触发        : {s['stop_hits']}")
    print(f"{'-'*64}")
    print(f"组合累计收益    : {_pct(s['port_total_ret'])}")
    print(f"基准累计收益    : {_pct(s['bench_total_ret'])}")
    print(f"等权组合累计    : {_pct(s.get('naive_total_ret'))}  (无脑等权分散对照,同口径)")
    print(f"仓位加权增量    : {_pct(s.get('weight_edge'))}  (>0=position_pct加权优于等权)")
    print(f"累计超额(alpha) : {_pct(s['alpha_total'])}")
    print(f"组合最大回撤    : {_pct(s['max_drawdown'])}")
    print(f"基准最大回撤    : {_pct(s['bench_max_dd'])}")
    print(f"夏普(年化)      : {s['sharpe']:.2f}" if s['sharpe'] is not None else "夏普(年化)      : —")
    print(f"{'-'*64}")
    print(f"按 {group_field} 归因（超额降序，仅显示样本≥5）：")
    print(f"{'分组':<22}{'笔数':>5}{'均收':>9}{'均超额':>9}{'止损':>5}")
    for g in groups:
        if g.n < 5:
            continue
        avg_r = sum(g.rets) / len(g.rets) if g.rets else None
        avg_e = sum(g.excess) / len(g.excess) if g.excess else None
        print(f"{g.key:<22}{g.n:>5}{_pct(avg_r):>9}{_pct(avg_e):>9}{g.stops:>5}")
    print(f"{'='*64}\n")


# ─── 主入口 ──────────────────────────────────────────────────────────────────

def _run_and_print(rows, args, title_suffix=""):
    trades, equity = run_backtest(rows, args.hold, args.cost, args.group,
                                  next_day_fill=args.next_open, liq_penalty=args.liq_penalty)
    s = summarize(trades, equity)
    groups = group_attribution(trades)
    print_report(args.type + title_suffix, args.hold, args.cost, s, groups, args.group)
    return s


def main() -> None:
    p = argparse.ArgumentParser(description="组合回测引擎")
    p.add_argument("--type", default="stock", choices=["stock", "etf", "tail"],
                   help="信号类型（默认 stock）")
    p.add_argument("--tag", default="202601", help="review CSV 标签 YYYYMM（默认 202601）")
    p.add_argument("--hold", type=int, default=5, choices=[1, 3, 5, 10],
                   help="持有交易日（默认 5）")
    p.add_argument("--cost", type=float, default=_DEFAULT_COST,
                   help=f"往返交易成本（默认 {_DEFAULT_COST}）")
    p.add_argument("--group", default="cluster",
                   help="归因分组字段（cluster/theme/signal/signal_3d/regime，默认 cluster）")
    p.add_argument("--oos", action="store_true",
                   help="walk-forward 样本外验证：按 signal_date 中位切成 IS/OOS 两段对比，检测过拟合")
    p.add_argument("--next-open", dest="next_open", action="store_true",
                   help="次日收盘建仓（模拟A股T+1，含隔夜跳空暴露），更贴近实盘")
    p.add_argument("--liq-penalty", dest="liq_penalty", type=float, default=0.0,
                   help="低价/小盘(watch池)额外单边滑点惩罚，如 0.003=30bp")
    args = p.parse_args()

    rows = load_signals(args.type, args.tag)

    if not args.oos:
        _run_and_print(rows, args)
        return

    # walk-forward：按 signal_date 时间中位切分
    dates = sorted({r.get("signal_date", "") for r in rows if r.get("signal_date")})
    if len(dates) < 4:
        logger.warning("样本日期不足，无法做 IS/OOS 切分")
        _run_and_print(rows, args)
        return
    split = dates[len(dates) // 2]
    is_rows = [r for r in rows if r.get("signal_date", "") < split]
    oos_rows = [r for r in rows if r.get("signal_date", "") >= split]
    logger.info(f"OOS 切分点={split}  训练段(IS) {len(is_rows)} 条 / 测试段(OOS) {len(oos_rows)} 条")
    s_is = _run_and_print(is_rows, args, "[IS训练段]")
    s_oos = _run_and_print(oos_rows, args, "[OOS测试段]")
    # 过拟合提示
    if s_is.get("avg_excess") is not None and s_oos.get("avg_excess") is not None:
        gap = s_is["avg_excess"] - s_oos["avg_excess"]
        verdict = "⚠️ IS远好于OOS，疑似过拟合" if gap > 0.02 else "✓ IS/OOS一致性尚可"
        print(f"样本外检验：IS均超额={_pct(s_is['avg_excess'])} OOS均超额={_pct(s_oos['avg_excess'])} 差={_pct(gap)} → {verdict}\n")


if __name__ == "__main__":
    main()
