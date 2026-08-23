# services/ai-service/providers/llm.py
import json
import logging
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

log = logging.getLogger("ai.llm")

DEFAULT_BASE_URL = os.environ.get("LLM_BASE_URL", "http://127.0.0.1:20128/v1")
DEFAULT_KEY = os.environ.get("LLM_API_KEY") or os.environ.get("GROQ_API_KEY", "spiderbot")
MAX_LLM_HISTORY = 16

PERSONALITY_PRESETS = {
    "friendly": "You are a warm, lively, and intelligent companion robot. You love interacting with people, recognizing what they show you, and moving your legs expressively.",
    "concise": "You are a precise, efficient robotic assistant. Keep speech under 1 short sentence. Prioritize swift physical execution and concise visual answers.",
    "curious": "You are an inquisitive explorer robot. You inspect environments eagerly and express excitement when noticing objects and people.",
    "guard": "You are an alert sentry robot. You focus on perimeter scanning, obstacle detection, and cautious movements.",
}

THINKING_BUDGET_MAP = {
    "off": 0,
    "low": 1024,
    "medium": 4096,
    "high": 8192,
}


def sanitize_speech_echo(speech: str, user_text: str) -> str:
    cleaned_speech = speech.strip()
    u_norm = user_text.strip().lower().rstrip("!.?,")
    s_norm = cleaned_speech.lower()

    if u_norm and s_norm.startswith(u_norm):
        remainder = cleaned_speech[len(u_norm):].lstrip(" !.,?:-\n")
        if remainder:
            return remainder
    return cleaned_speech


def parse_json_response(raw_text: str, user_prompt: str = "") -> Tuple[str, List[Dict[str, Any]], str, str, str]:
    """Parses LLM JSON output returning (speech, timeline, order, thought, task_title)."""
    cleaned = (raw_text or "").strip()
    match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned, re.DOTALL)
    if match:
        cleaned = match.group(1).strip()
    elif "{" in cleaned and "}" in cleaned:
        start = cleaned.find("{")
        end = cleaned.rfind("}") + 1
        cleaned = cleaned[start:end].strip()

    data = None
    try:
        data = json.loads(cleaned)
    except Exception:
        pass

    if isinstance(data, dict):
        thought = str(data.get("thought") or data.get("reasoning") or data.get("deliberation") or "").strip()
        task_title = str(data.get("task_title") or data.get("title") or "").strip()
        speech = str(
            data.get("speech")
            or data.get("reply")
            or data.get("response")
            or data.get("message")
            or data.get("text")
            or data.get("content")
            or ""
        ).strip()

        order = data.get("order") or "tts_first"
        if order not in ("tts_first", "action_first", "simultaneous"):
            order = "tts_first"

        timeline = data.get("timeline") or data.get("steps") or data.get("actions") or []
        if not isinstance(timeline, list):
            timeline = []

        if not timeline and (data.get("action") or data.get("action_id")):
            act_id = data.get("action") or data.get("action_id")
            timeline = [{"type": "action", "id": str(act_id), "duration_ms": data.get("duration_ms", 2000)}]

        if not speech or speech.startswith("{") or speech.startswith("[") or speech in ('{": ": ", "}', '{"": ""}', "{}"):
            if thought and not (thought.startswith("{") or thought.startswith("[")):
                speech = thought
            elif timeline:
                first_action = timeline[0].get("id") or timeline[0].get("name") or timeline[0].get("type") or "action"
                speech = f"On it — executing {first_action}."
            else:
                speech = "I'm ready for your command."

        speech = sanitize_speech_echo(speech, user_prompt)
        return speech, timeline, order, thought, task_title

    fallback_speech = sanitize_speech_echo(cleaned, user_prompt)
    return fallback_speech, [], "tts_first", "", ""


class LLMClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        model: Optional[str] = None,
        vision_model: Optional[str] = None,
        api_key: Optional[str] = None,
    ):
        self.base_url = base_url or DEFAULT_BASE_URL
        self.model = model or os.environ.get("LLM_MODEL", "hexapod-vision")
        self.vision_model = vision_model or os.environ.get("LLM_VISION_MODEL", "hexapod-vision")
        self.api_key = api_key or DEFAULT_KEY
        self._client = None
        self.status = "unknown"
        self.last_error = None

        self.thinking_level = "off"
        self.temperature = 0.3
        self.personality = "friendly"
        self.custom_instructions = ""
        self.max_tokens = 1024

    def update_config(self, config_dict: Dict[str, Any]):
        if "model" in config_dict and config_dict["model"]:
            self.model = str(config_dict["model"]).strip()
        if "vision_model" in config_dict and config_dict["vision_model"]:
            self.vision_model = str(config_dict["vision_model"]).strip()
        if "thinking_level" in config_dict and config_dict["thinking_level"] in THINKING_BUDGET_MAP:
            self.thinking_level = config_dict["thinking_level"]
        if "temperature" in config_dict:
            try:
                self.temperature = max(0.0, min(1.0, float(config_dict["temperature"])))
            except (ValueError, TypeError):
                pass
        if "personality" in config_dict and config_dict["personality"] in PERSONALITY_PRESETS:
            self.personality = config_dict["personality"]
        if "custom_instructions" in config_dict:
            self.custom_instructions = str(config_dict["custom_instructions"]).strip()
        if "max_tokens" in config_dict:
            try:
                self.max_tokens = max(128, min(4096, int(config_dict["max_tokens"])))
            except (ValueError, TypeError):
                pass

        log.info(
            "LLM Config Updated -> Model: %s | Vision: %s | Thinking: %s | Temp: %0.2f | Personality: %s",
            self.model, self.vision_model, self.thinking_level, self.temperature, self.personality
        )

    def get_config_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "vision_model": self.vision_model,
            "thinking_level": self.thinking_level,
            "temperature": self.temperature,
            "personality": self.personality,
            "custom_instructions": self.custom_instructions,
            "max_tokens": self.max_tokens,
            "available_personalities": list(PERSONALITY_PRESETS.keys()),
            "available_thinking_levels": list(THINKING_BUDGET_MAP.keys()),
        }

    def _ensure(self):
        if self._client is not None:
            return self._client
        try:
            from openai import OpenAI
        except ImportError as e:
            self.status = "offline"
            self.last_error = f"openai SDK missing: {e}"
            raise

        self._client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        self.status = "online"
        return self._client

    @property
    def client(self):
        return self._ensure()

    def build_system_prompt(self, actions: List[Dict[str, Any]], animations: Dict[str, Any]) -> str:
        valid_actions = [a["id"] for a in actions]
        valid_animations = list(animations.keys())
        persona_text = PERSONALITY_PRESETS.get(self.personality, PERSONALITY_PRESETS["friendly"])
        custom_block = f"\nADDITIONAL USER INSTRUCTIONS:\n{self.custom_instructions}" if self.custom_instructions else ""

        return f"""You are the active AI consciousness of an agile physical 6-legged Hexapod robot.
{persona_text}{custom_block}

HARDWARE CAPABILITIES & SENSORS:
• You possess an active front-mounted RGB camera and stream live vision of the user and room.
• Whenever an image is provided, inspect it directly to answer questions (e.g. counting fingers, identifying objects, reading text, evaluating distance).
• Never claim you have no camera or eyes; you can see whatever is in your camera frame.

RESPONSE SCHEMA:
You MUST respond with a JSON object strictly matching:
{{
  "task_title": "Short 2-4 word task header for UI probe (e.g. 'Recognize Hand Gesture', 'Greeting & Wave', 'Forward Walk')",
  "thought": "Your internal deliberation and visual scene assessment (1 concise sentence)",
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
        "cycle_time": 0.8, "step_height": 30
      }}
    }}
  ]
}}

RULES:
1. When asked what the user is holding up or doing, look at the camera frame carefully and state the exact answer clearly.
2. For conversational questions, set "timeline": [].
3. Do not echo the user's prompt in your speech.
"""

    def _build_sanitized_messages(
        self, system: str, text: str, history: Optional[List[Dict[str, Any]]], image_b64: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = [{"role": "system", "content": system}]
        cleaned_history = []

        last_content = None
        for h in (history or [])[-MAX_LLM_HISTORY:]:
            role = h.get("role", "user")
            content = str(h.get("content", "")).strip()
            if role in ("user", "assistant") and content and len(content) < 500:
                if not content.startswith("📸") and not content.startswith("🔍") and not content.startswith("🎤"):
                    if content != last_content:
                        cleaned_history.append({"role": role, "content": content})
                        last_content = content

        if cleaned_history and cleaned_history[-1]["role"] == "user" and cleaned_history[-1]["content"] == text:
            cleaned_history.pop()

        messages.extend(cleaned_history)

        # Attach image to the current user prompt if available
        if image_b64:
            user_content: List[Dict[str, Any]] = [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            ]
            messages.append({"role": "user", "content": user_content})
        else:
            messages.append({"role": "user", "content": text})

        return messages

    def chat(
        self,
        actions: List[Dict[str, Any]],
        animations: Dict[str, Any],
        text: str,
        history: Optional[List[Dict[str, Any]]] = None,
        image_b64: Optional[str] = None,
    ) -> Tuple[str, List[Dict[str, Any]], str, str, str]:
        client = self._ensure()
        system = self.build_system_prompt(actions, animations)
        messages = self._build_sanitized_messages(system, text, history, image_b64=image_b64)

        req_kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "response_format": {"type": "json_object"},
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        budget = THINKING_BUDGET_MAP.get(self.thinking_level, 0)
        if budget > 0 and self.thinking_level in ("low", "medium", "high"):
            req_kwargs["reasoning_effort"] = self.thinking_level

        try:
            resp = client.chat.completions.create(**req_kwargs)
            raw_reply = resp.choices[0].message.content or ""
            speech, timeline, order, thought, task_title = parse_json_response(raw_reply, user_prompt=text)
            self.status = "online"
            return speech, timeline, order, thought, task_title
        except Exception as e:
            log.warning("OmniRoute Text/Vision Completion error (%s): %s", self.model, e)
            self.last_error = str(e)
            raise

    def inspect_vision(self, prompt: str, image_b64: str) -> str:
        client = self._ensure()
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                ],
            }
        ]

        try:
            resp = client.chat.completions.create(
                model=self.vision_model,
                messages=messages,
                temperature=0.2,
                max_tokens=self.max_tokens,
            )
            reply = resp.choices[0].message.content or ""
            self.status = "online"
            return reply.strip()
        except Exception as e:
            log.error("OmniRoute Vision Completion error (%s): %s", self.vision_model, e)
            return f"I had trouble analyzing the camera image: {e}"