# services/ai-service/memory_manager.py
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger("ai.memory")

DEFAULT_STORAGE_PATH = "/opt/hexapod-ai/memory_pool.json"


class MemoryMode:
    EPHEMERAL = "ephemeral"      # Single turn, no history retained
    SESSION = "session"          # Rolling in-memory history (resets after inactivity)
    PERSISTENT = "persistent"    # Session history + permanent Key-Value Memory Pool on disk


class MemoryManager:
    def __init__(
        self,
        storage_path: str = DEFAULT_STORAGE_PATH,
        mode: str = MemoryMode.SESSION,
        max_turns: int = 16,
        timeout_s: float = 900.0,
    ):
        self.storage_path = storage_path
        self.mode = mode if mode in (MemoryMode.EPHEMERAL, MemoryMode.SESSION, MemoryMode.PERSISTENT) else MemoryMode.SESSION
        self.max_turns = max_turns
        self.timeout_s = timeout_s

        self.session_history: List[Dict[str, str]] = []
        self.memory_pool: Dict[str, Any] = {}
        self.recent_actions: List[str] = []
        self.last_visual_summary: str = ""
        self.last_activity = time.time()
        self._lock = threading.Lock()

        self._load_from_disk()

    def record_action(self, action_id: str):
        """Tracks recent physical actions to prevent repetitive behavior loops."""
        with self._lock:
            clean = str(action_id).strip().lower()
            if clean and clean not in ("stop", "pose"):
                self.recent_actions.append(clean)
                if len(self.recent_actions) > 6:
                    self.recent_actions.pop(0)

    def set_visual_summary(self, summary: str):
        """Caches the latest visual perception summary to reduce redundant VLM queries."""
        with self._lock:
            self.last_visual_summary = (summary or "").strip()

    def get_dst_prompt_block(self) -> str:
        """Constructs an active Dialogue State Tracking block for prompt injection."""
        with self._lock:
            lines = []
            if self.recent_actions:
                recent_str = ", ".join(self.recent_actions[-4:])
                lines.append(f"- Recently Executed Actions: [{recent_str}] (Pick a DIFFERENT action on open-ended requests)")
            if self.last_visual_summary:
                lines.append(f"- Last Visual Observation: \"{self.last_visual_summary}\"")
            if not lines:
                return ""
            return "\n### ACTIVE DIALOGUE STATE & RECENT ACTION HISTORY:\n" + "\n".join(lines) + "\n"

    def set_mode(self, mode: str):
        with self._lock:
            if mode in (MemoryMode.EPHEMERAL, MemoryMode.SESSION, MemoryMode.PERSISTENT):
                self.mode = mode
                log.info("Memory Manager mode changed -> %s", self.mode)
                if self.mode == MemoryMode.EPHEMERAL:
                    self.session_history.clear()

    def add_user_message(self, text: str):
        with self._lock:
            self._check_timeout()
            if self.mode == MemoryMode.EPHEMERAL:
                self.session_history.clear()
                return

            clean = re.sub(r'^[🎤📸🔍\s"\'״]+|[״"\'\s]+$', '', text).strip()
            if clean:
                self.session_history.append({"role": "user", "content": clean})
                self._trim()
            self.last_activity = time.time()

    def add_user(self, text: str):
        """Alias for add_user_message."""
        self.add_user_message(text)

    def add_assistant_message(self, text: str):
        with self._lock:
            self._check_timeout()
            if self.mode == MemoryMode.EPHEMERAL:
                self.session_history.clear()
                return

            clean = text.strip()
            if clean:
                self.session_history.append({"role": "assistant", "content": clean})
                self._trim()
            self.last_activity = time.time()

    def add_assistant(self, text: str):
        """Alias for add_assistant_message."""
        self.add_assistant_message(text)

    def get_context_history(self) -> List[Dict[str, str]]:
        with self._lock:
            self._check_timeout()
            if self.mode == MemoryMode.EPHEMERAL:
                return []
            return list(self.session_history)

    def get_history(self) -> List[Dict[str, str]]:
        """Alias for get_context_history."""
        return self.get_context_history()

    def set_fact(self, key: str, value: Any):
        """Sets a persistent fact in the long-term memory pool."""
        with self._lock:
            k = re.sub(r"[^\w\s_-]", "", str(key)).strip().lower()
            if k:
                self.memory_pool[k] = value
                self._save_to_disk()
                log.info("Memory Pool updated: %s = %s", k, value)

    def delete_fact(self, key: str):
        """Removes a fact from the long-term memory pool."""
        with self._lock:
            k = str(key).strip().lower()
            if k in self.memory_pool:
                del self.memory_pool[k]
                self._save_to_disk()
                log.info("Memory Pool deleted key: %s", k)

    def clear_session(self):
        """Clears active conversation turns (keeps long-term memory pool)."""
        with self._lock:
            self.session_history.clear()
            self.last_activity = time.time()
            log.info("Session history cleared.")

    def clear_all(self):
        """Clears both session history and persistent memory pool."""
        with self._lock:
            self.session_history.clear()
            self.memory_pool.clear()
            self._save_to_disk()
            self.last_activity = time.time()
            log.info("All session history and long-term memory pool wiped.")

    def get_memory_pool_prompt_block(self) -> str:
        """Formats the persistent memory pool into system instructions."""
        with self._lock:
            if not self.memory_pool or self.mode == MemoryMode.EPHEMERAL:
                return ""
            facts = "\n".join(f"- {k}: {v}" for k, v in self.memory_pool.items())
            return f"\n### LEARNED FACTS & PERSISTENT MEMORY POOL:\n{facts}\n"

    def get_state_dict(self) -> Dict[str, Any]:
        """Returns full state for Web-UI synchronization."""
        with self._lock:
            return {
                "mode": self.mode,
                "turns_count": len(self.session_history),
                "pool_count": len(self.memory_pool),
                "memory_pool": dict(self.memory_pool),
                "last_activity_ts": int(self.last_activity * 1000),
            }

    def _check_timeout(self):
        if (time.time() - self.last_activity) > self.timeout_s:
            if self.session_history:
                log.info("Session inactive for >%ds — resetting working turns.", self.timeout_s)
                self.session_history.clear()

    def _trim(self):
        if len(self.session_history) > self.max_turns:
            self.session_history = self.session_history[-self.max_turns:]

    def _save_to_disk(self):
        try:
            os.makedirs(os.path.dirname(self.storage_path), exist_ok=True)
            with open(self.storage_path, "w", encoding="utf-8") as f:
                json.dump(self.memory_pool, f, indent=2)
        except Exception as e:
            log.warning("Failed to save memory pool to %s: %s", self.storage_path, e)

    def _load_from_disk(self):
        if os.path.exists(self.storage_path):
            try:
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    self.memory_pool = json.load(f)
                log.info("Loaded %d persistent facts from %s", len(self.memory_pool), self.storage_path)
            except Exception as e:
                log.warning("Could not read %s: %s", self.storage_path, e)