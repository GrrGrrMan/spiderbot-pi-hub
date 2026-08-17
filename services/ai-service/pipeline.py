# pi-hub/services/ai-service/pipeline.py
# P5 decision + execution pipeline (broker-agnostic; the MQTT client injects
# publish callbacks). Order of operations:
#   1. stage-1 keyword match (deterministic, offline)
#   2. remote LLM (Groq) with function-calling tools -> action_id + reply
#   3. canned chat reply if LLM unavailable and no keyword matched
# Motion actions auto-stop after the table's duration_ms via a timer
# (mirrors the web-ui card TTL behavior; the firmware watchdog is the last resort).
import logging
import threading

from action_parser import match_action, action_by_id, llm_tool_schema

log = logging.getLogger("ai.pipeline")

SYSTEM_PROMPT = (
    "You are the voice assistant inside a six-legged hexapod robot named Hexa. "
    "Keep replies to 1-2 short sentences. If the user asks you to move, turn, spin, "
    "sing, beep, sleep, or wake, call perform_hexapod_action with the closest action_id. "
    "If the request is chat-only, just reply conversationally."
)
CANNED_UNKNOWN = "I didn't quite catch a command I can run — try something like 'walk forward' or tap an action button."


class PipelineResult:
    def __init__(self, action=None, reply=None):
        self.action = action          # action dict or None
        self.reply = reply or ""


class Pipeline:
    def __init__(self, actions, llm=None, stt=None):
        self.actions = actions
        self.llm = llm              # providers.llm.LLMClient or None
        self.stt = stt              # providers.stt.STTClient or None
        self._timers = set()        # TTL stop timers

    # --- decision ------------------------------------------------------------
    def decide(self, text, history=None):
        """Return PipelineResult for a text utterance."""
        text = (text or "").strip()
        if not text:
            return PipelineResult(reply=CANNED_UNKNOWN)

        # 1) deterministic fast path
        action = match_action(text, self.actions)
        if action:
            return PipelineResult(action=action, reply=action.get("reply", ""))

        # 2) remote LLM (attempt on "online" or first/unknown; fall through on error)
        if self.llm and self.llm.status != "offline":
            try:
                action_id, reply = self.llm.chat(self.actions, text, history=history or [], system=SYSTEM_PROMPT)
                if action_id:
                    action = action_by_id(self.actions, action_id)
                    return PipelineResult(action=action, reply=reply or action.get("reply", ""))
                return PipelineResult(reply=reply or CANNED_UNKNOWN)
            except Exception as e:   # network / 429 / auth / parse
                log.warning("LLM path failed, falling back to canned: %s", e)

        # 3) offline fallback
        return PipelineResult(reply=CANNED_UNKNOWN)

    # ------------------------------------------------------------------ execute
    def execute(self, result, on_cmd, on_audio, on_tts_text, on_ai_reply):
        """Publish the decided action + speak the reply.

        on_cmd(payload)                -> publish hexapod/{id}/cmd
        on_audio(payload)              -> publish hexapod/{id}/audio (alarm/beep)
        on_tts_text(reply)             -> synth + publish chunked TTS frames
        on_ai_reply(text, action_id=None) -> publish assistant chat message;
                                    action_id tells web-ui to run a preset locally
        """
        action = result.action
        directive_action_id = None
        if action:
            payload = action["payload"]
            if action["topic"] == "audio":
                on_audio(payload)
            elif payload.get("type") == "preset":
                # Chunk 2 — presets are web-ui-executed: the firmware has no
                # preset handler, so never publish to the cmd topic. The reply
                # carries the action_id; web-ui AIPanel runs the local
                # interpolator (motionSynthesizer) when it sees it.
                directive_action_id = action["id"]
            else:
                on_cmd(payload)
                duration_ms = action.get("duration_ms") or 0
                if duration_ms > 0:
                    stop = dict(payload)
                    for k in ("vx", "vy", "omega"):
                        stop[k] = 0
                    self._schedule_stop(duration_ms, stop, on_cmd)

        if result.reply:
            on_ai_reply(result.reply, action_id=directive_action_id)
            on_tts_text(result.reply)

    def _schedule_stop(self, delay_ms, stop_payload, on_cmd):
        def stop():
            try:
                on_cmd(stop_payload)
            except Exception as e:  # pragma: no cover
                log.warning("TTS auto-stop publish failed: %s", e)
        t = threading.Timer(delay_ms / 1000.0, stop)
        t.daemon = True
        self._timers.add(t)
        t.start()

    def cancel_timers(self):
        for t in tuple(self._timers):
            t.cancel()
        self._timers.clear()