# Binance USDT-M Futures Volume Scanner Bot

Real-time scanner for Binance perpetual futures. Detects breakout setups on 1h timeframe and sends alerts to Telegram.

---

## Signal Strategy

### 3 Main Conditions (ALL must pass to fire a signal)

**1. 24h High Breakout**
- Current 1h candle must close **above the highest high of the last 24 candles** (24h high)
- Confirms the coin is breaking resistance and starting a potential pump

**2. Consecutive Volume Increase**
- The last **3 consecutive 1h candles** must have strictly increasing volume
- e.g. $250K → $850K → $1.5M
- Confirms sustained buying pressure, not a one-candle fake spike

**3. 24h Price Change Cap**
- The coin's 24h price change must be **within ±20%**
- Avoids entering coins that already made a big move and may dump

If all 3 pass → signal fires and alert is sent to Telegram.

---

## Additional Data (collected at signal time — not filters)

These values are **not used to block signals**. They are collected and stored with every signal for future analysis, backtesting, and improving strategy accuracy.

| Field | Description |
|---|---|
| RVOL (20-candle) | Current candle volume vs 20-candle average |
| OI change % | Current OI vs 24h average OI |
| OI growth ratio | Current OI growth vs avg hourly OI growth |
| Funding rate | Current funding rate + whether in ideal range (-0.02% to 0.15%) |
| 24h volume | Total 24h volume + whether above $50M liquidity threshold |
| 4h EMA50 | Whether price is above or below 4h EMA50 + distance % |
| Volatility compression | Range of last 10 candles vs prior 10 (is the coin compressing before breakout?) |
| Breakout margin % | How far above 24h high the close is |

This data builds a dataset over time so you can answer: "which coins pumped most? what was OI at entry? was it compressed?"

---

## Price Tracking (post-signal)

After a signal fires, the bot tracks the coin for 7 days (configurable):

- **Peak price** — highest price reached since entry
- **Lowest price** — lowest price reached since entry
- **Take profit alerts** — at +5%, +10%, +15%, +20%
- **Reversal warning** — if price drops 5% from its peak (after reaching +3%)
- Price updates every 5 minutes

---

## Telegram Commands

| Command | Description |
|---|---|
| `/report` | All active signals with current P&L, peak |
| `/report XYZUSDT` | Detailed breakdown for one coin + additional data |
| `/summary` | Win rates, averages, best/worst across active + history |
| `/active` | Quick list of all currently tracked signals |
| `/detailed_report` | Sends a JSON file with all completed signals (≥7 days old). Contains every field: entry, peak, lowest, exit prices, all main criteria values, all additional data collected at signal time |
| `/help` | Command reference |

---

## Configuration (config.json)

| Key | Default | Description |
|---|---|---|
| `timeframe` | `1h` | Candle timeframe |
| `scan_interval_seconds` | `900` | How often to scan (15 min) |
| `breakout_lookback_candles` | `24` | How many candles define the "24h high" |
| `consecutive_vol_candles` | `3` | How many candles must be increasing |
| `max_price_change_24h_pct` | `20.0` | Max 24h price move allowed |
| `min_volume_usdt` | `0` | Optional: minimum current candle volume (0 = off) |
| `cooldown_hours` | `12` | No repeat alert for same coin within this window |
| `excluded_symbols` | `[USDCUSDT, BTCDOMUSDT]` | Symbols to skip |
| `max_age_hours` | `168` | Track signals for 7 days |
| `take_profit_targets` | `[5,10,15,20]` | TP alert levels in % |
| `detailed_report_min_age_hours` | `168` | Min age for signals to appear in /detailed_report |

---

## Deployment

Runs as a Python process. Requires:
- Python 3.11+
- `requests` package
- Telegram bot token + chat ID (set in config.json or environment)
- Optional: `PROXY_URL` environment variable for geo-restricted servers

Health check HTTP server runs on port 8080 for deployment probes.
