"""
尾盘择时飞书通知
推送：大盘情绪 + 技术面买点清单
"""

from __future__ import annotations
import logging
from datetime import datetime
from typing import Optional

from config import FEISHU_WEBHOOK, FEISHU_TAIL_WEBHOOKS, DEFENSIVE_ROTATION_POOL
from tail_market_scanner import TailMarketReading, STAR_MAP
from rotation_advisor import format_rotation_card_md as _fmt_rotation
from feishu_pusher import post_card

logger = logging.getLogger(__name__)

STAR_EMOJI = {3: "🟢", 2: "🔵", 1: "🟡", 0: "⚪"}
PANIC_EMOJI = {True: "😱", False: "😐"}


def _format_signal(s) -> str:
    em = STAR_EMOJI.get(s.stars, "⚪")
    star_label = STAR_MAP.get(s.stars, "—")
    trigger_str = "\n    ".join(f"✓ {t}" for t in s.triggers) if s.triggers else "— 无明显信号"
    caution_str = ("\n    " + "\n    ".join(f"⚠️ {c}" for c in s.cautions)) if s.cautions else ""

    flow = getattr(s, "main_force_flow", 0.0)
    flow_str = ""
    if abs(flow) >= 0.1:
        arrow = "↑" if flow > 0 else "↓"
        flow_str = f" | 主力{arrow}{abs(flow):.2f}亿"

    ma60 = getattr(s, "ma60_trend_pct", 0.0)
    ma60_str = ""
    if abs(ma60) >= 0.2:
        arrow = "↑" if ma60 > 0 else "↓"
        ma60_str = f" | MA60{arrow}"

    return (
        f"{em} **{s.name}**（{s.code}）— {star_label}\n"
        f"    现价 {s.current_price:.3f} | 今日 {s.change_pct:+.1f}% | "
        f"RSI(6)={s.rsi6:.0f} | MA20偏{s.ma20_dev_pct:+.1f}%{ma60_str}{flow_str}\n"
        f"    {trigger_str}{caution_str}"
    )


_MODE_CONFIG = {
    "open": {
        "title_normal": "🌅 竞价后买点扫描 | {date}",
        "title_panic":  "😱 竞价跳空低开！买点来了 | {date}",
        "header_color": "wathet",
        "market_hint":  "大盘未见明显低开恐慌，以下是开盘折让品种",
        "panic_hint":   "**📌 低开恐慌即低吸信号——9:25 竞价窗口**",
        "footer": "⏰ 9:25 竞价结束 | 以昨收为基准看开盘折让，量能数据暂不可用，9:25–9:45 建仓窗口",
    },
    "early": {
        "title_normal": "🌄 早盘黄金买点 | {date}",
        "title_panic":  "😱 早盘急跌！黄金买点来了 | {date}",
        "header_color": "green",
        "market_hint":  "大盘平稳，以下是开盘15分钟内出现回调的品种",
        "panic_hint":   "**📌 早盘急跌是情绪面最强买入信号**",
        "footer": "⏰ 9:45 早盘 | 开盘15分钟信号，量能仍需观察，建议等量能确认后介入",
    },
    "tail": {
        "title_normal": "📉 尾盘择时扫描 | {date}",
        "title_panic":  "😱 尾盘恐慌！低吸机会来了 | {date}",
        "header_color": "blue",
        "market_hint":  "大盘未现明显恐慌，以下是技术面回调品种",
        "panic_hint":   "**📌 恐惧时贪婪——尾盘是最佳低吸窗口**",
        "footer": "⏰ 14:45 尾盘扫描 | 建议14:50前决策，15:00前执行 | 尾盘拉升需警惕诱多，缩量下跌止稳才是真正低点",
    },
}


def build_tail_card(reading: TailMarketReading, run_date: str,
                    rotation: Optional[object] = None,
                    scan_mode: str = "tail") -> dict:
    cfg = _MODE_CONFIG.get(scan_mode, _MODE_CONFIG["tail"])
    panic_em = PANIC_EMOJI[reading.market_panic]
    header_color = "red" if reading.market_panic else cfg["header_color"]

    title = (
        cfg["title_panic"].format(date=run_date)
        if reading.market_panic
        else cfg["title_normal"].format(date=run_date)
    )

    # 大盘情绪块
    market_block = (
        f"**{panic_em} 大盘情绪**\n"
        f"{reading.market_note}\n"
        + (cfg["panic_hint"] if reading.market_panic else cfg["market_hint"])
    )

    sections: list[dict] = [
        {"tag": "div", "text": {"tag": "lark_md", "content": market_block}},
        {"tag": "hr"},
        *([{
            "tag": "div",
            "text": {"tag": "lark_md",
                     "content": _fmt_rotation(rotation, DEFENSIVE_ROTATION_POOL)},
        }, {"tag": "hr"}] if rotation is not None and rotation.strength != "NORMAL" else []),
    ]

    # 信号分组
    strong = [s for s in reading.signals if s.stars == 3]
    good   = [s for s in reading.signals if s.stars == 2]
    watch  = [s for s in reading.signals if s.stars == 1]

    if strong:
        content = "**🟢 绝佳买点（★★★）— 重点关注**\n\n"
        content += "\n\n".join(_format_signal(s) for s in strong)
        sections += [
            {"tag": "div", "text": {"tag": "lark_md", "content": content}},
            {"tag": "hr"},
        ]

    if good:
        content = "**🔵 较好买点（★★）— 可轻仓介入**\n\n"
        content += "\n\n".join(_format_signal(s) for s in good)
        sections += [
            {"tag": "div", "text": {"tag": "lark_md", "content": content}},
            {"tag": "hr"},
        ]

    if watch:
        content = "**🟡 一般机会（★）— 观察为主**\n\n"
        content += "\n\n".join(_format_signal(s) for s in watch)
        sections += [
            {"tag": "div", "text": {"tag": "lark_md", "content": content}},
            {"tag": "hr"},
        ]

    if not (strong or good or watch):
        sections.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": "**⚪ 当前无明显买点**\n股池品种未出现符合条件的技术面低点，建议继续等待。",
            },
        })

    sections.append({
        "tag": "note",
        "elements": [{"tag": "plain_text", "content": cfg["footer"]}],
    })

    return {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": header_color,
            },
            "elements": sections,
        },
    }


def send_tail_signal(
    reading: TailMarketReading,
    run_date: Optional[str] = None,
    webhook: str = FEISHU_WEBHOOK,
    rotation: Optional[object] = None,
    scan_mode: str = "tail",
) -> bool:
    if run_date is None:
        run_date = datetime.today().strftime("%Y-%m-%d")
    card = build_tail_card(reading, run_date, rotation, scan_mode=scan_mode)
    return post_card(card, FEISHU_TAIL_WEBHOOKS)
