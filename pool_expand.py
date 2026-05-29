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


def scan_new_faces() -> list[dict]:
    """用腾讯K线扫描扩展池中不在研究池的标的，计算5日涨幅并排序"""
    import sys
    sys.path.insert(0, str(_DIR))
    from data_fetcher import _tencent_kline

    discoveries: list[dict] = []

    for sector, stocks in SCAN_POOL.items():
        for code, name in stocks:
            if code in _EXISTING_CODES:
                continue
            kl = _tencent_kline(f"{_market_prefix(code)}{code}", 25)
            if not kl or len(kl) < 6:
                continue
            try:
                c_now = float(kl[-1][2])
                c_5 = float(kl[-6][2])
                c_prev = float(kl[-2][2])
                chg_5d = (c_now / c_5 - 1) * 100
                chg_today = (c_now / c_prev - 1) * 100
                if chg_5d < 5:
                    continue
                discoveries.append({
                    "code": code,
                    "name": name,
                    "sector": sector,
                    "chg_5d": round(chg_5d, 1),
                    "chg_today": round(chg_today, 1),
                    "close": c_now,
                })
            except (IndexError, ValueError, ZeroDivisionError):
                continue
        time.sleep(0.3)

    discoveries.sort(key=lambda x: -x["chg_5d"])
    return discoveries


def build_card(discoveries: list[dict], ts: str) -> dict:
    elements: list[dict] = []

    if not discoveries:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
            "content": "本周未发现新的候选标的（现有池已覆盖各板块主力）"}})
    else:
        elements.append({"tag": "div", "text": {"tag": "lark_md",
            "content": f"发现 **{len(discoveries)}** 只不在研究池中的新面孔\n"
                       f"以下标的近5日涨幅突出且不在 STOCK_UNIVERSE 中，建议人工审核后决定是否纳入"}})
        elements.append({"tag": "hr"})

        lines = []
        for i, d in enumerate(discoveries[:15], 1):
            lines.append(
                f"{i}. **{d['name']}**（{d['code']}）｜{d['sector']}\n"
                f"   5日涨幅 **{d['chg_5d']:.1f}%**  今日{d['chg_today']:+.1f}%  "
                f"价格{d['close']:.2f}"
            )
        elements.append({"tag": "div", "text": {"tag": "lark_md",
            "content": "\n\n".join(lines)}})
        elements.append({"tag": "hr"})

        elements.append({"tag": "div", "text": {"tag": "lark_md",
            "content": "**审核要点**\n"
                       "1. 是否属于AI产业链核心环节？\n"
                       "2. 公募/北向是否有增持迹象？\n"
                       "3. 市值>100亿？流动性OK？\n"
                       "4. 是否只是短期游资炒作？"}})

    elements.append({
        "tag": "note",
        "elements": [{"tag": "plain_text",
            "content": "研究池扩充建议 ｜ 每周一次 ｜ 仅供参考，需人工确认后手动加入config.py"}],
    })

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text",
                    "content": f"🔍 研究池扩充建议 ｜ {ts}"},
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
        log.info(f"  {d['name']}({d['code']}) {d['sector']} 5日+{d['chg_5d']:.1f}%")

    card = build_card(discoveries, ts)

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
