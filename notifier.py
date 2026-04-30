"""
飞书 Webhook 通知模块
发送富文本卡片消息，包含机制状态、操作信号、风险提示
"""

from __future__ import annotations
import json
import logging
import requests
from datetime import datetime
from typing import Optional as _Optional

from config import (FEISHU_WEBHOOK, FEISHU_WEBHOOKS, SIGNAL_BUY_STRONG, SIGNAL_BUY_WATCH,
                    SIGNAL_HOLD, SIGNAL_REDUCE, SIGNAL_SELL_STOP, SIGNAL_AVOID,
                    DEFENSIVE_ROTATION_POOL)
from rotation_advisor import format_rotation_card_md as _fmt_rotation

logger = logging.getLogger(__name__)

# 信号图标
SIGNAL_EMOJI = {
    SIGNAL_BUY_STRONG: "🟢",
    SIGNAL_BUY_WATCH:  "🔵",
    SIGNAL_HOLD:       "⚪",
    SIGNAL_REDUCE:     "🟡",
    SIGNAL_SELL_STOP:  "🔴",
    SIGNAL_AVOID:      "⛔",
}

REGIME_EMOJI = {"R1": "📈", "R2": "📊", "R3": "🔄", "R4": "⚠️"}
REGIME_LABEL = {"R1": "趋势牛市", "R2": "震荡市", "R3": "轮动市", "R4": "风险市"}
REGIME_COLOR = {"R1": "green", "R2": "blue", "R3": "yellow", "R4": "red"}

SENTIMENT_EMOJI = {
    "OVERHEATED": "🔥🔥",
    "HOT":        "🔥",
    "WARM":       "🌤",
    "NEUTRAL":    "😐",
    "COLD":       "🧊",
}
SENTIMENT_LABEL = {
    "OVERHEATED": "严重过热·禁止追高",
    "HOT":        "情绪偏热·信号降级",
    "WARM":       "偏暖·谨慎操作",
    "NEUTRAL":    "情绪中性·正常操作",
    "COLD":       "市场恐惧·逢低布局",
}


def _format_signal_line(s: dict) -> str:
    emoji = SIGNAL_EMOJI.get(s["signal"], "⚪")
    factors = s.get("factors")
    factor_str = ""
    if factors:
        parts = [f"政策{factors.get('政策','-')}"
                 f" 动量{factors.get('动量','-')}"
                 f" 资金{factors.get('资金','-')}"]
        factor_str = f"\n    └ {parts[0]}"

    weight_str = f" | 建仓{s['weight_pct']}%" if s["weight_pct"] > 0 else ""
    stop_str   = f" | 止损{s['stop_loss_pct']}%" if s["stop_loss_pct"] > 0 else ""

    return (f"{emoji} **{s['name']}**（{s['code']}）{weight_str}{stop_str}\n"
            f"    评分: {s['composite']} | {s['tier']}级 | {s['note']}"
            f"{factor_str}")


def _format_sentiment_block(sentiment: _Optional[object]) -> str:
    """生成情绪温度飞书卡片段落"""
    if sentiment is None:
        return ""
    level = getattr(sentiment, "level", "NEUTRAL")
    score = getattr(sentiment, "score", 50)
    em = SENTIMENT_EMOJI.get(level, "😐")
    label = SENTIMENT_LABEL.get(level, level)
    consec = getattr(sentiment, "consec_up_days", 0)
    ma5_dev = getattr(sentiment, "ma5_dev_pct", 0.0)
    rsi6 = getattr(sentiment, "rsi6", 50.0)
    limit_up = getattr(sentiment, "limit_up", 0)
    limit_down = getattr(sentiment, "limit_down", 0)
    note = getattr(sentiment, "note", "")
    block = getattr(sentiment, "timing_block", False)

    block_warn = "\n**⛔ 买入信号已自动降级，等待情绪回落**" if block else ""
    return (
        f"{em} **情绪温度 {score:.0f}/100 — {label}**\n"
        f"连涨{consec}日 | MA5偏{ma5_dev:+.1f}% | RSI(6)={rsi6:.0f} "
        f"| 涨停{limit_up}家/跌停{limit_down}家\n"
        f"{note}{block_warn}"
    )


