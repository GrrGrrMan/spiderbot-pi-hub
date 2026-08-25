import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

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
        "twist_torso": "look_around",
        "twist_and_look": "look_around",
    }
    return aliases.get(n, n)


# ==============================================================================
# MODULAR KEYWORD & PHONETIC DICTIONARIES
# ==============================================================================

# 1. Wake Prefixes
WAKE_PREFIXES = [
    "hey", "hi", "hello", "hay", "hai", "ok", "okay", "yo", "sup", "dear"
]

# 2. Wake Roots & Phonetic Slur Variants (sorted by token length descending)
WAKE_PHONETIC_PHRASES = [
    "peter parker", "spider bot", "spider robot",
]

WAKE_PHONETIC_WORDS = [
    # Hexapod acoustic slurs ("Haxaf", "Hexod", "Haxpod", etc.)
    "hexapod", "hexap", "hexa", "hexaf", "hexav", "hexod", "hex",
    "haxapod", "haxpod", "haxaf", "haxav", "haxod", "haxa", "hax", "hacks",
    "huxapod", "huxa", "hux", "hacker", "Hetsa"
]

# 3. Domain & Slang Phonetic Replacements (STT Misrecognitions -> Canonical)
PHONETIC_CORRECTIONS = {
    # Clipped speech & Slang (e.g. "posi", "crotch down")
    r"\bposi\b": "position",
    r"\bposish\b": "position",
    r"\bpozition\b": "position",
    r"\bcrotch\b": "crouch",
    r"\bboard\b(?=\s+\d+)": "forward",  # "board 30 degrees" -> "forward 30 degrees"

    # Kinematics & Leg anatomy
    r"\bcoxsat\b": "coxa",
    r"\bcoxats\b": "coxas",
    r"\bcocksa\b": "coxa",
    r"\bcoxa left\b": "coxa left",
    r"\bcoax\b": "coxa",
    r"\bfeemur\b": "femur",
    r"\bfemmer\b": "femur",
    r"\btibea\b": "tibia",
    r"\btibeo\b": "tibia",

    # Directional & Movement typos
    r"\bfoward\b": "forward",
    r"\bforwrd\b": "forward",
    r"\bbakward\b": "backward",
    r"\bbackword\b": "backward",

    # Units
    r"\bdeg\b": "degrees",
    r"\bdegs\b": "degrees",
    r"\bmils\b": "millimeters",
}


def sanitize_robot_phonetics(text: str) -> str:
    """Standardizes clipped speech, slang, and phonetic STT slips into canonical robotic terms."""
    if not text:
        return ""
    result = text
    for pattern, replacement in PHONETIC_CORRECTIONS.items():
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    return result


def extract_wake_command(text: str) -> tuple[Optional[str], str]:
    """
    Modular wake-word extractor:
    1. Normalizes phonetic slang ('posi' -> 'position', 'coxsat' -> 'coxa').
    2. Strips leading conversational prefixes ('hey', 'okay').
    3. Matches multi-word and single-word wake roots with phonetic tolerance ('haxaf', 'peter parker').
    4. Returns ('command', 'sanitized command text') | ('standalone', '') | (None, '').
    """
    if not text:
        return None, ""

    sanitized = sanitize_robot_phonetics(text)
    cleaned = re.sub(r"[^\w\s]", " ", sanitized.lower())
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    if not cleaned:
        return None, ""

    # Phase A: Check multi-word wake phrases first
    for phrase in WAKE_PHONETIC_PHRASES:
        pattern = rf"^(?:{'|'.join(WAKE_PREFIXES)}\s+)?{re.escape(phrase)}\b\s*"
        match = re.search(pattern, cleaned)
        if match:
            cmd = cleaned[match.end():].strip()
            return ("command", cmd) if cmd else ("standalone", "")

    # Phase B: Token scan for single-word wake roots
    words = cleaned.split()
    wake_idx = -1

    for i, word in enumerate(words):
        if word in WAKE_PHONETIC_WORDS:
            wake_idx = i + 1
            break

    if wake_idx == -1:
        return None, ""

    command_words = words[wake_idx:]
    if not command_words:
        return "standalone", ""

    return "command", " ".join(command_words)


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


def compile_dynamic_joint_sequence(joints_dict: Dict[str, Any], dur_ms: int = 2500, auto_balance: bool = False) -> Dict[str, Any]:
    """Dynamically compiles arbitrary leg/joint angles into a safe 3-phase keyframe trajectory."""
    lifted_legs = [leg.lower() for leg, j in joints_dict.items() if isinstance(j, dict) and float(j.get("beta", 0)) > 20]

    tz, rx, ry = 0.0, 0.0, 0.0
    if auto_balance and len(lifted_legs) > 0:
        front_lifted = sum(1 for leg in lifted_legs if leg.endswith("f"))
        rear_lifted = sum(1 for leg in lifted_legs if leg.endswith("r"))
        right_lifted = sum(1 for leg in lifted_legs if leg.startswith("r"))
        left_lifted = sum(1 for leg in lifted_legs if leg.startswith("l"))

        if front_lifted > rear_lifted:
            rx = -10.0 * min(2, front_lifted - rear_lifted)
            tz = -10.0 * min(2, front_lifted - rear_lifted)
        elif rear_lifted > front_lifted:
            rx = 10.0 * min(2, rear_lifted - front_lifted)
            tz = -10.0 * min(2, rear_lifted - front_lifted)

        if right_lifted > left_lifted:
            ry = -8.0 * min(2, right_lifted - left_lifted)
        elif left_lifted > right_lifted:
            ry = 8.0 * min(2, left_lifted - right_lifted)

    sanitized_joints = {}
    neutral_joints = {}
    for leg, jdict in joints_dict.items():
        leg_key = leg.lower().strip()
        if leg_key not in ("rf", "rm", "rr", "lf", "lm", "lr") or not isinstance(jdict, dict):
            continue
        
        # Preserve neutral joint baseline for unspecified angles rather than zeroing out bent knees
        leg_entry = {}
        if "alpha" in jdict:
            leg_entry["alpha"] = max(-40.0, min(40.0, float(jdict["alpha"])))
        if "beta" in jdict:
            leg_entry["beta"] = max(-20.0, min(65.0, float(jdict["beta"])))
        if "gamma" in jdict:
            leg_entry["gamma"] = max(-65.0, min(20.0, float(jdict["gamma"])))
            
        if leg_entry:
            sanitized_joints[leg_key] = leg_entry
        neutral_joints[leg_key] = {"alpha": 0, "beta": 0, "gamma": -45}

    dur_ms = max(400, min(10000, dur_ms))
    # Smooth transition to target joint stance, then hold posture permanently
    transition_ms = min(800, int(dur_ms * 0.6))
    hold_ms = max(200, dur_ms - transition_ms)

    keyframes = [
        {"duration_ms": 40, "easing": "easeInOutCubic", "tz": tz * 0.2, "rx": rx * 0.2, "ry": ry * 0.2},
        {"duration_ms": transition_ms, "easing": "easeInOutCubic", "tz": tz, "rx": rx, "ry": ry, "joints": sanitized_joints},
        {"duration_ms": hold_ms, "easing": "linear", "tz": tz, "rx": rx, "ry": ry, "joints": sanitized_joints},
    ]

    return {
        "type": "sequence",
        "name": "dynamic_joint_motion",
        "duration_ms": dur_ms,
        "keyframes": keyframes,
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