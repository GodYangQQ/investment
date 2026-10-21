#!/usr/bin/env python3
"""
OKX 标的探针：确认是否上架 SOXX/SOXL，以及正确的 instId。
在能访问 OKX 的网络运行。

用法:
    python scripts/okx_probe.py SOXX
    python scripts/okx_probe.py SOXL
    python scripts/okx_probe.py SPY
"""
import sys
import requests

H = {"User-Agent": "Mozilla/5.0"}
BASES = ["https://www.okx.com", "https://aws.okx.com"]


def probe(base, q):
    print(f"\n== {base}  搜索: {q}")
    for inst_type in ("SPOT", "SWAP"):
        try:
            r = requests.get(base + "/api/v5/public/instruments",
                             params={"instType": inst_type}, headers=H, timeout=15)
            data = (r.json() or {}).get("data") or []
            hits = [x for x in data if q.upper() in str(x.get("instId", "")).upper()]
            print(f"  {inst_type}: 共 {len(data)} 个，命中 {len(hits)} 个")
            for x in hits:
                iid = x.get("instId")
                print(f"    - {iid}   ({x.get('baseCcy','')}/{x.get('quoteCcy','')})")
                try:
                    t = requests.get(base + "/api/v5/market/ticker",
                                     params={"instId": iid}, headers=H, timeout=10).json()
                    d = (t.get("data") or [])
                    if d:
                        print(f"        last={d[0].get('last')}  open24h={d[0].get('open24h')}  vol24h={d[0].get('vol24h')}")
                except Exception as e:
                    print("        ticker ERR", str(e)[:60])
        except Exception as e:
            print(f"  {inst_type}: 连接失败 {str(e)[:80]}")


def main():
    q = sys.argv[1] if len(sys.argv) > 1 else "SOXX"
    ok = False
    for base in BASES:
        probe(base, q)
    print("\n如果上面有命中，用它的 instId 启动看盘：")
    print(f'  python run/soxx_monitor.py --source okx --okx-inst <上面命中的instId>')


if __name__ == "__main__":
    main()
