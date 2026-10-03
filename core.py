"""Nifty 250 Scanner v2 - core engine (Angel One + indicators + backtest + charts + Telegram).

v2 changes (swing focus):
  * Score ab 4 INDEPENDENT signals ka hai (Trend, Momentum, Breakout, Volume) - EMA/RSI/MACD ko ek hi signal ginta hai
  * Naye filters: liquidity, price>EMA200, relative strength, extension limit, RSI upper cap,
    resistance tak min room (R), circuit-lock, split/bonus jump, results calendar
  * Swing scan sirf COMPLETED daily candle par
  * Market regime ab universe ke apne equal-weight index + breadth se (smallcap ke liye Nifty50 se behtar)
  * Backtest: portfolio limit (max positions), trailing stop, gap-skip, in-sample vs out-of-sample,
    variants comparison, 0.25% default cost
"""
import io
import json
import os
import time
import zlib
import datetime as dt
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import requests
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
SCRIP_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
_NI = "https://niftyindices.com/IndexConstituent/"
UNIVERSES = {
    "Nifty Smallcap 250": (_NI + "ind_niftysmallcap250list.csv", "universe_smallcap250.csv"),
    "Nifty LargeMidcap 250": (_NI + "ind_niftylargemidcap250list.csv", "universe_largemidcap250.csv"),
    "Nifty 500": (_NI + "ind_nifty500list.csv", "universe_nifty500.csv"),
}
DEFAULT_UNIVERSE = "Nifty Smallcap 250"
NIFTY_TOKEN = "99926000"  # NSE Nifty 50 index token (Angel One)
UA = {"User-Agent": "Mozilla/5.0"}
SCORE_MAX = 4

DEFAULT_P = dict(
    # score: Trend, Momentum(RSI+MACD), Breakout, Volume  (max 4)
    score_buy=3, score_watch=2, rsi_min=55, rsi_max=80, vol_mult=1.5, brk_n=20, must_brk=True,
    # risk
    atr_mult=2.0, rr=2.0, gap_skip_atr=0.5, trail_on=True, trail_mult=2.5, target_on=True,
    # filters
    adx_on=True, adx_min=20, regime_on=True, sector_on=True,
    liq_on=True, min_turnover_cr=2.0, ema200_on=True,
    rs_on=True, rs_min=0.0, rs_n=60,
    ext_on=True, ext_max_atr=3.0,
    room_on=True, min_room_r=1.5,
    event_days=5,
    # portfolio
    max_pos=5, max_hold=10,
)

# (flag column, label) - flag True = pass. Same flags live scan aur backtest dono me use hote hain.
OK_FLAGS = [
    ("ok_adx", "ADX<{adx_min}"),
    ("ok_liq", "Illiquid(<{min_turnover_cr}Cr)"),
    ("ok_200", "Below EMA200"),
    ("ok_rs", "Weak vs market"),
    ("ok_ext", "Extended(>{ext_max_atr}ATR)"),
    ("ok_rsi", "RSI>{rsi_max}"),
    ("ok_jump", "Split/bonus?"),
    ("ok_circ", "Circuit lock"),
    ("ok_brk", "No breakout"),
    ("ok_vwap", "Below VWAP"),
]

# Backtest variants (jo variant tikta hai wahi rakho)
VARIANTS = {
    "Base (default)": {},
    "Score 4/4": {"score_buy": 4},
    "Score 2/4": {"score_buy": 2},
    "ADX OFF": {"adx_on": False},
    "Regime OFF": {"regime_on": False},
    "RS + EMA200 OFF": {"rs_on": False, "ema200_on": False},
    "Extension limit OFF": {"ext_on": False},
    "Resistance room OFF": {"room_on": False},
    "Liquidity OFF": {"liq_on": False},
    "SL 1.5 ATR": {"atr_mult": 1.5},
    "SL 2.5 ATR": {"atr_mult": 2.5},
    "Trail, no fixed target": {"trail_on": True, "target_on": False},
    "No trail": {"trail_on": False},
    "Purani loose strategy": {"adx_on": False, "regime_on": False, "liq_on": False, "ema200_on": False,
                              "rs_on": False, "ext_on": False, "room_on": False, "must_brk": False,
                              "trail_on": False},
}


def now_ist():
    return dt.datetime.now(IST).replace(tzinfo=None)


def market_open():
    n = now_ist()
    return n.weekday() < 5 and dt.time(9, 15) <= n.time() <= dt.time(15, 30)


def get_secret(name, default=""):
    v = os.environ.get(name)
    if v:
        return v
    try:
        import streamlit as st
        return st.secrets.get(name, default)
    except Exception:
        return default


def drop_incomplete(df, mode="swing"):
    """Swing: aaj ki adhoori daily candle hata do (final candle 15:45 ke baad hi maani jayegi)."""
    if mode != "swing" or df is None or df.empty:
        return df
    n = now_ist()
    if df.index[-1].normalize() == pd.Timestamp(n.date()) and n.time() < dt.time(15, 45):
        return df.iloc[:-1]
    return df


# --------------------------------------------------------------------------- indicators
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(c, n=14):
    d = c.diff()
    up, dn = d.clip(lower=0), -d.clip(upper=0)
    ru = up.ewm(alpha=1 / n, adjust=False).mean()
    rd = dn.ewm(alpha=1 / n, adjust=False).mean()
    return (100 - 100 / (1 + ru / rd.replace(0, np.nan))).fillna(100)


