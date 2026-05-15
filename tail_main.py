#!/usr/bin/env python3
"""
尾盘择时分析 — 每日 14:45 执行
从股池中扫描：尾盘恐慌情绪 + 技术面绝佳买点 → 飞书推送
"""

from __future__ import annotations
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import ETF_UNIVERSE, FEISHU_WEBHOOK, INDEX_CSI300, DEFENSIVE_ROTATION_POOL
import data_fetcher as df_api
import sentiment_gauge
import rotation_advisor
import tail_market_scanner as scanner
import tail_notifier

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(
            LOG_DIR / f"tail_{datetime.today().strftime('%Y%m')}.log",
            encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("tail_main")


def is_trading_day() -> bool:
    from utils import is_trading_day as _it
    return _it()


def _detect_scan_mode() -> tuple[str, float]:
    """根据当前时间判断扫描模式和量能因子。
    open  (9:25): 竞价结束，量数据无意义，vol_factor=0.05
    early (9:45): 早盘15分钟，量数据参考性低，vol_factor=0.10
    tail  (其他): 尾盘，vol_factor=0.80
    """
    now = datetime.now()
    h, m = now.hour, now.minute
    if h == 9 and m <= 35:
        return "open", 0.05
    if h == 9 and m <= 50:
        return "early", 0.10
    return "tail", 0.88


_MODE_LABEL = {"open": "竞价后买点", "early": "早盘黄金买点", "tail": "尾盘择时"}


def run() -> None:
    today_str = datetime.today().strftime("%Y-%m-%d")
    scan_mode, vol_factor = _detect_scan_mode()
    logger.info(f"════ {_MODE_LABEL[scan_mode]} {today_str} {datetime.now().strftime('%H:%M')} ════")

    if not is_trading_day():
        logger.info("非交易日，跳过")
        return

    # ── 1. 获取ETF日K线 + 三维评判所需数据 ─────────────────────────────────
    logger.info("获取ETF历史K线及市场数据...")
    etf_codes = list(ETF_UNIVERSE.keys())
    etf_prices    = df_api.get_etf_prices(etf_codes, days=120)  # MA60斜率需要65+根
    csi300        = df_api.get_index_prices(INDEX_CSI300, days=80)
    gem           = df_api.get_index_prices("399006", days=80)   # 创业板指
    breadth       = df_api.get_market_breadth()
    north_flow    = df_api.get_north_flow(days=5)
    policy_prices = df_api.get_etf_prices(["159326", "512480", "515880"], days=3)
    logger.info(f"K线加载：{sum(v is not None for v in etf_prices.values())}个")

    logger.info("获取ETF主力资金流向...")
    main_force_flows = df_api.get_etf_main_force_flow(etf_codes)
    flow_summary = {k: v for k, v in main_force_flows.items() if abs(v) >= 0.1}
    logger.info(f"主力流向（有效值）: {flow_summary}")

    # ── 2. 买点扫描 ──────────────────────────────────────────────────────────
    logger.info(f"扫描{_MODE_LABEL[scan_mode]}...")
    reading = scanner.scan_tail_market(
        ETF_UNIVERSE, etf_prices,
        main_force_flows=main_force_flows,
        vol_factor=vol_factor,
        index_hist={"000300": csi300, "399006": gem},
        defensive_pool=DEFENSIVE_ROTATION_POOL,
    )

    # 读取今日主流程评分，挂载到每个尾盘信号
    today_date_compact = datetime.today().strftime("%Y%m%d")
    main_signal_map: dict[str, dict] = {}
    _main_log = LOG_DIR / f"signal_detail_{today_date_compact}.json"
    if _main_log.exists():
        try:
            _main_data = json.loads(_main_log.read_text(encoding="utf-8"))
            for _s in _main_data.get("signals", []):
                main_signal_map[_s["code"]] = _s
        except Exception as _e:
            logger.warning(f"读取主流程评分失败: {_e}")
    else:
        logger.info("今日主流程日志不存在，跳过主流程评分挂载")

    for sig in reading.signals:
        ms = main_signal_map.get(sig.code)
        if ms:
            sig.main_composite = float(ms.get("composite", 0.0))
            sig.main_tier      = ms.get("tier", "")
            sig.main_signal    = ms.get("signal", "")

    logger.info(f"大盘情绪: {'恐慌' if reading.market_panic else '正常'} | {reading.market_note}")
    for s in reading.signals:
        logger.info(
            f"  {scanner.STAR_MAP[s.stars]} {s.name}({s.code}) "
            f"今日{s.change_pct:+.1f}% 评分{s.score:.0f} "
            f"RSI={s.rsi6:.0f} MA20偏{s.ma20_dev_pct:+.1f}%"
        )

    # ── 2.5 攻守切换三维评判 ─────────────────────────────────────────────────
    senti = sentiment_gauge.calc_market_sentiment(csi300, breadth)
    logger.info(f"情绪温度(尾盘): {senti.score:.0f}/100 [{senti.level}]")

    _north_5d: float | None = None
    _north_5d_deal_avg: float | None = None
    _north_today_deal: float | None = None
    if north_flow is not None and len(north_flow) >= 1:
        # 优先用接口直接返回的5日合计（attrs），废额度后缓存累加不可靠
        _north_5d = north_flow.attrs.get("net_buy_5d")
        try:
            deal_s = north_flow["deal_amt_billion"].dropna()
            if len(deal_s) >= 1:
                _north_today_deal = round(float(deal_s.iloc[-1]), 2)
            if len(deal_s) >= 5:
                _north_5d_deal_avg = round(float(deal_s.tail(5).mean()), 2)
        except Exception:
            pass

    _policy_chgs: list[float] = []
    for _code in ("159326", "512480", "515880"):
        _df = policy_prices.get(_code)
        if _df is None:
            _df = etf_prices.get(_code)
        if _df is not None and len(_df) >= 2:
            try:
                _pct = (float(_df["close"].iloc[-1]) - float(_df["close"].iloc[-2])) / float(_df["close"].iloc[-2]) * 100
                _policy_chgs.append(_pct)
            except Exception:
                pass
    _policy_avg: float | None = round(sum(_policy_chgs) / len(_policy_chgs), 2) if _policy_chgs else None

    rotation = rotation_advisor.calc_rotation_signal(
        senti, _north_5d, _policy_avg,
        north_5d_deal_avg=_north_5d_deal_avg,
        north_today_deal=_north_today_deal,
    )
    logger.info(
        f"攻守切换(尾盘): {rotation.strength} (分={rotation.total_score}) "
        f"情绪{rotation.sentiment_dim} 资金{rotation.capital_dim} 政策{rotation.policy_dim}"
    )
    if rotation.should_switch:
        logger.warning(f"⚠️ 切换信号触发: {rotation.label()}")

    # ── 3. 飞书推送 ──────────────────────────────────────────────────────────
    logger.info(f"推送{_MODE_LABEL[scan_mode]}信号...")
    ok = tail_notifier.send_tail_signal(reading, today_str, rotation=rotation, scan_mode=scan_mode)
    logger.info(f"推送{'成功 ✓' if ok else '失败 ✗'}")

    # ── 4. 保存日志 ──────────────────────────────────────────────────────────
    log_file = LOG_DIR / f"tail_detail_{datetime.today().strftime('%Y%m%d')}.json"
    log_file.write_text(
        json.dumps({
            "date": today_str,
            "market_panic": reading.market_panic,
            "market_change_pct": reading.market_change_pct,
            "market_note": reading.market_note,
            "signals": [
                {
                    "code": s.code, "name": s.name,
                    "change_pct": s.change_pct, "score": s.score,
                    "stars": s.stars, "rsi6": s.rsi6,
                    "ma20_dev_pct": s.ma20_dev_pct,
                    "ma60_trend_pct": s.ma60_trend_pct,
                    "main_force_flow": s.main_force_flow,
                    "consec_down_days": s.consec_down_days,
                    "main_composite": s.main_composite,
                    "main_tier": s.main_tier,
                    "main_signal": s.main_signal,
                    "triggers": s.triggers, "cautions": s.cautions,
                }
                for s in reading.signals
            ],
        }, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"════ 尾盘扫描完成 {today_str} ════\n")


if __name__ == "__main__":
    import argparse
    _parser = argparse.ArgumentParser()
    _parser.add_argument("--mode", choices=["open", "early", "tail"], default=None,
                         help="强制指定扫描模式（默认按当前时间自动判断）")
    _args = _parser.parse_args()
    if _args.mode:
        _VOL = {"open": 0.05, "early": 0.10, "tail": 0.80}
        _detect_scan_mode = lambda: (_args.mode, _VOL[_args.mode])  # noqa: E731

    try:
        run()
    except Exception as e:
        logger.exception(f"尾盘系统异常: {e}")
        try:
            import requests
            requests.post(FEISHU_WEBHOOK, json={
                "msg_type": "text",
                "content": {"text": f"⚠️ 尾盘择时异常\n{e}\n{datetime.now().strftime('%H:%M')}"},
            }, timeout=5)
        except Exception:
            pass
        sys.exit(1)
