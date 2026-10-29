#!/usr/bin/env python3
"""一次性预取看盘所需数据：板块/指数/美股/（可选）全A股 + 评分缓存。

用法:
    python scripts/prefetch_all.py --all --workers 4 --sleep 0.1
    python scripts/prefetch_all.py --all --with-stocks --workers 1 --sleep 0.5
    python scripts/prefetch_all.py --scores

之后用只读模式启动看盘（前端只拿本地现成数据，不触发外部抓取）:
    python apps/web/sector_monitor_web.py --port 8888 --offline

建议在收盘后（15:30 之后）运行，抓到的当日数据才是最终值。
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "apps" / "web"))

import sector_monitor_web as sm  # noqa: E402


def targets(include_stocks: bool = False) -> list[tuple[str, str, str, str]]:
    jobs: list[tuple[str, str, str, str]] = []

    for kind in ("industry", "concept"):
        try:
            rows, _ = sm.fetch_cn_sectors(kind)
            for r in rows:
                jobs.append(("cn", kind, str(r["code"]), r.get("name", "")))
        except Exception as e:
            print(f"skip {kind}: {e}")

    try:
        for r in sm.fetch_cn_indices():
            jobs.append(("cn", "index", str(r["code"]), r.get("name", "")))
    except Exception as e:
        print(f"skip index: {e}")

    try:
        rows, _ = sm.fetch_us_sectors()
        for r in rows:
            jobs.append(("us", "industry", str(r["code"]), r.get("name", "")))
    except Exception:
        for g in sm.US_GROUPS:
            for code, name in g[2]:
                jobs.append(("us", "industry", code, name))

    if include_stocks:
        try:
            import backtest_game as bg

            for it in bg._cn_stock_universe():
                jobs.append(("cn", "stock", str(it["code"]), it.get("name", "")))
        except Exception as e:
            print(f"skip stocks: {e}")

    seen: set[tuple[str, str]] = set()
    out: list[tuple[str, str, str, str]] = []
    for j in jobs:
        k = (j[0], j[2])
        if k in seen:
            continue
        seen.add(k)
        out.append(j)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="板块 + 指数 + 美股 + 评分")
    ap.add_argument("--with-stocks", action="store_true", help="加上全部 A 股个股")
    ap.add_argument("--scores", action="store_true", help="只构建评分缓存")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--sleep", type=float, default=0.1)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    if not args.all and not args.with_stocks and not args.scores:
        args.all = True

    do_data = args.all or args.with_stocks
    do_scores = args.scores or args.all

    if do_data:
        jobs = targets(include_stocks=args.with_stocks)
        if args.limit:
            jobs = jobs[: args.limit]
        print(f"targets {len(jobs)} | workers={args.workers}", flush=True)
        ok = fail = 0

        def work(job):
            market, kind, code, name = job
            bars, _src = sm._bars_fetch(market, code, "1d", name, kind, retry=True, tail=None)
            return job, len(bars)

        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as ex:
            futs = {ex.submit(work, j): j for j in jobs}
            for i, fut in enumerate(as_completed(futs), 1):
                job = futs[fut]
                try:
                    _, n = fut.result()
                except Exception:
                    n = 0
                if n:
                    ok += 1
                else:
                    fail += 1
                print(f"[{i}/{len(jobs)}] {job[0]}:{job[1]}:{job[3]}({job[2]}) -> {n}", flush=True)
                if args.sleep:
                    time.sleep(args.sleep)
        print(f"data done ok={ok} fail={fail}", flush=True)

    if do_scores:
        try:
            from core.scoring.company_score import load_scores

            t = time.time()
            df = load_scores(force=True)
            print(
                f"scores rows={0 if df is None else len(df)} cost={time.time() - t:.1f}s",
                flush=True,
            )
        except Exception as e:
            print("scores ERR", e, flush=True)

    print("done", flush=True)


if __name__ == "__main__":
    main()
