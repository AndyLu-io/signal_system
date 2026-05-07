"""
数据获取层 v2 — 已验证可用的接口
价格/指数  : 腾讯财经 web.ifzq.gtimg.cn
ETF主力流向: 东方财富 push2.eastmoney.com（无历史代理问题）
北向资金   : 东方财富 RPT_MUTUAL_DEALAMT（成交总额，活跃度代理）
             + 东方财富 RPT_MUTUAL_NETINFLOW_STATISTICS（今日精确净买入，自动）
             + 手动文件 state/north_manual/*.xlsx（历史日期精确净买入，可选覆盖）
两融余额   : AkShare macro_china_market_margin_sh
市场宽度   : AkShare stock_zt_pool_em

北向资金数据说明
─────────────────
2024年10月废除每日额度限制后，东方财富等平台的净买入字段（NET_DEAL_AMT）
停止更新，仅能获取成交总额（NF_DEAL_AMT，万元，买+卖合计）。

三级数据源（按优先级）：
  Tier-1 自动：东方财富 RPT_MUTUAL_DEALAMT
               → deal_amt_billion（成交总额，亿元），净方向未知，历史多日可获取
  Tier-2 自动：东方财富 RPT_MUTUAL_NETINFLOW_STATISTICS（TIME_TYPE="1"）
               → 今日实时北向净买入（沪+深合计），每次运行自动刷新当日值
               → 每日运行积累缓存，逐步建立 5 日净买入历史
  Tier-3 手动：上交所/深交所官方文件放入 state/north_manual/
               → 任意日期精确净买入，优先级最高（覆盖 Tier-2）

手动文件放置方式（Tier-3，可选）：
  1. 上交所：http://www.sse.com.cn/market/hkexmarket/overview/dailydata/
     下载日度统计表，另存为 state/north_manual/sse_YYYYMMDD.xlsx（或 .csv）
  2. 深交所：https://www.szse.cn/market/hkexmarket/
     下载"深港通每日成交概况"，另存为 state/north_manual/szse_YYYYMMDD.xlsx
  系统会自动合并沪+深两方向的净买入得到北向合计，并写入缓存。
"""

from __future__ import annotations
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, TypeVar, Callable
import requests
import pandas as pd
import akshare as ak

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "Mozilla/5.0"}
_NORTH_CACHE  = Path(__file__).parent / "state" / "north_flow_cache.json"
_NORTH_MANUAL = Path(__file__).parent / "state" / "north_manual"
_EM_HSGT_URL  = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_EM_HSGT_TOKEN = "894050c76af8597a853f5b408b759f5d"

T = TypeVar("T")


def _retry(fn: Callable[[], T], attempts: int = 3, delays: tuple = (2, 5)) -> T:
    """重试包装：最多 attempts 次，失败后按 delays 等待，最终仍失败则抛出最后一个异常。"""
    last_exc: Exception = RuntimeError("unknown")
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:
            last_exc = e
            if i < len(delays):
                logger.warning(f"第{i+1}次失败({e})，{delays[i]}s后重试...")
                time.sleep(delays[i])
    raise last_exc


# ─── 工具函数 ──────────────────────────────────────────────────────────────────

def _market(code: str) -> str:
    """判断交易所前缀：sz / sh（ETF + 个股）"""
    # 深市ETF：159xxx / 56xxxx（含561/562/563）
    if code.startswith("15") or code.startswith("56"):
        return "sz"
    # 深市个股：0xxxxx（中小板）/ 3xxxxx（创业板）
    if code.startswith("0") or code.startswith("3"):
        return "sz"
    return "sh"


def _tencent_kline(symbol: str, count: int = 45) -> Optional[list]:
    """
    腾讯财经日K线接口（完全无代理问题）
    symbol 示例: sz159326 / sh000300
    返回 list of [日期, 开, 收, 高, 低, 成交量(手)]
    """
    url = (f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
           f"?_var=kline_dayqfq&param={symbol},day,,,{count},qfq")
    def _fetch():
        r = requests.get(url, headers=_HEADERS, timeout=10)
        r.raise_for_status()
        raw = r.text.replace("kline_dayqfq=", "")
        data = json.loads(raw)
        inner = data.get("data", {}).get(symbol, {})
        return inner.get("day") or inner.get("qfqday") or []
    try:
        return _retry(_fetch)
    except Exception as e:
        logger.warning(f"腾讯K线 {symbol} 三次均失败: {e}")
        return None


