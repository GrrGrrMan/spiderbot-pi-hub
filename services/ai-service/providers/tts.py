# services/ai-service/providers/tts.py
import base64
import io
import json
import logging
import os
import threading
import time
import uuid
import wave
from typing import Any, Dict, List, Optional

try:
    import requests
    _HAS_REQUESTS = True
except ImportError:
    import urllib.request
    _HAS_REQUESTS = False

log = logging.getLogger("ai.tts")

TTS_FRAME_MAX_B64 = 4096
DEFAULT_VOICE_DIR = "/opt/hexapod-ai/voices"
DEFAULT_LOCAL_VOICE = "en_US-lessac-medium"
DEFAULT_FALLBACK_MODELS = [
    "hexapod-voice",
    "cartesia/sonic-english",
    "deepgram/aura-asteria-en",
    "openai/tts-1",
    "tts-1",
]


def convert_to_22050_mono(wav_bytes: bytes) -> bytes:
    """Ensures any cloud audio is strictly 22050 Hz Mono 16-bit for ESP32-S3 I2S."""
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            n_channels = wf.getnchannels()
            sampwidth = wf.getsampwidth()
            framerate = wf.getframerate()
            n_frames = wf.getnframes()
            raw_data = wf.readframes(n_frames)

        if framerate == 22050 and n_channels == 1 and sampwidth == 2:
            return wav_bytes

        import numpy as np

        if sampwidth == 2:
            audio = np.frombuffer(raw_data, dtype=np.int16)
        elif sampwidth == 1:
            audio = (np.frombuffer(raw_data, dtype=np.uint8).astype(np.int16) - 128) * 256
        else:
            return wav_bytes

        # Downmix stereo to mono
        if n_channels > 1:
            audio = audio.reshape(-1, n_channels).mean(axis=1).astype(np.int16)

        # Resample to 22050 Hz
        if framerate != 22050 and len(audio) > 0:
            num_target = int(len(audio) * 22050 / framerate)
            indices = np.linspace(0, len(audio) - 1, num_target)
            audio = np.interp(indices, np.arange(len(audio)), audio).astype(np.int16)

        out_buf = io.BytesIO()
        with wave.open(out_buf, "wb") as out_wf:
            out_wf.setnchannels(1)
            out_wf.setsampwidth(2)
            out_wf.setframerate(22050)
            out_wf.writeframes(audio.tobytes())
        return out_buf.getvalue()
    except Exception as e:
        log.warning("WAV resample conversion error: %s", e)
        return wav_bytes


class TTSClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        voice: str = DEFAULT_LOCAL_VOICE,
        voice_dir: str = DEFAULT_VOICE_DIR,
    ):
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL", "http://127.0.0.1:20128/v1")).rstrip("/")
        self.api_key = api_key or os.environ.get("LLM_API_KEY", "spiderbot")
        self.model = model or os.environ.get("TTS_MODEL", "hexapod-voice")
        self.voice_name = os.environ.get("TTS_VOICE", "alloy")
        self.timeout_s = float(os.environ.get("TTS_TIMEOUT_S", 3.0))

        self.voice = voice
        self.voice_dir = voice_dir
        self._local_voice = None
        self._local_ready = False
        self._http_session = requests.Session() if _HAS_REQUESTS else None

    @property
    def local_model_path(self) -> str:
        return os.path.join(self.voice_dir, self.voice + ".onnx")

    def warmup(self):
        """Warm up local Piper in the background so fallback is instant (0ms delay)."""
        threading.Thread(target=self._ensure_local_warm, daemon=True, name="tts-warmup").start()

    def _ensure_local_warm(self):
        if self._local_ready and self._local_voice is not None:
            return self._local_voice
        try:
            from piper import PiperVoice

            if os.path.exists(self.local_model_path):
                log.info("Pre-warming local Piper voice from %s...", self.local_model_path)
                t0 = time.time()
                self._local_voice = PiperVoice.load(self.local_model_path)
                # Prime memory pool with a micro-synthesis
                buf = io.BytesIO()
                with wave.open(buf, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(22050)
                    self._local_voice.synthesize_wav(" ", wf)
                self._local_ready = True
                log.info("Local Piper voice pre-warmed in %.2fs (Ready for 0ms offline fallback)", time.time() - t0)
        except Exception as e:
            log.warning("Could not pre-warm local Piper voice: %s", e)
        return self._local_voice

    def _synthesize_omniroute(self, text: str, model_candidate: str) -> Optional[bytes]:
        """Synthesizes speech using OmniRoute /v1/audio/speech."""
        url = f"{self.base_url}/audio/speech"
        payload = {
            "model": model_candidate,
            "input": text,
            "voice": self.voice_name,
            "response_format": "wav",
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        try:
            if _HAS_REQUESTS and self._http_session:
                resp = self._http_session.post(url, json=payload, headers=headers, timeout=self.timeout_s)
                if resp.status_code == 200 and len(resp.content) > 100:
                    return convert_to_22050_mono(resp.content)
                log.debug("OmniRoute TTS %s returned HTTP %d", model_candidate, resp.status_code)
            else:
                req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                    if resp.status == 200:
                        content = resp.read()
                        if len(content) > 100:
                            return convert_to_22050_mono(content)
        except Exception as e:
            log.debug("OmniRoute TTS failed for model %s: %s", model_candidate, e)
        return None

    def _synthesize_local(self, text: str) -> bytes:
        """Fallback synthesis using local pre-warmed Piper."""
        voice = self._ensure_local_warm()
        if voice is None:
            raise RuntimeError("Local Piper voice unavailable.")
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(22050)
            voice.synthesize_wav(text, wf)
        return buf.getvalue()

    def available(self) -> bool:
        return True

    def synthesize_wav_bytes(self, text: str) -> bytes:
        """Synthesizes speech using Cloud OmniRoute first, falling back to local Piper if offline."""
        clean_text = text.strip()
        if not clean_text:
            return b""

        # Direct Local Override
        if self.model.lower() in ("local", "piper", "offline", "none"):
            t0 = time.time()
            local_wav = self._synthesize_local(clean_text)
            log.info("TTS generated via Local Piper in %.2fs (%d bytes)", time.time() - t0, len(local_wav))
            return local_wav

        # 1. Primary: OmniRoute Cloud Speech
        candidates = [self.model] + [m for m in DEFAULT_FALLBACK_MODELS if m != self.model]
        for candidate in candidates:
            wav_bytes = self._synthesize_omniroute(clean_text, candidate)
            if wav_bytes:
                log.info("TTS generated via OmniRoute Cloud (%s, %d bytes)", candidate, len(wav_bytes))
                return wav_bytes

        # 2. Offline Fallback: Local Pre-Warmed Piper
        log.info("OmniRoute TTS unreachable or offline. Falling back to local Piper...")
        t0 = time.time()
        local_wav = self._synthesize_local(clean_text)
        log.info("TTS generated via Local Piper in %.2fs (%d bytes)", time.time() - t0, len(local_wav))
        return local_wav

    def frames(self, wav_bytes: bytes, flow_id: Optional[str] = None):
        """Yields chunked base64 MQTT frames for ESP32-S3."""
        flow_id = flow_id or uuid.uuid4().hex[:12]
        b64 = base64.b64encode(wav_bytes).decode("ascii")
        size = TTS_FRAME_MAX_B64
        chunks = [b64[i : i + size] for i in range(0, len(b64), size)]
        total = len(chunks)
        for seq, chunk in enumerate(chunks):
            yield {
                "action": "tts",
                "flow_id": flow_id,
                "seq": seq,
                "total": total,
                "payload": chunk,
            }