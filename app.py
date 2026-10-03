import time

import pandas as pd
import streamlit as st

import core
import positions

try:
    from streamlit_autorefresh import st_autorefresh
except Exception:  # noqa: BLE001
    st_autorefresh = None

st.set_page_config(page_title="Nifty250 Scanner", page_icon="📈", layout="centered",
                   initial_sidebar_state="collapsed")
st.markdown("<style>.block-container{padding:1rem .8rem 3rem}</style>", unsafe_allow_html=True)
ss = st.session_state
ss.setdefault("res", None)
ss.setdefault("last_scan", 0.0)
ss.setdefault("sent", set())
ss.setdefault("top5", [])

D = core.DEFAULT_P
st.title("📈 Nifty250 Scanner v2")

# ------------------------------------------------------------------ settings
with st.expander("⚙️ Settings", expanded=False):
    c1, c2 = st.columns(2)
    mode_label = c1.radio("Mode", ["Swing (1D)", "Intraday"], horizontal=True)
    mode = "swing" if mode_label.startswith("Swing") else "intraday"
    iv_label = c2.selectbox("Intraday candle", ["15 min", "5 min", "30 min"], disabled=(mode == "swing"))
    interval = {"15 min": "FIFTEEN_MINUTE", "5 min": "FIVE_MINUTE", "30 min": "THIRTY_MINUTE"}[iv_label]

    st.markdown("**Strategy (4 alag signals: Trend, Momentum, Breakout, Volume)**")
    s1, s2 = st.columns(2)
    score_buy = s1.slider("BUY min score (of 4)", 2, 4, D["score_buy"])
    must_brk = s2.toggle("Breakout zaroori", D["must_brk"])
    s3, s4, s5 = st.columns(3)
    rsi_min = s3.slider("RSI >", 50, 70, D["rsi_min"])
    rsi_max = s4.slider("RSI <", 70, 90, D["rsi_max"])
    vol_mult = s5.number_input("Volume x", 1.0, 5.0, D["vol_mult"], 0.1)

    st.markdown("**Filters (ON/OFF)**")
    f1, f2, f3 = st.columns(3)
    adx_on = f1.toggle("ADX", D["adx_on"])
    regime_on = f2.toggle("Market regime", D["regime_on"])
    sector_on = f3.toggle("Sector", D["sector_on"])
    f4, f5, f6 = st.columns(3)
    liq_on = f4.toggle("Liquidity", D["liq_on"])
    ema200_on = f5.toggle("Price>EMA200", D["ema200_on"])
    rs_on = f6.toggle("Rel. strength", D["rs_on"])
    f7, f8 = st.columns(2)
    ext_on = f7.toggle("Extension limit", D["ext_on"])
    room_on = f8.toggle("Resistance room", D["room_on"])
    k1, k2, k3 = st.columns(3)
    adx_min = k1.slider("Min ADX", 10, 40, D["adx_min"], disabled=not adx_on)
    min_turn = k2.number_input("Min turnover (Cr/day)", 0.5, 50.0, D["min_turnover_cr"], 0.5, disabled=not liq_on)
    ext_max = k3.number_input("Max EMA20 se ATR door", 1.0, 6.0, D["ext_max_atr"], 0.5, disabled=not ext_on)
    min_room = st.number_input("Resistance tak min room (R)", 0.5, 4.0, D["min_room_r"], 0.5, disabled=not room_on)

    st.markdown("**Risk / Position**")
    r1, r2, r3 = st.columns(3)
    atr_mult = r1.number_input("SL = ATR x", 0.5, 4.0, D["atr_mult"], 0.5)
    rr = r2.number_input("Reward:Risk", 1.0, 5.0, D["rr"], 0.5)
    trail_on = r3.toggle("Trailing SL", D["trail_on"])
    r4, r5, r6 = st.columns(3)
    max_pos = r4.slider("Max positions", 1, 10, D["max_pos"])
    capital = r5.number_input("Capital (Rs)", 10000, 10000000, 100000, 10000)
    risk_pct = r6.number_input("Risk % / trade", 0.25, 3.0, 1.0, 0.25)

    st.markdown("**Scan / Alerts**")
    uni_name = st.selectbox("Stock list", list(core.UNIVERSES), index=0)
    topn = st.slider("Kitne stocks scan karne hain", 20, 500, 500, 10)
    auto = st.toggle("🔄 Live auto-scan", False)
    every = st.slider("Auto-scan har (min)", 5, 30, 10, disabled=not auto)
    tg_auto = st.toggle("📨 Naye BUY par Telegram auto-send", False)
    demo = st.toggle("🧪 Demo data (Angel login ke bina test)", False)

