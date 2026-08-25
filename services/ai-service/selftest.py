#!/usr/bin/env python3
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from action_parser import load_actions, load_animations, expand_animation_keyframes, match_action

FAILS = []

def check(cond, label):
    if cond:
        print("ok   -", label)
    else:
        FAILS.append(label)
        print("FAIL -", label)

def main():
    actions = load_actions()
    animations = load_animations()
    
    check(len(actions) >= 8, f"action table has >= 8 actions ({len(actions)})")
    check(len(animations) >= 4, f"animation table has >= 4 animations ({len(animations)})")

    # Test keyframe expansion
    stretch_anim = animations.get("stretch")
    check(stretch_anim is not None, "stretch animation exists")
    if stretch_anim:
        expanded = expand_animation_keyframes(stretch_anim, 2000)
        check(len(expanded) == len(stretch_anim["keyframes"]), f"stretch expanded into {len(expanded)} keyframe chunks")
        total_time = sum(dur for _, dur in expanded)
        check(abs(total_time - 2000) < 50, f"expanded duration matches 2000ms (got {total_time}ms)")

    # Test wave joint override expansion
    wave_anim = animations.get("wave")
    check(wave_anim is not None, "wave animation exists")
    if wave_anim:
        expanded_wave = expand_animation_keyframes(wave_anim, 2200)
        check(len(expanded_wave) > 0 and "joints" in expanded_wave[0][0], "wave keyframes contain joint override dictionary")

    # Test Echo Sanitizer regressions
    from providers.llm import sanitize_speech_echo, SKILL_TOOLS
    check(sanitize_speech_echo("Hi there! How are you?", "hi!") == "Hi there! How are you?", "greeting 'hi!' is not stripped")
    check(sanitize_speech_echo("walk forward: On it, moving forward.", "walk forward") == "On it, moving forward.", "colon echo prefix stripped")
    check(sanitize_speech_echo("spin around", "spin around") == "On it!", "exact echo returns fallback reply")

    # Test Native Tool Schemas
    check(len(SKILL_TOOLS) >= 9, f"native tool schema registry loaded ({len(SKILL_TOOLS)} tools)")
    tool_names = [t["function"]["name"] for t in SKILL_TOOLS]
    check("inspect_scene" in tool_names, "active perception tool 'inspect_scene' registered")
    check("play_music" in tool_names, "media tool 'play_music' registered")
    check("get_weather" in tool_names, "weather tool 'get_weather' registered")

    # Test Sentence Splitting for Low-Latency TTFA
    from providers.tts import split_sentences
    test_text = "I see a green cup on the table! Walking forward to take a closer look. Let me know if you need anything else."
    splits = split_sentences(test_text)
    check(len(splits) == 3, f"sentence stream splitter chunked into 3 segments (got {len(splits)})")

    print()
    if FAILS:
        print(f"SELFTEST FAILED ({len(FAILS)} failures)")
        sys.exit(1)
    print("ALL TESTS PASSED (100% Regression Free)")

if __name__ == "__main__":
    main()