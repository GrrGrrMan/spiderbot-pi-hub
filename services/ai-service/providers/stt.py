# pi-hub/services/ai-service/providers/stt.py
# Local Speech-To-Text via faster-whisper ("tiny" int8 is Pi-4 friendly).
# Lazy imports: ai_service.py imports this module, but the heavy lib + model
# only load on first transcribe() (so --mock / config checks run without deps).
import io
import logging
import os
import wave

log = logging.getLogger("ai.stt")

DEFAULT_MODEL = "tiny"
DEFAULT_MODEL_DIR = "/opt/hexapod-ai/models"          # Pi layout
DEFAULT_MODEL_DIR_OVERRIDE = os.environ.get("AI_MODEL_DIR", DEFAULT_MODEL_DIR)


class STTClient:
    def __init__(self, model_name=DEFAULT_MODEL, model_dir=DEFAULT_MODEL_DIR_OVERRIDE):
        self.model_name = model_name
        self.model_dir = model_dir
        self._model = None
        self._load_error = None

    def _ensure(self):
        if self._model is not None:
            return self._model
        try:
            from faster_whisper import WhisperModel
            os.makedirs(self.model_dir, exist_ok=True)
            log.info("Loading faster-whisper '%s' (download_root=%s)…", self.model_name, self.model_dir)
            self._model = WhisperModel(self.model_name, device="cpu", compute_type="int8", download_root=self.model_dir)
            log.info("STT model ready")
        except Exception as e:      # missing lib OR missing model
            self._load_error = e
            log.error("STT unavailable: %s", e)
            return None
        return self._model

    def available(self):
        return self._ensure() is not None

    def transcribe_wav_bytes(self, wav_bytes):
        """Transcribe a 16 kHz mono 16-bit WAV (as sent by the web-ui mic).

        Returns the utterance text, or "" on failure.
        """
        model = self._ensure()
        if model is None:
            raise RuntimeError("STT model unavailable: %s" % self._load_error)

        with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
            if wf.getframerate() != 16000:
                log.warning("STT input rate %dHz (protocol says 16 kHz); still attempting", wf.getframerate())
            raw = wf.readframes(wf.getnframes())
            import numpy as np
            samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0

        segments, _info = model.transcribe(samples, beam_size=5)
        return "".join(s.text for s in segments).strip()