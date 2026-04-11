"""
Core scanner engine.

Strategy (3 main conditions — ALL must pass to fire a signal):
  1. Current 1h candle closes above the highest high of the last 24 candles (24h breakout)
  2. Last 3 consecutive 1h candles have strictly increasing volume
  3. 24h price change is within ±20%

Hard filters (ALL must pass after main criteria):
  4. vol_ratio ≤ 15
  5. funding_rate > -0.05
  6. vol_24h_usdt > $5,000,000
  7. market_cap_usd < $1,000,000,000

Soft flags — data-driven (each true adds 1 flag; 4+ flags = signal blocked):
  Flags warn about conditions correlated with FAILURE in 333-signal backtest:
  1. RVOL < 2x (low relative volume — 31% TP10 vs 48% for 4-8x)
  2. Market cap > $200M (large caps — only 23% TP10)
  3. OI growth ratio > 50 (extreme OI surge)
  4. Funding rate < -0.02 (heavily negative funding)
  5. 24h volume < $5M (low liquidity)
  6. |24h price change| > 15% (overextended move)
  7. EMA50 distance > 15% (very far from trend support)

Quality score (0–8 points): higher = better signal.
  Data-driven scoring based on 333-signal backtest:
  1. RVOL 4-8x (sweet spot — 48% TP10, 17.9% avg peak)
  2. RVOL >= 2x (adequate volume — <2x underperforms)
  3. Market cap $10-50M (best range — 50% TP10)
  4. OI growth ratio 5-50 (moderate — best performing range)
  5. Funding rate >= 0 (neutral/positive — healthy)
  6. 24h volume >= $10M (good liquidity)
  7. Breakout margin 0.5-5% (conviction without overextension)
  8. 24h price change 0-10% (positive momentum, not extreme)

Additional data is collected at signal time for analysis:
  - RVOL vs 20-candle baseline
  - Relative OI change vs 24h average
  - Funding rate
  - 24h volume (liquidity check)
  - Price vs 4h EMA50
  - Volatility compression score (last 10 vs prior 10 candles)
  - Breakout margin %
"""

from __future__ import annotations

import logging
import math
import time
from datetime import datetime, timezone
from typing import Dict, List, Optional

from binance_client import BinanceClient
from market_cap import MarketCapProvider
from notifier import TelegramNotifier
from tracker import SignalTracker

logger = logging.getLogger(__name__)


class _CooldownTracker:
    def __init__(self, cooldown_seconds: float) -> None:
        self._cooldown = cooldown_seconds
        self._last_alert: Dict[str, float] = {}

    def is_on_cooldown(self, symbol: str) -> bool:
        last = self._last_alert.get(symbol)
        if last is None:
            return False
        remaining = self._cooldown - (time.time() - last)
        if remaining > 0:
            logger.debug("%s  on cooldown — %.1f min remaining", symbol, remaining / 60)
            return True
        return False

    def record(self, symbol: str) -> None:
        self._last_alert[symbol] = time.time()

    def prune(self) -> None:
        now = time.time()
        expired = [s for s, t in self._last_alert.items() if now - t > self._cooldown]
        for s in expired:
            del self._last_alert[s]

    @property
    def active_count(self) -> int:
        now = time.time()
        return sum(1 for t in self._last_alert.values() if now - t < self._cooldown)


