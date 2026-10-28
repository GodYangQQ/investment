#!/usr/bin/env python3
"""
Serenity 供应链卡点排序
======================
基于 @aleabitoreddit 的逻辑：按 卡点强度 × 需求能见度 × 边际拐点/估值 给 AI 供应链排序。
美股 + A股一张图。

用法:
    python apps/cli/serenity_chain.py
    python apps/cli/serenity_chain.py --market US
    python apps/cli/serenity_chain.py --layer memory
    python apps/cli/serenity_chain.py --top 25
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CHAIN = os.path.join(ROOT_DIR, "data", "serenity_chain.json")
OUTPUT_DIR = os.path.join(ROOT_DIR, "output")

logging.basicConfig(level=logging.ERROR, format="%(asctime)s [%(levelname)s] %(message)s")


def load_chain() -> dict:
    with open(CHAIN, encoding="utf-8") as f:
        return json.load(f)


def _cn_quote_map(codes: list[str]) -> dict:
    try:
        from core.datasets.cn_data import get_quotes_batch
        return get_quotes_batch(codes)
    except Exception:
        return {}


def _cn_mom20(code: str):
    try:
        from core.datasets.cn_data import get_kline
        df = get_kline(code, 40)
        if df is not None and len(df) >= 21:
            c = df["close"]
            return round((c.iloc[-1] / c.iloc[-21] - 1) * 100, 1)
    except Exception:
        pass
    return None


def _us_metrics(code: str) -> dict:
    """只用K线取 价/涨跌/20日动量（轻量，避免逐只拉财务超时）。"""
    out = {"price": None, "pct": None, "pe": None, "mom20": None}
    try:
        from core.datasets.us_data import fetch_daily_kline
        df = fetch_daily_kline(code, 60)
        c = df["close"]
        out["price"] = round(float(c.iloc[-1]), 2)
        if len(c) >= 2:
            out["pct"] = round((c.iloc[-1] / c.iloc[-2] - 1) * 100, 1)
        if len(c) >= 21:
            out["mom20"] = round((c.iloc[-1] / c.iloc[-21] - 1) * 100, 1)
    except Exception:
        pass
    return out


def _mom_tilt(mom) -> float:
    if mom is None:
        return 0.0
    return max(-10.0, min(10.0, mom / 3.0))


def run(market=None, layer=None, top=30):
    data = load_chain()
    layers = data["layers"]
    if layer:
        layers = [l for l in layers if layer in l["key"] or layer in l["name"]]

    # 收集 A股代码批量取行情
    cn_codes = [c["code"] for l in layers for c in l["companies"] if c["market"] == "CN"]
    cn_q = _cn_quote_map(cn_codes) if cn_codes else {}

    rows = []
    for l in layers:
        for c in l["companies"]:
            if market and c["market"] != market:
                continue
            m = {"price": None, "pct": None, "pe": None, "mom20": None}
            if c["market"] == "US":
                m = _us_metrics(c["code"])
            else:
                q = cn_q.get(c["code"], {})
                m = {"price": q.get("price"), "pct": q.get("pct_change"),
                     "pe": q.get("pe"), "mom20": None}
            thesis = l["chokepoint"] * 12 + l["visibility"] * 10
            total = thesis + (8 if c.get("fwd") else 0) + _mom_tilt(m["mom20"])
            rows.append({
                "layer": l["name"], "chokepoint": l["chokepoint"], "visibility": l["visibility"],
                "code": c["code"], "name": c["name"], "market": c["market"],
                "fwd": "是" if c.get("fwd") else "—",
                "price": m["price"], "pct": m["pct"], "pe": m["pe"], "mom20": m["mom20"],
                "thesis": thesis, "score": round(total, 1), "note": c.get("note", ""),
            })

    rows.sort(key=lambda x: (x["score"], x["chokepoint"]), reverse=True)

    # 板块层排序
    print("\n" + "=" * 92)
    print("  AI 供应链卡点排序（Serenity 逻辑）")
    print("=" * 92)
    print(f"  {'卡点层':<22}{'卡点':>4}{'能见度':>6}{'定位'}")
    for l in sorted(layers, key=lambda x: (x["chokepoint"] + x["visibility"]), reverse=True):
        print(f"  {l['name']:<22}{l['chokepoint']:>4}{l['visibility']:>6}  {l['thesis'][:44]}")

    print("\n【个股排序（卡点2×12 + 能见度×10 + 前瞻PE压缩 + 动量）】")
    print(f"  {'代码':<10}{'名称':<16}{'市':<4}{'现价':>9}{'今日%':>7}{'20日%':>7}{'PE':>7}{'前瞻':>5}{'分':>6}  {'卡点层'}")
    print("  " + "-" * 90)
    for r in rows[:top]:
        px = f"{r['price']:.2f}" if isinstance(r["price"], (int, float)) and r["price"] else "?"
        pct = f"{r['pct']:+.1f}" if isinstance(r["pct"], (int, float)) else "?"
        mo = f"{r['mom20']:+.1f}" if isinstance(r["mom20"], (int, float)) else "?"
        pe = f"{r['pe']:.1f}" if isinstance(r["pe"], (int, float)) and r["pe"] else "—"
        print(f"  {r['code']:<10}{r['name'][:14]:<16}{r['market']:<4}{px:>9}{pct:>7}{mo:>7}{pe:>7}{r['fwd']:>5}{r['score']:>6}  {r['layer']}")

    out = os.path.join(OUTPUT_DIR, "serenity_chain.csv")
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    print(f"\n  已保存: {out}")
    print("  ⚠️ 评分=方法论复现，PE为trailing，'前瞻'为定性；非投资建议。")
    return rows


def main():
    ap = argparse.ArgumentParser(description="Serenity AI 供应链卡点排序")
    ap.add_argument("--market", choices=["US", "CN"], default=None)
    ap.add_argument("--layer", default=None, help="只看某层：memory/optics/leaders/hbm_packaging...")
    ap.add_argument("--top", type=int, default=30)
    args = ap.parse_args()
    run(market=args.market, layer=args.layer, top=args.top)


if __name__ == "__main__":
    main()