P = {**D, "score_buy": score_buy, "must_brk": must_brk, "rsi_min": rsi_min, "rsi_max": rsi_max, "vol_mult": vol_mult,
     "adx_on": adx_on, "regime_on": regime_on, "sector_on": sector_on, "liq_on": liq_on, "ema200_on": ema200_on,
     "rs_on": rs_on, "ext_on": ext_on, "room_on": room_on, "adx_min": adx_min, "min_turnover_cr": min_turn,
     "ext_max_atr": ext_max, "min_room_r": min_room, "atr_mult": atr_mult, "rr": rr, "trail_on": trail_on,
     "max_pos": max_pos}


# ------------------------------------------------------------------ helpers
def cred(name):
    return ss.get(f"in_{name}") or core.get_secret(name)


@st.cache_resource(ttl=6 * 3600, show_spinner="Angel One login...")
def angel_login(k, c, p, t):
    return core.Angel(k, c, p, t)


@st.cache_data(ttl=6 * 3600, show_spinner="Stock list load ho rahi hai...")
def universe(name):
    return core.load_universe(name)


@st.cache_resource(ttl=86400, show_spinner="Token list load ho rahi hai...")
def tokens():
    return core.load_tokens()


def get_feed():
    if demo:
        return core.demo_fetch, {s: s for s in uni()["Symbol"]}
    keys = [cred(k) for k in ("ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PIN", "ANGEL_TOTP_SECRET")]
    if not all(keys):
        st.error("Angel One keys nahi mili. Setup tab me daalo (ya Demo ON karo).")
        st.stop()
    return angel_login(*keys).candles, tokens()


def uni():
    u = ss.get("uni_override")
    return u if u is not None else universe(uni_name)


def qty_lines(res):
    """Top BUY ke liye position size (1% risk rule)."""
    out = {}
    t = res["table"]
    if t.empty:
        return out
    for _, r in t[t["Signal"] == "BUY"].iterrows():
        q = positions.qty_for(r["Entry"], r["Entry"] - r["StopLoss"], capital, risk_pct, max_pos)
        out[r["Symbol"]] = f"Qty ~{q} (capital Rs{capital:,.0f}, {risk_pct}% risk)"
    return out


def do_scan():
    fetch, tok = get_feed()
    bar = st.progress(0.0, text="Scan shuru...")
    ttl = 60 if not auto else max(60, every * 60 - 30)
    events = ss.get("events_override") or core.load_events()
    res = core.run_scan(fetch, uni(), tok, mode, interval, P, topn,
                        progress=lambda i, n, s: bar.progress(i / n, text=f"{s}  ({i}/{n})"), ttl=ttl, events=events)
    bar.empty()
    ss.res, ss.last_scan, ss.res_key = res, time.time(), (tuple(sorted(P.items())), mode)
    ss.top5 = core.build_top5(res, extra=qty_lines(res))
    if tg_auto:
        send_telegram(only_new=True)


def send_telegram(only_new=False):
    token, chat = cred("TELEGRAM_BOT_TOKEN"), cred("TELEGRAM_CHAT_ID")
    if not (token and chat):
        st.warning("Telegram token/chat id Setup tab me daalo.")
        return
    day = time.strftime("%Y%m%d")
    items = [i for i in ss.top5 if not only_new or f"{day}|{ss.res['mode']}|{i['symbol']}" not in ss.sent]
    if not items:
        if not only_new:
            st.info("Abhi koi BUY signal nahi hai.")
        return
    ok = core.tg_send_top5(token, chat, ss.res, items)
    for i in items:
        ss.sent.add(f"{day}|{ss.res['mode']}|{i['symbol']}")
    st.toast("Telegram bhej diya ✅" if ok else "Telegram me error ❌")


# ------------------------------------------------------------------ tabs
tab1, tab2, tab3, tab4 = st.tabs(["📡 Scanner", "🧪 Backtest", "📌 Positions", "🔑 Setup"])

