#!/usr/bin/env python3
"""仓位管家 · 规则引擎（纯函数）
=================================
对应 prompt/01-系统蓝图.md 第六节、prompt/02-交易方法论.md。

铁律：
  ① 行情判断归人（方向标签由人设定，压制一切自动判断）
  ② 价位必须确认才入库（本模块只读已确认价位）
  ③ 只用日线收盘价（输入 bars 全部是日线）
  ④ 质地评分不进入任何信号
  ⑤ 不下单（只产出 Signal）

evaluate() 不依赖当前时间、不读写数据库，可直接用于历史回放与单测。
"""
from __future__ import annotations

from typing import Any

# ---------------- 默认参数（02 册 · 参数总表） ----------------
DEFAULTS: dict[str, Any] = {
    # 结构生命周期
    "structure_realized": 0.90,
    "structure_exhaust": 0.15,
    "invalidation_k": 0.95,
    # 入场
    "index_late": 0.90,
    "late_position": 0.50,
    "confirm_bars": 1,
    # 阶梯减仓：完成度 -> 剩余仓位
    "ladder": [[0.70, 0.70], [0.80, 0.50], [0.90, 0.25]],
    # 触及目标时的终减（按主指数完成度）
    "final_idx_ge90": 0.10,
    "final_idx_70_90": 0.15,
    "final_idx_lt70": 0.20,
    "final_idx_exhaust": 0.10,
    # 离场
    "trail_drawdown": 0.20,
    "trail_enable": 0.70,
    "fake_break_k": 0.95,
    # 回撤 / 候场
    "zone_prefill_ratio": 1.10,
    "buyback_invalid_k": 0.95,
    # 筑底判据
    "base_shrink_n": 10,
    "base_shrink_m": 20,
    "base_shrink_x": 0.30,
    "base_vol_k": 2.0,
    "base_rebound": 0.05,
    # 提醒
    "report_band2": 0.03,
    "report_band3": 0.08,
    "sentinel_near": 0.05,
    "sentinel_far": 0.15,
    "sentinel_push": 0.03,
    "peak_window": 250,
    "future_space": 30,
    "leverage": 1.0,
    # 通知 / 调度
    "push_time": "16:30",
    "push_enabled": True,
    "feishu_webhook": "",
    # 首次启动是否已灌入常用指数（内部标记）
    "_seeded": False,
}

PRIORITY = {
    "fake_break": "P1", "top_break": "P1", "trail": "P1",
    "trim": "P2", "final_trim": "P2", "target_watch": "P2",
    "entry": "P3", "entry_wait": "P3",
    "pullback": "P3", "pullback_invalid": "P3",
    "left_to_base": "P4", "base_candidate": "P4",
    "sentinel_cross": "P2", "sentinel_near": "P2", "sentinel_far": "P4",
}


def pget(settings: dict | None, key: str):
    v = (settings or {}).get(key)
    return DEFAULTS[key] if v is None else v


def completion(close: float, neckline: float, target: float) -> float:
    """完成度 = (现价 - 颈线) / (目标 - 颈线)。颈线=目标时返回 0，避免除零。"""
    if neckline is None or target is None:
        return float("nan")
    span = float(target) - float(neckline)
    if abs(span) < 1e-9:
        return 0.0
    return (float(close) - float(neckline)) / span


