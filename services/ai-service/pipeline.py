# services/ai-service/pipeline.py
import logging
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from action_parser import (
    action_by_id,
    compile_animation_sequence,
    load_animations,
    match_action,
    normalize_animation_name,
)

log = logging.getLogger("ai.pipeline")

CANNED_UNKNOWN = "I didn't catch that command — try saying 'walk forward' or select an action."

class PipelineResult:
    def __init__(
        self,
        timeline: Optional[List[Dict[str, Any]]] = None,
        reply: str = "",
        order: str = "tts_first",
        mode: str = "standard",  # "standard" | "compound"
        goal_text: str = "",
        thought: str = "",
        task_title: str = "",
        camera_cmd: Optional[Dict[str, Any]] = None,
        audio_cmd: Optional[Dict[str, Any]] = None,
    ):
        self.timeline = timeline or []
        self.reply = reply or ""
        self.order = order or "tts_first"
        self.mode = mode
        self.goal_text = goal_text
        self.thought = thought
        self.task_title = task_title
        self.camera_cmd = camera_cmd
        self.audio_cmd = audio_cmd


class Pipeline:
    def __init__(
        self,
        actions: List[Dict[str, Any]],
        llm: Optional[Any] = None,
        stt: Optional[Any] = None,
        animations: Optional[Dict[str, Any]] = None
    ):
        self.actions = actions
        self.animations = animations or load_animations()
        self.llm = llm
        self.stt = stt

    def decide(
        self,
        text: str,
        history: Optional[List[Dict[str, Any]]] = None,
        image_b64: Optional[str] = None,
        memory_block: str = "",
    ) -> PipelineResult:
        norm = (text or "").lower().strip().replace("foward", "forward")
        if not norm:
            return PipelineResult(reply=CANNED_UNKNOWN)

        # 1. Primary Cognitive Planner: LLM chat with full multimodal context
        if self.llm and self.llm.status != "offline":
            try:
                speech, timeline, order, thought, task_title, camera_cmd, audio_cmd = self.llm.chat(
                    self.actions, self.animations, text, history=history or [], image_b64=image_b64, memory_block=memory_block
                )

                # Check if this requires multi-step compound graph execution
                is_compound = len(timeline) > 1 or any(
                    s.get("type") in ("perception", "condition", "tool") or "if" in s for s in timeline
                )

                return PipelineResult(
                    timeline=timeline,
                    reply=speech,
                    order=order,
                    mode="compound" if is_compound else "standard",
                    thought=thought,
                    task_title=task_title or "Task",
                    goal_text=text,
                    camera_cmd=camera_cmd,
                    audio_cmd=audio_cmd,
                )
            except Exception as e:
                log.warning("Primary LLM reasoning failed, dropping to offline keyword fallback: %s", e)

        # 2. Safe Deterministic Offline Fallback (When LLM is offline)
        action = match_action(text, self.actions)
        if action:
            return PipelineResult(
                timeline=[{"type": "action", "id": action["id"]}],
                reply=action.get("reply", ""),
                task_title=action.get("name", "Action"),
            )
        return PipelineResult(reply=CANNED_UNKNOWN)

    def execute(
        self,
        result: PipelineResult,
        embodied_agent: Optional[Any],
        on_cmd: Callable[[Dict[str, Any]], None],
        on_audio: Callable[[Dict[str, Any]], None],
        on_tts_text: Callable[[str], float],
        on_ai_reply: Callable[[str], None],
        on_action_directive: Optional[Callable[[str], None]],
        on_agent_event: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_cam_cmd: Optional[Callable[[Dict[str, Any]], None]] = None,
        wait_for_audio_fn: Optional[Callable[[float], None]] = None,
        abort_event: Optional[threading.Event] = None,
    ):
        abort = abort_event or threading.Event()

        # 1. Multi-Step Compound Graph Path
        if result.mode == "compound" and embodied_agent:
            embodied_agent.run_procedural_task(result.goal_text, initial_plan={
                "task_title": result.task_title,
                "steps": result.timeline,
                "completion_speech": result.reply,
            })
            return

        timeline = result.timeline
        order = result.order or "tts_first"
        reply = result.reply or ""
        task_title = result.task_title or "Task"

        # 2. Hardware Camera Tuning & Clamping
        if result.camera_cmd and on_cam_cmd:
            cam_payload: Dict[str, Any] = {"type": "camera"}
            c = result.camera_cmd
            if "flash" in c or "lamp" in c or "led" in c:
                val = c.get("flash", c.get("lamp", c.get("led", 0)))
                try:
                    cam_payload["flash"] = max(0, min(100, int(val)))
                except (ValueError, TypeError):
                    pass
            if "quality" in c:
                try:
                    cam_payload["quality"] = max(0, min(63, int(c["quality"])))
                except (ValueError, TypeError):
                    pass
            if "brightness" in c:
                try:
                    cam_payload["brightness"] = max(-2, min(2, int(c["brightness"])))
                except (ValueError, TypeError):
                    pass
            if "contrast" in c:
                try:
                    cam_payload["contrast"] = max(-2, min(2, int(c["contrast"])))
                except (ValueError, TypeError):
                    pass
            if "saturation" in c:
                try:
                    cam_payload["saturation"] = max(-2, min(2, int(c["saturation"])))
                except (ValueError, TypeError):
                    pass
            if "special_effect" in c:
                try:
                    cam_payload["special_effect"] = max(0, min(6, int(c["special_effect"])))
                except (ValueError, TypeError):
                    pass
            if "crop" in c and isinstance(c["crop"], (list, tuple)) and len(c["crop"]) == 4:
                try:
                    sx, sy, w, h = [int(v) for v in c["crop"]]
                    sx = max(0, min(640, sx))
                    sy = max(0, min(480, sy))
                    w = max(32, min(640 - sx, w))
                    h = max(32, min(480 - sy, h))
                    cam_payload["crop"] = [sx, sy, w, h]
                except (ValueError, TypeError):
                    pass
            on_cam_cmd(cam_payload)

        # 3. Audio & Expressive Sounds
        if result.audio_cmd and on_audio:
            a = result.audio_cmd
            if "action" in a:
                on_audio(a)
            elif "alarm" in a:
                on_audio({"action": "alarm", "payload": str(a["alarm"])})
            elif "beep" in a:
                on_audio({"action": "beep"})

        if on_agent_event and result.thought:
            on_agent_event({
                "stage": "thinking",
                "title": f"Task: {task_title}",
                "thought": result.thought,
            })

        if on_agent_event and timeline:
            steps = []
            for idx, s in enumerate(timeline):
                label = s.get("id") or s.get("name") or s.get("type", f"Step {idx+1}")
                steps.append({"index": idx, "label": label.replace("_", " ").title(), "type": s.get("type", "action")})
            on_agent_event({
                "stage": "plan",
                "title": f"Task: {task_title}",
                "thought": result.thought or "Coordinating kinematics",
                "steps": steps,
            })

        def run_timeline():
            for step in timeline:
                if abort.is_set():
                    break
                stype = step.get("type", "action")
                act_id = (step.get("id") or step.get("action") or "").lower()
                dur_ms = step.get("duration_ms", 2500)
                params = step.get("params") or {}
                anim_key = normalize_animation_name(act_id)

                # Keyframe Animation Sequences
                if stype in ("gesture", "sequence") or (anim_key in self.animations):
                    target_anim = anim_key or act_id
                    if target_anim in self.animations:
                        anim = self.animations[target_anim]
                        compiled_kfs, total_ms = compile_animation_sequence(anim, duration_override_ms=dur_ms)
                        seq_payload = {"type": "sequence", "name": target_anim, "duration_ms": total_ms, "keyframes": compiled_kfs}
                        on_cmd(seq_payload)
                        if on_action_directive:
                            on_action_directive(seq_payload)
                        time.sleep(total_ms / 1000.0)
                        continue

                # Action Preset Fallback
                act = action_by_id(self.actions, act_id)
                if act and act.get("topic") in ("audio", "cmd") and act.get("payload", {}).get("type") != "motion":
                    on_cmd(act["payload"])
                    if on_action_directive:
                        on_action_directive(act["payload"])
                    if dur_ms > 0:
                        time.sleep(dur_ms / 1000.0)
                    continue

                # Kinematics & Body Pose Execution
                base_act = action_by_id(self.actions, act_id)
                resolved_id = act_id
                resolved_name = act_id.replace("_", " ").title()

                if base_act and base_act.get("payload", {}).get("type") == "motion":
                    payload = dict(base_act["payload"])
                    resolved_id = base_act["id"]
                    resolved_name = base_act.get("name", resolved_name)
                else:
                    is_spin = "spin" in act_id or "rotate" in act_id
                    is_left = "left" in act_id
                    is_right = "right" in act_id
                    is_back = "backward" in act_id or "back" in act_id
                    is_pose = stype == "pose" or any(k in params for k in ("pos_z", "pos_x", "pos_y", "roll", "pitch", "yaw"))

                    if is_pose and "vx" not in params and "omega" not in params:
                        default_vx, default_omega = 0, 0
                        resolved_id = "pose"
                    elif is_spin:
                        default_vx, default_omega = 0, 50
                        resolved_id = "spin"
                    elif is_left:
                        default_vx, default_omega = 0, -25
                        resolved_id = "turn_left"
                    elif is_right:
                        default_vx, default_omega = 0, 25
                        resolved_id = "turn_right"
                    elif is_back:
                        default_vx, default_omega = -40, 0
                        resolved_id = "walk_backward"
                    else:
                        default_vx, default_omega = 40, 0
                        resolved_id = "walk_forward"

                    payload = {
                        "type": "motion",
                        "gait": "tripod",
                        "vx": default_vx,
                        "vy": 0,
                        "omega": default_omega,
                        "step_height": 35,
                        "cycle_time": 0.8,
                        "hip_stance": 20,
                        "leg_stance": 0,
                        "pos_x": 0, "pos_y": 0, "pos_z": 0,
                        "roll": 0, "pitch": 0, "yaw": 0,
                    }

                # Clamp Kinematics Parameters to Safe Physical Envelopes
                for k in ("vx", "vy", "omega", "step_height", "cycle_time", "leg_stance", "pos_x", "pos_y", "pos_z", "roll", "pitch", "yaw"):
                    if k in params:
                        try:
                            val = float(params[k])
                            if k == "pos_z":
                                val = max(-40.0, min(50.0, val))
                            elif k in ("pos_x", "pos_y"):
                                val = max(-30.0, min(30.0, val))
                            elif k in ("roll", "pitch", "yaw"):
                                val = max(-15.0, min(15.0, val))
                            payload[k] = val
                        except (ValueError, TypeError):
                            pass

                if "gait" in params and params["gait"]:
                    payload["gait"] = str(params["gait"])

                run_dur = dur_ms or 2500
                payload["duration_ms"] = run_dur

                directive_payload = {
                    "type": "directive",
                    "action_id": resolved_id,
                    "name": resolved_name,
                    "duration_ms": run_dur,
                    "payload": payload,
                }

                on_cmd(payload)
                if on_action_directive:
                    on_action_directive(directive_payload)

                time.sleep(run_dur / 1000.0)

                # Stop velocity while holding final body stance on physical hardware
                stop = dict(payload)
                stop["vx"] = stop["vy"] = stop["omega"] = 0
                on_cmd(stop)

        try:
            if order == "action_first":
                if timeline:
                    run_timeline()
                if reply and not abort.is_set():
                    on_ai_reply(reply)
                    on_tts_text(reply)
            elif order == "simultaneous":
                t = threading.Thread(target=run_timeline, daemon=True)
                if timeline:
                    t.start()
                if reply and not abort.is_set():
                    on_ai_reply(reply)
                    on_tts_text(reply)
                if timeline:
                    t.join()
            else:
                if reply and not abort.is_set():
                    on_ai_reply(reply)
                    tts_dur = on_tts_text(reply) or 0.0
                    if wait_for_audio_fn:
                        wait_for_audio_fn(tts_dur + 0.5)
                if timeline and not abort.is_set():
                    run_timeline()
        finally:
            if on_agent_event:
                on_agent_event({"stage": "done", "title": f"Task: {task_title}", "thought": "Execution complete."})