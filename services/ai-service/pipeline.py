# services/ai-service/pipeline.py
import logging
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from action_parser import (
    action_by_id,
    compile_animation_sequence,
    compile_dynamic_joint_sequence,
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


import json


class Pipeline:
    def __init__(
        self,
        actions: List[Dict[str, Any]],
        llm: Optional[Any] = None,
        stt: Optional[Any] = None,
        animations: Optional[Dict[str, Any]] = None,
        skill_manager: Optional[Any] = None,
    ):
        self.actions = actions
        self.animations = animations or load_animations()
        self.llm = llm
        self.stt = stt
        self.skill_manager = skill_manager

    def decide(
        self,
        text: str,
        history: Optional[List[Dict[str, Any]]] = None,
        image_b64: Optional[str] = None,
        memory_block: str = "",
        state_block: str = "",
        dst_block: str = "",
        skills_block: str = "",
    ) -> PipelineResult:
        norm = (text or "").lower().strip().replace("foward", "forward")
        if not norm:
            return PipelineResult(reply=CANNED_UNKNOWN)

        # 1. Primary Cognitive Planner: LLM chat with full multimodal context
        if self.llm and self.llm.status != "offline":
            try:
                skill_fn = self.skill_manager.execute_skill if self.skill_manager else None
                speech, timeline, order, thought, task_title, camera_cmd, audio_cmd, tool_call = self.llm.chat(
                    self.actions, self.animations, text, history=history or [], image_b64=image_b64,
                    memory_block=memory_block, state_block=state_block, dst_block=dst_block, skills_block=skills_block,
                    skill_executor=skill_fn
                )

                # Fallback support for prompt-emulated tool calls if model returned JSON tool_call
                if tool_call and skill_fn:
                    tool_name = tool_call.get("name", "")
                    tool_args = tool_call.get("args", {})
                    log.info("Executing Fallback Emulated Tool -> %s with args: %s", tool_name, tool_args)
                    skill_fn(tool_name, tool_args)

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
        timeline = result.timeline or []
        order = result.order or "tts_first"
        reply = result.reply or ""
        task_title = result.task_title or "Task"

        # 1. Multi-Step Compound Graph Path
        if result.mode == "compound" and embodied_agent:
            if order == "tts_first" and reply and not abort.is_set():
                on_ai_reply(reply)
                tts_dur = on_tts_text(reply) or 0.0
                if wait_for_audio_fn:
                    wait_for_audio_fn(tts_dur + 0.5)

            embodied_agent.run_procedural_task(result.goal_text, initial_plan={
                "task_title": task_title,
                "steps": timeline,
                "completion_speech": "" if order == "tts_first" else reply,
            })
            return

        # 2. Hardware Camera Tuning & Presets
        if result.camera_cmd and on_cam_cmd:
            cam_payload: Dict[str, Any] = {"type": "camera"}
            c = result.camera_cmd

            if "preset" in c and c["preset"]:
                cam_payload["preset"] = str(c["preset"]).strip().lower()

            if "flash" in c or "lamp" in c or "led" in c:
                val = c.get("flash", c.get("lamp", c.get("led", 0)))
                try:
                    cam_payload["flash"] = max(0, min(100, int(val)))
                except (ValueError, TypeError):
                    pass
            if "fps" in c:
                try:
                    cam_payload["fps"] = max(1, min(30, int(c["fps"])))
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
            if "exposure_ctrl" in c:
                cam_payload["exposure_ctrl"] = bool(c["exposure_ctrl"])
            if "ae_level" in c:
                try:
                    cam_payload["ae_level"] = max(-2, min(2, int(c["ae_level"])))
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

        # 3. Master Audio, Presets & Expressive Sounds
        if result.audio_cmd and on_audio:
            a = result.audio_cmd

            # Master Hardware Volume scaling
            if "volume" in a:
                try:
                    vol = max(0.0, min(1.0, float(a["volume"])))
                    on_audio({"action": "volume", "volume": vol})
                except (ValueError, TypeError):
                    pass

            # Audio Presets
            if "preset" in a and a["preset"]:
                p_name = str(a["preset"]).strip().lower()
                if p_name in ("stealth", "mute", "quiet"):
                    on_audio({"action": "volume", "volume": 0.0})
                elif p_name in ("alert", "loud"):
                    on_audio({"action": "volume", "volume": 0.85})
                elif p_name in ("normal", "default"):
                    on_audio({"action": "volume", "volume": 0.35})

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
                p = s.get("params") or {}
                raw_label = s.get("desc") or s.get("name") or s.get("id") or s.get("type") or f"Action {idx+1}"
                dur_s = p.get("duration_s") or s.get("duration_s") or (
                    (p.get("duration_ms") or s.get("duration_ms", 0)) / 1000.0
                )
                dur_tag = f" ({int(dur_s)}s)" if dur_s and dur_s >= 1 and "(" not in raw_label else ""
                clean_label = f"{raw_label.replace('_', ' ').title()}{dur_tag}"
                steps.append({"index": idx, "label": clean_label, "type": s.get("type", "action")})
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

                # Generalized Dynamic Joint / Leg Overrides
                if stype in ("joints", "joint_override") or "joints" in params or "joints" in step:
                    raw_joints = params.get("joints") or step.get("joints") or {}
                    seq_payload = compile_dynamic_joint_sequence(raw_joints, dur_ms=dur_ms, auto_balance=params.get("auto_balance", False))
                    on_cmd(seq_payload)
                    if on_action_directive:
                        on_action_directive(seq_payload)
                    time.sleep(seq_payload["duration_ms"] / 1000.0)
                    continue

                # Action Preset Fallback (Bypass if the LLM provided custom kinematics)
                has_kinematics = stype in ("pose", "gait") or any(k in params for k in ("pos_z", "pos_x", "pos_y", "roll", "pitch", "yaw", "vx", "vy", "omega"))
                act = action_by_id(self.actions, act_id)
                
                if not has_kinematics and act and act.get("topic") in ("audio", "cmd") and act.get("payload", {}).get("type") != "motion":
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

                # Extract gait aliases (e.g. "hip_swing" -> "hip_stance", "speed" -> "cycle_time")
                if "hip_swing" in params and "hip_stance" not in params:
                    params["hip_stance"] = params["hip_swing"]
                if "swing" in params and "hip_stance" not in params:
                    params["hip_stance"] = params["swing"]
                if "lift" in params and "step_height" not in params:
                    params["step_height"] = params["lift"]
                if "speed" in params and "cycle_time" not in params:
                    params["cycle_time"] = params["speed"]

                # Clamp Kinematics & Gait Parameters to Safe Physical Envelopes
                for k in ("vx", "vy", "omega", "step_height", "cycle_time", "hip_stance", "leg_stance", "pos_x", "pos_y", "pos_z", "roll", "pitch", "yaw"):
                    if k in params:
                        try:
                            val = float(params[k])
                            if k == "pos_z":
                                val = max(-40.0, min(50.0, val))
                            elif k in ("pos_x", "pos_y"):
                                val = max(-30.0, min(30.0, val))
                            elif k in ("roll", "pitch", "yaw"):
                                val = max(-15.0, min(15.0, val))
                            elif k == "hip_stance":
                                val = max(0.0, min(45.0, val))
                            elif k == "leg_stance":
                                val = max(-30.0, min(40.0, val))
                            elif k == "step_height":
                                val = max(15.0, min(65.0, val))
                            elif k == "cycle_time":
                                val = max(0.4, min(2.5, val))
                            payload[k] = val
                        except (ValueError, TypeError):
                            pass

                if "gait" in params and params["gait"]:
                    payload["gait"] = str(params["gait"])

                run_dur = dur_ms or 2500
                payload["duration_ms"] = run_dur
                payload["lease_ms"] = 350

                directive_payload = {
                    "type": "directive",
                    "action_id": resolved_id,
                    "name": resolved_name,
                    "duration_ms": run_dur,
                    "payload": payload,
                }

                if on_action_directive:
                    on_action_directive(directive_payload)

                # 20 Hz Leased Dispatch Loop with Preemption Check
                dur_s = run_dur / 1000.0
                start_t = time.time()
                while time.time() - start_t < dur_s:
                    if abort.is_set():
                        break
                    on_cmd(payload)
                    time.sleep(0.05)

                # Stop velocity while holding final body stance on physical hardware
                stop = dict(payload)
                stop["vx"] = stop["vy"] = stop["omega"] = 0
                stop["lease_ms"] = 0
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