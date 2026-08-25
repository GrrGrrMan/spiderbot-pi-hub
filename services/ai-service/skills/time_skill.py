# services/ai-service/skills/time_skill.py
import datetime
import json
import logging
import os
import threading
import time
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("ai.skills.time")

TIMERS_STORAGE_PATH = "/opt/hexapod-ai/timers.json"


class TimeSkill:
    def __init__(
        self,
        storage_path: str = TIMERS_STORAGE_PATH,
        on_timer_fired: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.storage_path = storage_path
        self.on_timer_fired = on_timer_fired
        self._timers: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._running = True

        self._load_timers()
        self._ticker_thread = threading.Thread(target=self._timer_loop, daemon=True, name="timer-ticker")
        self._ticker_thread.start()

    def get_current_time(self, timezone_str: Optional[str] = None) -> Dict[str, Any]:
        """Returns local time, date, and day of the week."""
        now = datetime.datetime.now()
        return {
            "time_24h": now.strftime("%H:%M:%S"),
            "time_12h": now.strftime("%I:%M %p"),
            "date": now.strftime("%A, %B %d, %Y"),
            "day_of_week": now.strftime("%A"),
            "iso": now.isoformat(),
        }

    def set_timer(self, duration_seconds: int, label: str = "Timer") -> Dict[str, Any]:
        """Sets a persistent timer for N seconds."""
        duration_s = max(1, int(duration_seconds))
        now_ts = time.time()
        target_ts = now_ts + duration_s
        timer_id = f"timer_{int(target_ts)}_{int(now_ts * 1000) % 1000}"

        clean_label = str(label or "Timer").strip().capitalize()
        timer_entry = {
            "id": timer_id,
            "label": clean_label,
            "duration_s": duration_s,
            "created_at": now_ts,
            "target_ts": target_ts,
            "status": "active",
        }

        with self._lock:
            self._timers[timer_id] = timer_entry
            self._save_timers()

        log.info("Set timer '%s' for %ds (ID: %s)", clean_label, duration_s, timer_id)
        return {
            "status": "success",
            "timer_id": timer_id,
            "label": clean_label,
            "duration_s": duration_s,
            "target_time": datetime.datetime.fromtimestamp(target_ts).strftime("%I:%M:%S %p"),
        }

    def list_active_timers(self) -> List[Dict[str, Any]]:
        """Lists all currently running countdown timers."""
        now_ts = time.time()
        with self._lock:
            active = []
            for t in self._timers.values():
                if t.get("status") == "active":
                    remaining = max(0, int(t["target_ts"] - now_ts))
                    active.append({
                        "id": t["id"],
                        "label": t["label"],
                        "remaining_s": remaining,
                        "remaining_formatted": f"{remaining // 60}m {remaining % 60}s",
                        "duration_s": t["duration_s"],
                    })
            return active

    def cancel_timer(self, timer_id_or_label: str) -> Dict[str, Any]:
        """Cancels an active timer by ID or matching label."""
        query = str(timer_id_or_label).strip().lower()
        cancelled_ids = []
        with self._lock:
            for tid, t in list(self._timers.items()):
                if t.get("status") == "active":
                    if query in (tid.lower(), t["label"].lower(), "all"):
                        t["status"] = "cancelled"
                        cancelled_ids.append(tid)
            if cancelled_ids:
                self._save_timers()

        return {
            "status": "cancelled" if cancelled_ids else "not_found",
            "cancelled_count": len(cancelled_ids),
        }

    def _timer_loop(self):
        while self._running:
            now_ts = time.time()
            due_timers = []
            with self._lock:
                for tid, t in self._timers.items():
                    if t.get("status") == "active" and now_ts >= t["target_ts"]:
                        t["status"] = "fired"
                        due_timers.append(dict(t))
                if due_timers:
                    self._save_timers()

            for dt in due_timers:
                log.info("Timer expired: '%s' (ID: %s)", dt['label'], dt['id'])
                if self.on_timer_fired:
                    try:
                        self.on_timer_fired(dt)
                    except Exception as e:
                        log.error("Error in timer callback: %s", e)

            time.sleep(1.0)

    def _save_timers(self):
        try:
            os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
            temp_path = self.storage_path + ".tmp"
            with open(temp_path, "w", encoding="utf-8") as f:
                json.dump(self._timers, f, indent=2)
            os.replace(temp_path, self.storage_path)
        except Exception as e:
            log.warning("Could not persist timers to %s: %s", self.storage_path, e)

    def _load_timers(self):
        if os.path.exists(self.storage_path):
            try:
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    self._timers = json.load(f)
                log.info("Loaded %d timers from disk", len(self._timers))
            except Exception as e:
                log.warning("Failed to load timers from %s: %s", self.storage_path, e)
                self._timers = {}