def build_card_content(
    regime: str,
    regime_score: dict,
    signals: list[dict],
    risk_summary: dict,
    run_date: str,
    sentiment: _Optional[object] = None,
    rotation: _Optional[object] = None,
    data_health: _Optional[object] = None,
) -> dict:
    """构建飞书 Interactive Card 消息体"""

    regime_label = REGIME_LABEL.get(regime, regime)
    regime_em = REGIME_EMOJI.get(regime, "")
    color = REGIME_COLOR.get(regime, "blue")

    # ── 机制摘要 ──
    score_total = regime_score.get("total", "-")
    score_detail = (
        f"沪深300趋势:{regime_score.get('s1_trend','-')} | "
        f"成交额:{regime_score.get('s2_volume','-')} | "
        f"北向:{regime_score.get('s3_north','-')} | "
        f"轮动:{regime_score.get('s4_rotation','-')} | "
        f"两融:{regime_score.get('s5_margin','-')} | "
        f"波动率:{regime_score.get('s6_volatility','-')}"
    )

    # ── 风险参数 ──
    risk_line = (
        f"最大仓位 **{risk_summary['max_position_pct']}%** | "
        f"日止损线 **¥{risk_summary['daily_loss_limit']:,}** | "
        f"融资上限 **{risk_summary['max_leverage_pct']}%** | "
        f"防御仓≥ **{risk_summary['min_defensive_pct']}%** | "
        f"持仓≤ **{risk_summary['max_holding_days']}天**"
    )

    # ── 分类信号 ──
    buy_strong = [s for s in signals if s["signal"] == SIGNAL_BUY_STRONG]
    buy_watch  = [s for s in signals if s["signal"] == SIGNAL_BUY_WATCH]
    hold_sigs  = [s for s in signals if s["signal"] == SIGNAL_HOLD]
    exit_sigs  = [s for s in signals if s["signal"] in (SIGNAL_SELL_STOP, SIGNAL_REDUCE)]
    _defense_codes = set(DEFENSIVE_ROTATION_POOL.keys())
    avoid_sigs = [s for s in signals if s["signal"] == SIGNAL_AVOID
                  and s["code"] not in _defense_codes]

    sections = []

    if exit_sigs:
        lines = "\n\n".join(_format_signal_line(s) for s in exit_sigs)
        sections.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                     "content": f"**🔴 止损/减仓信号（优先执行）**\n{lines}"}
        })
        sections.append({"tag": "hr"})

    if buy_strong:
        lines = "\n\n".join(_format_signal_line(s) for s in buy_strong)
        sections.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                     "content": f"**🟢 强买入信号（三因子确认）**\n{lines}"}
        })
        sections.append({"tag": "hr"})

    if buy_watch:
        lines = "\n\n".join(_format_signal_line(s) for s in buy_watch)
        sections.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                     "content": f"**🔵 观察建仓信号（等待时机）**\n{lines}"}
        })
        sections.append({"tag": "hr"})

    if hold_sigs:
        hold_names = "、".join(f"{s['name']}({s['tier']})" for s in hold_sigs)
        sections.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                     "content": f"**⚪ 维持持仓**\n{hold_names}"}
        })
        sections.append({"tag": "hr"})

    if avoid_sigs:
        avoid_names = "、".join(f"{s['name']}" for s in avoid_sigs[:5])  # 最多显示5个
        sections.append({
            "tag": "div",
            "text": {"tag": "lark_md",
                     "content": f"**⛔ 禁止配置**：{avoid_names}"}
        })

    card = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": f"A股ETF信号 {regime_em}{regime_label} | {run_date}"
                },
                "template": color,
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"**机制评分 {score_total}/100** → {regime_em}{regime_label}\n"
                            f"{score_detail}\n\n"
                            f"**今日操作边界**\n{risk_line}"
                        ),
                    },
                },
                {"tag": "hr"},
                *([{
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": _format_sentiment_block(sentiment)},
                }, {"tag": "hr"}] if sentiment is not None else []),
                *([{
                    "tag": "div",
                    "text": {"tag": "lark_md",
                             "content": _fmt_rotation(rotation, DEFENSIVE_ROTATION_POOL)},
                }, {"tag": "hr"}] if rotation is not None and rotation.strength != "NORMAL" else []),
                *sections,
                *([{
                    "tag": "div",
                    "text": {"tag": "lark_md",
                             "content": data_health.to_summary_line()},
                }] if data_health is not None else []),
                {
                    "tag": "note",
                    "elements": [
                        {"tag": "plain_text",
                         "content": "⚡ 信号仅供参考，执行前务必完成行为护栏清单。止损线触及必须当日执行。"}
                    ],
                },
            ],
        },
    }
    return card


def send_signal(
    regime: str,
    regime_score: dict,
    signals: list[dict],
    risk_summary: dict,
    run_date: _Optional[str] = None,
    webhook: str = FEISHU_WEBHOOK,
    sentiment: _Optional[object] = None,
    rotation: _Optional[object] = None,
    data_health: _Optional[object] = None,
) -> bool:
    if run_date is None:
        run_date = datetime.today().strftime("%Y-%m-%d")

    card = build_card_content(regime, regime_score, signals, risk_summary, run_date, sentiment, rotation, data_health)

    targets = FEISHU_WEBHOOKS if webhook == FEISHU_WEBHOOK else [webhook]
    success = False
    for url in targets:
        try:
            resp = requests.post(
                url,
                headers={"Content-Type": "application/json"},
                data=json.dumps(card, ensure_ascii=False),
                timeout=10,
            )
            if resp.status_code == 200:
                result = resp.json()
                if result.get("code") == 0 or result.get("StatusCode") == 0:
                    logger.info(f"飞书推送成功: {url[-8:]}")
                    success = True
                else:
                    logger.warning(f"飞书返回异常({url[-8:]}): {result}")
            else:
                logger.warning(f"飞书 HTTP {resp.status_code}({url[-8:]}): {resp.text}")
        except Exception as e:
            logger.error(f"飞书推送失败({url[-8:]}): {e}")
    return success


def send_error_alert(message: str, webhook: str = FEISHU_WEBHOOK) -> None:
    """系统异常时发送简单文本告警"""
    payload = {
        "msg_type": "text",
        "content": {"text": f"⚠️ 信号系统异常\n{message}\n{datetime.now().strftime('%Y-%m-%d %H:%M')}"},
    }
    try:
        requests.post(webhook, json=payload, timeout=5)
    except Exception:
        pass