def structure_state(neckline: float, target: float, bars: list[dict],
                    start_idx: int = 0, settings: dict | None = None) -> dict:
    """结构生命周期（纯数学，收盘价判定）。

    待突破 → 首个收盘 > 颈线 → 进行中 → 峰值完成度 >= 90% → 已兑现
    → 兑现后距峰值回撤 >= 15% → 已衰竭（终态）

    bars: 升序日线 [{t,o,h,l,c,v}]；start_idx: 价位生效日所在下标（回放护栏）。
    """
    realized_th = float(pget(settings, "structure_realized"))
    exhaust_th = float(pget(settings, "structure_exhaust"))
    phase = "pending"
    activated_at = None
    peak = None
    peak_at = None
    peak_completion = None
    completion_now = float("nan")
    start = max(0, min(int(start_idx), max(len(bars) - 1, 0)))
    for i in range(start, len(bars)):
        c = float(bars[i]["c"])
        cp = completion(c, neckline, target)
        completion_now = cp
        if phase == "pending":
            if c > float(neckline):
                phase = "active"
                activated_at = bars[i]["t"]
                peak, peak_at, peak_completion = c, bars[i]["t"], cp
            continue
        if phase in ("active", "realized") and (peak is None or c > peak):
            peak, peak_at = c, bars[i]["t"]
            peak_completion = completion(c, neckline, target) if peak_completion is not None else cp
        if phase == "active" and peak_completion is not None and peak_completion >= realized_th:
            phase = "realized"
        if phase == "realized" and peak and c <= peak * (1 - exhaust_th):
            phase = "exhausted"
    return {
        "phase": phase,
        "activated_at": activated_at,
        "peak_close": peak,
        "peak_at": peak_at,
        "peak_completion": peak_completion,
        "completion": completion_now,
    }


def _sig(rule: str, trade_date: str, state_key: str, message: str, **payload) -> dict:
    return {"rule": rule, "priority": PRIORITY.get(rule, "P4"),
            "trade_date": trade_date, "state_key": state_key,
            "message": message, "payload": payload}


def _right_bars_before(bars: list[dict], idx: int, close: float, neckline: float,
                       n: int) -> bool:
    """连续 n 根收盘都站在颈线上方（含当前）。"""
    if n <= 1:
        return close > neckline
    lo = max(0, idx - n + 1)
    return all(float(b["c"]) > neckline for b in bars[lo:idx + 1])


def _base_candidates(bars: list[dict], settings: dict | None) -> dict:
    """筑底三判据（只给候选，不自动转右侧）。返回 {name: (hit, desc)}"""
    out: dict[str, tuple[bool, str]] = {}
    n = len(bars)
    if n < 3:
        return out
    today = bars[-1]
    # ② 缩量企稳后放量阳线（均量不含当日）
    sn = int(pget(settings, "base_shrink_n"))
    sm = int(pget(settings, "base_shrink_m"))
    sx = float(pget(settings, "base_shrink_x"))
    vk = float(pget(settings, "base_vol_k"))
    if n >= sn + sm + 1:
        recent = [float(b.get("v") or 0) for b in bars[-sn - 1:-1]]
        before = [float(b.get("v") or 0) for b in bars[-sn - sm - 1:-sn - 1]]
        if recent and before and sum(before) > 0:
            ma_recent = sum(recent) / len(recent)
            ma_before = sum(before) / len(before)
            shrink = ma_recent <= ma_before * (1 - sx)
            bull = float(today["c"]) > float(today["o"])
            vol_up = ma_recent > 0 and float(today.get("v") or 0) >= ma_recent * vk
            out["volume"] = (bool(shrink and bull and vol_up),
                             f"缩量 {ma_recent / ma_before:.0%} 后放量阳线")
    # ③ 二次探底不破前低
    look = bars[-min(n, 60):]
    lo = min(float(b["c"]) for b in look)
    lo_idx = max(i for i, b in enumerate(look) if float(b["c"]) == lo)
    after = look[lo_idx:]
    rebound = (max(float(b["c"]) for b in after) / lo - 1) if after and lo > 0 else 0
    today_ok = float(today["c"]) >= lo * 0.999
    out["double_bottom"] = (bool(rebound >= float(pget(settings, "base_rebound")) and today_ok),
                            f"前低 {lo:.2f} 反弹 {rebound:.0%} 未破")
    return out


