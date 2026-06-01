#!/usr/bin/env python3
"""
个股研究池扩充建议（每周运行一次）

扫描逻辑：
  1. 当前STOCK_UNIVERSE中各cluster的ETF成分股变化（权重提升=被动资金加仓）
  2. 近5日板块涨幅TOP标的中不在研究池内的"新面孔"
  3. 输出建议列表（不自动入池，人工确认后手动加入config.py）

用法:
    python3 signal_system/pool_expand.py          # 扫描+输出建议
    python3 signal_system/pool_expand.py --push   # 扫描+推送飞书
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests

_DIR = Path(__file__).parent
sys.path.insert(0, str(_DIR))

from config import STOCK_UNIVERSE, ETF_UNIVERSE
from data_fetcher import market_prefix as _market_prefix
from feishu_pusher import post_card as _post_card

FEISHU_WEBHOOK = "https://open.feishu.cn/open-apis/bot/v2/hook/2335ea51-ea2b-4050-8ac0-cd18f7e66dbb"

LOG_FILE = _DIR / "logs" / f"pool_expand_{date.today():%Y%m}.log"
LOG_FILE.parent.mkdir(exist_ok=True)
logging.basicConfig(
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8"), logging.StreamHandler(sys.stdout)],
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Referer": "https://finance.qq.com/",
}

# 现有池内所有代码（用于过滤已有的）
_EXISTING_CODES: set[str] = set(STOCK_UNIVERSE.keys()) | set(ETF_UNIVERSE.keys())

# 扩展扫描池：各板块中可能成为新龙头的标的（不在STOCK_UNIVERSE中）
# 每季度人工审视更新，来源: ETF成分股 + 同花顺板块排行 + 公募季报新进
SCAN_POOL: dict[str, list[tuple[str, str]]] = {
    "光通信/CPO": [
        ("301205", "联特科技"), ("300710", "万隆光电"), ("688069", "德林海"),
        ("688556", "高测股份"), ("301162", "国能日新"), ("688378", "奥来德"),
    ],
    "MLCC/被动元器件": [
        ("000636", "风华高科"), ("300408", "三环集团"), ("002138", "顺络电子"),
        ("002484", "江海股份"), ("300975", "商络电子"), ("301511", "德福科技"),
        ("603738", "泰晶科技"), ("603989", "艾华集团"),
    ],
    "半导体设备材料": [
        ("688380", "中微半导"), ("300236", "上海新阳"), ("688183", "生益电子"),
        ("688568", "中科星图"), ("300373", "扬杰科技"), ("688536", "思瑞浦"),
    ],
    "AI服务器/算力": [
        ("603236", "移远通信"), ("688579", "山大地纬"), ("300474", "景嘉微"),
        ("688032", "禾迈股份"), ("300496", "中科创达"), ("688023", "安恒信息"),
    ],
    "铜缆/连接器": [
        ("002130", "沃尔核材"), ("300548", "博创科技"), ("300252", "金信诺"),
        ("002916", "深南电路"), ("603068", "博通集成"),
    ],
    "液冷/散热": [
        ("002837", "英维克"), ("300602", "飞荣达"), ("301487", "高澜股份"),
        ("301018", "申菱环境"), ("603912", "佳力图"), ("831834", "曙光数创"),
    ],
    "服务器电源": [
        ("002851", "麦格米特"), ("300870", "欧陆通"), ("002364", "中恒电气"),
        ("603063", "禾望电气"),
    ],
    "交换机/网络": [
        ("301165", "锐捷网络"), ("688702", "盛科通信"), ("002396", "星网锐捷"),
    ],
    "AI应用": [
        ("300624", "万兴科技"), ("688083", "中望软件"), ("300364", "中文在线"),
        ("300182", "捷成股份"),
    ],
}


def _expand_scan_pool_from_etfs() -> list[tuple[str, str, str]]:
    """
    从ETF_UNIVERSE的top5字段自动提取成分股代码，找出不在研究池+扫描池中的新面孔。
    返回 [(code, name, sector), ...]
    """
    import re
    scan_pool_codes = set()
    for stocks in SCAN_POOL.values():
        scan_pool_codes.update(code for code, _ in stocks)

    all_known = _EXISTING_CODES | scan_pool_codes
    extras: list[tuple[str, str, str]] = []
    seen: set[str] = set()

    for etf_code, info in ETF_UNIVERSE.items():
        top5 = info.get("top5", "")
        cluster = info.get("cluster", "ETF成分")
        for match in re.finditer(r"(\S+?)\((\d{6})\)", top5):
            name, code = match.group(1), match.group(2)
            if code not in all_known and code not in seen:
                seen.add(code)
                extras.append((code, name, f"ETF成分/{cluster}"))

    return extras[:30]


def _load_last_week_results() -> list[dict]:
    """读取上次推荐的A级标的，返回 [{code, name, score, close_then, sector}, ...]"""
    from datetime import timedelta
    for days_back in range(1, 10):
        prev_date = (date.today() - timedelta(days=days_back)).isoformat()
        prev_file = _DIR / "logs" / f"pool_expand_{prev_date}.json"
        if prev_file.exists():
            try:
                data = json.loads(prev_file.read_text(encoding="utf-8"))
                return [d for d in data if d.get("grade") == "A"]
            except Exception:
                pass
    return []


def scan_new_faces() -> list[dict]:
    """
    用腾讯K线扫描扩展池标的，计算技术指标并评审。
    返回带评级的候选列表（A=建议入池 / B=观望 / C=回避）
    """
    import sys
    sys.path.insert(0, str(_DIR))
    from data_fetcher import _tencent_kline

    discoveries: list[dict] = []

    # 合并：静态SCAN_POOL + ETF成分股动态扩展
    all_targets: list[tuple[str, str, str]] = []
    for sector, stocks in SCAN_POOL.items():
        for code, name in stocks:
            all_targets.append((code, name, sector))

    etf_extras = _expand_scan_pool_from_etfs()
    all_targets.extend(etf_extras)
    log.info(f"扫描范围: 静态池{sum(len(v) for v in SCAN_POOL.values())}只 + ETF成分扩展{len(etf_extras)}只 = {len(all_targets)}只")

    for code, name, sector in all_targets:
        if code in _EXISTING_CODES:
            continue
        kl = _tencent_kline(f"{_market_prefix(code)}{code}", 30)
        if not kl or len(kl) < 15:
            continue
        try:
            closes = [float(k[2]) for k in kl]
            volumes = [float(k[5]) for k in kl]
            c_now = closes[-1]
            c_5 = closes[-6] if len(closes) >= 6 else c_now
            c_20 = closes[-21] if len(closes) >= 21 else closes[0]
            c_prev = closes[-2]

            chg_5d = (c_now / c_5 - 1) * 100
            chg_today = (c_now / c_prev - 1) * 100
            if chg_5d < 5:
                continue

            ma20 = sum(closes[-20:]) / min(20, len(closes))
            dev_ma20 = (c_now - ma20) / ma20 * 100

            import numpy as np
            _c = np.array(closes[-15:])
            _d = np.diff(_c)
            _gain = np.mean(_d[_d > 0]) if np.any(_d > 0) else 0
            _loss = -np.mean(_d[_d < 0]) if np.any(_d < 0) else 1e-9
            rsi = 100 - 100 / (1 + _gain / max(_loss, 1e-9))

            vol_now = volumes[-1] if volumes else 0
            vol_avg = sum(volumes[-20:]) / min(20, len(volumes)) if volumes else 1
            vol_ratio = vol_now / max(vol_avg, 1)

            ma12 = sum(closes[-12:]) / 12 if len(closes) >= 12 else c_now
            ma26 = sum(closes[-min(26, len(closes)):]) / min(26, len(closes))
            dif = ma12 - ma26
            macd_ok = dif > 0

            score = 0
            score_details = []

            if c_now > ma20 and macd_ok:
                score += 25; score_details.append("趋势25/25")
            elif c_now > ma20:
                score += 18; score_details.append("趋势18/25")
            elif macd_ok:
                score += 12; score_details.append("趋势12/25")
            else:
                score += 5; score_details.append("趋势5/25")

            if 40 <= rsi <= 60:
                score += 25; score_details.append("动量25/25")
            elif 30 <= rsi <= 70:
                _mom = 25 - abs(rsi - 50) * 0.5
                score += round(_mom); score_details.append(f"动量{round(_mom)}/25")
            else:
                _mom = max(0, 25 - abs(rsi - 50) * 0.8)
                score += round(_mom); score_details.append(f"动量{round(_mom)}/25")

            if vol_ratio >= 2.0:
                score += 20; score_details.append("量能20/20")
            elif vol_ratio >= 1.5:
                score += 16; score_details.append("量能16/20")
            elif vol_ratio >= 1.0:
                score += 12; score_details.append("量能12/20")
            elif vol_ratio >= 0.7:
                score += 6; score_details.append("量能6/20")
            else:
                score += 2; score_details.append("量能2/20")

            if 5 <= chg_5d <= 12:
                score += 20; score_details.append("时机20/20")
            elif 12 < chg_5d <= 20:
                score += 14; score_details.append("时机14/20")
            elif 20 < chg_5d <= 30:
                score += 8; score_details.append("时机8/20")
            else:
                score += 3; score_details.append("时机3/20")

            _hot_sectors = {"MLCC/被动元器件", "光通信/CPO", "铜缆/连接器", "液冷/散热", "服务器电源"}
            if sector in _hot_sectors:
                score += 10; score_details.append("赛道10/10")
            else:
                score += 5; score_details.append("赛道5/10")

            if score >= 70:
                grade = "A"
            elif score >= 45:
                grade = "B"
            else:
                grade = "C"

            grade_reasons = []
            if grade == "A":
                if chg_5d <= 12:
                    grade_reasons.append("启动初期(最佳买点)")
                if vol_ratio >= 1.3:
                    grade_reasons.append("放量确认")
                if c_now > ma20 and macd_ok:
                    grade_reasons.append("技术面完整")
                if not grade_reasons:
                    grade_reasons.append("综合评分优秀")
            elif grade == "B":
                if chg_5d > 20:
                    grade_reasons.append(f"涨幅较大+{chg_5d:.0f}%")
                if rsi > 65:
                    grade_reasons.append(f"RSI偏高{rsi:.0f}")
                if vol_ratio < 0.8:
                    grade_reasons.append("缩量")
                if c_now < ma20:
                    grade_reasons.append("仍在MA20下方")
                if not grade_reasons:
                    grade_reasons.append("中性观望")
            else:
                if rsi > 80:
                    grade_reasons.append(f"RSI超买{rsi:.0f}")
                if dev_ma20 > 30:
                    grade_reasons.append(f"偏离MA20+{dev_ma20:.0f}%")
                if chg_5d > 30 and vol_ratio < 0.8:
                    grade_reasons.append("暴涨+缩量=游资")
                if not grade_reasons:
                    grade_reasons.append("技术面恶化")

            discoveries.append({
                "code": code,
                "name": name,
                "sector": sector,
                "chg_5d": round(chg_5d, 1),
                "chg_today": round(chg_today, 1),
                "close": c_now,
                "rsi": round(rsi, 0),
                "vol_ratio": round(vol_ratio, 1),
                "dev_ma20": round(dev_ma20, 1),
                "macd_ok": macd_ok,
                "grade": grade,
                "score": score,
                "score_details": score_details,
                "grade_reasons": grade_reasons,
            })
        except (IndexError, ValueError, ZeroDivisionError):
            continue
        time.sleep(0.1)

    discoveries.sort(key=lambda x: -x["score"])
    return discoveries


def build_card(discoveries: list[dict], ts: str, retrospective: list[dict] | None = None) -> dict:
    elements: list[dict] = []

    # ── 上周推荐回溯（卡片最顶部）──
    if retrospective:
        retro_lines = []
        wins = sum(1 for r in retrospective if r["return_pct"] > 0)
        avg_ret = sum(r["return_pct"] for r in retrospective) / len(retrospective)
        for r in retrospective:
            icon = "✅" if r["return_pct"] > 0 else "❌"
            retro_lines.append(
                f"{icon} **{r['name']}** 评分{r['score_then']}　"
                f"{r['close_then']:.2f}→{r['close_now']:.2f}　**{r['return_pct']:+.1f}%**"
            )
        elements.append({"tag": "div", "text": {"tag": "lark_md",
            "content": (
                f"**📊 上次推荐回溯**　胜率 {wins}/{len(retrospective)}　"
                f"平均收益 **{avg_ret:+.1f}%**\n" + "\n".join(retro_lines)
            )}})
        elements.append({"tag": "hr"})

    if not discoveries:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
            "content": "本周未发现新的候选标的（现有池已覆盖各板块主力）"}})
        return _wrap_card(elements, ts)

    # 分级统计
    grade_a = [d for d in discoveries if d["grade"] == "A"]
    grade_b = [d for d in discoveries if d["grade"] == "B"]
    grade_c = [d for d in discoveries if d["grade"] == "C"]

    # ── 总览 ──
    elements.append({"tag": "div", "text": {"tag": "lark_md",
        "content": (
            f"扫描 **{sum(len(v) for v in SCAN_POOL.values())}** 只候选，"
            f"发现 **{len(discoveries)}** 只活跃标的\n"
            f"🟢 建议入池 **{len(grade_a)}** 只　"
            f"🟡 观望 **{len(grade_b)}** 只　"
            f"🔴 回避 **{len(grade_c)}** 只"
        )}})
    elements.append({"tag": "hr"})

    # ── A级：建议入池 ──
    if grade_a:
        lines = []
        for d in grade_a[:8]:
            ma_tag = "✅站MA20" if d["dev_ma20"] > 0 else "❌破MA20"
            vol_tag = f"量{d['vol_ratio']:.1f}x" + ("📈" if d["vol_ratio"] >= 1.3 else "")
            macd_tag = "MACD✅" if d["macd_ok"] else "MACD❌"
            bar = "█" * (d["score"] // 10) + "░" * (10 - d["score"] // 10)
            lines.append(
                f"🟢 **{d['name']}**（{d['code']}）｜{d['sector']}\n"
                f"　　`{bar}` **{d['score']}/100**　{' '.join(d['score_details'])}\n"
                f"　　5日 **+{d['chg_5d']:.1f}%**　今日{d['chg_today']:+.1f}%　"
                f"价格{d['close']:.2f}\n"
                f"　　RSI={d['rsi']:.0f}　{ma_tag}({d['dev_ma20']:+.1f}%)　"
                f"{vol_tag}　{macd_tag}\n"
                f"　　💡 {'、'.join(d['grade_reasons'])}"
            )
        elements.append({"tag": "div", "text": {"tag": "lark_md",
            "content": "**🟢 ━━ 建议入池（评分≥70）━━**\n\n" + "\n\n".join(lines)}})
        elements.append({"tag": "hr"})

    # ── B级：观望 ──
    if grade_b:
        lines = []
        for d in grade_b[:6]:
            bar = "█" * (d["score"] // 10) + "░" * (10 - d["score"] // 10)
            lines.append(
                f"🟡 **{d['name']}**（{d['code']}）｜{d['sector']}　"
                f"`{bar}` **{d['score']}/100**\n"
                f"　　5日+{d['chg_5d']:.1f}%　RSI={d['rsi']:.0f}　"
                f"偏MA20={d['dev_ma20']:+.1f}%　量{d['vol_ratio']:.1f}x\n"
                f"　　⚠️ {'、'.join(d['grade_reasons'])}"
            )
        elements.append({"tag": "div", "text": {"tag": "lark_md",
            "content": "**🟡 ━━ 观望（评分45-69）━━**\n\n" + "\n\n".join(lines)}})
        elements.append({"tag": "hr"})

    # ── C级：回避 ──
    if grade_c:
        lines = []
        for d in grade_c[:4]:
            lines.append(
                f"🔴 {d['name']}（{d['code']}）{d['sector']}　"
                f"5日+{d['chg_5d']:.1f}%　RSI={d['rsi']:.0f}　"
                f"{'、'.join(d['grade_reasons'])}"
            )
        elements.append({"tag": "div", "text": {"tag": "lark_md",
            "content": "**🔴 回避（超买/游资/破位）**\n" + "\n".join(lines)}})
        elements.append({"tag": "hr"})

    # ── 入池操作提示 ──
    if grade_a:
        top_name = grade_a[0]["name"]
        elements.append({"tag": "div", "text": {"tag": "lark_md",
            "content": (
                f"**操作建议**\n"
                f"1. 将🟢标的加入 `config.py → STOCK_UNIVERSE`\n"
                f"2. 设定 `pool: \"watch\"` 观察1-2周\n"
                f"3. 确认持续性后升级为 `candidate` 或 `core`\n"
                f"4. 本周首选关注：**{top_name}**"
            )}})

    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text",
            "content": "研究池扩充 ｜ A=建议入池 B=观望 C=回避 ｜ 评级基于RSI/MA20/量能/MACD"}],
    })

    return _wrap_card(elements, ts)


def _wrap_card(elements: list[dict], ts: str) -> dict:
    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text",
                    "content": f"🔍 研究池扩充·技术评审 ｜ {ts}"},
                "template": "indigo",
            },
            "elements": elements,
        },
    }


def main(push: bool = False) -> None:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    log.info(f"=== 研究池扩充扫描 {ts} ===")
    log.info(f"现有池: {len(_EXISTING_CODES)} 只")

    discoveries = scan_new_faces()
    log.info(f"新发现: {len(discoveries)} 只")
    for d in discoveries[:10]:
        log.info(f"  {d['name']}({d['code']}) {d['sector']} 5日+{d['chg_5d']:.1f}% 评分{d['score']}")

    # 上周推荐回溯
    retrospective: list[dict] = []
    last_week_a = _load_last_week_results()
    if last_week_a:
        log.info(f"回溯上次A级推荐: {len(last_week_a)} 只")
        from data_fetcher import _tencent_kline
        for prev in last_week_a[:5]:
            code = prev["code"]
            kl = _tencent_kline(f"{_market_prefix(code)}{code}", 10)
            if kl and len(kl) >= 2:
                c_now = float(kl[-1][2])
                c_then = prev.get("close", c_now)
                if c_then > 0:
                    ret = (c_now / c_then - 1) * 100
                    retrospective.append({
                        "code": code,
                        "name": prev.get("name", code),
                        "score_then": prev.get("score", 0),
                        "close_then": c_then,
                        "close_now": c_now,
                        "return_pct": round(ret, 1),
                    })
                    log.info(f"  回溯: {prev.get('name',code)} 评分{prev.get('score',0)} → {ret:+.1f}%")

    card = build_card(discoveries, ts, retrospective=retrospective)

    if push:
        _post_card(card, [FEISHU_WEBHOOK])
        log.info("推送完成")
    else:
        for el in card["card"]["elements"]:
            if el.get("tag") == "div":
                txt = el.get("text", {}).get("content", "")
                if txt:
                    print(txt[:400])
                    print()

    snap = _DIR / "logs" / f"pool_expand_{date.today()}.json"
    snap.write_text(json.dumps(discoveries[:20], ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"快照: {snap}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--push", action="store_true", help="推送飞书")
    args = ap.parse_args()
    main(push=args.push)
