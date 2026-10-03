"""Headless scan for GitHub Actions.

swing   : completed daily candle scan -> position update (exit alerts) -> naye BUY (dedup + slot limit) -> Telegram
intraday: complete candles scan -> naye BUY (din me ek baar per stock) -> Telegram
State positions.json me rehti hai (workflow use commit kar deta hai).
"""
import os
import sys

import core
import positions

mode = os.environ.get("SCAN_MODE", "swing")
interval = os.environ.get("INTRADAY_INTERVAL", "FIFTEEN_MINUTE")
g = core.get_secret
tg_token, tg_chat = g("TELEGRAM_BOT_TOKEN"), g("TELEGRAM_CHAT_ID")

MAX_POS = int(os.environ.get("MAX_POS") or core.DEFAULT_P["max_pos"])
CAPITAL = float(os.environ.get("CAPITAL") or 100000)
RISK_PCT = float(os.environ.get("RISK_PCT") or 1.0)
P = dict(core.DEFAULT_P)
P["max_pos"] = MAX_POS


def tg(text):
    try:
        core.tg_send_text(tg_token, tg_chat, text)
    except Exception as e:  # noqa: BLE001
        print("Telegram error:", e)


if mode == "intraday" and not core.market_open():
    print("Market band hai, intraday scan skip.")
    sys.exit(0)

state = positions.load_state()
angel = core.Angel(g("ANGEL_API_KEY"), g("ANGEL_CLIENT_ID"), g("ANGEL_PIN"), g("ANGEL_TOTP_SECRET"))
uni = core.load_universe(os.environ.get("UNIVERSE", core.DEFAULT_UNIVERSE))
src = uni.attrs.get("source")
print(f"Universe: {len(uni)} symbols (source: {src})")
if src == "fallback" and mode == "swing":
    tg(f"⚠️ Stock list load nahi hui - sirf {len(uni)} symbols ki backup list use hui. "
       "niftyindices ki CSV download karke repo me 'universe_smallcap250.csv' naam se daal do.")

res = core.run_scan(angel.candles, uni, core.load_tokens(), mode, interval, P, ttl=60, events=core.load_events())
nb = int((res["table"]["Signal"] == "BUY").sum()) if not res["table"].empty else 0
print(f"{res['scanned']} scanned | regime {res['regime']['state']} ({res['regime'].get('src')}) | BUY: {nb}")

if mode == "swing":
    asof = res["asof"].strftime("%Y-%m-%d") if res.get("asof") is not None else core.now_ist().strftime("%Y-%m-%d")
    events = positions.update_positions(state, res["raw"], P, CAPITAL, RISK_PCT)
    new_rows = positions.pick_new_signals(state, res, P, MAX_POS)
    extra = {}
    for r in new_rows:
        risk = r["Entry"] - r["StopLoss"]
        extra[r["Symbol"]] = (f"Qty ~{positions.qty_for(r['Entry'], risk, CAPITAL, RISK_PCT, MAX_POS)} "
                              f"(capital Rs{CAPITAL:,.0f}, {RISK_PCT}% risk)")
    items = core.build_top5(res, n=MAX_POS, allow={r["Symbol"] for r in new_rows}, extra=extra) if new_rows else []
    for r in new_rows:
        positions.register(state, r, P, asof)
    summary = positions.open_summary(state, res["raw"])
    if events or summary or items:
        head = f"📊 Swing update ({asof}) | {core.regime_line(res['regime'])}"
        body = "\n".join(events + (["", "Open positions:"] + summary if summary else []))
        tg(head + ("\n\n" + body if body else ""))
    if items:
        core.tg_send_top5(tg_token, tg_chat, res, items)
        print("Telegram bheja:", [i["symbol"] for i in items])
    else:
        print("Koi naya BUY signal nahi (ya slots full / cooldown).")
else:
    day = core.now_ist().strftime("%Y%m%d")
    fresh = []
    if not res["table"].empty:
        for s in res["table"].loc[res["table"]["Signal"] == "BUY", "Symbol"]:
            if f"{day}|intraday|{s}" not in state["sent"]:
                fresh.append(s)
    items = core.build_top5(res, allow=set(fresh)) if fresh else []
    if items:
        core.tg_send_top5(tg_token, tg_chat, res, items)
        for i in items:
            state["sent"][f"{day}|intraday|{i['symbol']}"] = 1
        print("Telegram bheja:", [i["symbol"] for i in items])
    else:
        print("Koi naya BUY signal nahi.")

positions.save_state(state)
