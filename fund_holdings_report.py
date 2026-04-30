#!/usr/bin/env python3
"""
A 股公募基金持仓 Top 100 报告生成器
Usage:
    python3 signal_system/fund_holdings_report.py
    python3 signal_system/fund_holdings_report.py --date 20260331
    python3 signal_system/fund_holdings_report.py --fast      # 跳过增持/减持统计
"""

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import requests
import pandas as pd

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# ─────────────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────────────
AGGREGATE_URL = "http://data.eastmoney.com/dataapi/zlsj/list"
DETAIL_URL = "https://datacenter-web.eastmoney.com/api/data/v1/get"
TOP_N = 100
_FETCH_PAGE_SIZE = 100  # API 单页上限
REQUEST_TIMEOUT = 20
MAX_WORKERS = 8
PAGE_SIZE = 500

FONT_CANDIDATES = [
    ("/System/Library/Fonts/STHeiti Light.ttc", 0, "STHeiti"),
    ("/System/Library/Fonts/PingFang.ttc", 0, "PingFang"),
    ("/Library/Fonts/Arial Unicode MS.ttf", None, "ArialUnicode"),
]


# ─────────────────────────────────────────────────────────────
# 日期工具
# ─────────────────────────────────────────────────────────────
def _parse_date(date_str: str) -> str:
    """把 YYYYMMDD 或 YYYY-MM-DD 统一成 YYYY-MM-DD。"""
    d = date_str.replace("-", "")
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


def prev_quarter_end(date_str: str) -> str:
    """返回上一季度末，格式 YYYY-MM-DD。"""
    d = _parse_date(date_str)
    year, month = int(d[:4]), int(d[5:7])
    quarter_ends = [(3, 31), (6, 30), (9, 30), (12, 31)]
    for i, (m, _) in enumerate(quarter_ends):
        if month == m:
            if i == 0:
                return f"{year - 1}-12-31"
            pm, pd_ = quarter_ends[i - 1]
            return f"{year}-{pm:02d}-{pd_}"
    raise ValueError(f"日期 {date_str} 不是季度末")


