# pi-hub/services/ai-service/pipeline.py
import logging
import threading

from action_parser import match_action, action_by_id

log = logging.getLogger("ai.pipeline")

CANNED_OFFLINE_GREETING = "Hello! I'm running in offline mode right now, but ready for hardware commands."
CANNED_UNKNOWN = "I'm offline and didn't catch a direct command — try saying 'walk forward' or select an action card."


class PipelineResult:
    def __init__(self, action=None, reply=None):
        self.action = action
        self.reply = reply or ""


class Pipeline:
    def __init__(self, actions, llm=None, stt=None):
        self.actions = actions
        self.llm = llm
        self.stt = stt
        self._timers = set()

    def decide(self, text, history=None):
        """Processes text. When the LLM is available, it handles the conversation."""
        text = (text or "").strip()
        if not text:
            return PipelineResult(reply=CANNED_UNKNOWN)

        # 1. Primary: Remote LLM with Embodiment and Tool Decisions
        if self.llm and self.llm.status != "offline":
            try:
                action_id, speech = self.llm.chat(self.actions, text, history=history or [])
                action = action_by_id(self.actions, action_id) if action_id else None
                return PipelineResult(action=action, reply=speech)
            except Exception as e:
                log.warning("LLM call failed, falling back to offline keywords: %s", e)

        # 2. Offline Fallback: Deterministic Keyword Matcher
        action = match_action(text, self.actions)
        if action:
            return PipelineResult(action=action, reply=action.get("reply", "Executing command."))

        return PipelineResult(reply=CANNED_UNKNOWN)

    def execute(self, result, on_cmd, on_audio, on_tts_text, on_ai_reply):
            action = result.action
            directive_action_id = None

            if action:
                payload = action["payload"]
                if action["topic"] == "audio":
                    on_audio(payload)
                elif payload.get("type") == "preset":
                    directive_action_id = action["id"]
                else:
                    on_cmd(payload)
                    
                    # Calculate required duration: action duration or speech duration + buffer
                    base_duration = action.get("duration_ms") or 0
                    speech_len = len(result.reply) if result.reply else 0
                    # ~15 characters per second + 1.5s TTS transfer buffer
                    tts_duration_ms = int((speech_len / 15.0) * 1000) + 1500 if speech_len > 0 else 0
                    
                    effective_duration_ms = max(base_duration, tts_duration_ms)
                    
                    if effective_duration_ms > 0:
                        stop = dict(payload)
                        for k in ("vx", "vy", "omega"):
                            stop[k] = 0
                        self._schedule_stop(effective_duration_ms, stop, on_cmd)

            if result.reply:
                on_ai_reply(result.reply, action_id=directive_action_id)
                on_tts_text(result.reply)

    def _schedule_stop(self, delay_ms, stop_payload, on_cmd):
        def stop():
            try:
                on_cmd(stop_payload)
            except Exception as e:
                log.warning("Auto-stop publish failed: %s", e)
        t = threading.Timer(delay_ms / 1000.0, stop)
        t.daemon = True
        self._timers.add(t)
        t.start()

    def cancel_timers(self):
        for t in tuple(self._timers):
            t.cancel()
        self._timers.clear()