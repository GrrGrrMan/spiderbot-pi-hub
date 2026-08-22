# services/ai-service/pipeline.py
import logging
import threading
import time

from action_parser import match_action, action_by_id

log = logging.getLogger("ai.pipeline")

CANNED_OFFLINE_GREETING = "Hello! I'm running in offline mode right now, but ready for hardware commands."
CANNED_UNKNOWN = "I'm offline and didn't catch a direct command — try saying 'walk forward' or select an action card."


class PipelineResult:
    def __init__(self, action=None, reply=None, order="tts_first"):
        self.action = action
        self.reply = reply or ""
        self.order = order or "tts_first"


class Pipeline:
    def __init__(self, actions, llm=None, stt=None):
        self.actions = actions
        self.llm = llm
        self.stt = stt
        self._timers = set()

    def decide(self, text, history=None):
        text = (text or "").strip()
        if not text:
            return PipelineResult(reply=CANNED_UNKNOWN, order="tts_first")

        # 1. Primary: LLM Embodied Decision
        if self.llm and self.llm.status != "offline":
            try:
                action_id, speech, order = self.llm.chat(self.actions, text, history=history or [])
                action = action_by_id(self.actions, action_id) if action_id else None
                return PipelineResult(action=action, reply=speech, order=order)
            except Exception as e:
                log.warning("LLM call failed, falling back to offline matcher: %s", e)

        # 2. Offline Fallback
        action = match_action(text, self.actions)
        if action:
            return PipelineResult(action=action, reply=action.get("reply", "Executing command."), order="tts_first")

        return PipelineResult(reply=CANNED_UNKNOWN, order="tts_first")

    def execute(self, result, on_cmd, on_audio, on_tts_text, on_ai_reply, on_action_directive, wait_for_audio_fn):
        action = result.action
        order = result.order or "tts_first"
        reply = result.reply or ""

        def dispatch_action():
            if not action:
                return
            payload = action["payload"]
            duration_ms = action.get("duration_ms") or 0

            if action.get("topic") == "audio":
                on_audio(payload)
            elif payload.get("type") == "preset":
                # Presets are interpolated 60fps by the Web-UI solver
                on_action_directive(action["id"])
            else:
                # Direct hardware motion command
                on_cmd(payload)
                if duration_ms > 0:
                    stop = dict(payload)
                    for k in ("vx", "vy", "omega"):
                        stop[k] = 0
                    self._schedule_stop(duration_ms, stop, on_cmd)

        # ── Execution Mode 1: Action First, then TTS Speech ──
        if order == "action_first":
            if action:
                dispatch_action()
                action_duration_s = (action.get("duration_ms") or 2000) / 1000.0
                time.sleep(action_duration_s)
            if reply:
                on_ai_reply(reply)
                on_tts_text(reply)

        # ── Execution Mode 2: Simultaneous (Walk and Talk) ──
        elif order == "simultaneous":
            if action:
                dispatch_action()
            if reply:
                on_ai_reply(reply)
                on_tts_text(reply)

        # ── Execution Mode 3: TTS First, then Action (Default & Safest) ──
        else: # tts_first
            if reply:
                on_ai_reply(reply)
                tts_duration_s = on_tts_text(reply) or 0.0
                # Wait for S3 playback to finish via S3 MQTT event or calculated timeout
                wait_for_audio_fn(timeout_s=tts_duration_s + 0.5)

            if action:
                dispatch_action()

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