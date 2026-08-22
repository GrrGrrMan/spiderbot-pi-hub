import logging
import threading
import time

from action_parser import (
    match_action, 
    action_by_id, 
    load_animations, 
    compile_animation_sequence,
    normalize_animation_name
)

log = logging.getLogger("ai.pipeline")

CANNED_OFFLINE_GREETING = "Hello! I'm running in offline mode right now, but ready for hardware commands."
CANNED_UNKNOWN = "I'm offline and didn't catch a direct command — try saying 'walk forward' or select an action card."


MAX_REPEAT_COUNT = 10
MAX_STEP_DURATION_MS = 15000  # 15 seconds max continuous motion per block
MAX_TIMELINE_STEPS = 10
MAX_VX = 80.0
MAX_VY = 60.0
MAX_OMEGA = 60.0

def sanitize_step(step):
    """Clamps hallucinated LLM values to physically safe hardware limits."""
    dur_ms = min(int(step.get("duration_ms", 1000)), MAX_STEP_DURATION_MS)
    dur_ms = max(50, dur_ms)
    step["duration_ms"] = dur_ms

    if "repeat" in step:
        step["repeat"] = max(1, min(int(step["repeat"]), MAX_REPEAT_COUNT))

    if "params" in step and isinstance(step["params"], dict):
        p = step["params"]
        if "vx" in p: p["vx"] = max(-MAX_VX, min(float(p["vx"]), MAX_VX))
        if "vy" in p: p["vy"] = max(-MAX_VY, min(float(p["vy"]), MAX_VY))
        if "omega" in p: p["omega"] = max(-MAX_OMEGA, min(float(p["omega"]), MAX_OMEGA))

    return step

class PipelineResult:
    def __init__(self, timeline=None, reply=None, order="tts_first"):
        self.timeline = timeline or []
        self.reply = reply or ""
        self.order = order or "tts_first"


class Pipeline:
    def __init__(self, actions, llm=None, stt=None, animations=None):
        self.actions = actions
        self.animations = animations or load_animations()
        self.llm = llm
        self.stt = stt

    def decide(self, text, history=None):
        text = (text or "").strip()
        if not text:
            return PipelineResult(reply=CANNED_UNKNOWN, order="tts_first")

        # 1. Primary: LLM Embodied Decision with Full Timeline Support
        if self.llm and self.llm.status != "offline":
            try:
                speech, timeline, order = self.llm.chat(
                    self.actions, self.animations, text, history=history or []
                )
                return PipelineResult(timeline=timeline, reply=speech, order=order)
            except Exception as e:
                log.warning("LLM call failed, falling back to offline matcher: %s", e)

        # 2. Offline Fallback: Single Action Matching
        action = match_action(text, self.actions)
        if action:
            timeline = [{ "type": "action", "id": action["id"], "duration_ms": action.get("duration_ms", 2000) }]
            return PipelineResult(timeline=timeline, reply=action.get("reply", "Executing command."), order="tts_first")

        return PipelineResult(reply=CANNED_UNKNOWN, order="tts_first")

    def execute(self, result, on_cmd, on_audio, on_tts_text, on_ai_reply, on_action_directive, wait_for_audio_fn):
        timeline = result.timeline
        order = result.order or "tts_first"
        reply = result.reply or ""

        def run_timeline():
            safe_timeline = [sanitize_step(s) for s in timeline[:MAX_TIMELINE_STEPS]]
            for step in safe_timeline:
                stype = step.get("type", "action")
                act_id = step.get("id") or ""
                dur_ms = step.get("duration_ms", 0)
                params = step.get("params", {})

                # Check if act_id resolves to a known keyframe animation
                anim_key = normalize_animation_name(act_id or step.get("name") or "")

                # ── A. Single-Packet Dynamic Sequence Execution ──
                if stype in ("gesture", "sequence") or (anim_key in self.animations):
                    anim_id = anim_key or step.get("name")
                    if anim_id in self.animations:
                        anim = self.animations[anim_id]
                        repeat_count = max(1, int(step.get("repeat", 1)))
                        target_dur = step.get("duration_ms") or anim.get("default_duration_ms", 2000)

                        compiled_kfs, total_ms = compile_animation_sequence(anim, duration_override_ms=target_dur)

                        for _ in range(repeat_count):
                            payload = {
                                "type": "sequence",
                                "name": anim_id,
                                "duration_ms": total_ms,
                                "keyframes": compiled_kfs
                            }
                            on_cmd(payload)
                            time.sleep(total_ms / 1000.0)
                        continue

                # ── B. Direct Parameterized Gait ──
                if stype == "gait":
                    payload = {
                        "type": "motion",
                        "gait": params.get("gait", "tripod"),
                        "vx": params.get("vx", 40),
                        "vy": params.get("vy", 0),
                        "omega": params.get("omega", 0),
                        "step_height": params.get("step_height", 30),
                        "cycle_time": params.get("cycle_time", 1.0),
                        "tx": params.get("tx", 0),
                        "ty": params.get("ty", 0),
                        "tz": params.get("tz", 0),
                        "rx": params.get("rx", 0),
                        "ry": params.get("ry", 0),
                        "rz": params.get("rz", 0)
                    }
                    on_cmd(payload)
                    run_dur = dur_ms or 1000
                    time.sleep(run_dur / 1000.0)
                    
                    stop = dict(payload)
                    stop["vx"] = stop["vy"] = stop["omega"] = 0
                    on_cmd(stop)
                    continue

                # ── C. Action ID Look-up (from actions.json) ──
                act = action_by_id(self.actions, act_id)
                if act:
                    payload = act["payload"]
                    topic = act.get("topic", "cmd")
                    act_dur = dur_ms or act.get("duration_ms", 0)

                    if topic == "audio":
                        on_audio(payload)
                    elif payload.get("type") == "preset":
                        preset_name = normalize_animation_name(payload.get("preset", ""))
                        if preset_name in self.animations:
                            target_anim = self.animations[preset_name]
                            compiled_kfs, total_ms = compile_animation_sequence(target_anim, duration_override_ms=act_dur)
                            seq_payload = {
                                "type": "sequence",
                                "name": preset_name,
                                "duration_ms": total_ms,
                                "keyframes": compiled_kfs
                            }
                            on_cmd(seq_payload)
                            time.sleep(total_ms / 1000.0)
                        else:
                            on_cmd(payload)
                            if act_dur > 0:
                                time.sleep(act_dur / 1000.0)
                    else:
                        on_cmd(payload)
                        if act_dur > 0:
                            time.sleep(act_dur / 1000.0)
                            if payload.get("type") == "motion":
                                stop = dict(payload)
                                stop["vx"] = stop["vy"] = stop["omega"] = 0
                                on_cmd(stop)

        # ── Orchestrate Audio & Motion Execution ──
        if order == "action_first":
            if timeline:
                run_timeline()
            if reply:
                on_ai_reply(reply)
                on_tts_text(reply)

        elif order == "simultaneous":
            t = threading.Thread(target=run_timeline, daemon=True)
            t.start()
            if reply:
                on_ai_reply(reply)
                on_tts_text(reply)
            t.join(timeout=12.0)

        else: # tts_first
            if reply:
                on_ai_reply(reply)
                tts_dur = on_tts_text(reply) or 0.0
                wait_for_audio_fn(timeout_s=tts_dur + 0.5)
            if timeline:
                run_timeline()