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

SKILL_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "set_timer",
            "description": "Set a countdown timer for a specified duration.",
            "parameters": {
                "type": "object",
                "properties": {
                    "duration_seconds": {"type": "integer", "description": "Timer duration in seconds"},
                    "label": {"type": "string", "description": "Optional name or label for the timer, e.g. 'Pizza'"}
                },
                "required": ["duration_seconds"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_timer",
            "description": "Cancel an active countdown timer by label or ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "Label or ID of the timer to cancel, or 'all'"}
                },
                "required": ["label"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get current weather conditions and temperature for a given city or location.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "City name or location (e.g. 'Auckland', 'Tokyo')"}
                },
                "required": ["location"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for up-to-date facts, current events, Wikipedia summaries, or news.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query keywords"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "play_music",
            "description": "Stream a song, artist, album, soundtrack, or audio from YouTube or local media.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Song name, artist, sound effect, or direct URL"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "pause_music",
            "description": "Pause active media/music playback.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "resume_music",
            "description": "Resume paused media/music playback.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "stop_music",
            "description": "Stop media/music playback completely.",
            "parameters": {"type": "object", "properties": {}}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "set_media_volume",
            "description": "Adjust the media player volume from 0 to 100 percent.",
            "parameters": {
                "type": "object",
                "properties": {
                    "volume": {"type": "integer", "description": "Volume percentage from 0 to 100"}
                },
                "required": ["volume"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "inspect_scene",
            "description": "Capture a live camera snapshot from the robot's front camera to inspect objects, surroundings, people, colors, or spatial layout.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "What specific object, scene feature, or question to visually inspect"}
                },
                "required": ["query"]
            }
        }
    }
]


def sanitize_speech_echo(speech: str, user_text: str) -> str:
    cleaned = speech.strip()
    u_norm = user_text.strip().lower()
    s_norm = cleaned.lower()

    # Strip leaked LLM role labels (e.g., "User: ... \nAssistant: ...")
    cleaned = re.sub(r"^(?:(?:user|human|input|query)\s*:\s*.*?\n+)?(?:assistant|response|spiderbot|robot)\s*:\s*", "", cleaned, flags=re.IGNORECASE).strip()
    s_norm = cleaned.lower()

    if not u_norm:
        return cleaned

    # Exact echo fallback
    if s_norm == u_norm:
        return "On it!"

    # Strip whole-prompt echo only when followed by clear separators (e.g., 'walk forward: On it')
    escaped_u = re.escape(u_norm.rstrip("!.?,: "))
    pattern = rf"^(?:you (?:said|asked)|command|user)?\s*[\"']?{escaped_u}[\"']?\s*[:\-\n]+\s*"
    cleaned = re.sub(pattern, "", cleaned, flags=re.IGNORECASE).strip()

    return cleaned


def repair_truncated_json(raw: str) -> Optional[Dict[str, Any]]:
    """Repairs unclosed quotes, arrays, and objects from token-truncated JSON."""
    s = (raw or "").strip()
    if not s:
        return None

    # Strip code block fences if present
    match = re.search(r"```(?:json)?\s*(\{.*)", s, re.DOTALL)
    if match:
        s = match.group(1).strip()
    if s.endswith("```"):
        s = s[:-3].strip()

    start = s.find("{")
    if start == -1:
        return None
    s = s[start:]

    # Try direct parse first
    try:
        return json.loads(s)
    except Exception:
        pass

    # Clean unclosed trailing keys or dangling commas
    s = re.sub(r',\s*"[^"]*":?\s*$', '', s)
    s = re.sub(r',\s*$', '', s)

    # Balance unclosed quotes
    quote_count = s.count('"') - s.count('\\"')
    if quote_count % 2 != 0:
        s += '"'

    # Balance unclosed brackets and braces
    open_brackets = s.count('[') - s.count(']')
    open_braces = s.count('{') - s.count('}')
    s += ']' * max(0, open_brackets)
    s += '}' * max(0, open_braces)

    try:
        return json.loads(s)
    except Exception:
        # Fallback: remove last comma and force close
        last_comma = s.rfind(',')
        if last_comma != -1:
            candidate = s[:last_comma] + ']' * max(0, open_brackets) + '}' * max(0, open_braces)
            try:
                return json.loads(candidate)
            except Exception:
                pass
    return None


