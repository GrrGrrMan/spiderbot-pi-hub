#!/usr/bin/env python3
# pi-hub/services/ai-service/ai_service.py
# P5 AI voice layer on the RPi (pi-hub). Loop:
#   web-ui mic -> STT (local faster-whisper) -> LLM (remote Groq, tools) | stage-1
#   keywords -> cmd/audio MQTT action + Piper TTS reply (chunked frames) -> S3 speaker
# Health heartbeat on hexapod/{id}/ai/status every 5s.
#
# CLI:  --mock            decide-and-print over a canned corpus (no broker/deps)
#       --no-llm          deterministic mode only   --no-stt / --no-tts to skip providers
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

MOCK_CORPUS = [
    "walk forward",
    "please go forward two steps",
    "turn left",
    "do a spin",
    "power off and sleep",
    "wake up",
    "make a beep",
    "play the curious sound",
    "tell me a fun fact",
    "",
]


class AIService:
    def __init__(self, args):
        self.device_id = args.device
        self.broker_host = args.broker
        self.broker_port = args.port
        self.mqtt_user = args.user
        self.mqtt_pass = args.password

        self.topic_cmd = "hexapod/cmd"                      # global cmd topic
        self.topic_cmd_dev = f"hexapod/{self.device_id}/cmd"
        self.topic_ai = f"hexapod/{self.device_id}/ai"
        self.topic_ai_status = f"hexapod/{self.device_id}/ai/status"
        self.topic_audio = f"hexapod/{self.device_id}/audio"

        self.actions = load_actions(args.actions)
        self.llm = LLMClient(model=args.llm_model, base_url=args.llm_base_url) if args.llm else None
        self.stt = STTClient() if args.stt else None
        self.tts = TTSClient() if args.tts else None
        self.pipeline = Pipeline(self.actions, llm=self.llm)

        self.mqtt = None
        self._work = queue.Queue(maxsize=16)
        self._busy = False
        self._running = True
        self._sender = f"ai-service-{self.device_id}"

    # --- MQTT ------------------------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        log.info("MQTT connected rc=%s", reason_code)
        client.subscribe(self.topic_ai)
        log.info("Subscribed to %s", self.topic_ai)

    def _on_message(self, client, userdata, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            log.warning("Bad payload on %s: %s", msg.topic, e)
            return
        if msg.topic == self.topic_ai:
            # Self-talk guard: this service publishes its own assistant replies to
            # topic_ai (and subscribes to it). Without this, those replies come back,
            # re-match motion keywords, and loop forever (observed as a cmd/ai storm).
            if data.get("role") in ("assistant", "system") or data.get("sender") == self._sender:
                return
            self._enqueue(data.get("type", "text"), data)

    def _enqueue(self, kind, data):
        try:
            self._work.put_nowait((kind, data))
        except queue.Full:
            log.warning("Worker queue full — dropping message")

    # --- publishers (injected into the pipeline) -----------------------------
    def _publish(self, topic, payload, qos=1):
        if not self.mqtt or not self.mqtt.is_connected():
            log.warning("MQTT not connected; dropping publish to %s", topic)
            return
        compact = json.dumps(payload)
        self.mqtt.publish(topic, compact, qos=qos)
        if len(compact) < 200:
            log.info("PUB %s -> %s", topic, compact)
        else:
            log.info("PUB %s -> <large payload %d bytes>", topic, len(compact))

    def _on_cmd(self, payload):
        self._publish(self.topic_cmd_dev, payload, qos=0)

    def _on_audio(self, payload):
        self._publish(self.topic_audio, payload, qos=0)

    def _on_ai_reply(self, reply):
        if not reply:
            return
        self._publish(self.topic_ai, {"type": "text", "role": "assistant", "sender": "ai-service", "content": reply})

    def _on_tts_text(self, reply):
        if not reply or not self.tts or not self.tts.available():
            return
        try:
            wav = self.tts.synthesize_wav_bytes(reply)
            for frame in self.tts.frames(wav):
                self._publish(self.topic_audio, frame, qos=0)
        except Exception as e:
            log.error("TTS synthesize failed: %s", e)

    # --- pipeline worker -------------------------------------------------------
    def _handle(self, kind, data):
        if kind == "audio":
            content = data.get("content", "")
            if not content:
                return
            wav_bytes = base64.b64decode(content) if isinstance(content, str) else bytes(content)
            if not self.stt:
                self._on_ai_reply("Voice is unavailable right now — try typing.")
                return
            text = self.stt.transcribe_wav_bytes(wav_bytes)
            log.info("STT -> %r", text)
            if not text:
                return
            payload = dict(data)
            payload.update({"type": "text", "content": text})
        else:
            payload = data
            text = payload.get("content", "")

        text = (text or "").strip()
        if not text:
            return
        # Full conversation memory (2026-08-17): the web-ui ships the prior
        # chat turns as a `history` array on every ai-text payload. Trimming
        # happens inside LLMClient.chat() (MAX_LLM_HISTORY). Defaults to []
        # for clients that don't send it (back-compat with older web-ui builds).
        history = payload.get("history") or []
        if not isinstance(history, list):
            log.warning("ignoring non-list history field (%s)", type(history).__name__)
            history = []
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
                log.exception("worker error")
            finally:
                self._busy = False
    def _status_loop(self):
        while self._running:
            state = "online"
            if not (self.mqtt and self.mqtt.is_connected()):
                state = "offline"
            elif self._busy:
                state = "busy"
            payload = {
                "state": state,
                "llm": {
                    "provider": "groq" if self.llm else "none",
                    "model": (self.llm.model if self.llm else None),
                    "status": (self.llm.status if self.llm else "offline"),
                    "error": (self.llm.last_error if self.llm else None),
                },
                "stt": bool(self.stt),
                "tts": bool(self.tts),
                "ts": int(time.time() * 1000),
            }
            # LLM response cache stats (2026-08-18): hit rate is the metric that
            # tells you whether the cache is actually saving Groq round-trips.
            # Surfaced in the heartbeat so the web-ui status bar can show it
            # without an extra MQTT round-trip.
            if self.llm and self.llm.cache is not None:
                payload["llm"]["cache"] = self.llm.cache_stats()
            self._publish(self.topic_ai_status, payload, qos=0)
            time.sleep(5)

    # -- lifecycle ----------------------------------------------------------
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

        log.info("AI service up (device=%s broker=%s:%s llm=%s stt=%s tts=%s)",
                 self.device_id, self.broker_host, self.broker_port,
                 "groq" if self.llm else "off", bool(self.stt), bool(self.tts))
        try:
            while self._running:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self._running = False
            self.mqtt.loop_stop()
            log.info("AI service stopped")


def mock_run(args):
    """Offline selftest of the decision chain (no broker, no models)."""
    service = AIService(args)
    ok = True
    for text in MOCK_CORPUS:
        result = service.pipeline.decide(text)
        action = result.action
        print("  %-35r -> action=%-14s reply=%r" % (text, action["id"] if action else None, result.reply))
        if action and "payload" not in action:
            ok = False
            print("    !! action missing payload")
        # Smoke-test execute() wiring with spy callbacks (no broker / models).
        calls = {"cmd": 0, "audio": 0, "tts": 0, "reply": 0}
        service.pipeline.execute(
            result,
            on_cmd=lambda p: calls.__setitem__("cmd", calls["cmd"] + 1),
            on_audio=lambda p: calls.__setitem__("audio", calls["audio"] + 1),
            on_tts_text=lambda r: calls.__setitem__("tts", calls["tts"] + 1),
            on_ai_reply=lambda r: calls.__setitem__("reply", calls["reply"] + 1),
        )
        expect_action_calls = 1 if action else 0
        if calls["cmd"] + calls["audio"] != expect_action_calls:
            ok = False
            print("    !! execute action wiring failed %s" % calls)
        if result.reply and (calls["tts"] != 1 or calls["reply"] != 1):
            ok = False
            print("    !! execute reply wiring failed %s" % calls)
    return 0 if ok else 2


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="V2 hexapod P5 AI voice service (RPi)")
    ap.add_argument("--device", default=os.environ.get("DEVICE_ID", "hexapod-s3-01"))
    ap.add_argument("--broker", default=os.environ.get("MQTT_HOST", "localhost"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("MQTT_PORT", "1883")))
    ap.add_argument("--user", default=os.environ.get("MQTT_USER"))
    ap.add_argument("--password", default=os.environ.get("MQTT_PASS"))
    ap.add_argument("--actions", default=os.path.join(here, "actions.json"))
    ap.add_argument("--llm-model", default=os.environ.get("LLM_MODEL", "llama-3.3-70b-versatile"))
    ap.add_argument("--llm-base-url", default=os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1"))
    ap.add_argument("--no-llm", action="store_true", help="force deterministic offline mode")
    ap.add_argument("--no-stt", action="store_true", help="disable local STT")
    ap.add_argument("--no-tts", action="store_true", help="disable local TTS")
    ap.add_argument("--mock", action="store_true", help="offline decision selftest, no broker")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    args.llm = os.environ.get("LLM_ENABLED", "1") != "0" and not args.no_llm
    args.stt = not args.no_stt
    args.tts = not args.no_tts

    if args.mock:
        sys.exit(mock_run(args))
    AIService(args).run()


if __name__ == "__main__":
    main()