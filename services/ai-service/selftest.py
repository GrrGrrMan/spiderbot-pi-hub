#!/usr/bin/env python3
# pi-hub/services/ai-service/selftest.py
# Stdlib-only contract + parser checks (runs on the PC / CI without piper,
# whisper or a broker). Exit code 0 = pass.
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from action_parser import load_actions, match_action, llm_tool_schema

FAILS = []


def check(cond, label):
    if cond:
        print("ok   -", label)
    else:
        FAILS.append(label)
        print("FAIL -", label)


def main():
    actions = load_actions()
    check(len(actions) >= 8, "action table has >= 8 actions (%d)" % len(actions))

    ids = set()
    for a in actions:
        ids.add(a["id"])
        check("topic" in a and a["topic"] in ("cmd", "audio"), "action %s topic" % a["id"])
        check("payload" in a, "action %s payload" % a["id"])
        check("keywords" in a and a["keywords"], "action %s keywords" % a["id"])
        if a["topic"] == "cmd":
            p = a["payload"]
            check(p.get("type") in ("motion", "system"), "action %s cmd type" % a["id"])
            if p.get("type") == "motion":
                check("vx" in p and "vy" in p and "omega" in p and "gait" in p,
                      "action %s motion fields" % a["id"])

    check(len(ids) == len(actions), "action ids unique")

    # Deterministic matching sanity
    check(match_action("please walk forward", actions) is not None, "match 'walk forward'")
    check(match_action("do a spin", actions) is not None, "match 'spin'")
    check(match_action("tell me a fun fact", actions) is None, "no match on chat-only")

    # LLM tool schema derived from the table
    schema = llm_tool_schema(actions)
    enum = schema["function"]["parameters"]["properties"]["action_id"]["enum"]
    check(sorted(enum) == sorted(ids), "tool schema enum == action ids")

    # Web-ui mirror is in sync (when the repo layout is present)
    mirror = os.path.normpath(os.path.join(HERE, "../../../web-ui/src/constants/aiActions.json"))
    if os.path.exists(mirror):
        with open(mirror, "r", encoding="utf-8") as f:
            mirror_data = json.load(f)
        check(sorted(a["id"] for a in mirror_data["actions"]) == sorted(ids),
              "web-ui aiActions.json mirror in sync")
    else:
        print("skip - web-ui mirror not found (fine on the Pi)")

    print()
    if FAILS:
        print("SELFTEST FAILED (%d):" % len(FAILS))
        for f in FAILS:
            print("  -", f)
        sys.exit(1)
    print("SELFTEST PASSED")


if __name__ == "__main__":
    main()