# Binance Futures Volume Scanner Bot

A Python bot that monitors Binance USDT-M perpetual futures pairs for unusual trading activity and sends real-time alerts to Telegram.

## Features

- Volume spike detection (recent vs. baseline candle comparison)
- Price breakout confirmation
- Open Interest surge detection
- Market cap filtering via CoinGecko API
- 3-layer signal filtering: hard filters (4 conditions), soft flags (7 data-driven warning conditions, 4+ blocks), quality score (0–8 data-driven points)
- Quality score rewards: RVOL sweet spot (4-8x), adequate RVOL (≥2x), small market cap ($10-50M), moderate OI growth ratio (5-50), healthy funding (≥0), good liquidity (≥$10M 24h vol), breakout conviction (0.5-5%), positive momentum (0-10% 24h change)
- Soft flags warn about: low RVOL (<2x), large market cap (>$200M), extreme OI ratio (>50), negative funding (<-0.02), low volume (<$5M), extreme price change (>±15%), far from EMA50 (>15%)
- All filter/flag/score thresholds configurable in config.json (scanner.hard_filters, scanner.soft_flags, scanner.quality_score)
- Signal tracker with take-profit alerts, reversal detection, and outcome tracking
- Detailed outcome block per signal: TP hit timestamps, max drawdown, signal type classification, close lifecycle (signal_closed/close_reason/close_time), BTC context (btc_change_entry_to_tp, btc_trend_during_signal)
- Full market snapshot at every TP hit (tp{level}_snapshot): same fields as entry additional_data (RVOL, OI, funding, EMA, volatility, market cap) plus price_momentum_1h_pct, price_momentum_4h_pct, candle_colors_at_hit — enables comparing conditions at each TP level
- TP targets: [5, 10, 20, 30, 50, 75, 100] for deep tracking
- Event-based price journey (new high/low, below entry, 4h checkpoint, TP hit, BTC >2% move) with btc_pct_from_signal_entry, volume_1h, is_new_low, is_new_high
- Live signal_type classification (active → fast/slow/delayed as TPs are hit); failed only at archive
- high_breakout_warning stored at signal root level only (not duplicated in outcome)
- Monthly gzip-compressed JSON archives (data/signals_YYYY_MM.json.gz) — ~70-80% size reduction
- Flat CSV export (export_csv.py) flattening all fields into one row per signal for analysis
- Telegram bot commands for interactive queries (/report, /summary, /active, /export, /export_csv, /detailed_report)
- Rate limit handling and caching

## Architecture

| File | Description |
|------|-------------|
| `main.py` | Entry point — loads config, starts threads |
| `scanner.py` | Core scanning loop and detection algorithms |
| `binance_client.py` | Binance Futures API wrapper with rate limiting |
| `notifier.py` | Telegram alert formatting and sending |
| `market_cap.py` | CoinGecko market cap fetch and caching |
| `tracker.py` | Background price updater + take-profit alerts |
| `bot_commands.py` | Telegram bot command handler |
| `export_csv.py` | Flat CSV export — flattens all signal data into one row per signal |
| `config.json` | Configuration file (thresholds, scan settings) |

## Configuration

Settings live in `config.json`. Sensitive credentials are loaded from environment variables (which override `config.json` values):

- `TELEGRAM_BOT_TOKEN` — Telegram bot token from @BotFather
- `TELEGRAM_CHAT_ID` — Telegram channel/chat ID for alerts
- `BINANCE_API_KEY` — (optional) Binance API key
- `BINANCE_API_SECRET` — (optional) Binance API secret

## Running

The app runs as a console workflow (`python main.py`). It has no web frontend.

## Dependencies

- Python 3.11
- `requests>=2.31.0`
