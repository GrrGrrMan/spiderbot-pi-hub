#!/usr/bin/env python3
# services/ai-service/ai_service.py
import argparse
import base64
import json
import logging
import os
import queue
import random
import re
import sys
import threading
import time
from typing import Any, Dict, List, Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    import urllib.request
    _HAS_REQUESTS = False

import paho.mqtt.client as mqtt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from action_parser import load_actions, load_animations, extract_wake_command
from embodied_agent import EmbodiedAgent
from memory_manager import MemoryManager, MemoryMode
from pipeline import Pipeline
from providers.llm import LLMClient
from providers.stt import STTClient
from providers.tts import TTSClient
from skills.skill_manager import SkillManager

log = logging.getLogger("ai.service")


class AIService:
    def __init__(self, args):
        self.device_id = args.device
        self.cam_device_id = args.cam_device
        self.broker_host = args.broker
        self.broker_port = args.port
        self.mqtt_user = args.user
        self.mqtt_pass = args.password
        self.snapshot_url = args.snapshot_url

        self.topic_cmd_dev = f"hexapod/{self.device_id}/cmd"
        self.topic_ai = f"hexapod/{self.device_id}/ai"
        self.topic_ai_config = f"hexapod/{self.device_id}/ai/config"
        self.topic_ai_status = f"hexapod/{self.device_id}/ai/status"
        self.topic_ai_memory_cmd = f"hexapod/{self.device_id}/ai/memory/cmd"
        self.topic_ai_memory_state = f"hexapod/{self.device_id}/ai/memory/state"
        self.topic_audio = f"hexapod/{self.device_id}/audio"
        self.topic_audio_status = f"hexapod/{self.device_id}/audio/status"
        self.topic_telemetry = f"hexapod/{self.device_id}/telemetry"
        self.topic_cam_cmd = f"hexapod/{self.cam_device_id}/cmd"

        self.telemetry: Dict[str, Any] = {}
        self.cam_telemetry: Dict[str, Any] = {}
        self.abort_event = threading.Event()
        self._http_session = requests.Session() if _HAS_REQUESTS else None

        self.actions = load_actions(args.actions)
        self.animations = load_animations()
        self.wake_words = [
            w.strip().lower() for w in os.environ.get("WAKE_WORDS", "hey spider,hey hexapod,ok spider").split(",") if w.strip()
        ]
        self.llm = (
            LLMClient(
                base_url=args.llm_base_url,
                model=args.llm_model,
                vision_model=args.llm_vision_model,
                api_key=args.llm_api_key,
            )
            if args.llm
            else None
        )
        self.stt = STTClient() if args.stt else None
        self.tts = (
            TTSClient(
                base_url=args.llm_base_url,
                api_key=args.llm_api_key,
                model=os.environ.get("TTS_MODEL", "local"),
            )
            if args.tts
            else None
        )
        if self.tts:
            self.tts.warmup()

        self.memory = MemoryManager(
            storage_path="/opt/hexapod-ai/memory_pool.json",
            mode=os.environ.get("MEMORY_MODE", MemoryMode.SESSION),
        )
        self.skills = SkillManager(
            on_alarm_trigger=lambda track: self._on_audio({"action": "alarm", "payload": track}),
            on_speak_alert=lambda msg: (self._on_ai_reply(msg), self._on_tts_text(msg)),
            publish_audio_frame_fn=lambda frame: self._publish(self.topic_audio, frame),
            fetch_snapshot_fn=self.fetch_camera_snapshot,
        )
        self.pipeline = Pipeline(self.actions, llm=self.llm, stt=self.stt, skill_manager=self.skills)

        self.embodied_agent = (
            EmbodiedAgent(
                llm_client=self.llm,
                fetch_snapshot_fn=self.fetch_camera_snapshot,
                publish_s3_cmd_fn=self._on_cmd,
                publish_cam_cmd_fn=self._on_cam_cmd,
                speak_fn=self._on_tts_text,
                reply_fn=self._on_ai_reply,
                event_fn=self._on_agent_event,
                directive_fn=self._on_action_directive,
                abort_event=self.abort_event,
                publish_audio_fn=self._on_audio,
                skill_manager=self.skills,
            )
            if self.llm
            else None
        )

        self.mqtt = None
        self._work = queue.Queue(maxsize=16)
        self._busy = False
        self._running = True
        self._sender = f"ai-service-{self.device_id}"
        self._audio_done_event = threading.Event()
        self._last_msg_text = ""
        self._last_msg_ts = 0.0

    def get_live_state_block(self) -> str:
        """Builds a real-time state grounding summary for the LLM context."""
        cam_flash = self.cam_telemetry.get("flash_pct", 0)
        cam_fps = self.cam_telemetry.get("target_fps", 10)
        is_powered = self.telemetry.get("power", True)
        audio_state = self.telemetry.get("audio", "idle")
        v_batt = self.telemetry.get("v_batt") or self.telemetry.get("battery_v")
        batt_str = f"{v_batt:.2f}V" if isinstance(v_batt, (int, float)) else "OK (Nominal)"
        motion_state = self.telemetry.get("motion_state", "idle")

        lines = [
            "\n### CURRENT HARDWARE PERIPHERAL & TELEMETRY STATE:",
            f"- Battery Level: {batt_str}",
            f"- Kinematics Engine: {motion_state.upper()}",
            f"- Camera Flashlight: {cam_flash}% active",
            f"- Camera Stream Target: {cam_fps} FPS",
            f"- Servo Bus Power: {'ENABLED' if is_powered else 'LIMP / DISABLED'}",
            f"- Audio Subsystem: {audio_state.upper()}",
        ]
        return "\n".join(lines) + "\n"

    def fetch_camera_snapshot(self) -> Optional[str]:
        """Fetches the latest frame via persistent requests or urllib fallback."""
        try:
            if _HAS_REQUESTS and self._http_session:
                resp = self._http_session.get(self.snapshot_url, timeout=1.5)
                if resp.status_code == 200:
                    return base64.b64encode(resp.content).decode("utf-8")
            else:
                req = urllib.request.Request(self.snapshot_url, headers={"User-Agent": "HexapodAI/2.0"})
                with urllib.request.urlopen(req, timeout=1.5) as resp:
                    if resp.status == 200:
                        return base64.b64encode(resp.read()).decode("utf-8")
        except Exception as e:
            log.warning("Snapshot fetch failed (%s): %s", self.snapshot_url, e)
        return None

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        log.info("Connected to MQTT broker at %s:%s", self.broker_host, self.broker_port)
        client.subscribe(self.topic_ai)
        client.subscribe(self.topic_ai_config)
        client.subscribe(self.topic_ai_memory_cmd)
        client.subscribe(self.topic_audio_status)
        client.subscribe(self.topic_telemetry)
        topic_cam_tel = f"hexapod/{self.cam_device_id}/telemetry"
        client.subscribe(topic_cam_tel)
        log.info("Subscribed -> %s, %s, %s, %s, %s, %s", self.topic_ai, self.topic_ai_config, self.topic_ai_memory_cmd, self.topic_audio_status, self.topic_telemetry, topic_cam_tel)
        self._broadcast_memory_state()

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except Exception as e:
            log.warning("Invalid JSON payload on %s: %s", msg.topic, e)
            return

        if msg.topic == self.topic_ai_memory_cmd:
            if isinstance(data, dict):
                action = data.get("action", "")
                if action == "set_mode" and "mode" in data:
                    self.memory.set_mode(data["mode"])
                elif action == "set_fact" and "key" in data and "value" in data:
                    self.memory.set_fact(data["key"], data["value"])
                elif action == "delete_fact" and "key" in data:
                    self.memory.delete_fact(data["key"])
                elif action == "clear_session":
                    self.memory.clear_session()
                elif action == "clear_all":
                    self.memory.clear_all()
                self._broadcast_memory_state()
                self._broadcast_status()
            return

        if msg.topic == self.topic_ai_config:
            if isinstance(data, dict):
                if "memory" in data and isinstance(data["memory"], dict):
                    if "mode" in data["memory"]:
                        self.memory.set_mode(data["memory"]["mode"])
                if "sentinel" in data and isinstance(data["sentinel"], dict):
                    if "wake_words" in data["sentinel"]:
                        self.wake_words = [str(w).lower().strip() for w in data["sentinel"]["wake_words"] if str(w).strip()]
                if "wake_words" in data and isinstance(data["wake_words"], list):
                    self.wake_words = [str(w).lower().strip() for w in data["wake_words"] if str(w).strip()]
                if self.llm:
                    llm_cfg = data.get("llm", data)
                    self.llm.update_config(llm_cfg)
                self._broadcast_memory_state()
                self._broadcast_status()
            return

        if msg.topic == self.topic_telemetry:
            self.telemetry = data
            return

        if msg.topic == f"hexapod/{self.cam_device_id}/telemetry":
            self.cam_telemetry = data
            return

        if msg.topic == self.topic_audio_status:
            if data.get("state") == "idle":
                self._audio_done_event.set()
            elif data.get("state") == "playing":
                self._audio_done_event.clear()
            return

        if msg.topic == self.topic_ai:
            if data.get("role") in ("assistant", "system") or data.get("type") in ("transcription", "directive", "sentinel_event") or data.get("sender") == self._sender:
                return

            text_content = str(data.get("content", "")).lower().strip()

            if text_content in ("stop", "halt", "freeze", "power off", "sleep", "shut down", "hold", "emergency stop"):
                log.warning("[EMERGENCY STOP] Preempting active tasks immediately!")
                self.abort_event.set()
                while not self._work.empty():
                    try: self._work.get_nowait()
                    except queue.Empty: break

                if text_content in ("freeze", "power off", "sleep", "shut down"):
                    self._on_cmd({"type": "system", "power": False})
                    self._on_ai_reply("Going limp. Goodnight!")
                else:
                    self._on_cmd({"type": "motion", "gait": "tripod", "vx": 0, "vy": 0, "omega": 0})
                    self._on_ai_reply("Stopping.")
                return

            now = time.time()
            if text_content and text_content == self._last_msg_text and (now - self._last_msg_ts) < 0.3:
                log.debug("Debouncing duplicate message: %s", text_content)
                return
            self._last_msg_text = text_content
            self._last_msg_ts = now

            self._enqueue(data.get("type", "text"), data)

    def _enqueue(self, kind, data):
        try:
            self._work.put_nowait((kind, data))
        except queue.Full:
            log.warning("Queue full — dropping message")

    def _publish(self, topic, payload, qos=0, retain=False):
        if not self.mqtt or not self.mqtt.is_connected():
            return
        if isinstance(payload, bytes):
            self.mqtt.publish(topic, payload, qos=qos, retain=retain)
        else:
            self.mqtt.publish(topic, json.dumps(payload), qos=qos, retain=retain)
    def _on_cmd(self, payload):
        self._publish(self.topic_cmd_dev, payload)

    def _on_cam_cmd(self, payload):
        self._publish(self.topic_cam_cmd, payload)

    def _on_audio(self, payload):
        self._publish(self.topic_audio, payload)

    def _on_ai_reply(self, reply):
        if not reply:
            return
        # Ground spoken assistant turn into persistent memory context
        self.memory.add_assistant(reply)
        self._broadcast_memory_state()

        msg = {
            "type": "text",
            "role": "assistant",
            "sender": self._sender,
            "content": reply,
            "timestamp": int(time.time() * 1000),
        }
        self._publish(self.topic_ai, msg)

    def _on_action_directive(self, action_payload):
        if not action_payload:
            return
        msg = {
            "type": "directive",
            "role": "assistant",
            "sender": self._sender,
            "timestamp": int(time.time() * 1000),
        }
        action_id = ""
        if isinstance(action_payload, dict):
            msg.update(action_payload)
            if "name" in action_payload and "action_id" not in msg:
                msg["action_id"] = action_payload["name"]
            action_id = msg.get("action_id", "")
        else:
            action_id = str(action_payload)
            msg["action_id"] = action_id

        if action_id:
            self.memory.record_action(action_id)

        self._publish(self.topic_ai, msg)

    def _on_agent_event(self, event_data: dict):
        msg = {
            "type": "agent_event",
            "sender": self._sender,
            "timestamp": int(time.time() * 1000),
            **event_data,
        }
        self._publish(self.topic_ai, msg)

    def _on_tts_text(self, reply):
        if not reply or not self.tts or not self.tts.available():
            return 0.0
        try:
            from providers.tts import split_sentences
            sentences = split_sentences(reply)
            if not sentences:
                return 0.0

            self._audio_done_event.clear()
            flow_id = random.randint(1, 0xFFFFFFFF)
            total_duration_s = 0.0

            for sentence in sentences:
                if self.abort_event.is_set():
                    break
                wav = self.tts.synthesize_wav_bytes(sentence)
                if not wav:
                    continue
                duration_s = max(0.15, (len(wav) - 44) / 44100.0)
                total_duration_s += duration_s

                for frame in self.tts.frames(wav, flow_id=flow_id):
                    if self.abort_event.is_set():
                        break
                    self._publish(self.topic_audio, frame)

            return total_duration_s
        except Exception as e:
            log.error("TTS synthesis error: %s", e)
            return 0.0

    def _wait_for_audio_done(self, timeout_s=3.0):
        finished = self._audio_done_event.wait(timeout=max(0.5, timeout_s))
        if not finished:
            log.debug("Audio timeout reached (%0.2fs)", timeout_s)
        time.sleep(0.1)

    def _handle(self, kind, data):
        self.abort_event.clear()
        self.skills.duck_audio()

        if kind == "audio":
            content = data.get("content", "")
            if not content:
                return
            wav_bytes = base64.b64decode(content) if isinstance(content, str) else bytes(content)
            if not self.stt:
                self._on_ai_reply("Voice transcription is unavailable.")
                return

            text = self.stt.transcribe_wav_bytes(wav_bytes)
            if not text:
                if not data.get("is_sentinel"):
                    self._on_ai_reply("I couldn't hear that clearly. Could you say that again?")
                return

            log.info("STT Transcribed -> %r", text)

            # Check if this audio slice came from 24/7 Smart Speaker mode
            if data.get("is_sentinel"):
                wake_status, extracted_cmd = extract_wake_command(text)
                if wake_status is None:
                    self._publish(self.topic_ai, {
                        "type": "sentinel_event",
                        "state": "ignored",
                        "transcript": text,
                        "timestamp": int(time.time() * 1000),
                    })
                    return

                if wake_status == "standalone":
                    self._publish(self.topic_ai, {
                        "type": "sentinel_event",
                        "state": "listening_prompt",
                        "transcript": text,
                        "timestamp": int(time.time() * 1000),
                    })
                    return

                text = extracted_cmd
                self._publish(self.topic_ai, {
                    "type": "sentinel_event",
                    "state": "recognized",
                    "transcript": text,
                    "command": text,
                    "timestamp": int(time.time() * 1000),
                })

            self._publish(self.topic_ai, {
                "type": "transcription",
                "role": "user",
                "content": f"🎤 \"{text}\"",
                "timestamp": int(time.time() * 1000),
            })

            payload = dict(data)
            payload.update({"type": "text", "content": text})
        else:
            payload = data
            text = payload.get("content", "")

        text = (text or "").strip()
        if not text:
            return

        # Natural Voice Commands for Memory Control
        norm_cmd = text.lower().strip()
        if norm_cmd in ("clear memory", "forget everything", "reset chat", "clear history", "reset memory"):
            self.memory.clear_session()
            self._broadcast_memory_state()
            self._on_ai_reply("Session history cleared! Starting fresh.")
            return
        elif norm_cmd.startswith("remember that ") or norm_cmd.startswith("remember: "):
            fact_body = re.sub(r"^remember(\s+that|:)\s+", "", text, flags=re.IGNORECASE).strip()
            if fact_body:
                key = f"fact_{int(time.time())}"
                self.memory.set_fact(key, fact_body)
                self._broadcast_memory_state()
                self._on_ai_reply("I've committed that to my long-term memory pool.")
                return

        # 1. Update session buffer, broadcast state to UI, and grab memory pool, DST & live skills blocks
        self.memory.add_user(text)
        self._broadcast_memory_state()
        session_history = self.memory.get_context_history()
        memory_block = self.memory.get_memory_pool_prompt_block()
        dst_block = self.memory.get_dst_prompt_block()
        skills_block = self.skills.get_live_skills_state_block()

        # Handle direct timer voice intents locally for zero latency
        if re.search(r"\b(set|start)\b.*\b(timer|alarm)\b", norm_cmd):
            sec_match = re.search(r"(\d+)\s*(sec|second|min|minute|hour)", norm_cmd)
            if sec_match:
                qty = int(sec_match.group(1))
                unit = sec_match.group(2)
                dur = qty * 3600 if "hour" in unit else qty * 60 if "min" in unit else qty
                res = self.skills.execute_skill("set_timer", {"duration_seconds": dur, "label": "Timer"})
                msg = f"Timer set for {qty} {unit}s!"
                self._on_ai_reply(msg)
                self._on_tts_text(msg)
                return

        # 2. Active Perception: Pre-fetch frame if visual intent is obvious, or allow dynamic model tool-call via inspect_scene
        visual_triggers = ("see", "look", "inspect", "camera", "view", "show", "watch", "finger", "fingers", "holding", "color", "who", "what is this", "what do you see", "find", "detect", "read", "check")
        needs_vision = any(vt in norm_cmd for vt in visual_triggers)

        current_frame = self.fetch_camera_snapshot() if needs_vision else None
        state_block = self.get_live_state_block()

        result = self.pipeline.decide(
            text, history=session_history, image_b64=current_frame,
            memory_block=memory_block, state_block=state_block,
            dst_block=dst_block, skills_block=skills_block
        )

        # 3. Store assistant response into memory
        if result.reply:
            self.memory.add_assistant(result.reply)
            self._broadcast_memory_state()

        try:
            self.pipeline.execute(
                result,
                embodied_agent=self.embodied_agent,
                on_cmd=self._on_cmd,
                on_cam_cmd=self._on_cam_cmd,
                on_audio=self._on_audio,
                on_tts_text=self._on_tts_text,
                on_ai_reply=self._on_ai_reply,
                on_action_directive=self._on_action_directive,
                on_agent_event=self._on_agent_event,
                wait_for_audio_fn=self._wait_for_audio_done,
                abort_event=self.abort_event,
            )
        finally:
            # Unduck background music after speech and actions finish
            time.sleep(0.5)
            self.skills.unduck_audio()

    def _worker_loop(self):
        while self._running:
            try:
                kind, data = self._work.get(timeout=0.5)
            except queue.Empty:
                continue
            self._busy = True
            try:
                self._handle(kind, data)
            except Exception:
                log.exception("Worker loop error")
            finally:
                self._busy = False

    def _broadcast_memory_state(self):
        if not self.mqtt or not self.mqtt.is_connected():
            return
        payload = self.memory.get_state_dict()
        self._publish(self.topic_ai_memory_state, payload, retain=True)

    def _broadcast_status(self):
        state = "online" if (self.mqtt and self.mqtt.is_connected()) else "offline"
        if self._busy and state == "online":
            state = "busy"

        payload = {
            "state": state,
            "memory": self.memory.get_state_dict(),
            "sentinel": {
                "wake_words": self.wake_words,
            },
            "llm": {
                "provider": "omniroute",
                "base_url": (self.llm.base_url if self.llm else None),
                "status": (self.llm.status if self.llm else "offline"),
                **(self.llm.get_config_dict() if self.llm else {}),
            },
            "actions": self.actions,
            "animations": list(self.animations.keys()),
            "stt": bool(self.stt),
            "tts": bool(self.tts),
            "ts": int(time.time() * 1000),
        }
        self._publish(self.topic_ai_status, payload, retain=True)

    def _status_loop(self):
        while self._running:
            self._broadcast_status()
            time.sleep(4)

    def run(self):
        self.mqtt = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"ai-service-{self.device_id}")
        if self.mqtt_user:
            self.mqtt.username_pw_set(self.mqtt_user, self.mqtt_pass)
        self.mqtt.on_connect = self._on_connect
        self.mqtt.on_message = self._on_message
        self.mqtt.connect_async(self.broker_host, self.broker_port, keepalive=30)
        self.mqtt.loop_start()

        threading.Thread(target=self._worker_loop, daemon=True, name="ai-worker").start()
        threading.Thread(target=self._status_loop, daemon=True, name="ai-heartbeat").start()

        log.info("OmniRoute AI service online for device %s", self.device_id)
        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self._running = False
            self.mqtt.loop_stop()


