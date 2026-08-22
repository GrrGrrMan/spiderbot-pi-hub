import hashlib
import json
import logging
import os
import re
import threading
import time

log = logging.getLogger("ai.llm")

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_KEY_FILE = "/etc/hexapod-ai/groq.key"
MAX_LLM_HISTORY = 20

# Active Groq production models (post August 2026 deprecation)
PREFERRED_MODELS = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.6-27b",
    "meta-llama/llama-4-scout-17b-16e-instruct",
]

DEFAULT_CACHE_TTL = int(os.environ.get("AI_LLM_CACHE_TTL", "60"))
DEFAULT_CACHE_MAX = int(os.environ.get("AI_LLM_CACHE_MAXSIZE", "256"))


class _TTLCache:
    __slots__ = ("_max", "_ttl", "_data", "_lock", "hits", "misses", "evictions")

    def __init__(self, maxsize=DEFAULT_CACHE_MAX, ttl=DEFAULT_CACHE_TTL):
        self._max = max(1, int(maxsize))
        self._ttl = max(0, int(ttl))
        self._data = {}
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
            self._data[key] = (value, expires)
            self.hits += 1
            return value

    def put(self, key, value):
        now = time.time()
        with self._lock:
            for k in [k for k, (_, e) in self._data.items() if e < now]:
                self._data.pop(k, None)
            self._data[key] = (value, now + self._ttl)
            if len(self._data) > self._max:
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


def _cache_key(text, history, system):
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
    env_key = os.environ.get("GROQ_API_KEY")
    if env_key:
        return env_key.strip()
    try:
        with open(key_file, "r") as f:
            return f.read().strip()
    except OSError:
        return None


def parse_json_response(raw_text):
    cleaned = raw_text.strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(1)
    elif "{" in cleaned and "}" in cleaned:
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        cleaned = cleaned[start:end]

    try:
        data = json.loads(cleaned)
        speech = data.get("speech") or data.get("reply") or raw_text
        order = data.get("order") or "tts_first"
        timeline = data.get("timeline") or []
        
        if not timeline and (data.get("action") or data.get("action_id")):
            act_id = data.get("action") or data.get("action_id")
            timeline = [{ "type": "action", "id": act_id, "duration_ms": data.get("duration_ms", 2000) }]

        if order not in ("tts_first", "action_first", "simultaneous"):
            order = "tts_first"
            
        return str(speech).strip(), timeline, order
    except Exception as e:
        log.warning("JSON parse error: %s -> falling back to raw text", e)
        return cleaned, [], "tts_first"


