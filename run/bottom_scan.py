#!/usr/bin/env python3
"""
主力抄底信号扫描器
==================
不看单日净流入（那没预测力），而是按【位置 + 量能 + 资金结构改善】综合打分，
找"可能在被吸筹/筑底"的个股。

用法:
    python run/bottom_scan.py --board BK1329          # 扫描某板块成分
    python run/bottom_scan.py --codes 600812,300308
    python run/bottom_scan.py --board BK1329 --limit 40 --top 15
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "core"))

import numpy as np
import pandas as pd

import cn_data as cd
from cn_data import _em_get

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")


def get_kline(code: str, days: int = 300):
    try:
        import akshare as ak
        end = datetime.now().strftime("%Y%m%d")
        start = (datetime.now() - timedelta(days=int(days * 1.6) + 60)).strftime("%Y%m%d")
        for _ in range(3):
            try:
                df = ak.stock_zh_a_hist(symbol=code, period="daily",
                                        start_date=start, end_date=end, adjust="qfq")
                if df is not None and not df.empty:
                    return df
            except Exception:
                pass
    except Exception:
        pass
    return None


def get_fflow(code: str, days: int = 60):
    secid = ("1." if str(code).startswith("6") else "0.") + str(code)
    url = ("https://push2his.eastmoney.com/api/qt/stock/fflow/daykline/get?lmt=%d&klt=101"
           "&secid=%s&fields1=f1,f2,f3,f7&fields2=f51,f52,f57,f63" % (days, secid))
    kl = ((_em_get(url) or {}).get("data", {}) or {}).get("klines") or []
    rows = []
    for k in kl:
        p = str(k).split(",")
        try:
            rows.append({"date": p[0], "main": float(p[1]), "ratio": float(p[2]), "pct": float(p[3])})
        except (IndexError, ValueError):
            continue
    return pd.DataFrame(rows)


def analyze(code: str, name: str = "") -> dict | None:
    k = get_kline(code)
    if k is None or len(k) < 60:
        return None
    close = pd.to_numeric(k["收盘"], errors="coerce").values
    vol = pd.to_numeric(k["成交量"], errors="coerce").values
    pct = pd.to_numeric(k["涨跌幅"], errors="coerce").values if "涨跌幅" in k else np.diff(close, prepend=close[0]) / close
    c = close[-1]
    hi250 = np.nanmax(close[-250:]) if len(close) >= 30 else np.nanmax(close)
    lo20 = np.nanmin(close[-20:]); lo60 = np.nanmin(close[-60:])
    ma20 = np.nanmean(close[-20:]); ma60 = np.nanmean(close[-60:])
    v5 = np.nanmean(vol[-5:]); v60 = np.nanmean(vol[-60:]) or 1
    dist_high = c / hi250 - 1
    off_low = c / lo20 - 1 if lo20 else 0
    vol_ratio = v5 / v60

    ff = get_fflow(code)
    ratio5 = ratio_prev5 = None
    if ff is not None and len(ff) >= 10:
        r = ff["ratio"].values
        ratio5 = float(np.nanmean(r[-5:]))
        ratio_prev5 = float(np.nanmean(r[-10:-5]))

    # —— 打分 ——
    score = 0.0
    notes = []
    # 1) 回撤深度
    if dist_high <= -0.6:
        score += 15; notes.append(f"深跌{dist_high*100:.0f}%")
    elif dist_high <= -0.4:
        score += 25; notes.append(f"深跌{dist_high*100:.0f}%")
    elif dist_high <= -0.2:
        score += 18; notes.append(f"回调{dist_high*100:.0f}%")
    else:
        score += 5
    # 2) 企稳（不再创新低）
    if off_low > 0.05:
        score += 15; notes.append("已离20日低")
    elif off_low > 0:
        score += 8
    else:
        notes.append("仍创新低")
    # 3) 站上均线
    if c > ma20:
        score += 10; notes.append("站上MA20")
    if c > ma60:
        score += 5
    # 4) 缩量
    if vol_ratio < 0.7:
        score += 15; notes.append("地量")
    elif vol_ratio < 1.0:
        score += 8
    # 5) 主力净占比改善
    if ratio5 is not None and ratio_prev5 is not None:
        if ratio5 - ratio_prev5 > 3:
            score += 15; notes.append("主力流出收窄/转正")
        elif ratio5 - ratio_prev5 > 0:
            score += 8
        if ratio5 > 0:
            score += 5; notes.append("近5日主力净流入")
    # 6) 底部放量吸筹（近20日有放量但未暴涨）
    seg = vol[-20:] / v60
    hot_days = ((seg > 1.5) & (np.abs(pct[-20:]) < 3)).sum()
    if hot_days >= 2:
        score += 10; notes.append(f"底部放量吸筹x{hot_days}")

    return {"代码": code, "名称": name, "现价": round(c, 2),
            "距高%": round(dist_high * 100, 1), "离20低%": round(off_low * 100, 1),
            "量能比": round(vol_ratio, 2),
            "近5日主力%": None if ratio5 is None else round(ratio5, 2),
            "主力改善": None if (ratio5 is None or ratio_prev5 is None) else round(ratio5 - ratio_prev5, 2),
            "抄底分": round(score, 0), "信号": " ".join(notes)}


def universe(args):
    if args.codes:
        return [(x.strip(), "") for x in args.codes.split(",") if x.strip()]
    if args.board:
        return [(s["code"], s.get("name", "")) for s in cd.get_board_constituents(args.board, args.limit)]
    return []


def main():
    ap = argparse.ArgumentParser(description="主力抄底信号扫描")
    ap.add_argument("--board", default=None, help="板块代码，如 BK1329")
    ap.add_argument("--codes", default=None, help="逗号分隔的股票代码")
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--top", type=int, default=15)
    args = ap.parse_args()

    pool = universe(args)
    if not pool:
        print("请指定 --board BKxxxx 或 --codes。")
        return
    print(f"扫描 {len(pool)} 只...")
    rows = []
    for code, name in pool:
        try:
            r = analyze(code, name)
            if r:
                rows.append(r)
        except Exception as e:
            logging.warning("%s %s", code, str(e)[:50])
    if not rows:
        print("无有效结果"); return
    df = pd.DataFrame(rows).sort_values("抄底分", ascending=False)

    print("\n" + "=" * 96)
    print(f"  抄底信号扫描  |  {datetime.now().strftime('%Y-%m-%d %H:%M')}  |  {len(df)} 只")
    print("=" * 96)
    print(f"  {'代码':<8}{'名称':<10}{'现价':>8}{'距高%':>7}{'离20低%':>8}{'量能比':>7}{'近5日主力%':>10}{'改善':>7}{'抄底分':>7}")
    print("  " + "-" * 90)
    for _, r in df.head(args.top).iterrows():
        v = "?" if r["近5日主力%"] is None else f"{r['近5日主力%']:.2f}"
        im = "?" if r["主力改善"] is None else f"{r['主力改善']:+.2f}"
        print(f"  {r['代码']:<8}{(r['名称'] or '')[:8]:<10}{r['现价']:>8}{r['距高%']:>7.1f}{r['离20低%']:>8.1f}"
              f"{r['量能比']:>7.2f}{v:>10}{im:>7}{int(r['抄底分']):>7}")
    print("\n  信号拆解（Top5）:")
    for _, r in df.head(5).iterrows():
        print(f"    {r['代码']} {r['名称']}: {r['信号']}")
    print("\n  ⚠️ 抄底是概率不是确定；深跌+缩量+主力改善≠必涨，务必设止损。")


if __name__ == "__main__":
    main()
