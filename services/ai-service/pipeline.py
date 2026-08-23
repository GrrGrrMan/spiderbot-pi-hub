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

PROCEDURAL_TRIGGERS = [
    "then rotate", "then turn", "then walk", "then move", "then look", "then see",
    "if you can see", "if you see", "if >", "if <", "if =", "dance if", "wave if", "cheer if",
    ", then", "and then", "if not", "after that", "walk until", "rotate until"
]


class PipelineResult:
    def __init__(
        self,
        timeline: Optional[List[Dict[str, Any]]] = None,
        reply: str = "",
        order: str = "tts_first",
        mode: str = "standard",  # "standard" | "procedural"
        goal_text: str = "",
        thought: str = "",
        task_title: str = "",
    ):
        self.timeline = timeline or []
        self.reply = reply or ""
        self.order = order or "tts_first"
        self.mode = mode
        self.goal_text = goal_text
        self.thought = thought
        self.task_title = task_title


class Pipeline:
    def __init__(self, actions: List[Dict[str, Any]], llm: Optional[Any] = None, stt: Optional[Any] = None, animations: Optional[Dict[str, Any]] = None):
        self.actions = actions
        self.animations = animations or load_animations()
        self.llm = llm
        self.stt = stt

    def decide(self, text: str, history: Optional[List[Dict[str, Any]]] = None, image_b64: Optional[str] = None) -> PipelineResult:
        norm = (text or "").lower().strip().replace("foward", "forward")
        if not norm:
            return PipelineResult(reply=CANNED_UNKNOWN)

        # 1. Multi-Step Procedural & Conditional Program
        if self.llm and self.llm.status != "offline" and any(k in norm for k in PROCEDURAL_TRIGGERS):
            return PipelineResult(mode="procedural", goal_text=text, thought="Compiling procedural execution graph...")

        # 2. General Multimodal Conversation & Immediate Actions
        if self.llm and self.llm.status != "offline":
            speech, timeline, order, thought, task_title = self.llm.chat(
                self.actions, self.animations, text, history=history or [], image_b64=image_b64
            )
            return PipelineResult(
                timeline=timeline,
                reply=speech,
                order=order,
                thought=thought,
                task_title=task_title or "Task",
                goal_text=text,
            )

        # 3. Offline Keyword Fallback
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
        wait_for_audio_fn: Optional[Callable[[float], None]] = None,
        abort_event: Optional[threading.Event] = None,
    ):
        abort = abort_event or threading.Event()

        # Procedural execution route
        if result.mode == "procedural" and embodied_agent:
            embodied_agent.run_procedural_task(result.goal_text)
            return

        timeline = result.timeline
        order = result.order or "tts_first"
        reply = result.reply or ""
        task_title = result.task_title or "Task"

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
                if abort.is_set(): break
                stype = step.get("type", "action")
                act_id = step.get("id") or ""
                dur_ms = step.get("duration_ms", 1500)
                params = step.get("params", {})
                anim_key = normalize_animation_name(act_id)

                if stype in ("gesture", "sequence") or (anim_key in self.animations):
                    target_anim = anim_key or act_id
                    if target_anim in self.animations:
                        anim = self.animations[target_anim]
                        compiled_kfs, total_ms = compile_animation_sequence(anim, duration_override_ms=dur_ms)
                        on_cmd({"type": "sequence", "name": target_anim, "duration_ms": total_ms, "keyframes": compiled_kfs})
                        time.sleep(total_ms / 1000.0)
                        continue

                if stype == "gait":
                    payload = {
                        "type": "motion",
                        "gait": params.get("gait", "tripod"),
                        "vx": params.get("vx", 40),
                        "vy": params.get("vy", 0),
                        "omega": params.get("omega", 0),
                        "step_height": params.get("step_height", 30),
                        "cycle_time": params.get("cycle_time", 1.0),
                    }
                    on_cmd(payload)
                    run_dur = dur_ms or 1000
                    time.sleep(run_dur / 1000.0)
                    stop = dict(payload)
                    stop["vx"] = stop["vy"] = stop["omega"] = 0
                    on_cmd(stop)
                    continue

                act = action_by_id(self.actions, act_id)
                if act:
                    on_cmd(act["payload"])
                    if dur_ms > 0: time.sleep(dur_ms / 1000.0)

        try:
            if order == "action_first":
                if timeline: run_timeline()
                if reply and not abort.is_set():
                    on_ai_reply(reply)
                    on_tts_text(reply)
            else:
                if reply and not abort.is_set():
                    on_ai_reply(reply)
                    tts_dur = on_tts_text(reply) or 0.0
                    if wait_for_audio_fn: wait_for_audio_fn(tts_dur + 0.5)
                if timeline and not abort.is_set():
                    run_timeline()
        finally:
            if on_agent_event:
                on_agent_event({"stage": "done", "title": f"Task: {task_title}", "thought": "Execution complete."})