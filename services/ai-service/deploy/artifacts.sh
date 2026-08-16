#!/usr/bin/env bash
# pi-hub/services/ai-service/deploy/artifacts.sh
# Pre-download the local AI artifacts so the service runs offline after install:
#   - faster-whisper "tiny" model  (/opt/hexapod-ai/models)
#   - piper voice en_US-lessac-medium + its .onnx.json  (/opt/hexapod-ai/voices)
set -euo pipefail

APP=/opt/hexapod-ai
MODELS=${AI_MODEL_DIR:-$APP/models}
VOICES=${PI_VOICE_DIR:-$APP/voices}
mkdir -p "$MODELS" "$VOICES"

if [ ! -x "$APP/venv/bin/python" ]; then
  echo "ERROR: venv missing — run install-ai-service.sh first" >&2
  exit 1
fi

echo "==> Whisper model"
"$APP/venv/bin/python" - "$MODELS" <<'PY'
import os, sys
root = sys.argv[1]
from faster_whisper import WhisperModel
WhisperModel("tiny", device="cpu", compute_type="int8", download_root=root)
print("faster-whisper 'tiny' ready in", root)
PY

echo "==> Piper voice"
VOICE=en_US-lessac-medium
BASE="https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/lessac/medium"
if [ ! -f "$VOICES/$VOICE.onnx" ]; then
  curl -fL --retry 3 -o "$VOICES/$VOICE.onnx" "$BASE/$VOICE.onnx"
fi
if [ ! -f "$VOICES/$VOICE.onnx.json" ]; then
  curl -fL --retry 3 -o "$VOICES/$VOICE.onnx.json" "$BASE/$VOICE.onnx.json"
fi
echo "piper voice ready -> $VOICES/"
echo "artifacts done"