def atr(df, n=14):
    pc = df["close"].shift()
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def adx(df, n=14):
    up, dn = df["high"].diff(), -df["low"].diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    a = atr(df, n)
    pdi = 100 * pdm.ewm(alpha=1 / n, adjust=False).mean() / a
    mdi = 100 * mdm.ewm(alpha=1 / n, adjust=False).mean() / a
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def _align(series, idx):
    """Daily series ko (intraday ya daily) index par date ke hisaab se ffill karke align karo."""
    s = series.copy()
    s.index = pd.DatetimeIndex(s.index).normalize()
    s = s[~s.index.duplicated()].sort_index()
    return s.reindex(pd.DatetimeIndex(idx).normalize(), method="ffill").values


def add_indicators(df, mode, P, bench=None):
    d = df.copy()
    swing = mode == "swing"
    f, s = (20, 50) if swing else (9, 21)
    c = d["close"]
    ix = d.index

    def flag(cond, on):
        return cond.fillna(False) if on else pd.Series(True, index=ix)

    d["ema_f"], d["ema_s"] = ema(c, f), ema(c, s)
    d["ema200"] = ema(c, 200)
    d["rsi"] = rsi(c)
    d["macd"] = ema(c, 12) - ema(c, 26)
    d["macd_sig"] = ema(d["macd"], 9)
    d["atr"] = atr(d)
    d["adx"] = adx(d)
    d["vol_ma"] = d["volume"].rolling(20).mean().shift(1)
    d["hh"] = d["high"].rolling(P["brk_n"]).max().shift(1)
    if mode == "intraday":
        day = ix.normalize()
        tp = (d["high"] + d["low"] + c) / 3
        d["vwap"] = (tp * d["volume"]).groupby(day).cumsum() / d["volume"].groupby(day).cumsum().replace(0, np.nan)
        vwap_ok = c > d["vwap"]
    else:
        vwap_ok = pd.Series(True, index=ix)
    d["vwap_ok"] = vwap_ok

    # --- swing-only helper columns
    d["turn"] = (c * d["volume"]).rolling(20).mean()                      # 20-day avg traded value (Rs)
    d["ext"] = (c - d["ema_f"]) / d["atr"]                                # EMA20 se kitne ATR door
    ret1 = c.pct_change()
    if swing and bench is not None:
        b = pd.Series(_align(bench, ix), index=ix)
        n = P["rs_n"]
        d["rs"] = (c / c.shift(n) - 1) - (b / b.shift(n) - 1)             # stock - market (fraction)
    else:
        d["rs"] = np.nan
    if swing:
        d["jump"] = (ret1.abs() > 0.30).astype(float).rolling(60, min_periods=1).max() > 0   # split/bonus suspect
        rng = (d["high"] - d["low"]) / c
        d["circ"] = (rng < 0.004) & (ret1 > 0.04)                          # upper-circuit lock
    else:
        d["jump"] = False
        d["circ"] = False

    # --- 4 independent score signals
    d["c_trend"] = (d["ema_f"] > d["ema_s"]) & (c > d["ema_s"])
    d["c_mom"] = (d["rsi"] > P["rsi_min"]) & (d["macd"] > d["macd_sig"])
    d["c_brk"] = c > d["hh"]
    d["c_vol"] = d["volume"] > P["vol_mult"] * d["vol_ma"]
    d["score"] = d[["c_trend", "c_mom", "c_brk", "c_vol"]].astype(int).sum(axis=1)

    # --- filter flags (True = pass)
    d["ok_adx"] = flag(d["adx"] >= P["adx_min"], P["adx_on"])
    d["ok_liq"] = flag(d["turn"] >= P["min_turnover_cr"] * 1e7, P["liq_on"] and swing)
    d["ok_200"] = flag(c > d["ema200"], P["ema200_on"] and swing)
    d["ok_rs"] = flag(d["rs"] * 100 >= P["rs_min"], P["rs_on"] and swing and bench is not None)
    d["ok_ext"] = flag(d["ext"] <= P["ext_max_atr"], P["ext_on"] and swing)
    d["ok_rsi"] = flag(d["rsi"] <= P["rsi_max"], swing)
    d["ok_jump"] = ~d["jump"].astype(bool)
    d["ok_circ"] = ~d["circ"].astype(bool)
    d["ok_brk"] = flag(d["c_brk"], P["must_brk"])
    d["ok_vwap"] = d["vwap_ok"]
    d["ok_all"] = d[[k for k, _ in OK_FLAGS]].all(axis=1)
    return d


def reason_labels(mode, P):
    f, s = (20, 50) if mode == "swing" else (9, 21)
    unit = "day" if mode == "swing" else "bar"
    return [f"Trend EMA{f}>EMA{s}", f"Momentum RSI>{P['rsi_min']} + MACD", f"{P['brk_n']}-{unit} breakout", "Volume spike"]


# --------------------------------------------------------------------------- support / resistance
def sr_levels(df, price, atr_v, window=5, lookback=160, n=2):
    d = df.tail(lookback)
    hi, lo = d["high"].values, d["low"].values
    piv = []
    for i in range(window, len(d) - window):
        if hi[i] == hi[i - window:i + window + 1].max():
            piv.append(hi[i])
        if lo[i] == lo[i - window:i + window + 1].min():
            piv.append(lo[i])
    piv.sort()
    clusters = []
    tol = max(0.5 * atr_v, price * 0.003)
    for p in piv:
        if clusters and abs(p - np.mean(clusters[-1])) <= tol:
            clusters[-1].append(p)
        else:
            clusters.append([p])
    lv = [float(np.mean(c)) for c in clusters]
    sup = sorted([x for x in lv if x < price], reverse=True)[:n]
    res = sorted([x for x in lv if x > price])[:n]
    if not res and d["high"].max() > price:
        res = [float(d["high"].max())]
    return sup, res