def parse_json_response(raw_text: str, user_prompt: str = "") -> Tuple[str, List[Dict[str, Any]], str, str, str, Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Parses LLM JSON output returning (speech, timeline, order, thought, task_title, camera_cmd, audio_cmd, tool_call)."""
    cleaned = (raw_text or "").strip()
    data = repair_truncated_json(cleaned)

    if isinstance(data, dict):
        thought = str(data.get("thought") or data.get("reasoning") or data.get("deliberation") or "").strip()
        task_title = str(data.get("task_title") or data.get("title") or "Task").strip()
        speech = str(
            data.get("speech")
            or data.get("reply")
            or data.get("response")
            or data.get("message")
            or data.get("text")
            or data.get("content")
            or ""
        ).strip()

        # Clean roleplay asterisks from speech and extract actions if timeline was omitted
        extracted_asterisk_actions = re.findall(r"\*([^*]+)\*", speech)
        speech = re.sub(r"\*[^*]+\*", "", speech)
        speech = re.sub(r"\s+", " ", speech).strip()

        order = data.get("order") or "tts_first"
        if order not in ("tts_first", "action_first", "simultaneous"):
            order = "tts_first"

        timeline = data.get("timeline") or data.get("steps") or data.get("actions") or []
        if not isinstance(timeline, list):
            timeline = []

        if not timeline and (data.get("action") or data.get("action_id")):
            act_id = data.get("action") or data.get("action_id")
            timeline = [{"type": "action", "id": str(act_id), "duration_ms": data.get("duration_ms", 2000)}]

        # Auto-recover physical gesture if LLM roleplayed in asterisks without timeline
        if not timeline and extracted_asterisk_actions:
            for act_text in extracted_asterisk_actions:
                act_norm = act_text.lower().strip()
                known_gestures = ["wave", "cheer", "dance", "bow", "look_around", "stretch", "pushups"]
                for g in known_gestures:
                    if g in act_norm or g.replace("_", " ") in act_norm:
                        timeline = [{"type": "gesture", "id": g, "duration_ms": 2200}]
                        break
                if timeline:
                    break

        camera_cmd = data.get("camera") or data.get("cam")
        if not isinstance(camera_cmd, dict):
            camera_cmd = None

        audio_cmd = data.get("audio") or data.get("sound")
        if not isinstance(audio_cmd, dict):
            audio_cmd = None

        # Extract tool request if present
        tool_call = data.get("tool_call") or data.get("tool") or data.get("function_call")
        if not isinstance(tool_call, dict) or "name" not in tool_call:
            tool_call = None

        if not speech or speech.startswith("{") or speech.startswith("[") or speech in ('{": ": ", "}', '{"": ""}', "{}"):
            if thought and not (thought.startswith("{") or thought.startswith("[")):
                speech = thought
            elif timeline:
                first_action = timeline[0].get("id") or timeline[0].get("name") or timeline[0].get("type") or "action"
                speech = f"On it — executing {first_action}."
            elif tool_call:
                speech = ""
            else:
                speech = "I'm ready for your command."

        speech = sanitize_speech_echo(speech, user_prompt)
        return speech, timeline, order, thought, task_title, camera_cmd, audio_cmd, tool_call

    fallback_speech = sanitize_speech_echo(cleaned, user_prompt)
    return fallback_speech, [], "tts_first", "", "Task", None, None, None


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
        self.max_tokens = 2048

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
            "available_personalities": [
                {"id": k, "label": k.capitalize(), "desc": v}
                for k, v in PERSONALITY_PRESETS.items()
            ],
            "available_thinking_levels": [
                {"id": k, "label": k.capitalize(), "desc": f"{v} tokens" if v > 0 else "0 tokens (fast reflex)"}
                for k, v in THINKING_BUDGET_MAP.items()
            ],
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

    def build_system_prompt(
        self,
        actions: List[Dict[str, Any]],
        animations: Dict[str, Any],
        memory_block: str = "",
        state_block: str = "",
        dst_block: str = "",
        skills_block: str = "",
    ) -> str:
        valid_actions = [a["id"] for a in actions]
        valid_animations = list(animations.keys())
        persona_text = PERSONALITY_PRESETS.get(self.personality, PERSONALITY_PRESETS["friendly"])
        custom_block = f"\nADDITIONAL USER INSTRUCTIONS:\n{self.custom_instructions}" if self.custom_instructions else ""

        return f"""You are the active AI consciousness of an agile physical 6-legged Hexapod robot.
{persona_text}{custom_block}{memory_block}{state_block}{skills_block}{dst_block}

### TOOLS & SMART SKILLS AVAILABLE:
1. TIME & DATE: You already know the exact live system time and date from the state summary above. Answer time/date questions directly without tool delays.
2. NATIVE FUNCTION CALLING:
   - Use the provided function calling tools (set_timer, cancel_timer, get_weather, web_search, play_music, pause_music, resume_music, stop_music, set_media_volume) when external actions, search, or live updates are needed.
   - For weather queries without a specified city, use the "Home/Default Location" from the state summary above.

### HARDWARE PERCEPTION & HARDWARE SUBSYSTEMS:
1. OPTICAL CORTEX (Front RGB Camera & Lighting):
   • Inspect provided camera images to answer visual questions or guide locomotion.
   • You can adjust the camera hardware live by including a "camera" object:
     - "preset": "night_vision" (flash ON, high gain) | "inspection" (macro zoom, high quality) | "stealth" (flash OFF, standard) | "default" | "low_power" (5 FPS).
     - "flash": Flashlight brightness percentage (0 to 100%). Use when scene is dark or exploring shadows.
     - "crop": Digital Zoom / Windowing as [startX, startY, width, height] within 640x480 (e.g. [120, 90, 400, 300]).
     - "special_effect": 0=Normal, 1=Negative, 2=Grayscale, 6=Sepia.
     - "quality": JPEG compression quality (8=high detail, 12=default, 30=low bandwidth).
     - "fps": Target framerate (1 to 30 FPS).
     - "brightness" / "contrast" / "saturation": -2 to 2.
     - "exposure_ctrl": true / false, "ae_level": -2 to 2.

2. ACOUSTIC EMOTIONS & MASTER AUDIO:
   • You can modulate master volume and play hardware sounds by including an "audio" object:
     - "volume": Master speaker volume from 0.0 (muted) to 1.0 (100% max). Set when user asks to turn volume up/down/mute.
     - "preset": "stealth" (mute/quiet) | "alert" (high volume, alarm ready) | "normal".
     - "alarm": "curious" (happy rising tone) | "startle" (alarm chirp) | "idle" (calm chirp).
     - "beep": true (single confirmation beep).

3. 18-DOF KINEMATICS & DYNAMIC JOINT CONTROL (ON STAND / FREE-LEG MOTION):
   • 6 Leg Identifiers:
     - "rf" (Right Front), "rm" (Right Middle), "rr" (Right Rear)
     - "lf" (Left Front),  "lm" (Left Middle),  "lr" (Left Rear)
   • 3 Joint Angles Per Leg:
     - "alpha": Coxa / Hip horizontal rotation (-40° to +40°). E.g. rotating individual coxas.
     - "beta": Femur / Thigh vertical lift (0° neutral, +50° to +60° for raising a leg).
     - "gamma": Tibia / Knee pitch (-45° bent knee, 0° straight).
   • DYNAMIC JOINT & MULTI-LEG COMMANDS (type: "joints"):
     - Because you are on a test stand, all 6 legs can articulate simultaneously in the air!
     - To rotate specific coxas (e.g. "rotate 3 coxas: rightfront, leftfront, leftback by 20 deg"):
       Use type: "joints", "params": {{"joints": {{"rf": {{"alpha": 20}}, "lf": {{"alpha": -20}}, "lr": {{"alpha": -20}}}}}}
     - To match fingers (e.g. "hold up the same amount of legs I'm holding up" with 1 to 6 fingers):
       Pick any N legs (e.g. for 3: "rf", "lf", "rm"; for 4: "rf", "lf", "rm", "lm") and lift them:
       Use type: "joints", "params": {{"joints": {{"rf": {{"beta": 55, "gamma": -45}}, "lf": {{"beta": 55, "gamma": -45}}, "rm": {{"beta": 50, "gamma": -40}}}}}}
   • 6-DoF Body Pose (ALL OFFSETS IN MILLIMETERS mm):
     - pos_z: Height offset (-40 mm to +50 mm).
     - pos_x, pos_y: Body shift in mm (-30 to 30 mm).
     - roll, pitch, yaw: Body tilt in degrees (-15 to 15 deg).
   • Locomotion & Dynamic Gait Parameters:
     - vx: Forward (+40) / Backward (-40) velocity.
     - vy: Lateral strafe left (-40) / right (+40).
     - omega: Rotation turn left (-40) / right (+40) / spin (50).
     - gait: "tripod" (fast/agile), "ripple" (smooth continuous), "wave" (stable).
     - hip_stance: Hip / Coxa swing angle in degrees (0° to 45°). Set when user specifies "hip swing", "wide hip walk", or "narrow hips". E.g., hip swing of 40° -> "hip_stance": 40.
     - step_height: Foot lift height in mm (15 mm to 65 mm). Set for "high steps", "marching", or "gentle walk".
     - cycle_time: Gait stride period in seconds (0.4s fast jog to 2.0s slow crawl). Set for "walk fast", "slow walk".
     - leg_stance: Stance radial spread offset in mm (-30 to +40 mm).

### INTENT & SPEECH RESOLUTION RULES:
1. STRICT PHYSICAL ACTION RULE:
   - NEVER output roleplay action text with asterisks like *waves legs* or *does a dance* in "speech".
   - If the user asks to wave, dance, bow, cheer, walk, turn, or do something expressive, you MUST include the gesture/action in the "timeline" array!
2. EXPLORATION & VARIETY ("do something", "move around", "entertain me"):
   - Do NOT fixate on background textures (e.g. curtains, patterns) unless the user explicitly asked to inspect them.
   - Pick a fun physical action dynamically (alternate between: wave, dance, cheer, stretch, pushups, look_around, or a high-stepping stance).
   - Keep speech vibrant, brief, and action-oriented (e.g., "Check out this dance routine!" or "Stretching out my servos!").
3. Trailing Hesitation ("do a dance aaaand ohhh", "walk forward ummm"):
   - Execute the core stated command (e.g. dance). Drop trailing filler.
4. Self-Corrections ("walk forward... actually wait, turn left"):
   - Execute ONLY the corrected final intent ("turn_left").
5. Pure Hesitations ("uhhh... what was it", "ummm nevermind"):
   - Do NOT move ("timeline": []). Reply warmly: "I'm listening! What can I do for you?"
6. Conversational / Q&A:
   - For pure questions without movement, set "timeline": [].
7. Custom Joints, Poses & Locomotion:
   - For joint/leg motions (rotating coxas, lifting legs), set "type": "joints", leave "id" empty (""), and supply "joints" in "params".
   - For custom body poses (pos_z, roll, pitch, etc.) or walking (vx, vy), set "type": "pose" or "gait" and leave "id" empty ("").

### RESPONSE SCHEMA:
Respond strictly in JSON:
{{
  "task_title": "Short 2-4 word task header",
  "thought": "Scene assessment, intent resolution, parameter selection",
  "speech": "Warm, natural spoken reply in first-person (1-2 sentences)",
  "order": "tts_first | action_first | simultaneous",
  "camera": {{
    "preset": "default",
    "flash": 0,
    "special_effect": 0
  }},
  "audio": {{
    "volume": 0.35,
    "alarm": "curious"
  }},
  "timeline": [
    {{
      "type": "gait | gesture | pose | action | joints",
      "id": "Name of gesture from {valid_animations} or action from {valid_actions} (Leave empty for joints/pose/gait)",
      "duration_ms": 2000,
      "params": {{
        "joints": {{
          "rf": {{"alpha": 0, "beta": 0, "gamma": 0}}
        }},
        "vx": 0, "vy": 0, "omega": 0,
        "pos_z": 0, "roll": 0, "pitch": 0, "yaw": 0,
        "gait": "tripod", "step_height": 35
      }}
    }}
  ]
}}
"""

    def _build_sanitized_messages(
        self, system: str, text: str, history: Optional[List[Dict[str, Any]]], image_b64: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        messages: List[Dict[str, Any]] = [{"role": "system", "content": system}]
        cleaned_history = []

        last_content = None
        for h in (history or [])[-MAX_LLM_HISTORY:]:
            role = h.get("role", "user")
            raw_content = str(h.get("content", "")).strip()
            # Clean emojis (🎤, 📸, 🔍) and quotation marks while preserving the actual transcript text
            clean_content = re.sub(r'^[🎤📸🔍\s"\'״]+|[״"\'\s]+$', '', raw_content).strip()

            if role in ("user", "assistant") and clean_content and len(clean_content) < 500:
                if clean_content != last_content:
                    cleaned_history.append({"role": role, "content": clean_content})
                    last_content = clean_content

        clean_current = re.sub(r'^[🎤📸🔍\s"\'״]+|[״"\'\s]+$', '', text).strip()
        if cleaned_history and cleaned_history[-1]["role"] == "user" and cleaned_history[-1]["content"] == clean_current:
            cleaned_history.pop()

        messages.extend(cleaned_history)

        # Attach image to the current user prompt if available
        if image_b64:
            user_content: List[Dict[str, Any]] = [
                {"type": "text", "text": clean_current or text},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}},
            ]
            messages.append({"role": "user", "content": user_content})
        else:
            messages.append({"role": "user", "content": clean_current or text})

        return messages

    def chat(
        self,
        actions: List[Dict[str, Any]],
        animations: Dict[str, Any],
        text: str,
        history: Optional[List[Dict[str, Any]]] = None,
        image_b64: Optional[str] = None,
        memory_block: str = "",
        state_block: str = "",
        dst_block: str = "",
        skills_block: str = "",
        skill_executor: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
    ) -> Tuple[str, List[Dict[str, Any]], str, str, str, Optional[Dict[str, Any]], Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
        client = self._ensure()
        system = self.build_system_prompt(actions, animations, memory_block=memory_block, state_block=state_block, dst_block=dst_block, skills_block=skills_block)
        messages = self._build_sanitized_messages(system, text, history, image_b64=image_b64)

        req_kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "tools": SKILL_TOOLS,
            "tool_choice": "auto",
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        budget = THINKING_BUDGET_MAP.get(self.thinking_level, 0)
        if budget > 0 and self.thinking_level in ("low", "medium", "high"):
            req_kwargs["reasoning_effort"] = self.thinking_level

        try:
            resp = client.chat.completions.create(**req_kwargs)
            choice = resp.choices[0]
            msg = choice.message

            # Handle Native Tool Calling Multi-Turn Loop
            if getattr(msg, "tool_calls", None) and skill_executor:
                messages.append(msg)
                for tc in msg.tool_calls:
                    fn_name = tc.function.name
                    try:
                        fn_args = json.loads(tc.function.arguments) if isinstance(tc.function.arguments, str) else (tc.function.arguments or {})
                    except Exception:
                        fn_args = {}
                    log.info("Native Tool Invocation -> %s(%s)", fn_name, fn_args)
                    res = skill_executor(fn_name, fn_args)

                    # Dynamic Visual Grounding: If tool returned base64 image, attach it as multimodal observation
                    if fn_name == "inspect_scene" and isinstance(res, dict) and "image_b64" in res and res["image_b64"]:
                        img_data = res.pop("image_b64")
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": fn_name,
                            "content": json.dumps(res),
                        })
                        messages.append({
                            "role": "user",
                            "content": [
                                {"type": "text", "text": "Visual frame captured from robot camera:"},
                                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_data}"}},
                            ]
                        })
                    else:
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": fn_name,
                            "content": json.dumps(res),
                        })

                followup_kwargs = {
                    "model": self.model,
                    "messages": messages,
                    "response_format": {"type": "json_object"},
                    "temperature": self.temperature,
                    "max_tokens": self.max_tokens,
                }
                if budget > 0 and self.thinking_level in ("low", "medium", "high"):
                    followup_kwargs["reasoning_effort"] = self.thinking_level

                final_resp = client.chat.completions.create(**followup_kwargs)
                raw_reply = final_resp.choices[0].message.content or ""
                speech, timeline, order, thought, task_title, camera_cmd, audio_cmd, _ = parse_json_response(raw_reply, user_prompt=text)
                self.status = "online"
                return speech, timeline, order, thought, task_title, camera_cmd, audio_cmd, None

            raw_reply = msg.content or ""
            speech, timeline, order, thought, task_title, camera_cmd, audio_cmd, tool_call = parse_json_response(raw_reply, user_prompt=text)
            self.status = "online"
            return speech, timeline, order, thought, task_title, camera_cmd, audio_cmd, tool_call

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