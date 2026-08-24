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
    from providers.llm import sanitize_speech_echo
    check(sanitize_speech_echo("Hi there! How are you?", "hi!") == "Hi there! How are you?", "greeting 'hi!' is not stripped")
    check(sanitize_speech_echo("walk forward: On it, moving forward.", "walk forward") == "On it, moving forward.", "colon echo prefix stripped")
    check(sanitize_speech_echo("spin around", "spin around") == "On it!", "exact echo returns fallback reply")

    print()
    if FAILS:
        print(f"SELFTEST FAILED ({len(FAILS)} failures)")
        sys.exit(1)
    print("ALL TESTS PASSED (100% Regression Free)")

if __name__ == "__main__":
    main()