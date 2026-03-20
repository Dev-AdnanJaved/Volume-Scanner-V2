"""
Market-cap provider backed by the CoinGecko *free* API.

• Fetches in bulk (top 3000 coins), caches for a configurable TTL.
• Normalises Binance "1000PEPE" style symbols automatically.
• Respects free-tier rate limits (10-30 calls/min without key).
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)


class MarketCapProvider:
    """Fetch + cache market-cap data from CoinGecko."""

    URL = "https://api.coingecko.com/api/v3/coins/markets"

    def __init__(self, cache_minutes: int = 120, include_unknown: bool = True):
        self._cache: Dict[str, float] = {}
        self._cache_ts: float = 0.0
        self._cache_ttl = cache_minutes * 60
        self._include_unknown = include_unknown
        self._session = requests.Session()
        self._session.headers["User-Agent"] = "BinanceFuturesScanner/1.0"
        self._session.headers["Accept"] = "application/json"

    # ── symbol normalisation ─────────────────────────────────────────

    @staticmethod
    def _normalise(symbol: str) -> str:
        """Strip Binance multiplier prefixes (1000PEPE → PEPE)."""
        upper = symbol.upper()
        for prefix in ("10000", "1000"):
            if upper.startswith(prefix) and len(upper) > len(prefix):
                return upper[len(prefix):]
        return upper

    # ── bulk fetch ───────────────────────────────────────────────────

    def refresh(self) -> None:
        logger.info("Refreshing market-cap cache from CoinGecko …")
        caps: Dict[str, float] = {}
        page = 1

        # Top 3000 coins covers virtually ALL Binance futures pairs
        # 12 pages × 250 = 3000 coins
        # With 12s delay = ~132 seconds total — very safe for free tier
        max_pages = 12
        page_delay = 20.0
        errors = 0
        max_errors = 3
        consecutive_429 = 0

        while page <= max_pages and errors < max_errors:
            try:
                logger.debug("CoinGecko: fetching page %d/%d …", page, max_pages)
                resp = self._session.get(
                    self.URL,
                    params={
                        "vs_currency": "usd",
                        "order": "market_cap_desc",
                        "per_page": 250,
                        "page": page,
                        "sparkline": "false",
                    },
                    timeout=30,
                )

                if resp.status_code == 429:
                    consecutive_429 += 1
                    if consecutive_429 >= 3:
                        logger.warning(
                            "CoinGecko: 3 consecutive 429s — stopping at page %d "
                            "(got %d coins so far, enough to proceed)",
                            page, len(caps),
                        )
                        break
                    wait = int(resp.headers.get("Retry-After", 90))
                    wait = wait + (consecutive_429 * 30)
                    logger.warning(
                        "CoinGecko 429 on page %d — waiting %ds (attempt %d/3)",
                        page, wait, consecutive_429,
                    )
                    time.sleep(wait)
                    continue

                if resp.status_code == 403:
                    logger.error(
                        "CoinGecko 403 Forbidden — stopping at page %d "
                        "(got %d coins)", page, len(caps),
                    )
                    break

                resp.raise_for_status()
                rows = resp.json()

                if not rows:
                    logger.debug("CoinGecko: page %d empty — done.", page)
                    break

                consecutive_429 = 0

                for coin in rows:
                    sym = coin.get("symbol", "").upper()
                    mcap = coin.get("market_cap")
                    if sym and mcap is not None:
                        caps.setdefault(sym, float(mcap))

                logger.debug(
                    "CoinGecko: page %d OK — %d coins cached so far",
                    page, len(caps),
                )
                errors = 0
                page += 1

                if page <= max_pages:
                    time.sleep(page_delay)

            except requests.exceptions.Timeout:
                logger.warning("CoinGecko timeout on page %d", page)
                errors += 1
                time.sleep(10)
            except requests.exceptions.ConnectionError as exc:
                logger.warning("CoinGecko connection error: %s", exc)
                errors += 1
                time.sleep(10)
            except requests.RequestException as exc:
                logger.error("CoinGecko error (page %d): %s", page, exc)
                errors += 1
                time.sleep(10)

        if caps:
            self._cache = caps
            self._cache_ts = time.time()
            logger.info(
                "Market-cap cache updated: %d coins loaded (%d pages in ~%ds)",
                len(caps), page - 1, int((page - 1) * page_delay),
            )
        elif self._cache:
            logger.warning(
                "CoinGecko refresh failed — keeping previous cache "
                "(%d coins, age %.0f min)",
                len(self._cache),
                (time.time() - self._cache_ts) / 60,
            )
        else:
            logger.error(
                "CoinGecko refresh failed and no previous cache — "
                "market cap filter will treat all coins as unknown"
            )

    # ── public API ───────────────────────────────────────────────────

    def _ensure_fresh(self) -> None:
        if not self._cache or time.time() - self._cache_ts > self._cache_ttl:
            self.refresh()

    def get(self, base_asset: str) -> Optional[float]:
        self._ensure_fresh()
        normed = self._normalise(base_asset)
        return self._cache.get(normed)

    def passes_filter(self, base_asset: str, max_mcap: float) -> bool:
        """True when market cap ≤ threshold (unknowns governed by flag)."""
        mcap = self.get(base_asset)
        if mcap is None:
            decision = self._include_unknown
            logger.debug(
                "No mcap for %s — %s", base_asset,
                "including" if decision else "excluding",
            )
            return decision
        return mcap <= max_mcap

    def format(self, base_asset: str) -> str:
        mcap = self.get(base_asset)
        if mcap is None:
            return "Unknown"
        if mcap >= 1e9:
            return f"${mcap / 1e9:.2f}B"
        if mcap >= 1e6:
            return f"${mcap / 1e6:.2f}M"
        if mcap >= 1e3:
            return f"${mcap / 1e3:.2f}K"
        return f"${mcap:.2f}"