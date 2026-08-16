# pi-hub/services/ai-service/providers/tts.py
# Local Text-To-Speech via piper-tts 1.6 (OHF-Voice/piper1-gpl, the active fork).
# Generates a 22050 Hz mono 16-bit WAV, then yields it as base64 slices
# (<= TTS_FRAME_MAX_B64 chars each) matching the S3 chunked contract:
#   {"action":"tts","flow_id":...,"seq":...,"total":...,"payload":...}
import base64
import io
import logging
import os
import uuid
import wave

log = logging.getLogger("ai.tts")

TTS_FRAME_MAX_B64 = 4096
DEFAULT_VOICE_DIR = "/opt/hexapod-ai/voices"
DEFAULT_VOICE = "en_US-lessac-medium"


class TTSClient:
    def __init__(self, voice=DEFAULT_VOICE, voice_dir=DEFAULT_VOICE_DIR):
        self.voice = voice
        self.voice_dir = voice_dir
        self._voice = None
        self._error = None

    @property
    def model_path(self):
        return os.path.join(self.voice_dir, self.voice + ".onnx")

    def _ensure(self):
        if self._voice is not None:
            return self._voice
        try:
            from piper import PiperVoice   # piper1-gpl keeps the classic API
        except ImportError as e:
            self._error = "piper-tts missing: %s" % e
            log.error(self._error)
            return None
        try:
            log.info("Loading piper voice %s", self.model_path)
            self._voice = PiperVoice.load(self.model_path)
            log.info("TTS voice ready")
        except Exception as e:
            self._error = "piper voice load failed: %s" % e
            log.error(self._error)
            return None
        return self._voice

    def available(self):
        return self._ensure() is not None

    def synthesize_wav_bytes(self, text):
        """Speak text -> complete 22050 Hz mono 16-bit WAV bytes."""
        voice = self._ensure()
        if voice is None:
            raise RuntimeError("TTS voice unavailable: %s" % self._error)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)   # piper synthesizes mono
            # piper1 sets the WAV header itself on the file-like object; keep
            # setnchannels minimal and let synthesize() stamp the real params.
            voice.synthesize(text, wf)
        return buf.getvalue()

    def frames(self, wav_bytes, flow_id=None):
        """Yield the chunked MQTT TTS frame dicts for a WAV (contract in PLAN.md §3)."""
        flow_id = flow_id or uuid.uuid4().hex[:12]
        b64 = base64.b64encode(wav_bytes).decode("ascii")
        size = TTS_FRAME_MAX_B64
        chunks = [b64[i:i + size] for i in range(0, len(b64), size)]
        total = len(chunks)
        for seq, chunk in enumerate(chunks):
            yield {
                "action": "tts",
                "flow_id": flow_id,
                "seq": seq,
                "total": total,
                "payload": chunk,
            }
        log.info("TTS flow %s: %d frames from %d WAV bytes", flow_id, len(chunks), len(wav_bytes))