# --------------------------------------------------------------------------- regime / benchmark
def build_bench(raw, nifty, min_syms=15):
    """Universe ka equal-weight index (clipped returns) -> (Series, source). <15 stocks par Nifty50 fallback."""
    closes = {}
    for sym, df in raw.items():
        if df is None or df.empty:
            continue
        s = df["close"].copy()
        s.index = pd.DatetimeIndex(s.index).normalize()
        closes[sym] = s[~s.index.duplicated()]
    if len(closes) >= min_syms:
        cl = pd.DataFrame(closes).sort_index()
        r = cl.pct_change(fill_method=None).clip(-0.25, 0.25)
        m = r.mean(axis=1)[r.notna().sum(axis=1) >= min_syms]
        if len(m) > 60:
            return 100 * (1 + m).cumprod(), "universe"
    if nifty is not None and len(nifty):
        s = nifty["close"].copy()
        s.index = pd.DatetimeIndex(s.index).normalize()
        return s[~s.index.duplicated()], "nifty"
    return None, "none"


def breadth(raw):
    """% stocks jo apne EMA50 ke upar hain."""
    n = up = 0
    for df in raw.values():
        if df is None or len(df) < 55:
            continue
        n += 1
        up += int(df["close"].iloc[-1] > ema(df["close"], 50).iloc[-1])
    return round(100 * up / n, 1) if n else float("nan")


def regime_info(idx_df):
    if idx_df is None or len(idx_df) < 55:
        return dict(state="UNKNOWN", ok=True, close=np.nan, ema20=np.nan, ema50=np.nan)
    c = idx_df["close"]
    e20, e50 = ema(c, 20).iloc[-1], ema(c, 50).iloc[-1]
    last = c.iloc[-1]
    if last > e50 and e20 > e50:
        st = "BULLISH"
    elif last > e50:
        st = "NEUTRAL"
    else:
        st = "BEARISH"
    return dict(state=st, ok=(st == "BULLISH"), close=float(last), ema20=float(e20), ema50=float(e50))


def regime_flags(idx_df):
    c = idx_df["close"]
    return (c > ema(c, 50)) & (ema(c, 20) > ema(c, 50))


def align_regime(flags, idx, mode):
    s = flags.astype(float).copy()
    s.index = pd.DatetimeIndex(s.index).normalize()
    s = s[~s.index.duplicated()]
    if mode == "intraday":
        s = s.shift(1)
    return s.reindex(pd.DatetimeIndex(idx).normalize()).ffill().fillna(0).astype(bool).values


# --------------------------------------------------------------------------- events (results calendar)
def load_events(path="results_calendar.csv"):
    """CSV: Symbol,Date (YYYY-MM-DD). Result/board-meeting dates; in dino ke aas-paas BUY block hota hai."""
    try:
        e = pd.read_csv(path)
        e["Date"] = pd.to_datetime(e["Date"], errors="coerce").dt.date
        out = {}
        for s, d in zip(e["Symbol"].astype(str).str.strip().str.upper(), e["Date"]):
            if pd.notna(d):
                out.setdefault(s, []).append(d)
        return out
    except Exception:  # noqa: BLE001
        return {}


# --------------------------------------------------------------------------- analysis
def analyse(sym, df, mode, P, bench=None):
    swing = mode == "swing"
    min_bars = 210 if (swing and P["ema200_on"]) else 70
    if df is None or len(df) < min_bars:
        return None, None
    d = add_indicators(df, mode, P, bench)
    r = d.iloc[-1]
    if pd.isna(r["atr"]) or pd.isna(r["adx"]):
        return None, None
    entry = float(r["close"])
    risk = P["atr_mult"] * float(r["atr"])
    lb = 20 if swing else 25
    ret = float(d["close"].iloc[-1] / d["close"].iloc[-lb - 1] - 1) * 100 if len(d) > lb else 0.0
    flags = [bool(r[k]) for k in ("c_trend", "c_mom", "c_brk", "c_vol")]
    labels = reason_labels(mode, P)

    room_r, r1 = np.inf, None
    if swing and P["room_on"] and int(r["score"]) >= P["score_buy"] and risk > 0:
        _, res = sr_levels(d, entry, float(r["atr"]))
        if res:
            r1 = res[0]
            room_r = (r1 - entry) / risk
    row = dict(Symbol=sym, Score=int(r["score"]), Entry=round(entry, 2),
               StopLoss=round(entry - risk, 2),
               Target=round(entry + P["rr"] * risk, 2) if P["target_on"] else None,
               MaxEntry=round(entry + P["gap_skip_atr"] * float(r["atr"]), 2),
               Reasons=", ".join(l for l, f in zip(labels, flags) if f),
               ADX=round(float(r["adx"]), 1), RSI=round(float(r["rsi"]), 1),
               VolX=round(float(r["volume"] / r["vol_ma"]), 2) if r["vol_ma"] and r["vol_ma"] > 0 else 0.0,
               Ret=round(ret, 2),
               TurnCr=round(float(r["turn"]) / 1e7, 1) if pd.notna(r["turn"]) else 0.0,
               Ext=round(float(r["ext"]), 2) if pd.notna(r["ext"]) else 0.0,
               RS=round(float(r["rs"]) * 100, 1) if pd.notna(r["rs"]) else 0.0,
               RoomR=round(min(room_r, 99.0), 2), R1=round(r1, 2) if r1 else None,
               ok_room=bool((not P["room_on"]) or room_r >= P["min_room_r"]))
    for k, _ in OK_FLAGS:
        row[k] = bool(r[k])
    return row, d