def main():
    ap = argparse.ArgumentParser(description="V2 Hexapod AI Voice & Vision Service (OmniRoute Integrated)")
    ap.add_argument("--device", default=os.environ.get("DEVICE_ID", "hexapod-s3-01"))
    ap.add_argument("--cam-device", default=os.environ.get("CAM_DEVICE_ID", "hexapod-cam-01"))
    ap.add_argument("--broker", default=os.environ.get("MQTT_HOST", "localhost"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
    ap.add_argument("--user", default=os.environ.get("MQTT_USER"))
    ap.add_argument("--password", default=os.environ.get("MQTT_PASS"))
    ap.add_argument("--actions", default=os.path.join(os.path.dirname(__file__), "actions.json"))
    ap.add_argument("--llm-base-url", default=os.environ.get("LLM_BASE_URL", "http://127.0.0.1:20128/v1"))
    ap.add_argument("--llm-model", default=os.environ.get("LLM_MODEL", "hexapod-vision"))
    ap.add_argument("--llm-vision-model", default=os.environ.get("LLM_VISION_MODEL", "hexapod-vision"))
    ap.add_argument("--llm-api-key", default=os.environ.get("LLM_API_KEY") or os.environ.get("GROQ_API_KEY", "spiderbot"))
    ap.add_argument("--snapshot-url", default=os.environ.get("SNAPSHOT_URL", "http://127.0.0.1:8088/snapshot"))
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--no-stt", action="store_true")
    ap.add_argument("--no-tts", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    args.llm = not args.no_llm
    args.stt = not args.no_stt
    args.tts = not args.no_tts

    AIService(args).run()


if __name__ == "__main__":
    main()