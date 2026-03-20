"""
Binance USDT-M Futures REST API client.

• Automatic rate-limit tracking (weight-aware).
• Transparent retry with back-off on 429 / 5xx / timeouts.
• Separates closed candles from the still-open candle.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from threading import Lock
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_KLINE_FIELDS = (
    "open_time", "open", "high", "low", "close",
    "volume", "close_time", "quote_volume",
    "trades", "taker_buy_base_vol", "taker_buy_quote_vol", "ignore",
)


class BinanceClient:
    """Thin wrapper around the Binance Futures (fapi) REST API."""

    BASE = "https://fapi.binance.com"
    MAX_WEIGHT_PER_MIN = 2400
    SAFE_WEIGHT_CEILING = 2000

    def __init__(
        self,
        api_key: str = "",
        api_secret: str = "",
        delay_ms: int = 100,
    ):
        self._delay = delay_ms / 1000.0
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "BinanceFuturesScanner/1.0"
        if api_key:
            self._session.headers["X-MBX-APIKEY"] = api_key

        self._weights: deque[tuple[float, int]] = deque()
        self._lock = Lock()

        self._symbols: Optional[List[Dict]] = None
        self._symbols_ts: float = 0.0

    def _consume_weight(self, weight: int = 1) -> None:
        with self._lock:
            now = time.time()
            while self._weights and now - self._weights[0][0] > 60:
                self._weights.popleft()
            used = sum(w for _, w in self._weights)
            if used + weight > self.SAFE_WEIGHT_CEILING:
                oldest = self._weights[0][0] if self._weights else now
                sleep = 60.0 - (now - oldest) + 0.5
                if sleep > 0:
                    logger.warning("Rate-limit headroom low — sleeping %.1fs", sleep)
                    time.sleep(sleep)
            self._weights.append((time.time(), weight))

    def _get(
        self,
        path: str,
        params: Optional[dict] = None,
        weight: int = 1,
        retries: int = 3,
    ) -> Any:
        url = f"{self.BASE}{path}"
        for attempt in range(1, retries + 1):
            self._consume_weight(weight)
            time.sleep(self._delay)
            try:
                resp = self._session.get(url, params=params, timeout=30)
                if resp.status_code == 451:
                    logger.error(
                        "HTTP 451 — Binance is blocking this server's IP (geo/legal restriction). "
                        "Sleeping 300s before retry."
                    )
                    time.sleep(300)
                    continue
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 60))
                    logger.warning("429 from Binance — backing off %ds", wait)
                    time.sleep(wait)
                    continue
                if resp.status_code == 418:
                    logger.error("IP auto-banned — sleeping 120s")
                    time.sleep(120)
                    continue
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.Timeout:
                logger.warning("Timeout %s (attempt %d/%d)", path, attempt, retries)
            except requests.exceptions.ConnectionError as exc:
                logger.warning("Conn error %s: %s (attempt %d/%d)", path, exc, attempt, retries)
            except requests.exceptions.HTTPError:
                if resp.status_code >= 500:
                    logger.warning("Server error %d (attempt %d/%d)", resp.status_code, attempt, retries)
                else:
                    raise
            if attempt < retries:
                time.sleep(2 ** attempt)
        raise RuntimeError(f"Failed {path} after {retries} attempts")

    def get_usdt_perpetual_symbols(self, ttl: float = 300) -> List[Dict]:
        """Return list of active USDT perpetual pairs (cached)."""
        now = time.time()
        if self._symbols and now - self._symbols_ts < ttl:
            return self._symbols

        info = self._get("/fapi/v1/exchangeInfo", weight=1)
        result = []
        for s in info["symbols"]:
            if (
                s.get("quoteAsset") == "USDT"
                and s.get("contractType") == "PERPETUAL"
                and s.get("status") == "TRADING"
            ):
                result.append(
                    {
                        "symbol": s["symbol"],
                        "base_asset": s["baseAsset"],
                    }
                )
        self._symbols = result
        self._symbols_ts = now
        logger.info("Loaded %d USDT perpetual symbols from exchange info", len(result))
        return result

    def get_mark_prices(self) -> Dict[str, float]:
        """All mark prices in one call (weight 1)."""
        data = self._get("/fapi/v1/premiumIndex", weight=1)
        return {
            d["symbol"]: float(d["markPrice"])
            for d in data
            if float(d["markPrice"]) > 0
        }

    def get_closed_klines(self, symbol: str, interval: str, count: int) -> List[Dict]:
        """
        Return exactly *count* **closed** candles (newest last).
        """
        raw = self._get(
            "/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": count + 2},
            weight=1 if count + 2 <= 100 else 2,
        )
        now_ms = int(time.time() * 1000)
        closed: list[dict] = []
        for row in raw:
            if int(row[6]) > now_ms:
                continue
            closed.append(
                {
                    "open_time":     int(row[0]),
                    "open":          float(row[1]),
                    "high":          float(row[2]),
                    "low":           float(row[3]),
                    "close":         float(row[4]),
                    "volume":        float(row[5]),
                    "close_time":    int(row[6]),
                    "quote_volume":  float(row[7]),
                    "trades":        int(row[8]),
                }
            )
        return closed[-count:]

    def get_24h_tickers(self) -> Dict[str, dict]:
        """Fetch 24h ticker stats for all USDT futures symbols in one call."""
        data = self._get("/fapi/v1/ticker/24hr", weight=40)
        result = {}
        for d in data:
            result[d["symbol"]] = {
                "price_change_pct": float(d.get("priceChangePercent", 0)),
                "quote_volume_24h": float(d.get("quoteVolume", 0)),
                "volume_24h":       float(d.get("volume", 0)),
                "high_price":       float(d.get("highPrice", 0)),
            }
        return result

    def get_oi_history(self, symbol: str, period: str, limit: int) -> List[Dict]:
        """
        Historical open interest (from /futures/data/ endpoint).
        Returns [] on failure so callers can degrade gracefully.
        """
        try:
            raw = self._get(
                "/futures/data/openInterestHist",
                params={"symbol": symbol, "period": period, "limit": limit},
                weight=1,
            )
            return [
                {
                    "timestamp":     int(e["timestamp"]),
                    "oi":            float(e["sumOpenInterest"]),
                    "oi_value_usdt": float(e["sumOpenInterestValue"]),
                }
                for e in raw
            ]
        except Exception as exc:
            logger.warning("OI history unavailable for %s: %s", symbol, exc)
            return []

    def get_funding_rate(self, symbol: str) -> Optional[float]:
        """
        Fetch current funding rate for a symbol.
        Returns None on failure.
        """
        try:
            data = self._get(
                "/fapi/v1/premiumIndex",
                params={"symbol": symbol},
                weight=1,
            )
            if isinstance(data, list):
                data = data[0]
            return float(data["lastFundingRate"])
        except Exception as exc:
            logger.debug("Funding rate unavailable for %s: %s", symbol, exc)
            return None