def finalize(rows, universe, regime, P, mode, events=None):
    tbl = pd.DataFrame(rows)
    if tbl.empty:
        return tbl, tbl
    events = events or {}
    today = now_ist().date()
    smap = dict(zip(universe["Symbol"], universe.get("Sector", pd.Series(["Unknown"] * len(universe)))))
    tbl["Sector"] = tbl["Symbol"].map(smap).fillna("Unknown")
    sec = tbl[tbl["Sector"] != "Unknown"].groupby("Sector")["Ret"].median()
    sec_rank = sec.rank(pct=True) if len(sec) >= 3 else pd.Series(1.0, index=sec.index)
    tbl["SectorRank"] = tbl["Sector"].map(sec_rank).fillna(1.0).round(2)
    tbl["SectorOK"] = tbl["SectorRank"] >= 0.5
    sigs, blocked = [], []
    for _, r in tbl.iterrows():
        fails = [lab.format(**P) for k, lab in OK_FLAGS if not r[k]]
        if P["regime_on"] and not regime["ok"]:
            fails.append(f"Market {regime['state']}")
        if P["sector_on"] and not r["SectorOK"]:
            fails.append("Weak sector")
        if mode == "swing" and not r["ok_room"]:
            fails.append(f"Resistance {r['RoomR']}R (<{P['min_room_r']}R)")
        ev = [x for x in events.get(r["Symbol"], []) if 0 <= (x - today).days <= P["event_days"]]
        if mode == "swing" and ev:
            fails.append(f"Results {min(ev):%d-%b}")
        if r["Score"] >= P["score_buy"] and not fails:
            sigs.append("BUY")
        elif r["Score"] >= P["score_watch"]:
            sigs.append("WATCH")
        else:
            sigs.append("-")
        blocked.append(", ".join(fails) if r["Score"] >= P["score_buy"] else "")
    tbl["Signal"], tbl["Blocked"] = sigs, blocked
    full = tbl.copy()
    tbl = tbl[tbl["Signal"] != "-"].copy()
    tbl["_o"] = tbl["Signal"].map({"BUY": 0, "WATCH": 1})
    tbl = tbl.sort_values(["_o", "Score", "RS", "ADX"], ascending=[True, False, False, False]).drop(columns="_o")
    return tbl.reset_index(drop=True), full


def run_scan(fetch, universe, tokens, mode, interval, P, topn=250, progress=None, ttl=300, events=None):
    """fetch(token, interval, days) -> OHLCV DataFrame indexed by datetime."""
    swing = mode == "swing"
    nifty = cached(("nifty",), 600, lambda: fetch(NIFTY_TOKEN, "ONE_DAY", 400))
    nifty = drop_incomplete(nifty, mode)
    iv, days = ("ONE_DAY", 400) if swing else (interval, 12)
    mins = {"FIVE_MINUTE": 5, "FIFTEEN_MINUTE": 15, "THIRTY_MINUTE": 30}.get(iv, 0)
    uni = universe.head(topn)
    raw, missing = {}, []
    n = len(uni)
    for i, sym in enumerate(uni["Symbol"], 1):
        if progress:
            progress(i, n, sym)
        tok = tokens.get(sym)
        if not tok:
            missing.append(sym)
            continue
        try:
            df = cached((tok, iv, days), ttl, lambda: fetch(tok, iv, days))
            if not swing and mins:
                df = df[df.index + pd.Timedelta(minutes=mins) <= now_ist()]
            raw[sym] = drop_incomplete(df, mode)
        except Exception as e:  # noqa: BLE001
            missing.append(f"{sym} ({str(e)[:40]})")
    if swing:
        bench, src = build_bench(raw, nifty)
        regime = regime_info(pd.DataFrame({"close": bench}) if bench is not None else None)
    else:
        bench, src = None, "nifty"
        regime = regime_info(nifty)
    regime["src"] = src
    regime["breadth50"] = breadth(raw)
    asof = max((df.index[-1] for df in raw.values() if len(df)), default=None)
    res = dict(raw=raw, uni=uni, regime=regime, missing=missing, mode=mode, time=now_ist(), fetched=len(raw),
               bench=bench, events=events or {}, asof=asof)
    return rescore(res, P)


def rescore(res, P):
    """Filters / score settings badalne par dobara data fetch kiye bina result recompute."""
    rows, frames = [], {}
    for sym, df in res["raw"].items():
        row, d = analyse(sym, df, res["mode"], P, res.get("bench"))
        if row:
            rows.append(row)
            frames[sym] = d
    tbl, full = finalize(rows, res["uni"], res["regime"], P, res["mode"], res.get("events"))
    return {**res, "table": tbl, "full": full, "frames": frames, "scanned": len(rows)}


_CACHE = {}


def cached(key, ttl, fn):
    now = time.time()
    v = _CACHE.get(key)
    if v and now - v[0] < ttl:
        return v[1].copy()
    df = fn()
    _CACHE[key] = (now, df)
    return df.copy()


