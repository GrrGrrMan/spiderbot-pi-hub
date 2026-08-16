# pi-hub/services/ai-service/action_parser.py
# P5 stage-1 deterministic action matcher (mirrors web-ui/src/utils/aiActionMatcher.js).
# Maps free text -> action table row from actions.json. Used BEFORE the LLM so
# common voice commands work even with no internet (offline fallback = 5.1).
import json
import os
import re

DEFAULT_ACTIONS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "actions.json")


def load_actions(path=None):
    """Load the canonical action table (list of action dicts)."""
    with open(path or DEFAULT_ACTIONS_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["actions"]


def normalize(text):
    text = (text or "").lower().strip()
    return re.sub(r"\s+", " ", text)


def match_action(text, actions):
    """Return the first action whose keyword is a substring of the text, else None."""
    if not text:
        return None
    norm = normalize(text)
    for action in actions:
        for kw in action.get("keywords", []):
            if norm and kw.lower().strip() in norm:
                return action
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


def action_by_id(actions, action_id):
    for a in actions:
        if a["id"] == action_id:
            return a
    return None