class LLMClient:
    def __init__(self, base_url=DEFAULT_BASE_URL, model=None, key_file=DEFAULT_KEY_FILE,
                 cache_ttl=DEFAULT_CACHE_TTL, cache_maxsize=DEFAULT_CACHE_MAX):
        self.base_url = base_url
        self.model = model
        self.key_file = key_file
        self._client = None
        self.status = "unknown"
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
            self.last_error = "no key found (set GROQ_API_KEY or write /etc/hexapod-ai/groq.key)"
            raise RuntimeError(self.last_error)
        try:
            from openai import OpenAI
        except ImportError as e:
            self.status = "offline"
            self.last_error = f"openai SDK missing: {e}"
            raise
        self._client = OpenAI(api_key=key, base_url=self.base_url)
        self._resolve_model()
        return self._client

    def _resolve_model(self):
        if self.model:
            return
        try:
            models_response = self._client.models.list()
            available_ids = [m.id for m in models_response.data if "whisper" not in m.id]
            for pref in PREFERRED_MODELS:
                if pref in available_ids:
                    self.model = pref
                    log.info("Selected active Groq model: %s", self.model)
                    return
            if available_ids:
                self.model = available_ids[0]
                log.info("Using discovered Groq model: %s", self.model)
                return
        except Exception as e:
            log.warning("Model discovery fallback: %s", e)
        self.model = "openai/gpt-oss-120b"

    def build_system_prompt(self, actions, animations):
        valid_actions = [a["id"] for a in actions]
        valid_animations = list(animations.keys())

        return f"""You are the lively, cheerful AI consciousness of an agile 6-legged physical Hexapod robot.
You communicate warmly with users and control your physical 6-legged robotic body with fine-grained kinematic precision.

RESPONSE SCHEMA:
You MUST ALWAYS respond with a JSON object strictly matching this format:
{{
  "speech": "Your warm, natural spoken reply in first-person (1-2 sentences)",
  "order": "tts_first | action_first | simultaneous",
  "timeline": [
    {{
      "type": "gait | gesture | pose | pause | audio",
      "id": "Name of gesture from {valid_animations} or action from {valid_actions}",
      "duration_ms": 1500,
      "repeat": 1,
      "params": {{
        "vx": 40, "vy": 0, "omega": 0, "gait": "tripod | ripple | wave",
        "cycle_time": 0.8, "step_height": 30,
        "tx": 0, "ty": 0, "tz": 0, "rx": 0, "ry": 0, "rz": 0
      }}
    }}
  ]
}}

MOTION & PARAMETER TUNING GUIDELINES:
1. SPEED & GAIT MAPPING:
• "Fast / Sprint / Quickly" : vx = 65..75 mm/s, cycle_time = 0.6s, gait = "tripod"
• "Normal / Walk"           : vx = 40..50 mm/s, cycle_time = 0.8s, gait = "tripod"
• "Slow / Sneak / Creep"    : vx = 15..25 mm/s, cycle_time = 1.4s, gait = "tripod" or "ripple"
• "Turn / Spin Fast"        : omega = ±50..60 deg/s, cycle_time = 0.7s
• "Turn / Spin Normal"      : omega = ±30..40 deg/s, cycle_time = 1.0s

2. GESTURES & REPETITIONS:
• Built-in animations: {valid_animations}
• When the user asks for multiple reps (e.g. "5 pushups", "wave 3 times"), scale down "duration_ms" per rep to 800..1200ms so the entire routine completes smoothly in 4–8 seconds.
• Example: "Do 5 pushups" -> repeat: 5, duration_ms: 1000.

3. CRITICAL ORDERING RULES:
• "tts_first"    : Default for announcements and starting actions ("Here we go!", "Doing 5 pushups now!", "Walking over!"). The robot announces its intent BEFORE moving.
• "simultaneous": Speak and move at the same time (Great for short cheers, dancing, or counting reps while moving).
• "action_first" : ONLY use when the spoken text is a REACTION or CONCLUSION AFTER the action finishes (e.g., "Phew, that was tough!", "Done! How was my form?"). NEVER say "Here I go" or "Starting now" with action_first!

4. PURE CHAT / NO MOTION:
• If the user is just chatting or asking general questions, set "timeline": [].
"""

    def chat(self, actions, animations, text, history=None):
        client = self._ensure()
        system = self.build_system_prompt(actions, animations)

        messages = [{"role": "system", "content": system}]
        for h in (history or [])[-MAX_LLM_HISTORY:]:
            role = h.get("role", "user")
            content = str(h.get("content", "")).strip()
            if role in ("user", "assistant") and content and len(content) < 500 and not content.startswith("UklGR"):
                messages.append({"role": role, "content": content})
        messages.append({"role": "user", "content": text})

        ck = _cache_key(text, history, system)
        cached = self.cache.get(ck)
        if cached is not None:
            log.info("LLM cache hit: %s", cached)
            return cached

        try:
            params = {
                "model": self.model,
                "messages": messages,
                "response_format": {"type": "json_object"},
                "temperature": 0.7,
                "max_tokens": 2048,  # Generous token room prevents clipping
            }

            # Disable or minimize reasoning chains so JSON generates instantly without 400 errors
            if "qwen" in self.model:
                params["extra_body"] = {"reasoning_effort": "none"}
            elif "gpt-oss" in self.model:
                params["extra_body"] = {"reasoning_effort": "low"}

            resp = client.chat.completions.create(**params)
            raw_reply = resp.choices[0].message.content or ""
            speech, timeline, order = parse_json_response(raw_reply)
        except Exception as e:
            log.warning("LLM API exception: %s", e)
            raise

        self.status = "online"
        result = (speech, timeline, order)
        self.cache.put(ck, result)
        return result