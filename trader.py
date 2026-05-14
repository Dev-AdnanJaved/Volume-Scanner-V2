"""
AutoTrader — executes USDT-M futures long trades on monster signals.

Trade flow:
  1. Monster signal fires → submit(signal) called from scanner
  2. Set leverage, fetch USDT balance, calculate quantities
  3. Market LONG entry
  4. Simultaneously place:
     - TP1 LIMIT at +tp1_pct%  (tp1_qty_pct% of total position)
     - TP2 LIMIT at +tp2_pct%  (tp2_qty_pct% of total position)
     - SL STOP_MARKET at -sl_pct% (full position)
  5. Watch loop (every watch_interval_seconds):
     - TP1 fills → cancel SL → place breakeven SL on remaining qty
     - TP2 fills → cancel SL → trade fully closed
     - SL fills → cancel TP1 + TP2 → trade closed
  6. Telegram notification on every event

Key Binance quirks handled (from production):
  - STOP_MARKET orders go into the Conditional/Algo bucket (returns algoId not orderId)
  - futures_get_order returns -2013 for algo orders → fall back to algo/futures/openOrders
  - futures_cancel_order fails on algo orders → fall back to DELETE algo/futures/order
  - Never retry SL placement blindly — each retry silently creates a real duplicate SL
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
import uuid
from typing import Dict, Optional

from binance import AsyncClient

logger = logging.getLogger(__name__)


# ── Order helpers ─────────────────────────────────────────────────────────────

def _order_id(o: dict):
    return o.get("orderId") or o.get("algoId")


def _order_status_str(o: dict) -> str:
    return (o.get("status") or o.get("algoStatus") or "").upper()


def _is_filled(status_dict) -> bool:
    if not status_dict or isinstance(status_dict, Exception):
        return False
    return _order_status_str(status_dict) == "FILLED"


# ── TP placement (regular LIMIT → normal endpoints) ───────────────────────────

async def _place_tp(
    client: AsyncClient,
    symbol: str,
    qty: float,
    tp_price: float,
    is_hedge: bool,
    attempts: int = 3,
) -> int | None:
    delay = 0.3
    for i in range(1, attempts + 1):
        try:
            kw = {"positionSide": "LONG"} if is_hedge else {"reduceOnly": True}
            res = await client.futures_create_order(
                symbol=symbol,
                side="SELL",
                type="LIMIT",
                price=str(tp_price),
                quantity=qty,
                timeInForce="GTC",
                **kw,
            )
            oid = _order_id(res)
            if oid:
                logger.info("%s TP placed id=%s @ %s", symbol, oid, tp_price)
                return oid
        except Exception as e:
            logger.warning("%s TP attempt %d failed: %s", symbol, i, e)
        if i < attempts:
            await asyncio.sleep(delay)
            delay = min(delay * 2, 2.0)
    return None


# ── SL placement (STOP_MARKET → Conditional/Algo bucket) ─────────────────────

async def _place_sl(
    client: AsyncClient,
    symbol: str,
    qty: float,
    sl_price: float,
    is_hedge: bool,
) -> int | str | None:
    """
    Places SL exactly once. Returns algoId on success, cid sentinel, or None.
    Only retries on hard network exceptions — never on 'no id in response',
    because each silent retry creates a real duplicate SL on Binance.
    """
    cid = f"BOTSL{uuid.uuid4().hex[:10]}{int(time.time()) % 10000}"[:30]
    kw = (
        {"positionSide": "LONG", "quantity": qty}
        if is_hedge
        else {"reduceOnly": True, "quantity": qty}
    )
    params = dict(
        symbol=symbol,
        side="SELL",
        type="STOP_MARKET",
        stopPrice=str(sl_price),
        workingType="MARK_PRICE",
        newClientOrderId=cid,
        **kw,
    )
    for i in range(1, 3):
        try:
            res = await client.futures_create_order(**params)
            oid = _order_id(res)
            if oid is None:
                logger.info("%s SL placed (no id — trusting Binance) cid=%s", symbol, cid)
                return cid
            logger.info("%s SL placed id=%s trigger=%s", symbol, oid, sl_price)
            return oid
        except Exception as e:
            msg = str(e)
            logger.warning("%s SL attempt %d exception: %s", symbol, i, e)
            if "-4130" in msg or "is existing" in msg:
                logger.info("%s SL already exists on Binance (-4130) — trusting it", symbol)
                return cid
            if i < 2:
                await asyncio.sleep(0.4)
    return None


# ── Cancel (handles both regular and algo/conditional orders) ─────────────────

async def _cancel_order(
    client: AsyncClient, symbol: str, order_id: int | str | None
) -> None:
    if order_id is None:
        return
    try:
        if isinstance(order_id, int) or (isinstance(order_id, str) and order_id.isdigit()):
            await client.futures_cancel_order(symbol=symbol, orderId=int(order_id))
        else:
            await client.futures_cancel_order(symbol=symbol, origClientOrderId=order_id)
        return
    except Exception as e:
        msg = str(e)
        if "Unknown order" in msg or "-2011" in msg:
            return
        try:
            data = {"symbol": symbol}
            if isinstance(order_id, int) or (isinstance(order_id, str) and order_id.isdigit()):
                data["algoId"] = int(order_id)
            else:
                data["clientAlgoId"] = order_id
            await client._request_futures_api("delete", "algo/futures/order", True, data=data)
            logger.info("%s cancelled via algo endpoint id=%s", symbol, order_id)
        except Exception as e2:
            msg2 = str(e2)
            if "Unknown order" not in msg2 and "-2011" not in msg2:
                logger.warning("%s cancel failed: %s / algo: %s", symbol, e, e2)


# ── Status check (handles both regular and algo/conditional orders) ───────────

async def _get_order_status(
    client: AsyncClient, symbol: str, order_id: int | str | None
) -> dict | None:
    if order_id is None:
        return None
    try:
        if isinstance(order_id, int) or (isinstance(order_id, str) and order_id.isdigit()):
            return await client.futures_get_order(symbol=symbol, orderId=int(order_id))
        return await client.futures_get_order(symbol=symbol, origClientOrderId=order_id)
    except Exception as e:
        if "-2013" not in str(e):
            return None
    try:
        res = await client._request_futures_api(
            "get", "algo/futures/openOrders", True, data={"symbol": symbol}
        )
        orders = res.get("orders") if isinstance(res, dict) else (res or [])
        for o in orders or []:
            if (
                str(o.get("algoId")) == str(order_id)
                or o.get("clientAlgoId") == str(order_id)
            ):
                st = o.get("algoStatus") or o.get("status")
                return {**o, "status": st}
    except Exception:
        pass
    return None


# ── Rounding helpers ──────────────────────────────────────────────────────────

def _round_step(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step) * step


def _round_price(price: float, tick: float, precision: int) -> float:
    if tick <= 0:
        return round(price, precision)
    return round(math.floor(price / tick) * tick, precision)


def _ema_calc(values: list, period: int) -> float:
    if len(values) < period:
        return 0.0
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


async def _get_position_size(
    client: AsyncClient, symbol: str, hedge_mode: bool
) -> float | None:
    """Return current LONG position size, or None if the API call fails."""
    try:
        positions = await client.futures_position_information(symbol=symbol)
        for p in positions:
            if hedge_mode:
                if p.get("positionSide") == "LONG":
                    return float(p.get("positionAmt", 0))
            else:
                return abs(float(p.get("positionAmt", 0)))
    except Exception as e:
        logger.debug("_get_position_size failed for %s: %s", symbol, e)
    return None


# ── AutoTrader ────────────────────────────────────────────────────────────────

class AutoTrader:
    """
    Runs an asyncio event loop in a background daemon thread.
    Scanner calls submit(signal) thread-safely via call_soon_threadsafe.
    """

    def __init__(self, config: dict, notifier) -> None:
        at = config.get("auto_trade", {})
        mn = config.get("monster", {})

        self._enabled: bool         = at.get("enabled", False)
        self._leverage: int         = at.get("leverage", 20)
        self._leverage_steps: list  = at.get("leverage_fallback_steps", [self._leverage])
        self._leverage_margin_map: dict = {
            int(k): float(v)
            for k, v in at.get("leverage_margin_map", {}).items()
        }
        self._margin_pct: float     = at.get("margin_pct", 2.0)
        self._sl_pct: float         = at.get("sl_pct", 10.0)
        self._move_sl_to_be: bool   = at.get("move_sl_to_breakeven_after_tp", True)
        self._hedge_mode: bool      = at.get("hedge_mode", False)
        self._watch_interval: int   = at.get("watch_interval_seconds", 30)
        self._max_open: int         = at.get("max_open_trades", 5)

        # Score tiers — sorted descending so first match wins
        # Each tier: {label, margin_multiplier, tp1_pct, tp2_pct}
        default_tiers = {
            "6": {"label": "max_size",  "margin_multiplier": 1.0,  "tp1_pct": 50.0, "tp2_pct": 100.0},
            "4": {"label": "full_size", "margin_multiplier": 0.75, "tp1_pct": 20.0, "tp2_pct": 30.0},
            "3": {"label": "half_size", "margin_multiplier": 0.5,  "tp1_pct": 10.0, "tp2_pct": 10.0},
        }
        raw_tiers = mn.get("score_tiers", default_tiers)
        self._score_tiers: list = sorted(
            [(int(k), v) for k, v in raw_tiers.items()], reverse=True
        )

        # TP5 snapshot thresholds
        self._tp5_hold_hours_max:    float = mn.get("tp5_hold_hours_max",    20.0)
        self._tp5_hold_ema_min:      float = mn.get("tp5_ema_dist_min",      15.0)
        self._tp5_hold_momentum_min: float = mn.get("tp5_momentum_min",       4.0)
        self._tp5_hold_oi_min:       float = mn.get("tp5_oi_change_min",     12.0)
        self._tp5_exit_hours_min:    float = mn.get("tp5_exit_hours_min",    40.0)
        self._tp5_exit_momentum_max: float = mn.get("tp5_exit_momentum_max",  2.0)
        self._tp5_exit_funding_max:  float = mn.get("tp5_exit_funding_max",  -0.01)

        # TP10 snapshot thresholds
        self._tp10_hold_hours_max:    float = mn.get("tp10_hold_hours_max",    30.0)
        self._tp10_hold_ema_min:      float = mn.get("tp10_hold_ema_dist_min", 20.0)
        self._tp10_hold_oi_min:       float = mn.get("tp10_hold_oi_min",       18.0)
        self._tp10_hold_mcap_max:     float = mn.get("tp10_hold_mcap_max",     100_000_000)
        self._tp10_exit_hours_min:    float = mn.get("tp10_exit_hours_min",    50.0)
        self._tp10_exit_mcap_max:     float = mn.get("tp10_exit_mcap_max",     150_000_000)
        self._tp10_exit_momentum_max: float = mn.get("tp10_exit_momentum_max",  2.0)

        self._api_key:    str = config.get("binance", {}).get("api_key", "")
        self._api_secret: str = config.get("binance", {}).get("api_secret", "")
        self._tg = notifier

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._queue: Optional[asyncio.Queue] = None
        self._thread: Optional[threading.Thread] = None
        self._open_trades: Dict[str, dict] = {}
        self._sym_info_cache: Dict[str, dict] = {}
        self._running = False

    # ── lifecycle ─────────────────────────────────────────────────────

    def start(self) -> None:
        if not self._enabled:
            logger.info("AutoTrader disabled in config — not starting")
            return
        if not self._api_key or not self._api_secret:
            logger.warning("AutoTrader: Binance API credentials missing — will not start")
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop, name="autotrader", daemon=True
        )
        self._thread.start()
        tier_summary = [(ms, t.get("label")) for ms, t in self._score_tiers]
        logger.info(
            "AutoTrader started  leverage=%dx  base_margin=%.1f%%  SL=%.1f%%  tiers=%s",
            self._leverage, self._margin_pct, self._sl_pct, tier_summary,
        )

    def stop(self) -> None:
        self._running = False
        if self._loop and self._queue:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)

    def submit(self, signal: dict) -> None:
        """Thread-safe: enqueue a monster signal for auto-trading."""
        if not self._enabled or not self._running:
            return
        sym = signal.get("symbol", "")
        if sym in self._open_trades:
            logger.info("AutoTrader: %s already open — skipping duplicate signal", sym)
            return
        if len(self._open_trades) >= self._max_open:
            logger.warning(
                "AutoTrader: max_open_trades (%d) reached — skipping %s",
                self._max_open, sym,
            )
            return
        if self._loop and self._queue:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, signal)
            logger.info("AutoTrader: queued %s for entry", sym)

    # ── background event loop ─────────────────────────────────────────

    def _run_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._main())
        except Exception:
            logger.error("AutoTrader event loop crashed", exc_info=True)
        finally:
            self._loop.close()

    async def _main(self) -> None:
        self._queue = asyncio.Queue()
        client = await AsyncClient.create(self._api_key, self._api_secret)
        logger.info("AutoTrader: Binance AsyncClient connected")

        # Auto-detect account position mode — overrides any config value
        try:
            mode_info = await client.futures_get_position_mode()
            self._hedge_mode = bool(mode_info.get("dualSidePosition", False))
            logger.info(
                "AutoTrader: position mode = %s",
                "Hedge (dualSidePosition)" if self._hedge_mode else "One-way",
            )
        except Exception as e:
            logger.warning(
                "AutoTrader: could not detect position mode — using config value "
                "(hedge_mode=%s): %s",
                self._hedge_mode, e,
            )

        try:
            while self._running:
                try:
                    signal = await asyncio.wait_for(self._queue.get(), timeout=2.0)
                except asyncio.TimeoutError:
                    continue
                if signal is None:
                    break
                asyncio.create_task(self._open_trade(client, signal))
        finally:
            await client.close_connection()
            logger.info("AutoTrader: Binance AsyncClient disconnected")

    # ── Telegram helper (non-blocking) ────────────────────────────────

    async def _notify(self, text: str) -> None:
        loop = asyncio.get_event_loop()
        try:
            await loop.run_in_executor(None, self._tg.send, text)
        except Exception as e:
            logger.warning("AutoTrader notify failed: %s", e)

    # ── Symbol info cache ─────────────────────────────────────────────

    async def _get_sym_info(self, client: AsyncClient, symbol: str) -> dict:
        if symbol in self._sym_info_cache:
            return self._sym_info_cache[symbol]
        info = await client.futures_exchange_info()
        for s in info.get("symbols", []):
            if s["symbol"] == symbol:
                qty_step = 0.001
                price_tick = 0.0001
                qty_prec = s.get("quantityPrecision", 3)
                price_prec = s.get("pricePrecision", 4)
                for f in s.get("filters", []):
                    if f["filterType"] == "LOT_SIZE":
                        qty_step = float(f["stepSize"])
                    elif f["filterType"] == "PRICE_FILTER":
                        price_tick = float(f["tickSize"])
                result = {
                    "qty_step": qty_step,
                    "price_tick": price_tick,
                    "qty_prec": qty_prec,
                    "price_prec": price_prec,
                }
                self._sym_info_cache[symbol] = result
                return result
        return {"qty_step": 0.001, "price_tick": 0.0001, "qty_prec": 3, "price_prec": 4}

    # ── Tier lookup ───────────────────────────────────────────────────

    def _get_tier(self, score: int) -> Optional[dict]:
        """Return the tier config matching this monster score, or None if below all tiers."""
        for min_score, tier in self._score_tiers:
            if score >= min_score:
                return tier
        return None

    # ── Live snapshot fetch ───────────────────────────────────────────

    async def _fetch_snapshot(
        self, client: AsyncClient, symbol: str, signal: dict
    ) -> dict:
        """Fetch fresh market data for mid-signal TP decision checks."""
        snap: dict = {}
        ad = signal.get("additional_data", {})

        # EMA50 distance from current price (4h)
        try:
            klines = await client.futures_klines(symbol=symbol, interval="4h", limit=55)
            if len(klines) >= 50:
                closes = [float(c[4]) for c in klines]
                ema50 = _ema_calc(closes, 50)
                if ema50 > 0:
                    snap["ema50_distance_pct"] = round(
                        (closes[-1] - ema50) / ema50 * 100, 2
                    )
        except Exception as e:
            logger.debug("%s snapshot EMA50 failed: %s", symbol, e)

        # 4h price momentum (last completed 4h candle vs one before)
        try:
            km = await client.futures_klines(symbol=symbol, interval="4h", limit=3)
            if len(km) >= 2:
                p_now  = float(km[-1][4])
                p_prev = float(km[-2][4])
                if p_prev > 0:
                    snap["price_momentum_4h_pct"] = round(
                        (p_now - p_prev) / p_prev * 100, 2
                    )
        except Exception as e:
            logger.debug("%s snapshot momentum failed: %s", symbol, e)

        # OI change % vs 24h average (fresh fetch)
        try:
            oi_hist = await client.futures_open_interest_hist(
                symbol=symbol, period="1h", limit=25
            )
            if len(oi_hist) >= 2:
                cur_oi    = float(oi_hist[-1]["sumOpenInterestValue"])
                prev_vals = [float(h["sumOpenInterestValue"]) for h in oi_hist[:-1]]
                avg_oi    = sum(prev_vals) / len(prev_vals)
                if avg_oi > 0:
                    snap["oi_change_pct"] = round(
                        (cur_oi - avg_oi) / avg_oi * 100, 2
                    )
        except Exception as e:
            snap["oi_change_pct"] = ad.get("oi_change_pct") or 0
            logger.debug("%s snapshot OI failed (using entry value): %s", symbol, e)

        # Funding rate (fresh)
        try:
            fr_data = await client.futures_funding_rate(symbol=symbol, limit=1)
            if fr_data:
                snap["funding_rate"] = round(float(fr_data[-1]["fundingRate"]) * 100, 4)
        except Exception as e:
            snap["funding_rate"] = ad.get("funding_rate") or 0
            logger.debug("%s snapshot funding failed (using entry value): %s", symbol, e)

        # Market cap from original signal (doesn't meaningfully change intraday)
        snap["market_cap_usd"] = ad.get("market_cap_usd") or 0

        return snap

    # ── TP snapshot decision ──────────────────────────────────────────

    async def _check_tp_snapshot(
        self, client: AsyncClient, symbol: str, level: int
    ) -> None:
        """Fetch snapshot at TP5 or TP10 price level, decide hold/exit, adjust TP if needed."""
        trade = self._open_trades.get(symbol)
        if not trade or trade.get("status") != "open":
            return

        signal = trade.get("signal", {})
        snap   = await self._fetch_snapshot(client, symbol, signal)
        hours  = (time.time() - trade["opened_at"]) / 3600

        ema_dist  = snap.get("ema50_distance_pct",   0)
        momentum  = snap.get("price_momentum_4h_pct", 0)
        oi_change = snap.get("oi_change_pct",          0)
        funding   = snap.get("funding_rate",            0)
        mcap      = snap.get("market_cap_usd",          0)
        mcap_m    = mcap / 1_000_000

        logger.info(
            "AutoTrader: %s TP%d snapshot  hours=%.1f  ema_dist=%.1f%%  "
            "momentum=%.2f%%  oi_chg=%.1f%%  funding=%.4f  mcap=$%.0fM",
            symbol, level, hours, ema_dist, momentum, oi_change, funding, mcap_m,
        )

        if level == 5:
            hold_all = (
                hours < self._tp5_hold_hours_max
                and ema_dist > self._tp5_hold_ema_min
                and momentum > self._tp5_hold_momentum_min
                and oi_change > self._tp5_hold_oi_min
            )
            exit_any = (
                hours > self._tp5_exit_hours_min
                or momentum < self._tp5_exit_momentum_max
                or funding < self._tp5_exit_funding_max
            )

            if hold_all:
                decision = "HOLD"
                reason = (
                    f"hours {hours:.1f}<{self._tp5_hold_hours_max}  "
                    f"ema {ema_dist:.1f}%>{self._tp5_hold_ema_min}%  "
                    f"mom {momentum:.2f}%>{self._tp5_hold_momentum_min}%  "
                    f"oi {oi_change:.1f}%>{self._tp5_hold_oi_min}%"
                )
            elif exit_any:
                decision = "EXIT_AT_TP10"
                parts = []
                if hours > self._tp5_exit_hours_min:
                    parts.append(f"slow {hours:.0f}h>{self._tp5_exit_hours_min}h")
                if momentum < self._tp5_exit_momentum_max:
                    parts.append(f"weak_mom {momentum:.2f}%<{self._tp5_exit_momentum_max}%")
                if funding < self._tp5_exit_funding_max:
                    parts.append(f"neg_funding {funding:.4f}<{self._tp5_exit_funding_max}")
                reason = "  ".join(parts)
            else:
                decision = "NEUTRAL"
                reason   = "no strong signal either way — keeping TPs"

            await self._notify(
                f"📊 <b>TP5 SNAPSHOT</b> — {symbol}\n"
                f"{'━' * 26}\n"
                f"⏱ {hours:.1f}h  📐 EMA+{ema_dist:.1f}%  📈 4h {momentum:+.2f}%\n"
                f"📊 OI {oi_change:+.1f}%  💰 FR {funding:+.4f}%  💎 ${mcap_m:.0f}M\n\n"
                f"Decision: <b>{decision}</b>\n"
                f"<i>{reason}</i>"
            )

            if decision == "EXIT_AT_TP10":
                tr2       = self._open_trades.get(symbol, {})
                tps_now   = tr2.get("tps", [])
                p_tick    = tr2.get("price_tick", 0.0001)
                p_prec    = tr2.get("price_prec", 4)
                for i, tp in enumerate(tps_now):
                    if not tp.get("filled", False) and tp.get("pct", 0) > 10.0:
                        new_price = _round_price(tr2["entry"] * 1.10, p_tick, p_prec)
                        old_pct   = tp["pct"]
                        await _cancel_order(client, symbol, tp.get("order_id"))
                        new_id = await _place_tp(
                            client, symbol, tp["qty"], new_price, self._hedge_mode
                        )
                        self._open_trades[symbol]["tps"][i]["order_id"] = new_id
                        self._open_trades[symbol]["tps"][i]["price"]    = new_price
                        self._open_trades[symbol]["tps"][i]["pct"]      = 10.0
                        logger.info(
                            "AutoTrader: %s TP%d adjusted to +10%% @ %.4f (was +%.0f%%)",
                            symbol, i + 1, new_price, old_pct,
                        )
                        break

        elif level == 10:
            hold_all = (
                hours < self._tp10_hold_hours_max
                and ema_dist > self._tp10_hold_ema_min
                and oi_change > self._tp10_hold_oi_min
                and mcap < self._tp10_hold_mcap_max
            )
            exit_any = (
                hours > self._tp10_exit_hours_min
                or mcap > self._tp10_exit_mcap_max
                or momentum < self._tp10_exit_momentum_max
            )

            if hold_all:
                decision = "HOLD"
                reason = (
                    f"hours {hours:.1f}<{self._tp10_hold_hours_max}  "
                    f"ema {ema_dist:.1f}%>{self._tp10_hold_ema_min}%  "
                    f"oi {oi_change:.1f}%>{self._tp10_hold_oi_min}%  "
                    f"mcap ${mcap_m:.0f}M<${self._tp10_hold_mcap_max/1e6:.0f}M"
                )
            elif exit_any:
                decision = "EXIT_AT_TP25"
                parts = []
                if hours > self._tp10_exit_hours_min:
                    parts.append(f"slow {hours:.0f}h>{self._tp10_exit_hours_min}h")
                if mcap > self._tp10_exit_mcap_max:
                    parts.append(f"large_cap ${mcap_m:.0f}M>${self._tp10_exit_mcap_max/1e6:.0f}M")
                if momentum < self._tp10_exit_momentum_max:
                    parts.append(f"weak_mom {momentum:.2f}%<{self._tp10_exit_momentum_max}%")
                reason = "  ".join(parts)
            else:
                decision = "NEUTRAL"
                reason   = "no strong signal either way — keeping TPs"

            await self._notify(
                f"📊 <b>TP10 SNAPSHOT</b> — {symbol}\n"
                f"{'━' * 26}\n"
                f"⏱ {hours:.1f}h  📐 EMA+{ema_dist:.1f}%  📈 4h {momentum:+.2f}%\n"
                f"📊 OI {oi_change:+.1f}%  💎 ${mcap_m:.0f}M\n\n"
                f"Decision: <b>{decision}</b>\n"
                f"<i>{reason}</i>"
            )

            if decision == "EXIT_AT_TP25":
                tr2       = self._open_trades.get(symbol, {})
                tps_now   = tr2.get("tps", [])
                p_tick    = tr2.get("price_tick", 0.0001)
                p_prec    = tr2.get("price_prec", 4)
                for i, tp in enumerate(tps_now):
                    if not tp.get("filled", False) and tp.get("pct", 0) > 25.0:
                        new_price = _round_price(tr2["entry"] * 1.25, p_tick, p_prec)
                        old_pct   = tp["pct"]
                        await _cancel_order(client, symbol, tp.get("order_id"))
                        new_id = await _place_tp(
                            client, symbol, tp["qty"], new_price, self._hedge_mode
                        )
                        self._open_trades[symbol]["tps"][i]["order_id"] = new_id
                        self._open_trades[symbol]["tps"][i]["price"]    = new_price
                        self._open_trades[symbol]["tps"][i]["pct"]      = 25.0
                        logger.info(
                            "AutoTrader: %s TP%d adjusted to +25%% @ %.4f (was +%.0f%%)",
                            symbol, i + 1, new_price, old_pct,
                        )
                        break

    # ── Open trade ────────────────────────────────────────────────────

    async def _open_trade(self, client: AsyncClient, signal: dict) -> None:
        symbol        = signal.get("symbol", "")
        monster_score = signal.get("monster_score", 0)
        self._open_trades[symbol] = {"status": "opening"}

        try:
            # Tier lookup
            tier = self._get_tier(monster_score)
            if tier is None:
                logger.info("AutoTrader: %s score=%d below all tiers — skipping",
                            symbol, monster_score)
                self._open_trades.pop(symbol, None)
                return

            tier_label        = tier.get("label", "unknown")
            margin_multiplier = float(tier.get("margin_multiplier", 1.0))
            tps_cfg = tier.get("take_profits", [
                {"pct": 50.0,  "qty_pct": 50.0,  "move_sl_to_breakeven": True},
                {"pct": 100.0, "qty_pct": 50.0,  "move_sl_to_breakeven": False},
            ])
            if not tps_cfg:
                logger.error("AutoTrader: %s tier %s has no take_profits — skipping",
                             symbol, tier_label)
                self._open_trades.pop(symbol, None)
                return

            sym_info   = await self._get_sym_info(client, symbol)
            qty_step   = sym_info["qty_step"]
            price_tick = sym_info["price_tick"]
            price_prec = sym_info["price_prec"]

            # Leverage fallback
            actual_leverage: int | None = None
            for lev in self._leverage_steps:
                try:
                    await client.futures_change_leverage(symbol=symbol, leverage=lev)
                    actual_leverage = lev
                    logger.info("AutoTrader: %s leverage = %dx", symbol, lev)
                    break
                except Exception as e:
                    logger.warning("AutoTrader: %s leverage %dx rejected — trying next: %s",
                                   symbol, lev, e)

            if actual_leverage is None:
                logger.error("AutoTrader: %s — all leverage steps failed, aborting", symbol)
                await self._notify(f"❌ <b>AutoTrader skipped</b> {symbol}\n"
                                   f"Could not set any leverage from {self._leverage_steps}")
                self._open_trades.pop(symbol, None)
                return

            base_margin   = self._leverage_margin_map.get(actual_leverage, self._margin_pct)
            actual_margin = base_margin * margin_multiplier
            logger.info("AutoTrader: %s tier=%s  score=%d  leverage=%dx  margin=%.2f%%",
                        symbol, tier_label, monster_score, actual_leverage, actual_margin)

            # Balance
            balance_info = await client.futures_account_balance()
            usdt_balance = 0.0
            for b in balance_info:
                if b.get("asset") == "USDT":
                    usdt_balance = float(b.get("availableBalance", 0))
                    break

            if usdt_balance < 10:
                logger.warning("AutoTrader: insufficient USDT (%.2f) — skipping %s",
                               usdt_balance, symbol)
                await self._notify(f"⚠️ <b>AutoTrader skipped</b> {symbol}\n"
                                   f"Insufficient USDT balance: ${usdt_balance:.2f}")
                self._open_trades.pop(symbol, None)
                return

            # Mark price
            mark        = await client.futures_mark_price(symbol=symbol)
            entry_price = float(mark.get("markPrice", 0))
            if entry_price <= 0:
                logger.error("AutoTrader: %s invalid mark price — aborting", symbol)
                self._open_trades.pop(symbol, None)
                return

            notional  = usdt_balance * (actual_margin / 100.0)
            raw_total = notional * actual_leverage / entry_price

            def rp(price: float) -> float:
                return _round_price(price, price_tick, price_prec)

            sl_price = rp(entry_price * (1 - self._sl_pct / 100.0))

            # Build TP list from config
            tps: list[dict] = []
            for tp_cfg in tps_cfg:
                qty = _round_step(raw_total * (float(tp_cfg["qty_pct"]) / 100.0), qty_step)
                tps.append({
                    "pct":                float(tp_cfg["pct"]),
                    "qty_pct":            float(tp_cfg["qty_pct"]),
                    "qty":                qty,
                    "price":              rp(entry_price * (1 + float(tp_cfg["pct"]) / 100.0)),
                    "move_sl_to_breakeven": bool(tp_cfg.get("move_sl_to_breakeven", False)),
                    "order_id":           None,
                    "filled":             False,
                })

            if any(tp["qty"] <= 0 for tp in tps):
                logger.error("AutoTrader: %s qty too small for one or more TPs — aborting",
                             symbol)
                self._open_trades.pop(symbol, None)
                return

            entry_qty = sum(tp["qty"] for tp in tps)

            # Market LONG entry
            kw_entry  = {"positionSide": "LONG"} if self._hedge_mode else {}
            entry_res = await client.futures_create_order(
                symbol=symbol, side="BUY", type="MARKET",
                quantity=entry_qty, **kw_entry,
            )
            actual_entry = float(entry_res.get("avgPrice") or entry_res.get("price") or 0)
            if actual_entry > 0:
                entry_price = actual_entry
                sl_price    = rp(entry_price * (1 - self._sl_pct / 100.0))
                for tp in tps:
                    tp["price"] = rp(entry_price * (1 + tp["pct"] / 100.0))

            logger.info("AutoTrader: %s MARKET LONG filled  qty=%.6f  @~%.4f",
                        symbol, entry_qty, entry_price)

            # Place all TPs + SL in one parallel gather
            results = await asyncio.gather(
                *[_place_tp(client, symbol, tp["qty"], tp["price"], self._hedge_mode)
                  for tp in tps],
                _place_sl(client, symbol, entry_qty, sl_price, self._hedge_mode),
            )
            for i, tp in enumerate(tps):
                tp["order_id"] = results[i]
            sl_id = results[-1]

            self._open_trades[symbol] = {
                "status":        "open",
                "entry":         entry_price,
                "qty_total":     entry_qty,
                "sl_price":      sl_price,
                "sl_id":         sl_id,
                "tps":           tps,
                "monster_score": monster_score,
                "tier_label":    tier_label,
                "opened_at":     time.time(),
                "price_tick":    price_tick,
                "price_prec":    price_prec,
                "qty_step":      qty_step,
                "leverage":      actual_leverage,
                "margin_pct":    actual_margin,
                "signal":        signal,
            }

            tp_lines = "\n".join(
                f"🎯 TP{i+1}:    ${tp['price']:.4f}"
                f"  (+{tp['pct']:.0f}% · {tp['qty_pct']:.0f}% pos)"
                f"  [BE-SL={'✓' if tp['move_sl_to_breakeven'] else '✗'}]"
                for i, tp in enumerate(tps)
            )
            tp_ids = "  ".join(f"TP{i+1}={tp['order_id']}" for i, tp in enumerate(tps))
            await self._notify(
                f"🤖 <b>AUTO TRADE OPENED</b>\n"
                f"{'━' * 26}\n"
                f"📌 <b>{symbol}</b>   🔥 {monster_score}/7  [{tier_label}]\n"
                f"\n"
                f"💰 Entry:   ${entry_price:.4f}\n"
                f"📊 Qty:     {entry_qty}  ({actual_leverage}x leverage)\n"
                f"💵 Margin:  ${notional:.2f}  ({actual_margin:.2f}%)\n"
                f"\n"
                f"{tp_lines}\n"
                f"🛑 SL:     ${sl_price:.4f}  (-{self._sl_pct}%)\n"
                f"\n"
                f"📋 {tp_ids}  SL={sl_id}"
            )

            asyncio.create_task(self._watch_trade(client, symbol))

        except Exception:
            logger.error("AutoTrader: failed to open trade for %s", symbol, exc_info=True)
            self._open_trades.pop(symbol, None)
            await self._notify(
                f"❌ <b>AutoTrader error</b>\n"
                f"Failed to open trade for {symbol} — check server logs"
            )

    # ── Watch trade ───────────────────────────────────────────────────

    async def _watch_trade(self, client: AsyncClient, symbol: str) -> None:
        logger.info("AutoTrader: watching %s", symbol)
        tp_index     = 0
        tp5_checked  = False
        tp10_checked = False

        while self._running and symbol in self._open_trades:
            trade = self._open_trades.get(symbol)
            if not trade or trade.get("status") != "open":
                self._open_trades.pop(symbol, None)
                logger.warning("AutoTrader: %s unexpected status — slot freed", symbol)
                break

            tps = trade.get("tps", [])
            if tp_index >= len(tps):
                self._open_trades.pop(symbol, None)
                logger.info("AutoTrader: %s all TPs consumed — slot freed", symbol)
                break

            current_tp = tps[tp_index]
            total_qty  = sum(t["qty"] for t in tps)

            try:
                # ── External-closure guard ────────────────────────────────
                pos_size = await _get_position_size(client, symbol, self._hedge_mode)
                if pos_size is not None and pos_size == 0:
                    await asyncio.gather(
                        *[_cancel_order(client, symbol, t.get("order_id"))
                          for t in tps[tp_index:]],
                        _cancel_order(client, symbol, trade.get("sl_id")),
                        return_exceptions=True,
                    )
                    await self._notify(
                        f"🔴 <b>EXTERNAL CLOSE</b> — {symbol}\n"
                        f"Position closed externally (size = 0).\n"
                        f"All pending orders cancelled. Trade slot freed."
                    )
                    self._open_trades.pop(symbol, None)
                    logger.info("AutoTrader: %s externally closed — slot freed", symbol)
                    return

                # ── TP snapshot price checks (+5% / +10% from entry) ──────
                if not (tp5_checked and tp10_checked):
                    try:
                        mk = await client.futures_mark_price(symbol=symbol)
                        cur_price = float(mk.get("markPrice", 0))
                    except Exception:
                        cur_price = 0.0

                    if cur_price > 0:
                        entry = trade["entry"]
                        if not tp5_checked and cur_price >= entry * 1.05:
                            tp5_checked = True
                            asyncio.create_task(
                                self._check_tp_snapshot(client, symbol, 5)
                            )
                        if not tp10_checked and cur_price >= entry * 1.10:
                            tp10_checked = True
                            asyncio.create_task(
                                self._check_tp_snapshot(client, symbol, 10)
                            )

                # ── Poll current TP and SL status in parallel ─────────────
                tp_status, sl_status = await asyncio.gather(
                    _get_order_status(client, symbol, current_tp.get("order_id")),
                    _get_order_status(client, symbol, trade.get("sl_id")),
                    return_exceptions=True,
                )

                # ── SL filled ─────────────────────────────────────────────
                if _is_filled(sl_status):
                    await asyncio.gather(
                        *[_cancel_order(client, symbol, t.get("order_id"))
                          for t in tps[tp_index:]],
                        return_exceptions=True,
                    )
                    trade_lev     = trade.get("leverage", self._leverage)
                    sl_pct_actual = (trade["sl_price"] - trade["entry"]) / trade["entry"] * 100
                    lev_result    = sl_pct_actual * trade_lev

                    if tp_index == 0:
                        msg = (
                            f"🛑 <b>SL HIT</b> — {symbol}\n"
                            f"Entry ${trade['entry']:.4f} → SL ${trade['sl_price']:.4f}\n"
                            f"Loss: {sl_pct_actual:.1f}%  (×{trade_lev} = {lev_result:.1f}%)"
                        )
                    else:
                        locked_pnl = sum(
                            t["pct"] * (t["qty"] / total_qty)
                            for t in tps[:tp_index]
                        )
                        be_close = abs(trade["sl_price"] - trade["entry"]) / trade["entry"] < 0.002
                        sl_label = "BREAKEVEN SL" if be_close else "SL"
                        msg = (
                            f"🔄 <b>{sl_label} HIT</b> — {symbol}\n"
                            f"{'━' * 26}\n"
                            f"{tp_index} TP(s) previously filled ✅\n"
                            f"Locked P&L: +{locked_pnl:.2f}% (position-weighted)\n"
                            f"Remaining qty closed at ${trade['sl_price']:.4f}"
                        )

                    await self._notify(msg)
                    self._open_trades.pop(symbol, None)
                    return

                # ── Current TP filled ──────────────────────────────────────
                if _is_filled(tp_status):
                    tps[tp_index]["filled"] = True
                    tp_pct    = current_tp["pct"]
                    trade_lev = trade.get("leverage", self._leverage)
                    lev_gain  = tp_pct * trade_lev
                    qty_frac  = current_tp["qty"] / total_qty * 100
                    tp_num    = tp_index + 1
                    tp_index += 1

                    if tp_index >= len(tps):
                        # ── All TPs filled — full exit ────────────────────
                        await _cancel_order(client, symbol, trade.get("sl_id"))

                        summary = "\n".join(
                            f"TP{i+1}: +{t['pct']:.0f}%  on {t['qty']/total_qty*100:.0f}%  ✅"
                            for i, t in enumerate(tps)
                        )
                        duration_h = (time.time() - trade["opened_at"]) / 3600
                        await self._notify(
                            f"🚀 <b>FULL EXIT</b> — {symbol}\n"
                            f"{'━' * 26}\n"
                            f"{summary}\n\n"
                            f"🔥 Monster {trade['monster_score']}/7"
                            f"  [{trade.get('tier_label', '')}]\n"
                            f"⏱ Duration: {duration_h:.1f}h"
                        )
                        self._open_trades.pop(symbol, None)
                        return

                    # ── Partial TP filled — more TPs remain ───────────────
                    next_tp = tps[tp_index]

                    # Move SL to breakeven if configured for this TP
                    sl_moved  = False
                    be_price  = None
                    if current_tp.get("move_sl_to_breakeven", False) or self._move_sl_to_be:
                        remaining_qty = sum(t["qty"] for t in tps[tp_index:])
                        price_tick    = trade.get("price_tick", 0.0001)
                        price_prec    = trade.get("price_prec", 4)
                        be_price      = _round_price(trade["entry"], price_tick, price_prec)
                        await _cancel_order(client, symbol, trade.get("sl_id"))
                        new_sl_id = await _place_sl(
                            client, symbol, remaining_qty, be_price, self._hedge_mode
                        )
                        self._open_trades[symbol]["sl_id"]   = new_sl_id
                        self._open_trades[symbol]["sl_price"] = be_price
                        sl_moved = True

                    msg = (
                        f"✅ <b>TP{tp_num} HIT</b> — {symbol}\n"
                        f"{'━' * 26}\n"
                        f"Closed {qty_frac:.0f}% @ ${current_tp['price']:.4f}"
                        f"  (+{tp_pct:.0f}% · ×{trade_lev} = +{lev_gain:.1f}%)\n"
                    )
                    if sl_moved and be_price is not None:
                        msg += f"🔄 SL → breakeven (${be_price:.4f})\n"
                    remaining_tps = len(tps) - tp_index
                    msg += (
                        f"🎯 Next: TP{tp_index+1} @ ${next_tp['price']:.4f}"
                        f"  (+{next_tp['pct']:.0f}%)"
                        f"  [{remaining_tps} TP(s) remaining]"
                    )
                    await self._notify(msg)

            except Exception:
                logger.error("AutoTrader: watch error for %s", symbol, exc_info=True)

            await asyncio.sleep(self._watch_interval)

        logger.info("AutoTrader: watch loop ended for %s", symbol)
