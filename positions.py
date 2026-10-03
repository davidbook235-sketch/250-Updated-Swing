"""Paper position tracking + exit alerts + alert dedup (state: positions.json).

Flow (swing):
  signal (completed candle) -> PENDING -> agle din open par OPEN (gap-up ho to SKIPPED)
  -> har scan par SL / trailing SL / Target / Time-exit check -> Telegram exit alert -> CLOSED
GitHub Actions workflow positions.json ko repo me commit karta hai, isliye state din-ba-din bachi rehti hai.
"""
import json
import datetime as dt

import pandas as pd

STATE_PATH = "positions.json"
COOLDOWN_DAYS = 5          # ek hi stock ka BUY alert itne din tak dobara nahi


def load_state(path=STATE_PATH):
    try:
        s = json.load(open(path))
    except Exception:  # noqa: BLE001
        s = {}
    s.setdefault("positions", [])
    s.setdefault("closed", [])
    s.setdefault("alerts", {})
    s.setdefault("sent", {})
    return s


def save_state(s, path=STATE_PATH):
    s["closed"] = s["closed"][-200:]
    today = dt.date.today().strftime("%Y%m%d")
    s["sent"] = {k: v for k, v in s["sent"].items() if k.startswith(today)}
    with open(path, "w") as f:
        json.dump(s, f, indent=1, default=str)


def active(state):
    return [p for p in state["positions"] if p["status"] in ("PENDING", "OPEN")]


def qty_for(entry, risk, capital, risk_pct, max_pos):
    """1% risk rule: qty = (capital*risk%) / SL-distance, aur ek position capital/max_pos se zyada nahi."""
    if risk <= 0 or entry <= 0:
        return 0
    q = int((capital * risk_pct / 100) / risk)
    cap_q = int(capital / max(max_pos, 1) / entry)
    return max(min(q, cap_q), 0)


def pick_new_signals(state, res, P, max_pos):
    """BUY rows jo naye hain: already active/cooldown wale nahi, aur free slots se zyada nahi."""
    t = res["table"]
    if t.empty:
        return []
    buys = t[t["Signal"] == "BUY"].sort_values(["Score", "RS", "VolX"], ascending=False)
    act = {p["symbol"] for p in active(state)}
    slots = max(max_pos - len(act), 0)
    asof = pd.Timestamp(res["asof"]).normalize() if res.get("asof") is not None else pd.Timestamp(dt.date.today())
    out = []
    for _, r in buys.iterrows():
        if len(out) >= slots:
            break
        sym = r["Symbol"]
        if sym in act:
            continue
        last = state["alerts"].get(sym)
        if last and (asof - pd.Timestamp(last)).days < COOLDOWN_DAYS:
            continue
        out.append(r)
    return out


def register(state, row, P, signal_date):
    atr = (row["Entry"] - row["StopLoss"]) / P["atr_mult"]
    state["positions"].append(dict(symbol=row["Symbol"], status="PENDING", signal_date=signal_date,
                                   last_date=signal_date, ref=float(row["Entry"]), atr=float(atr),
                                   score=int(row["Score"]), entry=None, entry_date=None, sl=None, target=None,
                                   risk=None, qty=0, hi_c=None, bars=0, trailed=False))
    state["alerts"][row["Symbol"]] = signal_date


def update_positions(state, raw, P, capital=100000.0, risk_pct=1.0):
    """Naye completed daily candles par saari active positions update karo. Returns alert text lines."""
    ev, keep = [], []
    max_hold = P["max_hold"]
    for p in state["positions"]:
        if p["status"] not in ("PENDING", "OPEN"):
            continue
        sym, df = p["symbol"], raw.get(p["symbol"])
        if df is None or df.empty:
            keep.append(p)
            continue
        new = df[df.index.normalize() > pd.Timestamp(p["last_date"])]
        done = False
        for ts, b in new.iterrows():
            o, h, l, c = float(b["open"]), float(b["high"]), float(b["low"]), float(b["close"])
            p["last_date"] = ts.strftime("%Y-%m-%d")
            first = False
            if p["status"] == "PENDING":
                if P["gap_skip_atr"] and o > p["ref"] + P["gap_skip_atr"] * p["atr"]:
                    ev.append(f"⏭️ {sym}: gap-up open {o:.2f} (limit {p['ref'] + P['gap_skip_atr'] * p['atr']:.2f}) - trade SKIP")
                    done = True
                    break
                risk = P["atr_mult"] * p["atr"]
                p.update(status="OPEN", entry=o, entry_date=p["last_date"], risk=risk, sl=o - risk,
                         target=(o + P["rr"] * risk) if P["target_on"] else None, hi_c=o, bars=0,
                         qty=qty_for(o, risk, capital, risk_pct, P["max_pos"]))
                ev.append(f"🟢 ENTRY {sym} @ {o:.2f} | SL {p['sl']:.2f} | "
                          + (f"TGT {p['target']:.2f}" if p["target"] else "TGT: trail")
                          + f" | Qty {p['qty']}")
                first = True
            else:
                p["bars"] += 1
            sl = p["sl"]
            tag = "TRAIL" if p["trailed"] else "SL"
            px = why = None
            if not first and o <= sl:
                px, why = o, tag
            elif l <= sl:
                px, why = sl, tag
            elif p["target"] and h >= p["target"]:
                px, why = p["target"], "TARGET"
            elif p["bars"] >= max_hold:
                px, why = c, "TIME"
            if why:
                pnl = (px / p["entry"] - 1) * 100
                r_mult = (px - p["entry"]) / p["risk"]
                ev.append(f"{'✅' if pnl > 0 else '🔴'} EXIT {sym} ({why}) @ {px:.2f} | P&L {pnl:+.2f}% ({r_mult:+.2f}R)")
                state["closed"].append(dict(symbol=sym, entry=p["entry"], entry_date=p["entry_date"], exit=round(px, 2),
                                            exit_date=p["last_date"], why=why, pnl_pct=round(pnl, 2), R=round(r_mult, 2)))
                done = True
                break
            if P["trail_on"]:
                p["hi_c"] = max(p["hi_c"], c)
                if p["hi_c"] >= p["entry"] + p["risk"]:
                    new_sl = p["hi_c"] - P["trail_mult"] * p["atr"]
                    if new_sl > p["sl"]:
                        p["sl"], p["trailed"] = new_sl, True
        if not done:
            keep.append(p)
    state["positions"] = keep
    return ev


def open_summary(state, raw):
    lines = []
    for p in active(state):
        df = raw.get(p["symbol"])
        ltp = float(df["close"].iloc[-1]) if df is not None and len(df) else None
        if p["status"] == "PENDING":
            lines.append(f"⏳ {p['symbol']}: kal open par entry (ref {p['ref']:.2f})")
        else:
            pnl = f"{(ltp / p['entry'] - 1) * 100:+.2f}%" if ltp else "-"
            lines.append(f"📌 {p['symbol']}: entry {p['entry']:.2f} | LTP {ltp:.2f} ({pnl}) | SL {p['sl']:.2f}"
                         + (" (trailing)" if p["trailed"] else "")
                         + (f" | TGT {p['target']:.2f}" if p["target"] else "") + f" | din {p['bars']}")
    return lines
