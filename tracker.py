"""
Signal performance tracker with take-profit alerts.

Responsibilities:
  - Store every alert to disk with full enrichment (including additional_data)
  - Continuously track highest price AND lowest price since entry
  - Record event-based price journey snapshots (new high/low, below entry,
    4h checkpoint, TP hit, BTC >2% move) — not every hour
  - Maintain detailed outcome block (TP hit times, drawdown, signal type,
    close lifecycle, BTC context)
  - Full market snapshot at every TP hit (same fields as entry additional_data
    plus momentum and candle colors) for hold-vs-exit analysis
  - Live signal_type classification (active/fast/slow/delayed during tracking,
    failed only at archive)
  - Send take-profit target alerts when price hits configurable levels
  - Send reversal warnings when price drops significantly from peak
  - Archive signals after configurable max age with close reason and BTC trend
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import time
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

from binance_client import BinanceClient
from market_cap import MarketCapProvider
from notifier import TelegramNotifier

logger = logging.getLogger(__name__)


class SignalTracker:

    def __init__(
        self,
        config: dict,
        binance: BinanceClient,
        notifier: TelegramNotifier,
        market_cap: Optional[MarketCapProvider] = None,
    ) -> None:
        tc = config.get("tracker", {})
        self._max_age = tc.get("max_age_hours", 168) * 3600
        self._update_interval = tc.get("price_update_interval_seconds", 300)
        self._data_dir = Path(tc.get("data_dir", "data"))
        self._signals_file = self._data_dir / "signals.json"
        self._history_file = self._data_dir / "history.json"

        self._tp_targets: List[int] = sorted(tc.get("take_profit_targets", [5, 10, 20, 30, 50, 75, 100]))
        self._reversal_enabled: bool = tc.get("reversal_alert_enabled", True)
        self._min_reversal_peak: float = tc.get("min_reversal_peak_pct", 3.0)
        self._reversal_drop: float = tc.get("reversal_drop_from_peak_pct", 5.0)
        self._detailed_min_age: float = tc.get("detailed_report_min_age_hours", 168) * 3600
        self._daily_report_hour: int = int(tc.get("daily_report_hour", 0))

        self._pending_file = self._data_dir / "pending_report.json"
        self._last_report_file = self._data_dir / "last_report_date.txt"

        self._binance = binance
        self._notifier = notifier
        self._market_cap = market_cap
        self._lock = threading.Lock()
        self._running = False

        self._data_dir.mkdir(parents=True, exist_ok=True)
        logger.info(
            "Tracker initialised  (max_age=%dh, update=%ds, TP targets=%s, reversal=%s, report_hour=%02d:00 UTC)",
            self._max_age // 3600, self._update_interval,
            self._tp_targets, self._reversal_enabled, self._daily_report_hour,
        )

    # ── file I/O ─────────────────────────────────────────────────────

    def _load(self, path: Path) -> list:
        if not path.exists():
            return []
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                return data if isinstance(data, list) else []
        except (json.JSONDecodeError, IOError) as exc:
            logger.error("Failed to read %s: %s", path, exc)
            return []

    def _save(self, path: Path, data: list) -> None:
        tmp = path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2)
            tmp.replace(path)
        except IOError as exc:
            logger.error("Failed to write %s: %s", path, exc)

    @staticmethod
    def _fmt_age(ts: float) -> str:
        age = time.time() - ts
        if age < 3600:
            return f"{int(age / 60)}m"
        hours = int(age // 3600)
        mins = int((age % 3600) // 60)
        return f"{hours}h {mins}m"

    @staticmethod
    def _hours_since(start_ts: float, end_ts: float) -> float:
        return round((end_ts - start_ts) / 3600, 2)

    @staticmethod
    def _ts_to_utc(ts: float) -> str:
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    # ── outcome helpers ───────────────────────────────────────────────

    @staticmethod
    def _init_outcome(tp_targets: List[int]) -> dict:
        outcome: dict = {
            "max_drawdown_pct": 0.0,
            "max_drawdown_time": None,
            "max_drawdown_hours_after_entry": None,
            "went_negative_before_tp": False,
            "hours_negative_total": 0.0,
            "peak_pct": 0.0,
            "peak_time": None,
            "peak_hours_after_entry": None,
            "signal_type": "active",
            "signal_closed": False,
            "close_reason": None,
            "close_time": None,
            "btc_change_entry_to_tp": None,
            "btc_trend_during_signal": None,
        }
        for tp in tp_targets:
            key = f"tp{tp}"
            outcome[f"{key}_hit"] = False
            outcome[f"{key}_hit_time"] = None
            outcome[f"{key}_hit_hours_after_entry"] = None
            outcome[f"{key}_max_drawdown_before"] = None
            outcome[f"{key}_btc_price_at_hit"] = None
        return outcome

    # ── record new signal ────────────────────────────────────────────

    def record_signal(self, alert: dict) -> None:
        try:
            price = float(alert["price"]) if alert.get("price") not in (None, "N/A") else 0.0
        except (ValueError, TypeError):
            price = 0.0

        now_ts = time.time()

        signal = {
            "symbol":              alert["symbol"],
            "entry_price":         price,
            "highest_price":       price,
            "lowest_price":        price,
            "current_price":       price,
            "alert_time_ts":       now_ts,
            "alert_time":          datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "timeframe":           alert.get("timeframe", "1h"),
            "price_change_24h":    alert.get("price_change_24h", 0),
            "breakout_margin_pct": alert.get("breakout_margin_pct"),
            "high_breakout_warning": alert.get("high_breakout_warning", False),
            "high_24h":            alert.get("high_24h"),
            "vol_candle_1":        alert.get("vol_candle_1"),
            "vol_candle_2":        alert.get("vol_candle_2"),
            "vol_candle_3":        alert.get("vol_candle_3"),
            "vol_candle_1_fmt":    alert.get("vol_candle_1_fmt"),
            "vol_candle_2_fmt":    alert.get("vol_candle_2_fmt"),
            "vol_candle_3_fmt":    alert.get("vol_candle_3_fmt"),
            "vol_candle_1_base":       alert.get("vol_candle_1_base"),
            "vol_candle_2_base":       alert.get("vol_candle_2_base"),
            "vol_candle_3_base":       alert.get("vol_candle_3_base"),
            "vol_candle_1_base_fmt":   alert.get("vol_candle_1_base_fmt"),
            "vol_candle_2_base_fmt":   alert.get("vol_candle_2_base_fmt"),
            "vol_candle_3_base_fmt":   alert.get("vol_candle_3_base_fmt"),
            "vol_ratio":           alert.get("vol_ratio"),
            "candle_colors":       alert.get("candle_colors"),
            "rvol":                alert.get("rvol"),
            "btc_price":           alert.get("btc_price"),
            "candle_time":         alert.get("candle_time"),
            "soft_flags":          alert.get("soft_flags", 0),
            "soft_flag_details":   alert.get("soft_flag_details", []),
            "quality_score":       alert.get("quality_score", 0),
            "quality_details":     alert.get("quality_details", []),
            "additional_data":     alert.get("additional_data", {}),
            "btc_trend_at_entry":  alert.get("btc_trend", "unknown"),
            "btc_trend_detail":    alert.get("btc_trend_detail", {}),
            "tp_sent":             [],
            "reversal_warned":     False,
            "outcome":             self._init_outcome(self._tp_targets),
            "price_journey":       [],
        }

        with self._lock:
            signals = self._load(self._signals_file)
            signals.append(signal)
            self._save(self._signals_file, signals)

        logger.info("Tracker: recorded %s @ $%.8f", signal["symbol"], price)

    # ── price updates ────────────────────────────────────────────────

    def apply_prices(self, prices: Dict[str, float]) -> None:
        with self._lock:
            signals = self._load(self._signals_file)
            if not signals:
                return
            changed = False
            now = time.time()
            btc_price = prices.get("BTCUSDT")

            for sig in signals:
                sym = sig["symbol"]
                if sym not in prices:
                    continue
                current = prices[sym]
                entry = sig.get("entry_price", 0)
                prev_update_ts = sig.get("last_update_ts", sig.get("alert_time_ts", now))
                sig["current_price"] = current
                sig["last_update_ts"] = now

                if current > sig.get("highest_price", 0):
                    sig["highest_price"] = current
                lowest = sig.get("lowest_price", current)
                if lowest == 0 or current < lowest:
                    sig["lowest_price"] = current

                if entry > 0:
                    self._update_outcome(sig, current, entry, now, btc_price, prev_update_ts)
                    self._record_journey_snapshot(sig, current, entry, now, btc_price)

                changed = True
            if changed:
                self._save(self._signals_file, signals)

    def _ensure_outcome(self, sig: dict) -> dict:
        outcome = sig.get("outcome")
        if outcome is None:
            outcome = self._init_outcome(self._tp_targets)
            sig["outcome"] = outcome
            tp_sent = sig.get("tp_sent", [])
            if tp_sent:
                for tp in tp_sent:
                    key = f"tp{tp}"
                    if f"{key}_hit" not in outcome:
                        outcome[f"{key}_hit"] = False
                        outcome[f"{key}_hit_time"] = None
                        outcome[f"{key}_hit_hours_after_entry"] = None
                        outcome[f"{key}_max_drawdown_before"] = None
                        outcome[f"{key}_btc_price_at_hit"] = None
                    outcome[f"{key}_hit"] = True
        outcome.pop("high_breakout_warning", None)
        for tp in self._tp_targets:
            key = f"tp{tp}"
            if f"{key}_hit" not in outcome:
                outcome[f"{key}_hit"] = False
                outcome[f"{key}_hit_time"] = None
                outcome[f"{key}_hit_hours_after_entry"] = None
                outcome[f"{key}_max_drawdown_before"] = None
                outcome[f"{key}_btc_price_at_hit"] = None
        for field, default in (
            ("signal_closed", False),
            ("close_reason", None),
            ("close_time", None),
            ("btc_change_entry_to_tp", None),
            ("btc_trend_during_signal", None),
        ):
            if field not in outcome:
                outcome[field] = default
        return outcome

    def _update_outcome(
        self, sig: dict, current: float, entry: float, now: float,
        btc_price: Optional[float], prev_update_ts: float,
    ) -> None:
        outcome = self._ensure_outcome(sig)

        alert_ts = sig["alert_time_ts"]
        cur_pct = ((current - entry) / entry) * 100.0

        if cur_pct < outcome.get("max_drawdown_pct", 0.0):
            outcome["max_drawdown_pct"] = round(cur_pct, 2)
            outcome["max_drawdown_time"] = self._ts_to_utc(now)
            outcome["max_drawdown_hours_after_entry"] = self._hours_since(alert_ts, now)

        highest = sig.get("highest_price", entry)
        high_pct = ((highest - entry) / entry) * 100.0
        if high_pct > outcome.get("peak_pct", 0.0):
            outcome["peak_pct"] = round(high_pct, 2)
            outcome["peak_time"] = self._ts_to_utc(now)
            outcome["peak_hours_after_entry"] = self._hours_since(alert_ts, now)

        has_any_tp = any(outcome.get(f"tp{tp}_hit", False) for tp in self._tp_targets)
        if cur_pct < 0 and not has_any_tp:
            outcome["went_negative_before_tp"] = True

        elapsed_hours = (now - prev_update_ts) / 3600.0
        if cur_pct < 0 and elapsed_hours > 0:
            outcome["hours_negative_total"] = round(
                outcome.get("hours_negative_total", 0.0) + elapsed_hours, 2
            )

        outcome["signal_type"] = self._classify_signal_type(sig)

    def _record_journey_snapshot(
        self, sig: dict, current: float, entry: float, now: float,
        btc_price: Optional[float],
    ) -> None:
        journey = sig.get("price_journey")
        if journey is None:
            journey = []
            sig["price_journey"] = journey

        alert_ts = sig["alert_time_ts"]
        prev_highest = sig.get("_prev_highest", sig.get("highest_price", entry))
        prev_lowest = sig.get("_prev_lowest", sig.get("lowest_price", entry))

        events: list[str] = []

        is_new_high = current > prev_highest
        is_new_low = current < prev_lowest if prev_lowest > 0 else False

        if is_new_high:
            events.append("new_high")
        if is_new_low:
            events.append("new_low")

        if current < entry:
            was_above = prev_highest >= entry and (not journey or journey[-1].get("price", entry) >= entry)
            if was_above:
                events.append("below_entry")

        btc_entry = sig.get("btc_price")
        if btc_entry and btc_price and btc_entry > 0:
            btc_pct_now = ((btc_price - btc_entry) / btc_entry) * 100.0
            last_btc_pct = 0.0
            if journey:
                last_btc_pct = journey[-1].get("btc_pct_from_signal_entry", 0.0) or 0.0
            if abs(btc_pct_now - last_btc_pct) >= 2.0:
                events.append("btc_move")

        hours_since = (now - alert_ts) / 3600.0
        last_checkpoint_hour = 0.0
        if journey:
            for snap in reversed(journey):
                if "4h_checkpoint" in snap.get("event", ""):
                    last_checkpoint_hour = snap.get("hours_after_entry", 0.0)
                    break
        if hours_since - last_checkpoint_hour >= 4.0:
            events.append("4h_checkpoint")

        if not events:
            return

        cur_pct = ((current - entry) / entry) * 100.0
        btc_pct_from_entry = None
        if btc_entry and btc_price and btc_entry > 0:
            btc_pct_from_entry = round(((btc_price - btc_entry) / btc_entry) * 100.0, 2)

        vol_1h, vol_1h_base = self._fetch_latest_volume(sig["symbol"])

        snapshot = {
            "event": "+".join(events),
            "timestamp": self._ts_to_utc(now),
            "timestamp_ts": now,
            "hours_after_entry": self._hours_since(alert_ts, now),
            "price": current,
            "pct_from_entry": round(cur_pct, 2),
            "btc_price": btc_price,
            "btc_pct_from_signal_entry": btc_pct_from_entry,
            "volume_1h": vol_1h,
            "volume_1h_base": vol_1h_base,
            "is_new_low": is_new_low,
            "is_new_high": is_new_high,
        }
        journey.append(snapshot)
        journey.sort(key=lambda s: s.get("timestamp_ts", 0))

        sig["_prev_highest"] = sig.get("highest_price", current)
        sig["_prev_lowest"] = sig.get("lowest_price", current)

    def _fetch_latest_volume(self, symbol: str):
        try:
            klines = self._binance.get_closed_klines(symbol, "1h", 1)
            if klines:
                return klines[-1].get("quote_volume"), klines[-1].get("volume")
        except Exception:
            pass
        return None, None

    @staticmethod
    def _ema(values: List[float], period: int) -> float:
        if len(values) < period:
            return 0.0
        k = 2 / (period + 1)
        ema = sum(values[:period]) / period
        for v in values[period:]:
            ema = v * k + ema * (1 - k)
        return ema

    def _fetch_snapshot_market_data(self, symbol: str) -> dict:
        data: dict = {}

        try:
            data["candles_1h"] = self._binance.get_closed_klines(symbol, "1h", 25)
        except Exception:
            data["candles_1h"] = []

        try:
            data["oi_hist"] = self._binance.get_oi_history(symbol, "1h", 25)
        except Exception:
            data["oi_hist"] = []

        try:
            data["funding_rate"] = self._binance.get_funding_rate(symbol)
        except Exception:
            data["funding_rate"] = None

        try:
            data["candles_4h"] = self._binance.get_closed_klines(symbol, "4h", 55)
        except Exception:
            data["candles_4h"] = []

        try:
            if self._market_cap is not None:
                base = symbol.replace("USDT", "").replace("BUSD", "")
                data["market_cap_usd"] = self._market_cap.get(base)
                data["market_cap_fmt"] = self._market_cap.format(base)
        except Exception:
            pass

        return data

    def _build_tp_snapshot(
        self, symbol: str, sig: dict, target: int,
        now: float, btc_at_check: Optional[float],
        market_data: dict, cached_tickers: Optional[dict],
    ) -> dict:
        entry = sig.get("entry_price", 0)
        outcome = sig.get("outcome", {})

        snapshot: dict = {
            "hit_time": self._ts_to_utc(now),
            "hit_hours_after_entry": self._hours_since(sig["alert_time_ts"], now),
            "max_drawdown_before": outcome.get("max_drawdown_pct", 0.0),
            "btc_price_at_hit": btc_at_check,
        }

        btc_entry = sig.get("btc_price")
        if btc_entry and btc_at_check and btc_entry > 0:
            snapshot["btc_pct_change_since_entry"] = round(
                ((btc_at_check - btc_entry) / btc_entry) * 100.0, 2
            )
        else:
            snapshot["btc_pct_change_since_entry"] = None

        candles_1h = market_data.get("candles_1h", [])
        last = candles_1h[-1] if candles_1h else None

        try:
            if candles_1h and len(candles_1h) >= 21 and last:
                baseline = candles_1h[-(20 + 1):-1]
                if baseline:
                    avg_b = sum(c["quote_volume"] for c in baseline) / len(baseline)
                    snapshot["rvol_20"] = round(last["quote_volume"] / avg_b, 2) if avg_b > 0 else None
                    snapshot["vol_baseline_avg"] = round(avg_b, 2)
            if last:
                snapshot["volume_1h"] = last.get("quote_volume")
                snapshot["volume_1h_base"] = last.get("volume")
        except Exception:
            pass

        try:
            oi_hist = market_data.get("oi_hist", [])
            if len(oi_hist) >= 2:
                current_oi = oi_hist[-1]["oi_value_usdt"]
                prev_oi_values = [h["oi_value_usdt"] for h in oi_hist[:-1]]
                avg_oi = sum(prev_oi_values) / len(prev_oi_values)
                snapshot["oi_current_usdt"] = round(current_oi, 2)
                snapshot["oi_avg_24h_usdt"] = round(avg_oi, 2)
                snapshot["oi_change_pct"] = round(((current_oi - avg_oi) / avg_oi) * 100, 2) if avg_oi > 0 else None
                if len(oi_hist) >= 3:
                    oi_changes = [
                        oi_hist[i]["oi_value_usdt"] - oi_hist[i - 1]["oi_value_usdt"]
                        for i in range(1, len(oi_hist))
                    ]
                    current_oi_growth = oi_changes[-1]
                    avg_oi_growth = sum(oi_changes[:-1]) / len(oi_changes[:-1]) if oi_changes[:-1] else 0
                    snapshot["oi_growth_current"] = round(current_oi_growth, 2)
                    snapshot["oi_growth_avg"] = round(avg_oi_growth, 2)
                    if avg_oi_growth != 0:
                        snapshot["oi_growth_ratio"] = round(current_oi_growth / abs(avg_oi_growth), 2)
        except Exception:
            pass

        try:
            fr = market_data.get("funding_rate")
            if fr is not None:
                snapshot["funding_rate"] = round(fr * 100, 4)
                snapshot["funding_in_ideal_range"] = -0.02 <= fr * 100 <= 0.15
        except Exception:
            pass

        try:
            if cached_tickers:
                ticker = cached_tickers.get(symbol, {})
                vol_24h = ticker.get("quote_volume_24h", 0)
                snapshot["vol_24h_usdt"] = round(vol_24h, 2)
                snapshot["vol_24h_above_50m"] = vol_24h >= 50_000_000
                vol_24h_base = ticker.get("volume_24h", 0)
                snapshot["vol_24h_base"] = round(vol_24h_base, 2)
        except Exception:
            pass

        try:
            candles_4h = market_data.get("candles_4h", [])
            if len(candles_4h) >= 50:
                closes_4h = [c["close"] for c in candles_4h]
                ema50 = self._ema(closes_4h, 50)
                current_price = sig.get("current_price", entry)
                snapshot["ema50_4h"] = round(ema50, 8)
                snapshot["price_above_ema50_4h"] = current_price > ema50
                snapshot["ema50_distance_pct"] = round(((current_price - ema50) / ema50) * 100, 2) if ema50 > 0 else None
        except Exception:
            pass

        try:
            if candles_1h and len(candles_1h) >= 20:
                recent_10 = candles_1h[-10:]
                prior_10 = candles_1h[-20:-10]

                def avg_range(cs):
                    return sum((c["high"] - c["low"]) / c["close"] * 100 for c in cs if c["close"] > 0) / len(cs)

                recent_range_pct = avg_range(recent_10)
                prior_range_pct = avg_range(prior_10)
                snapshot["volatility_recent_10_pct"] = round(recent_range_pct, 4)
                snapshot["volatility_prior_10_pct"] = round(prior_range_pct, 4)
                if prior_range_pct > 0:
                    compression_ratio = recent_range_pct / prior_range_pct
                    snapshot["volatility_compression_ratio"] = round(compression_ratio, 3)
                    snapshot["is_compressed"] = compression_ratio < 0.7
        except Exception:
            pass

        try:
            mcap = market_data.get("market_cap_usd")
            if mcap is not None:
                snapshot["market_cap_usd"] = mcap
                snapshot["market_cap_fmt"] = market_data.get("market_cap_fmt")
        except Exception:
            pass

        try:
            if candles_1h and len(candles_1h) >= 2:
                prev_close = candles_1h[-2]["close"]
                cur_close = candles_1h[-1]["close"]
                if prev_close > 0:
                    snapshot["price_momentum_1h_pct"] = round(((cur_close - prev_close) / prev_close) * 100, 2)
        except Exception:
            pass

        try:
            candles_4h = market_data.get("candles_4h", [])
            if len(candles_4h) >= 2:
                prev_close_4h = candles_4h[-2]["close"]
                cur_close_4h = candles_4h[-1]["close"]
                if prev_close_4h > 0:
                    snapshot["price_momentum_4h_pct"] = round(((cur_close_4h - prev_close_4h) / prev_close_4h) * 100, 2)
        except Exception:
            pass

        try:
            if candles_1h and len(candles_1h) >= 3:
                last_3 = candles_1h[-3:]
                snapshot["candle_colors_at_hit"] = [
                    "green" if c["close"] >= c["open"] else "red" for c in last_3
                ]
        except Exception:
            pass

        return snapshot

    def _add_journey_event(self, sig: dict, event: str, now: float,
                           btc_price: Optional[float]) -> None:
        journey = sig.get("price_journey")
        if journey is None:
            journey = []
            sig["price_journey"] = journey

        entry = sig.get("entry_price", 0)
        current = sig.get("current_price", entry)
        cur_pct = ((current - entry) / entry) * 100.0 if entry > 0 else 0.0
        btc_entry = sig.get("btc_price")
        btc_pct = None
        if btc_entry and btc_price and btc_entry > 0:
            btc_pct = round(((btc_price - btc_entry) / btc_entry) * 100.0, 2)

        vol_1h, vol_1h_base = self._fetch_latest_volume(sig["symbol"])

        snapshot = {
            "event": event,
            "timestamp": self._ts_to_utc(now),
            "timestamp_ts": now,
            "hours_after_entry": self._hours_since(sig["alert_time_ts"], now),
            "price": current,
            "pct_from_entry": round(cur_pct, 2),
            "btc_price": btc_price,
            "btc_pct_from_signal_entry": btc_pct,
            "volume_1h": vol_1h,
            "volume_1h_base": vol_1h_base,
            "is_new_low": False,
            "is_new_high": False,
        }
        journey.append(snapshot)
        journey.sort(key=lambda s: s.get("timestamp_ts", 0))

    def fetch_and_apply(self) -> None:
        try:
            prices = self._binance.get_mark_prices()
            self.apply_prices(prices)
        except Exception as exc:
            logger.warning("Tracker price update failed: %s", exc)

    # ── take-profit checking ─────────────────────────────────────────

    def _check_take_profits(self) -> None:
        with self._lock:
            signals = self._load(self._signals_file)
            if not signals:
                return

            changed = False
            alerts_to_send: list[dict] = []
            now = time.time()

            try:
                cached_prices = self._binance.get_mark_prices()
                btc_at_check = cached_prices.get("BTCUSDT")
            except Exception:
                btc_at_check = None

            cached_tickers: Optional[dict] = None

            for sig in signals:
                entry = sig.get("entry_price", 0)
                if entry <= 0:
                    continue

                highest = sig.get("highest_price", entry)
                current = sig.get("current_price", entry)
                high_pct = ((highest - entry) / entry) * 100
                cur_pct = ((current - entry) / entry) * 100
                age_str = self._fmt_age(sig["alert_time_ts"])

                tp_sent: list = sig.get("tp_sent", [])
                outcome = self._ensure_outcome(sig)

                new_hits = []
                for target in self._tp_targets:
                    if target in tp_sent:
                        continue
                    if high_pct >= target:
                        new_hits.append(target)

                if new_hits:
                    if cached_tickers is None:
                        try:
                            cached_tickers = self._binance.get_24h_tickers()
                        except Exception:
                            cached_tickers = {}

                    try:
                        market_data = self._fetch_snapshot_market_data(sig["symbol"])
                    except Exception:
                        market_data = {}

                for target in new_hits:
                    tp_sent.append(target)
                    changed = True

                    key = f"tp{target}"
                    outcome[f"{key}_hit"] = True
                    outcome[f"{key}_hit_time"] = self._ts_to_utc(now)
                    outcome[f"{key}_hit_hours_after_entry"] = self._hours_since(sig["alert_time_ts"], now)
                    outcome[f"{key}_max_drawdown_before"] = outcome.get("max_drawdown_pct", 0.0)
                    outcome[f"{key}_btc_price_at_hit"] = btc_at_check

                    if outcome.get("btc_change_entry_to_tp") is None:
                        btc_entry = sig.get("btc_price")
                        if btc_entry and btc_at_check and btc_entry > 0:
                            outcome["btc_change_entry_to_tp"] = round(
                                ((btc_at_check - btc_entry) / btc_entry) * 100.0, 2
                            )

                    try:
                        tp_snapshot = self._build_tp_snapshot(
                            sig["symbol"], sig, target, now, btc_at_check,
                            market_data, cached_tickers,
                        )
                        sig[f"{key}_snapshot"] = tp_snapshot
                    except Exception as exc:
                        logger.warning("TP snapshot collection failed for %s +%d%%: %s",
                                       sig["symbol"], target, exc)

                    self._add_journey_event(sig, f"tp_hit_{target}", now, btc_at_check)

                    alerts_to_send.append({
                        "type":          "take_profit",
                        "symbol":        sig["symbol"],
                        "target":        target,
                        "entry_price":   entry,
                        "current_price": current,
                        "highest_price": highest,
                        "cur_pct":       cur_pct,
                        "high_pct":      high_pct,
                        "age_str":       age_str,
                    })
                    logger.info(
                        "🎯 TP target +%d%% hit for %s (peak: +%.2f%%, now: %+.2f%%)",
                        target, sig["symbol"], high_pct, cur_pct,
                    )

                sig["tp_sent"] = tp_sent

                outcome["signal_type"] = self._classify_signal_type(sig)

                if (
                    self._reversal_enabled
                    and not sig.get("reversal_warned", False)
                    and high_pct >= self._min_reversal_peak
                ):
                    drop_from_peak = high_pct - cur_pct
                    if drop_from_peak >= self._reversal_drop:
                        sig["reversal_warned"] = True
                        changed = True
                        alerts_to_send.append({
                            "type":          "reversal",
                            "symbol":        sig["symbol"],
                            "entry_price":   entry,
                            "current_price": current,
                            "highest_price": highest,
                            "cur_pct":       cur_pct,
                            "high_pct":      high_pct,
                            "drop_pct":      drop_from_peak,
                            "age_str":       age_str,
                        })
                        logger.info(
                            "⚠️ Reversal warning for %s (peak: +%.2f%%, now: %+.2f%%, drop: %.2f%%)",
                            sig["symbol"], high_pct, cur_pct, drop_from_peak,
                        )

            if changed:
                self._save(self._signals_file, signals)

        for alert in alerts_to_send:
            try:
                if alert["type"] == "take_profit":
                    self._notifier.send_take_profit(alert)
                elif alert["type"] == "reversal":
                    self._notifier.send_reversal_warning(alert)
                time.sleep(0.5)
            except Exception as exc:
                logger.error("Failed to send %s alert: %s", alert["type"], exc)

    # ── signal type classification ────────────────────────────────────

    def _classify_signal_type(self, sig: dict, is_archiving: bool = False) -> str:
        outcome = sig.get("outcome", {})
        first_tp_hours = None

        for tp in self._tp_targets:
            key = f"tp{tp}"
            if outcome.get(f"{key}_hit"):
                hours = outcome.get(f"{key}_hit_hours_after_entry")
                if hours is not None:
                    if first_tp_hours is None or hours < first_tp_hours:
                        first_tp_hours = hours

        if first_tp_hours is None:
            return "failed" if is_archiving else "active"
        if first_tp_hours < 6:
            return "fast"
        if first_tp_hours <= 72:
            return "slow"
        return "delayed"

    # ── archive expired ──────────────────────────────────────────────

    def _monthly_gz_path(self, ts: float) -> Path:
        dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        return self._data_dir / f"signals_{dt.year}_{dt.month:02d}.json.gz"

    def _load_gzip(self, path: Path) -> list:
        if not path.exists():
            return []
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                data = json.load(fh)
                return data if isinstance(data, list) else []
        except (json.JSONDecodeError, IOError, OSError) as exc:
            logger.error("Failed to read gzip %s: %s", path, exc)
            return []

    def _save_gzip(self, path: Path, data: list) -> None:
        tmp = path.with_suffix(".tmp.gz")
        try:
            with gzip.open(tmp, "wt", encoding="utf-8") as fh:
                json.dump(data, fh, separators=(",", ":"))
            tmp.replace(path)
        except IOError as exc:
            logger.error("Failed to write gzip %s: %s", path, exc)
            raise

    def _append_to_monthly_gz(self, signals: list) -> None:
        by_month: Dict[Path, list] = {}
        for sig in signals:
            ts = sig.get("alert_time_ts", time.time())
            gz_path = self._monthly_gz_path(ts)
            by_month.setdefault(gz_path, []).append(sig)

        for gz_path, sigs in by_month.items():
            existing = self._load_gzip(gz_path)
            existing.extend(sigs)
            self._save_gzip(gz_path, existing)
            logger.info("Appended %d signal(s) to %s (total: %d)",
                        len(sigs), gz_path.name, len(existing))

    def archive_expired(self) -> int:
        now = time.time()
        with self._lock:
            signals = self._load(self._signals_file)

            active = []
            archived = 0
            newly_archived = []

            try:
                btc_exit = self._binance.get_mark_prices().get("BTCUSDT")
            except Exception:
                btc_exit = None

            for sig in signals:
                age = now - sig["alert_time_ts"]
                if age >= self._max_age:
                    entry = sig.get("entry_price", 0)
                    highest = sig.get("highest_price", 0)
                    lowest = sig.get("lowest_price", 0)
                    current = sig.get("current_price", 0)
                    sig["archived_time_ts"] = now
                    sig["archived_time"] = datetime.now(timezone.utc).strftime(
                        "%Y-%m-%d %H:%M:%S UTC"
                    )
                    sig["tracked_hours"] = round(age / 3600, 1)
                    if entry > 0:
                        sig["peak_pct"] = round(((highest - entry) / entry) * 100, 2)
                        sig["lowest_pct"] = round(((lowest - entry) / entry) * 100, 2) if lowest > 0 else None
                        sig["exit_pct"] = round(((current - entry) / entry) * 100, 2)
                        sig["exit_price"] = current
                        sig["highest_pct"] = sig["peak_pct"]
                    if self._market_cap is not None:
                        try:
                            base = sig["symbol"].replace("USDT", "").replace("BUSD", "")
                            sig["market_cap_usd_exit"] = self._market_cap.get(base)
                            sig["market_cap_exit_fmt"] = self._market_cap.format(base)
                        except Exception:
                            pass

                    outcome = self._ensure_outcome(sig)
                    outcome["signal_type"] = self._classify_signal_type(sig, is_archiving=True)
                    outcome["signal_closed"] = True
                    outcome["close_reason"] = "expired"
                    outcome["close_time"] = sig["archived_time"]

                    btc_entry = sig.get("btc_price")
                    if btc_entry and btc_entry > 0:
                        btc_ref = None
                        for tp in self._tp_targets:
                            bp = outcome.get(f"tp{tp}_btc_price_at_hit")
                            if bp:
                                btc_ref = bp
                                break
                        if btc_ref is None:
                            btc_ref = btc_exit
                        if btc_ref:
                            btc_chg = ((btc_ref - btc_entry) / btc_entry) * 100.0
                            if btc_chg > 2.0:
                                outcome["btc_trend_during_signal"] = "pumping"
                            elif btc_chg < -2.0:
                                outcome["btc_trend_during_signal"] = "dumping"
                            else:
                                outcome["btc_trend_during_signal"] = "ranging"

                    sig.pop("_prev_highest", None)
                    sig.pop("_prev_lowest", None)

                    newly_archived.append(sig)
                    archived += 1
                else:
                    active.append(sig)

            if archived > 0:
                try:
                    self._append_to_monthly_gz(newly_archived)
                except Exception as exc:
                    logger.error("CRITICAL: gzip archive write failed, keeping signals active: %s", exc)
                    return 0
                self._save(self._signals_file, active)

        if archived > 0:
            self._add_to_pending(newly_archived)

        return archived

    def _add_to_pending(self, signals: list) -> None:
        with self._lock:
            pending = self._load(self._pending_file)
            pending.extend(signals)
            self._save(self._pending_file, pending)
        logger.info("Queued %d signal(s) for daily report", len(signals))

    def _check_daily_report(self) -> None:
        now_utc = datetime.now(timezone.utc)
        if now_utc.hour != self._daily_report_hour:
            return

        today_str = now_utc.strftime("%Y-%m-%d")

        last_sent = ""
        if self._last_report_file.exists():
            try:
                last_sent = self._last_report_file.read_text(encoding="utf-8").strip()
            except IOError:
                pass

        if last_sent == today_str:
            return

        with self._lock:
            pending = self._load(self._pending_file)
            if not pending:
                self._last_report_file.write_text(today_str, encoding="utf-8")
                return

        tmp_path = self._data_dir / f"report_{today_str}.json"
        sent = False
        try:
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(pending, fh, indent=2)
            count = len(pending)
            symbols = ", ".join(s["symbol"] for s in pending)
            caption = (
                f"Daily 7-day report — {count} signal{'s' if count != 1 else ''} completed\n"
                f"{symbols}"
            )
            sent = self._notifier.send_document(str(tmp_path), caption=caption)
            if sent:
                logger.info("Sent daily report for %d signal(s): %s", count, symbols)
            else:
                logger.error("Failed to send daily report — will retry next cycle")
        except Exception as exc:
            logger.error("Failed to send daily report: %s", exc)
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        if sent:
            with self._lock:
                self._save(self._pending_file, [])
            try:
                self._last_report_file.write_text(today_str, encoding="utf-8")
            except IOError as exc:
                logger.error("Failed to save last_report_date: %s", exc)

    # ── data access ──────────────────────────────────────────────────

    def get_active_signals(self) -> List[dict]:
        now = time.time()
        with self._lock:
            signals = self._load(self._signals_file)
        return [s for s in signals if now - s["alert_time_ts"] < self._max_age]

    def get_tracked_symbols(self) -> Set[str]:
        with self._lock:
            signals = self._load(self._signals_file)
        return {s["symbol"] for s in signals}

    def get_history(self) -> List[dict]:
        with self._lock:
            history = self._load(self._history_file)
            for gz_file in sorted(self._data_dir.glob("signals_*.json.gz")):
                history.extend(self._load_gzip(gz_file))
            return history

    def get_completed_signals(self, min_age_seconds: float) -> List[dict]:
        """Return archived signals that have been tracked for at least min_age_seconds."""
        now = time.time()
        history = self.get_history()
        return [
            h for h in history
            if (now - h.get("alert_time_ts", now)) >= min_age_seconds
        ]

    @property
    def max_age_hours(self) -> int:
        return int(self._max_age // 3600)

    @property
    def tp_targets(self) -> List[int]:
        return self._tp_targets

    @property
    def detailed_report_min_age_seconds(self) -> float:
        return self._detailed_min_age

    # ── background loop ──────────────────────────────────────────────

    def run(self) -> None:
        self._running = True
        logger.info("Tracker background loop started (every %ds)", self._update_interval)
        while self._running:
            try:
                self.fetch_and_apply()
                self._check_take_profits()
                archived = self.archive_expired()
                if archived:
                    logger.info("Tracker: archived %d expired signals", archived)
                self._check_daily_report()
            except Exception:
                logger.error("Tracker loop error", exc_info=True)
            self._sleep(self._update_interval)

    def stop(self) -> None:
        self._running = False

    def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(1.0)
