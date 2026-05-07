"""
通用工具：重试、原子写、交易日判断、Webhook 配置加载。

抽出原本散落在 data_fetcher / daily_guidance / stock_timing / regime_engine
里的相同工具函数；同时把所有 Feishu Webhook 的来源统一收口。
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import date, datetime
from pathlib import Path
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ─── 重试 ──────────────────────────────────────────────────────────────────────

def retry(
    fn: Callable[[], T],
    attempts: int = 3,
    delays: tuple[float, ...] = (2.0, 5.0),
) -> T:
    """
    最多 attempts 次，失败后按 delays 等待，最终仍失败抛出最后一个异常。
    """
    last_exc: Exception = RuntimeError("unknown")
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last_exc = e
            if i < len(delays):
                logger.warning(f"第{i+1}次失败({e})，{delays[i]}s后重试...")
                time.sleep(delays[i])
    raise last_exc


# ─── 原子写 JSON ──────────────────────────────────────────────────────────────

def atomic_write_json(path: Path, data: object, *, indent: int = 2) -> None:
    """
    通过 tmp 文件 + rename 实现原子写，避免 crash 留下半截 JSON。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=indent),
        encoding="utf-8",
    )
    tmp.replace(path)


# ─── 交易日判断（统一节假日来源） ──────────────────────────────────────────────

def is_trading_day(today: str | date | None = None) -> bool:
    """
    判断是否交易日（周一-周五 + 不在 HOLIDAY_BLACKLIST 中）。

    today: ISO 字符串 'YYYY-MM-DD' / date / None（默认今天）
    """
    # 延迟导入避免循环依赖
    from config import HOLIDAY_BLACKLIST

    if today is None:
        today = date.today()
    if isinstance(today, str):
        today = datetime.strptime(today, "%Y-%m-%d").date()

    if today.weekday() >= 5:
        return False
    return today.isoformat() not in HOLIDAY_BLACKLIST


# ─── Webhook 配置加载（环境变量优先，硬编码默认作为 fallback） ─────────────────

def webhook_from_env(env_key: str, default: str | None = None) -> str | None:
    """
    优先从环境变量读取 Webhook，未配置时退回 default（兼容旧硬编码）。
    """
    val = os.environ.get(env_key)
    if val:
        return val
    if default:
        return default
    return None


def webhooks_from_env(env_key: str, defaults: list[str] | None = None) -> list[str]:
    """
    多 Webhook 列表：环境变量 `<env_key>` 用逗号分隔；
    未配置时退回 defaults。
    """
    raw = os.environ.get(env_key)
    if raw:
        return [u.strip() for u in raw.split(",") if u.strip()]
    return list(defaults or [])
