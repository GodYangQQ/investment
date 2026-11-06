#!/usr/bin/env python3
"""查询 SOXX 的 24 小时代币化价格。

数据源：
    OKX / Binance 公开行情（目前通常没有 SOXX 交易对）
    DexScreener → Robinhood 链上的 iShares Semiconductor ETF Token（24/7）

用法：
    python scripts/okx_soxx_ticker.py
    python scripts/okx_soxx_ticker.py --watch
    python scripts/okx_soxx_ticker.py --interval 5
    python scripts/okx_soxx_ticker.py --proxy http://127.0.0.1:7897
    python scripts/okx_soxx_ticker.py --no-dex

只做公开行情探测，不接交易，不写入项目数据库。
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    )
}
OKX_BASES = ["https://www.okx.com", "https://aws.okx.com"]
BINANCE_BASES = ["https://api.binance.com", "https://api1.binance.com"]

DEX_TOKENS = {
    "SOXX": {
        "chain": "robinhood",
        "address": "0x75742c18bc1f1c5c5f448f4c9d9c6f66dafaaa38",
        "label": "SOXX (24/7)",
    },
    "SOXL": {
        "chain": "bsc",
        "address": "0xd97d097a89113fa59b76c572e5b2eb647e8eefaf",
        "label": "SOXL (24/7)",
    },
    "QQQ": {
        "chain": "robinhood",
        "address": "0xd5f3879160bc7c32ebb4dc785f8a4f505888de68",
        "label": "QQQ (24/7)",
    },
}


def _session(proxy: str = "") -> requests.Session:
    s = requests.Session()
    s.headers.update(UA)
    if proxy:
        s.proxies.update({"http": proxy, "https": proxy})
    return s


def _get_json(s: requests.Session, url: str, params: dict | None = None, timeout: int = 15):
    r = s.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _fmt_ts(ms) -> str:
    try:
        return datetime.fromtimestamp(float(ms) / 1000).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError):
        return "-"


def _pct(new, old=None, direct=None) -> str:
    if direct is not None:
        try:
            return f"{float(direct):+.2f}%"
        except (TypeError, ValueError):
            return "-"
    try:
        n, o = float(new), float(old)
        if o <= 0:
            return "-"
        return f"{(n / o - 1) * 100:+.2f}%"
    except (TypeError, ValueError, ZeroDivisionError):
        return "-"


def okx_probe(s: requests.Session) -> tuple[list[dict], list[str]]:
    found: dict[str, str] = {}
    errors: list[str] = []
    for base in OKX_BASES:
        for inst_type in ("SPOT", "SWAP"):
            try:
                d = _get_json(s, base + "/api/v5/public/instruments", {"instType": inst_type})
            except Exception as e:
                errors.append(f"OKX {inst_type}: {str(e)[:80]}")
                continue
            for it in d.get("data") or []:
                iid = str(it.get("instId") or "")
                base_ccy = str(it.get("baseCcy") or it.get("ctValCcy") or "")
                if "SOXX" in iid.upper() or base_ccy.upper() == "SOXX":
                    found[iid] = base
    rows = []
    for iid, base in found.items():
        try:
            d = _get_json(s, base + "/api/v5/market/ticker", {"instId": iid}, timeout=10)
            t = (d.get("data") or [{}])[0]
        except Exception as e:
            errors.append(f"OKX {iid}: {str(e)[:80]}")
            continue
        rows.append(
            {
                "venue": "OKX",
                "symbol": iid,
                "last": t.get("last"),
                "bid": t.get("bidPx"),
                "ask": t.get("askPx"),
                "open24h": t.get("open24h"),
                "high24h": t.get("high24h"),
                "low24h": t.get("low24h"),
                "vol24h": t.get("vol24h"),
                "ts": t.get("ts"),
            }
        )
    return rows, errors


def binance_probe(s: requests.Session) -> tuple[list[dict], list[str]]:
    rows: list[dict] = []
    errors: list[str] = []
    for base in BINANCE_BASES:
        try:
            d = _get_json(s, base + "/api/v3/exchangeInfo")
        except Exception as e:
            errors.append(f"Binance: {str(e)[:80]}")
            continue
        symbols = {
            str(x.get("symbol") or ""): x
            for x in d.get("symbols") or []
            if "SOXX" in str(x.get("symbol") or "").upper()
        }
        for sym in symbols:
            try:
                t = _get_json(s, base + "/api/v3/ticker/24hr", {"symbol": sym}, timeout=10)
            except Exception as e:
                errors.append(f"Binance {sym}: {str(e)[:80]}")
                continue
            rows.append(
                {
                    "venue": "Binance",
                    "symbol": sym,
                    "last": t.get("lastPrice"),
                    "bid": t.get("bidPrice"),
                    "ask": t.get("askPrice"),
                    "open24h": t.get("openPrice"),
                    "high24h": t.get("highPrice"),
                    "low24h": t.get("lowPrice"),
                    "vol24h": t.get("volume"),
                    "ts": t.get("closeTime"),
                }
            )
        if rows:
            break
    return rows, errors


def dex_probe(s: requests.Session, symbol: str) -> tuple[list[dict], list[str]]:
    cfg = DEX_TOKENS.get(symbol.upper())
    if not cfg:
        return [], [f"DexScreener: 未配置 {symbol}"]
    url = f"https://api.dexscreener.com/latest/dex/tokens/{cfg['address']}"
    try:
        d = _get_json(s, url, timeout=15)
    except Exception as e:
        return [], [f"DexScreener {symbol}: {str(e)[:80]}"]
    pairs = []
    for p in d.get("pairs") or []:
        base = p.get("baseToken") or {}
        if str(base.get("address") or "").lower() != cfg["address"]:
            continue
        if str(p.get("chainId") or "") != cfg["chain"]:
            continue
        pairs.append(p)
    if not pairs:
        return [], [f"DexScreener: 未找到 {symbol} Token"]
    pairs.sort(key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0), reverse=True)
    p = pairs[0]
    liq = (p.get("liquidity") or {}).get("usd")
    vol = (p.get("volume") or {}).get("h24")
    vol_h1 = (p.get("volume") or {}).get("h1")
    h1 = (p.get("txns") or {}).get("h1") or {}
    txns_h1 = int(h1.get("buys") or 0) + int(h1.get("sells") or 0)
    return [
        {
            "venue": f"{cfg['chain'].title()} Token",
            "symbol": cfg["label"],
            "last": p.get("priceUsd"),
            "bid": None,
            "ask": None,
            "change24": (p.get("priceChange") or {}).get("h24"),
            "high24h": None,
            "low24h": None,
            "vol24h": vol,
            "vol_h1": vol_h1,
            "txns_h1": txns_h1,
            "liquidity": liq,
            "url": p.get("url"),
            "ts": None,
        }
    ], []


def _log_rows(path: str, rows: list[dict]) -> None:
    if not path:
        return
    try:
        fp = Path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        new = not fp.exists() or fp.stat().st_size == 0
        with open(fp, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(
                    ["time", "venue", "symbol", "last", "bid", "ask",
                     "change24", "open24h", "high24h", "low24h", "vol24h", "liquidity"]
                )
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            for r in rows:
                w.writerow(
                    [now, r.get("venue"), r.get("symbol"), r.get("last"), r.get("bid"),
                     r.get("ask"), r.get("change24"), r.get("open24h"), r.get("high24h"),
                     r.get("low24h"), r.get("vol24h"), r.get("liquidity")]
                )
    except Exception as e:
        print(f"写日志失败：{e}", file=sys.stderr)


def _compact(rows: list[dict]) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    for r in rows:
        chg = r.get("change24")
        chg_s = _pct(None, direct=chg) if chg is not None else _pct(r.get("last"), r.get("open24h"))
        extra = ""
        if r.get("liquidity") is not None:
            extra += f"  liq=${float(r.get('liquidity')):,.0f}"
        if r.get("vol24h") is not None:
            extra += f"  vol={r.get('vol24h')}"
        if r.get("txns_h1") is not None:
            extra += f"  1h笔数={r.get('txns_h1')}"
        print(f"{now}   {r.get('symbol'):<12} {str(r.get('last')):>10}  {chg_s:>8}{extra}", flush=True)


def print_rows(rows: list[dict]) -> None:
    for r in rows:
        print(f"{r['venue']} {r['symbol']}")
        print(f"  last     {r.get('last')}")
        if r.get("bid") is not None or r.get("ask") is not None:
            print(f"  bid/ask  {r.get('bid')} / {r.get('ask')}")
        if r.get("change24") is not None:
            print(f"  24h      {_pct(None, direct=r.get('change24'))}")
        elif r.get("open24h") is not None:
            print(
                f"  24h      {r.get('open24h')} -> {r.get('last')}  ({_pct(r.get('last'), r.get('open24h'))})"
            )
        if r.get("high24h") is not None or r.get("low24h") is not None:
            print(f"  high/low {r.get('high24h')} / {r.get('low24h')}")
        if r.get("vol24h") is not None:
            print(f"  vol24h   {r.get('vol24h')}")
        if r.get("txns_h1") is not None:
            print(f"  近1h     {r.get('txns_h1')} 笔 / 成交额 {r.get('vol_h1')}")
        if r.get("liquidity") is not None:
            print(f"  liquidity ${float(r.get('liquidity')):,.0f}")
        if r.get("url"):
            print(f"  pair     {r.get('url')}")
        if r.get("ts"):
            print(f"  time     {_fmt_ts(r.get('ts'))}")


def main() -> int:
    ap = argparse.ArgumentParser(description="查询 SOXX 24 小时代币化价格")
    ap.add_argument("--watch", action="store_true", help="持续刷新")
    ap.add_argument("--interval", type=float, default=1.0, help="刷新/重试间隔秒，默认 1")
    ap.add_argument("--proxy", default=os.environ.get("HTTPS_PROXY", ""), help="HTTP/HTTPS 代理")
    ap.add_argument("--no-dex", action="store_true", help="不查询代币价格")
    ap.add_argument("--symbol", choices=["SOXX", "SOXL", "QQQ", "both", "all"], default="both",
                    help="监控标的，默认 both（SOXX+SOXL），all 含 QQQ")
    ap.add_argument("--log", default="output/soxx_ticker.csv", help="价格变化时追加写入 CSV")
    ap.add_argument("--venue", choices=["dex", "all", "okx", "binance"], default="dex",
                    help="dex=只查 24/7 Token；all=同时探测 OKX/Binance")
    args = ap.parse_args()

    s = _session(args.proxy)
    if args.symbol == "both":
        symbols = ["SOXX", "SOXL"]
    elif args.symbol == "all":
        symbols = ["SOXX", "SOXL", "QQQ"]
    else:
        symbols = [args.symbol]
    first = True
    spot_rows: list[dict] = []
    last_spot = 0.0
    prev_last: dict[str, str] = {}
    while True:
        rows: list[dict] = []
        errors: list[str] = []
        now = time.time()
        if args.venue in ("dex", "all") and not args.no_dex:
            for sym in symbols:
                r, e = dex_probe(s, sym)
                rows += r
                errors += e
        if args.venue in ("all", "okx", "binance") and now - last_spot >= 60:
            spot_rows = []
            if args.venue in ("all", "okx"):
                r, e = okx_probe(s)
                spot_rows += r
                errors += e
            if args.venue in ("all", "binance"):
                r, e = binance_probe(s)
                spot_rows += r
                errors += e
            last_spot = now
        rows += spot_rows

        changed: list[dict] = []
        for r in rows:
            key = f"{r.get('venue')}:{r.get('symbol')}"
            val = str(r.get("last"))
            if prev_last.get(key) != val:
                changed.append(r)
                prev_last[key] = val

        to_log = rows if (first or not args.watch) else changed
        if to_log:
            _log_rows(args.log, to_log)

        if first or not args.watch:
            print(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
            if rows:
                print_rows(rows)
                print("\n注：代币价格是 24/7 最后成交价，可能相对 ETF 净值有溢价/折价。")
                print(f"日志：{args.log}\n")
                print("等待价格变化…")
            else:
                print("暂未拿到价格，继续重试…")
                for e in errors[:4]:
                    print(f"  - {e}")
        elif changed:
            _compact(changed)
        first = False

        if rows and not args.watch:
            return 0
        try:
            time.sleep(max(1.0, args.interval))
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except requests.RequestException as e:
        print(f"网络请求失败：{e}", file=sys.stderr)
        print(
            "如果本机需要代理，试试：python scripts/okx_soxx_ticker.py --proxy http://127.0.0.1:7897",
            file=sys.stderr,
        )
        raise SystemExit(1)
