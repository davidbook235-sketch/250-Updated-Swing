# 📈 Nifty250 Scanner v2 (Angel One + Streamlit + Telegram)

Swing (1D) aur Intraday scanner. **Swing ke liye tuned** - intraday optional hai (API limit ki wajah se ~2 min/scan).

## v2 me kya badla
**Strategy**
- Score ab **4 alag signals** ka hai (EMA/RSI/MACD ek hi baat dohrate the): Trend, Momentum (RSI+MACD), Breakout, Volume.
  Default: **BUY = score 3/4 + Breakout zaroori**.
- Naye filters (sab ON/OFF): **Liquidity** (20-day avg turnover >= Rs2 Cr), **Price > EMA200**, **Relative strength**
  (60-day return market se behtar), **Extension limit** (close EMA20 se max 3 ATR door), **RSI < 80**,
  **Resistance tak min 1.5R room**, circuit-lock, split/bonus jump detection, **Results calendar**.
- **Market regime** ab universe ke apne equal-weight index + breadth (% stocks > EMA50) se - smallcap ke liye Nifty50 se sahi.
- SL default 2 ATR, **trailing stop** (+1R ke baad), gap-up entry skip (open > close + 0.5 ATR).

**Live use**
- Swing scan sirf **completed daily candle** par (16:00 IST workflow). Adhoori candle automatically hata di jati hai.
- Signal = aaj close par, **entry kal open par** (backtest bhi yahi maanta hai - live aur backtest ab match karte hain).
- **Position tracking + exit alerts** (SL / trailing SL / Target / 10-din time exit) - `positions.json` me paper positions.
- **Dedup**: ek stock ka alert 5 din me ek hi baar; active position wale stock par dobara alert nahi.
- **Max 5 positions** (MAX_POS) aur **1% risk/trade** ke hisaab se Qty alert me aati hai.

**Backtest**
- Portfolio limit (max positions), cost default 0.25%, trailing stop, gap-skip, in-sample vs **out-of-sample** (last 30%).
- **Compare variants** button: score 3 vs 4, ADX/regime/RS/extension on-off, SL 1.5/2/2.5, trailing, purani loose strategy - ek hi data par.
- Split/bonus jump (>30% ek din me) ke baad 60 din tak signal block.

## Pehle ye karo (order me)
1. **Stock list:** niftyindices.com se *Nifty Smallcap 250* CSV download karke repo me **`universe_smallcap250.csv`** naam se daalo
   (GitHub Actions par niftyindices aksar block hota hai, tab sirf 17 symbols ki backup list chalti hai - ab Telegram par warning aati hai).
2. App ke **Backtest tab** me 2-3 saal (750-1000 days), 60-100+ stocks par **Compare variants** chalao.
   Wahi variant rakho jiska **OOS_PF > ~1.3** ho aur trades 100+ hon.
3. Phir Telegram alerts par 3-4 hafte paper-trade karo, tab paise lagao.

## Mobile se setup (sab phone par)
1. GitHub → **New repository** (Private).
2. Files upload/create karo: `app.py`, `core.py`, `positions.py`, `run_scan.py`, `requirements.txt`, `universe.csv`,
   `results_calendar.csv`, `positions.json`, `.github/workflows/scan.yml` (aur step 1 wali universe CSV).
3. share.streamlit.io → **Create app** → Main file: `app.py`.
4. App → **Settings → Secrets**:
   ```toml
   ANGEL_API_KEY = "..."
   ANGEL_CLIENT_ID = "..."
   ANGEL_PIN = "..."
   ANGEL_TOTP_SECRET = "..."
   TELEGRAM_BOT_TOKEN = "..."
   TELEGRAM_CHAT_ID = "..."
   ```
5. Same keys GitHub repo → Settings → Secrets and variables → Actions → **Secrets** me daalo.
   Optional **Variables**: `CAPITAL` (default 100000), `RISK_PCT` (1), `MAX_POS` (5).
6. Repo → Settings → Actions → General → Workflow permissions → **Read and write** (positions.json commit ke liye).

## Results calendar (optional par recommended)
`results_calendar.csv` me `Symbol,Date` (YYYY-MM-DD) daalo. Results ke 5 din ke andar BUY block ho jata hai.
Dates NSE corporate-announcements page se milti hain - koi free reliable API nahi hai, isliye manual.

## Intraday
Workflow me intraday cron hai; sirf swing chahiye to scan.yml se wo `- cron: "*/30 4-9 * * 1-5"` line hata do.
Intraday me naye swing-filters (liquidity, EMA200, RS, extension, room) nahi lagte.

## Dhyan rakhne wali baatein
- **Keys kabhi code/CSV me commit mat karo.**
- Angel `getCandleData` ~3 req/sec: 250 stocks ka scan ~1.5-2 min. Historical API wali key chahiye ho sakti hai.
- Angel ke candles split/bonus adjust nahi karte - isliye jump-detection hai, par phir bhi corporate-action wale stocks par nazar rakho.
- Streamlit free app sleep ho jata hai - 24x7 alerts GitHub Actions se aate hain.
- Backtest survivorship-bias wala hai (aaj ki list par purana test) - result thoda optimistic maano.
- Positions paper-tracking hain (bot ke maane hue entry/exit). Asli trade me khud SL laga ke rakho.
- Ye educational tool hai, investment advice nahi.