# --------------------------------------------------------------------------- backtest
def backtest_symbol(sym, df, mode, P, regime_ok=None, max_hold=10, cost_pct=0.25, bench=None, start_ts=None):
    swing = mode == "swing"
    d = add_indicators(df, mode, P, bench)
    sig = (d["score"] >= P["score_buy"]) & d["ok_all"]
    if P["regime_on"] and regime_ok is not None:
        sig &= pd.Series(regime_ok, index=d.index)
    sig = sig.fillna(False).values
    o, h, l, c = (d[k].values for k in ("open", "high", "low", "close"))
    a, idx, n = d["atr"].values, d.index, len(d)
    sc = d["score"].values
    day_last = None
    if mode == "intraday":
        codes = pd.factorize(idx.normalize())[0]
        day_last = pd.Series(np.arange(n)).groupby(codes).transform("max").values
    i = 210 if (swing and P["ema200_on"]) else 60
    if start_ts is not None:
        i = max(i, int(idx.searchsorted(start_ts)))
    trades = []
    while i < n - 1:
        if not sig[i] or np.isnan(a[i]):
            i += 1
            continue
        if mode == "intraday" and day_last[i + 1] != day_last[i]:
            i += 1
            continue
        entry = o[i + 1]
        risk = P["atr_mult"] * a[i]
        if risk <= 0 or entry <= 0:
            i += 1
            continue
        if swing and P["gap_skip_atr"] and entry > c[i] + P["gap_skip_atr"] * a[i]:   # gap-up: trade skip
            i += 1
            continue
        if swing and P["room_on"]:
            _, rs_ = sr_levels(d.iloc[:i + 1], c[i], a[i])
            if rs_ and (rs_[0] - c[i]) / risk < P["min_room_r"]:
                i += 1
                continue
        sl, tgt = entry - risk, (entry + P["rr"] * risk) if P["target_on"] else np.inf
        last = day_last[i + 1] if mode == "intraday" else min(i + 1 + max_hold, n - 1)
        exit_p, why, j = c[last], "TIME", last
        sl_cur, hi_c = sl, entry
        for k in range(i + 1, last + 1):
            tag = "TRAIL" if sl_cur > sl else "SL"
            if k > i + 1 and o[k] <= sl_cur:
                exit_p, why, j = o[k], tag, k
                break
            if l[k] <= sl_cur:
                exit_p, why, j = sl_cur, tag, k
                break
            if h[k] >= tgt:
                exit_p, why, j = tgt, "TARGET", k
                break
            if P["trail_on"]:
                hi_c = max(hi_c, c[k])
                if hi_c >= entry + risk:
                    sl_cur = max(sl_cur, hi_c - P["trail_mult"] * a[i])
        risk_pct = risk / entry * 100
        pnl = (exit_p / entry - 1) * 100 - cost_pct
        trades.append(dict(Symbol=sym, Score=int(sc[i]), Entry_time=idx[i + 1], Exit_time=idx[j], Entry=round(entry, 2),
                           Exit=round(exit_p, 2), Exit_reason=why, PnL_pct=round(pnl, 2),
                           R=round(pnl / risk_pct, 2), Bars=j - i))
        i = j + 1
    return trades


def _block(t, risk_pct):
    """Trades DataFrame -> (stats dict, equity Series)."""
    if t is None or len(t) == 0:
        return None, None
    t = t.sort_values("Exit_time").reset_index(drop=True)
    eq = (1 + t["R"] * risk_pct / 100).cumprod() * 100
    peak = eq.cummax()
    gp, gl = t.loc[t["R"] > 0, "R"].sum(), -t.loc[t["R"] <= 0, "R"].sum()
    st = dict(Trades=len(t), WinRate=round(float((t["R"] > 0).mean() * 100), 1), AvgR=round(float(t["R"].mean()), 2),
              ProfitFactor=round(float(gp / gl), 2) if gl > 0 else float("inf"),
              Return_pct=round(float(eq.iloc[-1] - 100), 1), MaxDD_pct=round(float((eq / peak - 1).min() * 100), 1),
              AvgBars=round(float(t["Bars"].mean()), 1))
    return st, pd.Series(eq.values, index=t["Exit_time"])


def backtest_stats(trades, risk_pct=1.0, max_pos=None, oos_frac=0.3):
    """Portfolio limit (max_pos ek saath open positions) + in-sample / out-of-sample split."""
    if not trades:
        return None
    t = pd.DataFrame(trades).sort_values(["Entry_time", "Score"], ascending=[True, False]).reset_index(drop=True)
    raw_n = len(t)
    if max_pos:
        keep, open_exits = [], []
        for r in t.itertuples():
            open_exits = [e for e in open_exits if e >= r.Entry_time]
            if len(open_exits) < max_pos:
                open_exits.append(r.Exit_time)
                keep.append(r.Index)
        t = t.loc[keep].reset_index(drop=True)
    if t.empty:
        return None
    stats, eq = _block(t, risk_pct)
    stats["Skipped_slots"] = raw_n - len(t)
    cut = t["Entry_time"].iloc[max(int(len(t) * (1 - oos_frac)) - 1, 0)]
    is_st, _ = _block(t[t["Entry_time"] <= cut], risk_pct)
    oos_st, _ = _block(t[t["Entry_time"] > cut], risk_pct)
    return dict(stats=stats, is_stats=is_st, oos_stats=oos_st, oos_from=cut,
                trades=t.sort_values("Exit_time").reset_index(drop=True), equity=eq)


