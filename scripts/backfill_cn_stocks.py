#!/usr/bin/env python3
"""慢速、可续跑地补齐全 A 股 15 年日线（akshare 兜底）。

用法:
    python scripts/backfill_cn_stocks.py --wait 1200 --sleep 2
    python scripts/backfill_cn_stocks.py --limit 300 --sleep 1.5

已 >= --min-bars 的代码会自动跳过，可反复运行。
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "apps" / "web"))

import sector_monitor_web as sm  # noqa: E402
import backtest_game as bg  # noqa: E402


def bare(code: str) -> str:
    return re.sub(r"^(SH|SZ|BJ)", "", str(code).strip().upper())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", type=int, default=0, help="启动前等待秒数（等限流恢复）")
    ap.add_argument("--sleep", type=float, default=1.5, help="每只股票后的暂停")
    ap.add_argument("--min-bars", type=int, default=3000, help="达到多少根视为已完成")
    ap.add_argument("--limit", type=int, default=0, help="本次最多处理多少只")
    args = ap.parse_args()

    if args.wait:
        print(f"wait {args.wait}s ...", flush=True)
        time.sleep(args.wait)

    universe = bg._cn_stock_universe()
    conn = sqlite3.connect(sm.DB_FILE)
    done = set()
    for code, n in conn.execute(
        "select code, count(*) from bars where market='cn' and tf='1d' group by code"
    ):
        if n >= args.min_bars:
            done.add(bare(code))
    todo = [x for x in universe if bare(x["code"]) not in done]
    if args.limit:
        todo = todo[: args.limit]
    print(f"universe {len(universe)} | done {len(done)} | todo {len(todo)}", flush=True)

    ok = fail = 0
    for i, it in enumerate(todo, 1):
        code = sm._cn_code_norm(it["code"])
        name = it.get("name", "")
        got = 0
        try:
            bars, _src = sm._bars_fetch("cn", code, "1d", name, "stock", retry=True, tail=None)
            got = len(bars)
        except Exception:
            pass
        print(f"[{i}/{len(todo)}] {name}({code}) -> {got}", flush=True)
        ok += 1 if got >= args.min_bars else 0
        fail += 0 if got >= args.min_bars else 1
        time.sleep(args.sleep)
    print(f"DONE ok={ok} fail={fail}", flush=True)


if __name__ == "__main__":
    main()
