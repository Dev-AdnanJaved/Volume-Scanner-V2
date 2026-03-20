# Binance Futures Volume Scanner Bot

A production-ready Python bot that continuously scans **all Binance USDT perpetual futures pairs** for unusual volume spikes and sends real-time alerts to Telegram.

---

## Table of Contents

- [Features](#features)
- [How It Works](#how-it-works)
- [Project Structure](#project-structure)
- [File Descriptions](#file-descriptions)
- [Setup Guide](#setup-guide)
- [Configuration Reference](#configuration-reference)
- [Running the Bot](#running-the-bot)
- [Alert Example](#alert-example)
- [FAQ](#faq)

---

## Features

- Scans **all** Binance USDT-M perpetual futures pairs automatically
- Filters coins by **market cap** (via CoinGecko free API — no key needed)
- Detects **volume spikes** by comparing recent candles vs historical baseline
- Optional **breakout confirmation** (price breaks recent highs)
- Optional **open interest surge** detection
- Sends formatted **Telegram alerts** with full details
- **12-hour cooldown** per coin to prevent spam (configurable)
- Handles Binance API **rate limits** automatically
- Runs **continuously** with configurable scan interval
- Clean **logging** to console and file

---

## How It Works

Every scan cycle (default: 2 minutes):

Fetch all USDT perpetual futures symbols from Binance
Filter out coins with market cap above threshold (default: $50M)
For each remaining coin:
a. Skip if coin is on cooldown (already alerted recently)
b. Fetch closed 1h candles
c. Compare average volume of last 2 candles vs previous 15 candles
d. If volume ratio ≥ 3x → volume spike detected
e. If breakout filter ON → check if price broke 20-candle high
f. If OI filter ON → check if open interest increased by ≥ 5%
g. If ALL enabled conditions pass → send Telegram alert
h. Put coin on 12-hour cooldown
Sleep until next cycle

text

---

## Project Structure

binance-volume-scanner/
├── config.json # All bot settings
├── requirements.txt # Python dependencies
├── main.py # Entry point — starts the bot
├── scanner.py # Core scanning logic and alert decisions
├── binance_client.py # Binance Futures API wrapper
├── market_cap.py # CoinGecko market cap fetcher + cache
├── notifier.py # Telegram message sender
└── scanner.log # Log file (created at runtime)

text

---

## File Descriptions

### `main.py`

**What it does:** Entry point of the bot.

- Loads `config.json`
- Sets up logging (console + file)
- Validates that Telegram credentials are filled in
- Creates the `Scanner` object and starts it
- Catches `Ctrl+C` / `SIGTERM` for graceful shutdown

### `scanner.py`

**What it does:** The brain of the bot — all scanning logic lives here.

- Loops through all qualifying symbols every cycle
- Fetches candle data and runs volume spike detection
- Runs optional breakout and open interest checks
- Manages the per-symbol cooldown timer (default 12h)
- Builds alert data and passes it to the Telegram notifier
- Sends a startup summary message when the bot launches

### `binance_client.py`

**What it does:** Handles all communication with the Binance Futures API.

- Fetches the list of all USDT perpetual trading pairs
- Fetches mark prices (current prices) for all pairs in one call
- Fetches closed historical candles (klines) for a given symbol
- Fetches historical open interest data
- Tracks API weight usage with a sliding window to avoid rate limits
- Automatically retries on timeouts, 429 (rate limit), and server errors
- Caches exchange info to reduce unnecessary calls

### `market_cap.py`

**What it does:** Fetches and caches market cap data from CoinGecko.

- Uses the **free public API** — no API key required
- Fetches top coins in bulk (250 per page, typically 4-5 pages needed)
- Caches results for a configurable duration (default: 60 minutes)
- Handles Binance naming quirks (e.g., `1000PEPE` → `PEPE`)
- Provides a simple pass/fail filter for the scanner
- Formats market cap for display in alerts (e.g., `$45.20M`)

### `notifier.py`

**What it does:** Sends formatted alert messages to Telegram.

- Validates the bot token on startup
- Formats rich HTML alert messages with emojis
- Auto-retries on Telegram rate limits (429)
- Sends startup configuration summary
- Handles connection errors gracefully

### `config.json`

**What it does:** Single file containing ALL bot settings.

- Every parameter is configurable — no code changes needed
- See [Configuration Reference](#configuration-reference) below for full details

### `requirements.txt`

**What it does:** Lists Python packages needed.

- Only one dependency: `requests` (HTTP library)

---

## Setup Guide

### Prerequisites

- Python 3.8 or higher
- A Telegram bot token (from [@BotFather](https://t.me/BotFather))
- Your Telegram chat ID (from [@userinfobot](https://t.me/userinfobot))

### Step-by-step

```bash
# 1. Create project folder
mkdir binance-volume-scanner && cd binance-volume-scanner

# 2. Create all files as provided (main.py, scanner.py, etc.)

# 3. Create virtual environment
python3 -m venv .venv
source .venv/bin/activate        # Linux/Mac
# .venv\Scripts\activate         # Windows

# 4. Install dependencies
pip install -r requirements.txt

# 5. Edit config.json
#    - Set your Telegram bot_token
#    - Set your Telegram chat_id
#    - Adjust scanner settings as needed

# 6. Run the bot
python main.py
Getting Telegram Credentials
Bot Token:

Open Telegram, search for @BotFather
Send /newbot
Follow prompts to name your bot
Copy the token (looks like 123456789:ABCdefGHI...)
Chat ID:

Search for @userinfobot on Telegram
Send /start
It replies with your numeric chat ID (looks like 123456789)
Important: Start a conversation with YOUR bot first (send /start to it)
```

{
// ─── Binance API credentials ────────────────────────────────
// Optional. The bot uses only PUBLIC endpoints.
// Only needed if you want higher rate limits with a Binance account.
"binance": {
"api_key": "", // Leave empty — not required
"api_secret": "" // Leave empty — not required
},

    // ─── Telegram settings ──────────────────────────────────────
    // REQUIRED. The bot sends all alerts here.
    "telegram": {
        "bot_token": "YOUR_BOT_TOKEN",   // From @BotFather
        "chat_id": "YOUR_CHAT_ID"        // Your personal or group chat ID
    },

    // ─── Scanner settings ───────────────────────────────────────
    "scanner": {

        // Candlestick timeframe.
        // The bot analyses candles of this size.
        // Options: "1m","3m","5m","15m","30m","1h","2h","4h","6h","8h","12h","1d","3d","1w"
        // Default: "1h"
        "timeframe": "1h",

        // How often (in seconds) the bot runs a full scan of all coins.
        // Lower = more responsive but more API calls.
        // Recommended: 60-300 for 1h timeframe.
        // Default: 120 (2 minutes)
        "scan_interval_seconds": 120,

        // Maximum market cap in USD.
        // Coins ABOVE this value are SKIPPED.
        // Purpose: focus on smaller/mid-cap coins where volume spikes matter more.
        // Set very high (e.g., 999999999999) to disable this filter.
        // Default: 50000000 ($50 million)
        "market_cap_max_usd": 50000000,

        // Number of most recent closed candles to average for "current" volume.
        // These are compared against the baseline.
        // Default: 2
        "volume_recent_candles": 2,

        // Number of older closed candles to average for "baseline" volume.
        // This represents "normal" volume for the coin.
        // Default: 15
        "volume_baseline_candles": 15,

        // Minimum ratio: recent_avg_volume / baseline_avg_volume.
        // If recent volume is at least this many times the baseline → spike detected.
        // Example: 3.0 means recent volume must be 3x the normal average.
        // Default: 3.0
        "volume_multiplier": 3.0,

        // Enable or disable the breakout price filter.
        // When ON: the last closed candle's CLOSE price must be HIGHER than
        //          the highest HIGH of the previous N candles (see breakout_lookback).
        // When OFF: volume spike alone is enough (no price check).
        // Default: true
        "breakout_enabled": true,

        // Number of previous candles to look back for the highest high.
        // Only used when breakout_enabled is true.
        // Example: 20 means "close must break the 20-candle high"
        // Default: 20
        "breakout_lookback": 20,

        // Enable or disable the open interest filter.
        // When ON: current open interest must be higher than the average OI
        //          of the last N periods by at least X% (see below).
        // When OFF: OI is ignored entirely.
        // Note: OI data is sometimes unavailable for newer/smaller pairs.
        //       When unavailable and filter is ON, the coin is SKIPPED.
        // Default: true
        "open_interest_enabled": true,

        // Number of historical OI data points to average as baseline.
        // Only used when open_interest_enabled is true.
        // Default: 15
        "open_interest_periods": 15,

        // Minimum percentage increase in OI vs the baseline average.
        // Only used when open_interest_enabled is true.
        // Example: 5.0 means OI must be at least 5% above the average.
        // Default: 5.0
        "open_interest_min_increase_pct": 5.0,

        // What to do when a coin's market cap is unknown on CoinGecko.
        // true  = include it (scan it anyway — might catch new listings)
        // false = exclude it (safer, avoids false positives on obscure coins)
        // Default: true
        "include_unknown_market_cap": true,

        // Cooldown period in hours after alerting a coin.
        // Once an alert fires for a symbol, that symbol will NOT alert again
        // until this many hours have passed — even if conditions are still met.
        // Prevents spam when a coin stays in high-volume regime.
        // Default: 12
        "cooldown_hours": 12,

        // List of symbols to permanently exclude from scanning.
        // Use exact Binance symbol names (e.g., "BTCUSDT").
        // Default: ["USDCUSDT", "BTCDOMUSDT"]
        "excluded_symbols": ["USDCUSDT", "BTCDOMUSDT"]
    },

    // ─── Rate limiting settings ─────────────────────────────────
    "rate_limit": {

        // Delay in milliseconds between each Binance API call.
        // Prevents burst requests. The bot also tracks API weight internally.
        // Lower = faster scans but higher rate-limit risk.
        // Default: 100
        "binance_delay_ms": 100,

        // How long to cache CoinGecko market cap data (in minutes).
        // Market caps don't change drastically in short periods.
        // Recommended: 60-180 minutes.
        // Lower = more CoinGecko API calls (free tier allows ~30/min).
        // Default: 60
        "market_cap_cache_minutes": 60
    },

    // ─── Logging settings ───────────────────────────────────────
    "logging": {

        // Log level. Controls how much detail is printed.
        // Options: "DEBUG", "INFO", "WARNING", "ERROR"
        // DEBUG   = everything (very verbose, useful for troubleshooting)
        // INFO    = normal operation messages + alerts
        // WARNING = only warnings and errors
        // Default: "INFO"
        "level": "INFO",

        // Log file path. All logs are written here AND to console.
        // Default: "scanner.log"
        "log_file": "scanner.log"
    }

}

FAQ
Do I need a Binance API key?
No. The bot uses only public endpoints. A key is optional and only provides slightly higher rate limits.

Do I need a CoinGecko API key?
No. The bot uses the free public API. It fetches ~4-5 pages of data per refresh (every 60 min by default), which is well within free tier limits (~2,880 calls/month).

How many API calls does it make?
Per scan cycle (every 2 minutes):

1 call for exchange info (cached 5 min)
1 call for all mark prices
1 call per qualifying symbol for klines (~100-200 symbols)
1 call per qualifying symbol for OI (if enabled)
Total: ~200-400 calls per cycle, well within Binance's 2,400 weight/minute limit
Can I scan multiple timeframes?
Not simultaneously. The bot scans one timeframe. Run multiple instances with different configs for multiple timeframes.

What happens if the bot crashes?
If running as a systemd service with Restart=always, it automatically restarts after 10 seconds. Cooldown state is lost on restart (stored in memory only).

Why is a coin not being alerted?
Check these in order:

Market cap might be above the threshold
Volume ratio might be below the multiplier
Breakout condition might not be met (if enabled)
OI increase might be below threshold (if enabled)
Coin might be on cooldown from a recent alert
Coin might be in the excluded list
Set "level": "DEBUG" in config to see exactly why each coin is skipped
Can I use this for spot / other exchanges?
No. This is built specifically for Binance USDT-M perpetual futures.

text

---

This README covers everything — what each file does, what every config variable means with examples, setup instructions, how to run in production, and common troubleshooting.
