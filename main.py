#!/usr/bin/env python3
"""
A股ETF信号系统 V4.0 - 每日主程序
运行时机：工作日 08:30（launchd/cron 调度）
"""

from __future__ import annotations
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

# 确保 signal_system 目录在路径中
sys.path.insert(0, str(Path(__file__).parent))

from config import ETF_UNIVERSE, INDEX_CSI300, INDEX_CSI500
import data_fetcher as df_api
import regime_engine
import factor_scorer
import signal_generator
import sentiment_gauge
import rotation_advisor
import notifier
import etf_reversal
from data_health import check_data_health
from tail_market_scanner import detect_cluster_panic, detect_offense_surge
import forward_indicators

# ─── 日志配置 ─────────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / f"signal_{datetime.today().strftime('%Y%m')}.log",
                            encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("main")

# ─── 当前持仓（手动维护，每日开盘前更新） ──────────────────────────────────────
# 格式：{ETF代码: 成本价}，未持仓的不填
# 每天需要人工更新这里，或通过外部 JSON 文件维护
POSITIONS_FILE = Path(__file__).parent / "state" / "positions.json"


def load_positions() -> dict[str, float]:
    """从本地文件加载当前持仓成本价"""
    if POSITIONS_FILE.exists():
        try:
            data = json.loads(POSITIONS_FILE.read_text(encoding="utf-8"))
            return {str(k): float(v) for k, v in data.items()}
        except Exception as e:
            logger.warning(f"持仓文件读取失败: {e}")
    return {}


def is_trading_day() -> bool:
    """委托 utils.is_trading_day（含 config.HOLIDAY_BLACKLIST 节假日判断）。"""
    from utils import is_trading_day as _it
    return _it()


def save_daily_log(data: dict) -> None:
    """保存当日信号到 JSON 日志"""
    today = datetime.today().strftime("%Y%m%d")
    log_file = LOG_DIR / f"signal_detail_{today}.json"
    log_file.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    logger.info(f"信号详情已保存: {log_file}")


