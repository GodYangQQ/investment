#!/usr/bin/env python3
"""
A股回购 / 股东增持查询
======================
数据源：东方财富（akshare stock_repurchase_em），回购是强制披露信息。

用法:
    python run/repurchase.py                    # 全市场回购排行（按已回购金额）
    python run/repurchase.py --stock 600812     # 某只股票的回购
    python run/repurchase.py --board BK1329     # 某板块成分股的回购
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core"))

import pandas as pd

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(ROOT_DIR, "data", "repurchase_cache.csv")


def _yi(v):
    try:
        return f"{float(v)/1e8:.2f}亿"
    except (TypeError, ValueError):
        return "—"


def load(force: bool = False) -> pd.DataFrame:
    today = date.today().isoformat()
    if not force and os.path.exists(CACHE):
        try:
            df = pd.read_csv(CACHE, dtype=str)
            if df.attrs.get("d") == today or (len(df) and "股票代码" in df.columns):
                # 简单同日判断：文件修改日期
                mday = date.fromtimestamp(os.path.getmtime(CACHE)).isoformat()
                if mday == today:
                    return df
        except Exception:
            pass
    import akshare as ak
    df = ak.stock_repurchase_em()
    if df is None:
        df = pd.DataFrame()
    try:
        os.makedirs(os.path.dirname(CACHE), exist_ok=True)
        df.to_csv(CACHE, index=False, encoding="utf-8-sig")
    except Exception:
        pass
    return df


def _prep(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["股票代码"] = df["股票代码"].astype(str).str.zfill(6)
    for c in ["计划回购金额区间-上限", "计划回购金额区间-下限", "已回购金额", "占公告前一日总股本比例-上限"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


def render(df: pd.DataFrame, title: str, top: int = 30):
    if df is None or df.empty:
        print(f"\n{title}：无回购记录")
        return
    df = df.sort_values("已回购金额", ascending=False, na_position="last") if "已回购金额" in df.columns else df
    print(f"\n{'='*104}\n  {title}  （共 {len(df)} 条）\n{'='*104}")
    print(f"  {'代码':<8}{'简称':<10}{'现价':>8}{'计划价上限':>10}{'计划金额上限':>12}{'已回购金额':>12}{'已回购%':>9}{'进度':<10}{'公告日'}")
    print("  " + "-" * 100)
    for _, r in df.head(top).iterrows():
        print(f"  {r.get('股票代码',''):<8}{str(r.get('股票简称',''))[:8]:<10}"
              f"{str(r.get('最新价',''))[:6]:>8}{str(r.get('计划回购价格区间',''))[:6]:>10}"
              f"{_yi(r.get('计划回购金额区间-上限')):>12}{_yi(r.get('已回购金额')):>12}"
              f"{('%.2f' % r.get('占公告前一日总股本比例-上限')) if pd.notna(r.get('占公告前一日总股本比例-上限')) else '—':>9}"
              f"{str(r.get('实施进度',''))[:8]:<10}{str(r.get('最新公告日期',''))[:10]}")


def main():
    ap = argparse.ArgumentParser(description="A股回购查询")
    ap.add_argument("--stock", default=None, help="股票代码")
    ap.add_argument("--board", default=None, help="板块代码，如 BK1329")
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args()

    try:
        df = _prep(load(args.refresh))
    except Exception as e:
        print("取数失败（网络/数据源）：", str(e)[:100]); return
    if df.empty:
        print("无数据"); return

    if args.stock:
        sub = df[df["股票代码"] == str(args.stock).zfill(6)]
        render(sub, f"{args.stock} 回购情况", args.top)
    elif args.board:
        import cn_data as cd
        cons = {s["code"] for s in cd.get_board_constituents(args.board, 200)}
        sub = df[df["股票代码"].isin(cons)]
        render(sub, f"板块 {args.board} 成分股回购情况", args.top)
    else:
        render(df, "全市场回购排行（按已回购金额）", args.top)
    print("\n  说明：回购是强制披露；'已回购金额/进度'反映真实执行，比'计划'更可靠。数据来自东财，非投资建议。")


if __name__ == "__main__":
    main()