with tab1:
    open_ = core.market_open()
    st.caption((("🟢 Market OPEN — swing scan sirf pichhli COMPLETED candle par" if mode == "swing" else "🟢 Market OPEN")
                if open_ else "🔴 Market CLOSED — last completed session ka data") + f"  •  IST {core.now_ist():%H:%M}")
    if auto and st_autorefresh:
        st_autorefresh(interval=every * 60 * 1000, key="auto")
    due = auto and (time.time() - ss.last_scan) >= every * 60 - 5
    if st.button("🔍 Scan Now", type="primary", use_container_width=True) or due:
        do_scan()

    res = ss.res
    key = (tuple(sorted(P.items())), mode)
    if res is not None and res["mode"] != mode:
        st.warning(f"Ye result **{res['mode']}** scan ka hai. Mode badla hai — dobara **Scan Now** dabao.")
        res = None
    elif res is not None and ss.get("res_key") != key:
        res = ss.res = core.rescore(res, P)   # filters/score badle -> data dobara fetch kiye bina recompute
        ss.res_key, ss.top5 = key, core.build_top5(res, extra=qty_lines(res))
    if res is None:
        st.info("Settings check karke **Scan Now** dabao.")
    else:
        rg = res["regime"]
        icon = {"BULLISH": "🟢", "NEUTRAL": "🟡", "BEARISH": "🔴"}.get(rg["state"], "⚪")
        st.markdown(f"**{icon} {core.regime_line(rg)}**  \nIndex {rg['close']:.1f} | EMA20 {rg['ema20']:.1f} | EMA50 {rg['ema50']:.1f}")
        if res.get("asof") is not None and mode == "swing":
            st.caption(f"Data as of: {pd.Timestamp(res['asof']):%d-%b-%Y} (last completed daily candle)")
        t = res["table"]
        nb = int((t["Signal"] == "BUY").sum()) if not t.empty else 0
        st.caption(f"Scan {res['time']:%H:%M:%S} • {uni_name} • {res['scanned']} stocks • {nb} BUY • "
                   f"{len(t) - nb} WATCH • {len(res['missing'])} skip")
        on = [n for n, v in (("ADX", adx_on), ("Regime", regime_on), ("Sector", sector_on), ("Liquidity", liq_on),
                             ("EMA200", ema200_on), ("RS", rs_on), ("Extension", ext_on), ("Room", room_on)) if v]
        st.caption("Filters ON: " + (", ".join(on) if on else "koi nahi (sirf score)"))
        src = getattr(res["uni"], "attrs", {}).get("source")
        if src == "fallback" and not ss.get("uni_override") and not demo:
            st.error("Stock list niftyindices se load nahi hui, sirf 17 symbols ki backup list use ho rahi hai. "
                     "Repo me 'universe_smallcap250.csv' commit karo ya Setup tab me CSV upload karo.")
        full = res.get("full")
        if full is not None and not full.empty:
            with st.expander("🔎 BUY kyun nahi aa raha? (diagnosis)", expanded=(nb == 0)):
                dist = full["Score"].value_counts().reindex(list(range(core.SCORE_MAX, -1, -1)), fill_value=0)
                st.write("**Score distribution**: " + "  |  ".join(f"{k}/{core.SCORE_MAX}: {v}" for k, v in dist.items()))
                cand = full[full["Score"] >= score_buy]
                if len(cand):
                    blocked = cand[cand["Blocked"] != ""]
                    if len(blocked):
                        st.write(f"Score {score_buy}+ wale {len(cand)} stocks me se {len(blocked)} filter se block: " +
                                 ", ".join(f"{r.Symbol} ({r.Blocked})" for r in blocked.head(25).itertuples()))
                else:
                    st.write(f"Koi stock {score_buy}/{core.SCORE_MAX} tak nahi pahuncha. Setup hi nahi bana — "
                             "BUY min score kam karke ya stock list badal kar dekho.")
        if t.empty:
            st.warning("Is waqt koi BUY/WATCH signal nahi mila.")
        else:
            show = ["Symbol", "Signal", "Score", "Entry", "StopLoss", "Target", "MaxEntry", "ADX", "RSI", "VolX", "RS",
                    "Ext", "RoomR", "TurnCr", "Sector", "Blocked", "Reasons"]
            st.dataframe(t[show], use_container_width=True, hide_index=True,
                         column_config={"Score": st.column_config.ProgressColumn("Score", min_value=0, max_value=core.SCORE_MAX, format="%d")})
            st.caption("Entry = last close (reference). Swing me kal OPEN par lo; open > MaxEntry ho to skip.")
            st.download_button("⬇️ CSV download", t[show].to_csv(index=False).encode(),
                               f"scan_{mode}_{res['time']:%Y%m%d_%H%M}.csv", "text/csv", use_container_width=True)
            st.subheader("🏆 Top 5 BUY — Support / Resistance")
            if not ss.top5:
                st.caption("Abhi koi BUY rating nahi.")
            for it in ss.top5:
                st.image(it["png"], use_container_width=True)
                st.code(it["caption"], language=None)
            if ss.top5 and st.button("📨 Top 5 Telegram par bhejo", use_container_width=True):
                send_telegram()
            if sector_on and "SectorRank" in t:
                with st.expander("Sector strength"):
                    sec = t.groupby("Sector").agg(Stocks=("Symbol", "size"), AvgRet=("Ret", "mean"),
                                                  Rank=("SectorRank", "first")).round(2).sort_values("Rank", ascending=False)
                    st.dataframe(sec, use_container_width=True)
        if res["missing"]:
            with st.expander("Skip hue symbols"):
                st.write(", ".join(res["missing"][:80]))