# ─────────────────────────────────────────────────────────────
# 聚合数据抓取（zlsj/list）
# ─────────────────────────────────────────────────────────────
def _fetch_aggregate_page(date_str: str, page: int, size: int = 100) -> dict:
    params = {
        "date": date_str,
        "type": "1",
        "zjc": "0",
        "sortField": "HOULD_NUM",
        "sortDirec": "1",
        "pageNum": str(page),
        "pageSize": str(size),
        "p": str(page),
        "pageNo": str(page),
    }
    r = requests.get(AGGREGATE_URL, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json()


def fetch_top_stocks(date_str: str, top_n: int = TOP_N) -> pd.DataFrame:
    """
    从东方财富获取基金持仓聚合排行，取前 top_n 条。
    返回字段: code, name, fund_count, ownership_pct, market_value_b
    """
    date_str = _parse_date(date_str)
    print(f"  [聚合] 获取 {date_str} 基金持仓数据（Top {top_n}）…")
    rows = []
    page = 1
    while len(rows) < top_n:
        d = _fetch_aggregate_page(date_str, page=page, size=_FETCH_PAGE_SIZE)
        batch = d.get("data") or []
        if not batch:
            break
        rows.extend(batch)
        if page >= (d.get("pages") or 1):
            break
        page += 1

    records = []
    for item in rows[:top_n]:
        value_yuan = item.get("HOLD_VALUE") or 0
        records.append(
            {
                "code": str(item.get("SECURITY_CODE", "")).zfill(6),
                "name": item.get("SECURITY_NAME_ABBR", ""),
                "fund_count": int(item.get("HOULD_NUM") or 0),
                "ownership_pct": float(item.get("TOTALSHARES_RATIO") or 0),
                "market_value_b": round(value_yuan / 1e8, 2),
            }
        )
    print(f"  [聚合] 共 {len(records)} 条记录")
    return pd.DataFrame(records)


def fetch_prev_fund_counts(codes: list, prev_date: str) -> dict:
    """
    批量获取上期各股持有基金数，返回 {code: fund_count}。
    扫描上期排行前 max(len(codes)*2, 500) 条以覆盖名次靠后的标的。
    """
    prev_date = _parse_date(prev_date)
    scan_limit = max(len(codes) * 2, 500)
    print(f"  [上期] 获取 {prev_date} 基金持仓数据（扫描前 {scan_limit} 条）…")
    codes_set = set(codes)
    result = {}
    page = 1
    scanned = 0
    while scanned < scan_limit:
        d = _fetch_aggregate_page(prev_date, page=page, size=_FETCH_PAGE_SIZE)
        batch = d.get("data") or []
        if not batch:
            break
        for item in batch:
            code = str(item.get("SECURITY_CODE", "")).zfill(6)
            if code in codes_set:
                result[code] = int(item.get("HOULD_NUM") or 0)
        scanned += len(batch)
        if page >= (d.get("pages") or 1):
            break
        page += 1
    print(f"  [上期] 匹配到 {len(result)}/{len(codes)} 只股票")
    return result


# ─────────────────────────────────────────────────────────────
# 每股基金明细（增持/减持统计）
# ─────────────────────────────────────────────────────────────
def _fetch_holders_page(code: str, date_str: str, page: int) -> tuple[list, int]:
    """
    返回 (rows_list, total_pages)。
    rows_list 每条: {"holder_code": str, "shares": int}
    """
    params = {
        "sortColumns": "TOTAL_SHARES",
        "sortTypes": "-1",
        "pageSize": str(PAGE_SIZE),
        "pageNumber": str(page),
        "reportName": "RPT_MAINDATA_MAIN_POSITIONDETAILS",
        "columns": "HOLDER_CODE,TOTAL_SHARES",
        "filter": f'(SECURITY_CODE="{code}") (REPORT_DATE=\'{date_str}\')(ORG_TYPE="基金")',
        "source": "WEB",
        "client": "WEB",
    }
    r = requests.get(DETAIL_URL, params=params, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    d = r.json()
    result = d.get("result") or {}
    data = result.get("data") or []
    pages = result.get("pages") or 1
    rows = [
        {
            "holder_code": row.get("HOLDER_CODE", ""),
            "shares": int(row.get("TOTAL_SHARES") or 0),
        }
        for row in data
    ]
    return rows, pages


def _fetch_all_holders(code: str, date_str: str) -> dict:
    """获取某股在 date_str 的全部基金持仓人 {holder_code: shares}。"""
    all_rows, total_pages = _fetch_holders_page(code, date_str, 1)
    for p in range(2, total_pages + 1):
        rows, _ = _fetch_holders_page(code, date_str, p)
        all_rows.extend(rows)
        time.sleep(0.05)
    return {r["holder_code"]: r["shares"] for r in all_rows}


def _compute_changes(curr: dict, prev: dict) -> tuple[int, int]:
    """
    返回 (增持基金数, 减持基金数)。
    增持 = 持股量上升（含新进）；减持 = 持股量下降（含退出）。
    """
    all_codes = set(curr) | set(prev)
    increased = sum(
        1
        for c in all_codes
        if (curr.get(c) or 0) > (prev.get(c) or 0)
    )
    decreased = sum(
        1
        for c in all_codes
        if (curr.get(c) or 0) < (prev.get(c) or 0)
    )
    return increased, decreased


def fetch_change_counts(
    codes: list, curr_date: str, prev_date: str
) -> dict:
    """
    并发获取每只股票的增持/减持基金数。
    返回 {code: (增持数, 减持数)}
    """
    curr_date = _parse_date(curr_date)
    prev_date = _parse_date(prev_date)
    print(f"  [增减] 并发获取 {len(codes)} 只股票的基金明细（约需 1-3 分钟）…")

    def _task(code):
        try:
            curr = _fetch_all_holders(code, curr_date)
            time.sleep(0.1)
            prev = _fetch_all_holders(code, prev_date)
            return code, _compute_changes(curr, prev)
        except Exception as e:
            print(f"    WARN {code}: {e}")
            return code, (0, 0)

    results = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_task, c): c for c in codes}
        done = 0
        for future in as_completed(futures):
            code, counts = future.result()
            results[code] = counts
            done += 1
            if done % 10 == 0:
                print(f"    进度: {done}/{len(codes)}")
    return results


# ─────────────────────────────────────────────────────────────
# PDF 生成
# ─────────────────────────────────────────────────────────────
def _register_font() -> str:
    """注册中文字体，返回字体名。"""
    for path, idx, name in FONT_CANDIDATES:
        if not os.path.exists(path):
            continue
        try:
            if idx is not None:
                pdfmetrics.registerFont(TTFont(name, path, subfontIndex=idx))
            else:
                pdfmetrics.registerFont(TTFont(name, path))
            return name
        except Exception:
            continue
    raise RuntimeError("未找到可用中文字体，请检查系统字体目录。")


