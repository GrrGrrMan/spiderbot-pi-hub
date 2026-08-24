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
        step_height: float = 35,
        cycle_time: float = 0.8,
        hip_stance: float = 20,
        pos_z: float = 0,
    ) -> bool:
        if self.abort_event.is_set():
            return False

        duration_s = max(0.2, min(15.0, duration_s))
        vx = max(-60.0, min(60.0, vx))
        vy = max(-60.0, min(60.0, vy))
        omega = max(-50.0, min(50.0, omega))

        resolved_id = "spin" if (vx == 0 and omega > 40) else "turn_right" if omega > 0 else "turn_left" if omega < 0 else "walk_backward" if vx < 0 else "walk_forward"
        resolved_name = resolved_id.replace("_", " ").title()

        motion_payload = {
            "type": "motion",
            "gait": gait,
            "vx": vx,
            "vy": vy,
            "omega": omega,
            "step_height": step_height,
            "cycle_time": cycle_time,
            "hip_stance": hip_stance,
            "pos_z": pos_z,
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

    def run_procedural_task(self, user_goal: str):
        """Compiles compound instructions into an execution graph and executes it physically."""
        self.abort_event.clear()
        task_title = extract_clean_objective(user_goal)
        log.info("[DYNAMIC PROCEDURAL TASK]: %s", user_goal)

        valid_gestures = list(self.animations.keys())

        # 1. Compile User Command into Execution Plan
        plan_prompt = f"""You are the kinematic task compiler for an agile 6-legged physical Hexapod robot.
The user's compound command is: "{user_goal}".
Available physical animations: {valid_gestures}.

Decompose this into a chronological JSON execution plan:
{{
  "task_title": "Short 2-4 word title",
  "motion_steps": [
    {{
      "desc": "Short description (e.g. 'Counting to 10', 'Walking forward 2 steps', 'Turning 90 degrees left')",
      "type": "walk_forward | walk_backward | rotate_left | rotate_right | speak | pause | gesture",
      "text": "Spoken text if type is speak/count (e.g. 'Counting to 10: 1, 2, 3... 10!')",
      "gesture": "Name of animation if type is gesture (from {valid_gestures})",
      "vx": 45,
      "omega": 0,
      "duration_s": 2.5
    }}
  ],
  "visual_inspection_query": "What specific question to inspect in camera at destination (e.g. 'How many fingers is the user holding up?')"
}}

RULES FOR MOTION:
- 'turn left' or 'turn 90 degrees left': type='rotate_left', vx=0, omega=-40, duration_s=2.25
- 'turn right' or 'turn 90 degrees right': type='rotate_right', vx=0, omega=40, duration_s=2.25
- 'walk forward': type='walk_forward', vx=45, omega=0, duration_s=2.5
- 'count to N' or verbal action: type='speak', text='1, 2, 3... N!'
"""
        plan = None
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

        # Safely extract plan structure regardless of dict or list return
        motion_steps = []
        visual_query = user_goal
        if isinstance(plan, dict):
            task_title = plan.get("task_title") or plan.get("title") or task_title
            visual_query = plan.get("visual_inspection_query") or plan.get("query") or user_goal
            raw_steps = plan.get("motion_steps") or plan.get("steps") or plan.get("timeline") or []
            if isinstance(raw_steps, list):
                motion_steps = raw_steps
        elif isinstance(plan, list):
            for item in plan:
                if isinstance(item, dict):
                    motion_steps.append(item)
                elif isinstance(item, str):
                    motion_steps.append({"desc": item.replace("_", " ").title(), "type": item})

        # Fallback keyword extraction if motion parsing returned empty
        if not motion_steps:
            norm_lower = user_goal.lower()
            if "count" in norm_lower:
                motion_steps.append({"desc": "Counting to 10", "type": "speak", "text": "1, 2, 3, 4, 5, 6, 7, 8, 9, 10!"})
            if "walk" in norm_lower or "step" in norm_lower:
                motion_steps.append({"desc": "Walking forward", "type": "walk_forward", "vx": 45, "omega": 0, "duration_s": 2.5})
            if "left" in norm_lower:
                motion_steps.append({"desc": "Turning 90° left", "type": "rotate_left", "vx": 0, "omega": -40, "duration_s": 2.25})
            elif "right" in norm_lower or "turn" in norm_lower or "rotate" in norm_lower:
                motion_steps.append({"desc": "Turning 90° right", "type": "rotate_right", "vx": 0, "omega": 40, "duration_s": 2.25})

        ui_steps = []
        for idx, s in enumerate(motion_steps):
            ui_steps.append({"index": idx, "label": s.get("desc", f"Step {idx+1}"), "type": s.get("type", "motion")})
        ui_steps.append({"index": len(ui_steps), "label": "Inspect Camera & Evaluate Conditions", "type": "vision"})
        ui_steps.append({"index": len(ui_steps), "label": "Execute Resulting Action", "type": "action"})

        if self.event:
            self.event({
                "stage": "plan",
                "title": f"Task: {task_title}",
                "thought": f"Executing physical procedure: '{user_goal}'",
                "steps": ui_steps,
                "active_step": 0,
            })

        # 2. Execute Physical Steps Chronologically
        for step_idx, step in enumerate(motion_steps):
            if self.abort_event.is_set():
                return
            if self.event:
                self.event({
                    "stage": "step_progress",
                    "title": f"Task: {task_title}",
                    "active_step": step_idx,
                    "thought": f"Executing: {step.get('desc')}",
                    "steps": ui_steps,
                })

            stype = str(step.get("type", "walk_forward")).lower()
            dur = float(step.get("duration_s", 2.0))
            hip = float(step.get("hip_stance", step.get("hip_swing", 20)))
            sh = float(step.get("step_height", 35))
            ct = float(step.get("cycle_time", 0.8))
            gt = str(step.get("gait", "tripod"))
            pz = float(step.get("pos_z", 0))
            vy = float(step.get("vy", 0.0))

            if stype in ("speak", "say", "count"):
                speak_text = step.get("text") or step.get("desc") or ""
                if speak_text:
                    self.reply(speak_text)
                    tts_dur = self.speak(speak_text) or 0.0
                    self._sleep_interruptible(max(0.5, tts_dur + 0.3))
            elif stype in ("pause", "wait"):
                self._sleep_interruptible(dur)
            elif stype == "gesture" or stype in self.animations or normalize_animation_name(stype) in self.animations:
                gname = step.get("gesture") or stype
                self._execute_gesture(gname)
            else:
                if "left" in stype:
                    vx = float(step.get("vx", 0.0))
                    omega = float(step.get("omega", -40.0))
                elif "right" in stype:
                    vx = float(step.get("vx", 0.0))
                    omega = float(step.get("omega", 40.0))
                elif "backward" in stype or "back" in stype:
                    vx = float(step.get("vx", -45.0))
                    omega = float(step.get("omega", 0.0))
                else:
                    vx = float(step.get("vx", 45.0))
                    omega = float(step.get("omega", 0.0))

                if not self._move(
                    vx=vx, omega=omega, duration_s=dur,
                    vy=vy, gait=gt, step_height=sh,
                    cycle_time=ct, hip_stance=hip, pos_z=pz
                ):
                    return

        # 3. Arrived at Destination -> Capture Fresh Camera Snapshot
        time.sleep(0.4)
        img_b64 = self.fetch_snapshot()
        if not img_b64:
            self.reply("I reached the destination, but the camera buffer is offline.")
            return

        eval_step_idx = len(motion_steps)
        if self.event:
            self.event({
                "stage": "step_progress",
                "title": f"Task: {task_title}",
                "active_step": eval_step_idx,
                "thought": "Inspecting camera from new position...",
                "steps": ui_steps,
            })

        query = visual_query

        # 4. Universal Dynamic Decision & Branching via VLM
        branch_prompt = f"""You are the visual cortex and decision engine of an agile 6-legged robot.
The user's original goal with conditional rules was: "{user_goal}".
Specific condition query: "{query}".
Available robot gesture animations: {valid_gestures} (or 'none').

Inspect this camera image (taken at your new physical position) and decide:
1. What do you observe?
2. Based on the user's conditional rules (e.g. if >3 dance, if <=3 wave, if person seen cheer, etc.), which gesture animation should be executed?
3. What is the warm spoken reply in first-person (1-2 sentences)?

Respond strictly in JSON:
{{
  "observation": "1-sentence description of what is seen in the image",
  "chosen_gesture": "Name from {valid_gestures} or 'none'",
  "speech": "Spoken reply explaining what you saw from your new position and what action you are taking"
}}
"""
        eval_raw = self.llm.inspect_vision(prompt=branch_prompt, image_b64=img_b64)
        branch_res = safe_extract_json(eval_raw)

        if isinstance(branch_res, dict):
            obs = branch_res.get("observation", eval_raw)
            chosen_gesture = str(branch_res.get("chosen_gesture", "none")).strip().lower()
            speech = str(branch_res.get("speech", f"From my new spot, I see: {obs}")).strip()
        else:
            obs = str(eval_raw)
            chosen_gesture = "none"
            speech = obs

        # 5. Execute Chosen Branch & Speak
        branch_step_idx = len(motion_steps) + 1
        if self.event:
            self.event({
                "stage": "step_progress",
                "title": f"Task: {task_title}",
                "active_step": branch_step_idx,
                "thought": f"Decision: {obs}",
                "steps": ui_steps,
            })

        self.reply(speech)
        self.speak(speech)

        if chosen_gesture and chosen_gesture != "none":
            self._execute_gesture(chosen_gesture)

        if self.event:
            self.event({
                "stage": "done",
                "title": f"Task: {task_title}",
                "thought": f"Completed: {speech}",
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