def bt_fetch(fetch, symbols, tokens, mode, interval, days, progress=None):
    """Data ek baar fetch karo; phir bt_run / run_variants kai baar chala sakte ho."""
    swing = mode == "swing"
    warm = 330 if swing else 10          # EMA200 / RS warm-up
    nifty = fetch(NIFTY_TOKEN, "ONE_DAY", days + (warm if swing else 120))
    iv = "ONE_DAY" if swing else interval
    data, missing = {}, []
    for i, sym in enumerate(symbols, 1):
        if progress:
            progress(i, len(symbols), sym)
        tok = tokens.get(sym)
        if not tok:
            missing.append(sym)
            continue
        try:
            data[sym] = drop_incomplete(fetch(tok, iv, days + warm), mode)
        except Exception:  # noqa: BLE001
            missing.append(sym)
    if swing:
        bench, src = build_bench(data, drop_incomplete(nifty, mode))
    else:
        bench, src = nifty["close"], "nifty"
    flags = regime_flags(pd.DataFrame({"close": bench})) if bench is not None else None
    return dict(data=data, bench=bench if swing else None, flags=flags, src=src, missing=missing,
                cutoff=pd.Timestamp(now_ist()) - pd.Timedelta(days=days))


def bt_run(bundle, mode, P, max_hold=10, cost_pct=0.25, risk_pct=1.0, max_pos=5):
    all_t = []
    for sym, df in bundle["data"].items():
        try:
            reg = align_regime(bundle["flags"], df.index, mode) if bundle["flags"] is not None else None
            all_t += backtest_symbol(sym, df, mode, P, reg, max_hold, cost_pct, bundle["bench"], bundle["cutoff"])
        except Exception:  # noqa: BLE001
            continue
    out = backtest_stats(all_t, risk_pct, max_pos)
    if out:
        t = out["trades"]
        out["per_symbol"] = t.groupby("Symbol").agg(Trades=("R", "size"), WinRate=("R", lambda x: round((x > 0).mean() * 100, 1)),
                                                    AvgR=("R", "mean"), TotalR=("R", "sum")).round(2).sort_values("TotalR", ascending=False)
    return out


def run_variants(bundle, mode, P, variants=None, max_hold=10, cost_pct=0.25, risk_pct=1.0, max_pos=5, progress=None):
    """Har variant same data par chalao -> comparison table (OOS columns sabse zaroori hain)."""
    variants = variants or VARIANTS
    rows = []
    for i, (name, ov) in enumerate(variants.items(), 1):
        if progress:
            progress(i, len(variants), name)
        out = bt_run(bundle, mode, {**P, **ov}, max_hold, cost_pct, risk_pct, max_pos)
        if not out:
            rows.append(dict(Variant=name, Trades=0))
            continue
        s, o_ = out["stats"], out["oos_stats"] or {}
        rows.append(dict(Variant=name, Trades=s["Trades"], WinRate=s["WinRate"], AvgR=s["AvgR"], PF=s["ProfitFactor"],
                         Return_pct=s["Return_pct"], MaxDD_pct=s["MaxDD_pct"],
                         OOS_Trades=o_.get("Trades", 0), OOS_AvgR=o_.get("AvgR"), OOS_PF=o_.get("ProfitFactor")))
    return pd.DataFrame(rows)


def run_backtest(fetch, symbols, tokens, mode, interval, P, days, max_hold=10, cost_pct=0.25,
                 risk_pct=1.0, max_pos=5, progress=None):
    b = bt_fetch(fetch, symbols, tokens, mode, interval, days, progress)
    return bt_run(b, mode, P, max_hold, cost_pct, risk_pct, max_pos)


# --------------------------------------------------------------------------- charts + telegram
BG, FG, GREEN, RED, BLUE, AMBER = "#0e1117", "#e6e6e6", "#26a69a", "#ef5350", "#42a5f5", "#ffca28"


