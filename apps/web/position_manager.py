#!/usr/bin/env python3
"""仓位管家 · Web 服务
======================
对应 prompt/01-系统蓝图.md。零框架（stdlib ThreadingHTTPServer + 原生 HTML）。

启动:
    python apps/web/position_manager.py                 # http://127.0.0.1:8781
    python apps/web/position_manager.py --port 8782
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

RUN_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(os.path.dirname(RUN_DIR))
UI_FILE = os.path.join(RUN_DIR, "position_manager.html")
sys.path.insert(0, ROOT_DIR)

from core.position import position_rules as rules  # noqa: E402
from core.position import position_store as store  # noqa: E402
from core.datasets import price_data  # noqa: E402


def jload(s, default=None):
    try:
        return json.loads(s) if s else default
    except Exception:
        return default


DEFAULT_INDICES = (
    ("SH000001", "上证指数"),
    ("SZ399001", "深证成指"),
    ("SZ399006", "创业板指"),
    ("SH000300", "沪深300"),
    ("SH000688", "科创50"),
)


class Service:
    def __init__(self):
        store.init_db()

    def seed_indices(self, force: bool = False) -> dict:
        """首次启动灌入常用指数并同步日线；force=True 时补缺失的指数。"""
        s = store.get_settings()
        if s.get("_seeded") and not force:
            return {"ok": True, "added": []}
        have = {i["code"] for i in store.list_instruments(include_archived=True,
                                                          exclude_test=False)}
        added = []
        for code, name in DEFAULT_INDICES:
            if code in have:
                continue
            store.add_instrument(code, name, type="index", pool="watch",
                                 direction_label="undecided")
            added.append(code)
        for code in added:
            try:
                self.sync(code)
            except Exception as exc:  # noqa: BLE001
                print(f"[seed] {code} 同步失败: {exc}", flush=True)
        store.save_settings({"_seeded": True})
        return {"ok": True, "added": added}

    # ---------------- 数据 ----------------
    def sync(self, code: str, count: int = 900) -> dict:
        c = price_data.code_norm(code)
        if not c:
            return {"ok": False, "error": "code required"}
        bars, src = price_data.fetch_daily(c, count=count)
        n = store.put_bars(c, bars, src) if bars else 0
        return {"ok": bool(bars), "code": c, "rows": n, "source": src,
                "error": "" if bars else "行情抓取失败"}

    def ensure_bars(self, code: str) -> list[dict]:
        c = price_data.code_norm(code)
        bars = store.get_bars(c)
        last = str(bars[-1]["t"])[:10] if bars else ""
        if not bars or last < datetime.now().strftime("%Y-%m-%d"):
            self.sync(c)
            bars = store.get_bars(c)
        return bars

    def _start_idx(self, bars: list[dict], eff: str | None) -> int:
        if not eff:
            return 0
        for i, b in enumerate(bars):
            if str(b["t"])[:10] >= str(eff)[:10]:
                return i
        return 0

    def _idx_state(self, inst: dict, upto: str | None = None) -> dict:
        links = inst.get("links") or store.list_index_links(inst["id"])
        primary = next((x for x in links if x.get("is_primary")), None) or (links[0] if links else None)
        code = (primary or {}).get("index_code") or "SH000001"
        idx = store.get_instrument(code)
        base = {"code": code, "label": (idx or {}).get("direction_label") or "undecided",
                "completion": None, "phase": None, "close": None, "name": (idx or {}).get("name") or code}
        if not idx:
            return base
        bars = store.get_bars(code)
        if upto:
            bars = [b for b in bars if str(b["t"])[:10] <= str(upto)[:10]]
        if not bars:
            return base
        base["close"] = float(bars[-1]["c"])
        lv = store.active_levels(idx["id"])
        nk = (lv.get("neckline") or {}).get("price")
        tg = (lv.get("target") or {}).get("price")
        if nk is not None and tg is not None:
            start = self._start_idx(bars, (lv.get("neckline") or {}).get("effective_from"))
            st = rules.structure_state(float(nk), float(tg), bars, start, store.get_settings())
            base.update(st)
        return base

    def compute_row(self, inst: dict) -> dict:
        bars = store.get_bars(inst["code"])
        close = float(bars[-1]["c"]) if bars else None
        pct = None
        if len(bars) >= 2 and close:
            prev = float(bars[-2]["c"])
            pct = (close / prev - 1) * 100 if prev else None
        lv = store.active_levels(inst["id"])
        nk = (lv.get("neckline") or {}).get("price")
        tg = (lv.get("target") or {}).get("price")
        cp = phase = None
        if close and nk is not None and tg is not None:
            st = rules.structure_state(float(nk), float(tg), bars,
                                       self._start_idx(bars, (lv.get("neckline") or {}).get("effective_from")),
                                       store.get_settings())
            cp, phase = st.get("completion"), st.get("phase")
        sent = []
        for s in inst.get("sentinels") or []:
            px = float(s["price"])
            sent.append({"id": s["id"], "price": px, "direction": s.get("direction") or "up",
                         "distance": (close / px - 1) if close else None})
        camp = store.open_campaign_for(inst["id"])
        return {"id": inst["id"], "code": inst["code"], "name": inst.get("name"),
                "type": inst.get("type"), "market": inst.get("market"),
                "sector": inst.get("sector"), "star": inst.get("star"),
                "direction_label": inst.get("direction_label") or "undecided",
                "pool": inst.get("pool") or "watch", "sub_status": inst.get("sub_status") or "",
                "option_note": inst.get("option_note") or "",
                "close": close, "pct": pct,
                "neckline": nk, "target": tg,
                "completion": cp, "phase": phase,
                "levels": lv, "sentinels": sent,
                "campaign": camp,
                "bars_n": len(bars), "last_bar": bars[-1]["t"] if bars else None}

    # ---------------- 评估 ----------------
    def eval_instrument(self, inst: dict, upto: str | None = None, persist: bool = True,
                        holding: dict | None = None) -> dict:
        bars = store.get_bars(inst["code"])
        if upto:
            bars = [b for b in bars if str(b["t"])[:10] <= str(upto)[:10]]
        if not bars:
            return {"code": inst["code"], "signals": [], "error": "no bars"}
        lv = store.active_levels(inst["id"])
        levels = {
            "neckline": lv.get("neckline"),
            "target": lv.get("target"),
            "second_target": lv.get("second_target"),
            "pre_breakout": lv.get("pre_breakout"),
            "top_neckline": lv.get("top_neckline"),
            "pullback": lv.get("pullback") or lv.get("pullback_zone"),
        }
        nk = (levels["neckline"] or {}).get("price")
        tg = (levels["target"] or {}).get("price")
        start = self._start_idx(bars, (levels["neckline"] or {}).get("effective_from"))
        st = (rules.structure_state(float(nk), float(tg), bars, start, store.get_settings())
              if nk is not None and tg is not None else None)
        if persist:
            holding = store.open_campaign_for(inst["id"])
        idx_state = self._idx_state(inst, upto or (str(bars[-1]["t"])[:10] if bars else None))
        settings = store.get_settings()
        sigs = rules.evaluate(inst, bars, levels, st, idx_state,
                              inst.get("sentinels") or store.list_sentinels(inst["id"]),
                              settings, holding, start)
        close = float(bars[-1]["c"])
        date = str(bars[-1]["t"])[:10]
        if persist:
            fresh = store.record_signals(inst["id"], sigs)
            for s in fresh:
                self._apply_signal(inst, s, close, date)
            if st:
                store.upsert_round(inst["id"], st, 1,
                                   (levels["neckline"] or {}).get("id"),
                                   (levels["target"] or {}).get("id"))
                camp = store.open_campaign_for(inst["id"])
                if camp and st.get("completion") == st.get("completion"):
                    store.update_campaign(camp["id"], completed=float(st["completion"]))
            # 唯一自动流转：左侧 → 筑底（价格进入起涨区/触及哨兵）
            if any(s["rule"] == "left_to_base" for s in fresh) and \
                    inst.get("direction_label") == "left":
                store.update_instrument(inst["id"], direction_label="base",
                                        sub_status="已进入起涨区·候形态")
            return {"code": inst["code"], "signals": fresh, "date": date, "close": close,
                    "structure": st, "index": idx_state}
        return {"code": inst["code"], "signals": sigs, "date": date, "close": close,
                "structure": st, "index": idx_state}

    def _apply_signal(self, inst: dict, s: dict, close: float, date: str) -> None:
        iid, rule, p = inst["id"], s["rule"], s.get("payload") or {}
        camp = store.open_campaign_for(iid)
        if rule == "entry":
            store.open_campaign(iid, date, close, float(p.get("position") or 1.0))
            return
        if not camp:
            return
        ev = {"date": date, "price": close, "reason": rule}
        if rule == "trim":
            ev["remain"] = min(float(camp.get("position") or 0), float(p.get("remain") or 0))
            store.update_campaign(camp["id"], position=ev["remain"], completed=float(p.get("stage") or 0),
                                  exit_event=ev)
        elif rule == "final_trim":
            ev["remain"] = min(float(camp.get("position") or 0), float(p.get("remain") or 0))
            store.update_campaign(camp["id"], position=ev["remain"], completed=1.0, exit_event=ev)
        elif rule in ("trail", "fake_break", "top_break"):
            ev["remain"] = 0.0
            pnl = rules.campaign_pnl(float(camp["entry_price"]),
                                     [dict(e, remain=e.get("remain", 0)) for e in (camp.get("exits") or [])]
                                     + [ev])
            store.update_campaign(camp["id"], position=0.0, exit_event=ev,
                                  close_it=True, pnl_pct=pnl["pnl_pct"])

    def evaluate_all(self) -> dict:
        out = []
        for inst in store.list_instruments():
            try:
                self.ensure_bars(inst["code"])
                r = self.eval_instrument(store.get_instrument(inst["id"]))
                out.append({"code": inst["code"], "signals": len(r.get("signals") or []),
                            "error": r.get("error")})
            except Exception as exc:  # noqa: BLE001
                out.append({"code": inst["code"], "error": str(exc)})
        return {"ok": True, "date": datetime.now().strftime("%Y-%m-%d"), "results": out}

    # ---------------- 看板 ----------------
    def dashboard(self) -> dict:
        insts = store.list_instruments()
        rows = []
        for i in insts:
            try:
                rows.append(self.compute_row(i))
            except Exception as exc:  # noqa: BLE001
                rows.append({"id": i["id"], "code": i["code"], "name": i.get("name"),
                             "error": str(exc)})
        idx_rows = [r for r in rows if r.get("type") == "index"]
        stocks = [r for r in rows if r.get("type") != "index"]
        stats = {"hold": 0, "ready": 0, "pullback": 0, "watch": 0,
                 "right": 0, "base": 0, "left": 0, "undecided": 0, "total": len(stocks)}
        for r in stocks:
            if r.get("pool") in stats:
                stats[r["pool"]] += 1
            lbl = r.get("direction_label") or "undecided"
            stats[lbl] = stats.get(lbl, 0) + 1
        heat = []
        for r in stocks:
            for s in r.get("sentinels") or []:
                d = s.get("distance")
                if d is not None and abs(d) <= 0.15:
                    heat.append({"code": r["code"], "name": r["name"], "price": s["price"],
                                 "close": r["close"], "direction": s["direction"],
                                 "distance": d})
        heat.sort(key=lambda x: abs(x["distance"]))
        return {"as_of": datetime.now().strftime("%Y-%m-%d %H:%M"),
                "stats": stats, "indices": idx_rows, "rows": stocks,
                "instruments": rows,
                "sentinels": heat[:12],
                "signals": store.list_signals(days=5, limit=80),
                "campaigns": store.list_campaigns(),
                "settings": store.get_settings(),
                "db": store.stats()}

    # ---------------- 回放 ----------------
    def replay(self, code: str, days: int = 120) -> dict:
        inst = store.get_instrument(code)
        if not inst:
            return {"ok": False, "error": "标的不存在"}
        self.ensure_bars(inst["code"])
        bars = store.get_bars(inst["code"])[-days:]
        if not bars:
            return {"ok": False, "error": "无日线"}
        lv = store.active_levels(inst["id"])
        levels = {"neckline": lv.get("neckline"), "target": lv.get("target"),
                  "top_neckline": lv.get("top_neckline"),
                  "pullback": lv.get("pullback") or lv.get("pullback_zone"),
                  "pre_breakout": lv.get("pre_breakout")}
        start = self._start_idx(bars, (levels["neckline"] or {}).get("effective_from"))
        idx_state = self._idx_state(inst)
        out = []
        sim = None
        seen: set[tuple[str, str]] = set()
        for i in range(start, len(bars)):
            seg = bars[:i + 1]
            res = self.eval_instrument(inst, upto=seg[-1]["t"], persist=False, holding=sim)
            close = seg[-1]["c"]
            fresh = []
            for s in res.get("signals") or []:
                key = (s["rule"], s["state_key"])
                if key in seen:
                    continue
                seen.add(key)
                fresh.append(s)
                p = s.get("payload") or {}
                if s["rule"] == "entry" and not sim:
                    sim = {"entry_price": close, "entry_date": seg[-1]["t"],
                           "position": float(p.get("position") or 1.0), "completed": 0.0}
                elif sim and s["rule"] == "trim":
                    sim["position"] = float(p.get("remain") or 0)
                    sim["completed"] = max(sim["completed"], float(p.get("stage") or 0))
                elif sim and s["rule"] == "final_trim":
                    sim["position"] = float(p.get("remain") or 0)
                    sim["completed"] = 1.0
                elif sim and s["rule"] in ("trail", "fake_break", "top_break"):
                    sim = None
            if sim and res.get("structure"):
                cp = res["structure"].get("completion")
                if cp is not None and cp == cp:
                    sim["completed"] = max(sim["completed"], float(cp))
            if fresh:
                out.append({"date": seg[-1]["t"], "close": close,
                            "signals": fresh,
                            "position": (sim or {}).get("position")})
        return {"ok": True, "code": inst["code"], "name": inst.get("name"),
                "start": bars[start]["t"], "bars": len(bars), "days": out}

    # ---------------- 日报 ----------------
    def build_report(self, trade_date: str | None = None) -> dict:
        d = trade_date or datetime.now().strftime("%Y-%m-%d")
        dash = self.dashboard()
        sigs = store.list_signals(trade_date=d, limit=200) or store.list_signals(days=1, limit=50)
        lines = [f"# 仓位管家日报 {d}", ""]
        stats = dash["stats"]
        lines += ["## 一、规格", "",
                  f"- 标的 {stats.get('total', 0)}；持有 {stats['hold']} / 就绪 {stats['ready']} / "
                  f"回撤 {stats['pullback']} / 观察 {stats['watch']}",
                  f"- 方向：右侧 {stats['right']} · 筑底 {stats['base']} · 左侧 {stats['left']} · "
                  f"未定 {stats['undecided']}", ""]
        lines += ["## 二、指数", ""]
        for r in dash["indices"]:
            cp = r.get("completion")
            lines.append(f"- {r['name']}（{r['code']}）收盘 {_f(r.get('close'))} "
                         f"{_pct(r.get('pct'))} · 标签 {_lbl(r.get('direction_label'))}"
                         f"{'' if cp is None else f' · 完成度 {cp:.0%}'}")
        lines += ["", "## 三、操作提示（按优先级）", ""]
        if not sigs:
            lines.append("- 无（今日无新信号）")
        for s in sigs[:40]:
            lines.append(f"- [{s['priority']}] **{s.get('name') or s.get('code')}** "
                         f"{s.get('message')}")
        lines += ["", "## 四、哨兵热度", ""]
        for h in dash["sentinels"]:
            lines.append(f"- {h['name']} 距哨兵 {h['price']:.2f} {h['distance']:+.1%}，"
                         f"现价 {h['close']:.2f}")
        lines += ["", "## 五、持仓 / 战役", ""]
        if not dash["campaigns"]:
            lines.append("- 空仓")
        for c in dash["campaigns"]:
            pnl = "-" if c.get("pnl_pct") is None else f"{float(c['pnl_pct']):+.2f}%"
            lines.append(f"- {c['code']} {c['name']} 入场 {c['entry_price']:.2f} "
                         f"剩余 {c['position']:.0%} 浮动 {pnl}")
        text = "\n".join(lines)
        payload = {"text": text, "stats": stats, "signals": sigs,
                   "indices": [{"code": r["code"], "name": r["name"],
                                "close": r.get("close"), "label": r.get("direction_label")}
                               for r in dash["indices"]],
                   "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        store.save_report(d, payload)
        s = store.get_settings()
        hook = os.getenv("POSITION_FEISHU_WEBHOOK") or s.get("feishu_webhook") or ""
        if hook and s.get("push_enabled", True):
            payload["pushed"] = self.push_feishu(hook, text)
        return payload

    def push_feishu(self, hook: str, text: str) -> dict:
        try:
            import requests
            r = requests.post(hook, json={"msg_type": "text", "content": {"text": text}},
                              timeout=10)
            return {"ok": r.status_code == 200, "status": r.status_code}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}


def _f(v):
    return "-" if v is None else f"{float(v):.2f}"


def _pct(v):
    return "-" if v is None else f"{float(v):+.2f}%"


def _lbl(v):
    return {"right": "右侧", "base": "筑底", "left": "左侧",
            "undecided": "未定"}.get(v or "undecided", v or "未定")


SVC = Service()


class Handler(BaseHTTPRequestHandler):
    server_version = "PositionManager/1.0"

    def log_message(self, fmt, *args):  # 静音
        pass

    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False).encode())

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode() or "{}")
        except Exception:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/index.html"):
                with open(UI_FILE, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
                return
            if u.path == "/api/ping":
                self._json({"ok": True, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")})
                return
            if u.path == "/api/bootstrap":
                self._json(self.bootstrap())
            elif u.path == "/api/dashboard":
                self._json(SVC.dashboard())
            elif u.path == "/api/instruments":
                self._json({"ok": True, "rows": [SVC.compute_row(i) for i in store.list_instruments()]})
            elif u.path == "/api/bars":
                code = q.get("code", "")
                limit = int(q.get("limit") or 800)
                self._json({"ok": True, "code": code,
                            "bars": store.get_bars(code, limit=limit),
                            "levels": store.active_levels(store.get_instrument(code)["id"])
                            if store.get_instrument(code) else {}})
            elif u.path == "/api/signals":
                self._json({"ok": True, "rows": store.list_signals(trade_date=q.get("date"),
                                                                   limit=300)})
            elif u.path == "/api/replay":
                self._json(SVC.replay(q.get("code", ""), int(q.get("days") or 120)))
            elif u.path == "/api/report":
                d = q.get("date") or datetime.now().strftime("%Y-%m-%d")
                rep = store.get_report(d)
                self._json({"ok": True, "date": d, "report": rep})
            elif u.path == "/api/search":
                self._json({"ok": True, "rows": price_data.search_symbols(q.get("q", ""))})
            elif u.path == "/api/seed":
                self._json(SVC.seed_indices(force=True))
            else:
                self._json({"ok": False, "error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "error": str(exc),
                        "trace": traceback.format_exc(limit=3)}, 500)

    def bootstrap(self) -> dict:
        dash = SVC.dashboard()
        dash["ok"] = True
        dash["server_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        dash["reports"] = []
        return dash

    def do_POST(self):
        u = urlparse(self.path)
        q = {k: v[-1] for k, v in parse_qs(u.query).items()}
        b = self._body()
        try:
            if u.path == "/api/instruments":
                code = price_data.code_norm(b.get("code") or "")
                if not code:
                    self._json({"ok": False, "error": "请输入代码"}, 400)
                    return
                inst = store.add_instrument(code, b.get("name") or code,
                                            **{k: b[k] for k in
                                               ("type", "market", "sector", "groups", "star",
                                                "quality_grade", "direction_label", "pool",
                                                "sub_status", "option_note", "note") if b.get(k) is not None})
                if b.get("links"):
                    store.set_index_links(inst["id"], b["links"])
                if b.get("sync", True):
                    SVC.sync(inst["code"])
                self._json({"ok": True, "instrument": SVC.compute_row(store.get_instrument(inst["id"]))})
            elif u.path.startswith("/api/instruments/") and u.path.endswith("/delete"):
                iid = int(u.path.split("/")[3])
                store.soft_delete_instrument(iid)
                self._json({"ok": True})
            elif u.path.startswith("/api/instruments/"):
                iid = int(u.path.split("/")[3])
                inst = store.update_instrument(iid, **b)
                if "links" in b:
                    store.set_index_links(iid, b.get("links") or [])
                self._json({"ok": True, "instrument": SVC.compute_row(store.get_instrument(iid))})
            elif u.path == "/api/levels":
                iid = int(b.get("instrument_id") or 0)
                inst = store.get_instrument(iid)
                if not inst:
                    self._json({"ok": False, "error": "标的不存在"}, 400)
                    return
                lv = store.add_level(iid, b.get("level_type") or "neckline",
                                     _num_or_none(b.get("price")),
                                     _num_or_none(b.get("price_lower")),
                                     b.get("zone_mode"), _num_or_none(b.get("buyback_ratio")),
                                     source=b.get("source") or "manual",
                                     confirm=bool(b.get("confirm")),
                                     note=b.get("note") or "")
                self._json({"ok": True, "level": lv})
            elif u.path.startswith("/api/levels/") and u.path.endswith("/confirm"):
                lid = int(u.path.split("/")[3])
                self._json({"ok": True, "level": store.confirm_level(lid, b.get("effective_from"))})
            elif u.path == "/api/sentinels":
                iid = int(b.get("instrument_id") or 0)
                s = store.add_sentinel(iid, float(b.get("price") or 0),
                                       b.get("direction") or "up")
                self._json({"ok": True, "sentinel": s})
            elif u.path.startswith("/api/sentinels/") and u.path.endswith("/delete"):
                store.delete_sentinel(int(u.path.split("/")[3]))
                self._json({"ok": True})
            elif u.path == "/api/seed":
                self._json(SVC.seed_indices(force=bool(b.get("force"))))
            elif u.path == "/api/settings":
                self._json({"ok": True, "settings": store.save_settings(b)})
            elif u.path == "/api/evaluate":
                code = b.get("code") or q.get("code")
                if code:
                    inst = store.get_instrument(code)
                    if not inst:
                        self._json({"ok": False, "error": "标的不存在"}, 400)
                        return
                    SVC.sync(inst["code"])
                    self._json({"ok": True, "result": SVC.eval_instrument(
                        store.get_instrument(inst["id"]))})
                else:
                    self._json(SVC.evaluate_all())
            elif u.path == "/api/sync":
                codes = b.get("codes") or [i["code"] for i in store.list_instruments()]
                self._json({"ok": True, "results": [SVC.sync(c) for c in codes]})
            elif u.path == "/api/ack":
                store.ack_signal(int(b.get("id") or 0))
                self._json({"ok": True})
            elif u.path == "/api/report":
                self._json({"ok": True, "report": SVC.build_report(b.get("date"))})
            elif u.path == "/api/push":
                rep = store.get_report(b.get("date") or datetime.now().strftime("%Y-%m-%d"))
                if not rep:
                    rep = SVC.build_report()
                hook = os.getenv("POSITION_FEISHU_WEBHOOK") or b.get("webhook") or \
                    store.get_settings().get("feishu_webhook") or ""
                if not hook:
                    self._json({"ok": False, "error": "未配置飞书 webhook（设置页或环境变量）"}, 400)
                    return
                self._json({"ok": True, "push": SVC.push_feishu(hook, rep["payload"]["text"])})
            else:
                self._json({"ok": False, "error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._json({"ok": False, "error": str(exc),
                        "trace": traceback.format_exc(limit=3)}, 500)


def _num_or_none(v):
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _scheduler(stop: threading.Event):
    """16:30 收盘后自动评估 + 生成日报（仅工作日）。"""
    done = ""
    while not stop.is_set():
        try:
            s = store.get_settings()
            now = datetime.now()
            hm = now.strftime("%H:%M")
            if now.weekday() < 5 and hm >= str(s.get("push_time") or "16:30") and done != now.strftime("%Y-%m-%d"):
                SVC.evaluate_all()
                rep = SVC.build_report()
                done = now.strftime("%Y-%m-%d")
                print(f"[scheduler] {done} 日报已生成 {len(rep.get('signals') or [])} 条提示",
                      flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[scheduler] error: {exc}", flush=True)
        stop.wait(60)


def main():
    ap = argparse.ArgumentParser(description="仓位管家（规则引擎 + 看板）")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8781)
    ap.add_argument("--no-scheduler", action="store_true")
    args = ap.parse_args()
    try:
        seeded = SVC.seed_indices()
        if seeded.get("added"):
            print(f"[seed] 已添加常用指数: {', '.join(seeded['added'])}", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[seed] 初始化失败: {exc}", flush=True)
    stop = threading.Event()
    if not args.no_scheduler:
        threading.Thread(target=_scheduler, args=(stop,), daemon=True).start()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"仓位管家 → http://{args.host}:{args.port}  (Ctrl+C 退出)", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.server_close()


if __name__ == "__main__":
    main()
