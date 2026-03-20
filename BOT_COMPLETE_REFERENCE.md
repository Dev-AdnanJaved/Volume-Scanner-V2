# Binance USDT-M Futures Volume Scanner Bot — Complete Reference

This document covers **every single thing** the bot does, from startup to final archival. Every field collected, every condition checked, every timing, every threshold, and every data flow is documented here.

---

## Table of Contents

1. [Startup & Configuration](#1-startup--configuration)
2. [Symbol Discovery](#2-symbol-discovery)
3. [Pre-Scan Data Fetch (Per Cycle)](#3-pre-scan-data-fetch-per-cycle)
4. [Signal Generation — 3 Mandatory Filters](#4-signal-generation--3-mandatory-filters)
5. [Additional Data Collected at Signal Time](#5-additional-data-collected-at-signal-time)
6. [Complete Signal Data Structure (What Gets Stored)](#6-complete-signal-data-structure-what-gets-stored)
7. [Telegram Alert Message (What Gets Sent)](#7-telegram-alert-message-what-gets-sent)
8. [Tracker — Continuous Price Monitoring](#8-tracker--continuous-price-monitoring)
9. [Outcome Block — Fields & When Each Is Updated](#9-outcome-block--fields--when-each-is-updated)
10. [Price Journey — Event-Based Snapshots](#10-price-journey--event-based-snapshots)
11. [Take-Profit Checking & TP Snapshot](#11-take-profit-checking--tp-snapshot)
12. [Reversal Warning](#12-reversal-warning)
13. [Signal Type Classification](#13-signal-type-classification)
14. [Archiving — After 168 Hours (7 Days)](#14-archiving--after-168-hours-7-days)
15. [Daily Report](#15-daily-report)
16. [Telegram Bot Commands](#16-telegram-bot-commands)
17. [CSV Export](#17-csv-export)
18. [Data Storage Files](#18-data-storage-files)
19. [Rate Limiting & API Details](#19-rate-limiting--api-details)
20. [Telegram Message Formats](#20-telegram-message-formats)
21. [Full config.json Reference](#21-full-configjson-reference)

---

## 1. Startup & Configuration

### What happens at startup (`main.py`)

1. **Load config** — reads `config.json` from project root
2. **Environment variable overrides** — if set, these override config.json values:
   - `TELEGRAM_BOT_TOKEN` → `config["telegram"]["bot_token"]`
   - `TELEGRAM_CHAT_ID` → `config["telegram"]["chat_id"]`
   - `BINANCE_API_KEY` → `config["binance"]["api_key"]`
   - `BINANCE_API_SECRET` → `config["binance"]["api_secret"]`
3. **Setup logging** — to both `stdout` and `scanner.log` file
4. **Validate config** — checks `bot_token` and `chat_id` are set (exits if missing or starts with `YOUR_`)
5. **Start health check server** — HTTP server on port `8080`, responds `200 OK` to any GET request
6. **Create shared components**:
   - `BinanceClient` — shared across scanner + tracker
   - `TelegramNotifier` — validates bot token via Telegram `getMe` API (exits if invalid)
   - `MarketCapProvider` — only created if `market_cap.enabled = true`

### 3 Threads launched

| Thread | Name | Type | What it does |
|--------|------|------|-------------|
| Scanner | `main thread` | Main | Scans all pairs every cycle (default 900s) |
| Tracker | `tracker` | Daemon | Updates prices every 300s, checks TPs, archives, daily reports |
| Command Listener | `commands` | Daemon | Listens for Telegram bot commands via long polling |

The tracker and command listener only start if `tracker.enabled = true` in config.

### Graceful shutdown

Handles `SIGINT` and `SIGTERM` — calls `stop()` on scanner, tracker, and command listener. Each component has a `_running` flag that breaks their loops within 1 second.

---

## 2. Symbol Discovery

### When it happens
At the **start of every scan cycle**, the scanner calls `get_usdt_perpetual_symbols()`.

### How it works
- **API endpoint**: `GET /fapi/v1/exchangeInfo` (weight: 1)
- **Cached for**: 300 seconds (TTL). If called again within 300s, returns cached list.
- **Filters applied** (all 3 must pass):
  1. `quoteAsset == "USDT"`
  2. `contractType == "PERPETUAL"`
  3. `status == "TRADING"`
- **Fields returned per symbol**: `symbol` (e.g., "BTCUSDT"), `base_asset` (e.g., "BTC")
- **Typical count**: ~542 pairs

### Symbol exclusion (3 layers)
Before scanning each symbol, 3 checks eliminate it:

| Check | Source | Default | Why |
|-------|--------|---------|-----|
| Excluded list | `scanner.excluded_symbols` in config | `["USDCUSDT", "BTCDOMUSDT"]` | Permanently excluded pairs |
| Already tracked | `tracker.get_tracked_symbols()` | Dynamically computed | Prevents duplicate signals for already-active pairs |
| Cooldown | `_CooldownTracker` | 168 hours in config.json (code fallback: 12h if key missing) | After a signal fires, the same symbol cannot fire again for this duration |

### Cooldown mechanics
- When a signal fires for a symbol, `_CooldownTracker.record(symbol)` stores `time.time()`
- `is_on_cooldown(symbol)` checks if `current_time - last_alert_time < cooldown_seconds`
- `prune()` clears expired entries after each cycle
- Cooldown period = `scanner.cooldown_hours` × 3600 seconds

---

## 3. Pre-Scan Data Fetch (Per Cycle)

Before scanning individual symbols, two bulk API calls are made once per cycle:

### 3a. Mark Prices
- **When**: Start of every scan cycle
- **API endpoint**: `GET /fapi/v1/premiumIndex` (weight: 1)
- **What it returns**: Dictionary of `{symbol: mark_price}` for ALL futures symbols
- **Used for**: Getting the current price of each symbol (used in the alert as the signal entry price) and BTCUSDT price for BTC context
- **Failure behavior**: Logs warning, continues with empty dict — mark prices are best-effort

### 3b. 24h Tickers
- **When**: Start of every scan cycle
- **API endpoint**: `GET /fapi/v1/ticker/24hr` (weight: 40)
- **What it returns per symbol**:
  - `price_change_pct` — 24h price change percentage
  - `quote_volume_24h` — 24h volume in USDT
  - `volume_24h` — 24h volume in base coin units
  - `high_price` — 24h high price
- **Used for**: Filter 3 (24h price change check) and additional data (liquidity check)
- **Failure behavior**: Logs warning, continues with empty dict

---

## 4. Signal Generation — 3 Mandatory Filters + Hard Filters + Soft Flags + Quality Score

For each non-excluded, non-tracked, non-cooldown symbol, the scanner runs `_analyse()`. ALL 3 main filters + ALL 4 hard filters must pass, and soft flags must be < threshold for a signal to fire.

### Data fetched per symbol
- **API call**: `GET /fapi/v1/klines` with `symbol`, `interval="1h"`, `limit=max(25, 3, 20) + 2`
- **Weight**: 1 (if limit ≤ 100, else 2)
- **Processing**: Only closed candles are kept (candles where `close_time <= now`). The still-open candle is excluded.
- **Each candle contains**: `open_time`, `open`, `high`, `low`, `close`, `volume` (base), `close_time`, `quote_volume` (USDT volume), `trades` (number of trades)

### Filter 1: 24h High Breakout

**Condition**: The last closed 1h candle's `close` price must be **strictly above** the highest `high` of the previous 24 closed candles.

- **Lookback window**: `breakout_lookback_candles` (default: 24)
- **Candles used**: `candles[-(lookback+1) : -1]` — the 24 candles BEFORE the last one
- **High calculated as**: `max(c["high"] for c in lookback_candles)`
- **Check**: `last["close"] > high_24h` — must be strictly greater, not equal
- **On pass**: calculates `breakout_margin_pct = ((close - high_24h) / high_24h) * 100`
- **High breakout warning**: If `breakout_margin_pct > high_breakout_warning_pct` (default: 5.0%), sets `high_breakout_warning = True`. This is a caution flag — the signal still fires, but the alert gets a warning header.

**Why this filter**: Detects price breaking out of a 24-hour range, a key breakout signal.

### Filter 2: Consecutive Volume Increase

**Condition**: The last 3 closed 1h candles must have **strictly increasing** `quote_volume` (USDT volume), AND the ratio of newest to oldest must be ≥ 2.0x.

- **Candles used**: `candles[-3:]` (the last 3 closed candles)
- **Strictly increasing check**: `candle[i].quote_volume > candle[i-1].quote_volume` for each consecutive pair
- **Ratio check**: `vol_ratio = last_candle.quote_volume / first_candle.quote_volume >= consecutive_vol_min_ratio` (default: 2.0)
- **Volume type**: `quote_volume` = USDT volume (dollars flowing in), NOT base volume

**Fields recorded on pass**:
| Field | What it is |
|-------|-----------|
| `vol_candle_1` | Raw USDT volume of candle [-3] (oldest) |
| `vol_candle_2` | Raw USDT volume of candle [-2] (middle) |
| `vol_candle_3` | Raw USDT volume of candle [-1] (newest) |
| `vol_candle_1_fmt` | Formatted USDT string (e.g., "$4.52M") |
| `vol_candle_2_fmt` | Formatted USDT string |
| `vol_candle_3_fmt` | Formatted USDT string |
| `vol_candle_1_base` | Raw base (coin) volume of candle [-3] |
| `vol_candle_2_base` | Raw base (coin) volume of candle [-2] |
| `vol_candle_3_base` | Raw base (coin) volume of candle [-1] |
| `vol_candle_1_base_fmt` | Formatted coin volume (e.g., "1.20M") |
| `vol_candle_2_base_fmt` | Formatted coin volume |
| `vol_candle_3_base_fmt` | Formatted coin volume |
| `vol_ratio` | Rounded to 2 decimals (e.g., 3.41) |

**Why this filter**: Ensures money is accelerating into the asset — not just a spike but a building trend.

### Filter 3: 24h Price Change Cap

**Condition**: The 24h price change percentage (from the pre-fetched 24h ticker) must be within ±20%.

- **Value source**: `ticker.get("price_change_pct", 0)` from the bulk 24h ticker fetch
- **Check**: `abs(price_chg_24h) <= max_price_change_24h_pct` (default: 20.0)

**Why this filter**: Eliminates coins that have already pumped too much (chasing) or are in freefall (catching knives).

### Optional: Minimum Volume Floor

- **Condition**: If `min_volume_usdt > 0` (default: 0 = disabled), the last candle's `quote_volume` must exceed this threshold
- **When 0**: This check is completely skipped

### Hard Filters (after main criteria pass, before signal fires)

After the 3 main filters pass, additional data is collected (Section 5), then **all 4 hard filters** must also pass. Failing any one blocks the signal entirely. All thresholds are configurable in `config.json` under `scanner.hard_filters`.

| # | Field | Condition | Config key | Default |
|---|-------|-----------|------------|---------|
| 4 | `vol_ratio` | ≤ threshold | `vol_ratio_max` | 15 |
| 5 | `funding_rate` (from additional_data) | > threshold | `funding_rate_min` | -0.05 |
| 6 | `vol_24h_usdt` (from additional_data) | > threshold | `vol_24h_usdt_min` | 5,000,000 |
| 7 | `market_cap_usd` (from additional_data) | < threshold | `market_cap_usd_max` | 1,000,000,000 |

- If `funding_rate`, `vol_24h_usdt`, or `market_cap_usd` is `None` (data fetch failed), the hard filter for that field is **skipped** (signal not blocked).

### Soft Flags (second gate — 5+ flags blocks signal)

After hard filters pass, each condition below is checked. Each true condition adds 1 flag. If total flags ≥ `max_flags_to_block` (default: 5), the signal is blocked. All thresholds are configurable in `config.json` under `scanner.soft_flags`.

| # | Field | Condition for flag | Config key | Default |
|---|-------|--------------------|------------|---------|
| 1 | `breakout_margin_pct` | > threshold | `breakout_margin_pct_max` | 1.5% |
| 2 | `price_change_24h` | abs() > threshold | `price_change_24h_max` | 5.0% |
| 3 | `ema50_distance_pct` | > threshold | `ema50_distance_pct_max` | 8.0% |
| 4 | `vol_ratio` | > threshold | `vol_ratio_max` | 10 |
| 5 | `vol_24h_usdt` | < threshold | `vol_24h_usdt_min` | 10,000,000 |
| 6 | `oi_change_pct` | > threshold | `oi_change_pct_max` | 12.0% |
| 7 | `funding_rate` | < threshold | `funding_rate_min` | -0.01 |

- If a field is `None` (data unavailable), that flag is **not counted**.
- Recorded in signal JSON as `soft_flags` (int count) and `soft_flag_details` (list of triggered flag descriptions).

### Quality Score (0–8 points)

Each condition true = +1 point. Score tells you how clean the setup is. Does not block signals — purely informational for position sizing and exit planning. All thresholds are configurable in `config.json` under `scanner.quality_score`.

| # | Field | Condition for +1 point | Config key | Default |
|---|-------|-----------------------|------------|---------|
| 1 | `vol_ratio` | ≤ threshold | `vol_ratio_max` | 10 |
| 2 | `funding_rate` | ≥ threshold | `funding_rate_min` | 0 |
| 3 | `price_change_24h` | abs() ≤ threshold | `price_change_24h_max` | 5.0% |
| 4 | `breakout_margin_pct` | ≤ threshold | `breakout_margin_pct_max` | 1.5% |
| 5 | `oi_change_pct` | ≤ threshold | `oi_change_pct_max` | 8.0% |
| 6 | `ema50_distance_pct` | ≤ threshold | `ema50_distance_pct_max` | 5.0% |
| 7 | `market_cap_usd` | between min–max | `market_cap_usd_min` / `market_cap_usd_max` | $10M–$500M |
| 8 | `vol_24h_usdt` | ≥ threshold | `vol_24h_usdt_min` | 10,000,000 |

- Recorded in signal JSON as `quality_score` (int 0–8) and `quality_details` (list of field names that earned points).

### Signal Flow Summary

```
3 Main Filters pass
       │
       ├── Collect additional_data
       │
       ├── Any 1 of 4 Hard Filters fail? ──► BLOCK
       │
       ├── Count soft flags (0–7)
       │        ├── 5+ flags ──► BLOCK
       │
       └── Calculate quality score (0–8)
                 └── Signal fires with score + flags recorded
```

---

## 5. Additional Data Collected at Signal Time

After ALL 3 main filters pass, the scanner collects extra context data. Each piece is wrapped in its own `try/except` — **failure of any piece does NOT block the signal from firing**. This data is used by hard filters, soft flags, and quality scoring, and is recorded for analysis.

### 5a. RVOL vs 20-Candle Baseline

**When collected**: At signal time in `_collect_additional()`

| Field | Type | Description |
|-------|------|-------------|
| `rvol_20` | float | Last candle's quote_volume ÷ average of prior 20 candles' quote_volume. E.g., 4.1 means 4.1x the average |
| `vol_baseline_avg` | float | Average USDT volume of the 20 candles used as baseline |

**Candles used**: `candles[-(20+1) : -1]` — the 20 candles before the last one.

**Why**: Shows how unusual the current volume is relative to the recent norm. RVOL 4.0 = 4x normal volume.

### 5b. Open Interest (OI)

**When collected**: At signal time in `_collect_additional()`
**API call**: `GET /futures/data/openInterestHist` with `symbol`, `period="1h"`, `limit=25` (weight: 1)

| Field | Type | Description |
|-------|------|-------------|
| `oi_current_usdt` | float | Latest OI value in USDT |
| `oi_avg_24h_usdt` | float | Average OI over prior 24 periods |
| `oi_change_pct` | float | `((current - avg) / avg) * 100` — how much OI deviates from 24h average |
| `oi_growth_current` | float | Latest OI change (current period - previous period) in USDT |
| `oi_growth_avg` | float | Average OI change over prior periods |
| `oi_growth_ratio` | float | `current_growth / abs(avg_growth)` — how the current OI growth compares to average growth |

**Requires**: At least 2 OI data points for basic fields, at least 3 for growth fields.

**Why**: Rising OI alongside rising price confirms new money entering — stronger trend signal.

### 5c. Funding Rate

**When collected**: At signal time in `_collect_additional()`
**API call**: `GET /fapi/v1/premiumIndex` with `symbol` (weight: 1)

| Field | Type | Description |
|-------|------|-------------|
| `funding_rate` | float | Current funding rate × 100 (expressed as percentage, e.g., 0.01 = 0.01%) |
| `funding_in_ideal_range` | bool | `True` if funding rate is between -0.02% and 0.15% inclusive |

**Why**: Extreme funding rates indicate crowded positioning. Ideal range means the market isn't overheated.

### 5d. 24h Volume Liquidity

**When collected**: At signal time in `_collect_additional()`
**Source**: Pre-fetched 24h ticker data (no extra API call)

| Field | Type | Description |
|-------|------|-------------|
| `vol_24h_usdt` | float | Total 24h trading volume in USDT |
| `vol_24h_base` | float | Total 24h trading volume in base coin units |
| `vol_24h_above_50m` | bool | `True` if 24h USDT volume ≥ $50,000,000 |

**Why**: Low-liquidity coins have wider spreads and higher slippage risk.

### 5e. 4h EMA50

**When collected**: At signal time in `_collect_additional()`
**API call**: `GET /fapi/v1/klines` with `symbol`, `interval="4h"`, `limit=55` (weight: 1)

| Field | Type | Description |
|-------|------|-------------|
| `ema50_4h` | float | 50-period EMA on 4h candle closes (8 decimal places) |
| `price_above_ema50_4h` | bool | `True` if current price > EMA50 |
| `ema50_distance_pct` | float | `((price - ema50) / ema50) * 100` — how far price is from EMA50 |

**EMA calculation**: Standard exponential moving average with `k = 2 / (period + 1)`. Seeded with simple average of first `period` values.

**Why**: Price above the 4h EMA50 confirms the broader trend is bullish. Distance shows how extended the move is.

### 5f. Volatility Compression

**When collected**: At signal time in `_collect_additional()`
**Source**: Already-fetched 1h candles (no extra API call)

| Field | Type | Description |
|-------|------|-------------|
| `volatility_recent_10_pct` | float | Average `(high - low) / close * 100` of last 10 candles |
| `volatility_prior_10_pct` | float | Average `(high - low) / close * 100` of candles [-20:-10] |
| `volatility_compression_ratio` | float | `recent / prior` — how recent volatility compares to prior |
| `is_compressed` | bool | `True` if compression_ratio < 0.7 |

**Requires**: At least 20 candles available.

**Why**: Volatility compression (ratio < 0.7) often precedes explosive breakouts — the spring is coiled.

### 5g. Market Cap (Optional)

**When collected**: At signal time in `_collect_additional()` — only if `market_cap.enabled = true`
**Source**: CoinGecko free API, cached in `MarketCapProvider`

| Field | Type | Description |
|-------|------|-------------|
| `market_cap_usd` | float or None | Market cap in USD |
| `market_cap_fmt` | string | Formatted (e.g., "$1.25B", "$450.00M", "Unknown") |

**CoinGecko caching**:
- Fetches top 3000 coins (12 pages × 250 per page)
- Cache TTL: `cache_minutes` (default: 120 minutes)
- Page delay: 20 seconds between pages (CoinGecko free-tier rate limit safe)
- Normalises Binance multiplier prefixes: `1000PEPE` → `PEPE`, `10000SATS` → `SATS`
- On refresh failure: keeps previous cache if available

**Why**: Market cap helps categorize signals into large-cap (safer) vs small-cap (higher risk/reward).

---

## 6. Complete Signal Data Structure (What Gets Stored)

When a signal passes all 3 filters, a signal record is created and saved to `data/signals.json`. Here is every single field:

### Root-level fields (stored directly on the signal object)

| Field | Type | Set When | Description |
|-------|------|----------|-------------|
| `symbol` | string | Signal creation | e.g., "ETHUSDT" |
| `entry_price` | float | Signal creation | Mark price at signal time (8 decimal precision) |
| `highest_price` | float | Signal creation, updated every 5 min | Highest mark price seen since entry |
| `lowest_price` | float | Signal creation, updated every 5 min | Lowest mark price seen since entry |
| `current_price` | float | Signal creation, updated every 5 min | Latest mark price |
| `alert_time_ts` | float | Signal creation | Unix timestamp (seconds) — set by the tracker at `record_signal()` time (its own `time.time()`), not the scanner's timestamp |
| `alert_time` | string | Signal creation | Human-readable UTC time, e.g., "2026-03-15 08:00:00 UTC" — set by tracker at `record_signal()` time |
| `timeframe` | string | Signal creation | Always "1h" |
| `price_change_24h` | float | Signal creation | 24h price change % from ticker (e.g., 5.2) |
| `breakout_margin_pct` | float | Signal creation | How far above 24h high the close was (e.g., 1.85) |
| `high_breakout_warning` | bool | Signal creation | `True` if breakout margin > 5% |
| `high_24h` | float | Signal creation | The 24h high price that was broken |
| `vol_candle_1` | float | Signal creation | Raw USDT volume of candle [-3] |
| `vol_candle_2` | float | Signal creation | Raw USDT volume of candle [-2] |
| `vol_candle_3` | float | Signal creation | Raw USDT volume of candle [-1] |
| `vol_candle_1_fmt` | string | Signal creation | Formatted USDT, e.g., "$4.52M" |
| `vol_candle_2_fmt` | string | Signal creation | Formatted USDT |
| `vol_candle_3_fmt` | string | Signal creation | Formatted USDT |
| `vol_candle_1_base` | float | Signal creation | Raw base (coin) volume of candle [-3] |
| `vol_candle_2_base` | float | Signal creation | Raw base (coin) volume of candle [-2] |
| `vol_candle_3_base` | float | Signal creation | Raw base (coin) volume of candle [-1] |
| `vol_candle_1_base_fmt` | string | Signal creation | Formatted coin volume, e.g., "1.20M" |
| `vol_candle_2_base_fmt` | string | Signal creation | Formatted coin volume |
| `vol_candle_3_base_fmt` | string | Signal creation | Formatted coin volume |
| `vol_ratio` | float | Signal creation | newest_vol / oldest_vol, e.g., 3.41 |
| `candle_colors` | list[string] | Signal creation | `["green", "red", "green"]` — color of each of the 3 volume candles. green = close ≥ open, red = close < open |
| `rvol` | float | Signal creation | RVOL calculated in scanner (same as additional_data.rvol_20 but calculated separately) |
| `btc_price` | float or None | Signal creation | BTCUSDT mark price at signal time. Used as BTC reference point for all future BTC comparisons |
| `candle_time` | string | Signal creation | Open time of the last closed candle, e.g., "2026-03-15 07:00 UTC" |
| `soft_flags` | int | Signal creation | Number of soft flags triggered (0–7) |
| `soft_flag_details` | list[string] | Signal creation | Descriptions of each triggered flag |
| `quality_score` | int | Signal creation | Quality score (0–8), higher = cleaner setup |
| `quality_details` | list[string] | Signal creation | Field names that earned quality points |
| `additional_data` | dict | Signal creation | All additional context data (see Section 5) |
| `tp_sent` | list[int] | Signal creation (empty), updated on TP hits | List of TP targets already sent, e.g., `[5, 10, 20]` |
| `reversal_warned` | bool | Signal creation (false), set to true once | Whether a reversal warning has been sent |
| `outcome` | dict | Signal creation (initialized), updated every 5 min | Detailed outcome tracking (see Section 9) |
| `price_journey` | list[dict] | Signal creation (empty), updated on events | Event-based price snapshots (see Section 10) |
| `_prev_highest` | float | Updated every 5 min | Internal tracking field — previous cycle's highest (stripped at archive) |
| `_prev_lowest` | float | Updated every 5 min | Internal tracking field — previous cycle's lowest (stripped at archive) |
| `last_update_ts` | float | Updated every 5 min | Timestamp of last price update (stripped in CSV export) |

### Fields added at TP hits (stored at signal root level)

| Field | Type | Set When |
|-------|------|----------|
| `tp5_snapshot` | dict | When +5% TP is first hit |
| `tp10_snapshot` | dict | When +10% TP is first hit |
| `tp20_snapshot` | dict | When +20% TP is first hit |
| `tp30_snapshot` | dict | When +30% TP is first hit |
| `tp50_snapshot` | dict | When +50% TP is first hit |
| `tp75_snapshot` | dict | When +75% TP is first hit |
| `tp100_snapshot` | dict | When +100% TP is first hit |

Each snapshot contains a full market re-scan — see Section 11 for all fields inside.

### Fields added at archive time (after 168 hours)

| Field | Type | Description |
|-------|------|-------------|
| `archived_time_ts` | float | Unix timestamp when archived |
| `archived_time` | string | "2026-03-22 08:00:00 UTC" |
| `tracked_hours` | float | Total hours tracked (e.g., 168.5) |
| `peak_pct` | float | `((highest - entry) / entry) * 100` |
| `lowest_pct` | float or None | `((lowest - entry) / entry) * 100` |
| `exit_pct` | float | `((current - entry) / entry) * 100` at archive time |
| `exit_price` | float | Current price at archive time |
| `highest_pct` | float | Same as peak_pct (duplicate for backward compat) |
| `market_cap_usd_exit` | float or None | Market cap at archive time (if enabled) |
| `market_cap_exit_fmt` | string | Formatted market cap at archive (if enabled) |

---

## 7. Telegram Alert Message (What Gets Sent)

When a signal fires, a Telegram message is immediately sent. The format:

**Normal signal**:
```
🚨 BREAKOUT SIGNAL
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📌 ETHUSDT  |  1h
💵 Price:  $3500.12345678

1️⃣ Breakout:  +1.85% above 24h high
2️⃣ Vol USDT:  $2.50M → $4.00M → $8.20M  (3.3x avg)
    Vol ETH:  720 → 1K → 2K
3️⃣ 24h Change:  🟢 +5.2%

⭐ Quality:  6/8  🟡 NORMAL
🚩 Flags:  1/7  (vol_24h $8.50M<$10.00M)

🕐 Time:  2026-03-15 08:00:00 UTC
⏱ Cooldown:  168h
```

**Quality grade labels**:
- 8/8: 🟢 EXCELLENT
- 7/8: 🟢 STRONG
- 5–6/8: 🟡 NORMAL
- 3–4/8: 🟠 WEAK
- ≤ 2/8: 🔴 POOR

**High breakout warning** (breakout margin > 5%):
```
⚠️ BREAKOUT SIGNAL — HIGH BREAKOUT
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📌 ETHUSDT  |  1h
💵 Price:  $3500.12345678

1️⃣ Breakout:  +7.50% above 24h high
2️⃣ Vol USDT:  $2.50M → $4.00M → $8.20M  (3.3x avg)
    Vol ETH:  720 → 1K → 2K
3️⃣ 24h Change:  🟢 +5.2%

⚠️ Warning: Breakout margin 7.50% > 5% — enter with caution

⭐ Quality:  3/8  🟠 WEAK
🚩 Flags:  3/7  (brk_margin 7.50%>1.5%, 24h_chg 5.2%>5.0%, ema50_dist 9.1%>8.0%)

🕐 Time:  2026-03-15 08:00:00 UTC
⏱ Cooldown:  168h
```

**Entry price formatting**: The signal alert always shows the price as a fixed 8-decimal string (e.g., `$3500.12345678`) or `$N/A` if unavailable. This is the raw mark price string from the scanner.

**TP/Reversal price formatting** (tiered, used in TP hit and reversal alerts only):
- `>= $1000`: `$3,500.12`
- `>= $1`: `$3.5012`
- `>= $0.001`: `$0.003512`
- `< $0.001`: `$0.00351234`

**After sending**: 0.3 second delay before scanning the next symbol (rate limit courtesy).

---

## 8. Tracker — Continuous Price Monitoring

### What it is
A background thread that runs continuously, updating prices and checking conditions every 300 seconds (5 minutes).

### The tracker loop (every 300 seconds)

Each cycle of `tracker.run()` does these 4 things in order:

| Step | Method | What happens |
|------|--------|-------------|
| 1 | `fetch_and_apply()` | Fetches mark prices for all symbols, updates every signal's current/highest/lowest price, updates outcome + journey |
| 2 | `_check_take_profits()` | Checks if any signal's highest_price has crossed a TP target, sends alerts, builds snapshots |
| 3 | `archive_expired()` | Removes signals older than 168h, writes to monthly gzip archive |
| 4 | `_check_daily_report()` | At midnight UTC, sends the daily report of expired signals |

### Step 1: Price updates (`apply_prices`)

For every active signal in `data/signals.json`:

1. **Fetch**: `GET /fapi/v1/premiumIndex` (weight: 1) — all mark prices in one call
2. **Update fields**:
   - `current_price` = latest mark price
   - `highest_price` = `max(current, previous_highest)`
   - `lowest_price` = `min(current, previous_lowest)` (only if lowest > 0)
   - `last_update_ts` = current timestamp
3. **Call `_update_outcome()`** — updates the outcome block (see Section 9)
4. **Call `_record_journey_snapshot()`** — checks for journey events (see Section 10)
5. **Save**: If any signal changed, write all signals back to `data/signals.json`

---

## 9. Outcome Block — Fields & When Each Is Updated

The outcome block tracks detailed performance metrics. It is initialized when a signal is created and updated every 5 minutes during the tracker's price update cycle.

### Outcome fields

| Field | Type | Default | When Set/Updated | Description |
|-------|------|---------|-----------------|-------------|
| `max_drawdown_pct` | float | 0.0 | Every 5 min | Most negative `((current - entry) / entry) * 100` ever seen. Always ≤ 0. |
| `max_drawdown_time` | string | None | When max_drawdown_pct worsens | UTC timestamp of the worst drawdown moment |
| `max_drawdown_hours_after_entry` | float | None | When max_drawdown_pct worsens | Hours after entry when worst drawdown occurred |
| `peak_pct` | float | 0.0 | Every 5 min | Highest `((highest - entry) / entry) * 100` ever seen |
| `peak_time` | string | None | When peak_pct improves | UTC timestamp of new peak |
| `peak_hours_after_entry` | float | None | When peak_pct improves | Hours after entry when peak occurred |
| `went_negative_before_tp` | bool | False | Every 5 min | Set to `True` if price goes below entry AND no TP target has been hit yet |
| `hours_negative_total` | float | 0.0 | Every 5 min | Cumulative hours the price has been below entry. Incremented by the time delta since last update when current price < entry. |
| `signal_type` | string | "active" | Every 5 min + at archive | Classification: "active", "fast", "slow", "delayed", or "failed" (see Section 13) |
| `signal_closed` | bool | False | At archive only | Set to `True` when signal is archived |
| `close_reason` | string | None | At archive only | Always "expired" (currently the only close reason) |
| `close_time` | string | None | At archive only | UTC timestamp of archive |
| `btc_change_entry_to_tp` | float | None | On FIRST TP hit only | `((btc_now - btc_entry) / btc_entry) * 100` — BTC's % change from signal entry to first TP hit. Only set once, never updated. |
| `btc_trend_during_signal` | string | None | At archive only | Uses BTC price at first TP hit (from `tp{N}_btc_price_at_hit`) vs entry. If no TP was ever hit (failed signal), falls back to live BTC at archive time. "pumping" if change > +2%, "dumping" if < -2%, "ranging" if between. |

### Per-TP-target fields (for each of [5, 10, 20, 30, 50, 75, 100])

| Field Pattern | Type | Default | When Set |
|--------------|------|---------|----------|
| `tp{N}_hit` | bool | False | When highest_price crosses +N% above entry |
| `tp{N}_hit_time` | string | None | When TP first hit — UTC timestamp |
| `tp{N}_hit_hours_after_entry` | float | None | When TP first hit — hours since signal creation |
| `tp{N}_max_drawdown_before` | float | None | When TP first hit — the max_drawdown_pct at that moment |
| `tp{N}_btc_price_at_hit` | float | None | When TP first hit — BTCUSDT price at that moment |

**Example**: When price reaches +10% above entry, the outcome will have:
- `tp10_hit = True`
- `tp10_hit_time = "2026-03-15 14:00:00 UTC"`
- `tp10_hit_hours_after_entry = 6.0`
- `tp10_max_drawdown_before = -2.5`
- `tp10_btc_price_at_hit = 87500.0`

### Backfill mechanism (`_ensure_outcome`)

If TP targets change in config (e.g., old `[5, 10, 15, 20]` → new `[5, 10, 20, 30, 50, 75, 100]`), the `_ensure_outcome` method automatically adds missing TP fields with default values (hit=False, times=None) when the signal is next processed. This prevents KeyErrors on config changes.

---

## 10. Price Journey — Event-Based Snapshots

The price journey is NOT recorded every 5 minutes. It only records snapshots when significant **events** happen. This keeps the data concise and meaningful.

### Event types (6 total)

| Event | Condition | Why it matters |
|-------|-----------|---------------|
| `new_high` | `current > _prev_highest` (where `_prev_highest` is the highest price at the time of the LAST journey snapshot, not the last 5-min update) | Price is making new highs — trend continuation |
| `new_low` | `current < _prev_lowest` (where `_prev_lowest` is the lowest price at the time of the LAST journey snapshot; and lowest > 0) | Price is making new lows — trend deterioration |
| `below_entry` | Price drops below entry from above (cross detection) | Critical moment — signal is now underwater |
| `btc_move` | BTC has moved ≥ 2% since the last recorded BTC snapshot | Market context shift |
| `4h_checkpoint` | 4+ hours since the last `4h_checkpoint` event | Regular heartbeat for time-series analysis |
| `tp_hit_{N}` | Take-profit target N% hit | Milestone event (added via `_add_journey_event`, not `_record_journey_snapshot`) |

**Multiple events can combine**: If both `new_high` and `4h_checkpoint` trigger in the same update, the event is stored as `"new_high+4h_checkpoint"`.

**If no events trigger**: No snapshot is added — the journey list does NOT grow every 5 minutes.

### Journey snapshot fields (every snapshot has these)

| Field | Type | Description |
|-------|------|-------------|
| `event` | string | Event name(s) joined by "+", e.g., "new_high", "new_low+btc_move" |
| `timestamp` | string | UTC timestamp, e.g., "2026-03-15 12:00:00 UTC" |
| `timestamp_ts` | float | Unix timestamp (seconds) |
| `hours_after_entry` | float | Hours since signal was created |
| `price` | float | Current mark price at this moment |
| `pct_from_entry` | float | `((current - entry) / entry) * 100` |
| `btc_price` | float or None | BTCUSDT mark price at this moment |
| `btc_pct_from_signal_entry` | float or None | BTC's % change since signal entry |
| `volume_1h` | float or None | Latest closed 1h candle's USDT quote_volume (freshly fetched via separate API call) |
| `volume_1h_base` | float or None | Latest closed 1h candle's base (coin) volume |
| `is_new_low` | bool | Whether this snapshot represents a new lowest price |
| `is_new_high` | bool | Whether this snapshot represents a new highest price |

### Volume fetch for journey
Each journey snapshot triggers a fresh API call: `GET /fapi/v1/klines` with `symbol`, `interval="1h"`, `limit=1` to get the latest closed 1h candle's `quote_volume` (USDT) and `volume` (base coin). These become the `volume_1h` and `volume_1h_base` fields.

### Journey sorting
After each new snapshot is added, the entire journey list is sorted by `timestamp_ts` ascending.

### Cross detection (below_entry)
The `below_entry` event uses cross detection — it only fires when the price transitions from being above entry to below entry, not on every update where price is below entry.

---

## 11. Take-Profit Checking & TP Snapshot

### When TP checking happens
Every 300 seconds (5 minutes), as part of the tracker loop (`_check_take_profits()`).

### TP targets
Configurable list: `[5, 10, 20, 30, 50, 75, 100]` (default). These are percentage gains above entry price.

### How TP hits are detected

For each active signal:
1. Calculate `high_pct = ((highest_price - entry_price) / entry_price) * 100`
2. For each TP target NOT already in `tp_sent`:
   - If `high_pct >= target` → TP is hit
3. **Once hit, a TP is never re-sent** — target is added to `tp_sent` list

### What happens on TP hit

When one or more new TP targets are hit for a signal:

1. **Lazy 24h ticker fetch**: `get_24h_tickers()` is called ONCE per cycle, only if at least one TP hit is detected across any signal. Cached and reused for all signals in that cycle.

2. **Market data fetch**: `_fetch_snapshot_market_data(symbol)` is called ONCE per symbol per cycle. This makes multiple API calls:
   - `GET /fapi/v1/klines` with `1h`, `limit=25` (1h candles)
   - `GET /futures/data/openInterestHist` with `1h`, `limit=25` (OI history)
   - `GET /fapi/v1/premiumIndex` with `symbol` (funding rate)
   - `GET /fapi/v1/klines` with `4h`, `limit=55` (4h candles)
   - Market cap from CoinGecko cache (if enabled)

3. **For each TP target hit** (using the same cached market data):
   - Update outcome fields (`tp{N}_hit`, `tp{N}_hit_time`, etc.)
   - Set `btc_change_entry_to_tp` on FIRST TP hit only
   - Build and store `tp{N}_snapshot` at signal root level
   - Add `tp_hit_{N}` event to price journey
   - Queue Telegram alert

### TP Snapshot — All fields inside `tp{N}_snapshot`

Each TP snapshot is a complete market re-scan at the moment of the TP hit. It mirrors the entry `additional_data` plus extra momentum/color fields:

| Field | Type | Description |
|-------|------|-------------|
| `hit_time` | string | UTC timestamp of TP hit |
| `hit_hours_after_entry` | float | Hours since signal entry |
| `max_drawdown_before` | float | Max drawdown at the time of TP hit |
| `btc_price_at_hit` | float or None | BTCUSDT price at TP hit |
| `btc_pct_change_since_entry` | float or None | BTC % change from signal entry to TP hit |
| `rvol_20` | float or None | Current RVOL vs 20-candle baseline (at TP hit time) |
| `vol_baseline_avg` | float or None | 20-candle average volume baseline (at TP hit time) |
| `oi_current_usdt` | float | Current OI in USDT |
| `oi_avg_24h_usdt` | float | Average OI over 24 periods |
| `oi_change_pct` | float or None | OI deviation from 24h average (%) |
| `oi_growth_current` | float | Latest OI period-over-period change |
| `oi_growth_avg` | float | Average OI period-over-period change |
| `oi_growth_ratio` | float | Current growth / abs(avg growth) |
| `funding_rate` | float | Current funding rate × 100 (%) |
| `funding_in_ideal_range` | bool | Whether funding is in -0.02% to 0.15% range |
| `volume_1h` | float or None | Latest 1h candle USDT quote_volume |
| `volume_1h_base` | float or None | Latest 1h candle base (coin) volume |
| `vol_24h_usdt` | float | 24h trading volume in USDT (from cached tickers) |
| `vol_24h_base` | float | 24h trading volume in base coin units (from cached tickers) |
| `vol_24h_above_50m` | bool | Whether 24h USDT volume ≥ $50M |
| `ema50_4h` | float | 4h EMA50 value |
| `price_above_ema50_4h` | bool | Whether current price > EMA50 |
| `ema50_distance_pct` | float or None | % distance from EMA50 |
| `volatility_recent_10_pct` | float | Avg candle range of last 10 1h candles |
| `volatility_prior_10_pct` | float | Avg candle range of candles [-20:-10] |
| `volatility_compression_ratio` | float | recent / prior volatility |
| `is_compressed` | bool | Whether ratio < 0.7 |
| `market_cap_usd` | float or None | Market cap (if enabled) |
| `market_cap_fmt` | string or None | Formatted market cap (if enabled) |
| `price_momentum_1h_pct` | float | `((last_1h_close - prev_1h_close) / prev_1h_close) * 100` — 1h momentum |
| `price_momentum_4h_pct` | float | `((last_4h_close - prev_4h_close) / prev_4h_close) * 100` — 4h momentum |
| `candle_colors_at_hit` | list[string] | Colors of last 3 1h candles, e.g., `["green", "green", "red"]` |

**Why TP snapshots exist**: They let you compare market conditions at entry vs each TP level. This enables hold-vs-exit analysis: "Was OI still rising at +10%? Was funding getting extreme at +50%? Were candles still green at +75%?"

---

## 12. Reversal Warning

### What it is
A one-time Telegram alert when a signal's price drops significantly from its peak.

### When it's checked
Every 300 seconds, during `_check_take_profits()`, after TP checks.

### Conditions (ALL must be true)

| Condition | Setting | Default |
|-----------|---------|---------|
| Reversal alerts enabled | `reversal_alert_enabled` | `true` |
| Not already warned | `reversal_warned == False` | Signal starts as False |
| Peak from entry ≥ threshold | `min_reversal_peak_pct` | 3.0% |
| Drop from peak ≥ threshold | `reversal_drop_from_peak_pct` | 5.0% |

**Drop calculation**: `drop_from_peak = high_pct - cur_pct` where:
- `high_pct = ((highest_price - entry) / entry) * 100`
- `cur_pct = ((current_price - entry) / entry) * 100`

### Example
- Entry: $100, Peak: $108 (+8%), Current: $101 (+1%)
- Peak ≥ 3% → YES (8%)
- Drop from peak: 8% - 1% = 7% ≥ 5% → YES
- Reversal warning fires!

### What happens
1. `reversal_warned` is set to `True` (never fires again for this signal)
2. Telegram message is sent (see Section 20 for format)

---

## 13. Signal Type Classification

### When classification runs
- **During tracking** (every 5 min): updates `outcome.signal_type` based on current data
- **At archive time**: final classification with `is_archiving=True`

### Classification logic

| Type | Condition | When assigned |
|------|-----------|---------------|
| `active` | No TP hit yet, signal still being tracked | During tracking only |
| `fast` | First TP hit happened < 6 hours after entry | During tracking + archive |
| `slow` | First TP hit happened between 6-72 hours after entry | During tracking + archive |
| `delayed` | First TP hit happened > 72 hours after entry | During tracking + archive |
| `failed` | No TP target was ever hit | Archive time only |

**"First TP hit"** = the TP with the smallest `hit_hours_after_entry` value.

**Key distinction**: A signal is never classified as `failed` while still active — it stays `active` until archived. Only at archive time can it become `failed`.

---

## 14. Archiving — After 168 Hours (7 Days)

### When it runs
Every 300 seconds, as part of the tracker loop (`archive_expired()`).

### Condition
`time.time() - signal["alert_time_ts"] >= max_age` where `max_age = max_age_hours * 3600` (default: 168 hours = 604800 seconds).

### What happens to each expired signal

1. **Calculate final percentages** (if entry_price > 0):
   - `peak_pct = ((highest - entry) / entry) * 100`
   - `lowest_pct = ((lowest - entry) / entry) * 100`
   - `exit_pct = ((current - entry) / entry) * 100`
   - `exit_price = current`
   - `highest_pct = peak_pct` (backward compat alias)

2. **Add archive metadata**:
   - `archived_time_ts` = current unix timestamp
   - `archived_time` = "YYYY-MM-DD HH:MM:SS UTC"
   - `tracked_hours` = `age / 3600` rounded to 1 decimal

3. **Exit market cap** (if market_cap enabled):
   - `market_cap_usd_exit` = current market cap
   - `market_cap_exit_fmt` = formatted

4. **Finalize outcome**:
   - `signal_type` = final classification (can become "failed" here)
   - `signal_closed = True`
   - `close_reason = "expired"`
   - `close_time` = archived_time

5. **BTC trend during signal** (set ONLY at archive):
   - Find BTC reference price: use `tp{N}_btc_price_at_hit` from the **first** TP target hit (lowest N in `[5, 10, 20, 30, 50, 75, 100]` that was hit). This captures BTC's state during the signal's active success period, not 7 days later.
   - If no TP was ever hit (failed signal): fall back to live BTC price at archive time
   - Calculate `btc_change = ((btc_ref - btc_entry) / btc_entry) * 100`
   - If btc_change > +2% → `"pumping"`
   - If btc_change < -2% → `"dumping"`
   - Otherwise → `"ranging"`

6. **Clean internal fields**:
   - Remove `_prev_highest`
   - Remove `_prev_lowest`

### Archive storage — Atomic gzip write

1. **Group by month**: Each signal goes to `data/signals_YYYY_MM.json.gz` based on its `alert_time_ts`
2. **Write gzip FIRST**: The gzip file is written before removing from active signals
3. **Safety**: `archive_expired()` wraps the gzip write in a `try/except`. If gzip write fails, `_save_gzip()` logs the error and re-raises the `IOError`, which is caught by `archive_expired()` — it returns 0 and signals remain in `data/signals.json` (no data loss).
4. **If gzip write succeeds**: Active signals file is updated (without the archived signals)
5. **Compact JSON**: Uses `separators=(",", ":")` — no spaces, no indentation — for ~70-80% compression vs indented JSON
6. **Atomic file write**: Uses `.tmp.gz` → `rename()` pattern to prevent corruption

### After archive
Archived signals are also added to `data/pending_report.json` for the next daily report.

---

## 15. Daily Report

### When it sends
- Checked every tracker cycle (every 300 seconds)
- Only sends at `daily_report_hour` UTC (default: 0 = midnight)
- Only sends once per calendar day (tracked via `data/last_report_date.txt`)

### What it contains
- All signals in `data/pending_report.json` — these are signals that were archived since the last daily report
- Sent as a Telegram **document** (JSON file attachment)
- Caption: `"Daily 7-day report — {count} signal(s) completed\n{symbol1, symbol2, ...}"`

### Flow
1. Check if current UTC hour == `daily_report_hour`
2. Check if today's date has NOT been sent yet (read `data/last_report_date.txt`)
3. Load `data/pending_report.json`
4. If empty: mark today as sent, return
5. Write pending signals to temp file `data/report_YYYY-MM-DD.json`
6. Send as Telegram document
7. If send succeeds:
   - Clear `data/pending_report.json` (write empty list)
   - Update `data/last_report_date.txt` with today's date
8. If send fails: leave pending queue — will retry next cycle
9. Always delete the temp file

---

## 16. Telegram Bot Commands

The command listener runs as a daemon thread, polling Telegram for messages via `getUpdates` with long polling (timeout: 10 seconds).

**Security**: Only messages from the configured `chat_id` are processed. Others are silently ignored.

**Message splitting**: Messages longer than 4000 characters are automatically split at newline boundaries.

### All 7 commands

#### `/report` — Performance Overview
- Fetches fresh mark prices and updates all signals
- Shows every active signal with: emoji, symbol, age, current %, peak %, breakout %
- Bottom summary: total signals, avg current %, avg peak %, win rate (now), win rate (peak > 2%)
- **Win now** = signals currently above entry. **Win peak** = signals that reached > +2% at some point.
- **TP Hits summary**: Shows how many active signals have hit each TP target (reads targets from config). Example: `TP +5%: 12 signals`, `TP +10%: 8 signals`, etc. Shows all configured TP levels including those with 0 hits.

#### `/report SYMBOL` — Detailed Single-Coin Breakdown
- Accepts symbol with or without "USDT" suffix (e.g., `/report BTC` or `/report BTCUSDT`)
- Shows 5 sections:
  1. **Price**: Entry, Current, Peak, Lowest (with % changes)
  2. **Main Criteria**: Breakout %, volume progression, 24h change
  3. **Additional Data**: RVOL, OI change, funding rate (with ✅/⚠️), 24h volume, EMA50 status, volatility compression
  4. **Outcome**: Signal type (with icon), status, max drawdown, negative before TP, peak timing, each TP hit with hours and drawdown, TP snapshots (OI, funding, momentum, candle colors), first TP timing, BTC change to first TP, BTC trend
  5. **BTC Context**: BTC price at entry vs now

#### `/summary` — Win Rates & Statistics
- **Active section**: count, avg current %, avg peak %, win rate now, win rate peak, best/worst/top peak symbols
- **History section**: count of archived signals, avg exit %, avg peak %, win rate

#### `/active` — Quick List
- List of all active signals sorted newest first
- Each line: symbol, age, breakout %, volume range (first → last)
- Shows tracking window length

#### `/export` — JSON File of Active Signals
- Sends active signals as JSON Telegram document(s)
- Strips `_prev_highest` and `_prev_lowest` internal fields from the JSON before sending
- **Chunked**: splits into multiple files of 200 signals each when total exceeds 200
- Caption per file: label, part number (if multi-file), signal count, total count, generated timestamp

#### `/detailed_report` — JSON of Completed Signals
- Builds JSON file(s) of signals from `get_completed_signals()` with `min_age = detailed_report_min_age_hours` (default: 168h)
- Includes TP snapshots (any key ending with `_snapshot`)
- **Chunked**: splits into multiple files of 200 signals each when total exceeds 200
- Caption per file: label, part number (if multi-file), signal count, total count, generated timestamp

#### `/export_csv` — Flat CSV Export
- Calls `export_csv.build_csv()` to flatten ALL signals (active + archived) into CSV
- **Chunked**: splits into multiple CSV files of 200 signals each when total exceeds 200. Each file has its own header row.
- Caption per file: label, part number (if multi-file), signal count, total count, file size, generated timestamp

#### `/coin ETH` — Single Coin JSON Export
- Accepts base name (`ETH`) or full symbol (`ETHUSDT`) — auto-appends USDT if missing
- Filters active signals for the specified coin
- Updates prices before export for fresh current_price/highest_price
- Sends as Telegram document (JSON file) with signal count and timestamp
- JSON output is always an array (even for a single signal) for consistent schema
- If coin not found: sends "No active signal for ETHUSDT"

#### `/validate` — Data Integrity Check
- Checks all active signals for data completeness
- Validates: additional_data not empty, oi_growth_ratio, funding_rate, rvol_20, vol_24h_usdt, vol_24h_base, high_breakout_warning, vol_candle_1_base, outcome TP fields
- Reports: total signals, clean count, issue count, and lists each issue
- Truncates at 50 issues to avoid Telegram message limits

#### `/help` — Command Reference
Lists all commands with descriptions, tracking window, update frequency, TP alert info, and signal criteria summary.

---

## 17. CSV Export

### What it does
Flattens every signal (active + all archived) into one CSV row per signal, with every field as a column.

### Data sources read
1. `data/signals.json` — active signals
2. `data/history.json` — legacy archive (backward compat)
3. `data/signals_*.json.gz` — all monthly gzip archives

### How fields are flattened

| Signal structure | CSV column naming | Example |
|-----------------|-------------------|---------|
| Root fields | As-is | `symbol`, `entry_price`, `breakout_margin_pct` |
| `additional_data.{field}` | Prefixed with `add_` | `add_rvol_20`, `add_oi_change_pct` |
| `outcome.{field}` | Prefixed with `out_` | `out_tp5_hit`, `out_signal_type` |
| `tp{N}_snapshot.{field}` | `tp{N}_{field}` | `tp10_oi_change_pct`, `tp50_funding_rate` |
| `price_journey` (list) | 3 columns | `journey_count`, `journey_events`, `price_journey_json` |

### Special handling

| Data type | How it's exported |
|-----------|------------------|
| `None` | Empty string `""` |
| `bool` | `True` / `False` |
| `int`, `float` | Numeric value |
| `list` (candle_colors, tp_sent, candle_colors_at_hit) | Pipe-separated: `"green\|red\|green"` |
| `dict` (any remaining) | Compact JSON string |
| `price_journey` | `journey_count` = length, `journey_events` = pipe-separated event names, `price_journey_json` = full compact JSON of all journey snapshots |

### Column ordering
Columns appear in this order:
1. **Priority fields**: symbol, alert_time, alert_time_ts, timeframe, entry_price, current_price, highest_price, lowest_price, peak_pct, lowest_pct, exit_pct, exit_price, breakout_margin_pct, high_breakout_warning, high_24h, price_change_24h, vol_candle_1/2/3, vol_candle_1/2/3_fmt, vol_candle_1/2/3_base, vol_candle_1/2/3_base_fmt, vol_ratio, rvol, candle_colors, btc_price, candle_time
2. **Additional data** (`add_*`): sorted alphabetically
3. **Outcome** (`out_*`): sorted alphabetically
4. **TP snapshots** (`tp{N}_*`): sorted by TP level (5, 10, 20, 30, 50, 75, 100) then by field name
5. **Remaining fields**: sorted alphabetically

### Skipped fields
These internal fields are excluded from CSV: `_prev_highest`, `_prev_lowest`, `last_update_ts`

### CLI usage
```bash
python export_csv.py                      # exports to data/signals_export.csv
python export_csv.py --out my_file.csv    # custom output path
python export_csv.py --active-only        # only active signals
python export_csv.py --history-only       # only archived signals
```

---

## 18. Data Storage Files

| File | Format | Contents | When written |
|------|--------|----------|-------------|
| `data/signals.json` | Indented JSON | Active signals being tracked | Every 5 min (price updates) + on new signal + on archive |
| `data/signals_YYYY_MM.json.gz` | Gzip-compressed compact JSON | Archived signals grouped by month of alert_time | On archive (after 168h) |
| `data/history.json` | JSON | Legacy archive file (read-only for backward compat) | No longer written to; only read by get_history() |
| `data/pending_report.json` | JSON | Signals awaiting next daily report | On archive (signals queued) + after daily report (cleared) |
| `data/last_report_date.txt` | Plain text | "YYYY-MM-DD" of last daily report sent | After daily report sent |
| `scanner.log` | Plain text | All application logs | Continuously |
| `config.json` | JSON | All configuration | Read-only at startup |

### File write safety
- **JSON files**: Written to `.tmp` file first, then atomically renamed via `tmp.replace(path)`
- **Gzip files**: Written to `.tmp.gz` first, then atomically renamed
- **Archive atomicity**: `archive_expired()` wraps the gzip write in `try/except`. If gzip write fails, `_save_gzip()` re-raises the `IOError`, which is caught — signals remain in `data/signals.json` (no data loss).

---

## 19. Rate Limiting & API Details

### Binance API

| Setting | Value | Description |
|---------|-------|-------------|
| Max weight per minute | 2400 | Binance hard limit |
| Safe ceiling | 2000 | Bot stops sending when used weight reaches this |
| Per-request delay | 100ms | Configurable via `rate_limit.binance_delay_ms` |
| Weight tracking | Sliding 60-second window | Weights are tracked with timestamps, expired entries are pruned |

**When ceiling is hit**: Calculates sleep time based on oldest weight entry: `60 - (now - oldest) + 0.5` seconds.

**Retry behavior by HTTP status**:

| Status | Action | Wait |
|--------|--------|------|
| 429 | Retry | `Retry-After` header (default 60s) |
| 418 | IP auto-banned, retry | 120 seconds |
| 451 | Geo/legal block, retry | 300 seconds |
| 5xx | Server error, retry | `2^attempt` seconds (2s after 1st fail, 4s after 2nd) |
| Timeout | Retry | `2^attempt` seconds (2s after 1st fail, 4s after 2nd) |
| Connection error | Retry | `2^attempt` seconds (2s after 1st fail, 4s after 2nd) |

All retries: maximum 3 attempts (attempts 1, 2, 3). Sleep only happens between retries (after attempt 1 and 2, not after 3). After 3 failures, raises `RuntimeError`.

### API endpoints used and their weights

| Endpoint | Weight | Called when |
|----------|--------|-----------|
| `GET /fapi/v1/exchangeInfo` | 1 | Every cycle (cached 300s) |
| `GET /fapi/v1/premiumIndex` (all) | 1 | Every cycle + every 5 min (tracker) |
| `GET /fapi/v1/premiumIndex` (single) | 1 | Per symbol for funding rate |
| `GET /fapi/v1/ticker/24hr` | 40 | Every cycle + lazy on TP hit |
| `GET /fapi/v1/klines` | 1-2 | Per symbol for candles (1 if ≤100, 2 if >100) |
| `GET /futures/data/openInterestHist` | 1 | Per symbol for OI |

### CoinGecko API (if enabled)

| Setting | Value |
|---------|-------|
| Endpoint | `GET /api/v3/coins/markets` |
| Pages fetched | Up to 12 (250 coins per page = 3000 total) |
| Page delay | 20 seconds between pages |
| Cache TTL | 120 minutes (configurable) |
| 429 handling | Wait `Retry-After + (attempt × 30)` seconds, max 3 consecutive 429s then stop |
| 403 handling | Stop fetching, keep what was collected |
| Timeout/connection error | Max 3 total errors then stop |
| On failure with cache | Keep previous cache, log warning |

### Telegram API

| Setting | Value |
|---------|-------|
| Send retries | 3 attempts per message |
| 429 handling | Wait `retry_after` from response (default 30s) |
| Between sends | 0.3s delay (alerts), 0.5s delay (TP/reversal alerts) |
| Long poll timeout | 10 seconds (getUpdates) |
| Request timeout | 15s for messages, 30s for documents |

---

## 20. Telegram Message Formats

### Signal Alert (Section 7 above has full example)
- **Normal**: Header `🚨 BREAKOUT SIGNAL`
- **High breakout**: Header `⚠️ BREAKOUT SIGNAL — HIGH BREAKOUT` + warning line
- Shows: symbol, timeframe, price, 3 criteria lines, time, cooldown

### Take-Profit Hit

**Icon by target level**:

| Target | Icon |
|--------|------|
| 5% | 🎯 |
| 10-29% | 🚀 |
| 30-49% | 🚀🚀 |
| 50-74% | 🚀🚀🚀 |
| 75%+ | 💎🚀🚀 |

**Format**:
```
🚀 TARGET HIT  +10%
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📌 ETHUSDT
💵 Entry:    $3,500.00
🏔  Peak:     $3,920.00  (+12.00%)
💵 Now:      $3,850.00  (+10.00%)
⏱  Age:      12h 30m

🟢 Still above target
```

If current price has pulled back below target: `⚠️ Price pulled back from target`

### Reversal Warning

```
⚠️ REVERSAL WARNING
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

📌 ETHUSDT
💵 Entry:    $3,500.00
🏔  Peak:     $3,780.00  (+8.00%)
💵 Now:      $3,535.00  (+1.00%)
📉 Drop:     7.00% from peak
⏱  Age:      24h 15m

Price has dropped significantly from its peak.
Consider taking remaining profits.
```

### Startup Message

```
🤖 Volume Scanner Started

⚙️ Scanner Started — New Strategy

Main Criteria (all 3 must pass):
1️⃣ Current 1h candle closes above last 24h high
2️⃣ Last 3 candles have strictly increasing volume
3️⃣ 24h price change ≤ ±20%

Additional data collected (not filters):
📊 RVOL vs 20-candle baseline
📈 OI change vs 24h average
💰 Funding rate
💧 24h volume (liquidity)
📉 Price vs 4h EMA50
🔲 Volatility compression score

⏱ Scan every 900s  |  Cooldown 168h

Scanner is now running …
```

---

## 21. Full config.json Reference

```json
{
  "binance": {
    "api_key": "",              // Optional Binance API key (env: BINANCE_API_KEY)
    "api_secret": ""            // Optional Binance API secret (env: BINANCE_API_SECRET)
  },
  "telegram": {
    "bot_token": "YOUR_BOT_TOKEN",  // Required (env: TELEGRAM_BOT_TOKEN)
    "chat_id": "YOUR_CHAT_ID"       // Required (env: TELEGRAM_CHAT_ID)
  },
  "scanner": {
    "timeframe": "1h",                    // Candle timeframe
    "scan_interval_seconds": 900,         // 15 minutes between full scans
    "breakout_lookback_candles": 24,      // Number of candles for 24h high
    "consecutive_vol_candles": 3,         // Must be 3 increasing volume candles
    "consecutive_vol_min_ratio": 2.0,     // Min ratio newest/oldest volume
    "high_breakout_warning_pct": 5.0,     // Breakout margin warning threshold
    "max_price_change_24h_pct": 20.0,     // Max ±24h price change
    "min_volume_usdt": 0,                 // 0 = disabled
    "cooldown_hours": 168,                // 7 days between signals for same symbol
    "excluded_symbols": ["USDCUSDT", "BTCDOMUSDT"],
    "hard_filters": {
      "vol_ratio_max": 15,              // Block if vol_ratio > this
      "funding_rate_min": -0.05,        // Block if funding_rate ≤ this
      "vol_24h_usdt_min": 5000000,      // Block if 24h volume ≤ this
      "market_cap_usd_max": 1000000000  // Block if market cap ≥ this
    },
    "soft_flags": {
      "breakout_margin_pct_max": 1.5,   // Flag if brk margin > this
      "price_change_24h_max": 5.0,      // Flag if |24h change| > this
      "ema50_distance_pct_max": 8.0,    // Flag if EMA50 distance > this
      "vol_ratio_max": 10,              // Flag if vol_ratio > this
      "vol_24h_usdt_min": 10000000,     // Flag if 24h vol < this
      "oi_change_pct_max": 12.0,        // Flag if OI change > this
      "funding_rate_min": -0.01,        // Flag if funding < this
      "max_flags_to_block": 5           // Block signal if flags ≥ this
    },
    "quality_score": {
      "vol_ratio_max": 10,              // +1 if vol_ratio ≤ this
      "funding_rate_min": 0,            // +1 if funding ≥ this
      "price_change_24h_max": 5.0,      // +1 if |24h change| ≤ this
      "breakout_margin_pct_max": 1.5,   // +1 if brk margin ≤ this
      "oi_change_pct_max": 8.0,         // +1 if OI change ≤ this
      "ema50_distance_pct_max": 5.0,    // +1 if EMA50 dist ≤ this
      "market_cap_usd_min": 10000000,   // +1 if mcap between min-max
      "market_cap_usd_max": 500000000,  // +1 if mcap between min-max
      "vol_24h_usdt_min": 10000000      // +1 if 24h vol ≥ this
    }
  },
  "tracker": {
    "enabled": true,                         // Enable tracker + commands
    "max_age_hours": 168,                    // Archive after 7 days
    "price_update_interval_seconds": 300,    // 5 minutes between price updates
    "data_dir": "data",                      // Directory for all data files
    "take_profit_targets": [5, 10, 20, 30, 50, 75, 100],
    "reversal_alert_enabled": true,
    "min_reversal_peak_pct": 3.0,            // Min peak before reversal can trigger
    "reversal_drop_from_peak_pct": 5.0,      // Drop from peak to trigger warning
    "detailed_report_min_age_hours": 168,    // Min age for /detailed_report
    "daily_report_hour": 0                   // UTC hour for daily report (0 = midnight)
  },
  "market_cap": {
    "enabled": false,             // Enable CoinGecko market cap
    "cache_minutes": 120          // Cache duration
  },
  "rate_limit": {
    "binance_delay_ms": 100       // Delay between Binance API calls
  },
  "logging": {
    "level": "INFO",              // INFO, DEBUG, WARNING, ERROR
    "log_file": "scanner.log"     // Log file path
  }
}
```

---

## Complete Data Flow Summary

```
STARTUP
  │
  ├── Load config.json + env overrides
  ├── Start health server (port 8080)
  ├── Validate Telegram bot token
  └── Launch 3 threads
        │
        ├── SCANNER (main thread, every 900s)
        │     │
        │     ├── Fetch all USDT perpetual symbols (~542)
        │     ├── Fetch all mark prices (1 API call)
        │     ├── Fetch all 24h tickers (1 API call)
        │     ├── Filter: exclude list, already tracked, cooldown
        │     │
        │     └── For each remaining symbol:
        │           ├── Fetch 25 closed 1h candles
        │           ├── Filter 1: Close > 24h high?
        │           ├── Filter 2: 3 candles increasing vol, ratio ≥ 2x?
        │           ├── Filter 3: 24h change ≤ ±20%?
        │           ├── (Optional) Min volume check
        │           ├── Collect additional data (RVOL, OI, FR, vol, EMA, volatility, mcap)
        │           ├── Hard Filter 4–7: vol_ratio, funding, vol_24h, mcap
        │           ├── Soft Flags: count 0–7, block if ≥ 5
        │           ├── Quality Score: 0–8 points
        │           │
        │           └── ALL PASS → SIGNAL FIRES
        │                 ├── Send Telegram alert (with score + flags)
        │                 ├── Record to data/signals.json
        │                 └── Start cooldown (168h)
        │
        ├── TRACKER (daemon thread, every 300s)
        │     │
        │     ├── 1. PRICE UPDATE
        │     │     ├── Fetch all mark prices
        │     │     ├── Update current/highest/lowest for each signal
        │     │     ├── Update outcome (drawdown, peak, negative tracking)
        │     │     └── Record journey snapshots (if events triggered)
        │     │
        │     ├── 2. TP CHECK
        │     │     ├── For each signal, check if highest crossed any TP target
        │     │     ├── On hit: build TP snapshot (full market re-scan)
        │     │     ├── Send TP Telegram alert
        │     │     └── Check reversal conditions
        │     │
        │     ├── 3. ARCHIVE
        │     │     ├── Signals > 168h old → finalize + write to monthly .gz
        │     │     ├── Set final signal_type, close_reason, BTC trend
        │     │     └── Queue for daily report
        │     │
        │     └── 4. DAILY REPORT
        │           └── At midnight UTC → send pending signals as JSON document
        │
        └── COMMAND LISTENER (daemon thread, continuous)
              └── Long-poll Telegram → dispatch /report, /summary, /active,
                  /export, /detailed_report, /export_csv, /help
```

---

*This document reflects the bot codebase as of March 2026. Every field, condition, timing, and data point has been verified against the source code.*
