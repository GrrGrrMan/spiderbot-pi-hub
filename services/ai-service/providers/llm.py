# pi-hub/services/ai-service/providers/llm.py
# Remote LLM via an OpenAI-compatible endpoint (default: Groq free tier).
# Lazy import of the `openai` SDK + on-demand key read from /etc/hexapod-ai/groq.key.
import hashlib
import json
import logging
import os
import threading
import time

log = logging.getLogger("ai.llm")

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "llama-3.3-70b-versatile"
DEFAULT_KEY_FILE = "/etc/hexapod-ai/groq.key"
# Hard cap on how many prior messages get sent to the LLM per call.
# The web-ui caps persisted history at MAX_PERSISTED_MESSAGES=200 (sessionStorage);
# 50 short chat turns fit comfortably within llama-3.3-70b's 128k context
# while preventing a malicious or pathological payload from exhausting it.
MAX_LLM_HISTORY = 50

# --- Response cache (2026-08-18) ----------------------------------------------
# TTL+LRU cache of (action_id, reply) pairs keyed on the request that produced
# them. Catches the "user repeated themselves" / "same turn after page reload"
# cases without burning a Groq round-trip. Stage-1 keyword matching in
# pipeline.decide() already short-circuits ~80% of phrases for free; this
# only matters for the LLM-bound ~20%.
#
# Default TTL 60s: long enough that back-to-back repeats hit, short enough
# that a rephrased similar request after 90s still goes to the LLM. Tunable
# via AI_LLM_CACHE_TTL / AI_LLM_CACHE_MAXSIZE env (in case it turns out to
# help or hurt; cheap to experiment with).
DEFAULT_CACHE_TTL = int(os.environ.get("AI_LLM_CACHE_TTL", "60"))
DEFAULT_CACHE_MAX = int(os.environ.get("AI_LLM_CACHE_MAXSIZE", "256"))


class _TTLCache:
    """Stdlib-only LRU+TTL cache. Bounded; expired entries are evicted on get().
    Thread-safe via a single mutex (the LLM call path is already serialized by
    pipeline._busy, so contention here is negligible).
    """

    __slots__ = ("_max", "_ttl", "_data", "_lock", "hits", "misses", "evictions")

    def __init__(self, maxsize=DEFAULT_CACHE_MAX, ttl=DEFAULT_CACHE_TTL):
        self._max = max(1, int(maxsize))
        self._ttl = max(0, int(ttl))
        self._data = {}                # key -> (value, expires_at)
        self._lock = threading.Lock()
        self.hits = 0
        self.misses = 0
        self.evictions = 0

    def get(self, key):
        now = time.time()
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self.misses += 1
                return None
            value, expires = entry
            if expires < now:
                self._data.pop(key, None)
                self.misses += 1
                return None
            # LRU touch
            self._data[key] = (value, expires)
            self.hits += 1
            return value

    def put(self, key, value):
        now = time.time()
        with self._lock:
            # Drop expired first so the new entry doesn't immediately get evicted.
            for k in [k for k, (_, e) in self._data.items() if e < now]:
                self._data.pop(k, None)
            self._data[key] = (value, now + self._ttl)
            if len(self._data) > self._max:
                # FIFO eviction: first inserted key. Good enough for a cache
                # whose TTL is much smaller than the access cadence.
                first = next(iter(self._data))
                if first != key:
                    self._data.pop(first, None)
                    self.evictions += 1

    def stats(self):
        with self._lock:
            return {
                "size": len(self._data),
                "max": self._max,
                "ttl": self._ttl,
                "hits": self.hits,
                "misses": self.misses,
                "evictions": self.evictions,
            }

    def clear(self):
        with self._lock:
            self._data.clear()


def _cache_key(text, history, system):
    """Hash on (text, last-2-history-turns, system). Last-2-only because the
    full LLM context is the system prompt + last few turns; a repeat 30s later
    with the same preceding turn is a cache hit by intent. Using the last 2
    turns also prevents the key from growing with chat length.
    """
    h = hashlib.sha256()
    h.update((system or "").encode("utf-8"))
    h.update(b"\x00")
    h.update((text or "").encode("utf-8"))
    h.update(b"\x00")
    for turn in (history or [])[-2:]:
        h.update(turn.get("role", "user").encode("utf-8"))
        h.update(b"\x00")
        h.update((turn.get("content", "") or "").encode("utf-8"))
        h.update(b"\x00")
    return h.hexdigest()[:32]


