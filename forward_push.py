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

    # 保存预判快照（供次日验证闭环：昨日预判vs今日实际）
    import json
    _pred = {
        "date": run_date,
        "slot": slot,
        "us10y_regime": fwd.us10y_regime,
        "cg_trend": fwd.cg_trend,
        "qvix_level": fwd.qvix_level,
        "bdi_signal": fwd.bdi_signal,
        "gold_oil_signal": fwd.gold_oil_signal,
        "composite_label": fwd.composite_label,
        "composite_score": fwd.composite_score,
    }
    _pred_path = Path(__file__).parent / "state" / f"forward_pred_{run_date}.json"
    _pred_path.parent.mkdir(parents=True, exist_ok=True)
    _pred_path.write_text(json.dumps(_pred, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"预判快照: {_pred_path.name}")


if __name__ == "__main__":
    main()