class Scanner:

    def __init__(
        self,
        config: dict,
        binance: BinanceClient,
        notifier: TelegramNotifier,
        tracker: Optional[SignalTracker] = None,
        market_cap: Optional[MarketCapProvider] = None,
    ) -> None:
        sc = config["scanner"]

        self.timeframe:              str   = sc.get("timeframe", "1h")
        self.interval:               int   = sc.get("scan_interval_seconds", 900)
        self.brk_lookback:           int   = sc.get("breakout_lookback_candles", 24)
        self.consec_vol_candles:     int   = sc.get("consecutive_vol_candles", 3)
        self.max_price_chg_24h:      float = sc.get("max_price_change_24h_pct", 20.0)
        self.min_vol_usdt:           float = sc.get("min_volume_usdt", 0)
        self.vol_ratio_min:          float = sc.get("consecutive_vol_min_ratio", 2.0)
        self.high_brk_warn_pct:      float = sc.get("high_breakout_warning_pct", 5.0)
        self.cooldown_hours:         float = sc.get("cooldown_hours", 12)
        self.excluded:               set   = set(sc.get("excluded_symbols", []))

        hf = sc.get("hard_filters", {})
        self.hf_vol_ratio_max:       float = hf.get("vol_ratio_max", 15)
        self.hf_funding_rate_min:    float = hf.get("funding_rate_min", -0.05)
        self.hf_vol_24h_usdt_min:    float = hf.get("vol_24h_usdt_min", 5_000_000)
        self.hf_market_cap_max:      float = hf.get("market_cap_usd_max", 1_000_000_000)

        sf = sc.get("soft_flags", {})
        self.sf_rvol_min:            float = sf.get("rvol_min", 2.0)
        self.sf_mcap_max:            float = sf.get("market_cap_usd_max", 200_000_000)
        self.sf_oi_ratio_max:        float = sf.get("oi_growth_ratio_max", 50)
        self.sf_funding_rate_min:    float = sf.get("funding_rate_min", -0.02)
        self.sf_vol_24h_min:         float = sf.get("vol_24h_usdt_min", 5_000_000)
        self.sf_price_chg_max:       float = sf.get("price_change_24h_max", 15.0)
        self.sf_ema50_dist_max:      float = sf.get("ema50_distance_pct_max", 15.0)
        self.sf_max_flags:           int   = sf.get("max_flags_to_block", 4)

        qs = sc.get("quality_score", {})
        self.qs_rvol_sweet_min:      float = qs.get("rvol_sweet_spot_min", 4.0)
        self.qs_rvol_sweet_max:      float = qs.get("rvol_sweet_spot_max", 8.0)
        self.qs_rvol_adequate_min:   float = qs.get("rvol_adequate_min", 2.0)
        self.qs_mcap_min:            float = qs.get("market_cap_usd_min", 10_000_000)
        self.qs_mcap_max:            float = qs.get("market_cap_usd_max", 50_000_000)
        self.qs_oi_ratio_min:        float = qs.get("oi_growth_ratio_min", 5)
        self.qs_oi_ratio_max:        float = qs.get("oi_growth_ratio_max", 50)
        self.qs_funding_min:         float = qs.get("funding_rate_min", 0)
        self.qs_vol_24h_min:         float = qs.get("vol_24h_usdt_min", 10_000_000)
        self.qs_brk_margin_min:      float = qs.get("breakout_margin_pct_min", 0.5)
        self.qs_brk_margin_max:      float = qs.get("breakout_margin_pct_max", 5.0)
        self.qs_price_chg_min:       float = qs.get("price_change_24h_min", 0)
        self.qs_price_chg_max:       float = qs.get("price_change_24h_max", 10.0)

        self._candles_needed = max(self.brk_lookback + 1, self.consec_vol_candles, 20)

        self._binance = binance
        self._tg = notifier
        self._tracker = tracker
        self._market_cap = market_cap
        self._cooldown = _CooldownTracker(cooldown_seconds=self.cooldown_hours * 3600)
        self._mark_prices: Dict[str, float] = {}
        self._tickers: Dict[str, dict] = {}
        self._running = False

    @staticmethod
    def _fmt_vol_usd(vol: float) -> str:
        if vol >= 1e9:
            return f"${vol / 1e9:.1f}B"
        if vol >= 1e6:
            return f"${vol / 1e6:.2f}M"
        if vol >= 1e3:
            return f"${vol / 1e3:.0f}K"
        return f"${vol:.0f}"

    @staticmethod
    def _fmt_vol_count(vol: float) -> str:
        if vol >= 1e9:
            return f"{vol / 1e9:.1f}B"
        if vol >= 1e6:
            return f"{vol / 1e6:.2f}M"
        if vol >= 1e3:
            return f"{vol / 1e3:.0f}K"
        return f"{vol:.0f}"

    @staticmethod
    def _ema(values: List[float], period: int) -> float:
        """Calculate EMA for a list of values."""
        if len(values) < period:
            return 0.0
        k = 2 / (period + 1)
        ema = sum(values[:period]) / period
        for v in values[period:]:
            ema = v * k + ema * (1 - k)
        return ema

    def stop(self) -> None:
        self._running = False

    def run(self) -> None:
        try:
            self._send_startup()
        except Exception as exc:
            logger.warning("Startup message failed (non-fatal): %s", exc)
        self._running = True
        logger.info(
            "Scanner loop started  (interval %ds, need %d candles/symbol, cooldown %.1fh)",
            self.interval, self._candles_needed, self.cooldown_hours,
        )
        while self._running:
            t0 = time.time()
            try:
                self._cycle()
            except Exception:
                logger.error("Scan cycle error — will retry next interval", exc_info=True)
            elapsed = time.time() - t0
            logger.info("Cycle finished in %.1fs", elapsed)
            self._sleep(max(0.0, self.interval - elapsed))

    def _sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while self._running and time.time() < end:
            time.sleep(min(1.0, end - time.time()))

    def _cycle(self) -> None:
        try:
            all_syms = self._binance.get_usdt_perpetual_symbols()
        except Exception as exc:
            logger.error("Failed to fetch symbol list — skipping cycle: %s", exc)
            return
        if not all_syms:
            logger.warning("Symbol list is empty — skipping cycle")
            return

        try:
            self._mark_prices = self._binance.get_mark_prices()
        except Exception as exc:
            logger.warning("Mark-price fetch failed: %s", exc)
            self._mark_prices = {}

        try:
            self._tickers = self._binance.get_24h_tickers()
        except Exception as exc:
            logger.warning("24h ticker fetch failed: %s", exc)
            self._tickers = {}

        already_tracked: set = set()
        if self._tracker:
            try:
                already_tracked = self._tracker.get_tracked_symbols()
            except Exception:
                pass

        targets = [
            s for s in all_syms
            if s["symbol"] not in self.excluded
            and s["symbol"] not in already_tracked
            and not self._cooldown.is_on_cooldown(s["symbol"])
        ]

        logger.info(
            "Targets: %d / %d  (%d excluded, %d tracked, %d on cooldown)",
            len(targets), len(all_syms),
            len(self.excluded), len(already_tracked), self._cooldown.active_count,
        )

        alerts = 0
        for idx, sym in enumerate(targets, 1):
            if not self._running:
                return
            logger.info("Scanning [%d/%d] %s", idx, len(targets), sym["symbol"])
            try:
                data = self._analyse(sym)
                if data:
                    if self._tg.send_alert(data):
                        alerts += 1
                    time.sleep(0.3)
            except Exception:
                logger.error("Error analysing %s", sym["symbol"], exc_info=True)

        self._cooldown.prune()
        if alerts:
            logger.info("Alerts sent this cycle: %d", alerts)

    def _analyse(self, sym: dict) -> Optional[dict]:
        symbol = sym["symbol"]

        if self._cooldown.is_on_cooldown(symbol):
            return None

        candles = self._binance.get_closed_klines(
            symbol, self.timeframe, self._candles_needed,
        )
        if len(candles) < self._candles_needed:
            return None

        last = candles[-1]
        ticker = self._tickers.get(symbol, {})

        # ── FILTER 1: 24h high breakout ──────────────────────────────
        lookback_candles = candles[-(self.brk_lookback + 1):-1]
        if len(lookback_candles) < self.brk_lookback:
            return None

        high_24h = max(c["high"] for c in lookback_candles)
        if last["close"] <= high_24h:
            logger.info("%s  rejected — close %.8f did not break 24h high %.8f",
                        symbol, last["close"], high_24h)
            return None

        brk_margin_pct = ((last["close"] - high_24h) / high_24h) * 100
        logger.info("%s  ✅ Breakout +%.2f%% above 24h high %.8f",
                    symbol, brk_margin_pct, high_24h)

        # ── FILTER 2: consecutive volume increase (last 3 candles) ───
        consec = candles[-self.consec_vol_candles:]
        if len(consec) < self.consec_vol_candles:
            return None

        is_increasing = all(
            consec[i]["quote_volume"] > consec[i - 1]["quote_volume"]
            for i in range(1, len(consec))
        )
        if not is_increasing:
            vol_vals = [self._fmt_vol_usd(c["quote_volume"]) for c in consec]
            logger.info("%s  rejected — volume NOT consecutively increasing: %s",
                        symbol, " → ".join(vol_vals))
            return None

        first_vol = consec[0]["quote_volume"]
        last_vol = consec[-1]["quote_volume"]
        vol_ratio = last_vol / first_vol if first_vol > 0 else 0
        if vol_ratio < self.vol_ratio_min:
            vol_vals = [self._fmt_vol_usd(c["quote_volume"]) for c in consec]
            logger.info("%s  rejected — volume ratio %.2fx < min %.2fx: %s",
                        symbol, vol_ratio, self.vol_ratio_min, " → ".join(vol_vals))
            return None

        vol_vals = [self._fmt_vol_usd(c["quote_volume"]) for c in consec]
        base_vol_vals = [self._fmt_vol_count(c["volume"]) for c in consec]
        logger.info("%s  ✅ Consecutive volume: %s (ratio %.2fx)", symbol, " → ".join(vol_vals), vol_ratio)

        # ── FILTER 3: 24h price change cap ───────────────────────────
        price_chg_24h = ticker.get("price_change_pct", 0)
        if abs(price_chg_24h) > self.max_price_chg_24h:
            logger.info("%s  rejected — 24h price change %.1f%% > max %.1f%%",
                        symbol, price_chg_24h, self.max_price_chg_24h)
            return None

        logger.info("%s  ✅ 24h price change: %.1f%%", symbol, price_chg_24h)

        # ── optional min volume floor ─────────────────────────────────
        current_vol = last["quote_volume"]
        if self.min_vol_usdt > 0 and current_vol < self.min_vol_usdt:
            logger.info("%s  rejected — volume %s < min %s",
                        symbol, self._fmt_vol_usd(current_vol),
                        self._fmt_vol_usd(self.min_vol_usdt))
            return None

        # ── ALL MAIN CRITERIA PASSED — collect additional data ───────
        additional = self._collect_additional(symbol, candles, last, ticker)

        # ── HARD FILTERS (all must pass) ──────────────────────────────
        hard_result = self._apply_hard_filters(
            symbol, vol_ratio, additional,
        )
        if hard_result is not None:
            return None

        # ── SOFT FLAGS + QUALITY SCORE ────────────────────────────────
        soft_flags, soft_details = self._count_soft_flags(
            brk_margin_pct, price_chg_24h, vol_ratio, additional,
        )
        if soft_flags >= self.sf_max_flags:
            logger.info(
                "%s  rejected — %d soft flags (max %d): %s",
                symbol, soft_flags, self.sf_max_flags,
                ", ".join(soft_details),
            )
            return None

        quality_score, quality_details = self._calc_quality_score(
            vol_ratio, price_chg_24h, brk_margin_pct, additional,
        )

        candle_colors = []
        for c in consec:
            candle_colors.append("green" if c["close"] >= c["open"] else "red")

        self._cooldown.record(symbol)

        price = self._mark_prices.get(symbol)
        btc_price = self._mark_prices.get("BTCUSDT")
        candle_dt = datetime.fromtimestamp(last["open_time"] / 1000, tz=timezone.utc)
        now_dt = datetime.now(timezone.utc)

        vol_baseline = candles[-(20 + 1):-1]
        avg_baseline = sum(c["quote_volume"] for c in vol_baseline) / len(vol_baseline) if vol_baseline else 0
        rvol = current_vol / avg_baseline if avg_baseline > 0 else 0

        high_breakout_warning = brk_margin_pct > self.high_brk_warn_pct

        alert = {
            "symbol":            symbol,
            "timeframe":         self.timeframe,
            "price":             f"{price:.8f}" if price else "N/A",
            "price_change_24h":  price_chg_24h,
            "breakout_margin_pct": brk_margin_pct,
            "high_breakout_warning": high_breakout_warning,
            "high_24h":          high_24h,
            "vol_candle_1":      consec[0]["quote_volume"],
            "vol_candle_2":      consec[1]["quote_volume"],
            "vol_candle_3":      consec[2]["quote_volume"],
            "vol_candle_1_fmt":  vol_vals[0],
            "vol_candle_2_fmt":  vol_vals[1],
            "vol_candle_3_fmt":  vol_vals[2],
            "vol_candle_1_base":     consec[0]["volume"],
            "vol_candle_2_base":     consec[1]["volume"],
            "vol_candle_3_base":     consec[2]["volume"],
            "vol_candle_1_base_fmt": base_vol_vals[0],
            "vol_candle_2_base_fmt": base_vol_vals[1],
            "vol_candle_3_base_fmt": base_vol_vals[2],
            "vol_ratio":         round(vol_ratio, 2),
            "candle_colors":     candle_colors,
            "rvol":              rvol,
            "btc_price":         btc_price,
            "candle_time":       candle_dt.strftime("%Y-%m-%d %H:%M UTC"),
            "alert_time":        now_dt.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "alert_time_ts":     now_dt.timestamp(),
            "cooldown_hours":    self.cooldown_hours,
            "soft_flags":        soft_flags,
            "soft_flag_details": soft_details,
            "quality_score":     quality_score,
            "quality_details":   quality_details,
            "additional_data":   additional,
        }

        if self._tracker:
            self._tracker.record_signal(alert)

        logger.info(
            "🚨 SIGNAL  %s  brk:+%.2f%%  vols:%s→%s→%s  24h:%.1f%%  flags:%d  score:%d/8",
            symbol, brk_margin_pct,
            vol_vals[0], vol_vals[1], vol_vals[2], price_chg_24h,
            soft_flags, quality_score,
        )
        return alert

    # ── hard / soft / quality helpers ─────────────────────────────────

    def _apply_hard_filters(
        self, symbol: str, vol_ratio: float, add: dict,
    ) -> Optional[str]:
        funding = add.get("funding_rate")
        vol_24h = add.get("vol_24h_usdt")
        mcap = add.get("market_cap_usd")

        if vol_ratio > self.hf_vol_ratio_max:
            logger.info("%s  HARD rejected — vol_ratio %.2f > max %.2f",
                        symbol, vol_ratio, self.hf_vol_ratio_max)
            return "vol_ratio"

        if funding is not None and funding <= self.hf_funding_rate_min:
            logger.info("%s  HARD rejected — funding_rate %.4f ≤ min %.4f",
                        symbol, funding, self.hf_funding_rate_min)
            return "funding_rate"

        if vol_24h is not None and vol_24h <= self.hf_vol_24h_usdt_min:
            logger.info("%s  HARD rejected — vol_24h_usdt %s ≤ min %s",
                        symbol, self._fmt_vol_usd(vol_24h),
                        self._fmt_vol_usd(self.hf_vol_24h_usdt_min))
            return "vol_24h_usdt"

        if mcap is not None and mcap >= self.hf_market_cap_max:
            logger.info("%s  HARD rejected — market_cap %s ≥ max %s",
                        symbol, self._fmt_vol_usd(mcap),
                        self._fmt_vol_usd(self.hf_market_cap_max))
            return "market_cap_usd"

        return None

    def _count_soft_flags(
        self,
        brk_margin_pct: float,
        price_chg_24h: float,
        vol_ratio: float,
        add: dict,
    ) -> tuple:
        flags = []

        rvol = add.get("rvol_20")
        if rvol is not None and rvol < self.sf_rvol_min:
            flags.append(f"low_rvol {rvol:.1f}x<{self.sf_rvol_min}x")

        mcap = add.get("market_cap_usd")
        if mcap is not None and mcap > self.sf_mcap_max:
            flags.append(f"large_mcap {self._fmt_vol_usd(mcap)}>{self._fmt_vol_usd(self.sf_mcap_max)}")

        oi_ratio = add.get("oi_growth_ratio")
        if oi_ratio is not None and oi_ratio > self.sf_oi_ratio_max:
            flags.append(f"extreme_oi {oi_ratio:.1f}>{self.sf_oi_ratio_max}")

        funding = add.get("funding_rate")
        if funding is not None and funding < self.sf_funding_rate_min:
            flags.append(f"neg_funding {funding:.4f}<{self.sf_funding_rate_min}")

        vol_24h = add.get("vol_24h_usdt")
        if vol_24h is not None and vol_24h < self.sf_vol_24h_min:
            flags.append(f"low_vol {self._fmt_vol_usd(vol_24h)}<{self._fmt_vol_usd(self.sf_vol_24h_min)}")

        if abs(price_chg_24h) > self.sf_price_chg_max:
            flags.append(f"extreme_chg {price_chg_24h:.1f}%>±{self.sf_price_chg_max}%")

        ema_dist = add.get("ema50_distance_pct")
        if ema_dist is not None and ema_dist > self.sf_ema50_dist_max:
            flags.append(f"far_ema {ema_dist:.1f}%>{self.sf_ema50_dist_max}%")

        return len(flags), flags

    def _calc_quality_score(
        self,
        vol_ratio: float,
        price_chg_24h: float,
        brk_margin_pct: float,
        add: dict,
    ) -> tuple:
        points = []

        rvol = add.get("rvol_20")
        if rvol is not None and self.qs_rvol_sweet_min <= rvol <= self.qs_rvol_sweet_max:
            points.append("rvol_sweet")

        if rvol is not None and rvol >= self.qs_rvol_adequate_min:
            points.append("rvol_ok")

        mcap = add.get("market_cap_usd")
        if mcap is not None and self.qs_mcap_min <= mcap <= self.qs_mcap_max:
            points.append("small_mcap")

        oi_ratio = add.get("oi_growth_ratio")
        if oi_ratio is not None and self.qs_oi_ratio_min <= oi_ratio <= self.qs_oi_ratio_max:
            points.append("oi_moderate")

        funding = add.get("funding_rate")
        if funding is not None and funding >= self.qs_funding_min:
            points.append("funding_ok")

        vol_24h = add.get("vol_24h_usdt")
        if vol_24h is not None and vol_24h >= self.qs_vol_24h_min:
            points.append("vol_24h_ok")

        if self.qs_brk_margin_min <= brk_margin_pct <= self.qs_brk_margin_max:
            points.append("brk_conviction")

        if self.qs_price_chg_min <= price_chg_24h <= self.qs_price_chg_max:
            points.append("momentum_ok")

        return len(points), points

    def _collect_additional(
        self, symbol: str, candles: list, last: dict, ticker: dict
    ) -> dict:
        """
        Collect additional context data. Each piece is wrapped in try/except
        so any failure does NOT block the signal from firing.
        """
        data: dict = {}

        # RVOL vs 20-candle baseline
        try:
            baseline = candles[-(20 + 1):-1]
            if baseline:
                avg_b = sum(c["quote_volume"] for c in baseline) / len(baseline)
                data["rvol_20"] = round(last["quote_volume"] / avg_b, 2) if avg_b > 0 else None
                data["vol_baseline_avg"] = round(avg_b, 2)
        except Exception:
            pass

        # OI: current vs average of last 24 periods
        try:
            oi_hist = self._binance.get_oi_history(symbol, "1h", 25)
            if len(oi_hist) >= 2:
                current_oi = oi_hist[-1]["oi_value_usdt"]
                prev_oi_values = [h["oi_value_usdt"] for h in oi_hist[:-1]]
                avg_oi = sum(prev_oi_values) / len(prev_oi_values)
                data["oi_current_usdt"] = round(current_oi, 2)
                data["oi_avg_24h_usdt"] = round(avg_oi, 2)
                data["oi_change_pct"] = round(((current_oi - avg_oi) / avg_oi) * 100, 2) if avg_oi > 0 else None
                # relative OI growth ratio
                if len(oi_hist) >= 3:
                    oi_changes = [
                        oi_hist[i]["oi_value_usdt"] - oi_hist[i - 1]["oi_value_usdt"]
                        for i in range(1, len(oi_hist))
                    ]
                    current_oi_growth = oi_changes[-1]
                    avg_oi_growth = sum(oi_changes[:-1]) / len(oi_changes[:-1]) if oi_changes[:-1] else 0
                    data["oi_growth_current"] = round(current_oi_growth, 2)
                    data["oi_growth_avg"] = round(avg_oi_growth, 2)
                    if avg_oi_growth != 0:
                        data["oi_growth_ratio"] = round(current_oi_growth / abs(avg_oi_growth), 2)
        except Exception:
            pass

        # Funding rate
        try:
            fr = self._binance.get_funding_rate(symbol)
            if fr is not None:
                data["funding_rate"] = round(fr * 100, 4)
                data["funding_in_ideal_range"] = -0.02 <= fr * 100 <= 0.15
        except Exception:
            pass

        # 24h volume liquidity
        try:
            vol_24h = ticker.get("quote_volume_24h", 0)
            data["vol_24h_usdt"] = round(vol_24h, 2)
            data["vol_24h_above_50m"] = vol_24h >= 50_000_000
            vol_24h_base = ticker.get("volume_24h", 0)
            data["vol_24h_base"] = round(vol_24h_base, 2)
        except Exception:
            pass

        # 4h EMA50
        try:
            candles_4h = self._binance.get_closed_klines(symbol, "4h", 55)
            if len(candles_4h) >= 50:
                closes_4h = [c["close"] for c in candles_4h]
                ema50 = self._ema(closes_4h, 50)
                current_price = last["close"]
                data["ema50_4h"] = round(ema50, 8)
                data["price_above_ema50_4h"] = current_price > ema50
                data["ema50_distance_pct"] = round(((current_price - ema50) / ema50) * 100, 2) if ema50 > 0 else None
        except Exception:
            pass

        # Volatility compression (range of last 10 candles vs prior 10)
        try:
            if len(candles) >= 20:
                recent_10 = candles[-10:]
                prior_10 = candles[-20:-10]

                def avg_range(cs):
                    return sum((c["high"] - c["low"]) / c["close"] * 100 for c in cs if c["close"] > 0) / len(cs)

                recent_range_pct = avg_range(recent_10)
                prior_range_pct = avg_range(prior_10)
                data["volatility_recent_10_pct"] = round(recent_range_pct, 4)
                data["volatility_prior_10_pct"] = round(prior_range_pct, 4)
                if prior_range_pct > 0:
                    compression_ratio = recent_range_pct / prior_range_pct
                    data["volatility_compression_ratio"] = round(compression_ratio, 3)
                    data["is_compressed"] = compression_ratio < 0.7
        except Exception:
            pass

        # Market cap from CoinGecko (optional)
        try:
            if self._market_cap is not None:
                base = symbol.replace("USDT", "").replace("BUSD", "")
                mcap = self._market_cap.get(base)
                data["market_cap_usd"] = mcap
                data["market_cap_fmt"] = self._market_cap.format(base)
        except Exception:
            pass

        return data

    def _send_startup(self) -> None:
        lines = [
            "⚙️ <b>Scanner Started</b>",
            "",
            "<b>Main Criteria (all must pass):</b>",
            f"1️⃣ 1h close above last {self.brk_lookback}h high",
            f"2️⃣ Last {self.consec_vol_candles} candles increasing volume",
            f"3️⃣ 24h change ≤ ±{self.max_price_chg_24h}%",
            "",
            "<b>Hard Filters (all must pass):</b>",
            f"4️⃣ vol_ratio ≤ {self.hf_vol_ratio_max}",
            f"5️⃣ funding &gt; {self.hf_funding_rate_min}",
            f"6️⃣ 24h vol &gt; {self._fmt_vol_usd(self.hf_vol_24h_usdt_min)}",
            f"7️⃣ mcap &lt; {self._fmt_vol_usd(self.hf_market_cap_max)}",
            "",
            f"<b>Soft Flags ({self.sf_max_flags}+ = block):</b>",
            f"brk_margin &gt; {self.sf_brk_margin_max}% | 24h_chg &gt; {self.sf_price_chg_max}%",
            f"ema50_dist &gt; {self.sf_ema50_dist_max}% | vol_ratio &gt; {self.sf_vol_ratio_max}",
            f"vol_24h &lt; {self._fmt_vol_usd(self.sf_vol_24h_min)} | oi_chg &gt; {self.sf_oi_change_max}%",
            f"funding &lt; {self.sf_funding_rate_min}",
            "",
            "<b>Quality Score:</b> 0–8 points",
            "",
            f"⏱ Scan every {self.interval}s  |  Cooldown {self.cooldown_hours}h",
        ]
        if self.min_vol_usdt > 0:
            lines.append(f"🔻 Min volume: {self._fmt_vol_usd(self.min_vol_usdt)}")
        self._tg.send_startup("\n".join(lines))
