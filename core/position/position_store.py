#!/usr/bin/env python3
"""仓位管家 · 存储层（SQLite）
=============================
- 价位带版本 + 确认门槛（候选不生效）
- 结构轮次、信号去抖、软删归档
- 核心表装触发器：禁止硬删（prompt/03 纪律 5）
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime
from typing import Any

from core.position.position_rules import DEFAULTS

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(ROOT_DIR, "data")
DB_FILE = os.getenv("POSITION_DB", os.path.join(DATA_DIR, "position.db"))

_LOCK = threading.RLock()
_CONN: sqlite3.Connection | None = None

LEVEL_TYPES = ("neckline", "measured_target", "second_target", "pre_breakout",
               "pullback_zone", "top_neckline", "reference")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS instruments(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  code TEXT NOT NULL UNIQUE,
  name TEXT NOT NULL DEFAULT '',
  type TEXT NOT NULL DEFAULT 'stock',           -- index/stock/etf
  market TEXT NOT NULL DEFAULT 'A',
  sector TEXT DEFAULT '',
  groups TEXT DEFAULT '[]',
  star INTEGER DEFAULT 0,
  quality_grade TEXT DEFAULT '',
  direction_label TEXT DEFAULT 'undecided',     -- right/base/left/undecided
  pool TEXT DEFAULT 'watch',                    -- hold/ready/pullback/watch/archived
  sub_status TEXT DEFAULT '',
  option_note TEXT DEFAULT '',
  note TEXT DEFAULT '',
  created_at TEXT, updated_at TEXT, deleted_at TEXT);

CREATE TABLE IF NOT EXISTS index_links(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  instrument_id INTEGER NOT NULL,
  index_code TEXT NOT NULL,
  is_primary INTEGER DEFAULT 0,
  UNIQUE(instrument_id, index_code));

CREATE TABLE IF NOT EXISTS price_levels(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  instrument_id INTEGER NOT NULL,
  level_type TEXT NOT NULL,
  price REAL, price_lower REAL,
  zone_mode TEXT, buyback_ratio REAL,
  version INTEGER DEFAULT 1,
  status TEXT DEFAULT 'candidate',              -- candidate/active/superseded
  superseded_by_id INTEGER,
  source TEXT DEFAULT 'manual',                 -- manual/drawing/vision/system
  confirmed_at TEXT, effective_from TEXT,
  round_no INTEGER DEFAULT 1,
  note TEXT DEFAULT '',
  created_at TEXT);

CREATE TABLE IF NOT EXISTS structure_rounds(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  instrument_id INTEGER NOT NULL,
  round_no INTEGER NOT NULL,
  neckline_level_id INTEGER, target_level_id INTEGER,
  phase TEXT DEFAULT 'pending',
  activated_at TEXT, peak_close REAL, peak_at TEXT, peak_completion REAL,
  updated_at TEXT,
  UNIQUE(instrument_id, round_no));

CREATE TABLE IF NOT EXISTS sentinels(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  instrument_id INTEGER NOT NULL,
  price REAL NOT NULL,
  direction TEXT DEFAULT 'up',                  -- up/down
  source_ref TEXT DEFAULT '',
  created_at TEXT, deleted_at TEXT);

CREATE TABLE IF NOT EXISTS daily_bars(
  code TEXT NOT NULL, trade_date TEXT NOT NULL,
  o REAL, h REAL, l REAL, c REAL, v REAL,
  source TEXT DEFAULT '',
  PRIMARY KEY(code, trade_date));

CREATE TABLE IF NOT EXISTS signals(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  instrument_id INTEGER NOT NULL,
  rule TEXT NOT NULL, priority TEXT NOT NULL,
  trade_date TEXT NOT NULL,
  state_key TEXT NOT NULL,
  message TEXT, payload TEXT,
  created_at TEXT, acked_at TEXT);

CREATE TABLE IF NOT EXISTS trigger_state(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  instrument_id INTEGER NOT NULL,
  rule TEXT NOT NULL, state_key TEXT NOT NULL,
  trade_date TEXT, updated_at TEXT,
  UNIQUE(instrument_id, rule, state_key));

CREATE TABLE IF NOT EXISTS campaigns(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  instrument_id INTEGER NOT NULL,
  round_no INTEGER DEFAULT 1,
  entry_date TEXT, entry_price REAL,
  position REAL DEFAULT 1.0,
  completed REAL DEFAULT 0.0,
  exits TEXT DEFAULT '[]',
  pnl_pct REAL, status TEXT DEFAULT 'open',
  created_at TEXT, updated_at TEXT);

CREATE TABLE IF NOT EXISTS daily_reports(
  trade_date TEXT PRIMARY KEY,
  payload TEXT NOT NULL,
  created_at TEXT);

CREATE TABLE IF NOT EXISTS archived_rows(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  table_name TEXT NOT NULL, row_json TEXT NOT NULL,
  archived_at TEXT);
"""

