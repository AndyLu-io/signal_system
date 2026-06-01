"""
周度自动复盘 + cluster 动态健康度追踪

每周末运行：
1. 调用 signal_review 生成本周后验数据
2. 用 backtest_engine 按 cluster 归因
3. 把连续负超额的 cluster 写入 state/cluster_health.json（降级标记）
4. stock_timing 主循环读取该文件，被降级的 cluster 买入门槛自动提高

飞轮效果：系统自动识别"最近不赚钱的赛道"并降低配置，无需人工干预。

用法：
    python3 signal_system/weekly_review.py          # 周度复盘
    python3 signal_system/weekly_review.py --show   # 查看当前cluster健康度
"""

import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

_STATE_DIR = Path(__file__).parent / "state"
_HEALTH_FILE = _STATE_DIR / "cluster_health.json"


def _load_health() -> dict:
    if _HEALTH_FILE.exists():
        try:
            return json.loads(_HEALTH_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_health(health: dict) -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    _HEALTH_FILE.write_text(json.dumps(health, ensure_ascii=False, indent=2), encoding="utf-8")


def run_weekly_review() -> dict:
    """跑本周后验，返回 {cluster: 本周均超额}"""
    import csv
    from collections import defaultdict

    # 找最新的 review CSV
    review_dir = Path(__file__).parent / "review"
    csvs = sorted(review_dir.glob("signals_stock_*.csv"))
    if not csvs:
        logger.warning("无 review CSV，请先运行 signal_review.py")
        return {}

    rows = list(csv.DictReader(csvs[-1].open(encoding="utf-8-sig")))

    # 只看最近 7 个交易日的买入信号
    today = datetime.today()
    week_ago = (today - timedelta(days=10)).strftime("%Y-%m-%d")

    cluster_excess: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        if r.get("signal") not in ("BUY_STRONG", "BUY_WATCH"):
            continue
        if (r.get("signal_date") or "") < week_ago:
            continue
        cluster = r.get("cluster", "")
        excess = r.get("excess_5d")
        if cluster and excess:
            cluster_excess[cluster].append(float(excess))

    result = {}
    for c, vals in cluster_excess.items():
        result[c] = sum(vals) / len(vals) if vals else 0.0

    return result


def update_health(weekly_excess: dict) -> dict:
    """更新 cluster 健康度。连续2周负超额 → 降级"""
    health = _load_health()
    today_str = datetime.today().strftime("%Y-%m-%d")

    for cluster, avg_exc in weekly_excess.items():
        if cluster not in health:
            health[cluster] = {"neg_weeks": 0, "status": "healthy", "last_update": today_str}

        if avg_exc < -0.01:  # 周均超额 < -1%
            health[cluster]["neg_weeks"] += 1
        else:
            health[cluster]["neg_weeks"] = max(0, health[cluster]["neg_weeks"] - 1)

        health[cluster]["last_update"] = today_str
        health[cluster]["last_excess"] = round(avg_exc * 100, 2)

        # 连续2周负超额 → 降级
        if health[cluster]["neg_weeks"] >= 2:
            health[cluster]["status"] = "degraded"
            logger.warning(f"🔴 {cluster} 连续{health[cluster]['neg_weeks']}周负超额，已降级")
        elif health[cluster]["neg_weeks"] == 0:
            if health[cluster]["status"] == "degraded":
                logger.info(f"🟢 {cluster} 恢复健康")
            health[cluster]["status"] = "healthy"

    _save_health(health)
    return health


def show_health():
    health = _load_health()
    if not health:
        print("无健康度数据，请先运行周度复盘")
        return
    print(f"{'Cluster':<20}{'状态':>8}{'连续负周':>8}{'上周超额':>10}{'更新日期':>12}")
    print("-" * 58)
    for c, h in sorted(health.items(), key=lambda x: x[1].get("neg_weeks", 0), reverse=True):
        status_em = "🔴" if h["status"] == "degraded" else "🟢"
        exc = h.get("last_excess", "—")
        exc_str = f"{exc:+.2f}%" if isinstance(exc, (int, float)) else "—"
        print(f"{status_em} {c:<18}{h['status']:>8}{h['neg_weeks']:>8}{exc_str:>10}{h['last_update']:>12}")


def main():
    import argparse
    p = argparse.ArgumentParser(description="周度自动复盘")
    p.add_argument("--show", action="store_true", help="查看当前cluster健康度")
    args = p.parse_args()

    if args.show:
        show_health()
        return

    logger.info("=== 周度自动复盘 ===")
    weekly = run_weekly_review()
    if not weekly:
        logger.info("本周无买入信号数据")
        return

    logger.info(f"本周 cluster 超额: {', '.join(f'{c}={v*100:+.1f}%' for c,v in sorted(weekly.items(), key=lambda x:x[1]))}")
    health = update_health(weekly)
    degraded = [c for c, h in health.items() if h["status"] == "degraded"]
    if degraded:
        logger.warning(f"降级赛道: {degraded}")
    else:
        logger.info("所有赛道健康")
    show_health()


if __name__ == "__main__":
    main()