def _klines_to_df(klines: list) -> pd.DataFrame:
    """['2026-04-16','1.9','1.91','1.92','1.86','9937196','0'] → DataFrame"""
    rows = []
    for k in klines:
        rows.append({
            "date":   pd.to_datetime(k[0]),
            "open":   float(k[1]),
            "close":  float(k[2]),
            "high":   float(k[3]),
            "low":    float(k[4]),
            "volume": float(k[5]),
            # 腾讯无成交额字段，用 close×volume 估算
            "amount": float(k[2]) * float(k[5]),
        })
    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    return df


# ─── 价格数据（腾讯） ──────────────────────────────────────────────────────────

def get_etf_prices(codes: list[str], days: int = 40) -> dict[str, Optional[pd.DataFrame]]:
    """返回 {code: DataFrame(date,open,close,high,low,volume,amount)}"""
    result: dict[str, Optional[pd.DataFrame]] = {}
    for code in codes:
        sym = f"{_market(code)}{code}"
        klines = _tencent_kline(sym, days + 5)
        if klines:
            result[code] = _klines_to_df(klines).tail(days)
        else:
            logger.warning(f"ETF {code} 价格获取失败")
            result[code] = None
    return result


def get_index_prices(index_code: str, days: int = 40) -> Optional[pd.DataFrame]:
    """沪深300=000300 / 中证500=000905"""
    prefix = "sh" if index_code.startswith("0003") else "sh"
    sym = f"sh{index_code}"
    klines = _tencent_kline(sym, days + 5)
    if klines:
        return _klines_to_df(klines).tail(days)
    logger.warning(f"指数 {index_code} 价格获取失败")
    return None


# ─── 北向资金（东方财富自动 + 手动文件合并） ──────────────────────────────────
#
# 缓存格式（每条记录）：
#   {
#     "date":             "2026-04-24",
#     "deal_amt_billion": 34.3,      # 成交总额（亿元），Tier-1 自动，活跃度代理
#     "net_buy_billion":  3.15       # 净买入（亿元），Tier-2 自动（今日）或 Tier-3 手动覆盖
#   }
#
# 下游信号使用规则：
#   - net_buy_billion 有值 → 用于机制评分 + 轮动信号（精确方向）
#   - net_buy_billion 为 null → 机制评分返回中性5.0，轮动信号显示"活跃度代理"

def _load_north_cache() -> list[dict]:
    if _NORTH_CACHE.exists():
        try:
            data = json.loads(_NORTH_CACHE.read_text(encoding="utf-8"))
            # 兼容旧格式（只有 net_flow_billion 字段）
            for r in data:
                if "deal_amt_billion" not in r:
                    r["deal_amt_billion"] = None
                if "net_buy_billion" not in r:
                    # 旧缓存的 net_flow_billion 如果不为0则迁移过来
                    old_net = r.pop("net_flow_billion", None)
                    r["net_buy_billion"] = old_net if old_net and old_net != 0.0 else None
                elif "net_flow_billion" in r:
                    r.pop("net_flow_billion", None)
            return data
        except Exception:
            pass
    return []


