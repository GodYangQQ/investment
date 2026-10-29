#!/usr/bin/env python3
"""一次性预取板块/指数历史 K 线到本地 SQLite。

之后前端 `/api/bars` 会优先读本地库；缓存过期时只增量拉最近 N 根
（日线 5 根、分钟 30 根），不会每次全量重拉。

用法:
    python scripts/prefetch_sector_history.py --market cn --kind industry
    python scripts/prefetch_sector_history.py --market cn --kind concept
    python scripts/prefetch_sector_history.py --market cn --kind index
    python scripts/prefetch_sector_history.py --market us
    python scripts/prefetch_sector_history.py --all
    python scripts/prefetch_sector_history.py --all --update   # 只补最新 5 根

常用参数:
    --limit 20      只处理前 20 个（调试）
    --workers 4     并发数
    --sleep 0.2     每个任务后暂停，避免被限流
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "apps" / "web"))

import sector_monitor_web as sm  # noqa: E402


def targets(market: str, kind: str) -> Iterator[tuple[str, str, str, str]]:
    if market == "us":
        rows = []
        try:
            rows, _ = sm.fetch_us_sectors()
        except Exception:
            rows = []
        if not rows:
            rows = [{"code": c, "name": n, "group": g[0]} for g in sm.US_GROUPS for c, n in g[2]]
        for r in rows:
            yield "us", "industry", str(r["code"]), r.get("name", "")
        return
    if kind == "index":
        for r in sm.fetch_cn_indices():
            yield "cn", "index", str(r["code"]), r.get("name", "")
        return
    rows, _src = sm.fetch_cn_sectors(kind)
    for r in rows:
        yield "cn", kind, str(r["code"]), r.get("name", "")


def stock_targets():
    """项目股票池：my_pool.csv + 自选 + 美股热门/别名。"""
    import csv
    import json
    import sqlite3

    # my_pool.csv（美股）
    try:
        path = ROOT / "data" / "my_pool.csv"
        for r in csv.DictReader(open(path, encoding="utf-8-sig")):
            code = str(r.get("代码") or "").strip()
            if code:
                yield "us", "stock", code, r.get("名称") or code
    except Exception:
        pass

    # 自选（sector_monitor.db watch 表）
    try:
        c = sqlite3.connect(ROOT / "data" / "sector_monitor.db")
        row = c.execute("select payload from watch where id='default'").fetchone()
        for it in json.loads(row[0]) if row else []:
            code = str(it.get("code") or "").strip()
            if not code:
                continue
            if it.get("market") == "us":
                yield "us", "stock", code, it.get("name") or code
            else:
                yield "cn", "stock", sm._cn_code_norm(code), it.get("name") or code
    except Exception:
        pass

    # 美股热门 + 中文别名
    try:
        sys.path.insert(0, str(ROOT / "apps" / "web"))
        import backtest_game as bg

        for it in getattr(bg, "_US_POPULAR", []):
            yield "us", "stock", it["code"], it.get("name") or it["code"]
        for sym in (getattr(bg, "_US_ALIAS", {}) or {}).values():
            yield "us", "stock", str(sym), str(sym)
    except Exception:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description="预取板块/指数历史 K 线到本地 SQLite")
    ap.add_argument("--market", choices=["cn", "us"], default="cn")
    ap.add_argument("--kind", choices=["industry", "concept", "index"], default="industry")
    ap.add_argument("--all", action="store_true", help="A股行业+概念+指数+美股")
    ap.add_argument("--stocks", action="store_true", help="加上项目股票池/自选/美股热门")
    ap.add_argument("--cn-all", action="store_true", help="加上全部 A 股（约5000，重）")
    ap.add_argument("--update", action="store_true", help="只补最新 5 根（日线）")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 个（调试）")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--sleep", type=float, default=0.0, help="每个任务后 sleep 秒")
    args = ap.parse_args()

    if args.all:
        jobs = []
        for m, k in [("cn", "industry"), ("cn", "concept"), ("cn", "index"), ("us", "industry")]:
            jobs.extend(targets(m, k))
    else:
        jobs = list(targets(args.market, args.kind))
    if args.stocks:
        jobs.extend(stock_targets())
    if args.cn_all:
        sys.path.insert(0, str(ROOT / "apps" / "web"))
        import backtest_game as bg

        jobs.extend(
            ("cn", "stock", str(it["code"]), it.get("name", "")) for it in bg._cn_stock_universe()
        )
    # 去重
    seen = set()
    uniq = []
    for j in jobs:
        key = (j[0], str(j[2]).upper())
        if key in seen:
            continue
        seen.add(key)
        uniq.append(j)
    jobs = uniq
    if args.limit:
        jobs = jobs[: args.limit]

    tail = 5 if args.update else None
    print(f"目标 {len(jobs)} 个 | workers={args.workers} | update={args.update} | DB={sm.DB_FILE}")
    if not jobs:
        print("没有目标，退出")
        return

    ok = fail = 0
    t0 = time.time()

    def work(job):
        market, kind, code, name = job
        bars, src = sm._bars_fetch(market, code, "1d", name, kind, retry=True, tail=tail)
        return job, len(bars), src

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
        futs = {ex.submit(work, j): j for j in jobs}
        for i, fut in enumerate(as_completed(futs), 1):
            job = futs[fut]
            try:
                _, n, src = fut.result()
                if n:
                    ok += 1
                else:
                    fail += 1
                print(f"[{i}/{len(jobs)}] {job[0]}:{job[1]}:{job[3]}({job[2]}) -> {n} bars {src}")
            except Exception as e:
                fail += 1
                print(f"[{i}/{len(jobs)}] FAIL {job}: {e}")
            if args.sleep:
                time.sleep(args.sleep)

    print(f"完成: ok={ok} fail={fail} 用时 {time.time() - t0:.1f}s")
    print(f"数据库: {sm.DB_FILE}")


if __name__ == "__main__":
    main()