def make_chart(sym, d, row, mode, bars=None):
    n = bars or (90 if mode == "swing" else 130)
    v = d.tail(n)
    price, a = row["Entry"], float(d["atr"].iloc[-1])
    sup, res = sr_levels(d, price, a)
    x = np.arange(len(v))
    up = (v["close"] >= v["open"]).values
    col = [GREEN if u else RED for u in up]
    fig, (ax, axv) = plt.subplots(2, 1, figsize=(8, 6.4), sharex=True, facecolor=BG,
                                  gridspec_kw={"height_ratios": [4, 1], "hspace": 0.05})
    for a_ in (ax, axv):
        a_.set_facecolor(BG)
        a_.tick_params(colors=FG, labelsize=8)
        for s in a_.spines.values():
            s.set_color("#333")
        a_.grid(color="#222", lw=0.5)
    ax.vlines(x, v["low"], v["high"], color=col, lw=1)
    body = (v["close"] - v["open"]).abs().clip(lower=price * 0.0003)
    ax.bar(x, body, bottom=np.minimum(v["open"], v["close"]), color=col, width=0.65)
    f, s = (20, 50) if mode == "swing" else (9, 21)
    ax.plot(x, v["ema_f"], color=AMBER, lw=1.1, label=f"EMA{f}")
    ax.plot(x, v["ema_s"], color=BLUE, lw=1.1, label=f"EMA{s}")
    if mode == "intraday" and "vwap" in v:
        ax.plot(x, v["vwap"], color="#ab47bc", lw=1, ls=":", label="VWAP")
    xr = len(v) + 9

    def hl(y, color, label, ls="--", lw=1.0):
        ax.axhline(y, color=color, ls=ls, lw=lw, alpha=0.9)
        ax.text(xr, y, f"{label} {y:.2f}", color=color, fontsize=7.5, va="center", ha="right",
                bbox=dict(facecolor=BG, edgecolor="none", pad=1, alpha=0.85))
    for k, y in enumerate(sup):
        hl(y, "#66bb6a", f"S{k + 1}")
    for k, y in enumerate(res):
        hl(y, "#ff7043", f"R{k + 1}")
    hl(row["Entry"], BLUE, "Entry", "-", 1.2)
    hl(row["StopLoss"], RED, "SL", "-", 1.2)
    tg = row.get("Target")
    if tg is not None and not pd.isna(tg):
        hl(tg, GREEN, "TGT", "-", 1.2)
    lo = min(v["low"].min(), row["StopLoss"], *(sup or [1e12])) * 0.995
    hi = max(v["high"].max(), tg if tg is not None and not pd.isna(tg) else 0, *(res or [0])) * 1.005
    ax.set_ylim(lo, hi)
    ax.set_xlim(-1, xr + 1)
    axv.bar(x, v["volume"], color=col, width=0.65, alpha=0.8)
    ticks = np.linspace(0, len(v) - 1, 6).astype(int)
    fmt = "%d %b" if mode == "swing" else "%d %b %H:%M"
    axv.set_xticks(ticks)
    axv.set_xticklabels([v.index[t].strftime(fmt) for t in ticks])
    ax.legend(loc="upper left", fontsize=7, facecolor=BG, edgecolor="#333", labelcolor=FG)
    ax.set_title(f"{sym}  |  {row['Signal']} {row['Score']}/{SCORE_MAX}  |  RSI {row['RSI']}  ADX {row['ADX']}  "
                 f"({'Swing 1D' if mode == 'swing' else 'Intraday'})", color=FG, fontsize=10)
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=130, facecolor=BG, bbox_inches="tight")
    plt.close(fig)
    return buf.getvalue(), sup, res


def build_top5(res, n=5, allow=None, extra=None):
    """allow: sirf ye symbols; extra: {symbol: extra caption line} (jaise qty)."""
    t = res["table"]
    if t.empty:
        return []
    buys = t[t["Signal"] == "BUY"]
    if allow is not None:
        buys = buys[buys["Symbol"].isin(allow)]
    buys = buys.sort_values(["Score", "RS", "VolX"], ascending=False).head(n)
    swing = res["mode"] == "swing"
    out = []
    for k, (_, r) in enumerate(buys.iterrows(), 1):
        png, sup, rs = make_chart(r["Symbol"], res["frames"][r["Symbol"]], r, res["mode"])
        tgt = f" | Target {r['Target']}" if r["Target"] is not None and not pd.isna(r["Target"]) else " | Target: trail"
        cap = (f"🟢 BUY #{k}: {r['Symbol']}  (Score {r['Score']}/{SCORE_MAX})\n"
               f"Ref close {r['Entry']} | SL {r['StopLoss']}{tgt}\n"
               + (f"Kal OPEN par lo; open > {r['MaxEntry']} ho to SKIP\n" if swing else "")
               + f"Support: {', '.join(f'{x:.2f}' for x in sup) or '-'}\n"
               f"Resistance: {', '.join(f'{x:.2f}' for x in rs) or '-'}  (room {r['RoomR']}R)\n"
               f"ADX {r['ADX']} | RSI {r['RSI']} | Vol x{r['VolX']} | RS {r['RS']:+.1f}% | {r['Sector']}\n"
               + (f"{extra[r['Symbol']]}\n" if extra and r["Symbol"] in extra else "")
               + f"Why: {r['Reasons']}")
        out.append(dict(symbol=r["Symbol"], png=png, caption=cap))
    return out


def tg_send_text(token, chat_id, text):
    r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      data={"chat_id": chat_id, "text": text[:4000]}, timeout=30)
    return r.ok, r.text[:200]


def tg_send_photo(token, chat_id, png, caption):
    r = requests.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                      data={"chat_id": chat_id, "caption": caption[:1000]},
                      files={"photo": ("chart.png", png, "image/png")}, timeout=60)
    return r.ok, r.text[:200]


def regime_line(rg):
    src = {"universe": "Universe EW index", "nifty": "Nifty50"}.get(rg.get("src"), "Index")
    b = rg.get("breadth50")
    return f"Market regime ({src}): {rg['state']}" + (f" | Breadth>EMA50: {b}%" if b is not None and not pd.isna(b) else "")


def tg_send_top5(token, chat_id, res, items):
    head = (f"📊 Nifty250 {res['mode'].upper()} scan  {res['time']:%d-%b %H:%M}\n"
            f"{regime_line(res['regime'])}  |  {len(items)} naye BUY")
    tg_send_text(token, chat_id, head)
    ok = True
    for it in items:
        ok &= tg_send_photo(token, chat_id, it["png"], it["caption"])[0]
        time.sleep(0.6)
    return ok


