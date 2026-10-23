#!/usr/bin/env python3
"""仓位管家 · 规则引擎单测（python -m unittest tests.test_position_rules -v）"""
from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.position_rules import (  # noqa: E402
    DEFAULTS, campaign_pnl, completion, evaluate, structure_state,
)


def bars_from(closes, start=0, vol=1000.0):
    out = []
    for i, c in enumerate(closes):
        d = f"2026-{(i // 28) + 1:02d}-{(i % 28) + 1:02d}"
        out.append({"t": d, "o": c * 0.99, "h": c * 1.01, "l": c * 0.98,
                    "c": c, "v": vol + (i + start) * 10})
    return out


BASE_LEVELS = {"neckline": {"price": 100.0, "version": 1},
               "target": {"price": 140.0, "version": 1}}


class TestStructure(unittest.TestCase):
    def test_lifecycle(self):
        closes = [98, 99, 101, 110, 125, 127, 126, 126]   # 100 颈线 140 目标
        st = structure_state(100, 140, bars_from(closes))
        # 峰 127 → 完成度 67.5%，只到 active
        self.assertEqual(st["phase"], "active")
        self.assertAlmostEqual(st["completion"], (126 - 100) / 40)

        closes = [98, 101, 120, 130, 137, 138, 139]      # 峰 139 → 97.5% → 已兑现
        st = structure_state(100, 140, bars_from(closes))
        self.assertEqual(st["phase"], "realized")

        closes = [98, 101, 120, 130, 137, 138, 139, 120] # 峰 139 → 回撤 13.7%
        st = structure_state(100, 140, bars_from(closes))
        self.assertEqual(st["phase"], "realized")
        closes.append(118)                                # 回撤 15.1% → 已衰竭
        st = structure_state(100, 140, bars_from(closes))
        self.assertEqual(st["phase"], "exhausted")
        self.assertEqual(st["peak_close"], 139.0)

    def test_replay_start_guard(self):
        """回放护栏：价位生效日之前的高点不能被扫进峰值。"""
        closes = [500, 510, 99, 101, 105]
        bars = bars_from(closes)
        st = structure_state(100, 140, bars, start_idx=2)
        self.assertEqual(st["activated_at"], bars[3]["t"])
        self.assertEqual(st["peak_close"], 105.0)

    def test_completion(self):
        self.assertAlmostEqual(completion(120, 100, 140), 0.5)
        self.assertEqual(completion(120, 100, 100), 0.0)


