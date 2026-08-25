# services/ai-service/embodied_agent.py
import json
import logging
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from action_parser import compile_animation_sequence, load_animations, normalize_animation_name

log = logging.getLogger("ai.embodied")


def extract_clean_objective(raw_text: str) -> str:
    cleaned = raw_text.strip()
    cleaned = re.sub(
        r"^(ok|okay|hey|hi|hello|now|so|well|can you|could you|please|try to|how about|how about now)\b[,\s!]*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    ).strip()
    if len(cleaned) > 36:
        cleaned = cleaned[:33].rstrip() + "..."
    return cleaned[0].upper() + cleaned[1:] if cleaned else "Procedural Task"


def safe_extract_json(raw_text: str) -> Any:
    """Robustly extracts JSON (dict or list) from raw LLM text with or without markdown fences."""
    cleaned = (raw_text or "").strip()

    # 1. Match code blocks ```json ... ``` or ``` ... ```
    match = re.search(r"```(?:json)?\s*([\{\[].*?[\}\]])\s*```", cleaned, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1).strip())
        except Exception:
            pass

    # 2. Try direct json.loads on entire string
    try:
        return json.loads(cleaned)
    except Exception:
        pass

    # 3. Find outermost matching braces or brackets
    start_obj, end_obj = cleaned.find("{"), cleaned.rfind("}")
    start_arr, end_arr = cleaned.find("["), cleaned.rfind("]")

    candidates = []
    if start_obj != -1 and end_obj != -1 and end_obj > start_obj:
        candidates.append((start_obj, end_obj + 1))
    if start_arr != -1 and end_arr != -1 and end_arr > start_arr:
        candidates.append((start_arr, end_arr + 1))

    candidates.sort(key=lambda x: x[0])
    for start, end in candidates:
        try:
            return json.loads(cleaned[start:end].strip())
        except Exception:
            pass

    return {}


