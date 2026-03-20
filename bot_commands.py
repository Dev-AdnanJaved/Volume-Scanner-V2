"""
Telegram command listener.

Commands:
  /report          — all active signals with performance
  /report SYMBOL   — detailed single-coin breakdown
  /summary         — win rate, averages, best/worst
  /active          — quick list of tracked symbols
  /detailed_report — sends JSON file of completed signals (≥7 days old) with all data
  /export_csv      — flat CSV of all signals (active + archived) for analysis
  /help            — command reference
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import requests

from binance_client import BinanceClient
from tracker import SignalTracker

logger = logging.getLogger(__name__)

EXPORT_CHUNK_SIZE = 200


class TelegramCommandListener:

    API = "https://api.telegram.org/bot{token}/{method}"

    def __init__(
        self,
        bot_token: str,
        chat_id: str,
        tracker: SignalTracker,
        binance: BinanceClient,
    ) -> None:
        self._token = bot_token
        self._chat_id = str(chat_id)
        self._tracker = tracker
        self._binance = binance
        self._session = requests.Session()
        self._offset: int = 0
        self._running = False

    def _url(self, method: str) -> str:
        return self.API.format(token=self._token, method=method)

    def _send(self, chat_id: str, text: str) -> bool:
        MAX_LEN = 4000
        parts: list[str] = []
        while len(text) > MAX_LEN:
            idx = text.rfind("\n", 0, MAX_LEN)
            if idx == -1:
                idx = MAX_LEN
            parts.append(text[:idx])
            text = text[idx:].lstrip("\n")
        parts.append(text)

        for part in parts:
            if not part.strip():
                continue
            for attempt in range(3):
                try:
                    r = self._session.post(
                        self._url("sendMessage"),
                        json={
                            "chat_id": chat_id,
                            "text": part,
                            "parse_mode": "HTML",
                            "disable_web_page_preview": True,
                        },
                        timeout=15,
                    ).json()
                    if r.get("ok"):
                        break
                    if r.get("error_code") == 429:
                        wait = r.get("parameters", {}).get("retry_after", 30)
                        time.sleep(wait)
                        continue
                    logger.error("Telegram send error: %s", r)
                    return False
                except Exception as exc:
                    logger.error("Telegram send failed (attempt %d): %s", attempt + 1, exc)
                    time.sleep(2)
            time.sleep(0.3)
        return True

    def _send_document(self, chat_id: str, file_path: str, caption: str = "") -> bool:
        """Send a file as a Telegram document."""
        for attempt in range(3):
            try:
                with open(file_path, "rb") as f:
                    r = self._session.post(
                        self._url("sendDocument"),
                        data={"chat_id": chat_id, "caption": caption},
                        files={"document": f},
                        timeout=30,
                    ).json()
                if r.get("ok"):
                    return True
                if r.get("error_code") == 429:
                    wait = r.get("parameters", {}).get("retry_after", 30)
                    time.sleep(wait)
                    continue
                logger.error("Telegram sendDocument error: %s", r)
                return False
            except Exception as exc:
                logger.error("Telegram sendDocument failed (attempt %d): %s", attempt + 1, exc)
                time.sleep(2)
        return False

    @staticmethod
    def _chunks(lst: list, size: int = EXPORT_CHUNK_SIZE) -> list:
        return [lst[i:i + size] for i in range(0, len(lst), size)]

    def _send_chunked_json(self, chat_id: str, data: list, prefix: str, label: str) -> None:
        if not data:
            self._send(chat_id, f"📭 No signals for {label}.")
            return

        chunks = self._chunks(data)
        total_parts = len(chunks)
        now_ts = int(time.time())
        gen_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        for idx, chunk in enumerate(chunks, 1):
            part_label = f"Part {idx}/{total_parts} • " if total_parts > 1 else ""
            tmp_path = f"/tmp/{prefix}_part{idx}of{total_parts}_{now_ts}.json"
            try:
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(chunk, f, indent=2)

                caption = (
                    f"{label}\n"
                    f"{part_label}{len(chunk)} signals\n"
                    f"Total: {len(data)}\n"
                    f"Generated: {gen_str}"
                )
                success = self._send_document(chat_id, tmp_path, caption)

                if not success:
                    self._send(chat_id, f"❌ Failed to send file part {idx}.")
                    return
            except Exception as exc:
                self._send(chat_id, f"❌ Failed to write/send file part {idx}: {exc}")
                return
            finally:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

        logger.info("%s sent: %d signals in %d file(s)", label, len(data), total_parts)

    def _poll(self) -> list:
        try:
            resp = self._session.get(
                self._url("getUpdates"),
                params={
                    "offset": self._offset,
                    "timeout": 10,
                    "allowed_updates": '["message"]',
                },
                timeout=15,
            ).json()
            if not resp.get("ok"):
                return []
            return resp.get("result", [])
        except Exception:
            return []

    # ── formatting helpers ───────────────────────────────────────────

    @staticmethod
    def _fmt_price(price: float) -> str:
        if price <= 0:
            return "N/A"
        if price >= 1000:
            return f"${price:,.2f}"
        if price >= 1:
            return f"${price:.4f}"
        if price >= 0.001:
            return f"${price:.6f}"
        return f"${price:.8f}"

    @staticmethod
    def _fmt_pct(pct: float) -> str:
        icon = "🟢" if pct > 0 else "🔴" if pct < 0 else "⚪"
        return f"{icon} {pct:+.2f}%"

    @staticmethod
    def _fmt_age(ts: float) -> str:
        age = time.time() - ts
        if age < 3600:
            return f"{int(age / 60)}m"
        hours = int(age // 3600)
        mins = int((age % 3600) // 60)
        return f"{hours}h {mins}m"

    @staticmethod
    def _calc_pct(entry: float, current: float) -> float:
        if entry <= 0:
            return 0.0
        return ((current - entry) / entry) * 100.0

    @staticmethod
    def _result_emoji(pct: float) -> str:
        if pct >= 10:
            return "🚀"
        if pct >= 5:
            return "✅"
        if pct >= 0:
            return "🟢"
        if pct >= -5:
            return "🟡"
        return "🔴"

    # ── main loop ────────────────────────────────────────────────────

    def run(self) -> None:
        self._running = True
        logger.info("Telegram command listener started")
        updates = self._poll()
        if updates:
            self._offset = updates[-1]["update_id"] + 1
            logger.info("Skipped %d old queued messages", len(updates))
        while self._running:
            try:
                updates = self._poll()
                for update in updates:
                    self._offset = update["update_id"] + 1
                    self._handle(update)
            except Exception:
                logger.error("Command listener error", exc_info=True)
                time.sleep(5)

    def stop(self) -> None:
        self._running = False

    # ── dispatcher ───────────────────────────────────────────────────

    def _handle(self, update: dict) -> None:
        msg = update.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        text = msg.get("text", "").strip()

        if chat_id != self._chat_id:
            return
        if not text.startswith("/"):
            return

        parts = text.split()
        cmd = parts[0].lower().split("@")[0]
        args = parts[1:]

        logger.info("Command received: %s %s", cmd, args)

        handlers = {
            "/report":          lambda: self._cmd_report(chat_id, args),
            "/summary":         lambda: self._cmd_summary(chat_id),
            "/active":          lambda: self._cmd_active(chat_id),
            "/export":          lambda: self._cmd_export(chat_id),
            "/coin":            lambda: self._cmd_coin(chat_id, args),
            "/detailed_report": lambda: self._cmd_detailed_report(chat_id),
            "/export_csv":      lambda: self._cmd_export_csv(chat_id),
            "/validate":        lambda: self._cmd_validate(chat_id),
            "/help":            lambda: self._cmd_help(chat_id),
            "/start":           lambda: self._cmd_help(chat_id),
        }
        handler = handlers.get(cmd)
        if handler:
            handler()
        else:
            self._send(chat_id, "❓ Unknown command. Send /help")

    # ── /report ──────────────────────────────────────────────────────

    def _cmd_report(self, chat_id: str, args: list) -> None:
        try:
            prices = self._binance.get_mark_prices()
            self._tracker.apply_prices(prices)
        except Exception:
            prices = {}

        signals = self._tracker.get_active_signals()
        if not signals:
            self._send(chat_id, "📊 No active signals.")
            return

        if args:
            sym = args[0].upper()
            if not sym.endswith("USDT"):
                sym += "USDT"
            matches = [s for s in signals if s["symbol"] == sym]
            if not matches:
                self._send(chat_id, f"📊 No active signal for <b>{sym}</b>")
                return
            for sig in matches:
                self._send_detailed_report(chat_id, sig, prices)
            return

        signals.sort(key=lambda s: s["alert_time_ts"], reverse=True)
        lines = ["📊 <b>PERFORMANCE REPORT</b>", ""]

        valid_changes: list[float] = []
        valid_highest: list[float] = []

        for sig in signals:
            sym = sig["symbol"]
            entry = sig.get("entry_price", 0)
            highest = sig.get("highest_price", entry)
            current = prices.get(sym, sig.get("current_price", 0))
            if current > highest:
                highest = current

            age = self._fmt_age(sig["alert_time_ts"])
            brk = sig.get("breakout_margin_pct", 0)

            if entry > 0 and current > 0:
                cur_pct = self._calc_pct(entry, current)
                high_pct = self._calc_pct(entry, highest)
                valid_changes.append(cur_pct)
                valid_highest.append(high_pct)
                emoji = self._result_emoji(cur_pct)
                lines.append(f"{emoji} <b>{sym}</b>  •  {age}")
                lines.append(f"   Now: {cur_pct:+.2f}%  │  Peak: {high_pct:+.2f}%  │  Brk: +{brk:.2f}%")
                lines.append("")
            else:
                lines.append(f"⚪ <b>{sym}</b>  •  {age}  •  No price data")
                lines.append("")

        if valid_changes:
            total = len(valid_changes)
            avg_cur = sum(valid_changes) / total
            avg_high = sum(valid_highest) / total
            winners = sum(1 for c in valid_changes if c > 0)
            peak_w = sum(1 for h in valid_highest if h > 2)
            lines.append("━" * 26)
            lines.append(f"📡 Signals:    {total}")
            lines.append(f"📊 Avg now:    {avg_cur:+.2f}%")
            lines.append(f"🏔  Avg peak:   {avg_high:+.2f}%")
            lines.append(f"🎯 Win now:    {winners}/{total} ({winners/total*100:.0f}%)")
            lines.append(f"🎯 Win peak:   {peak_w}/{total} ({peak_w/total*100:.0f}%)")
            lines.append("")
            lines.append("━━━ 🎯 TP HITS ━━━")
            for tp in self._tracker.tp_targets:
                count = sum(
                    1 for s in signals
                    if s.get("outcome", {}).get(f"tp{tp}_hit", False)
                )
                label = f"{count} signal{'s' if count != 1 else ''}" if count > 0 else "0"
                lines.append(f"TP +{tp}%:".ljust(11) + label)
            lines.append("")
            lines.append("💡 /report SYMBOL for details")

        self._send(chat_id, "\n".join(lines))

    def _send_detailed_report(self, chat_id: str, sig: dict, prices: dict) -> None:
        sym = sig["symbol"]
        entry = sig.get("entry_price", 0)
        highest = sig.get("highest_price", entry)
        lowest = sig.get("lowest_price", entry)
        current = prices.get(sym, sig.get("current_price", 0))
        if current > highest:
            highest = current

        cur_pct = self._calc_pct(entry, current) if entry > 0 else 0
        high_pct = self._calc_pct(entry, highest) if entry > 0 else 0
        low_pct = self._calc_pct(entry, lowest) if entry > 0 and lowest > 0 else 0
        age = self._fmt_age(sig["alert_time_ts"])

        lines = [
            f"📊 <b>{sym} — DETAILED</b>",
            "",
            "━━━ 💵 PRICE ━━━",
            f"Entry:     {self._fmt_price(entry)}",
            f"Current:   {self._fmt_price(current)}   {self._fmt_pct(cur_pct)}",
            f"Peak:      {self._fmt_price(highest)}   {self._fmt_pct(high_pct)}",
            f"Lowest:    {self._fmt_price(lowest)}   {self._fmt_pct(low_pct)}",
            f"Age:       {age}",
            "",
            "━━━ 🔺 MAIN CRITERIA ━━━",
            f"Breakout:  +{sig.get('breakout_margin_pct', 0):.2f}% above 24h high",
            f"Volume:    {sig.get('vol_candle_1_fmt','?')} → {sig.get('vol_candle_2_fmt','?')} → {sig.get('vol_candle_3_fmt','?')}",
            f"24h chg:   {sig.get('price_change_24h', 0):+.1f}%",
        ]

        add = sig.get("additional_data", {})
        if add:
            lines.append("")
            lines.append("━━━ 📈 ADDITIONAL DATA ━━━")
            if add.get("rvol_20") is not None:
                lines.append(f"RVOL (20):  {add['rvol_20']:.2f}x")
            if add.get("oi_change_pct") is not None:
                lines.append(f"OI change:  {add['oi_change_pct']:+.2f}%")
            if add.get("funding_rate") is not None:
                fr = add["funding_rate"]
                fr_ok = "✅" if add.get("funding_in_ideal_range") else "⚠️"
                lines.append(f"Funding:    {fr_ok} {fr:.4f}%")
            if add.get("vol_24h_usdt") is not None:
                vol_m = add["vol_24h_usdt"] / 1e6
                liq_ok = "✅" if add.get("vol_24h_above_50m") else "⚠️"
                lines.append(f"24h Vol:    {liq_ok} ${vol_m:.1f}M")
            if add.get("price_above_ema50_4h") is not None:
                ema_ok = "✅ above" if add["price_above_ema50_4h"] else "⚠️ below"
                lines.append(f"4h EMA50:   {ema_ok} ({add.get('ema50_distance_pct', 0):+.2f}%)")
            if add.get("volatility_compression_ratio") is not None:
                cr = add["volatility_compression_ratio"]
                comp = "✅ compressed" if add.get("is_compressed") else "➡️ normal"
                lines.append(f"Volatility: {comp} (ratio {cr:.2f})")

        outcome = sig.get("outcome", {})
        if outcome:
            lines.append("")
            lines.append("━━━ 📉 OUTCOME ━━━")
            sig_type = outcome.get("signal_type", "active")
            type_icons = {"fast": "⚡", "slow": "🐌", "delayed": "🕐", "failed": "❌", "active": "🔄"}
            lines.append(f"Type:      {type_icons.get(sig_type, '❓')} {sig_type}")

            closed = outcome.get("signal_closed", False)
            close_reason = outcome.get("close_reason")
            lines.append(f"Status:    {'🔒 Closed' if closed else '🟢 Active'}" + (f" ({close_reason})" if close_reason else ""))

            dd = outcome.get("max_drawdown_pct", 0)
            dd_hrs = outcome.get("max_drawdown_hours_after_entry")
            dd_time = f" ({dd_hrs:.1f}h after entry)" if dd_hrs is not None else ""
            lines.append(f"Max DD:    {dd:+.2f}%{dd_time}")

            neg = outcome.get("went_negative_before_tp", False)
            neg_hrs = outcome.get("hours_negative_total", 0)
            lines.append(f"Neg b/TP:  {'Yes' if neg else 'No'}" + (f" ({neg_hrs:.1f}h total)" if neg_hrs > 0 else ""))

            peak_hrs = outcome.get("peak_hours_after_entry")
            if peak_hrs is not None:
                lines.append(f"Peak at:   {peak_hrs:.1f}h after entry")

            first_tp_hrs = None
            for tp in self._tracker.tp_targets:
                key = f"tp{tp}"
                if outcome.get(f"{key}_hit"):
                    tp_hrs = outcome.get(f"{key}_hit_hours_after_entry")
                    tp_dd = outcome.get(f"{key}_max_drawdown_before", 0)
                    tp_line = f"TP +{tp}%:   ✅ hit"
                    if tp_hrs is not None:
                        tp_line += f" @ {tp_hrs:.1f}h"
                        if first_tp_hrs is None or tp_hrs < first_tp_hrs:
                            first_tp_hrs = tp_hrs
                    if tp_dd and tp_dd < 0:
                        tp_line += f" (DD before: {tp_dd:+.2f}%)"
                    lines.append(tp_line)

                    snap = sig.get(f"{key}_snapshot")
                    if snap:
                        snap_parts = []
                        oi_chg = snap.get("oi_change_pct")
                        if oi_chg is not None:
                            snap_parts.append(f"OI {oi_chg:+.1f}%")
                        fr = snap.get("funding_rate")
                        if fr is not None:
                            fr_ok = "✅" if snap.get("funding_in_ideal_range") else "⚠️"
                            snap_parts.append(f"FR {fr_ok}{fr:.4f}%")
                        mom_1h = snap.get("price_momentum_1h_pct")
                        if mom_1h is not None:
                            snap_parts.append(f"1h {mom_1h:+.1f}%")
                        mom_4h = snap.get("price_momentum_4h_pct")
                        if mom_4h is not None:
                            snap_parts.append(f"4h {mom_4h:+.1f}%")
                        colors = snap.get("candle_colors_at_hit")
                        if colors:
                            color_str = "".join("🟢" if c == "green" else "🔴" for c in colors)
                            snap_parts.append(color_str)
                        if snap_parts:
                            lines.append(f"         📸 {' | '.join(snap_parts)}")

            if first_tp_hrs is not None:
                lines.append(f"1st TP:    ⚡ {first_tp_hrs:.1f}h after entry")

            btc_to_tp = outcome.get("btc_change_entry_to_tp")
            if btc_to_tp is not None:
                lines.append(f"BTC→1stTP: {btc_to_tp:+.2f}%")

            btc_trend = outcome.get("btc_trend_during_signal")
            if btc_trend:
                trend_icons = {"pumping": "🟢", "dumping": "🔴", "ranging": "➡️"}
                lines.append(f"BTC trend: {trend_icons.get(btc_trend, '❓')} {btc_trend}")

        btc_at = sig.get("btc_price")
        btc_now = prices.get("BTCUSDT")
        if btc_at and btc_now:
            btc_chg = self._calc_pct(btc_at, btc_now)
            lines.append("")
            lines.append("━━━ ₿ BTC CONTEXT ━━━")
            lines.append(f"BTC:  {self._fmt_price(btc_at)} → {self._fmt_price(btc_now)}  ({btc_chg:+.2f}%)")

        lines.append("")
        lines.append(f"🕐 Signal: {sig.get('alert_time', 'N/A')}")

        self._send(chat_id, "\n".join(lines))

    # ── /summary ─────────────────────────────────────────────────────

    def _cmd_summary(self, chat_id: str) -> None:
        try:
            prices = self._binance.get_mark_prices()
            self._tracker.apply_prices(prices)
        except Exception:
            prices = {}

        signals = self._tracker.get_active_signals()
        history = self._tracker.get_history()

        lines = ["📊 <b>SUMMARY</b>", ""]

        active_valid = [s for s in signals if s.get("entry_price", 0) > 0]
        if active_valid:
            changes: list[float] = []
            highest_changes: list[float] = []
            for s in active_valid:
                cur = prices.get(s["symbol"], s.get("current_price", s["entry_price"]))
                changes.append(self._calc_pct(s["entry_price"], cur))
                highest_changes.append(
                    self._calc_pct(
                        s["entry_price"],
                        max(s.get("highest_price", s["entry_price"]), cur),
                    )
                )

            winners = sum(1 for c in changes if c > 0)
            peak_w = sum(1 for h in highest_changes if h > 2)
            best_i = changes.index(max(changes))
            worst_i = changes.index(min(changes))
            best_h_i = highest_changes.index(max(highest_changes))

            lines.append(f"━━━ 📡 ACTIVE ({len(active_valid)}) ━━━")
            lines.append(f"Avg now:    {sum(changes)/len(changes):+.2f}%")
            lines.append(f"Avg peak:   {sum(highest_changes)/len(highest_changes):+.2f}%")
            lines.append(f"Win now:    {winners}/{len(active_valid)} ({winners/len(active_valid)*100:.0f}%)")
            lines.append(f"Win peak:   {peak_w}/{len(active_valid)} ({peak_w/len(active_valid)*100:.0f}%)")
            lines.append("")
            lines.append(f"🚀 Best:     {active_valid[best_i]['symbol']} {changes[best_i]:+.2f}%")
            lines.append(f"🔴 Worst:    {active_valid[worst_i]['symbol']} {changes[worst_i]:+.2f}%")
            lines.append(f"🏔  Top peak:  {active_valid[best_h_i]['symbol']} {highest_changes[best_h_i]:+.2f}%")
        else:
            lines.append("📡 No active signals")

        lines.append("")

        if history:
            exit_pcts = [h.get("exit_pct") for h in history if h.get("exit_pct") is not None]
            high_pcts = [h.get("peak_pct") or h.get("highest_pct") for h in history if (h.get("peak_pct") or h.get("highest_pct")) is not None]
            if exit_pcts:
                h_win = sum(1 for p in exit_pcts if p > 0)
                lines.append(f"━━━ 📜 HISTORY ({len(history)}) ━━━")
                lines.append(f"Avg exit:   {sum(exit_pcts)/len(exit_pcts):+.2f}%")
                if high_pcts:
                    lines.append(f"Avg peak:   {sum(high_pcts)/len(high_pcts):+.2f}%")
                lines.append(f"Win rate:   {h_win}/{len(exit_pcts)} ({h_win/len(exit_pcts)*100:.0f}%)")
        else:
            lines.append("📜 No history yet")

        self._send(chat_id, "\n".join(lines))

    # ── /active ──────────────────────────────────────────────────────

    def _cmd_active(self, chat_id: str) -> None:
        signals = self._tracker.get_active_signals()
        if not signals:
            self._send(chat_id, "📡 No active signals.")
            return

        signals.sort(key=lambda s: s["alert_time_ts"], reverse=True)
        lines = [f"📡 <b>ACTIVE ({len(signals)})</b>", ""]

        for sig in signals:
            age = self._fmt_age(sig["alert_time_ts"])
            sym = sig["symbol"]
            brk = sig.get("breakout_margin_pct", 0)
            v1 = sig.get("vol_candle_1_fmt", "?")
            v3 = sig.get("vol_candle_3_fmt", "?")
            lines.append(f"• <b>{sym}</b>  {age}  brk:+{brk:.1f}%  vol:{v1}→{v3}")

        lines.append("")
        lines.append(f"Window: {self._tracker.max_age_hours}h")
        lines.append("/report SYMBOL for details")
        self._send(chat_id, "\n".join(lines))

    # ── /detailed_report ─────────────────────────────────────────────

    def _cmd_detailed_report(self, chat_id: str) -> None:
        self._send(chat_id, "⏳ Building detailed report, please wait…")

        try:
            completed = self._tracker.get_completed_signals(
                self._tracker.detailed_report_min_age_seconds
            )
        except Exception as exc:
            self._send(chat_id, f"❌ Error loading signals: {exc}")
            return

        if not completed:
            min_h = int(self._tracker.detailed_report_min_age_seconds // 3600)
            self._send(
                chat_id,
                f"📭 No completed signals yet.\n"
                f"Signals need to be at least {min_h}h old to appear in this report.\n"
                f"Use /report or /summary to see active signals."
            )
            return

        report = []
        for sig in completed:
            entry = sig.get("entry_price", 0)
            highest = sig.get("highest_price", 0)
            lowest = sig.get("lowest_price", 0)
            current = sig.get("current_price", 0)

            record = {
                "symbol":              sig.get("symbol"),
                "timeframe":           sig.get("timeframe", "1h"),
                "signal_time":         sig.get("alert_time"),
                "archived_time":       sig.get("archived_time"),
                "tracked_hours":       sig.get("tracked_hours"),
                "entry_price":         entry,
                "exit_price":          sig.get("exit_price", current),
                "peak_price":          highest,
                "lowest_price":        lowest,
                "peak_pct":            sig.get("peak_pct"),
                "lowest_pct":          sig.get("lowest_pct"),
                "exit_pct":            sig.get("exit_pct"),
                "tp_targets_hit":      sig.get("tp_sent", []),
                "reversal_warned":     sig.get("reversal_warned", False),
                "main_criteria": {
                    "breakout_margin_pct": sig.get("breakout_margin_pct"),
                    "high_24h_at_signal":  sig.get("high_24h"),
                    "vol_candle_1":        sig.get("vol_candle_1"),
                    "vol_candle_2":        sig.get("vol_candle_2"),
                    "vol_candle_3":        sig.get("vol_candle_3"),
                    "vol_candle_1_fmt":    sig.get("vol_candle_1_fmt"),
                    "vol_candle_2_fmt":    sig.get("vol_candle_2_fmt"),
                    "vol_candle_3_fmt":    sig.get("vol_candle_3_fmt"),
                    "rvol":                sig.get("rvol"),
                    "price_change_24h":    sig.get("price_change_24h"),
                },
                "additional_data":     sig.get("additional_data", {}),
                "btc_price_at_signal": sig.get("btc_price"),
                "candle_time":         sig.get("candle_time"),
                "high_breakout_warning": sig.get("high_breakout_warning", False),
                "outcome":             sig.get("outcome", {}),
                "price_journey":       sig.get("price_journey", []),
            }
            for k, v in sig.items():
                if k.endswith("_snapshot") and k.startswith("tp") and isinstance(v, dict):
                    record[k] = v
            report.append(record)

        chunks = self._chunks(report)
        if len(chunks) > 1:
            self._send(chat_id, f"📊 {len(report)} signals → {len(chunks)} files ({EXPORT_CHUNK_SIZE} per file)")

        self._send_chunked_json(chat_id, report, "detailed_report", "📊 Detailed Signal Report")

    # ── /export ─────────────────────────────────────────────────────

    def _cmd_export(self, chat_id: str) -> None:
        self._send(chat_id, "⏳ Exporting active signals…")

        try:
            prices = self._binance.get_mark_prices()
            self._tracker.apply_prices(prices)
        except Exception:
            prices = {}

        signals = self._tracker.get_active_signals()
        if not signals:
            self._send(chat_id, "📭 No active signals to export.")
            return

        for sig in signals:
            sym = sig["symbol"]
            entry = sig.get("entry_price", 0)
            current = prices.get(sym, sig.get("current_price", 0))
            highest = sig.get("highest_price", entry)
            lowest = sig.get("lowest_price", entry)
            if current > highest:
                highest = current
            sig["current_price"] = current
            sig["highest_price"] = highest
            if entry > 0:
                sig["current_pct"] = round(((current - entry) / entry) * 100, 2)
                sig["peak_pct"] = round(((highest - entry) / entry) * 100, 2)
                sig["lowest_pct"] = round(((lowest - entry) / entry) * 100, 2) if lowest > 0 else None
            sig.pop("_prev_highest", None)
            sig.pop("_prev_lowest", None)

        chunks = self._chunks(signals)
        if len(chunks) > 1:
            self._send(chat_id, f"📡 {len(signals)} signals → {len(chunks)} files ({EXPORT_CHUNK_SIZE} per file)")

        self._send_chunked_json(chat_id, signals, "active_signals", "📡 Active Signals Export")

    # ── /coin ──────────────────────────────────────────────────────────

    def _cmd_coin(self, chat_id: str, args: list) -> None:
        if not args:
            self._send(chat_id, "Usage: /coin ETH  or  /coin ETHUSDT")
            return

        sym = args[0].upper()
        if not sym.endswith("USDT"):
            sym += "USDT"

        try:
            prices = self._binance.get_mark_prices()
            self._tracker.apply_prices(prices)
        except Exception:
            prices = {}

        signals = self._tracker.get_active_signals()
        matches = [s for s in signals if s["symbol"] == sym]

        if not matches:
            self._send(chat_id, f"📭 No active signal for <b>{sym}</b>")
            return

        for sig in matches:
            entry = sig.get("entry_price", 0)
            current = prices.get(sym, sig.get("current_price", 0))
            highest = sig.get("highest_price", entry)
            lowest = sig.get("lowest_price", entry)
            if current > highest:
                highest = current
            sig["current_price"] = current
            sig["highest_price"] = highest
            if entry > 0:
                sig["current_pct"] = round(((current - entry) / entry) * 100, 2)
                sig["peak_pct"] = round(((highest - entry) / entry) * 100, 2)
                sig["lowest_pct"] = round(((lowest - entry) / entry) * 100, 2) if lowest > 0 else None
            sig.pop("_prev_highest", None)
            sig.pop("_prev_lowest", None)

        data = matches

        now_ts = int(time.time())
        tmp_path = f"/tmp/{sym}_{now_ts}.json"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as exc:
            self._send(chat_id, f"❌ Failed to write file: {exc}")
            return

        caption = (
            f"📌 {sym} Signal Export\n"
            f"Signals: {len(matches)}\n"
            f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
        success = self._send_document(chat_id, tmp_path, caption)

        try:
            os.remove(tmp_path)
        except Exception:
            pass

        if not success:
            self._send(chat_id, "❌ Failed to send file. Check bot logs.")
        else:
            logger.info("Coin export sent for %s: %d signal(s)", sym, len(matches))

    # ── /export_csv ──────────────────────────────────────────────────

    def _cmd_export_csv(self, chat_id: str) -> None:
        self._send(chat_id, "⏳ Building flat CSV export…")

        try:
            from export_csv import load_all_signals, build_csv, compute_fieldnames

            signals = load_all_signals(active=True, history=True)
            if not signals:
                self._send(chat_id, "📭 No signals found (active or archived).")
                return

            all_fieldnames = compute_fieldnames(signals)

            chunks = self._chunks(signals)
            total_parts = len(chunks)
            if total_parts > 1:
                self._send(chat_id, f"📊 {len(signals)} signals → {total_parts} files ({EXPORT_CHUNK_SIZE} per file)")

            now_ts = int(time.time())
            now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            gen_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

            for idx, chunk in enumerate(chunks, 1):
                part_label = f"Part {idx}/{total_parts} • " if total_parts > 1 else ""
                tmp_path = f"/tmp/signals_flat_part{idx}of{total_parts}_{now_str}_{now_ts}.csv"
                try:
                    count = build_csv(chunk, tmp_path, fieldnames=all_fieldnames)

                    file_size = os.path.getsize(tmp_path)
                    size_str = f"{file_size / 1024:.1f} KB" if file_size < 1_048_576 else f"{file_size / 1_048_576:.1f} MB"

                    caption = (
                        f"📊 Flat CSV Export\n"
                        f"{part_label}{count} signals\n"
                        f"Total: {len(signals)}\n"
                        f"Size: {size_str}\n"
                        f"Generated: {gen_str}"
                    )
                    success = self._send_document(chat_id, tmp_path, caption)

                    if not success:
                        self._send(chat_id, f"❌ Failed to send CSV file part {idx}.")
                        return
                except Exception as exc:
                    logger.error("CSV export chunk %d failed: %s", idx, exc)
                    self._send(chat_id, f"❌ CSV export failed on part {idx}: {exc}")
                    return
                finally:
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass

            logger.info("CSV export sent: %d signals in %d file(s)", len(signals), total_parts)
        except Exception as exc:
            logger.error("CSV export failed: %s", exc)
            self._send(chat_id, f"❌ CSV export failed: {exc}")

    # ── /validate ──────────────────────────────────────────────────────

    def _cmd_validate(self, chat_id: str) -> None:
        signals = self._tracker.get_active_signals()

        if not signals:
            self._send(chat_id, "🔍 <b>VALIDATE</b>\n\nNo active signals to check.")
            return

        tp_targets = self._tracker.tp_targets

        cat_additional: list = []
        cat_signal: list = []
        cat_volume: list = []
        cat_outcome: list = []

        for s in signals:
            sym = s.get("symbol", "???")
            ad = s.get("additional_data", {})
            out = s.get("outcome", {})

            if not ad:
                cat_additional.append(f"{sym}: additional_data is empty")
            else:
                if ad.get("oi_growth_ratio") is None:
                    cat_additional.append(f"{sym}: oi_growth_ratio is null")
                if ad.get("funding_rate") is None:
                    cat_additional.append(f"{sym}: funding_rate is null")
                if ad.get("rvol_20") is None:
                    cat_additional.append(f"{sym}: rvol_20 is null")
                if ad.get("vol_24h_usdt") is None:
                    cat_additional.append(f"{sym}: vol_24h_usdt missing")
                if ad.get("vol_24h_base") is None:
                    cat_additional.append(f"{sym}: vol_24h_base missing")

            if "high_breakout_warning" not in s:
                cat_signal.append(f"{sym}: high_breakout_warning missing")

            for n in (1, 2, 3):
                if s.get(f"vol_candle_{n}_base") is None:
                    cat_volume.append(f"{sym}: vol_candle_{n}_base missing")

            for tp in tp_targets:
                key = f"tp{tp}_hit"
                if out.get(key) is None:
                    cat_outcome.append(f"{sym}: {key} missing from outcome")
                    break

        all_issues = cat_additional + cat_signal + cat_volume + cat_outcome
        problem_syms = set()
        for i in all_issues:
            problem_syms.add(i.split(":")[0])
        clean = len(signals) - len(problem_syms)

        lines = [
            "🔍 <b>VALIDATE</b>",
            "",
            f"📊 Total signals: {len(signals)}",
            f"✅ Clean: {clean}",
            f"⚠️ With issues: {len(problem_syms)}",
        ]

        if all_issues:
            shown = 0
            limit = 50
            for label, cat in [
                ("📋 Additional Data", cat_additional),
                ("📌 Signal Fields", cat_signal),
                ("📊 Volume Fields", cat_volume),
                ("🎯 Outcome Fields", cat_outcome),
            ]:
                if not cat or shown >= limit:
                    continue
                lines.append("")
                lines.append(f"<b>{label} ({len(cat)}):</b>")
                for i in cat:
                    if shown >= limit:
                        lines.append(f"  ... truncated")
                        break
                    lines.append(f"  • {i}")
                    shown += 1
        else:
            lines.append("")
            lines.append("🎉 All signals look clean!")

        self._send(chat_id, "\n".join(lines))

    # ── /help ────────────────────────────────────────────────────────

    def _cmd_help(self, chat_id: str) -> None:
        text = (
            "🤖 <b>COMMANDS</b>\n\n"
            "/report — Performance overview of all active signals\n"
            "/report BTC — Detailed breakdown for one coin\n"
            "/summary — Win rates, averages, best/worst\n"
            "/active — Quick list of tracked signals\n"
            "/export — JSON file of all currently active signals\n"
            "/coin ETH — JSON file for a specific coin\n"
            "/detailed_report — JSON file of completed signals (≥7 days)\n"
            "                   Includes all main + additional data,\n"
            "                   peak, lowest, exit prices\n"
            "/export_csv — Flat CSV of all signals for analysis\n"
            "/validate — Data integrity check on active signals\n"
            "/help — This message\n\n"
            f"📡 Tracking window: {self._tracker.max_age_hours}h\n"
            "🏔 Prices update every 5 min\n"
            "🎯 Auto TP alerts at configured targets\n"
            "⚠️ Auto reversal warnings\n\n"
            "<b>Signal criteria:</b>\n"
            "1️⃣ 1h close breaks last 24h high\n"
            "2️⃣ Last 3 candles volume increasing (min 2x ratio)\n"
            "3️⃣ 24h price change ≤ ±20%"
        )
        self._send(chat_id, text)