class TestEvaluate(unittest.TestCase):
    def test_label_suppression(self):
        bars = bars_from([98, 101, 110, 120])            # 已突破并上涨
        for label in ("left", "base"):
            inst = {"code": "T", "direction_label": label, "pool": "watch"}
            sigs = evaluate(inst, bars, BASE_LEVELS, None,
                            {"label": "right", "completion": 0.95}, [], DEFAULTS, None)
            rules = {s["rule"] for s in sigs}
            self.assertFalse(rules & {"entry", "trim", "trail", "fake_break"}, rules)
        # 右侧正常产出入场
        inst = {"code": "T", "direction_label": "right", "pool": "ready"}
        sigs = evaluate(inst, bars, BASE_LEVELS, None,
                        {"label": "right", "completion": 0.40}, [], DEFAULTS, None)
        entry = [s for s in sigs if s["rule"] == "entry"]
        self.assertEqual(len(entry), 1)
        self.assertEqual(entry[0]["payload"]["position"], 1.0)
        self.assertFalse(entry[0]["payload"]["half"])

    def test_entry_gate_and_late(self):
        bars = bars_from([98, 101, 102])
        inst = {"direction_label": "right", "pool": "ready"}
        # 指数非右侧 → 只给等待
        sigs = evaluate(inst, bars, BASE_LEVELS, None,
                        {"label": "base", "completion": 0.1}, [], DEFAULTS, None)
        rules = [s["rule"] for s in sigs]
        self.assertIn("entry_wait", rules)
        self.assertNotIn("entry", rules)
        # 指数右侧但完成度 95% → 半仓
        sigs = evaluate(inst, bars, BASE_LEVELS, None,
                        {"label": "right", "completion": 0.95}, [], DEFAULTS, None)
        entry = [s for s in sigs if s["rule"] == "entry"][0]
        self.assertTrue(entry["payload"]["half"])
        self.assertAlmostEqual(entry["payload"]["position"], 0.5)

    def test_fake_break_and_trailing(self):
        levels = {"neckline": {"price": 100.0, "version": 1},
                  "target": {"price": 140.0, "version": 1}}
        # 收盘 94 < 100*0.95 → 假突破清仓
        bars = bars_from([101, 100, 99, 94])
        sigs = evaluate({"direction_label": "right", "pool": "hold"}, bars, levels, None,
                        {"label": "right", "completion": 0.5}, [], DEFAULTS,
                        {"entry_price": 101, "entry_date": "2026-01-01",
                         "position": 1.0, "completed": 0.5})
        self.assertIn("fake_break", [s["rule"] for s in sigs])

        # 峰值 130（完成度 75%）后回撤 20% → trail
        bars = bars_from([101, 110, 130, 104])
        st = structure_state(100, 140, bars)
        sigs = evaluate({"direction_label": "right", "pool": "hold"}, bars, levels, st,
                        {"label": "right", "completion": 0.6}, [], DEFAULTS,
                        {"entry_price": 101, "entry_date": "2026-01-01",
                         "position": 1.0, "completed": 0.75})
        self.assertIn("trail", [s["rule"] for s in sigs])

    def test_ladder_and_final(self):
        levels = {"neckline": {"price": 100.0, "version": 1},
                  "target": {"price": 140.0, "version": 1}}
        bars = bars_from([101, 110, 128, 129])          # 完成度 72.5%
        sigs = evaluate({"direction_label": "right", "pool": "hold"}, bars, levels, None,
                        {"label": "right", "completion": 0.2}, [], DEFAULTS,
                        {"entry_price": 101, "entry_date": "2026-01-01",
                         "position": 1.0, "completed": 0.60})
        trims = [s for s in sigs if s["rule"] == "trim"]
        self.assertEqual({t["payload"]["stage"] for t in trims}, {0.70})
        self.assertEqual(trims[0]["payload"]["remain"], 0.70)

        # 一天直接跨到 92%：只提示最高一档（90%），不连发三档
        bars = bars_from([101, 120, 137])
        sigs = evaluate({"direction_label": "right", "pool": "hold"}, bars, levels, None,
                        {"label": "right", "completion": 0.2}, [], DEFAULTS,
                        {"entry_price": 101, "entry_date": "2026-01-01",
                         "position": 1.0, "completed": 0.60})
        trims = [s for s in sigs if s["rule"] == "trim"]
        self.assertEqual(len(trims), 1)
        self.assertEqual(trims[0]["payload"]["stage"], 0.90)

        bars = bars_from([101, 120, 141])               # 完成度 102.5%
        sigs = evaluate({"direction_label": "right", "pool": "hold"}, bars, levels, None,
                        {"label": "right", "completion": 0.95, "phase": "realized"},
                        [], DEFAULTS,
                        {"entry_price": 101, "entry_date": "2026-01-01",
                         "position": 1.0, "completed": 1.0})
        fin = [s for s in sigs if s["rule"] == "final_trim"][0]
        self.assertEqual(fin["payload"]["remain"], 0.10)   # 指数 ≥90% → 剩 10%

    def test_sentinel_and_pullback(self):
        bars = bars_from([101, 105, 106])
        sigs = evaluate({"direction_label": "left", "pool": "watch"}, bars,
                        {"neckline": {"price": 120, "version": 1},
                         "target": {"price": 150, "version": 1}},
                        None, None, [{"id": 7, "price": 107.0, "direction": "up"}],
                        DEFAULTS, None)
        near = [s for s in sigs if s["rule"] == "sentinel_near"]
        self.assertEqual(len(near), 1)
        self.assertAlmostEqual(near[0]["payload"]["distance"], abs(106 / 107 - 1))

        levels = {"neckline": {"price": 100, "version": 1},
                  "target": {"price": 140, "version": 1},
                  "pullback": {"price": 118, "price_lower": 112, "zone_mode": "buyback",
                               "buyback_ratio": 3, "status": "active"}}
        bars = bars_from([130, 120, 115, 114])
        sigs = evaluate({"direction_label": "right", "pool": "watch"}, bars, levels,
                        None, {"label": "right", "completion": 0.5}, [], DEFAULTS, None)
        self.assertIn("pullback", [s["rule"] for s in sigs])
        # 跌破下沿×0.95=106.4 → 作废
        bars = bars_from([130, 120, 110, 105])
        sigs = evaluate({"direction_label": "right", "pool": "watch"}, bars, levels,
                        None, {"label": "right", "completion": 0.5}, [], DEFAULTS, None)
        self.assertIn("pullback_invalid", [s["rule"] for s in sigs])

    def test_base_candidates(self):
        # 缩量后放量阳线
        vols = [100] * 20 + [60] * 10 + [200]
        closes = [100] * 30 + [103]
        bars = []
        for i, (c, v) in enumerate(zip(closes, vols)):
            bars.append({"t": f"2026-01-{i + 1:02d}", "o": c * 0.99, "h": c * 1.01,
                         "l": c * 0.99, "c": c, "v": v})
        inst = {"direction_label": "base", "pool": "watch"}
        sigs = evaluate(inst, bars, {"neckline": {"price": 110, "version": 1},
                                     "target": {"price": 140, "version": 1}},
                        None, None, [], DEFAULTS, None)
        bases = [s for s in sigs if s["rule"] == "base_candidate"]
        self.assertTrue(any("放量" in b["payload"]["basis"] or "缩量" in b["payload"]["basis"]
                            for b in bases), bases)


class TestCampaign(unittest.TestCase):
    def test_pnl(self):
        # 满仓 100 入场；130 减到 70%；140 清仓
        r = campaign_pnl(100, [{"date": "d1", "remain": 0.70, "price": 130},
                               {"date": "d2", "remain": 0.0, "price": 140}])
        self.assertAlmostEqual(r["pnl_pct"], (0.3 * 0.3 + 0.7 * 0.4) * 100, places=3)
        self.assertEqual(r["open_pos"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