def evaluate(inst: dict, bars: list[dict], levels: dict, round_: dict | None,
             index_state: dict | None, sentinels: list[dict],
             settings: dict | None = None, holding: dict | None = None,
             start_idx: int = 0) -> list[dict]:
    """评估单个标的一天。调用方负责先按日期切片 bars（含当天）。

    inst: {code,name,type,direction_label,pool,sub_status}
    levels: {neckline:{price,version,effective_from}, target:{price,...},
             top_neckline:{price}, pullback:{price,price_lower,zone_mode,buyback_ratio,
                                              status}, pre_breakout:{price}}
    round_: structure_state() 的结果（可为 None → 现算）
    index_state: {label:'right'|'base'|'left'|'undecided', completion, phase}
    holding: {entry_price, entry_date, position, completed:0.0}  # position=当前剩余仓位
    """
    if not bars:
        return []
    today = bars[-1]
    close = float(today["c"])
    date = str(today["t"])[:10]
    label = str(inst.get("direction_label") or "undecided")
    pool = str(inst.get("pool") or "watch")
    sub = str(inst.get("sub_status") or "")
    out: list[dict] = []

    neck = (levels or {}).get("neckline") or {}
    tgt = (levels or {}).get("target") or {}
    top = (levels or {}).get("top_neckline") or {}
    pull = (levels or {}).get("pullback") or {}
    pre = (levels or {}).get("pre_breakout") or {}
    nk = float(neck["price"]) if neck.get("price") is not None else None
    tg = float(tgt["price"]) if tgt.get("price") is not None else None
    nver = neck.get("version") or 0
    st = round_ or (structure_state(nk, tg, bars, start_idx, settings)
                    if (nk is not None and tg is not None) else {"phase": "none", "completion": float("nan")})
    cp = st.get("completion")
    peak = st.get("peak_close")
    idx_lbl = (index_state or {}).get("label") or "undecided"
    idx_cp = (index_state or {}).get("completion")
    idx_phase = (index_state or {}).get("phase")

    # ---------- 0. 方向标签压制：左侧 / 筑底 ----------
    suppressed = label in ("left", "base")
    if suppressed:
        # 8. 左侧 → 筑底：收盘进入起涨区（pattern 回撤区）或触及哨兵
        zone = pull if (pull.get("zone_mode") == "pattern") else None
        if label == "left" and zone and zone.get("price") is not None:
            lo = float(zone.get("price_lower") if zone.get("price_lower") is not None else zone["price"])
            hi = float(zone["price"])
            if lo <= close <= hi:
                out.append(_sig("left_to_base", date, f"left_to_base|{pool}|v{nver}",
                                f"收盘 {close:.2f} 进入起涨区 {lo:.2f}~{hi:.2f}，自动转筑底",
                                zone=[lo, hi], close=close))
        # 9. 筑底三判据（命中同一判据只提示一次，由去抖表控制）
        if label in ("base", "left") and nk is not None:
            cand = _base_candidates(bars, settings)
            hit_bars = 1
            if nk is not None and close > nk and _right_bars_before(
                    bars, len(bars) - 1, close, nk, max(1, int(pget(settings, "confirm_bars")))):
                out.append(_sig("base_candidate", date, f"base|neck|v{nver}",
                                f"收盘 {close:.2f} 站上颈线 {nk:.2f}，疑似筑底完成，请复核",
                                basis="站上颈线", close=close, neckline=nk,
                                target=tg, completion=completion(close, nk, tg) if tg else None))
            for key, (hit, desc) in cand.items():
                if hit:
                    out.append(_sig("base_candidate", date, f"base|{key}",
                                    f"{desc}，疑似筑底完成，请复核",
                                    basis=desc, close=close, neckline=nk, target=tg,
                                    completion=completion(close, nk, tg) if tg else None))
        # 哨兵照常（第 10 步）
        out.extend(_sentinel_signals(sentinels, close, date, settings, pool))
        return out

    # ---------- 1. 假突破清仓（仅持有） ----------
    if holding and nk is not None:
        inv = nk * float(pget(settings, "invalidation_k"))
        if close < inv:
            out.append(_sig("fake_break", date, f"fake_break|{pool}|v{nver}",
                            f"收盘 {close:.2f} < 失效线 {inv:.2f}（颈线×{pget(settings, 'invalidation_k')}），清仓",
                            close=close, invalidation=inv, neckline=nk))

    # ---------- 2. 头部颈线跌破 ----------
    if top.get("price") is not None:
        tp = float(top["price"])
        if close < tp:
            out.append(_sig("top_break", date, f"top_break|{pool}|v{nver}",
                            f"收盘 {close:.2f} 跌破头部颈线 {tp:.2f}，确认清仓？",
                            close=close, top_neckline=tp))

    # ---------- 3. 移动止盈（P1） ----------
    if holding and peak and cp is not None and cp == cp:
        ever = max(float(holding.get("completed") or 0), cp)
        dd = float(pget(settings, "trail_drawdown"))
        if ever >= float(pget(settings, "trail_enable")) and close <= float(peak) * (1 - dd):
            out.append(_sig("trail", date, f"trail|{pool}|v{nver}|{holding.get('entry_date')}",
                            f"自峰值 {peak:.2f} 回撤 {1 - close / peak:.1%} ≥ {dd:.0%}，剩余清仓",
                            close=close, peak=peak, drawdown=1 - close / peak,
                            trail_line=float(peak) * (1 - dd)))

    # ---------- 4. 阶梯减仓（P2）：只提示“新跨过”的最高一档 ----------
    if holding and cp == cp and cp is not None:
        if cp >= 1.0:
            # ---------- 5. 到目标终减（替代阶梯） ----------
            remain = _final_remain(idx_cp, idx_phase, settings)
            out.append(_sig("final_trim", date, f"final|{holding.get('entry_date')}",
                            f"完成度 {cp:.0%} 已到量度目标，按主指数终减 → 剩 {remain:.0%}",
                            completion=cp, remain=remain, close=close,
                            index_completion=idx_cp, index_phase=idx_phase))
        else:
            done = float(holding.get("completed") or 0)
            crossed = [(float(lvl), float(remain)) for lvl, remain in pget(settings, "ladder")
                       if cp >= float(lvl) and float(lvl) > done + 1e-9]
            if crossed:
                lvl, remain = max(crossed, key=lambda x: x[0])
                out.append(_sig("trim", date, f"trim|{lvl}|{holding.get('entry_date')}",
                                f"完成度 {cp:.0%} ≥ {lvl:.0%}，减仓后剩 {remain:.0%}",
                                completion=cp, stage=lvl, remain=remain, close=close))
    # 完成度 >=90% 提醒看头部（无持仓也提醒）
    if cp == cp and cp is not None and cp >= float(pget(settings, "structure_realized")):
        out.append(_sig("target_watch", date, f"target_watch|v{nver}",
                        f"完成度 {cp:.0%}，已进入量度满足区，开始留意头部形态",
                        completion=cp, close=close))

    # ---------- 6. 共振入场 ----------
    if nk is not None and (tg is not None) and not holding \
            and st.get("phase") not in ("exhausted",) \
            and not (cp == cp and cp is not None and cp >= 1.0):
        confirm_n = max(1, int(pget(settings, "confirm_bars")))
        broke = _right_bars_before(bars, len(bars) - 1, close, nk, confirm_n)
        if broke:
            late = idx_cp is not None and (idx_cp >= float(pget(settings, "index_late")))
            pos = float(pget(settings, "late_position")) if late else 1.0
            right = idx_lbl == "right" and idx_phase != "exhausted"
            if right:
                note = "指数晚期·半仓入场" if late else "满仓满融"
                out.append(_sig("entry", date, f"entry|{nver}",
                                f"收盘 {close:.2f} 站上颈线 {nk:.2f}，主指数右侧 → {note}",
                                close=close, neckline=nk, target=tg, position=pos,
                                index_completion=idx_cp, half=bool(late)))
            else:
                why = "主指数结构已衰竭" if idx_phase == "exhausted" else "等待主指数转为右侧"
                out.append(_sig("entry_wait", date, f"entry_wait|{nver}",
                                f"个股已突破颈线 {nk:.2f}，{why}",
                                close=close, neckline=nk, index_label=idx_lbl,
                                index_phase=idx_phase))

    # ---------- 7. 回撤买回 / 买点作废（路 A） ----------
    if pull.get("price") is not None and pull.get("status", "active") == "active":
        lo = float(pull.get("price_lower") if pull.get("price_lower") is not None else pull["price"])
        hi = float(pull["price"])
        if lo <= close <= hi and pull.get("zone_mode") == "buyback":
            ratio = pull.get("buyback_ratio")
            warn = bool(idx_cp is not None and idx_cp >= 0.7)
            out.append(_sig("pullback", date, f"pullback|{lo}|{hi}",
                            f"收盘 {close:.2f} 进入回撤买入区 {lo:.2f}~{hi:.2f}，买回 {ratio or '?'} 成"
                            + ("（逆共振警示：主指数高位）" if warn else ""),
                            close=close, zone=[lo, hi], buyback_ratio=ratio,
                            index_label=idx_lbl, index_high=warn))
        inv = lo * float(pget(settings, "buyback_invalid_k"))
        if close < inv:
            out.append(_sig("pullback_invalid", date, f"pullback_invalid|{lo}",
                            f"收盘 {close:.2f} < 买入区下沿×0.95={inv:.2f}，买点作废",
                            close=close, invalidation=inv))

    # ---------- 10. 哨兵 ----------
    out.extend(_sentinel_signals(sentinels, close, date, settings, pool))
    return out