# 核心表禁止硬删（11 张核心表 → 这里装 7 张业务核心）
NO_DELETE_TABLES = ("instruments", "price_levels", "structure_rounds",
                    "sentinels", "signals", "campaigns", "daily_reports")


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def db() -> sqlite3.Connection:
    global _CONN
    with _LOCK:
        if _CONN is None:
            os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)
            _CONN = sqlite3.connect(DB_FILE, check_same_thread=False)
            _CONN.row_factory = sqlite3.Row
            _CONN.execute("PRAGMA journal_mode=WAL")
            _CONN.execute("PRAGMA foreign_keys=ON")
        return _CONN


def init_db() -> None:
    with _LOCK:
        conn = db()
        conn.executescript(SCHEMA)
        for t in NO_DELETE_TABLES:
            conn.execute(
                f"CREATE TRIGGER IF NOT EXISTS no_delete_{t} BEFORE DELETE ON {t} "
                f"BEGIN SELECT RAISE(ABORT, 'hard delete forbidden: {t}'); END")
        for k, v in DEFAULTS.items():
            conn.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)",
                         (k, json.dumps(v, ensure_ascii=False)))
        conn.commit()


# ---------------- settings ----------------
def get_settings() -> dict:
    with _LOCK:
        rows = db().execute("SELECT key,value FROM settings").fetchall()
    out = dict(DEFAULTS)
    for r in rows:
        try:
            out[r["key"]] = json.loads(r["value"])
        except Exception:
            out[r["key"]] = r["value"]
    return out