class EmbodiedAgent:
    def __init__(
        self,
        llm_client: Any,
        fetch_snapshot_fn: Callable[[], Optional[str]],
        publish_s3_cmd_fn: Callable[[Dict[str, Any]], None],
        publish_cam_cmd_fn: Callable[[Dict[str, Any]], None],
        speak_fn: Callable[[str], float],
        reply_fn: Callable[[str], None],
        event_fn: Optional[Callable[[Dict[str, Any]], None]] = None,
        directive_fn: Optional[Callable[[Any], None]] = None,
        abort_event: Optional[threading.Event] = None,
    ):
        self.llm = llm_client
        self.fetch_snapshot = fetch_snapshot_fn
        self.publish_s3_cmd = publish_s3_cmd_fn
        self.publish_cam_cmd = publish_cam_cmd_fn
        self.speak = speak_fn
        self.reply = reply_fn
        self.event = event_fn
        self.directive = directive_fn
        self.abort_event = abort_event or threading.Event()
        self.animations = load_animations()

    def abort(self):
        self.abort_event.set()
        self._stop_motion()

    def _sleep_interruptible(self, duration_s: float) -> bool:
        start = time.time()
        while time.time() - start < duration_s:
            if self.abort_event.is_set():
                return False
            time.sleep(0.02)
        return True

    def _stop_motion(self):
        self.publish_s3_cmd({"type": "motion", "gait": "tripod", "vx": 0, "vy": 0, "omega": 0})

    def _move(
        self,
        vx: float = 0,
        omega: float = 0,
        duration_s: float = 1.5,
        vy: float = 0,
        gait: str = "tripod",
        step_height: float = 38,
        cycle_time: float = 0.8,
        hip_stance: float = 20,
        leg_stance: float = 0,
        pos_z: float = 0,
        pos_x: float = 0,
        pos_y: float = 0,
        roll: float = 0,
        pitch: float = 0,
        yaw: float = 0,
        action_id: Optional[str] = None,
        action_name: Optional[str] = None,
    ) -> bool:
        if self.abort_event.is_set():
            return False

        duration_s = max(0.2, min(30.0, duration_s))
        vx = max(-60.0, min(60.0, vx))
        vy = max(-60.0, min(60.0, vy))
        omega = max(-50.0, min(50.0, omega))
        pos_z = max(-40.0, min(50.0, pos_z))
        pos_x = max(-30.0, min(30.0, pos_x))
        pos_y = max(-30.0, min(30.0, pos_y))
        roll = max(-15.0, min(15.0, roll))
        pitch = max(-15.0, min(15.0, pitch))
        yaw = max(-20.0, min(20.0, yaw))

        is_locomotion = (vx != 0 or vy != 0 or omega != 0)
        resolved_id = action_id or ("spin" if (vx == 0 and omega > 40) else "turn_right" if omega > 0 else "turn_left" if omega < 0 else "walk_backward" if vx < 0 else "walk_forward" if vx > 0 else "pose")
        resolved_name = action_name or resolved_id.replace("_", " ").title()

        motion_payload = {
            "type": "motion",
            "gait": gait,
            "vx": vx,
            "vy": vy,
            "omega": omega,
            "step_height": step_height,
            "cycle_time": cycle_time,
            "hip_stance": hip_stance,
            "leg_stance": leg_stance,
            "pos_x": pos_x,
            "pos_y": pos_y,
            "pos_z": pos_z,
            "roll": roll,
            "pitch": pitch,
            "yaw": yaw,
            "duration_ms": int(duration_s * 1000),
        }
        self.publish_s3_cmd(motion_payload)
        if self.directive:
            self.directive({
                "type": "directive",
                "action_id": resolved_id,
                "name": resolved_name,
                "duration_ms": int(duration_s * 1000),
                "payload": motion_payload,
            })
        ok = self._sleep_interruptible(duration_s)
        if is_locomotion:
            self._stop_motion()
        self._sleep_interruptible(0.05)
        return ok

    def _execute_gesture(self, gesture_name: str):
        """Executes any dynamic animation defined in animations.json."""
        if not gesture_name or gesture_name.lower() in ("none", "null", "false"):
            return

        anim_key = normalize_animation_name(gesture_name)
        if anim_key in self.animations:
            anim = self.animations[anim_key]
            compiled_kfs, total_ms = compile_animation_sequence(anim)
            payload = {
                "type": "sequence",
                "name": anim_key,
                "duration_ms": total_ms,
                "keyframes": compiled_kfs,
            }
            self.publish_s3_cmd(payload)
            if self.directive:
                self.directive({
                    "type": "directive",
                    "action_id": anim_key,
                    "name": anim_key.replace("_", " ").title(),
                    "duration_ms": total_ms,
                    "payload": payload,
                })
            self._sleep_interruptible(total_ms / 1000.0)
        else:
            log.warning("Requested gesture '%s' not found in animations.json", gesture_name)

    def run_procedural_task(self, user_goal: str, initial_plan: Optional[Dict[str, Any]] = None):
        """Executes multi-step compound tasks without forced camera dependencies."""
        self.abort_event.clear()
        task_title = extract_clean_objective(user_goal)
        log.info("[DYNAMIC TASK GRAPH]: %s", user_goal)

        valid_gestures = list(self.animations.keys())
        has_visual_intent = any(k in user_goal.lower() for k in ("see", "look", "inspect", "camera", "if you see", "if you can see", "find", "detect"))

        # 1. Compile User Command into Execution Plan if not pre-compiled
        plan_prompt = f"""You are the kinematic task compiler for an agile 6-legged physical Hexapod robot.
The user's compound command is: "{user_goal}".
Available physical animations: {valid_gestures}.

Decompose this into a chronological JSON execution plan:
{{
  "task_title": "Short 2-4 word title",
  "has_visual_inspection": { "true" if has_visual_intent else "false" },
  "visual_inspection_query": "Specific question to inspect if has_visual_inspection is true, else ''",
  "completion_speech": "Warm spoken confirmation after completion",
  "steps": [
    {{
      "desc": "Short description (e.g. 'Walking forward', 'Turning on flashlight', 'Looking around', 'Dancing')",
      "type": "walk_forward | walk_backward | rotate_left | rotate_right | pose | gesture | camera | speak | audio | pause",
      "gesture": "Name from {valid_gestures} if type is gesture",
      "camera_cmd": {{ "preset": "night_vision|inspection|default", "flash": 80, "special_effect": 0 }},
      "text": "Text to speak if type is speak",
      "audio_track": "Track or alarm name if type is audio",
      "vx": 45,
      "omega": 0,
      "duration_s": 2.5
    }}
  ]
}}

RULES:
- 'turn left', 'rotate left', 'twist left 90': type='rotate_left', vx=0, omega=-40, duration_s=2.25
- 'turn right', 'rotate right', 'twist right 90': type='rotate_right', vx=0, omega=40, duration_s=2.25
- 'twist N degrees': if angle is small (<40 deg) without direction, type='pose', pos_z=0, yaw=N
- 'walk', 'walk forward': type='walk_forward', vx=45, omega=0, duration_s=2.5
- 'dance', 'wiggle': type='gesture', gesture='dance'
- 'wave': type='gesture', gesture='wave'
- 'baby shark': type='audio', audio_track='baby_shark'
"""
        plan = initial_plan
        if not plan:
            client = self.llm._ensure()
            try:
                resp = client.chat.completions.create(
                    model=self.llm.model,
                    messages=[{"role": "user", "content": plan_prompt}],
                    response_format={"type": "json_object"},
                    temperature=0.2,
                    max_tokens=1024,
                )
                raw_content = resp.choices[0].message.content or "{}"
                plan = safe_extract_json(raw_content)
            except Exception as e:
                log.error("Plan compilation error: %s", e)
                self.fast_visual_qa(user_goal)
                return

        steps = []
        is_visual = False
        visual_query = ""
        completion_speech = "All actions completed."

        if isinstance(plan, dict):
            task_title = plan.get("task_title") or plan.get("title") or task_title
            is_visual = bool(plan.get("has_visual_inspection", False)) or has_visual_intent
            visual_query = plan.get("visual_inspection_query", "")
            completion_speech = plan.get("completion_speech", completion_speech)
            raw_steps = plan.get("steps") or plan.get("motion_steps") or plan.get("timeline") or []
            if isinstance(raw_steps, list):
                steps = raw_steps

        def format_step_label(step_obj: dict, i: int) -> str:
            # 1. Use explicit description if provided and not generic
            desc_val = str(step_obj.get("desc") or "").strip()
            if desc_val and not re.match(r"^step\s*\d+$", desc_val, re.IGNORECASE):
                return desc_val

            # 2. Extract kinematics and parameters
            p = step_obj.get("params") or {}
            raw_id = str(step_obj.get("id") or step_obj.get("action") or step_obj.get("type") or "").lower()
            dur_s = p.get("duration_s") or step_obj.get("duration_s") or (
                (p.get("duration_ms") or step_obj.get("duration_ms", 0)) / 1000.0
            )
            dur_tag = f" ({int(dur_s)}s)" if dur_s and dur_s >= 1 else ""

            pitch = float(p.get("pitch", step_obj.get("pitch", 0.0)))
            yaw = float(p.get("yaw", step_obj.get("yaw", 0.0)))
            roll = float(p.get("roll", step_obj.get("roll", 0.0)))
            pos_z = float(p.get("pos_z", step_obj.get("pos_z", 0.0)))

            known_gestures = {
                "wave": "Wave Hello",
                "cheer": "Cheer Yay",
                "dance": "Dance Groove",
                "bow": "Take a Bow",
                "stretch": "Stretch Limbs",
                "pushups": "Push-ups Workout",
                "look_around": "Look Around",
            }
            for gkey, gtitle in known_gestures.items():
                if gkey in raw_id:
                    return gtitle

            vx = float(p.get("vx", step_obj.get("vx", 0.0)))
            omega = float(p.get("omega", step_obj.get("omega", 0.0)))
            
            loco_parts = []
            if vx > 0 or "forward" in raw_id or "walk" in raw_id:
                loco_parts.append("Walk Forward")
            elif vx < 0 or "backward" in raw_id:
                loco_parts.append("Walk Backward")
            if omega > 0 or "turn_right" in raw_id or "rotate" in raw_id or "spin" in raw_id:
                loco_parts.append("Rotate Right")
            elif omega < 0 or "turn_left" in raw_id:
                loco_parts.append("Rotate Left")
                
            if loco_parts:
                base = " & ".join(loco_parts) + dur_tag
                if pos_z != 0:
                    return f"{base} (Z: {int(pos_z)}mm)"
                return base

            pose_parts = []
            if yaw != 0:
                dir_yaw = "Left" if yaw < 0 else "Right"
                pose_parts.append(f"Twist {dir_yaw} {abs(int(yaw))}°")
            if pitch != 0:
                dir_pitch = "Back" if pitch < 0 or "back" in raw_id or "tilt" in raw_id or "lean" in raw_id else "Forward"
                pose_parts.append(f"Lean {dir_pitch} {abs(int(pitch))}°")
            if roll != 0:
                pose_parts.append(f"Tilt Roll {int(roll)}°")
            if pos_z != 0:
                pose_parts.append(f"Height ({int(pos_z)}mm)")

            if pose_parts:
                return " & ".join(pose_parts) + dur_tag

            track = str(p.get("track") or step_obj.get("audio_track") or step_obj.get("track") or "")
            if track:
                return f"Play: {track.replace('_', ' ').title()}"

            if raw_id:
                return f"{raw_id.replace('_', ' ').title()}{dur_tag}"

            return f"Action {i + 1}{dur_tag}"

        ui_steps = []
        for idx, s in enumerate(steps):
            ui_steps.append({"index": idx, "label": format_step_label(s, idx), "type": s.get("type", "motion")})
        if is_visual:
            ui_steps.append({"index": len(ui_steps), "label": "Inspect Camera View", "type": "vision"})
            ui_steps.append({"index": len(ui_steps), "label": "Evaluate Branching Conditions", "type": "action"})

        if self.event:
            self.event({
                "stage": "plan",
                "title": f"Task: {task_title}",
                "thought": f"Executing task graph for: '{user_goal}'",
                "steps": ui_steps,
                "active_step": 0,
            })

        # 2. Execute Physical & Logical Steps Chronologically
        for step_idx, step in enumerate(steps):
            if self.abort_event.is_set():
                return
            if self.event:
                self.event({
                    "stage": "step_progress",
                    "title": f"Task: {task_title}",
                    "active_step": step_idx,
                    "thought": f"Executing: {step.get('desc', f'Step {step_idx+1}')}",
                    "steps": ui_steps,
                })

            p = step.get("params") or {}
            stype = str(step.get("type", "walk_forward")).lower()
            act_id = str(step.get("id") or step.get("action") or "").lower()
            sdesc = str(step.get("desc") or "").strip()

            dur = float(p.get("duration_s", step.get("duration_s", (p.get("duration_ms", step.get("duration_ms", 2500)) / 1000.0))))

            # Extract kinematic parameters with fallbacks from both p and step
            vx = float(p.get("vx", step.get("vx", 0.0)))
            vy = float(p.get("vy", step.get("vy", 0.0)))
            omega = float(p.get("omega", step.get("omega", 0.0)))
            pos_z = float(p.get("pos_z", step.get("pos_z", 0.0)))
            pos_x = float(p.get("pos_x", step.get("pos_x", 0.0)))
            pos_y = float(p.get("pos_y", step.get("pos_y", 0.0)))
            roll = float(p.get("roll", step.get("roll", 0.0)))
            pitch = float(p.get("pitch", step.get("pitch", 0.0)))
            yaw = float(p.get("yaw", step.get("yaw", 0.0)))
            hip_stance = float(p.get("hip_stance", step.get("hip_stance", 20.0)))
            leg_stance = float(p.get("leg_stance", step.get("leg_stance", 0.0)))
            step_height = float(p.get("step_height", step.get("step_height", 38.0)))
            cycle_time = float(p.get("cycle_time", step.get("cycle_time", 0.8)))
            gait = str(p.get("gait", step.get("gait", "tripod")))

            anim_candidate = normalize_animation_name(act_id or stype)
            is_anim = (
                stype in ("gesture", "sequence")
                or anim_candidate in self.animations
                or act_id in self.animations
            )

            if is_anim:
                target_anim = anim_candidate if anim_candidate in self.animations else act_id
                self._execute_gesture(target_anim)
            elif stype in ("camera", "cam", "flashlight", "light"):
                cam_obj = step.get("camera_cmd") or p.get("camera_cmd") or {}
                if not cam_obj and "flash" in p:
                    cam_obj = {"flash": p["flash"]}
                if cam_obj:
                    cam_payload = {"type": "camera", **cam_obj}
                    self.publish_cam_cmd(cam_payload)
                    self._sleep_interruptible(0.2)
            elif stype in ("speak", "say", "count"):
                speak_text = str(step.get("text") or p.get("text") or sdesc)
                if speak_text:
                    self.reply(speak_text)
                    tts_dur = self.speak(speak_text) or 0.0
                    self._sleep_interruptible(max(0.5, tts_dur + 0.3))
            elif stype in ("audio", "sound"):
                track = str(p.get("track") or step.get("audio_track") or step.get("track") or "curious")
                if "baby_shark" in track.lower():
                    self.reply("Playing Baby Shark!")
                    self.speak("Baby shark doo doo doo doo doo doo!")
                else:
                    self._on_audio({"action": "alarm", "payload": track})
            elif stype in ("pause", "wait"):
                self._sleep_interruptible(dur)
            else:
                # Disambiguate zero-velocity intents
                if vx == 0 and omega == 0 and vy == 0 and not any(k != 0 for k in (pos_z, pos_x, pos_y, roll, pitch, yaw)):
                    if "forward" in act_id or "forward" in sdesc or "walk" in act_id:
                        vx = 40.0
                    elif "backward" in act_id or "back" in act_id or "backward" in sdesc:
                        vx = -40.0
                    elif "rotate" in act_id or "rotate" in sdesc or "spin" in act_id:
                        omega = 40.0
                    elif "left" in act_id or "left" in sdesc:
                        omega = -40.0
                    elif "right" in act_id or "right" in sdesc:
                        omega = 40.0

                resolved_act_id = act_id or ("pose" if (vx == 0 and omega == 0 and vy == 0) else None)
                if not self._move(
                    vx=vx, omega=omega, duration_s=dur, vy=vy, gait=gait,
                    step_height=step_height, cycle_time=cycle_time,
                    hip_stance=hip_stance, leg_stance=leg_stance,
                    pos_z=pos_z, pos_x=pos_x, pos_y=pos_y,
                    roll=roll, pitch=pitch, yaw=yaw,
                    action_id=resolved_act_id,
                    action_name=sdesc or None,
                ):
                    return

        # 3. Visual Perception Branching (ONLY if plan requested it)
        if is_visual:
            eval_step_idx = len(steps)
            if self.event:
                self.event({
                    "stage": "step_progress",
                    "title": f"Task: {task_title}",
                    "active_step": eval_step_idx,
                    "thought": "Inspecting camera from new position...",
                    "steps": ui_steps,
                })

            time.sleep(0.3)
            img_b64 = self.fetch_snapshot()
            if not img_b64:
                self.reply("Reached target position, but camera feed is offline.")
                return

            branch_prompt = f"""You are the visual cortex of a 6-legged robot.
User goal: "{user_goal}".
Specific query: "{visual_query or user_goal}".
Available gestures: {valid_gestures} (or 'none').

Inspect image and return JSON:
{{
  "observation": "1-sentence visual description",
  "chosen_gesture": "Name from {valid_gestures} or 'none'",
  "speech": "First-person reply explaining what you saw and what you are doing"
}}"""
            eval_raw = self.llm.inspect_vision(prompt=branch_prompt, image_b64=img_b64)
            branch_res = safe_extract_json(eval_raw)

            obs = branch_res.get("observation", str(eval_raw)) if isinstance(branch_res, dict) else str(eval_raw)
            chosen_gesture = str(branch_res.get("chosen_gesture", "none")).strip().lower() if isinstance(branch_res, dict) else "none"
            speech = str(branch_res.get("speech", f"I see: {obs}")).strip() if isinstance(branch_res, dict) else obs

            self.reply(speech)
            self.speak(speech)
            if chosen_gesture and chosen_gesture != "none":
                self._execute_gesture(chosen_gesture)
        else:
            # Pure motion/gesture completion
            if completion_speech and not self.abort_event.is_set():
                self.reply(completion_speech)

        if self.event:
            self.event({
                "stage": "done",
                "title": f"Task: {task_title}",
                "thought": "Execution complete.",
                "active_step": len(ui_steps),
                "steps": ui_steps,
            })

    def fast_visual_qa(self, user_question: str):
        self.abort_event.clear()
        task_title = extract_clean_objective(user_question)
        if self.event:
            self.event({"stage": "thinking", "thought": f"Inspecting view for: '{task_title}'"})

        img_b64 = self.fetch_snapshot()
        if not img_b64:
            err = "Camera buffer offline."
            self.reply(err)
            self.speak(err)
            return

        persona = getattr(self.llm, "personality", "friendly")
        prompt = f"You are a 6-legged robot ({persona}). Answer concisely in 1-2 friendly sentences based on this frame: '{user_question}'"
        observation = self.llm.inspect_vision(prompt=prompt, image_b64=img_b64)

        if not self.abort_event.is_set():
            self.reply(observation)
            self.speak(observation)
        if self.event:
            self.event({"stage": "done", "thought": "Visual inspection complete."})