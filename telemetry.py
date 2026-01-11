import json
import sys
import time
import traceback
from datetime import datetime, date
from pathlib import Path
from typing import Optional, Dict, Any

import requests


class Telemetry:
    """
    Photobooth telemetry:
      - Crash detected message with stack trace (Telegram)
      - Heartbeat message periodically (Telegram)
      - Lifetime + daily counters for:
          * template selections
          * prints
          * photos taken
          * sessions started/completed
      - Paper "roll" tracking:
          * notify at 143 prints on current roll
          * notify at 147 prints on current roll
          * automatically reset roll_prints to 0 at 147 (and log)
      - Local persistence always (stats.json + daily jsonl log)
    """

    def __init__(
        self,
        booth_id: "Stanford booth",
        telegram_token="8537687055: AAHtcNOv2l - QYRmMBvcDNK8idGF2EmUnT7c",
        telegram_chat_id = "8471596713",
        data_dir: str = "telemetry",
        roll_warn_1: int = 143,
        roll_warn_2: int = 147,
    ):
        self.booth_id = booth_id
        self.telegram_token = telegram_token
        self.telegram_chat_id = str(telegram_chat_id)

        self.roll_warn_1 = int(roll_warn_1)
        self.roll_warn_2 = int(roll_warn_2)

        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.stats_path = self.data_dir / "stats.json"
        self.events_path = self.data_dir / f"events_{date.today().isoformat()}.jsonl"

        self._stats = self._load_stats()
        self._ensure_today_initialized()

    # ------------------------
    # Persistence helpers
    # ------------------------
    def _fresh_stats(self) -> Dict[str, Any]:
        today = date.today().isoformat()
        return {
            "booth_id": self.booth_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),

            # Lifetime counters
            "lifetime": {
                "sessions_started": 0,
                "sessions_completed": 0,
                "photos_taken": 0,
                "prints_total": 0,
                "prints_success": 0,
                "prints_failed": 0,
                "template_counts": {},  # template_id -> count
            },

            # Daily counters keyed by YYYY-MM-DD
            "daily": {
                today: {
                    "sessions_started": 0,
                    "sessions_completed": 0,
                    "photos_taken": 0,
                    "prints_total": 0,
                    "prints_success": 0,
                    "prints_failed": 0,
                    "template_counts": {},
                }
            },

            # Paper roll tracking
            "paper": {
                "roll_prints": 0,  # prints on current roll since last reset
                "last_reset_at": None,
                "notified_at": {
                    str(self.roll_warn_1): False,
                    str(self.roll_warn_2): False,
                }
            },

            "last_event": None,
        }

    def _load_stats(self) -> Dict[str, Any]:
        if self.stats_path.exists():
            try:
                data = json.loads(self.stats_path.read_text(encoding="utf-8"))
                return data
            except Exception:
                # Corrupt file shouldn't break your booth
                return self._fresh_stats()
        return self._fresh_stats()

    def _save_stats(self) -> None:
        tmp = self.stats_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._stats, indent=2), encoding="utf-8")
        tmp.replace(self.stats_path)

    def _ensure_today_initialized(self) -> None:
        today = date.today().isoformat()
        if "daily" not in self._stats:
            self._stats["daily"] = {}
        if today not in self._stats["daily"]:
            self._stats["daily"][today] = {
                "sessions_started": 0,
                "sessions_completed": 0,
                "photos_taken": 0,
                "prints_total": 0,
                "prints_success": 0,
                "prints_failed": 0,
                "template_counts": {},
            }
            self._save_stats()

        # Ensure paper notified flags exist for thresholds
        paper = self._stats.get("paper", {})
        if "notified_at" not in paper:
            paper["notified_at"] = {}
        if str(self.roll_warn_1) not in paper["notified_at"]:
            paper["notified_at"][str(self.roll_warn_1)] = False
        if str(self.roll_warn_2) not in paper["notified_at"]:
            paper["notified_at"][str(self.roll_warn_2)] = False
        self._stats["paper"] = paper
        self._save_stats()

    def _today_bucket(self) -> Dict[str, Any]:
        self._ensure_today_initialized()
        return self._stats["daily"][date.today().isoformat()]

    def _inc_counter(self, bucket: Dict[str, Any], key: str, n: int = 1) -> None:
        bucket[key] = int(bucket.get(key, 0)) + int(n)

    def _inc_template(self, bucket: Dict[str, Any], template_id: str, n: int = 1) -> None:
        tc = bucket.get("template_counts", {})
        tc[template_id] = int(tc.get(template_id, 0)) + int(n)
        bucket["template_counts"] = tc

    def log_event(self, event_type: str, **data) -> None:
        evt = {
            "ts": datetime.now().isoformat(timespec="seconds"),
            "booth_id": self.booth_id,
            "type": event_type,
            "data": data,
        }
        # rotate daily file automatically by date
        self.events_path = self.data_dir / f"events_{date.today().isoformat()}.jsonl"
        with self.events_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(evt) + "\n")

        self._stats["last_event"] = evt
        self._save_stats()

    # ------------------------
    # Telegram
    # ------------------------
    def send_telegram(self, text: str) -> None:
        if not self.telegram_token or not self.telegram_chat_id:
            return
        url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
        payload = {"chat_id": self.telegram_chat_id, "text": text}
        try:
            requests.post(url, json=payload, timeout=6)
        except Exception:
            # Never crash the booth because Telegram failed
            pass

    # ------------------------
    # Public API: call these from your app
    # ------------------------
    def booth_started(self) -> None:
        self._ensure_today_initialized()
        self.send_telegram(f"🎉 Booth started ({self.booth_id})")
        self.log_event("booth_started")

    def session_started(self) -> None:
        self._ensure_today_initialized()
        self._inc_counter(self._stats["lifetime"], "sessions_started", 1)
        self._inc_counter(self._today_bucket(), "sessions_started", 1)
        self._save_stats()
        self.log_event("session_started")

    def session_completed(self) -> None:
        self._ensure_today_initialized()
        self._inc_counter(self._stats["lifetime"], "sessions_completed", 1)
        self._inc_counter(self._today_bucket(), "sessions_completed", 1)
        self._save_stats()
        self.log_event("session_completed")

    def template_chosen(self, template_id: str) -> None:
        self._ensure_today_initialized()
        tid = str(template_id)

        self._inc_template(self._stats["lifetime"], tid, 1)
        self._inc_template(self._today_bucket(), tid, 1)

        self._save_stats()
        self.log_event("template_chosen", template_id=tid)

    def photos_taken(self, n: int = 1) -> None:
        self._ensure_today_initialized()
        n = int(n)
        self._inc_counter(self._stats["lifetime"], "photos_taken", n)
        self._inc_counter(self._today_bucket(), "photos_taken", n)
        self._save_stats()
        self.log_event("photos_taken", n=n)

    def print_result(self, ok: bool, details: str = "") -> None:
        """
        Call this ONCE per print attempt, after you know if it succeeded.
        This updates:
          - lifetime prints_total, prints_success/failed
          - daily prints_total, prints_success/failed
          - paper roll_prints (counts attempts as "prints" — adjust if you want success-only)
          - roll threshold notifications at 143 and 147; reset at 147
        """
        self._ensure_today_initialized()
        details = str(details) if details else ""

        # Print totals (lifetime + today)
        self._inc_counter(self._stats["lifetime"], "prints_total", 1)
        self._inc_counter(self._today_bucket(), "prints_total", 1)

        if ok:
            self._inc_counter(self._stats["lifetime"], "prints_success", 1)
            self._inc_counter(self._today_bucket(), "prints_success", 1)
            self.log_event("print_success", details=details)
        else:
            self._inc_counter(self._stats["lifetime"], "prints_failed", 1)
            self._inc_counter(self._today_bucket(), "prints_failed", 1)
            self.log_event("print_failed", details=details)

        # Paper roll tracking (✅ success-only)
        paper = self._stats.get("paper", {})
        paper.setdefault("roll_prints", 0)
        paper.setdefault("last_reset_at", None)
        paper.setdefault("notified_at", {str(self.roll_warn_1): False, str(self.roll_warn_2): False})

        if ok:
            paper["roll_prints"] = int(paper["roll_prints"]) + 1
            roll_prints = int(paper["roll_prints"])

            # Notify at 143
            if roll_prints >= self.roll_warn_1 and not paper["notified_at"].get(str(self.roll_warn_1), False):
                self.send_telegram(
                    f"🧻 Paper warning ({self.booth_id}): roll_prints = {roll_prints} "
                    f"(threshold {self.roll_warn_1}). Prep a new roll soon."
                )
                paper["notified_at"][str(self.roll_warn_1)] = True
                self.log_event("paper_threshold", threshold=self.roll_warn_1, roll_prints=roll_prints)

            # Notify at 147 + reset roll counter to 0
            if roll_prints >= self.roll_warn_2 and not paper["notified_at"].get(str(self.roll_warn_2), False):
                self.send_telegram(
                    f"🧻 PAPER CHANGE NOW ({self.booth_id}): roll_prints = {roll_prints} "
                    f"(threshold {self.roll_warn_2}). Resetting roll counter to 0."
                )
                paper["notified_at"][str(self.roll_warn_2)] = True
                self.log_event("paper_threshold", threshold=self.roll_warn_2, roll_prints=roll_prints)

                # Reset for next roll
                paper["roll_prints"] = 0
                paper["last_reset_at"] = datetime.now().isoformat(timespec="seconds")
                paper["notified_at"][str(self.roll_warn_1)] = False
                paper["notified_at"][str(self.roll_warn_2)] = False
                self.log_event("paper_roll_reset", last_reset_at=paper["last_reset_at"])

    # ------------------------
    # Summary helpers (optional)
    # ------------------------
    def summary_text(self, include_daily: bool = True) -> str:
        self._ensure_today_initialized()
        today_key = date.today().isoformat()

        life = self._stats.get("lifetime", {})
        today = self._stats.get("daily", {}).get(today_key, {})

        def top_templates(tc: Dict[str, int], limit: int = 8) -> str:
            items = sorted(tc.items(), key=lambda x: x[1], reverse=True)[:limit]
            return "\n".join([f"- {k}: {v}" for k, v in items]) if items else "- (none yet)"

        msg = (
            f"📊 Booth summary ({self.booth_id})\n"
            f"Created: {self._stats.get('created_at')}\n\n"
            f"Lifetime prints: {life.get('prints_total', 0)} "
            f"(✅ {life.get('prints_success', 0)} / ❌ {life.get('prints_failed', 0)})\n"
            f"Lifetime photos: {life.get('photos_taken', 0)}\n"
            f"Lifetime sessions: {life.get('sessions_started', 0)} started / {life.get('sessions_completed', 0)} completed\n\n"
            f"Lifetime templates:\n{top_templates(life.get('template_counts', {}))}\n"
        )

        if include_daily:
            msg += (
                f"\n📅 Today ({today_key})\n"
                f"Prints today: {today.get('prints_total', 0)} "
                f"(✅ {today.get('prints_success', 0)} / ❌ {today.get('prints_failed', 0)})\n"
                f"Photos today: {today.get('photos_taken', 0)}\n"
                f"Sessions today: {today.get('sessions_started', 0)} started / {today.get('sessions_completed', 0)} completed\n\n"
                f"Templates today:\n{top_templates(today.get('template_counts', {}))}\n"
            )

        paper = self._stats.get("paper", {})
        msg += f"\n🧻 Roll prints (current roll): {paper.get('roll_prints', 0)}\n"
        return msg

    # ------------------------
    # Crash + Heartbeat
    # ------------------------
    def install_crash_hook(self) -> None:
        """
        Sends "Crash detected" telegram message with stack trace for uncaught exceptions.
        """
        def excepthook(exc_type, exc, tb):
            trace = "".join(traceback.format_exception(exc_type, exc, tb))

            # Log locally
            self.log_event(
                "crash",
                exc_type=getattr(exc_type, "__name__", str(exc_type)),
                message=str(exc),
                trace=trace,
            )

            # Telegram message (trim to avoid overly long payload)
            trimmed = trace[-3500:]
            msg = (
                f"🚨 Crash detected ({self.booth_id})\n"
                f"{getattr(exc_type, '__name__', exc_type)}: {exc}\n\n"
                f"{trimmed}"
            )
            self.send_telegram(msg)

            # keep default behavior
            sys.__excepthook__(exc_type, exc, tb)

        sys.excepthook = excepthook

    def heartbeat(self, note: str = "", uptime_start: Optional[float] = None) -> None:
        """
        Call periodically (e.g., every 10 minutes).
        """
        uptime = ""
        if uptime_start is not None:
            seconds = int(time.time() - uptime_start)
            uptime = f"\nUptime: {seconds//3600}h {(seconds%3600)//60}m"

        paper = self._stats.get("paper", {})
        roll_prints = paper.get("roll_prints", 0)

        text = f"✅ Booth alive ({self.booth_id}){uptime}\nRoll prints: {roll_prints}"
        if note:
            text += f"\n{note}"

        self.send_telegram(text)
        self.log_event("heartbeat", note=note, roll_prints=roll_prints)