def _final_remain(idx_cp, idx_phase, settings) -> float:
    if idx_phase == "exhausted":
        return float(pget(settings, "final_idx_exhaust"))
    if idx_cp is None or idx_cp != idx_cp:
        return float(pget(settings, "final_idx_lt70"))
    if idx_cp >= 0.90:
        return float(pget(settings, "final_idx_ge90"))
    if idx_cp >= 0.70:
        return float(pget(settings, "final_idx_70_90"))
    return float(pget(settings, "final_idx_lt70"))


def _sentinel_signals(sentinels: list[dict], close: float, date: str,
                      settings: dict | None, pool: str) -> list[dict]:
    out = []
    for s in sentinels or []:
        try:
            px = float(s["price"])
        except (KeyError, TypeError, ValueError):
            continue
        if px <= 0:
            continue
        direction = s.get("direction") or "up"
        dist = abs(close / px - 1)
        crossed = (close >= px) if direction == "up" else (close <= px)
        sid = s.get("id") or f"{px}"
        if crossed:
            out.append(_sig("sentinel_cross", date, f"sentinel_cross|{sid}",
                            f"收盘 {close:.2f} {'站上' if direction == 'up' else '跌破'}哨兵 {px:.2f}，是否更新方向标签？",
                            price=px, close=close, direction=direction))
        elif dist <= float(pget(settings, "sentinel_push")):
            out.append(_sig("sentinel_near", date, f"sentinel_near|{sid}",
                            f"距哨兵 {px:.2f} 还有 {dist:.1%}",
                            price=px, close=close, distance=dist))
        elif dist <= float(pget(settings, "sentinel_near")):
            out.append(_sig("sentinel_near", date, f"sentinel_near|{sid}",
                            f"距哨兵 {px:.2f} 还有 {dist:.1%}（贴脸）",
                            price=px, close=close, distance=dist))
        elif dist <= float(pget(settings, "sentinel_far")):
            out.append(_sig("sentinel_far", date, f"sentinel_far|{sid}",
                            f"距哨兵 {px:.2f} 还有 {dist:.1%}",
                            price=px, close=close, distance=dist))
    return out


