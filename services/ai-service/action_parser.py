import json
import os
import re

DEFAULT_ACTIONS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "actions.json")
DEFAULT_ANIMATIONS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "animations.json")


def load_actions(path=None):
    with open(path or DEFAULT_ACTIONS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["actions"]


def load_animations(path=None):
    anim_file = path or DEFAULT_ANIMATIONS_PATH
    if not os.path.exists(anim_file):
        return {}
    with open(anim_file, "r", encoding="utf-8") as f:
        return json.load(f).get("animations", {})


def normalize(text):
    text = (text or "").lower().strip()
    return re.sub(r"\s+", " ", text)


def normalize_animation_name(name):
    """Maps action IDs and aliases (e.g., 'preset_look_around', 'lookAround') to canonical animation keys."""
    if not name:
        return ""
    n = name.replace("preset_", "").strip()
    # Handle camelCase to snake_case aliases
    aliases = {
        "lookaround": "look_around",
        "lookAround": "look_around",
        "standup": "stand_up",
        "standUp": "stand_up",
        "sitdown": "sit_down",
        "sitDown": "sit_down",
    }
    return aliases.get(n, n)


def match_action(text, actions):
    """Deterministic keyword matching for fast offline execution."""
    if not text:
        return None
    norm = normalize(text)
    for action in actions:
        for kw in action.get("keywords", []):
            if norm and kw.lower().strip() in norm:
                return action
    return None


def action_by_id(actions, action_id):
    """Finds and returns an action definition dict by its unique string ID."""
    for a in actions:
        if a["id"] == action_id:
            return a
    return None


def llm_tool_schema(actions):
    """OpenAI-compatible tool definition derived from the action table."""
    ids = [a["id"] for a in actions]
    return {
        "type": "function",
        "function": {
            "name": "perform_hexapod_action",
            "description": "Command the hexapod robot to perform one of its known actions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action_id": {
                        "type": "string",
                        "enum": ids,
                        "description": "The hexapod action to execute",
                    }
                },
                "required": ["action_id"],
            },
        },
    }


def compile_animation_sequence(animation_def, duration_override_ms=None):
    """
    Compiles an animation definition into a single payload keyframe array
    with exact millisecond slice durations calculated from t_pct.
    """
    total_ms = duration_override_ms or animation_def.get("default_duration_ms", 2000)
    keyframes = animation_def.get("keyframes", [])
    if not keyframes:
        return [], total_ms

    compiled_keyframes = []
    prev_t_pct = 0.0

    for kf in keyframes:
        t_pct = kf.get("t_pct", 1.0)
        segment_ms = max(40, int((t_pct - prev_t_pct) * total_ms))
        prev_t_pct = t_pct

        frame = {
            "duration_ms": segment_ms,
            "easing": kf.get("easing", "easeInOutCubic"),
            "tx": kf.get("tx", 0),
            "ty": kf.get("ty", 0),
            "tz": kf.get("tz", 0),
            "rx": kf.get("rx", 0),
            "ry": kf.get("ry", 0),
            "rz": kf.get("rz", 0),
        }
        if "joints" in kf:
            frame["joints"] = kf["joints"]

        compiled_keyframes.append(frame)

    return compiled_keyframes, total_ms


def expand_animation_keyframes(animation_def, duration_override_ms=None):
    """Legacy helper returning discrete (payload, segment_ms) tuples."""
    compiled, total = compile_animation_sequence(animation_def, duration_override_ms)
    return [(dict(type="keyframe", **kf), kf["duration_ms"]) for kf in compiled]