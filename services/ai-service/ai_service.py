#!/usr/bin/env python3
# pi-hub/services/ai-service/ai_service.py
import argparse
import base64
import json
import logging
import os
import queue
import sys
import threading
import time

import paho.mqtt.client as mqtt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from action_parser import load_actions
from pipeline import Pipeline
from providers.llm import LLMClient
from providers.stt import STTClient
from providers.tts import TTSClient

log = logging.getLogger("ai.service")


class AIService:
    def __init__(self, args):
        self.device_id = args.device
        self.broker_host = args.broker
        self.broker_port = args.port
        self.mqtt_user = args.user
        self.mqtt_pass = args.password

        self.topic_cmd_dev = f"hexapod/{self.device_id}/cmd"
        self.topic_ai = f"hexapod/{self.device_id}/ai"
        self.topic_ai_status = f"hexapod/{self.device_id}/ai/status"
        self.topic_audio = f"hexapod/{self.device_id}/audio"

        self.actions = load_actions(args.actions)
        self.llm = LLMClient(model=args.llm_model, base_url=args.llm_base_url) if args.llm else None
        self.stt = STTClient() if args.stt else None
        self.tts = TTSClient() if args.tts else None
        self.pipeline = Pipeline(self.actions, llm=self.llm, stt=self.stt)

        self.mqtt = None
        self._work = queue.Queue(maxsize=16)
        self._busy = False
        self._running = True
        self._sender = f"ai-service-{self.device_id}"

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        log.info("Connected to MQTT broker at %s:%s", self.broker_host, self.broker_port)
        client.subscribe(self.topic_ai)
        log.info("Listening on AI channel: %s", self.topic_ai)

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except Exception as e:
            log.warning("Invalid JSON payload on %s: %s", msg.topic, e)
            return

        if msg.topic == self.topic_ai:
            if data.get("role") in ("assistant", "system") or data.get("type") == "transcription" or data.get("sender") == self._sender:
                return
            self._enqueue(data.get("type", "text"), data)

    def _enqueue(self, kind, data):
        try:
            self._work.put_nowait((kind, data))
        except queue.Full:
            log.warning("Queue full — dropping message")

    def _publish(self, topic, payload, qos=0):
        if not self.mqtt or not self.mqtt.is_connected():
            return
        compact = json.dumps(payload)
        self.mqtt.publish(topic, compact, qos=qos)

    def _on_cmd(self, payload):
        self._publish(self.topic_cmd_dev, payload)

    def _on_audio(self, payload):
        self._publish(self.topic_audio, payload)

    def _on_ai_reply(self, reply, action_id=None):
        if not reply:
            return
        msg = {
            "type": "text",
            "role": "assistant",
            "sender": self._sender,
            "content": reply,
            "timestamp": int(time.time() * 1000),
        }
        if action_id:
            msg["action_id"] = action_id
        self._publish(self.topic_ai, msg)

    def _on_tts_text(self, reply):
        if not reply or not self.tts or not self.tts.available():
            return
        try:
            wav = self.tts.synthesize_wav_bytes(reply)
            for frame in self.tts.frames(wav):
                self._publish(self.topic_audio, frame)
        except Exception as e:
            log.error("TTS synthesis error: %s", e)

    def _handle(self, kind, data):
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
                self._on_ai_reply("I couldn't hear that clearly. Could you say that again?")
                return

            log.info("STT Transcribed -> %r", text)

            # Echo the transcribed speech to update the Web-UI chat
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

        history = payload.get("history") or []
        result = self.pipeline.decide(text, history=history)
        self.pipeline.execute(
            result,
            on_cmd=self._on_cmd,
            on_audio=self._on_audio,
            on_tts_text=self._on_tts_text,
            on_ai_reply=self._on_ai_reply,
        )

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

    def _status_loop(self):
        while self._running:
            state = "online" if (self.mqtt and self.mqtt.is_connected()) else "offline"
            if self._busy and state == "online":
                state = "busy"

            payload = {
                "state": state,
                "llm": {
                    "provider": "groq" if self.llm else "none",
                    "model": (self.llm.model if self.llm else None),
                    "status": (self.llm.status if self.llm else "offline"),
                },
                "stt": bool(self.stt),
                "tts": bool(self.tts),
                "ts": int(time.time() * 1000),
            }
            if self.llm and self.llm.cache:
                payload["llm"]["cache"] = self.llm.cache_stats()
            self._publish(self.topic_ai_status, payload)
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

        log.info("AI service online for device %s", self.device_id)
        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self._running = False
            self.mqtt.loop_stop()


def main():
    ap = argparse.ArgumentParser(description="V2 Hexapod AI Voice & Companion Service")
    ap.add_argument("--device", default=os.environ.get("DEVICE_ID", "hexapod-s3-01"))
    ap.add_argument("--broker", default=os.environ.get("MQTT_HOST", "localhost"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
    ap.add_argument("--user", default=os.environ.get("MQTT_USER"))
    ap.add_argument("--password", default=os.environ.get("MQTT_PASS"))
    ap.add_argument("--actions", default=os.path.join(os.path.dirname(__file__), "actions.json"))
    ap.add_argument("--llm-model", default=os.environ.get("LLM_MODEL"))
    ap.add_argument("--llm-base-url", default=os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1"))
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