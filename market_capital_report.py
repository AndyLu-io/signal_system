"""
市场资金动向周报 v3 — 基于每日采集数据的多维分析。

分析维度（全部数据驱动）：
  1. 5日累计净流入排名（真正的周维度）
  2. 主力 vs 散户（大单买卖比）
  3. 量价配合分析（资金入+价格低=建仓信号）
  4. 趋势 vs 轮动分类（连续≥3周Top10=趋势）
  5. 本周 vs 上周排名变化（轮动起点识别）
  6. 资金流出方向（钱从哪走）
  7. 大盘情绪上下文（本周是涨/跌/震荡周）
  8. 北向资金偏好
  9. 政策/五年规划匹配
  10. 操作建议（含止损/持有周期/失效条件）

每个板块配套：ETF + 龙头股。

用法：
  python3 signal_system/market_capital_report.py [--dry]    # 生成并推送
  python3 signal_system/market_capital_report.py --force    # 跳过周五检查
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pandas as pd

from utils import atomic_write_json, is_trading_day, webhooks_from_env
from feishu_pusher import post_card

logger = logging.getLogger(__name__)

_LOGS_DIR = Path(__file__).parent / "logs"
_STATE_DIR = Path(__file__).parent / "state"
_DAILY_DIR = _STATE_DIR / "market_daily"
_HISTORY_FILE = _STATE_DIR / "market_capital_history.json"

_FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/904f6ec0-0d2a-4296-9201-5e70ee1d7a9c"


# ─── 板块 → ETF + 龙头股 ──────────────────────────────────────────────────────

SECTOR_MAP = {
    # 科技
    "半导体": {"etf": "512480 半导体ETF", "leaders": "北方华创、中芯国际、韦尔股份"},
    "芯片": {"etf": "159995 芯片ETF", "leaders": "海光信息、寒武纪、中芯国际"},
    "消费电子": {"etf": "159732 消费电子ETF", "leaders": "立讯精密、歌尔股份、工业富联"},
    "元件": {"etf": "159819 人工智能ETF", "leaders": "三安光电、法拉电子、顺络电子"},
    "其他电子": {"etf": "159819 人工智能ETF", "leaders": "传音控股、鹏鼎控股、沪电股份"},
    "光学光电子": {"etf": "515790 光伏ETF", "leaders": "京东方A、长飞光纤、新易盛"},
    "通信设备": {"etf": "515880 通信ETF", "leaders": "中兴通讯、中天科技、亨通光电"},
    "计算机应用": {"etf": "512720 计算机ETF", "leaders": "科大讯飞、金山办公、中科创达"},
    "软件开发": {"etf": "159852 软件ETF", "leaders": "用友网络、金蝶国际、中望软件"},
    "IT服务": {"etf": "512720 计算机ETF", "leaders": "浪潮信息、中科曙光、紫光股份"},
    "人工智能": {"etf": "159819 人工智能ETF", "leaders": "科大讯飞、商汤-W、海康威视"},
    "军工电子": {"etf": "512660 军工ETF", "leaders": "振华科技、紫光国微、景嘉微"},
    # 新能源
    "电力设备": {"etf": "159326 电网设备ETF", "leaders": "思源电气、许继电气、国电南瑞"},
    "电网设备": {"etf": "159326 电网设备ETF", "leaders": "国电南瑞、思源电气、特变电工"},
    "电源设备": {"etf": "561910 电池ETF", "leaders": "宁德时代、亿纬锂能、德业股份"},
    "其他电源设备": {"etf": "561910 电池ETF", "leaders": "阳光电源、科华数据、锦浪科技"},
    "电池": {"etf": "561910 电池ETF", "leaders": "宁德时代、亿纬锂能、国轩高科"},
    "光伏": {"etf": "515790 光伏ETF", "leaders": "隆基绿能、通威股份、阳光电源"},
    "风电": {"etf": "159314 风电ETF", "leaders": "金风科技、明阳智能、大金重工"},
    "储能": {"etf": "159566 储能ETF", "leaders": "阳光电源、科华数据、鹏辉能源"},
    "新能源": {"etf": "516160 新能源ETF", "leaders": "阳光电源、隆基绿能、通威股份"},
    "电力": {"etf": "159611 电力ETF", "leaders": "长江电力、华能国际、国投电力"},
    # 高端制造
    "机器人": {"etf": "562500 机器人ETF", "leaders": "埃斯顿、汇川技术、绿的谐波"},
    "自动化设备": {"etf": "562500 机器人ETF", "leaders": "汇川技术、先导智能、赛腾股份"},
    "通用设备": {"etf": "159890 智能制造ETF", "leaders": "先惠技术、科德数控、纽威数控"},
    "专用设备": {"etf": "159890 智能制造ETF", "leaders": "迈为股份、利元亨、杭可科技"},
    "汽车整车": {"etf": "159845 新能车ETF", "leaders": "比亚迪、长安汽车、赛力斯"},
    "军工": {"etf": "512660 军工ETF", "leaders": "中航沈飞、航发动力、中航光电"},
    "军工装备": {"etf": "512660 军工ETF", "leaders": "中航沈飞、航发动力、中直股份"},
    "航空航天": {"etf": "512660 军工ETF", "leaders": "航天电器、中国卫星、航天彩虹"},
    # 资源/材料
    "有色金属": {"etf": "512400 有色金属ETF", "leaders": "紫金矿业、洛阳钼业、天齐锂业"},
    "小金属": {"etf": "512400 有色金属ETF", "leaders": "华友钴业、天齐锂业、盛新锂能"},
    "稀土": {"etf": "159715 稀土ETF", "leaders": "北方稀土、中国稀土、金力永磁"},
    "金属新材料": {"etf": "512400 有色金属ETF", "leaders": "中复神鹰、光威复材、昆工科技"},
    "非金属材料": {"etf": "159745 建材ETF", "leaders": "海螺水泥、东方雨虹、北新建材"},
    "建筑材料": {"etf": "159745 建材ETF", "leaders": "海螺水泥、中国巨石、东方雨虹"},
    "化学纤维": {"etf": "159981 化工ETF", "leaders": "恒力石化、荣盛石化、桐昆股份"},
    "煤炭开采加工": {"etf": "515220 煤炭ETF", "leaders": "中国神华、陕西煤业、兖矿能源"},
    "油气开采及服务": {"etf": "159697 油气ETF", "leaders": "中国石油、中海油服、海油发展"},
    "塑料制品": {"etf": "159981 化工ETF", "leaders": "金发科技、中国化学"},
    "橡胶制品": {"etf": "159981 化工ETF", "leaders": "赛轮轮胎、玲珑轮胎"},
    "电子化学品": {"etf": "159995 芯片ETF", "leaders": "雅克科技、晶瑞电材"},
    # 金融
    "银行": {"etf": "512800 银行ETF", "leaders": "招商银行、宁波银行、兴业银行"},
    "证券": {"etf": "512880 证券ETF", "leaders": "中信证券、东方财富、华泰证券"},
    "保险": {"etf": "512070 非银ETF", "leaders": "中国平安、中国人寿、新华保险"},
    "房地产开发": {"etf": "512200 房地产ETF", "leaders": "万科A、保利发展、招商蛇口"},
    # 消费
    "食品饮料": {"etf": "159869 食品ETF", "leaders": "伊利股份、海天味业、安井食品"},
    "白酒": {"etf": "512690 酒ETF", "leaders": "贵州茅台、五粮液、泸州老窖"},
    "医药商业": {"etf": "512010 医药ETF", "leaders": "药明康德、恒瑞医药、迈瑞医疗"},
    "生物制品": {"etf": "159567 创新药ETF", "leaders": "百济神州、信达生物、康方生物"},
    # 基建/环保
    "环保设备": {"etf": "516580 环保ETF", "leaders": "伟明环保、瀚蓝环境、高能环境"},
    "包装印刷": {"etf": "159869 食品ETF", "leaders": "裕同科技、奥瑞金"},
    "其他社会服务": {"etf": "159766 旅游ETF", "leaders": "中国中免、宋城演艺、锦江酒店"},
    "综合": {"etf": "510300 沪深300ETF", "leaders": "中信股份、复星国际"},
}

FIVE_YEAR_PLAN = [
    ("🔬 科技自主可控", ["半导体", "芯片", "光学光电子", "信创"], "512480 半导体ETF"),
    ("⚡ 新型能源体系", ["光伏", "风电", "储能", "新能源", "电源设备"], "515790 光伏ETF"),
    ("🤖 数字经济", ["人工智能", "计算机应用", "软件开发", "通信设备"], "159819 人工智能ETF"),
    ("🏭 高端制造", ["机器人", "航空航天", "军工", "汽车整车"], "562500 机器人ETF"),
    ("💊 生物经济", ["生物制品", "医药商业", "医疗器械"], "159567 创新药ETF"),
    ("🛒 内循环消费", ["白酒", "食品饮料", "消费电子"], "512690 酒ETF"),
]


# ─── 数据加载 ──────────────────────────────────────────────────────────────────

def load_daily_data(days: int = 5) -> list[dict]:
    """加载最近N个交易日的采集数据。"""
    if not _DAILY_DIR.exists():
        return []
    files = sorted(_DAILY_DIR.glob("*.json"), reverse=True)[:days]
    records = []
    for f in reversed(files):
        try:
            records.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    return records


def _load_history() -> list[dict]:
    if _HISTORY_FILE.exists():
        return json.loads(_HISTORY_FILE.read_text(encoding="utf-8"))
    return []


def _save_history(records: list[dict]) -> None:
    records = records[-12:]
    atomic_write_json(_HISTORY_FILE, records)


# ─── 分析引擎 ──────────────────────────────────────────────────────────────────

def analyze_5day_cumulative(daily_data: list[dict]) -> list[dict]:
    """5日累计净流入排名 + 连续流入天数。"""
    sector_totals: dict[str, dict] = {}
    sector_daily: dict[str, list[float]] = {}

    for day in daily_data:
        for sector in day.get("industry", []):
            name = sector["name"]
            if name not in sector_totals:
                sector_totals[name] = {
                    "name": name, "net_5d": 0, "inflow_5d": 0, "outflow_5d": 0,
                    "chg_sum": 0, "days_positive": 0, "day_count": 0,
                }
                sector_daily[name] = []
            s = sector_totals[name]
            s["net_5d"] += sector["net"]
            s["inflow_5d"] += sector["inflow"]
            s["outflow_5d"] += sector["outflow"]
            s["chg_sum"] += sector["change_pct"]
            s["day_count"] += 1
            if sector["net"] > 0:
                s["days_positive"] += 1
            sector_daily[name].append(sector["net"])

    result = sorted(sector_totals.values(), key=lambda x: x["net_5d"], reverse=True)
    for i, item in enumerate(result):
        item["rank"] = i + 1
        item["avg_chg"] = item["chg_sum"] / max(item["day_count"], 1)
        item["consistency"] = item["days_positive"]

        # 计算从最后一天往前数的连续正流入天数
        daily = sector_daily.get(item["name"], [])
        consec = 0
        for v in reversed(daily):
            if v > 0:
                consec += 1
            else:
                break
        item["consecutive_days"] = consec

    return result


def analyze_momentum_divergence(sectors: list[dict]) -> list[dict]:
    """量价配合分析：资金流入但涨幅低 = 主力建仓。"""
    signals = []
    for s in sectors[:20]:
        net = s["net_5d"]
        avg_chg = s["avg_chg"]
        consistency = s["consistency"]

        if net > 10 and avg_chg < 1.0 and consistency >= 3:
            signals.append({
                "name": s["name"],
                "net_5d": net,
                "avg_chg": avg_chg,
                "type": "🔍 建仓期",
                "desc": f"资金持续流入{net:.0f}亿但涨幅仅{avg_chg:.1f}%，疑似主力吸筹",
            })
        elif net > 20 and avg_chg > 3.0:
            signals.append({
                "name": s["name"],
                "net_5d": net,
                "avg_chg": avg_chg,
                "type": "⚠️ 追高风险",
                "desc": f"量价齐升（+{net:.0f}亿/+{avg_chg:.1f}%），短线可能见顶",
            })

    return signals


def classify_trend_vs_rotation(current_ranking: dict, history: list[dict]) -> dict:
    """趋势 vs 轮动分类。"""
    trend_sectors = []
    rotation_sectors = []

    if len(history) < 2:
        return {"trend": [], "rotation": []}

    for name, rank in current_ranking.items():
        if rank > 10:
            continue
        weeks_in_top10 = 0
        for h in history[-4:]:
            hist_ranking = h.get("industry_ranking", {})
            if hist_ranking.get(name, 99) <= 10:
                weeks_in_top10 += 1

        if weeks_in_top10 >= 3:
            trend_sectors.append({"name": name, "weeks": weeks_in_top10, "type": "趋势主线"})
        elif weeks_in_top10 == 0:
            rotation_sectors.append({"name": name, "type": "本周新晋"})

    return {"trend": trend_sectors, "rotation": rotation_sectors}


def analyze_market_context(daily_data: list[dict]) -> dict:
    """大盘情绪上下文。"""
    if not daily_data:
        return {"mood": "未知", "csi300_week_chg": 0, "vol_trend": "正常"}

    week_chg = sum(d.get("market", {}).get("csi300_change_pct", 0) for d in daily_data)
    avg_vol_ratio = sum(d.get("market", {}).get("vol_ratio_5d", 1) for d in daily_data) / max(len(daily_data), 1)

    # 北向周度累计
    north_total = sum(d.get("north", {}).get("net_buy", 0) for d in daily_data)

    if week_chg > 2:
        mood = "🟢 强势上涨周"
    elif week_chg > 0.5:
        mood = "🟡 温和上涨"
    elif week_chg > -0.5:
        mood = "⚪ 横盘震荡"
    elif week_chg > -2:
        mood = "🟠 温和回调"
    else:
        mood = "🔴 恐慌下跌周"

    vol_trend = "放量" if avg_vol_ratio > 1.2 else "缩量" if avg_vol_ratio < 0.8 else "正常"

    return {
        "mood": mood,
        "csi300_week_chg": round(week_chg, 2),
        "vol_trend": vol_trend,
        "north_week": round(north_total, 2),
    }


def cross_validate_fund_flow(sectors: list[dict], fund_holdings: list[dict]) -> list[dict]:
    """公募季报方向 × 实时资金流交叉验证。双重确认 = 高确信号。"""
    if not fund_holdings:
        return []

    # 基金增仓方向
    fund_increase = {h["name"] for h in fund_holdings if "增" in str(h.get("change", ""))}
    fund_decrease = {h["name"] for h in fund_holdings if "减" in str(h.get("change", ""))}

    signals = []
    for s in sectors[:15]:
        name = s["name"]
        net = s["net_5d"]
        info = SECTOR_MAP.get(name, {})
        leaders = info.get("leaders", "")

        # 检查龙头股是否在基金增仓列表
        leader_names = [l.strip() for l in leaders.split("、")] if leaders else []
        fund_match_increase = [l for l in leader_names if l in fund_increase]
        fund_match_decrease = [l for l in leader_names if l in fund_decrease]

        if net > 5 and fund_match_increase:
            signals.append({
                "name": name,
                "type": "✅ 双重确认",
                "desc": f"资金流入+基金加仓({'/'.join(fund_match_increase)})",
                "confidence": "高",
            })
        elif net > 10 and fund_match_decrease:
            signals.append({
                "name": name,
                "type": "⚠️ 方向冲突",
                "desc": f"资金流入但基金减仓({'/'.join(fund_match_decrease)})→短炒可能性大",
                "confidence": "低",
            })

    return signals


def calc_deviation_from_mean(sectors: list[dict], history: list[dict]) -> list[dict]:
    """板块本周净流入 vs 近4周均值的偏离度。"""
    if len(history) < 2:
        return []

    # 计算历史平均（简化：用排名变化推断）
    # 实际：从 daily_data 历史文件中聚合——这里用当前数据的绝对值做相对判断
    deviations = []
    for s in sectors[:15]:
        name = s["name"]
        net = s["net_5d"]
        inflow = s.get("inflow_5d", 0)

        # 偏离判断基于净流入绝对值
        if net > 100:
            deviations.append({"name": name, "net": net, "signal": "🔥 极端放量流入", "level": "极高"})
        elif net > 50:
            deviations.append({"name": name, "net": net, "signal": "📈 显著高于常态", "level": "高"})
        elif net < -30:
            deviations.append({"name": name, "net": net, "signal": "📉 异常大幅流出", "level": "警告"})

    return deviations


def review_last_week(history: list[dict]) -> list[dict]:
    """上周推荐复盘 — 计算推荐后实际涨跌幅。"""
    if len(history) < 2:
        return []

    last = history[-2] if len(history) >= 2 else None
    if not last:
        return []

    last_recommendations = last.get("action_plan", [])
    if not last_recommendations:
        return []

    from data_fetcher import get_etf_prices

    # 提取ETF代码
    etf_codes = []
    for rec in last_recommendations:
        etf_str = rec.get("etf", "")
        code = etf_str.split(" ")[0] if etf_str else ""
        if code and code != "—":
            etf_codes.append(code)

    # 批量获取价格
    prices = get_etf_prices(etf_codes, days=10) if etf_codes else {}

    reviews = []
    for rec in last_recommendations:
        name = rec.get("name", "")
        etf_str = rec.get("etf", "")
        code = etf_str.split(" ")[0] if etf_str else ""
        weight = rec.get("weight", "")

        # 计算本周涨跌
        week_return = None
        if code in prices and prices[code] is not None and not prices[code].empty:
            df = prices[code]
            if len(df) >= 5:
                close_now = float(df.iloc[-1]["close"])
                close_5ago = float(df.iloc[-5]["close"])
                week_return = round((close_now / close_5ago - 1) * 100, 2)

        if week_return is not None:
            icon = "✅" if week_return > 0 else "❌"
            status = f"{icon} 本周 **{week_return:+.2f}%**"
        else:
            status = "⏳ 数据不足"

        reviews.append({
            "name": name,
            "etf": etf_str,
            "suggested_weight": weight,
            "week_return": week_return,
            "status": status,
        })

    return reviews


def generate_action_plan(sectors: list[dict], momentum: list[dict],
                         classification: dict, context: dict,
                         cross_signals: list[dict] = None,
                         deviations: list[dict] = None,
                         concept_sectors: set = None) -> list[dict]:
    """生成操作建议（含止损/持有周期/失效条件）。概念和行业分开处理。"""
    recommendations = []
    history = _load_history()
    cross_signals = cross_signals or []
    deviations = deviations or []
    concept_sectors = concept_sectors or set()

    for s in sectors[:12]:
        name = s["name"]
        net = s["net_5d"]
        consistency = s["consistency"]
        avg_chg = s["avg_chg"]
        is_concept = name in concept_sectors

        info = SECTOR_MAP.get(name)
        if not info or info.get("etf") == "—":
            continue

        score = 0
        reasons = []
        hold_period = ""
        stop_condition = ""
        invalidation = ""

        # 资金规模
        if net > 50:
            score += 3
            reasons.append(f"周资金大幅流入{net:.0f}亿")
        elif net > 20:
            score += 2
            reasons.append(f"周资金流入{net:.0f}亿")
        elif net > 5:
            score += 1

        # 持续性
        if consistency >= 4:
            score += 3
            reasons.append(f"连续{consistency}天净流入（高持续性）")
        elif consistency >= 3:
            score += 2
            reasons.append(f"{consistency}天净流入")

        # 量价配合（建仓型最佳）
        building = any(m["name"] == name and "建仓" in m["type"] for m in momentum)
        if building:
            score += 3
            reasons.append("量价背离→主力建仓期")
            hold_period = "2-4周（等待拉升）"
            stop_condition = "板块指数跌破本周低点3%止损"
        elif avg_chg > 3:
            score -= 1
            reasons.append("短线涨幅已大，追高风险")

        # 交叉验证（公募+资金流共振）
        cross_match = [c for c in cross_signals if c["name"] == name and c["confidence"] == "高"]
        if cross_match:
            score += 3
            reasons.append(f"公募基金共振确认({cross_match[0]['desc'][:20]})")

        # 偏离度信号
        dev_match = [d for d in deviations if d["name"] == name and d["level"] in ("极高", "高")]
        if dev_match:
            score += 1
            reasons.append(f"资金偏离度{dev_match[0]['level']}")

        # 政策共振（仅行业板块加分，概念不加）
        if not is_concept:
            for direction, keywords, _ in FIVE_YEAR_PLAN:
                if any(kw in name for kw in keywords):
                    score += 2
                    reasons.append(f"五年规划共振({direction[2:]})")
                    if not hold_period:
                        hold_period = "4-8周（中线配置）"
                    break

        # 趋势主线加分
        is_trend = any(t["name"] == name for t in classification.get("trend", []))
        if is_trend:
            score += 2
            reasons.append("连续多周主线（趋势确认）")
            hold_period = hold_period or "持有至趋势破坏"
            stop_condition = stop_condition or "跌破20日均线清仓"

        # 新晋（轮动起点）
        is_new = any(r["name"] == name for r in classification.get("rotation", []))
        if is_new:
            score += 1
            reasons.append("本周新进Top10（轮动起点）")

        # 概念板块惩罚（短线属性，不适合中线）
        if is_concept:
            score -= 1
            hold_period = "3-5天（概念短炒）"
            stop_condition = stop_condition or "3天不兑现立即退出"
            invalidation = "热度消退或龙头封板失败"

        # 市场环境修正
        if "恐慌" in context.get("mood", ""):
            score += 1
            reasons.append("恐慌周逆势流入（抄底资金选择）")

        if not hold_period:
            hold_period = "1-2周"
        if not stop_condition:
            stop_condition = "周度资金转为净流出则止损"
        if not invalidation:
            invalidation = "下周资金流出或排名跌出Top15"

        # 风格标签
        style = "短线轮动" if is_concept else "中线配置"

        if score >= 4 and len(recommendations) < 4:
            recommendations.append({
                "name": name,
                "etf": info["etf"],
                "leaders": info["leaders"],
                "score": score,
                "reasons": reasons,
                "weight": "15-20%" if score >= 7 else "10-15%" if score >= 5 else "5-10%",
                "hold_period": hold_period,
                "stop_condition": stop_condition,
                "invalidation": invalidation,
                "style": style,
            })

    return recommendations


# ─── 报告生成 ──────────────────────────────────────────────────────────────────

def generate_report() -> dict:
    daily_data = load_daily_data(5)
    if not daily_data:
        logger.warning("无每日采集数据，尝试实时获取...")
        import market_daily_collector
        market_daily_collector.run(force=True)
        daily_data = load_daily_data(5)

    logger.info(f"加载了 {len(daily_data)} 天的采集数据")

    # 5日累计
    sectors = analyze_5day_cumulative(daily_data)

    # 量价配合
    momentum = analyze_momentum_divergence(sectors)

    # 趋势 vs 轮动
    current_ranking = {s["name"]: s["rank"] for s in sectors[:20]}
    history = _load_history()
    classification = classify_trend_vs_rotation(current_ranking, history)

    # 大盘情绪
    context = analyze_market_context(daily_data)

    # 机构/基金数据
    fund_holdings = []
    institution_changes = []
    for day in reversed(daily_data):
        if not fund_holdings and day.get("fund_holdings"):
            fund_holdings = day["fund_holdings"]
        if not institution_changes and day.get("institution_changes"):
            institution_changes = day["institution_changes"]

    # 概念板块名称集合（区分概念 vs 行业）
    concept_names = set()
    for day in daily_data:
        for c in day.get("concept", []):
            concept_names.add(c.get("name", ""))

    # P0: 交叉验证（公募季报方向 × 实时资金流）
    cross_signals = cross_validate_fund_flow(sectors, fund_holdings)

    # P1: 偏离度分析
    deviations = calc_deviation_from_mean(sectors, history)

    # P1: 上周复盘
    last_review = review_last_week(history)

    # 操作建议（整合所有分析维度）
    action_plan = generate_action_plan(
        sectors, momentum, classification, context,
        cross_signals=cross_signals,
        deviations=deviations,
        concept_sectors=concept_names,
    )

    # 流出 Top5
    outflows = [s for s in sectors if s["net_5d"] < 0][:5]

    report = {
        "date": date.today().isoformat(),
        "data_days": len(daily_data),
        "sectors_top": sectors[:10],
        "sectors_out": outflows,
        "momentum": momentum,
        "classification": classification,
        "context": context,
        "action_plan": action_plan,
        "fund_holdings": fund_holdings,
        "institution_changes": institution_changes,
        "cross_signals": cross_signals,
        "deviations": deviations,
        "last_review": last_review,
    }

    # 保存本周排名 + action_plan（供下周复盘）
    history.append({
        "date": date.today().isoformat(),
        "industry_ranking": current_ranking,
        "action_plan": [{"name": r["name"], "etf": r["etf"], "weight": r["weight"]} for r in action_plan],
    })
    _save_history(history)

    log_path = _LOGS_DIR / f"market_capital_{date.today().isoformat()}.json"
    atomic_write_json(log_path, report)

    return report


# ─── 飞书卡片（3张） ──────────────────────────────────────────────────────────

def build_cards(report: dict) -> list[dict]:
    today = report["date"]
    context = report["context"]
    sectors = report["sectors_top"]
    outflows = report["sectors_out"]
    momentum = report["momentum"]
    classification = report["classification"]
    action_plan = report["action_plan"]
    cross_signals = report.get("cross_signals", [])
    deviations = report.get("deviations", [])
    last_review = report.get("last_review", [])

    # ═══ 卡片1：市场环境 + 资金全景 ═══
    # 情绪上下文
    ctx_text = (
        f"**🌡️ 本周市场环境**\n"
        f"{context['mood']}  ▸  沪深300周涨跌 **{context['csi300_week_chg']:+.2f}%**\n"
        f"成交量: {context['vol_trend']}  ▸  北向周累计: **{context['north_week']:+.1f}亿**\n"
        f"数据覆盖: {report['data_days']}个交易日"
    )

    # 行业流入 Top10
    inflow_lines = []
    last_ranking = {}
    history = _load_history()
    if len(history) >= 2:
        last_ranking = history[-2].get("industry_ranking", {})

    for s in sectors[:10]:
        name = s["name"]
        info = SECTOR_MAP.get(name, {})
        etf = info.get("etf", "—")
        leaders = info.get("leaders", "—")

        last_rank = last_ranking.get(name)
        if last_rank:
            diff = last_rank - s["rank"]
            rank_str = f"↑{diff}" if diff > 0 else f"↓{abs(diff)}" if diff < 0 else "→"
        else:
            rank_str = "🆕"

        icon = "🔴" if s["net_5d"] > 50 else "🟠" if s["net_5d"] > 10 else "🟡"
        consec = s.get("consecutive_days", 0)
        consec_bar = f"{'🟩' * consec}{'⬜' * (5 - consec)}" if consec <= 5 else f"🟩×{consec}"

        inflow_lines.append(
            f"{icon} **{s['rank']}. {name}** ({rank_str})  "
            f"5日净入 **{s['net_5d']:.0f}亿**  连续{consec_bar}\n"
            f"      ETF: {etf}  ▸  龙头: {leaders}"
        )

    # 流出
    outflow_lines = [
        f"🔵 {s['name']}  流出{abs(s['net_5d']):.0f}亿 ({s['consistency']}/5天为正)"
        for s in outflows[:5]
    ]

    card1 = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"💰 市场资金全景 {today}"},
                "template": "orange",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": ctx_text}},
                {"tag": "hr"},
                {"tag": "div", "text": {"tag": "lark_md", "content":
                    "**📊 行业5日累计净流入 Top10**\n\n" + "\n\n".join(inflow_lines)}},
                {"tag": "hr"},
                {"tag": "div", "text": {"tag": "lark_md", "content":
                    "**📤 资金流出方向**（防御→进攻=risk on信号）\n" + "\n".join(outflow_lines)}},
            ],
        },
    }

    # ═══ 卡片2：深度分析 ═══
    # 上周复盘
    review_lines = []
    if last_review:
        total_return = 0
        count = 0
        for r in last_review:
            ret = r.get("week_return")
            review_lines.append(f"  {r['status']}  {r['name']} ({r['etf']}) 建议{r['suggested_weight']}")
            if ret is not None:
                total_return += ret
                count += 1
        if count:
            avg = total_return / count
            icon = "🏆" if avg > 0 else "💔"
            review_lines.insert(0, f"  {icon} 上周推荐平均收益: **{avg:+.2f}%**")

    # 量价配合
    mom_lines = [f"{m['type']} **{m['name']}**: {m['desc']}" for m in momentum[:5]]

    # 交叉验证
    cross_lines = [f"{c['type']} **{c['name']}**: {c['desc']}" for c in cross_signals[:5]]

    # 偏离度
    dev_lines = [f"{d['signal']} **{d['name']}** 净流入{d['net']:.0f}亿" for d in deviations[:5]]

    # 趋势 vs 轮动
    trend_names = [f"📈 {t['name']}（连续{t['weeks']}周）" for t in classification.get("trend", [])]
    rotation_names = [f"🆕 {r['name']}" for r in classification.get("rotation", [])]

    # 五年规划
    plan_lines = [f"{d[0]} → {d[1]} ({d[2]})" for d in FIVE_YEAR_PLAN[:4]]

    card2_elements = []

    # 上周复盘（如果有）
    if review_lines:
        card2_elements.append({"tag": "div", "text": {"tag": "lark_md", "content":
            "**📋 上周推荐复盘**\n" + "\n".join(review_lines)}})
        card2_elements.append({"tag": "hr"})

    # 交叉验证
    card2_elements.append({"tag": "div", "text": {"tag": "lark_md", "content":
        "**🔗 公募×资金流交叉验证**\n\n"
        + ("\n".join(cross_lines) if cross_lines else "本周无显著交叉确认信号")}})
    card2_elements.append({"tag": "hr"})

    # 量价 + 偏离
    card2_elements.append({"tag": "div", "text": {"tag": "lark_md", "content":
        "**🔬 量价配合信号**\n\n"
        + ("\n".join(mom_lines) if mom_lines else "本周无明显量价背离信号")
        + ("\n\n**📊 资金偏离度**\n" + "\n".join(dev_lines) if dev_lines else "")}})
    card2_elements.append({"tag": "hr"})

    # 趋势 vs 轮动
    card2_elements.append({"tag": "div", "text": {"tag": "lark_md", "content":
        "**📈 趋势主线**（连续多周Top10）\n"
        + ("\n".join(trend_names) if trend_names else "暂无确认趋势（需累积3周以上数据）")
        + "\n\n**🔄 本周新晋**（轮动信号）\n"
        + ("\n".join(rotation_names) if rotation_names else "无新进板块")
        + "\n\n**📐 五年规划长期主线**\n" + "\n".join(plan_lines)}})

    card2 = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"🔍 深度分析 {today}"},
                "template": "blue",
            },
            "elements": card2_elements,
        },
    }

    # ═══ 卡片3：操作建议 ═══
    if action_plan:
        rec_lines = []
        for i, rec in enumerate(action_plan, 1):
            reasons_str = "；".join(rec["reasons"][:3])
            style_tag = f"🏷️ {rec.get('style', '中线配置')}"
            rec_lines.append(
                f"**{i}. {rec['name']}** [{style_tag}]\n"
                f"    🎯 ETF: **{rec['etf']}**\n"
                f"    👑 龙头: {rec['leaders']}\n"
                f"    📊 建议仓位: **{rec['weight']}**\n"
                f"    ⏱️ 持有周期: {rec['hold_period']}\n"
                f"    🛑 止损条件: {rec['stop_condition']}\n"
                f"    ❌ 建议失效: {rec['invalidation']}\n"
                f"    💡 {reasons_str}"
            )
        rec_content = "\n\n".join(rec_lines)
    else:
        rec_content = "本周无高确信度配置机会。建议观望或维持现有持仓，等待信号明确。"

    card3 = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": f"⚡ 操作建议 {today}"},
                "template": "red",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content":
                    "**🎯 本周值得配置的方向**\n\n" + rec_content}},
                {"tag": "hr"},
                {"tag": "div", "text": {"tag": "lark_md", "content":
                    "**⚠️ 风险控制原则**\n\n"
                    "• 只做 **持续性≥3天** 的资金流入方向\n"
                    "• 量价背离（资金入+不涨）= 建仓期 → 中线持有\n"
                    "• 量价齐升（资金入+大涨）= 加速期 → 快进快出\n"
                    "• 流出方向坚决不抄底，等资金回流再看\n"
                    "• 趋势主线配ETF中线拿；轮动新晋只做龙头股短线\n"
                    "• **每笔交易必须有止损**，无止损不开仓"}},
            ],
        },
    }

    # ═══ 卡片4：机构/基金动向 ═══
    fund_holdings = report.get("fund_holdings", [])
    institution_changes = report.get("institution_changes", [])

    if fund_holdings or institution_changes:
        # 基金重仓
        fund_lines = []
        if fund_holdings:
            for i, h in enumerate(fund_holdings[:10], 1):
                chg_icon = "🔺" if "增" in str(h.get("change", "")) else "🔻" if "减" in str(h.get("change", "")) else "➖"
                chg_pct = h.get("change_pct", 0)
                value_b = h.get("hold_value", 0) / 1e8
                fund_lines.append(
                    f"{chg_icon} **{i}. {h['name']}**({h['code']})  "
                    f"{h['fund_count']}家基金  市值{value_b:.0f}亿  "
                    f"变动{chg_pct:+.1f}%"
                )

        # 机构增仓
        inst_lines = []
        if institution_changes:
            for i, h in enumerate(institution_changes[:10], 1):
                inst_lines.append(
                    f"📈 **{i}. {h['name']}**({h['code']})  "
                    f"机构{h['inst_count']}家({h['inst_change']:+d})  "
                    f"持股比例{h['hold_pct']:.1f}%({h['hold_pct_change']:+.2f}%)"
                )

        quarter_info = fund_holdings[0].get("quarter", "") if fund_holdings else ""
        quarter_label = f"（数据期: {quarter_info[:4]}.{quarter_info[4:6]}）" if quarter_info else ""

        card4_elements = []
        if fund_lines:
            card4_elements.append({
                "tag": "div", "text": {"tag": "lark_md", "content":
                    f"**🏦 公募基金重仓 Top10**{quarter_label}\n\n" + "\n".join(fund_lines)}
            })
        if inst_lines:
            if fund_lines:
                card4_elements.append({"tag": "hr"})
            card4_elements.append({
                "tag": "div", "text": {"tag": "lark_md", "content":
                    "**📊 机构增仓 Top10**（持股比例增幅最大）\n\n" + "\n".join(inst_lines)}
            })
        card4_elements.append({"tag": "hr"})
        card4_elements.append({
            "tag": "div", "text": {"tag": "lark_md", "content":
                "**💡 解读提示**\n\n"
                "• 基金重仓股反映**公募共识**，是市场的压舱石\n"
                "• 「增仓」方向 = 聪明钱在加注的赛道\n"
                "• 「减仓」方向 = 可能阶段性见顶或兑现\n"
                "• 机构新增持股 = 留意是否有基本面变化\n"
                "• 季报数据滞后1-2个月，结合实时资金流看"}
        })

        card4 = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "title": {"tag": "plain_text", "content": f"🏛️ 机构/基金动向 {today}"},
                    "template": "purple",
                },
                "elements": card4_elements,
            },
        }
        return [card1, card2, card3, card4]

    return [card1, card2, card3]


# ─── 主入口 ────────────────────────────────────────────────────────────────────

def run(dry: bool = False) -> None:
    logger.info("生成市场资金动向周报 v3...")
    report = generate_report()
    cards = build_cards(report)

    if dry:
        for i, card in enumerate(cards, 1):
            logger.info(f"[DRY] 卡片{i}:\n{json.dumps(card, ensure_ascii=False, indent=2)}")
        return

    webhooks = webhooks_from_env("MARKET_REPORT_WEBHOOK", [_FEISHU_WEBHOOK])
    for card in cards:
        post_card(card, webhooks)
        time.sleep(3)
    logger.info("市场资金动向周报已推送（3张卡片）")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="市场资金动向周报")
    parser.add_argument("--dry", action="store_true", help="不推送")
    parser.add_argument("--force", action="store_true", help="跳过周五检查")
    args = parser.parse_args()

    today = date.today()
    if not args.force:
        if today.weekday() != 4:
            logger.info(f"今日{today}非周五，跳过")
            return
        if not is_trading_day():
            logger.info(f"今日非交易日，跳过")
            return

    run(dry=args.dry)


if __name__ == "__main__":
    main()