# --------------------------------------------------------------------------- Angel One
class Angel:
    MAX_DAYS = {"ONE_DAY": 2000, "ONE_HOUR": 400, "THIRTY_MINUTE": 200, "FIFTEEN_MINUTE": 200,
                "FIVE_MINUTE": 100, "ONE_MINUTE": 30}

    def __init__(self, api_key, client_id, pin, totp_secret):
        self.api_key, self.client_id, self.pin, self.totp = api_key, client_id, pin, totp_secret
        self._last = 0.0
        self.login()

    def login(self):
        import pyotp
        from SmartApi import SmartConnect
        self.api = SmartConnect(api_key=self.api_key)
        data = self.api.generateSession(self.client_id, self.pin, pyotp.TOTP(self.totp).now())
        if not data or not data.get("status"):
            raise RuntimeError(f"Angel login failed: {data.get('message') if data else 'no response'}")

    def _wait(self):
        gap = 0.38 - (time.time() - self._last)
        if gap > 0:
            time.sleep(gap)
        self._last = time.time()

    def candles(self, token, interval, days, exchange="NSE"):
        to = now_ist()
        start = to - dt.timedelta(days=days)
        step = dt.timedelta(days=self.MAX_DAYS.get(interval, 100))
        parts, cur = [], start
        while cur < to:
            end = min(cur + step, to)
            parts.append(self._chunk(token, interval, cur, end, exchange))
            cur = end
        df = pd.concat([p for p in parts if p is not None and len(p)]) if parts else pd.DataFrame()
        if df.empty:
            raise RuntimeError("no candle data")
        return df[~df.index.duplicated()].sort_index()

    def _chunk(self, token, interval, frm, to, exchange):
        p = {"exchange": exchange, "symboltoken": str(token), "interval": interval,
             "fromdate": frm.strftime("%Y-%m-%d %H:%M"), "todate": to.strftime("%Y-%m-%d %H:%M")}
        for attempt in range(4):
            self._wait()
            try:
                res = self.api.getCandleData(p)
            except Exception:  # noqa: BLE001
                time.sleep(1.5)
                continue
            if res and res.get("status") and res.get("data"):
                df = pd.DataFrame(res["data"], columns=["ts", "open", "high", "low", "close", "volume"])
                df["ts"] = pd.to_datetime(df["ts"]).dt.tz_localize(None)
                return df.set_index("ts").astype(float)
            code = (res or {}).get("errorcode", "")
            if code in ("AG8001", "AG8002", "AB8050"):
                self.login()
            elif res and res.get("status") and not res.get("data"):
                return None
            else:
                time.sleep(1.5)
        return None


def load_tokens(path="tokens_nse.json"):
    """symbol -> Angel token for NSE equity (-EQ). Cached for the day."""
    if os.path.exists(path) and dt.date.fromtimestamp(os.path.getmtime(path)) == dt.date.today():
        return json.load(open(path))
    try:
        data = requests.get(SCRIP_URL, timeout=120).json()
        m = {x["name"]: x["token"] for x in data if x.get("exch_seg") == "NSE" and x.get("symbol", "").endswith("-EQ")}
        json.dump(m, open(path, "w"))
        return m
    except Exception:  # noqa: BLE001
        return json.load(open(path)) if os.path.exists(path) else {}


def load_universe(name=DEFAULT_UNIVERSE, fetch=True):
    """Index list + sector. Order: niftyindices live -> saved copy (repo me commit ki hui CSV) -> universe.csv seed.
    df.attrs['source'] tells which one was used."""
    url, path = UNIVERSES.get(name, UNIVERSES[DEFAULT_UNIVERSE])
    if fetch:
        try:
            r = requests.get(url, headers=UA, timeout=20)
            r.raise_for_status()
            u = pd.read_csv(io.StringIO(r.text)).rename(columns={"Industry": "Sector"})
            u = u[["Symbol", "Sector"]].dropna()
            u.to_csv(path, index=False)
            u.attrs["source"] = "live"
            return u
        except Exception:  # noqa: BLE001
            pass
    src = "saved" if os.path.exists(path) else "fallback"
    u = pd.read_csv(path if src == "saved" else "universe.csv").rename(columns={"Industry": "Sector"})
    if "Sector" not in u:
        u["Sector"] = "Unknown"
    u = u[["Symbol", "Sector"]].copy()
    u.attrs["source"] = src
    return u


# --------------------------------------------------------------------------- demo feed (offline test)
def demo_fetch(token, interval, days, exchange="NSE"):
    rng = np.random.default_rng(zlib.crc32(str(token).encode()))
    if interval == "ONE_DAY":
        idx = pd.bdate_range(end=pd.Timestamp(now_ist()).normalize(), periods=max(int(days * 0.68), 90))
    else:
        bars = {"FIVE_MINUTE": 75, "FIFTEEN_MINUTE": 25, "THIRTY_MINUTE": 13}.get(interval, 25)
        step = {"FIVE_MINUTE": 5, "FIFTEEN_MINUTE": 15, "THIRTY_MINUTE": 30}.get(interval, 15)
        ds = pd.bdate_range(end=pd.Timestamp(now_ist()).normalize(), periods=max(int(days * 0.68), 5))
        idx = pd.DatetimeIndex([d + pd.Timedelta(hours=9, minutes=15 + step * k) for d in ds for k in range(bars)])
    n = len(idx)
    drift = rng.normal(0.0004, 0.0003)
    ret = rng.normal(drift, 0.012 if interval == "ONE_DAY" else 0.003, n)
    c = 100 * rng.uniform(1, 20) * np.exp(np.cumsum(ret))
    o = np.r_[c[0], c[:-1]] * (1 + rng.normal(0, 0.002, n))
    h = np.maximum(o, c) * (1 + rng.uniform(0, 0.01, n))
    l = np.minimum(o, c) * (1 - rng.uniform(0, 0.01, n))
    v = rng.uniform(1e5, 5e5, n) * (1 + 2 * (rng.random(n) > 0.9))
    return pd.DataFrame(dict(open=o, high=h, low=l, close=c, volume=v), index=idx)
