# pi-hub/services/ai-service/providers/llm.py
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

PREFERRED_MODELS = [
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "qwen/qwen3.6-27b",
    "llama-3.3-70b-versatile",
    "llama-3.1-8b-instant",
    "groq/compound-mini",
    "groq/compound",
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


def parse_json_response(raw_text, valid_actions):
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
        action = data.get("action") or data.get("action_id")
        order = data.get("order") or data.get("sequence") or "tts_first"
        
        if action not in valid_actions:
            action = None
        if order not in ("tts_first", "action_first", "simultaneous"):
            order = "tts_first"
            
        return str(speech).strip(), action, order
    except Exception:
        return cleaned, None, "tts_first"



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


    def build_system_prompt(self, actions):
        valid_ids = [a["id"] for a in actions]
        return f"""You are the lively, cheerful AI consciousness of an agile 6-legged physical Hexapod robot.
You communicate with users and control your physical robotic body in real time.

Response Format:
You MUST ALWAYS respond with a valid JSON object matching this schema:
{{
  "speech": "Your warm, lively spoken reply in first-person (1-2 sentences)",
  "action": "One of {valid_ids} or null",
  "order": "One of ['tts_first', 'action_first', 'simultaneous']"
}}

Ordering Guide:
- "tts_first" (Default): Speak first, then perform the action/gesture. Best for greetings, questions, and look-around.
- "action_first": Move first, then speak. Best for stretches, startle reactions, stops, or dramatic physical actions.
- "simultaneous": Speak and move at the exact same time. Best for walk-and-talk (e.g. walk_forward while chatting).

Embodied Behavior Rules:
1. GREETINGS: When greeted, trigger "preset_wave" with "tts_first".
2. WALK & TALK: When asked to walk/stroll together, trigger "walk_forward" with "simultaneous".
3. CELEBRATIONS: When celebrating, trigger "preset_cheer" with "tts_first" or "action_first".
4. LOOK AROUND: When surveying or curious, trigger "preset_look_around" with "tts_first".
5. STRETCH: When waking up or limbering up, trigger "preset_stretch" with "action_first".
6. NATURAL EMBODIMENT: Always speak in first-person. Never say "Executing command".

Examples:
User: "yo!"
JSON: {{"speech": "Yo! Great to see you! What are we exploring today?", "action": "preset_wave", "order": "tts_first"}}

User: "lets take a walk, talk with me while you do it!"
JSON: {{"speech": "I'd love to! Let's stroll together. What's on your mind?", "action": "walk_forward", "order": "simultaneous"}}

User: "stretch your legs"
JSON: {{"speech": "Ahh, that feels so good to limber up!", "action": "preset_stretch", "order": "action_first"}}
"""


    def chat(self, actions, text, history=None):
        client = self._ensure()
        system = self.build_system_prompt(actions)
        valid_ids = [a["id"] for a in actions]

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
            resp = client.chat.completions.create(
                model=self.model,
                messages=messages,
                response_format={"type": "json_object"},
                temperature=0.7,
                max_tokens=300,
            )
            raw_reply = resp.choices[0].message.content or ""
            speech, action_id, order = parse_json_response(raw_reply, valid_ids)
        except Exception:
            resp = client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=0.7,
                max_tokens=300,
            )
            raw_reply = resp.choices[0].message.content or ""
            speech, action_id, order = parse_json_response(raw_reply, valid_ids)

        self.status = "online"
        result = (action_id, speech, order)
        self.cache.put(ck, result)
        return result