def generate_pdf(df: pd.DataFrame, output_path: Path, curr_date: str, prev_date: str, top_n: int = TOP_N) -> None:
    """生成 A4 横向 PDF 报告。"""
    font_name = _register_font()
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=landscape(A4),
        leftMargin=12 * mm,
        rightMargin=12 * mm,
        topMargin=12 * mm,
        bottomMargin=12 * mm,
    )

    title_style = ParagraphStyle(
        "title",
        fontName=font_name,
        fontSize=14,
        leading=20,
        alignment=1,  # center
    )
    footer_style = ParagraphStyle(
        "footer",
        fontName=font_name,
        fontSize=7,
        leading=10,
        textColor=colors.grey,
    )

    # 表头
    headers = [
        "排序",
        "股票名称",
        "代码",
        "上期\n基金数",
        "持有\n基金数",
        "基金持有\n股本占比(%)",
        "增持股票\n的基金数",
        "减持股票\n的基金数",
        "市值\n(亿元)",
    ]
    col_widths = [15, 50, 28, 30, 30, 35, 25, 25, 35]  # mm, total = 273mm = A4 横可用宽度
    col_widths_pt = [w * mm for w in col_widths]

    # 数据行
    rows = [headers]
    for _, row in df.iterrows():
        inc = row.get("increased", "-")
        dec = row.get("decreased", "-")
        rows.append(
            [
                str(int(row["rank"])),
                str(row["name"]),
                str(row["code"]),
                str(int(row.get("prev_fund_count", 0))),
                str(int(row["fund_count"])),
                f"{row['ownership_pct']:.2f}",
                str(int(inc)) if inc != "-" else "-",
                str(int(dec)) if dec != "-" else "-",
                f"{row['market_value_b']:.2f}",
            ]
        )

    table = Table(rows, colWidths=col_widths_pt, repeatRows=1)

    # 行颜色交替
    style_cmds = [
        ("FONTNAME", (0, 0), (-1, -1), font_name),
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("FONTSIZE", (0, 1), (-1, -1), 8),
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2C3E50")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#CCCCCC")),
        ("ROWHEIGHT", (0, 0), (-1, 0), 26),
        ("ROWHEIGHT", (0, 1), (-1, -1), 16),
        ("TOPPADDING", (0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]
    for i in range(1, len(rows)):
        bg = colors.HexColor("#F5F7FA") if i % 2 == 0 else colors.white
        style_cmds.append(("BACKGROUND", (0, i), (-1, i), bg))
    table.setStyle(TableStyle(style_cmds))

    note = "" if "increased" in df.columns and df["increased"].iloc[0] != "-" else "（增持/减持数据需使用完整模式，可加 --full 参数重新运行）"
    title_text = (
        f"A股公募基金持仓 Top {top_n}  ·  "
        f"报告期: {curr_date}  ·  对比期: {prev_date}"
    )
    footer_text = (
        f"数据来源: 东方财富网 / AkShare  ·  "
        f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}  {note}"
    )

    content = [
        Paragraph(title_text, title_style),
        Spacer(1, 4 * mm),
        table,
        Spacer(1, 3 * mm),
        Paragraph(footer_text, footer_style),
    ]
    doc.build(content)


# ─────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description="生成 A 股公募基金持仓 PDF 报告")
    parser.add_argument(
        "--date",
        default="20260331",
        help="报告期（季度末），格式 YYYYMMDD，默认 20260331",
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="完整模式：额外获取增持/减持基金数（较慢）",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=TOP_N,
        help=f"持仓排行取前 N 只，默认 {TOP_N}",
    )
    args = parser.parse_args()

    top_n = args.top
    curr_date = _parse_date(args.date)
    prev_date = prev_quarter_end(curr_date)
    print(f"\n=== 公募基金持仓报告 ===")
    print(f"报告期: {curr_date}  |  对比期: {prev_date}  |  Top {top_n}")
    print(f"模式: {'完整' if args.full else '快速（跳过增减持统计）'}\n")

    # Step 1: 当期 Top N
    curr_df = fetch_top_stocks(curr_date, top_n)
    if curr_df.empty:
        print("ERROR: 未获取到当期数据，请确认日期是否有效（季度末 + 披露后）。")
        sys.exit(1)

    # Step 2: 上期持有基金数
    codes = curr_df["code"].tolist()
    prev_counts = fetch_prev_fund_counts(codes, prev_date)

    curr_df["prev_fund_count"] = curr_df["code"].map(lambda c: prev_counts.get(c, 0))
    curr_df["rank"] = range(1, len(curr_df) + 1)

    # Step 3: 增持/减持（可选）
    if args.full:
        changes = fetch_change_counts(codes, curr_date, prev_date)
        curr_df["increased"] = curr_df["code"].map(lambda c: changes.get(c, (0, 0))[0])
        curr_df["decreased"] = curr_df["code"].map(lambda c: changes.get(c, (0, 0))[1])
    else:
        curr_df["increased"] = "-"
        curr_df["decreased"] = "-"

    # Step 4: 输出 PDF
    reports_dir = Path(__file__).parent / "reports"
    reports_dir.mkdir(exist_ok=True)
    date_tag = curr_date.replace("-", "")
    suffix = f"_top{top_n}" if top_n != TOP_N else ""
    output_path = reports_dir / f"fund_holdings_{date_tag}{suffix}.pdf"

    print(f"\n  [PDF] 生成报告：{output_path}")
    generate_pdf(curr_df, output_path, curr_date, prev_date, top_n)
    print(f"\n完成！文件已保存：{output_path}")
    print(f"运行以下命令打开：  open \"{output_path}\"")


if __name__ == "__main__":
    main()