with tab2:
    st.caption("Same strategy + filters historical data par (Sector aur Results filter backtest me nahi lagte). "
               "Regime/RS universe ke equal-weight index se, portfolio limit aur out-of-sample alag dikhta hai.")
    syms_all = list(uni()["Symbol"])
    b0, b1 = st.columns(2)
    n_syms = b0.slider("Kitne stocks (list ke pehle N)", 10, min(len(syms_all), 250), min(len(syms_all), 60))
    pick = b1.multiselect("Ya khud chuno (optional)", syms_all, default=[])
    syms = pick or syms_all[:n_syms]
    b2, b3 = st.columns(2)
    days = b2.slider("History (days)", 90 if mode == "swing" else 10, 1500 if mode == "swing" else 90,
                     750 if mode == "swing" else 30)
    max_hold = b3.slider("Max hold (days)", 2, 30, D["max_hold"], disabled=(mode == "intraday"))
    b4, b5 = st.columns(2)
    cost = b4.number_input("Cost+slippage % / trade", 0.0, 1.5, 0.25 if mode == "swing" else 0.1, 0.05)
    bt_risk = b5.number_input("Risk % per trade ", 0.25, 5.0, 1.0, 0.25)

    def bundle():
        bkey = (mode, interval, days, tuple(syms), demo)
        if ss.get("bt_bkey") != bkey:
            fetch, tok = get_feed()
            bar = st.progress(0.0, text="Data fetch...")
            ss.bt_bundle = core.bt_fetch(fetch, syms, tok, mode, interval, days,
                                         progress=lambda i, n, s: bar.progress(i / n, text=f"{s} ({i}/{n})"))
            ss.bt_bkey = bkey
            bar.empty()
        return ss.bt_bundle

    q1, q2 = st.columns(2)
    if q1.button("▶️ Run Backtest", type="primary", use_container_width=True):
        ss.bt = core.bt_run(bundle(), mode, P, max_hold, cost, bt_risk, max_pos)
        ss.bt_none = ss.bt is None
    if q2.button("⚖️ Compare variants", use_container_width=True):
        bar = st.progress(0.0, text="Variants...")
        ss.bt_var = core.run_variants(bundle(), mode, P, None, max_hold, cost, bt_risk, max_pos,
                                      progress=lambda i, n, s: bar.progress(i / n, text=f"{s} ({i}/{n})"))
        bar.empty()

    if ss.get("bt_var") is not None:
        st.markdown("**Variants (OOS_PF / OOS_AvgR = naye 30% period ka result; wahi dekhna, sirf Base nahi)**")
        st.dataframe(ss.bt_var, use_container_width=True, hide_index=True)
        st.caption("Jo variant in-sample aur out-of-sample dono me PF>1.3 aur theek-thaak trades de, wahi rakho. "
                   "100 se kam trades par result par bharosa mat karo.")
    bt = ss.get("bt")
    if ss.get("bt_none"):
        st.warning("Is period me koi trade nahi bana. Filters ya score kam karke dekho.")
    elif bt:
        s = bt["stats"]
        m1, m2, m3 = st.columns(3)
        m1.metric("Trades", s["Trades"])
        m2.metric("Win rate", f"{s['WinRate']}%")
        m3.metric("Avg R", s["AvgR"])
        m4, m5, m6 = st.columns(3)
        m4.metric("Profit factor", s["ProfitFactor"])
        m5.metric("Return", f"{s['Return_pct']}%")
        m6.metric("Max DD", f"{s['MaxDD_pct']}%")
        st.caption(f"Max {max_pos} positions ki limit ki wajah se {s['Skipped_slots']} signals skip hue.")
        cmp_ = pd.DataFrame({"In-sample": bt["is_stats"], f"Out-of-sample (>{pd.Timestamp(bt['oos_from']):%d-%b-%y})": bt["oos_stats"]})
        st.dataframe(cmp_, use_container_width=True)
        st.line_chart(bt["equity"], height=220)
        st.markdown("**Symbol-wise**")
        st.dataframe(bt["per_symbol"], use_container_width=True)
        with st.expander("Saare trades"):
            st.dataframe(bt["trades"], use_container_width=True, hide_index=True)
        st.caption("Entry: signal ke agle din ka open (gap-up > limit ho to skip). SL aur Target ek hi candle me lage to SL maana gaya.")

