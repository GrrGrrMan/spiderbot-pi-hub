# pi-hub/services/ai-service/providers/stt.py
import io
import logging
import os
import wave

log = logging.getLogger("ai.stt")

DEFAULT_MODEL = "tiny"
DEFAULT_MODEL_DIR = "/opt/hexapod-ai/models"
DEFAULT_KEY_FILE = "/etc/hexapod-ai/groq.key"


class STTClient:
    def __init__(self, model_name=DEFAULT_MODEL, model_dir=DEFAULT_MODEL_DIR, key_file=DEFAULT_KEY_FILE):
        self.model_name = model_name
        self.model_dir = model_dir
        self.key_file = key_file
        self._local_model = None
        self._cloud_client = None

    def _get_api_key(self):
        env_key = os.environ.get("GROQ_API_KEY")
        if env_key:
            return env_key.strip()
        try:
            with open(self.key_file, "r") as f:
                return f.read().strip()
        except OSError:
            return None

    def _ensure_cloud(self):
        if self._cloud_client is not None:
            return self._cloud_client
        key = self._get_api_key()
        if key:
            try:
                from openai import OpenAI
                self._cloud_client = OpenAI(api_key=key, base_url="https://api.groq.com/openai/v1")
                log.info("Cloud Whisper STT ready (Groq)")
            except Exception as e:
                log.warning("Could not initialize Cloud Whisper: %s", e)
        return self._cloud_client

    def _ensure_local(self):
        if self._local_model is not None:
            return self._local_model
        try:
            from faster_whisper import WhisperModel
            os.makedirs(self.model_dir, exist_ok=True)
            log.info("Loading local faster-whisper '%s' from %s...", self.model_name, self.model_dir)
            self._local_model = WhisperModel(self.model_name, device="cpu", compute_type="int8", download_root=self.model_dir)
            log.info("Local STT model ready")
        except Exception as e:
            log.warning("Local STT unavailable: %s", e)
            return None
        return self._local_model

    def available(self):
        return self._get_api_key() is not None or self._ensure_local() is not None

    def transcribe_wav_bytes(self, wav_bytes):
        """Transcribe 16 kHz mono 16-bit WAV bytes using Cloud Whisper or local fallback."""
        # 1. Cloud Whisper (Fast Path: ~80ms)
        cloud = self._ensure_cloud()
        if cloud:
            try:
                buf = io.BytesIO(wav_bytes)
                buf.name = "audio.wav"
                try:
                    res = cloud.audio.transcriptions.create(
                        model="whisper-large-v3-turbo",
                        file=buf,
                        response_format="text"
                    )
                except Exception:
                    buf.seek(0)
                    res = cloud.audio.transcriptions.create(
                        model="whisper-large-v3",
                        file=buf,
                        response_format="text"
                    )
                text = res.strip() if isinstance(res, str) else getattr(res, "text", "").strip()
                if text:
                    log.info("Cloud STT -> %r", text)
                    return text
            except Exception as e:
                log.warning("Cloud Whisper failed, falling back to local: %s", e)

        # 2. Local Faster-Whisper (Offline Fallback: ~2s)
        local = self._ensure_local()
        if local:
            try:
                with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
                    raw = wf.readframes(wf.getnframes())
                    import numpy as np
                    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                segments, _ = local.transcribe(samples, beam_size=3)
                text = "".join(s.text for s in segments).strip()
                log.info("Local STT -> %r", text)
                return text
            except Exception as e:
                log.error("Local STT transcribe error: %s", e)

        return ""