def campaign_pnl(entry: float, events: list[dict]) -> dict:
    """模拟收益：events = [{date, remain, price}]（减仓/清仓按剩余仓位）。

    仓位口径：满仓=1.0；每次减仓把 (当前仓位-目标仓位) 按当时价格卖出。
    返回 {pnl_pct, legs:[...]}，价格口径，未乘杠杆。
    """
    pos = 1.0
    realized = 0.0
    legs = []
    for ev in sorted(events, key=lambda x: str(x.get("date"))):
        remain = float(ev.get("remain", pos))
        price = float(ev.get("price") or 0)
        if price <= 0 or remain >= pos:
            continue
        sold = pos - remain
        realized += sold * (price / entry - 1)
        legs.append({"date": ev.get("date"), "sell": round(sold, 4),
                     "price": price, "pnl": round(sold * (price / entry - 1), 6)})
        pos = remain
        if pos <= 0:
            break
    if pos > 0 and events:
        last = sorted(events, key=lambda x: str(x.get("date")))[-1]
        price = float(last.get("price") or 0)
        # 剩余仓位按最后价格盯市
        mark = pos * (price / entry - 1) if price > 0 else 0.0
    else:
        mark = 0.0
    return {"pnl_pct": round((realized + mark) * 100, 3), "realized_pct": round(realized * 100, 3),
            "open_pos": round(pos, 4), "legs": legs}