with tab3:
    st.caption("Paper position tracking — GitHub Actions swing scan chalate waqt `positions.json` update karta hai "
               "(repo me commit hota hai), yahan wahi file dikhti hai.")
    stt = positions.load_state()
    act = positions.active(stt)
    if act:
        st.dataframe(pd.DataFrame(act)[["symbol", "status", "signal_date", "entry", "entry_date", "sl", "target", "qty", "bars", "trailed"]],
                     use_container_width=True, hide_index=True)
    else:
        st.info("Abhi koi open/pending position nahi.")
    cl = stt["closed"]
    if cl:
        cdf = pd.DataFrame(cl)
        c1_, c2_, c3_ = st.columns(3)
        c1_.metric("Closed trades", len(cdf))
        c2_.metric("Win rate", f"{(cdf['R'] > 0).mean() * 100:.0f}%")
        c3_.metric("Total R", f"{cdf['R'].sum():.1f}")
        st.dataframe(cdf.iloc[::-1], use_container_width=True, hide_index=True)

with tab4:
    st.markdown("**Best tareeka:** Streamlit Cloud → App → Settings → *Secrets* me daalo (neeche format). "
                "Yahan daalne se sirf is session tak chalega.")
    st.code('ANGEL_API_KEY = "..."\nANGEL_CLIENT_ID = "..."\nANGEL_PIN = "..."\nANGEL_TOTP_SECRET = "..."\n'
            'TELEGRAM_BOT_TOKEN = "..."\nTELEGRAM_CHAT_ID = "..."', language="toml")
    for k in ("ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PIN", "ANGEL_TOTP_SECRET", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
        st.text_input(k, type="password" if k != "ANGEL_CLIENT_ID" and k != "TELEGRAM_CHAT_ID" else "default",
                      key=f"in_{k}", placeholder="secrets me set hai" if core.get_secret(k) else "")
    if st.button("Telegram test message"):
        ok, msg = core.tg_send_text(cred("TELEGRAM_BOT_TOKEN"), cred("TELEGRAM_CHAT_ID"), "✅ Nifty250 Scanner connected")
        st.success("Bhej diya") if ok else st.error(msg)
    up = st.file_uploader("Apni stock list upload karo (CSV: Symbol[, Sector])", type="csv")
    if up is not None:
        u = pd.read_csv(up).rename(columns={"Industry": "Sector"})
        u["Sector"] = u["Sector"] if "Sector" in u else "Unknown"
        ss.uni_override = u[["Symbol", "Sector"]]
        st.success(f"{len(u)} symbols load ho gaye.")
    ev = st.file_uploader("Results calendar (CSV: Symbol,Date YYYY-MM-DD) — optional", type="csv", key="evup")
    if ev is not None:
        ev.seek(0)
        e = pd.read_csv(ev)
        e.to_csv("results_calendar.csv", index=False)
        ss.events_override = core.load_events("results_calendar.csv")
        st.success(f"{len(e)} results dates load ho gayi (is session me).")
    st.caption(f"Universe: {uni_name} — {len(uni())} symbols (niftyindices se auto-fetch).")