def run() -> None:
    today_str = datetime.today().strftime("%Y-%m-%d")
    logger.info(f"════ V4.0 信号系统启动 {today_str} ════")

    # ── 0. 非交易日跳过 ─────────────────────────────────────────────────────
    if not is_trading_day():
        logger.info("今日为非工作日，跳过运行")
        return

    # ── 1. 数据获取 ──────────────────────────────────────────────────────────
    logger.info("Step 1/5: 获取市场数据...")
    etf_codes = list(ETF_UNIVERSE.keys())

    etf_prices = df_api.get_etf_prices(etf_codes, days=40)
    csi300     = df_api.get_index_prices(INDEX_CSI300, days=40)
    csi500     = df_api.get_index_prices(INDEX_CSI500, days=40)
    north_flow = df_api.get_north_flow(days=15)
    margin     = df_api.get_margin_balance(days=10)
    main_force    = df_api.get_etf_main_force_flow(etf_codes)
    breadth       = df_api.get_market_breadth()
    policy_prices = df_api.get_etf_prices(["159326", "512480", "515880"], days=3)

    # 把指数价格加入 etf_prices 供 Momentum 计算使用
    if csi300 is not None:
        etf_prices["000300"] = csi300

    logger.info(f"ETF数据：{sum(v is not None for v in etf_prices.values())}个成功")
    logger.info(f"北向数据：{'OK' if north_flow is not None else '失败（降级）'}")
    logger.info(f"两融数据：{'OK' if margin is not None else '失败（降级）'}")
    logger.info(f"市场宽度：涨停{breadth.get('limit_up',0)}家 跌停{breadth.get('limit_down',0)}家")

    data_health = check_data_health(
        etf_prices=etf_prices,
        etf_codes=etf_codes,
        csi300=csi300,
        north_flow=north_flow,
        main_force=main_force,
    )
    logger.info(f"数据健康: {data_health.confidence} — ETF {data_health.etf_ok}/{data_health.etf_total} 北向{data_health.north_precision}")

    # ── 2. 市场机制判断 ──────────────────────────────────────────────────────
    logger.info("Step 2/5: 计算市场机制...")
    regime_score = regime_engine.calculate_regime_score(
        csi300, csi500, north_flow, margin, etf_prices
    )
    regime, regime_state = regime_engine.determine_regime(regime_score["total"])
    logger.info(f"机制评分: {regime_score['total']} → 确认机制: {regime}")

    # 机制切换提示
    pending = regime_state.get("pending_regime")
    if pending:
        days = regime_state.get("pending_days", 0)
        logger.info(f"待切换: {regime} → {pending}（{days}日待确认）")

    # ── 2.5 情绪温度计 ───────────────────────────────────────────────────────
    logger.info("Step 2.5/5: 计算市场情绪温度...")
    sentiment = sentiment_gauge.calc_market_sentiment(csi300, breadth)
    logger.info(
        f"情绪温度: {sentiment.score:.0f}/100 [{sentiment.level}] "
        f"连涨{sentiment.consec_up_days}日 MA5偏{sentiment.ma5_dev_pct:+.1f}% "
        f"RSI(6)={sentiment.rsi6:.0f} | {sentiment.note}"
    )
    if sentiment.timing_block:
        logger.warning("⚠️ 情绪过热：买入信号将自动降级，禁止追高！")

    # ── 2.6 攻守切换三维评判 ─────────────────────────────────────────────────
    # 北向资金：优先用接口直接返回的多周期合计（attrs），废额度后缓存累加不可靠
    north_5d_billion: float | None = None
    north_5d_deal_avg: float | None = None
    north_today_deal: float | None = None
    if north_flow is not None and len(north_flow) >= 1:
        # 优先用接口直接返回的5日合计
        north_5d_billion = north_flow.attrs.get("net_buy_5d")
        try:
            deal_series = north_flow["deal_amt_billion"].dropna()
            if len(deal_series) >= 1:
                north_today_deal = round(float(deal_series.iloc[-1]), 2)
            if len(deal_series) >= 5:
                north_5d_deal_avg = round(float(deal_series.tail(5).mean()), 2)
        except Exception:
            pass

    # 政策板块代理：电网设备/半导体/光通信 当日涨跌幅均值
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
    policy_sector_avg_chg: float | None = round(sum(_policy_chgs) / len(_policy_chgs), 2) if _policy_chgs else None

    rotation = rotation_advisor.calc_rotation_signal(
        sentiment, north_5d_billion, policy_sector_avg_chg,
        north_5d_deal_avg=north_5d_deal_avg,
        north_today_deal=north_today_deal,
    )
    logger.info(
        f"攻守切换: {rotation.strength} (分={rotation.total_score}) "
        f"情绪{rotation.sentiment_dim} 资金{rotation.capital_dim} 政策{rotation.policy_dim}"
    )
    if rotation.should_switch:
        logger.warning(f"⚠️ 切换信号触发: {rotation.label()}")

    # ── 3. 五因子评分 ────────────────────────────────────────────────────────
    logger.info("Step 3/5: 计算五因子评分...")
    factor_scores = factor_scorer.calc_composite_scores(
        codes=etf_codes,
        regime=regime,
        etf_prices=etf_prices,
        north_flow=north_flow,
        main_force=main_force,
    )

    for code, scores in sorted(factor_scores.items(),
                               key=lambda x: x[1]["composite"], reverse=True):
        name = ETF_UNIVERSE[code]["name"]
        logger.info(f"  {name}({code}): {scores['composite']} | {scores['tier']}级")

    # ── 4. 信号生成 ──────────────────────────────────────────────────────────
    logger.info("Step 4/5: 生成操作信号...")
    positions = load_positions()
    timing_risks = factor_scorer.calc_etf_timing_risks(etf_codes, etf_prices)
    signals = signal_generator.generate_signals(
        regime=regime,
        factor_scores=factor_scores,
        etf_prices=etf_prices,
        current_positions=positions,
        sentiment=sentiment,
        timing_risks=timing_risks,
    )
    risk_summary = signal_generator.calc_daily_risk_summary(regime)

    # 汇总输出
    for s in signals:
        if s["signal"] not in ("AVOID",):
            logger.info(f"  {s['signal']:12s} {s['name']}({s['code']}) "
                        f"评分{s['composite']} {s['tier']}级 仓位{s['weight_pct']}%")

    # ── 4.5 ETF底部反转候选检测 ───────────────────────────────────────────────
    logger.info("Step 4.5/5: 检测ETF底部反转候选...")
    etf_prices_long = df_api.get_etf_prices(etf_codes, days=300)
    north_5d_val: float | None = north_5d_billion  # 已在步骤2.6中计算

    reversals: list[dict] = []
    for code, info in ETF_UNIVERSE.items():
        df_long = etf_prices_long.get(code)
        if df_long is None:
            continue
        flow = main_force.get(code, 0.0)
        rev = etf_reversal.check_reversal(
            code=code,
            df=df_long,
            info=info,
            main_force_flow=flow,
            north_5d=north_5d_val,
            sentiment=sentiment,
        )
        if rev is not None:
            reversals.append(rev)
            logger.info(f"  ETF反转候选: {info['name']}({code}) {rev['rev_pts']}/18分")

    reversals.sort(key=lambda r: -r["rev_pts"])
    logger.info(f"ETF底部反转候选共 {len(reversals)} 只")

    # ── 4.6 前瞻指标 ────────────────────────────────────────────────────────
    logger.info("Step 4.6/5: 计算前瞻指标...")
    fwd = forward_indicators.calc_forward_indicators()
    logger.info(
        f"前瞻指标: {fwd.composite_score:.0f}/100 [{fwd.composite_label}] "
        f"QVIX={fwd.qvix} 利差={fwd.yield_spread}bp 铜金比={fwd.copper_gold}"
    )
    fwd_ok = forward_indicators.send_forward_card(fwd, today_str)
    logger.info(f"前瞻指标推送: {'✓' if fwd_ok else '✗'}")

    # ── 5. 飞书推送 ──────────────────────────────────────────────────────────
    logger.info("Step 5/5: 推送飞书...")

    # 用 etf_prices K线末两行计算今日涨跌幅，检测集群恐慌
    etf_chg_pcts: dict[str, float] = {}
    for code, df in etf_prices.items():
        if df is not None and len(df) >= 2:
            prev, cur = float(df["close"].iloc[-2]), float(df["close"].iloc[-1])
            if prev > 0:
                etf_chg_pcts[code] = (cur - prev) / prev * 100
    panic_clusters = detect_cluster_panic(ETF_UNIVERSE, etf_chg_pcts)
    offense_surge  = detect_offense_surge(ETF_UNIVERSE, etf_chg_pcts)
    if panic_clusters:
        logger.info(f"板块恐慌: {', '.join(f'{n}({v:+.1f}%)' for n,v in panic_clusters)}")
    if offense_surge:
        logger.info(f"进攻信号: {', '.join(f'{n}({v:+.1f}%)' for n,v in offense_surge)}")

    success = notifier.send_signal(
        regime=regime,
        regime_score=regime_score,
        signals=signals,
        risk_summary=risk_summary,
        run_date=today_str,
        sentiment=sentiment,
        rotation=rotation,
        data_health=data_health,
        reversals=reversals,
        timing_risks=timing_risks,
        panic_clusters=panic_clusters or None,
        offense_surge=offense_surge or None,
    )

    if success:
        logger.info("飞书推送成功 ✓")
    else:
        logger.warning("飞书推送失败，请检查 Webhook Token")

    # ── 保存当日日志 ─────────────────────────────────────────────────────────
    save_daily_log({
        "date": today_str,
        "regime": regime,
        "regime_score": regime_score,
        "regime_state": regime_state,
        "north_5d_billion": north_5d_billion,
        "data_health": data_health.to_dict(),
        "rotation_strength": rotation.strength,
        "forward_indicators": {
            "qvix": fwd.qvix,
            "qvix_level": fwd.qvix_level,
            "us10y": fwd.us10y,
            "cn10y": fwd.cn10y,
            "yield_spread": fwd.yield_spread,
            "copper_gold": fwd.copper_gold,
            "margin_balance": fwd.margin_balance,
            "margin_5d_chg": fwd.margin_5d_chg,
            "composite_score": fwd.composite_score,
            "composite_label": fwd.composite_label,
        },
        "sentiment": {
            "score": sentiment.score,
            "level": sentiment.level,
            "consec_up_days": sentiment.consec_up_days,
            "ma5_dev_pct": sentiment.ma5_dev_pct,
            "ma20_dev_pct": sentiment.ma20_dev_pct,
            "rsi6": sentiment.rsi6,
            "limit_up": sentiment.limit_up,
            "limit_down": sentiment.limit_down,
            "timing_block": sentiment.timing_block,
            "note": sentiment.note,
        },
        "timing_risks": timing_risks,
        "factor_scores": factor_scores,
        "signals": signals,
        "risk_summary": risk_summary,
        "positions": positions,
    })

    logger.info(f"════ 运行完成 {today_str} ════\n")


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        logger.exception(f"系统异常: {e}")
        notifier.send_error_alert(str(e))
        sys.exit(1)