def save_settings(d: dict) -> dict:
    with _LOCK:
        conn = db()
        for k, v in (d or {}).items():
            if k not in DEFAULTS:
                continue
            conn.execute("INSERT INTO settings(key,value) VALUES(?,?) "
                         "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                         (k, json.dumps(v, ensure_ascii=False)))
        conn.commit()
    return get_settings()


# ---------------- instruments ----------------
def row_out(r: sqlite3.Row | dict | None) -> dict | None:
    if r is None:
        return None
    d = dict(r)
    for k in ("groups",):
        if isinstance(d.get(k), str):
            try:
                d[k] = json.loads(d[k])
            except Exception:
                d[k] = []
    return d


def add_instrument(code: str, name: str = "", **kw) -> dict:
    code = str(code).strip().upper()
    if not code:
        raise ValueError("code required")
    fields = {"code": code, "name": name or kw.get("name") or code}
    for k in ("type", "market", "sector", "star", "quality_grade", "direction_label",
              "pool", "sub_status", "option_note", "note"):
        if kw.get(k) is not None:
            fields[k] = kw[k]
    if kw.get("groups") is not None:
        fields["groups"] = json.dumps(kw["groups"], ensure_ascii=False)
    ts = now()
    fields["created_at"], fields["updated_at"] = ts, ts
    cols = ",".join(fields)
    qs = ",".join("?" for _ in fields)
    with _LOCK:
        conn = db()
        try:
            cur = conn.execute(f"INSERT INTO instruments({cols}) VALUES({qs})",
                               tuple(fields.values()))
        except sqlite3.IntegrityError:
            sets = ",".join(f"{k}=?" for k in fields if k != "created_at")
            vals = [v for k, v in fields.items() if k != "created_at"]
            conn.execute(f"UPDATE instruments SET {sets} WHERE code=?", (*vals, code))
            cur = conn.execute("SELECT id FROM instruments WHERE code=?", (code,))
        conn.commit()
        rid = cur.lastrowid or conn.execute("SELECT id FROM instruments WHERE code=?",
                                            (code,)).fetchone()[0]
    return get_instrument(rid)


def update_instrument(iid: int, **kw) -> dict:
    allowed = ("name", "type", "market", "sector", "star", "quality_grade",
               "direction_label", "pool", "sub_status", "option_note", "note", "groups")
    sets, vals = [], []
    for k in allowed:
        if k not in kw:
            continue
        v = kw[k]
        if k == "groups":
            v = json.dumps(v or [], ensure_ascii=False)
        sets.append(f"{k}=?")
        vals.append(v)
    if sets:
        sets.append("updated_at=?")
        vals.append(now())
        with _LOCK:
            db().execute(f"UPDATE instruments SET {','.join(sets)} WHERE id=?", (*vals, iid))
            db().commit()
    return get_instrument(iid)


def get_instrument(iid_or_code: int | str) -> dict | None:
    with _LOCK:
        if isinstance(iid_or_code, int) or str(iid_or_code).isdigit():
            r = db().execute("SELECT * FROM instruments WHERE id=?",
                             (int(iid_or_code),)).fetchone()
        else:
            r = db().execute("SELECT * FROM instruments WHERE code=?",
                             (str(iid_or_code).upper(),)).fetchone()
    return row_out(r)


def list_instruments(include_archived: bool = False, exclude_test: bool = True) -> list[dict]:
    q = "SELECT * FROM instruments WHERE deleted_at IS NULL"
    if not include_archived:
        q += " AND pool <> 'archived'"
    if exclude_test:
        q += " AND code NOT LIKE 'TEST%'"
    q += " ORDER BY CASE pool WHEN 'hold' THEN 0 WHEN 'pullback' THEN 1 WHEN 'ready' THEN 2 " \
         "WHEN 'watch' THEN 3 ELSE 4 END, star DESC, code"
    with _LOCK:
        rows = db().execute(q).fetchall()
    out = []
    for r in rows:
        d = row_out(r)
        d["levels"] = active_levels(d["id"])
        d["sentinels"] = list_sentinels(d["id"])
        d["links"] = list_index_links(d["id"])
        out.append(d)
    return out


def soft_delete_instrument(iid: int) -> None:
    with _LOCK:
        db().execute("UPDATE instruments SET deleted_at=? WHERE id=?", (now(), iid))
        db().commit()


# ---------------- index links ----------------
def list_index_links(iid: int) -> list[dict]:
    with _LOCK:
        rows = db().execute("SELECT * FROM index_links WHERE instrument_id=? "
                            "ORDER BY is_primary DESC", (iid,)).fetchall()
    return [dict(r) for r in rows]


def set_index_links(iid: int, links: list[dict]) -> list[dict]:
    """links=[{index_code,is_primary}]；保证有且仅有一个主指数。"""
    with _LOCK:
        conn = db()
        conn.execute("DELETE FROM index_links WHERE instrument_id=?", (iid,))
        prim_done = False
        for ln in links or []:
            code = str(ln.get("index_code") or "").strip().upper()
            if not code:
                continue
            prim = 1 if (ln.get("is_primary") and not prim_done) else 0
            prim_done = prim_done or bool(prim)
            conn.execute("INSERT OR IGNORE INTO index_links(instrument_id,index_code,is_primary) "
                         "VALUES(?,?,?)", (iid, code, prim))
        if not prim_done:
            r = conn.execute("SELECT id FROM index_links WHERE instrument_id=? LIMIT 1",
                             (iid,)).fetchone()
            if r:
                conn.execute("UPDATE index_links SET is_primary=1 WHERE id=?", (r[0],))
        conn.commit()
    return list_index_links(iid)


# ---------------- price levels ----------------
def active_levels(iid: int) -> dict:
    with _LOCK:
        rows = db().execute("SELECT * FROM price_levels WHERE instrument_id=? AND status='active' "
                            "AND confirmed_at IS NOT NULL ORDER BY version DESC", (iid,)).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        d = dict(r)
        t = d["level_type"]
        if t == "measured_target":
            t = "target"
        if t not in out:
            out[t] = d
    return out


def get_level(lid: int) -> dict | None:
    with _LOCK:
        r = db().execute("SELECT * FROM price_levels WHERE id=?", (lid,)).fetchone()
    return dict(r) if r else None


def add_level(iid: int, level_type: str, price: float | None,
              price_lower: float | None = None, zone_mode: str | None = None,
              buyback_ratio: float | None = None, source: str = "manual",
              confirm: bool = False, round_no: int = 1, note: str = "") -> dict:
    if level_type not in LEVEL_TYPES:
        raise ValueError("bad level_type")
    ts = now()
    with _LOCK:
        conn = db()
        prev = conn.execute(
            "SELECT * FROM price_levels WHERE instrument_id=? AND level_type=? "
            "AND status='active' ORDER BY version DESC LIMIT 1",
            (iid, level_type)).fetchone()
        ver = (prev["version"] + 1) if prev else 1
        cur = conn.execute(
            "INSERT INTO price_levels(instrument_id,level_type,price,price_lower,zone_mode,"
            "buyback_ratio,version,status,source,confirmed_at,effective_from,round_no,note,created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (iid, level_type, price, price_lower, zone_mode, buyback_ratio, ver,
             "active" if confirm else "candidate", source,
             ts if confirm else None, today() if confirm else None, round_no, note, ts))
        lid = cur.lastrowid
        if confirm and prev:
            conn.execute("UPDATE price_levels SET status='superseded', superseded_by_id=? "
                         "WHERE id=?", (lid, prev["id"]))
        conn.commit()
    return get_level(lid)


