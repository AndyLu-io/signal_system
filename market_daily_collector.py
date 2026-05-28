"""
市场资金每日采集器 — 每个交易日 15:30 自动采集板块资金数据。

采集内容：
  - 行业板块资金流向（流入/流出/净额）
  - 概念板块资金流向
  - 大单成交统计（按行业汇总，区分主力 vs 散户）
  - 当日大盘指标（沪深300涨跌、成交额）

数据存储：state/market_daily/YYYY-MM-DD.json
周报从这里读取5日数据汇总。

用法：
  python3 signal_system/market_daily_collector.py         # 采集今天
  python3 signal_system/market_daily_collector.py --force # 跳过交易日检查
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import akshare as ak
import pandas as pd

from utils import atomic_write_json, is_trading_day, retry
from data_fetcher import get_index_prices

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).parent / "state" / "market_daily"


def collect_industry_flow() -> list[dict]:
    """行业板块资金流向。"""
    try:
        df = retry(lambda: ak.stock_fund_flow_industry())
        if df is None or df.empty:
            return []
        records = []
        for _, row in df.iterrows():
            records.append({
                "name": row.get("行业", ""),
                "index": float(row.get("行业指数", 0) or 0),
                "change_pct": float(row.get("行业-涨跌幅", 0) or 0),
                "inflow": float(row.get("流入资金", 0) or 0),
                "outflow": float(row.get("流出资金", 0) or 0),
                "net": float(row.get("净额", 0) or 0),
                "count": int(row.get("公司家数", 0) or 0),
                "leader": row.get("领涨股", ""),
                "leader_chg": float(row.get("领涨股-涨跌幅", 0) or 0),
            })
        return records
    except Exception as e:
        logger.warning(f"行业资金采集失败: {e}")
        return []


def collect_concept_flow() -> list[dict]:
    """概念板块资金流向。"""
    try:
        df = retry(lambda: ak.stock_fund_flow_concept())
        if df is None or df.empty:
            return []
        records = []
        for _, row in df.iterrows():
            records.append({
                "name": row.get("行业", ""),
                "change_pct": float(row.get("行业-涨跌幅", 0) or 0),
                "inflow": float(row.get("流入资金", 0) or 0),
                "outflow": float(row.get("流出资金", 0) or 0),
                "net": float(row.get("净额", 0) or 0),
                "count": int(row.get("公司家数", 0) or 0),
                "leader": row.get("领涨股", ""),
            })
        return records
    except Exception as e:
        logger.warning(f"概念资金采集失败: {e}")
        return []


def collect_big_deal_summary() -> dict:
    """大单成交汇总 — 区分买盘/卖盘。"""
    try:
        df = retry(lambda: ak.stock_fund_flow_big_deal())
        if df is None or df.empty:
            return {"buy_amount": 0, "sell_amount": 0, "net": 0, "count": 0}

        buy_df = df[df["大单性质"] == "买盘"]
        sell_df = df[df["大单性质"] == "卖盘"]

        buy_amount = buy_df["成交额"].sum() if not buy_df.empty else 0
        sell_amount = sell_df["成交额"].sum() if not sell_df.empty else 0

        return {
            "buy_amount": round(float(buy_amount), 2),
            "sell_amount": round(float(sell_amount), 2),
            "net": round(float(buy_amount - sell_amount), 2),
            "buy_count": len(buy_df),
            "sell_count": len(sell_df),
        }
    except Exception as e:
        logger.warning(f"大单数据采集失败: {e}")
        return {"buy_amount": 0, "sell_amount": 0, "net": 0}


def collect_market_overview() -> dict:
    """大盘概况：沪深300涨跌 + 成交额。"""
    try:
        df = get_index_prices("000300", days=5)
        if df is not None and not df.empty:
            latest = df.iloc[-1]
            prev = df.iloc[-2] if len(df) >= 2 else latest
            close = float(latest["close"])
            prev_close = float(prev["close"])
            change_pct = (close / prev_close - 1) * 100
            volume = float(latest.get("amount", 0) or latest.get("volume", 0))

            # 5日平均成交对比
            avg_vol = df["volume"].astype(float).mean() if "volume" in df.columns else 0
            vol_ratio = volume / avg_vol if avg_vol > 0 else 1.0

            return {
                "csi300_close": round(close, 2),
                "csi300_change_pct": round(change_pct, 2),
                "volume": round(volume, 0),
                "vol_ratio_5d": round(vol_ratio, 2),
            }
    except Exception as e:
        logger.warning(f"大盘概况采集失败: {e}")
    return {}


def collect_north_flow() -> dict:
    """北向资金 — 读取本系统已有的缓存（Tier-2自动采集，数据可靠）。"""
    cache_file = Path(__file__).parent / "state" / "north_flow_cache.json"
    try:
        if cache_file.exists():
            records = json.loads(cache_file.read_text(encoding="utf-8"))
            today_str = date.today().isoformat()
            for r in reversed(records):
                if r.get("date") == today_str:
                    return {
                        "net_buy": r.get("net_buy_billion", 0),
                        "deal_amt": r.get("deal_amt_billion", 0),
                        "date": today_str,
                    }
            if records:
                latest = records[-1]
                return {
                    "net_buy": latest.get("net_buy_billion", 0),
                    "deal_amt": latest.get("deal_amt_billion", 0),
                    "date": latest.get("date", ""),
                }
    except Exception as e:
        logger.warning(f"北向缓存读取失败: {e}")
    return {"net_buy": 0, "deal_amt": 0}


def collect_fund_top_holdings() -> list[dict]:
    """公募基金重仓股 Top20（季报数据，季度更新）。"""
    quarters = ["20250331", "20241231", "20240930"]
    for q in quarters:
        try:
            df = retry(lambda q=q: ak.stock_report_fund_hold(symbol="基金持仓", date=q))
            if df is not None and not df.empty:
                records = []
                for _, row in df.head(20).iterrows():
                    change = row.get("持股变化", "")
                    records.append({
                        "code": row.get("股票代码", ""),
                        "name": row.get("股票简称", ""),
                        "fund_count": int(row.get("持有基金家数", 0)),
                        "hold_value": float(row.get("持股市值", 0)),
                        "change": change,
                        "change_pct": float(row.get("持股变动比例", 0) or 0),
                        "quarter": q,
                    })
                return records
        except Exception:
            continue
    logger.warning("基金重仓数据获取失败")
    return []


def collect_institution_changes() -> list[dict]:
    """机构持仓变化 Top20（增仓最多的）。"""
    quarters = ["20251", "20244", "20243"]
    for q in quarters:
        try:
            df = retry(lambda q=q: ak.stock_institute_hold(symbol=q))
            if df is not None and not df.empty:
                df["持股比例增幅"] = pd.to_numeric(df["持股比例增幅"], errors="coerce")
                top_increase = df.nlargest(20, "持股比例增幅")
                records = []
                for _, row in top_increase.iterrows():
                    records.append({
                        "code": row.get("证券代码", ""),
                        "name": row.get("证券简称", ""),
                        "inst_count": int(row.get("机构数", 0)),
                        "inst_change": int(row.get("机构数变化", 0)),
                        "hold_pct": float(row.get("持股比例", 0)),
                        "hold_pct_change": float(row.get("持股比例增幅", 0)),
                        "quarter": q,
                    })
                return records
        except Exception:
            continue
    logger.warning("机构持仓变化数据获取失败")
    return []


ANOMALY_THRESHOLD = 80  # 单日净流入超过此值(亿)触发告警

_ALERT_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/904f6ec0-0d2a-4296-9201-5e70ee1d7a9c"

SECTOR_ETF_QUICK = {
    "通信设备": "515880 通信ETF", "半导体": "512480 半导体ETF",
    "电源设备": "561910 电池ETF", "电力设备": "159326 电网设备ETF",
    "汽车整车": "159845 新能车ETF", "军工": "512660 军工ETF",
    "有色金属": "512400 有色金属ETF", "证券": "512880 证券ETF",
    "人工智能": "159819 人工智能ETF", "光伏": "515790 光伏ETF",
}


def _check_anomaly_alert(industry_data: list[dict], today: date) -> None:
    """单日异常资金流入告警。"""
    from feishu_pusher import post_card
    from utils import webhooks_from_env

    alerts = [s for s in industry_data if s.get("net", 0) > ANOMALY_THRESHOLD]
    if not alerts:
        return

    lines = []
    for s in sorted(alerts, key=lambda x: -x["net"])[:5]:
        name = s["name"]
        etf = SECTOR_ETF_QUICK.get(name, "—")
        lines.append(
            f"🚨 **{name}**  单日净流入 **{s['net']:.0f}亿**  涨跌{s['change_pct']:+.1f}%\n"
            f"      ETF: {etf}  ▸  领涨: {s.get('leader', '')}"
        )

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"⚡ 资金异常告警 {today.isoformat()}"},
                "template": "red",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content":
                    f"**单日板块净流入超{ANOMALY_THRESHOLD}亿 — 极端信号**\n\n"
                    + "\n\n".join(lines)
                    + "\n\n⚠️ 提示: 极端单日流入可能是主力拉升出货，也可能是趋势加速。"
                    "结合连续性判断——如果明天继续流入则确认趋势。"}},
            ],
        },
    }

    webhooks = webhooks_from_env("MARKET_REPORT_WEBHOOK", [_ALERT_WEBHOOK])
    post_card(card, webhooks)
    logger.info(f"异常告警已推送: {len(alerts)}个板块超{ANOMALY_THRESHOLD}亿")


def run(force: bool = False) -> None:
    today = date.today()

    if not force and not is_trading_day():
        logger.info(f"{today} 非交易日，跳过采集")
        return

    logger.info(f"开始采集 {today} 市场资金数据...")

    data = {
        "date": today.isoformat(),
        "industry": collect_industry_flow(),
        "concept": collect_concept_flow(),
        "big_deal": collect_big_deal_summary(),
        "market": collect_market_overview(),
        "north": collect_north_flow(),
    }

    # 机构/基金数据（周五或强制时采集，季度更新频率）
    if today.weekday() == 4 or force:
        logger.info("采集机构/基金持仓数据...")
        data["fund_holdings"] = collect_fund_top_holdings()
        data["institution_changes"] = collect_institution_changes()

    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = _DATA_DIR / f"{today.isoformat()}.json"
    atomic_write_json(path, data)

    n_ind = len(data["industry"])
    n_con = len(data["concept"])
    big = data["big_deal"]
    north = data["north"].get("net_buy", 0)
    fund_n = len(data.get("fund_holdings", []))
    inst_n = len(data.get("institution_changes", []))
    logger.info(
        f"采集完成: 行业{n_ind}条, 概念{n_con}条, "
        f"大单净额{big.get('net',0):.0f}亿, 北向{north:.1f}亿"
        + (f", 基金重仓{fund_n}条, 机构变化{inst_n}条" if fund_n else "")
    )

    # P2: 异常日内告警 — 单日板块净流入超80亿立即推送
    _check_anomaly_alert(data["industry"], today)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description="市场资金每日采集")
    parser.add_argument("--force", action="store_true", help="跳过交易日检查")
    args = parser.parse_args()
    run(force=args.force)


if __name__ == "__main__":
    main()
