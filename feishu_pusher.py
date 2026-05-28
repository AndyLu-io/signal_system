"""
飞书推送统一接口。

合并 notifier / stock_timing / daily_guidance / tail_notifier 各自实现的
"循环 webhook + 限流处理 + 错误日志"逻辑。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Iterable

import requests

logger = logging.getLogger(__name__)

# 飞书限流 code
_RATE_LIMIT_CODE = 11232


def post_card(
    card: dict,
    webhooks: Iterable[str],
    *,
    timeout: int = 15,
    max_retries: int = 5,
) -> bool:
    """
    向多个 Webhook 发送同一张卡片，任一成功即返回 True。

    重试策略（指数退避）：
      - 飞书限流（code=11232 或 HTTP 429）
      - 网络超时 / 连接异常
      - HTTP 5xx 服务端错误
    退避间隔: 3s → 6s → 12s → 24s → 48s
    """
    payload = json.dumps(card, ensure_ascii=False)
    headers = {"Content-Type": "application/json"}
    any_success = False

    for url in webhooks:
        if not url:
            continue
        for attempt in range(max_retries):
            wait = 3 * (2 ** attempt)  # 3, 6, 12, 24, 48
            try:
                resp = requests.post(url, headers=headers, data=payload, timeout=timeout)

                # HTTP 429 或 5xx → 重试
                if resp.status_code == 429 or resp.status_code >= 500:
                    if attempt < max_retries - 1:
                        logger.warning(
                            f"飞书 HTTP {resp.status_code}({url[-8:]}), "
                            f"{wait}s 后重试 ({attempt+1}/{max_retries})"
                        )
                        time.sleep(wait)
                        continue
                    logger.error(f"飞书 HTTP {resp.status_code}({url[-8:]}) 重试耗尽")
                    break

                if resp.status_code != 200:
                    logger.warning(
                        f"飞书 HTTP {resp.status_code}({url[-8:]}): {resp.text[:200]}"
                    )
                    break

                result = resp.json()
                code = result.get("code", result.get("StatusCode", -1))
                if code == 0:
                    logger.info(f"飞书推送成功: {url[-8:]}")
                    any_success = True
                    break

                # 飞书业务限流 → 重试
                if code == _RATE_LIMIT_CODE and attempt < max_retries - 1:
                    logger.warning(
                        f"飞书限流({url[-8:]}), {wait}s 后重试 ({attempt+1}/{max_retries})"
                    )
                    time.sleep(wait)
                    continue

                logger.warning(f"飞书返回({url[-8:]}): {result}")
                break

            except (requests.Timeout, requests.ConnectionError) as e:
                if attempt < max_retries - 1:
                    logger.warning(
                        f"飞书网络异常({url[-8:]}): {e}, "
                        f"{wait}s 后重试 ({attempt+1}/{max_retries})"
                    )
                    time.sleep(wait)
                    continue
                logger.error(f"飞书网络异常({url[-8:]}) 重试耗尽: {e}")
                break
            except Exception as e:  # noqa: BLE001
                logger.error(f"飞书推送未知异常({url[-8:]}): {e}")
                break

    return any_success


def post_text(
    text: str,
    webhooks: Iterable[str],
    *,
    timeout: int = 5,
) -> bool:
    """简单文本推送（用于异常告警等场景）。"""
    payload = {"msg_type": "text", "content": {"text": text}}
    any_success = False
    for url in webhooks:
        if not url:
            continue
        try:
            resp = requests.post(url, json=payload, timeout=timeout)
            if resp.status_code == 200 and resp.json().get("code", -1) == 0:
                any_success = True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"飞书文本推送失败({url[-8:]}): {e}")
    return any_success