def confirm_level(lid: int, effective_from: str | None = None) -> dict:
    with _LOCK:
        conn = db()
        r = conn.execute("SELECT * FROM price_levels WHERE id=?", (lid,)).fetchone()
        if not r:
            raise ValueError("level not found")
        if r["confirmed_at"]:
            return dict(r)
        prev = conn.execute(
            "SELECT * FROM price_levels WHERE instrument_id=? AND level_type=? "
            "AND status='active' AND id<>?", (r["instrument_id"], r["level_type"], lid)).fetchone()
        ts = now()
        conn.execute("UPDATE price_levels SET status='active', confirmed_at=?, effective_from=?, "
                     "version=? WHERE id=?",
                     (ts, effective_from or today(),
                      (prev["version"] + 1) if prev else r["version"], lid))
        if prev:
            conn.execute("UPDATE price_levels SET status='superseded', superseded_by_id=? "
                         "WHERE id=?", (lid, prev["id"]))
        conn.commit()
    return get_level(lid)


def list_levels(iid: int, include_candidates: bool = True) -> list[dict]:
    q = "SELECT * FROM price_levels WHERE instrument_id=?"
    if not include_candidates:
        q += " AND confirmed_at IS NOT NULL"
    q += " ORDER BY level_type, version DESC"
    with _LOCK:
        rows = db().execute(q, (iid,)).fetchall()
    return [dict(r) for r in rows]


# ---------------- structure rounds ----------------
def get_round(iid: int, round_no: int = 1) -> dict | None:
    with _LOCK:
        r = db().execute("SELECT * FROM structure_rounds WHERE instrument_id=? AND round_no=?",
                         (iid, round_no)).fetchone()
    return dict(r) if r else None


def upsert_round(iid: int, st: dict, round_no: int = 1,
                 neckline_id: int | None = None, target_id: int | None = None) -> dict:
    ts = now()
    with _LOCK:
        conn = db()
        conn.execute(
            "INSERT INTO structure_rounds(instrument_id,round_no,neckline_level_id,"
            "target_level_id,phase,activated_at,peak_close,peak_at,peak_completion,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(instrument_id,round_no) DO UPDATE SET "
            "phase=excluded.phase, activated_at=excluded.activated_at, "
            "peak_close=excluded.peak_close, peak_at=excluded.peak_at, "
            "peak_completion=excluded.peak_completion, updated_at=excluded.updated_at",
            (iid, round_no, neckline_id, target_id, st.get("phase") or "pending",
             st.get("activated_at"), st.get("peak_close"), st.get("peak_at"),
             st.get("peak_completion"), ts))
        conn.commit()
    return get_round(iid, round_no)