def _save_north_cache(records: list[dict]) -> None:
    _NORTH_CACHE.parent.mkdir(parents=True, exist_ok=True)
    _NORTH_CACHE.write_text(
        json.dumps(records[-30:], ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _fetch_em_net_buy_today() -> Optional[float]:
    """
    从东方财富 RPT_MUTUAL_NETINFLOW_STATISTICS 获取今日北向净买入（亿元）。
    TIME_TYPE="1" = 今日实时累计净流入（沪+深合计，万元）。
    废除额度限制后此接口仍正常更新，为唯一可自动获取精确方向的免费来源。
    """
    try:
        r = requests.get(
            _EM_HSGT_URL,
            params={
                "reportName": "RPT_MUTUAL_NETINFLOW_STATISTICS",
                "columns": "DIRECTION_TYPE,TOTAL_INFLOW_BOTH,TIME_TYPE",
                "filter": '(DIRECTION_TYPE="2")(TIME_TYPE="1")',
                "token": _EM_HSGT_TOKEN,
                "client": "WEB",
            },
            headers={"Referer": "https://data.eastmoney.com/hsgt/"},
            timeout=10,
        )
        d = r.json()
        if not d.get("success"):
            return None
        items = (d.get("result") or {}).get("data") or []
        if not items:
            return None
        val = items[0].get("TOTAL_INFLOW_BOTH")
        if val is None:
            return None
        return round(float(val) / 10000, 2)  # 万元 → 亿元
    except Exception as e:
        logger.warning(f"东方财富北向净买入获取失败: {e}")
        return None


def _fetch_em_deal_amt(days: int = 10) -> list[dict]:
    """
    从东方财富 RPT_MUTUAL_DEALAMT 获取北向成交总额（亿元）。
    废额度后净买入字段为 null，仅返回成交总额作为活跃度指标。
    """
    from datetime import timedelta
    since = (datetime.today() - timedelta(days=days + 7)).strftime("%Y-%m-%d")
    try:
        r = requests.get(
            _EM_HSGT_URL,
            params={
                "reportName": "RPT_MUTUAL_DEALAMT",
                "columns": "TRADE_DATE,NF_DEAL_AMT",
                "filter": f"(TRADE_DATE>='{since}')",
                "pageNumber": "1",
                "pageSize": str(days + 10),
                "sortTypes": "-1",
                "sortColumns": "TRADE_DATE",
                "source": "WEB",
                "client": "WEB",
                "token": _EM_HSGT_TOKEN,
            },
            headers={"Referer": "https://data.eastmoney.com/hsgt/"},
            timeout=10,
        )
        d = r.json()
        if not d.get("success") or not d.get("result", {}).get("data"):
            return []
        result = []
        for row in d["result"]["data"]:
            dt = str(row["TRADE_DATE"])[:10]
            amt = row.get("NF_DEAL_AMT")
            if amt is not None:
                result.append({
                    "date": dt,
                    "deal_amt_billion": round(float(amt) / 10000, 2),  # 万元 → 亿元
                    "net_buy_billion": None,
                })
        return result
    except Exception as e:
        logger.warning(f"东方财富北向成交额获取失败: {e}")
        return []


def _parse_manual_north_files() -> dict[str, float]:
    """
    扫描 state/north_manual/ 目录，解析上交所/深交所官方文件，
    返回 {date_str: net_buy_billion} 字典（沪+深合计）。

    支持文件命名格式：
      sse_YYYYMMDD.xlsx / sse_YYYYMMDD.csv  （上交所）
      szse_YYYYMMDD.xlsx / szse_YYYYMMDD.csv （深交所）

    文件内容：需含有"净买"或"净流入"关键字的列（金额单位：万元）。
    """
    _NORTH_MANUAL.mkdir(parents=True, exist_ok=True)
    net_by_date: dict[str, float] = {}

    for fpath in sorted(_NORTH_MANUAL.iterdir()):
        stem = fpath.stem.lower()
        if not any(stem.startswith(p) for p in ("sse_", "szse_", "sh_", "sz_")):
            continue
        # 从文件名提取日期
        for part in stem.split("_"):
            if len(part) == 8 and part.isdigit():
                date_str = f"{part[:4]}-{part[4:6]}-{part[6:8]}"
                break
        else:
            logger.debug(f"无法从文件名提取日期: {fpath.name}")
            continue

        try:
            if fpath.suffix.lower() in (".xlsx", ".xls"):
                df = pd.read_excel(fpath, header=None)
            elif fpath.suffix.lower() == ".csv":
                df = pd.read_csv(fpath, header=None, encoding="utf-8-sig")
            else:
                continue

            # 找含"净买"或"净流入"的列
            net_val = _extract_net_amount(df)
            if net_val is not None:
                # 万元 → 亿元，累加（同一天可有沪+深两个文件）
                net_by_date[date_str] = round(
                    net_by_date.get(date_str, 0.0) + net_val / 10000, 2
                )
                logger.info(f"手动文件 {fpath.name}: 净买入 {net_val/10000:.2f}亿 → {date_str}")
            else:
                logger.warning(f"手动文件 {fpath.name}: 未找到净买入列")
        except Exception as e:
            logger.warning(f"手动文件 {fpath.name} 解析失败: {e}")

    return net_by_date


def _extract_net_amount(df: pd.DataFrame) -> Optional[float]:
    """
    从 DataFrame 中找到"净买入"金额（万元）。
    支持多种布局（行标题、列标题、混合）。
    """
    # 将所有单元格转为字符串做关键词搜索
    str_df = df.astype(str)

    net_keywords = ("净买", "净买入", "净流入", "净额", "netbuy")

    # 方法1：找含净买关键词的列头，取最后一个数值行
    for col_idx in range(len(df.columns)):
        col_vals = str_df.iloc[:, col_idx]
        for kw in net_keywords:
            if col_vals.str.contains(kw, case=False).any():
                # 找该列中的数值
                for row_idx in range(len(df) - 1, -1, -1):
                    try:
                        val = float(str(df.iloc[row_idx, col_idx]).replace(",", ""))
                        if abs(val) > 0:
                            return val
                    except (ValueError, TypeError):
                        continue

    # 方法2：找含净买关键词的行，取同行最后一个数值
    for row_idx in range(len(df)):
        row_vals = str_df.iloc[row_idx]
        if row_vals.str.contains("|".join(net_keywords), case=False).any():
            for col_idx in range(len(df.columns) - 1, -1, -1):
                try:
                    val = float(str(df.iloc[row_idx, col_idx]).replace(",", ""))
                    if abs(val) > 0:
                        return val
                except (ValueError, TypeError):
                    continue

    return None


def get_north_flow(days: int = 10) -> Optional[pd.DataFrame]:
    """
    返回 DataFrame(date, deal_amt_billion, net_buy_billion)，最近 days 个交易日。

    字段说明：
      deal_amt_billion : 成交总额（亿元），Tier-1 自动，反映外资活跃度
      net_buy_billion  : 净买入（亿元），Tier-2 自动（今日实时）或 Tier-3 手动覆盖

    数据积累方式：每次运行自动写入今日净买入，多日运行后缓存逐步形成 5 日历史。
    若东方财富接口完全失效，返回 None。
    """
    cache = _load_north_cache()
    cached_dates = {r["date"] for r in cache}

    # ── Step 1：从东方财富补充近期成交总额 ─────────────────────────────────
    em_records = _fetch_em_deal_amt(days=days + 5)
    changed = False
    for rec in em_records:
        if rec["date"] not in cached_dates:
            cache.append(rec)
            cached_dates.add(rec["date"])
            changed = True
        else:
            # 更新成交总额字段（净买入字段不覆盖，保留手动值）
            for existing in cache:
                if existing["date"] == rec["date"] and existing.get("deal_amt_billion") is None:
                    existing["deal_amt_billion"] = rec["deal_amt_billion"]
                    changed = True

    # ── Step 1.5：自动获取今日精确净买入（Tier-2，RPT_MUTUAL_NETINFLOW_STATISTICS）
    # 每次运行刷新当日值，每日积累一条缓存记录；手动文件（Step 2）优先级更高会覆盖此值
    today_str = datetime.today().strftime("%Y-%m-%d")
    auto_net_today = _fetch_em_net_buy_today()
    if auto_net_today is not None and abs(auto_net_today) >= 0.01:
        updated_today = False
        for existing in cache:
            if existing["date"] == today_str:
                existing["net_buy_billion"] = auto_net_today
                changed = True
                updated_today = True
                break
        if not updated_today:
            cache.append({
                "date": today_str,
                "deal_amt_billion": None,
                "net_buy_billion": auto_net_today,
            })
            cached_dates.add(today_str)
            changed = True
        logger.info(f"今日北向净买入（自动）: {auto_net_today:+.2f}亿")

    # ── Step 2：扫描手动文件，写入净买入（优先级高于 Tier-2 自动值）──────────
    manual_net = _parse_manual_north_files()
    for date_str, net_val in manual_net.items():
        found = False
        for existing in cache:
            if existing["date"] == date_str:
                if existing.get("net_buy_billion") != net_val:
                    existing["net_buy_billion"] = net_val
                    changed = True
                found = True
                break
        if not found:
            cache.append({"date": date_str, "deal_amt_billion": None, "net_buy_billion": net_val})
            changed = True

    if changed:
        _save_north_cache(cache)

    if not cache:
        logger.warning("北向资金缓存为空")
        return None

    df = pd.DataFrame(cache)
    # 统一字段（兼容旧缓存）
    if "deal_amt_billion" not in df.columns:
        df["deal_amt_billion"] = None
    if "net_buy_billion" not in df.columns:
        df["net_buy_billion"] = None

    df["date"] = pd.to_datetime(df["date"])
    df = (
        df.sort_values("date")
        .drop_duplicates("date")
        .tail(days)
        .reset_index(drop=True)
    )

    # 若成交总额全为 None，说明接口完全失效
    if df["deal_amt_billion"].isna().all() and df["net_buy_billion"].isna().all():
        logger.warning("北向资金：成交总额和净买入均无数据，返回 None")
        return None

    return df[["date", "deal_amt_billion", "net_buy_billion"]]


# ─── 两融余额（AkShare macro_china_market_margin_sh） ─────────────────────────

def get_margin_balance(days: int = 10) -> Optional[pd.DataFrame]:
    """返回 DataFrame(date, balance_billion)，上交所融资余额"""
    def _fetch():
        df = ak.macro_china_market_margin_sh()
        df.columns = [c.strip() for c in df.columns]
        df.rename(columns={"日期": "date", "融资余额": "balance_raw"}, inplace=True)
        df["date"] = pd.to_datetime(df["date"])
        df["balance_billion"] = pd.to_numeric(df["balance_raw"], errors="coerce") / 1e8
        df = df[["date", "balance_billion"]].dropna()
        df.sort_values("date", inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df.tail(days)
    try:
        return _retry(_fetch)
    except Exception as e:
        logger.warning(f"两融数据三次均失败: {e}")
        return None


# ─── ETF 主力净流入（东方财富 push2，无代理问题） ──────────────────────────────

def get_etf_main_force_flow(codes: list[str]) -> dict[str, float]:
    """
    返回 {code: net_flow_billion}（亿元，正=净流入，负=净流出）
    取最近一个交易日的主力净流入
    接口: push2.eastmoney.com/api/qt/stock/fflow/daykline/get
    并发拉取，单次 3s 超时，不重试（主力流向是加分项，失败不影响主流程）
    """
    market_map = {"sz": 0, "sh": 1}

    def _fetch_one(code: str) -> tuple[str, float]:
        mkt = market_map[_market(code)]
        url = (
            f"https://push2.eastmoney.com/api/qt/stock/fflow/daykline/get"
            f"?lmt=5&klt=101&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55"
            f"&ut=b2884a393a59ad64002292a3e90d46a5&secid={mkt}.{code}"
        )
        try:
            r = requests.get(url, headers=_HEADERS, timeout=3)
            r.raise_for_status()
            klines = r.json().get("data", {}).get("klines", [])
            if klines:
                parts = klines[-1].split(",")
                return code, round(float(parts[1]) / 1e8, 4)
        except Exception as e:
            logger.debug(f"{code} 主力流向获取失败: {e}")
        return code, 0.0

    result: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch_one, c): c for c in codes}
        for fut in as_completed(futures):
            code, val = fut.result()
            result[code] = val
    return result


def get_etf_main_force_flow_detail(codes: list[str]) -> dict[str, dict]:
    """
    返回 {code: {today, yesterday, delta, history[float]}}（亿元，正=流入）。
    使用与 get_etf_main_force_flow 相同的 EMF 接口，解析最近3日数据。
    """
    market_map = {"sz": 0, "sh": 1}

    def _fetch_one(code: str) -> tuple[str, dict]:
        mkt = market_map[_market(code)]
        url = (
            f"https://push2.eastmoney.com/api/qt/stock/fflow/daykline/get"
            f"?lmt=5&klt=101&fields1=f1,f2,f3,f7&fields2=f51,f52,f53,f54,f55"
            f"&ut=b2884a393a59ad64002292a3e90d46a5&secid={mkt}.{code}"
        )
        empty = {"today": 0.0, "yesterday": 0.0, "delta": 0.0, "history": []}
        try:
            r = requests.get(url, headers=_HEADERS, timeout=3)
            r.raise_for_status()
            klines = r.json().get("data", {}).get("klines", [])
            if not klines:
                return code, empty
            history = [round(float(k.split(",")[1]) / 1e8, 4) for k in klines]
            today = history[-1]
            yesterday = history[-2] if len(history) >= 2 else 0.0
            return code, {
                "today": today,
                "yesterday": yesterday,
                "delta": round(today - yesterday, 4),
                "history": history,
            }
        except Exception as e:
            logger.debug(f"{code} 主力流向详情失败: {e}")
        return code, empty

    result: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = {pool.submit(_fetch_one, c): c for c in codes}
        for fut in as_completed(futures):
            code, val = fut.result()
            result[code] = val
    return result


# ─── 市场宽度（涨停/跌停数） ──────────────────────────────────────────────────

def get_market_breadth() -> dict:
    """返回 {limit_up, limit_down, ratio}，今日数据"""
    today = datetime.today().strftime("%Y%m%d")
    limit_up = limit_down = 0
    try:
        df_up = _retry(lambda: ak.stock_zt_pool_em(date=today), attempts=3, delays=(3, 6))
        limit_up = len(df_up) if df_up is not None else 0
    except Exception as e:
        logger.warning(f"涨停池三次均失败: {e}")

    try:
        df_down = _retry(lambda: ak.stock_zt_pool_dtgc_em(date=today), attempts=2, delays=(3,))
        limit_down = len(df_down) if df_down is not None else 0
    except Exception as e:
        logger.warning(f"跌停池获取失败: {e}")

    return {
        "limit_up": limit_up,
        "limit_down": limit_down,
        "ratio": round(limit_up / max(limit_down, 1), 2),
    }
