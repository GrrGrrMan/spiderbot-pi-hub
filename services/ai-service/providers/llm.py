# pi-hub/services/ai-service/providers/llm.py
# Remote LLM via an OpenAI-compatible endpoint (default: Groq free tier).
# Lazy import of the `openai` SDK + on-demand key read from /etc/hexapod-ai/groq.key.
import json
import logging
import os
import threading

log = logging.getLogger("ai.llm")

DEFAULT_BASE_URL = "https://api.groq.com/openai/v1"
DEFAULT_MODEL = "llama-3.3-70b-versatile"
DEFAULT_KEY_FILE = "/etc/hexapod-ai/groq.key"
# Hard cap on how many prior messages get sent to the LLM per call.
# The web-ui caps persisted history at MAX_PERSISTED_MESSAGES=200 (sessionStorage);
# 50 short chat turns fit comfortably within llama-3.3-70b's 128k context
# while preventing a malicious or pathological payload from exhausting it.
MAX_LLM_HISTORY = 50


def read_key(key_file=DEFAULT_KEY_FILE):
    """Read the Groq API key file (chmod 600). Returns None if absent."""
    try:
        with open(key_file, "r") as f:
            key = f.read().strip()
        return key or None
    except OSError:
        return None


class LLMClient:
    def __init__(self, base_url=DEFAULT_BASE_URL, model=DEFAULT_MODEL, key_file=DEFAULT_KEY_FILE):
        self.base_url = base_url
        self.model = model
        self.key_file = key_file
        self._client = None
        self._lock = threading.Lock()
        self.status = "unknown"      # unknown|online|offline — flips online after a successful call
        self.last_error = None

    def is_available(self):
        return self.status == "online" and self._client is not None

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
        return action_id, reply