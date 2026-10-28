#!/usr/bin/env python3
"""仓位管家 · 存储层单测（python -m unittest tests.test_position_store -v）

使用独立临时库，不碰 data/position.db。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix="position_store_test_")
os.environ["POSITION_DB"] = os.path.join(_TMP, "test.db")

from core.position import position_store as store  # noqa: E402


def tearDownModule():
    try:
        if store._CONN is not None:
            store._CONN.close()
            store._CONN = None
    except Exception:
        pass
    for f in os.listdir(_TMP):
        try:
            os.remove(os.path.join(_TMP, f))
        except OSError:
            pass
    try:
        os.rmdir(_TMP)
    except OSError:
        pass


class TestStore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        store.init_db()

    def test_settings_roundtrip(self):
        s = store.get_settings()
        self.assertIn("structure_realized", s)
        store.save_settings({"structure_realized": 0.88, "unknown_key": 1})
        self.assertEqual(store.get_settings()["structure_realized"], 0.88)
        self.assertNotIn("unknown_key", store.get_settings())

    def test_level_confirm_and_version(self):
        inst = store.add_instrument("TEST001", "沙箱一", type="stock")
        iid = inst["id"]
        cand = store.add_level(iid, "neckline", 10.0, confirm=False)
        self.assertEqual(cand["status"], "candidate")
        self.assertEqual(store.active_levels(iid), {})

        c1 = store.confirm_level(cand["id"])
        self.assertEqual(c1["status"], "active")
        self.assertEqual(c1["version"], 1)
        self.assertEqual(store.active_levels(iid)["neckline"]["price"], 10.0)

        c2 = store.add_level(iid, "neckline", 12.0, confirm=True)
        self.assertEqual(c2["version"], 2)
        self.assertEqual(store.active_levels(iid)["neckline"]["price"], 12.0)
        self.assertEqual(store.get_level(cand["id"])["status"], "superseded")

    def test_sentinel_limit_and_soft_delete(self):
        inst = store.add_instrument("TEST002", "沙箱二")
        iid = inst["id"]
        store.add_sentinel(iid, 10.0)
        store.add_sentinel(iid, 12.0)
        with self.assertRaises(ValueError):
            store.add_sentinel(iid, 14.0)
        sid = store.list_sentinels(iid)[0]["id"]
        store.delete_sentinel(sid)
        self.assertEqual(len(store.list_sentinels(iid)), 1)
        store.add_sentinel(iid, 14.0)  # 软删后可以补一条

    def test_no_hard_delete(self):
        inst = store.add_instrument("TEST003", "沙箱三")
        with self.assertRaises(sqlite3.IntegrityError):
            store.db().execute("DELETE FROM instruments WHERE id=?", (inst["id"],))
            store.db().commit()

    def test_signal_dedupe(self):
        inst = store.add_instrument("TEST004", "沙箱四")
        sig = {"rule": "entry", "priority": "P3", "trade_date": "2026-10-22",
               "state_key": "entry|1", "message": "test", "payload": {"close": 1}}
        first = store.record_signals(inst["id"], [sig])
        self.assertEqual(len(first), 1)
        second = store.record_signals(inst["id"], [sig])
        self.assertEqual(second, [])
        # 改版（state_key 变化）后重新武装
        sig2 = dict(sig, state_key="entry|2")
        self.assertEqual(len(store.record_signals(inst["id"], [sig2])), 1)

    def test_soft_delete_instrument(self):
        inst = store.add_instrument("TEST005", "沙箱五")
        store.soft_delete_instrument(inst["id"])
        # 软删后按 code 仍能查到，但 deleted_at 已写
        self.assertIsNotNone(store.get_instrument("TEST005")["deleted_at"])
        # 默认列表排除 TEST*
        self.assertNotIn("TEST005", [x["code"] for x in store.list_instruments()])

    def test_test_instruments_filtered(self):
        store.add_instrument("TEST006", "沙箱六")
        codes = [x["code"] for x in store.list_instruments()]
        self.assertNotIn("TEST006", codes)
        self.assertIn("TEST006", [x["code"] for x in store.list_instruments(exclude_test=False)])


if __name__ == "__main__":
    unittest.main(verbosity=2)
