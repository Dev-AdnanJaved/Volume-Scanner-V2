"""
Telegram Bot API helper.

Sends:
  - Breakout alerts (signal entry)
  - Take-profit target hit alerts
  - Reversal warning alerts
  - Startup summary
"""

from __future__ import annotations

import logging
import time

import requests

logger = logging.getLogger(__name__)


class TelegramNotifier:
    API = "https://api.telegram.org/bot{token}/{method}"

    def __init__(self, bot_token: str, chat_id: str):
        self._token = bot_token
        self._chat_id = chat_id
        self._session = requests.Session()
        self._ok = False

    def _url(self, method: str) -> str:
        return self.API.format(token=self._token, method=method)

    def validate(self) -> bool:
        try:
            r = self._session.get(self._url("getMe"), timeout=10).json()
            if r.get("ok"):
                logger.info("Telegram bot validated: @%s", r["result"].get("username"))
                self._ok = True
                return True
            logger.error("Telegram validation failed: %s", r)
        except Exception as exc:
            logger.error("Telegram validation error: %s", exc)
        return False

    def send(self, text: str, parse_mode: str = "HTML") -> bool:
        for attempt in range(3):
            try:
                r = self._session.post(
                    self._url("sendMessage"),
                    json={
                        "chat_id": self._chat_id,
                        "text": text,
                        "parse_mode": parse_mode,
                        "disable_web_page_preview": True,
                    },
                    timeout=15,
                ).json()
                if r.get("ok"):
                    return True
                if r.get("error_code") == 429:
                    wait = r.get("parameters", {}).get("retry_after", 30)
                    logger.warning("Telegram 429 — waiting %ds", wait)
                    time.sleep(wait)
                    continue
                logger.error("Telegram error: %s", r)
                return False
            except Exception as exc:
                logger.error("Telegram send failed (attempt %d): %s", attempt + 1, exc)
                time.sleep(2)
        return False

    def send_document(self, file_path: str, caption: str = "") -> bool:
        """Send a file as a Telegram document."""
        for attempt in range(3):
            try:
                with open(file_path, "rb") as f:
                    r = self._session.post(
                        self._url("sendDocument"),
                        data={"chat_id": self._chat_id, "caption": caption},
                        files={"document": f},
                        timeout=30,
                    ).json()
                if r.get("ok"):
                    return True
                if r.get("error_code") == 429:
                    wait = r.get("parameters", {}).get("retry_after", 30)
                    time.sleep(wait)
                    continue
                logger.error("Telegram send_document error: %s", r)
                return False
            except Exception as exc:
                logger.error("Telegram send_document failed (attempt %d): %s", attempt + 1, exc)
                time.sleep(2)
        return False

    # ── alert types ──────────────────────────────────────────────────

    def send_alert(self, data: dict) -> bool:
        return self.send(self._fmt_alert(data))

    def send_startup(self, summary: str) -> bool:
        return self.send(
            f"🤖 <b>Volume Scanner Started</b>\n\n{summary}\n\nScanner is now running …"
        )

    def send_take_profit(self, data: dict) -> bool:
        return self.send(self._fmt_take_profit(data))

    def send_reversal_warning(self, data: dict) -> bool:
        return self.send(self._fmt_reversal(data))

    # ── price formatting ─────────────────────────────────────────────

    @staticmethod
    def _fp(price: float) -> str:
        if price <= 0:
            return "N/A"
        if price >= 1000:
            return f"${price:,.2f}"
        if price >= 1:
            return f"${price:.4f}"
        if price >= 0.001:
            return f"${price:.6f}"
        return f"${price:.8f}"

    # ── signal alert format ──────────────────────────────────────────

    @staticmethod
    def _fmt_alert(d: dict) -> str:
        symbol = d["symbol"]
        tf = d.get("timeframe", "1h")
        price = d.get("price", "N/A")
        brk_margin = d.get("breakout_margin_pct", 0)
        price_chg = d.get("price_change_24h", 0)
        v1 = d.get("vol_candle_1_fmt", "?")
        v2 = d.get("vol_candle_2_fmt", "?")
        v3 = d.get("vol_candle_3_fmt", "?")
        bv1 = d.get("vol_candle_1_base_fmt", "?")
        bv2 = d.get("vol_candle_2_base_fmt", "?")
        bv3 = d.get("vol_candle_3_base_fmt", "?")
        rvol = d.get("rvol", 0)
        alert_time = d.get("alert_time", "N/A")
        cooldown = d.get("cooldown_hours", 12)

        chg_icon = "🟢" if price_chg >= 0 else "🔴"
        high_brk = d.get("high_breakout_warning", False)

        header = "⚠️ <b>BREAKOUT SIGNAL — HIGH BREAKOUT</b>" if high_brk else "🚨 <b>BREAKOUT SIGNAL</b>"

        base_coin = symbol.replace("USDT", "").replace("BUSD", "")

        lines = [
            header,
            f"{'━' * 28}",
            "",
            f"📌 <b>{symbol}</b>  |  {tf}",
            f"💵 <b>Price:</b>  ${price}",
            "",
            f"1️⃣ <b>Breakout:</b>  +{brk_margin:.2f}% above 24h high",
            f"2️⃣ <b>Vol USDT:</b>  {v1} → {v2} → {v3}  ({rvol:.1f}x avg)",
            f"    <b>Vol {base_coin}:</b>  {bv1} → {bv2} → {bv3}",
            f"3️⃣ <b>24h Change:</b>  {chg_icon} {price_chg:+.1f}%",
            "",
        ]
        if high_brk:
            lines.append(f"⚠️ <b>Warning:</b> Breakout margin {brk_margin:.2f}% > 5% — enter with caution")
            lines.append("")

        q_score = d.get("quality_score", "?")
        s_flags = d.get("soft_flags", 0)
        sf_details = d.get("soft_flag_details", [])
        q_details = d.get("quality_details", [])

        if q_score >= 7:
            grade = "🟢 EXCELLENT"
        elif q_score >= 5:
            grade = "🟢 STRONG"
        elif q_score >= 4:
            grade = "🟡 GOOD"
        elif q_score >= 2:
            grade = "🟠 FAIR"
        else:
            grade = "🔴 WEAK"

        lines.append(f"⭐ <b>Quality:</b>  {q_score}/8  {grade}")
        if s_flags > 0:
            lines.append(f"🚩 <b>Warnings:</b>  {s_flags}/7  ({', '.join(sf_details)})")
        else:
            lines.append(f"🚩 <b>Warnings:</b>  0/7")
        lines.append("")

        lines.extend([
            f"🕐 <b>Time:</b>  {alert_time}",
            f"⏱ <b>Cooldown:</b>  {cooldown}h",
        ])
        return "\n".join(lines)

    # ── take-profit alert format ─────────────────────────────────────

    def _fmt_take_profit(self, d: dict) -> str:
        target = d["target"]
        if target >= 75:
            icon = "💎🚀🚀"
        elif target >= 50:
            icon = "🚀🚀🚀"
        elif target >= 30:
            icon = "🚀🚀"
        elif target >= 10:
            icon = "🚀"
        elif target >= 5:
            icon = "🎯"
        else:
            icon = "✅"

        cur_pct = d.get("cur_pct", 0)
        high_pct = d.get("high_pct", 0)
        age = d.get("age_str", "")

        return (
            f"{icon} <b>TARGET HIT  +{target}%</b>\n"
            f"{'━' * 28}\n\n"
            f"📌 <b>{d['symbol']}</b>\n"
            f"💵 Entry:    {self._fp(d['entry_price'])}\n"
            f"🏔  Peak:     {self._fp(d['highest_price'])}  (+{high_pct:.2f}%)\n"
            f"💵 Now:      {self._fp(d['current_price'])}  ({cur_pct:+.2f}%)\n"
            f"⏱  Age:      {age}\n\n"
            f"{'🟢 Still above target' if cur_pct >= target else '⚠️ Price pulled back from target'}"
        )

    # ── reversal warning format ──────────────────────────────────────

    def _fmt_reversal(self, d: dict) -> str:
        return (
            f"⚠️ <b>REVERSAL WARNING</b>\n"
            f"{'━' * 28}\n\n"
            f"📌 <b>{d['symbol']}</b>\n"
            f"💵 Entry:    {self._fp(d['entry_price'])}\n"
            f"🏔  Peak:     {self._fp(d['highest_price'])}  (+{d['high_pct']:.2f}%)\n"
            f"💵 Now:      {self._fp(d['current_price'])}  ({d['cur_pct']:+.2f}%)\n"
            f"📉 Drop:     {d['drop_pct']:.2f}% from peak\n"
            f"⏱  Age:      {d.get('age_str', '')}\n\n"
            f"Price has dropped significantly from its peak.\n"
            f"Consider taking remaining profits."
        )