def read_key(key_file=DEFAULT_KEY_FILE):
    """Read the Groq API key file (chmod 600). Returns None if absent."""
    try:
        with open(key_file, "r") as f:
            key = f.read().strip()
        return key or None
    except OSError:
        return None


class LLMClient:
    def __init__(self, base_url=DEFAULT_BASE_URL, model=DEFAULT_MODEL, key_file=DEFAULT_KEY_FILE,
                 cache_ttl=DEFAULT_CACHE_TTL, cache_maxsize=DEFAULT_CACHE_MAX):
        self.base_url = base_url
        self.model = model
        self.key_file = key_file
        self._client = None
        self._lock = threading.Lock()
        self.status = "unknown"      # unknown|online|offline — flips online after a successful call
        self.last_error = None
        self.cache = _TTLCache(maxsize=cache_maxsize, ttl=cache_ttl)

    def is_available(self):
        return self.status == "online" and self._client is not None

    def cache_stats(self):
        return self.cache.stats()

    def _ensure(self):
        if self._client is not None:
            return self._client
        key = read_key(self.key_file)
        if not key:
            self.status = "offline"
            self.last_error = "no key at %s" % self.key_file
            raise RuntimeError(self.last_error)
        try:
            from openai import OpenAI
        except ImportError as e:
            self.status = "offline"
            self.last_error = "openai lib missing: %s" % e
            raise
        self._client = OpenAI(api_key=key, base_url=self.base_url)
        return self._client

    def chat(self, actions, text, history=None, system=None):
        """Ask the LLM to map `text` to an action_id (+ short reply).

        Returns (action_id|None, reply_text).
        Raises on network/auth/parse errors (pipeline falls back to canned).
        """
        client = self._ensure()
        messages = [{"role": "system", "content": system or "You are a helpful robot assistant."}]
        # Full conversation memory (2026-08-17): include every prior message
        # the caller passes. The web-ui caps persisted history at
        # MAX_PERSISTED_MESSAGES=200, and we trim here to MAX_LLM_HISTORY
        # so a malicious/large payload can't blow Groq's request token limit
        # (llama-3.3-70b context is 128k tokens; 50 short chat turns easily fits).
        for h in (history or [])[-MAX_LLM_HISTORY:]:
            role = h.get("role", "user")
            if role not in ("user", "assistant", "system"):
                role = "user"
            messages.append({"role": role, "content": h.get("content", "")})
        messages.append({"role": "user", "content": text})

        # Cache hit? Return immediately. The key is hash(text + last 2 history
        # turns + system), so a "tell me a fun fact" repeated 10s later (with
        # the same preceding turn) hits, but a rephrased follow-up 90s later
        # misses and goes to the LLM. (2026-08-18)
        ck = _cache_key(text, history, system)
        cached = self.cache.get(ck)
        if cached is not None:
            log.info("LLM cache hit (size=%d)", self.cache.stats()["size"])
            return cached
        log.debug("LLM cache miss (size=%d)", self.cache.stats()["size"])

        from action_parser import llm_tool_schema
        resp = client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=[llm_tool_schema(actions)],
            tool_choice="auto",
            max_tokens=256,
            temperature=0.3,
        )
        self.status = "online"   # any successful round-trip proves the key/endpoint
        msg = resp.choices[0].message
        action_id = None
        reply = (msg.content or "").strip()
        if getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                    action_id = args.get("action_id") or action_id
                except (ValueError, AttributeError):
                    continue
        if not reply and action_id:
            reply = None   # caller uses the action's canned reply

        # Populate the cache only on a clean, parseable response. Caching an
        # error fallback would make the next identical request fail the same
        # way instead of being retried. (2026-08-18)
        result = (action_id, reply)
        self.cache.put(ck, result)
        return result