# ---------------- sentinels ----------------
def list_sentinels(iid: int) -> list[dict]:
    with _LOCK:
        rows = db().execute("SELECT * FROM sentinels WHERE instrument_id=? AND deleted_at IS NULL "
                            "ORDER BY price", (iid,)).fetchall()
    return [dict(r) for r in rows]


def add_sentinel(iid: int, price: float, direction: str = "up",
                 source_ref: str = "") -> dict:
    if len(list_sentinels(iid)) >= 2:
        raise ValueError("每个标的最多 2 条哨兵")
    with _LOCK:
        cur = db().execute("INSERT INTO sentinels(instrument_id,price,direction,source_ref,created_at)"
                           " VALUES(?,?,?,?,?)", (iid, float(price), direction, source_ref, now()))
        db().commit()
        r = db().execute("SELECT * FROM sentinels WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(r)


def delete_sentinel(sid: int) -> None:
    with _LOCK:
        db().execute("UPDATE sentinels SET deleted_at=? WHERE id=?", (now(), sid))
        db().commit()


# ---------------- bars ----------------
def put_bars(code: str, bars: list[dict], source: str = "") -> int:
    if not bars:
        return 0
    rows = [(str(code).upper(), str(b["t"])[:10], b.get("o"), b.get("h"), b.get("l"),
             b.get("c"), b.get("v"), source) for b in bars]
    with _LOCK:
        db().executemany(
            "INSERT INTO daily_bars(code,trade_date,o,h,l,c,v,source) VALUES(?,?,?,?,?,?,?,?) "
            "ON CONFLICT(code,trade_date) DO UPDATE SET o=excluded.o,h=excluded.h,l=excluded.l,"
            "c=excluded.c,v=excluded.v,source=excluded.source", rows)
        db().commit()
    return len(rows)


def get_bars(code: str, limit: int = 800) -> list[dict]:
    with _LOCK:
        rows = db().execute(
            "SELECT trade_date AS t,o,h,l,c,v FROM daily_bars WHERE code=? "
            "ORDER BY trade_date DESC LIMIT ?", (str(code).upper(), limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


# ---------------- signals ----------------
def record_signals(iid: int, sigs: list[dict]) -> list[dict]:
    """按 (instrument, rule, state_key) 去抖；价位改版/状态迁移会换 state_key。"""
    fresh = []
    ts = now()
    with _LOCK:
        conn = db()
        for s in sigs:
            key = (iid, s["rule"], s["state_key"])
            row = conn.execute("SELECT id,trade_date FROM trigger_state WHERE instrument_id=? "
                               "AND rule=? AND state_key=?", key).fetchone()
            if row:
                # 永久去抖：同 state_key 只提示一次；改版/换战役会换 key 重新武装
                continue
            conn.execute("INSERT INTO trigger_state(instrument_id,rule,state_key,trade_date,"
                         "updated_at) VALUES(?,?,?,?,?)",
                         (*key, s["trade_date"], ts))
            cur = conn.execute(
                "INSERT INTO signals(instrument_id,rule,priority,trade_date,state_key,message,"
                "payload,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (iid, s["rule"], s["priority"], s["trade_date"], s["state_key"],
                 s.get("message"), json.dumps(s.get("payload") or {}, ensure_ascii=False), ts))
            s2 = dict(s)
            s2["id"] = cur.lastrowid
            fresh.append(s2)
        conn.commit()
    return fresh


def list_signals(trade_date: str | None = None, days: int = 3,
                 unacked_only: bool = False, limit: int = 300) -> list[dict]:
    q = ("SELECT s.*, i.code, i.name FROM signals s JOIN instruments i ON i.id=s.instrument_id "
         "WHERE 1=1")
    args: list[Any] = []
    if trade_date:
        q += " AND s.trade_date=?"
        args.append(trade_date)
    if unacked_only:
        q += " AND s.acked_at IS NULL"
    q += " ORDER BY CASE s.priority WHEN 'P1' THEN 0 WHEN 'P2' THEN 1 WHEN 'P3' THEN 2 ELSE 3 END," \
         " s.trade_date DESC, s.id DESC LIMIT ?"
    args.append(limit)
    with _LOCK:
        rows = db().execute(q, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["payload"] = json.loads(d.get("payload") or "{}")
        except Exception:
            d["payload"] = {}
        out.append(d)
    return out


def ack_signal(sid: int) -> None:
    with _LOCK:
        db().execute("UPDATE signals SET acked_at=? WHERE id=?", (now(), sid))
        db().commit()


# ---------------- campaigns ----------------
def open_campaign(iid: int, entry_date: str, entry_price: float,
                  position: float = 1.0, round_no: int = 1) -> dict:
    with _LOCK:
        conn = db()
        row = conn.execute("SELECT * FROM campaigns WHERE instrument_id=? AND status='open'",
                           (iid,)).fetchone()
        if row:
            return dict(row)
        cur = conn.execute("INSERT INTO campaigns(instrument_id,round_no,entry_date,entry_price,"
                           "position,completed,exits,pnl_pct,status,created_at,updated_at) "
                           "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (iid, round_no, entry_date, entry_price, position, 0.0, "[]", None,
                            "open", now(), now()))
        conn.commit()
        r = conn.execute("SELECT * FROM campaigns WHERE id=?", (cur.lastrowid,)).fetchone()
    return dict(r)


def update_campaign(cid: int, position: float | None = None, completed: float | None = None,
                    exit_event: dict | None = None, close_it: bool = False,
                    pnl_pct: float | None = None) -> dict:
    with _LOCK:
        conn = db()
        r = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
        if not r:
            raise ValueError("campaign not found")
        exits = json.loads(r["exits"] or "[]")
        if exit_event:
            exits.append(exit_event)
        pos = r["position"] if position is None else position
        comp = r["completed"] if completed is None else completed
        status = "closed" if (close_it or pos <= 1e-6) else r["status"]
        conn.execute("UPDATE campaigns SET position=?,completed=?,exits=?,pnl_pct=?,status=?,"
                     "updated_at=? WHERE id=?",
                     (pos, comp, json.dumps(exits, ensure_ascii=False),
                      pnl_pct if pnl_pct is not None else r["pnl_pct"], status, now(), cid))
        conn.commit()
        r2 = conn.execute("SELECT * FROM campaigns WHERE id=?", (cid,)).fetchone()
    d = dict(r2)
    d["exits"] = json.loads(d.get("exits") or "[]")
    return d


def list_campaigns(iid: int | None = None) -> list[dict]:
    q = "SELECT c.*, i.code, i.name FROM campaigns c JOIN instruments i ON i.id=c.instrument_id"
    args: list[Any] = []
    if iid:
        q += " WHERE c.instrument_id=?"
        args.append(iid)
    q += " ORDER BY c.entry_date DESC, c.id DESC"
    with _LOCK:
        rows = db().execute(q, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["exits"] = json.loads(d.get("exits") or "[]")
        except Exception:
            d["exits"] = []
        out.append(d)
    return out


def open_campaign_for(iid: int) -> dict | None:
    with _LOCK:
        r = db().execute("SELECT * FROM campaigns WHERE instrument_id=? AND status='open' "
                         "ORDER BY id DESC LIMIT 1", (iid,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["exits"] = json.loads(d.get("exits") or "[]")
    return d


# ---------------- reports ----------------
def save_report(trade_date: str, payload: dict) -> None:
    with _LOCK:
        db().execute("INSERT INTO daily_reports(trade_date,payload,created_at) VALUES(?,?,?) "
                     "ON CONFLICT(trade_date) DO UPDATE SET payload=excluded.payload,"
                     "created_at=excluded.created_at",
                     (trade_date, json.dumps(payload, ensure_ascii=False), now()))
        db().commit()


def get_report(trade_date: str) -> dict | None:
    with _LOCK:
        r = db().execute("SELECT * FROM daily_reports WHERE trade_date=?", (trade_date,)).fetchone()
    if not r:
        return None
    d = dict(r)
    try:
        d["payload"] = json.loads(d["payload"])
    except Exception:
        d["payload"] = {}
    return d


def stats() -> dict:
    with _LOCK:
        conn = db()
        n = lambda t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]  # noqa: E731
        return {"instruments": n("instruments"), "levels": n("price_levels"),
                "signals": n("signals"), "sentinels": n("sentinels"),
                "bars": n("daily_bars"), "campaigns": n("campaigns")}
