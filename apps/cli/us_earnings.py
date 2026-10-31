#!/usr/bin/env python3
"""美股个股财报：从 SEC XBRL 拉最近几个季度的营收/净利润/EPS。

用法：
    python apps/cli/us_earnings.py AMD
    python apps/cli/us_earnings.py AMD AVGO MU --quarters 12
    python apps/cli/us_earnings.py --watch          # 自选里的美股
    python apps/cli/us_earnings.py AMD --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.datasets.us_fundamentals import (  # noqa: E402
    format_earnings,
    ticker_map,
    us_earnings,
)


def watch_tickers() -> list[str]:
    try:
        items = json.loads((ROOT / "data" / "watch.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    return [str(x.get("code") or "").upper() for x in items or [] if x.get("market") == "us"]


def main() -> int:
    ap = argparse.ArgumentParser(description="美股个股财报（SEC XBRL）")
    ap.add_argument("tickers", nargs="*", help="股票代码，如 AMD AVGO")
    ap.add_argument("--quarters", type=int, default=8, help="显示最近几个季度（默认 8）")
    ap.add_argument("--watch", action="store_true", help="使用自选里的美股")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    tickers = args.tickers or (watch_tickers() if args.watch else [])
    if args.watch:
        known = ticker_map()
        skipped = [t for t in tickers if t not in known]
        tickers = [t for t in tickers if t in known]
        if skipped:
            print(f"跳过非个股（ETF/指数）：{', '.join(skipped)}", file=sys.stderr)
    if not tickers:
        ap.error("请给出 ticker，或使用 --watch")

    rows = []
    for tk in tickers:
        try:
            d = us_earnings(tk, quarters=args.quarters)
        except Exception as e:  # noqa: BLE001
            print(f"{tk}: 获取失败 {e}", file=sys.stderr)
            continue
        rows.append(d)
        if not args.json:
            print(format_earnings(d))
            print()
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
