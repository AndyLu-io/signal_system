"""
前瞻指标独立推送脚本
定时任务入口：9:25 开盘前、14:50 尾盘前各推送一次
"""
import sys
import logging
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def main() -> None:
    from utils import is_trading_day
    from forward_indicators import calc_forward_indicators, send_forward_card

    today = datetime.today()
    if not is_trading_day(today):
        logger.info("非交易日，跳过前瞻指标推送")
        return

    run_date = today.strftime("%Y-%m-%d")
    slot = "09:25开盘前" if today.hour < 12 else "14:50尾盘前"
    logger.info(f"前瞻指标推送 [{slot}] {run_date}")

    fwd = calc_forward_indicators()
    logger.info(
        f"前瞻: {fwd.composite_score:.0f}/100 [{fwd.composite_label}] "
        f"US10Y={fwd.us10y}% [{fwd.us10y_regime}] "
        f"QVIX={fwd.qvix} BDI={fwd.bdi} 金油比={fwd.gold_oil_ratio}"
    )

    ok = send_forward_card(fwd, run_date)
    logger.info(f"推送结果: {'✓ 成功' if ok else '✗ 失败'}")


if __name__ == "__main__":
    main()
