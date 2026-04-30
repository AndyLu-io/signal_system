"""
数据健康检查 — 评估每次运行的数据质量，输出置信度标签
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
from typing import Optional
import pandas as pd


@dataclass
class DataHealth:
    etf_ok: int
    etf_total: int
    index_fresh: bool
    north_precision: str   # "exact" | "proxy" | "unavailable"
    main_force_ok: bool
    fallback_count: int = 0

    @property
    def etf_ratio(self) -> float:
        return self.etf_ok / max(self.etf_total, 1)

    @property
    def confidence(self) -> str:
        """整体置信度: HIGH / MEDIUM / LOW"""
        if (self.etf_ratio >= 0.9 and self.index_fresh
                and self.north_precision == "exact"
                and self.main_force_ok):
            return "HIGH"
        if self.etf_ratio >= 0.7:
            return "MEDIUM"
        return "LOW"

    @property
    def confidence_icon(self) -> str:
        return {"HIGH": "✅", "MEDIUM": "⚠️", "LOW": "❌"}.get(self.confidence, "❓")

    def to_summary_line(self) -> str:
        north_label = {
            "exact":       "精确净买入",
            "proxy":       "成交额代理",
            "unavailable": "不可用",
        }.get(self.north_precision, self.north_precision)
        return (
            f"{self.confidence_icon} 数据置信度 **{self.confidence}**  "
            f"ETF {self.etf_ok}/{self.etf_total} | "
            f"指数{'最新' if self.index_fresh else '可能滞后'} | "
            f"北向{north_label} | "
            f"主力资金{'✓' if self.main_force_ok else '✗'}"
        )

    def to_dict(self) -> dict:
        return {
            "confidence":      self.confidence,
            "etf_ok":          self.etf_ok,
            "etf_total":       self.etf_total,
            "index_fresh":     self.index_fresh,
            "north_precision": self.north_precision,
            "main_force_ok":   self.main_force_ok,
            "fallback_count":  self.fallback_count,
        }


def check_data_health(
    etf_prices: dict,
    etf_codes: list,
    csi300: Optional[pd.DataFrame],
    north_flow: Optional[pd.DataFrame],
    main_force: dict,
) -> DataHealth:
    etf_ok = sum(1 for c in etf_codes if etf_prices.get(c) is not None)

    index_fresh = False
    if csi300 is not None and len(csi300) > 0:
        try:
            last = csi300.index[-1]
            last_date = last.date() if hasattr(last, "date") else date.fromisoformat(str(last)[:10])
            index_fresh = (date.today() - last_date).days <= 3
        except Exception:
            pass

    if north_flow is None:
        north_precision = "unavailable"
    else:
        try:
            last_net = north_flow["net_buy_billion"].iloc[-1]
            north_precision = "exact" if pd.notna(last_net) else "proxy"
        except Exception:
            north_precision = "proxy"

    main_force_ok = bool(main_force) and any(v is not None for v in main_force.values())

    return DataHealth(
        etf_ok=etf_ok,
        etf_total=len(etf_codes),
        index_fresh=index_fresh,
        north_precision=north_precision,
        main_force_ok=main_force_ok,
    )
