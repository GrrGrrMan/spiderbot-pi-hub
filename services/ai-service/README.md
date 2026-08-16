# V2 Hexapod P5 — AI Voice Service (`pi-hub/services/ai-service/`)

Voice layer for the S3 hexapod: **local STT + local TTS on the RPi, remote LLM
(Groq free tier)** per ADR-005 (amended 2026-08-16 — the 2 GB Pi 4 cannot host
an in-RAM LLM; local `ollama` is an optional Pi-5+ upgrade).

```
web-ui mic ──(16 kHz WAV base64)──> hexapod/{id}/ai     │  ai_service.py
                                   STT (faster-whisper) │  ─ pipeline ─┬ stage-1 keywords (offline)
                                   LLM (Groq tools)     │              └ remote LLM + function calling
                                   action + reply ──────┴───────────────┬--> hexapod/{id}/cmd   (motion/system)
                                                   TTS (piper)          └--> hexapod/{id}/audio (chunked frames)
```

## Layout

| File | Purpose |
|------|---------|
| `ai_service.py` | entry: MQTT client, worker thread, 5 s health heartbeat on `hexapod/{id}/ai/status`; `--mock` offline selftest |
| `action_parser.py` | stage-1 keyword matcher + LLM tool schema derived from `actions.json` |
| `pipeline.py` | decide (keywords → LLM → canned) + execute (publish + TTS + auto-stop TTL) |
| `providers/stt.py` | faster-whisper `tiny` int8 (lazy load) |
| `providers/llm.py` | OpenAI-compatible client (Groq default), key from `/etc/hexapod-ai/groq.key` |
| `providers/tts.py` | piper-tts 1.6 `en_US-lessac-medium`, emits ≤4 KB base64 frames |
| `actions.json` | **canonical action table** (mirrors `docs/future-roadmap/ai-voice/actions.json`) |
| `deploy/` | systemd unit + idempotent installer + artifact pre-download |
| `selftest.py` | stdlib-only contract/parser checks — runs without deps/broker |

## Install (RPi)

```sh
sudo ./deploy/install-ai-service.sh
sudo nano /etc/hexapod-ai/groq.key      # one line: sk-...
sudo chown spider:spider /etc/hexapod-ai/groq.key   # service runs as spider (unreadable root:600 = LLM stays offline)
sudo systemctl restart hexapod-ai
systemctl status hexapod-ai
```

Artifacts land in `/opt/hexapod-ai/{models,voices}` (whisper `tiny`, piper medium).
`faster-whisper`/`piper-tts` install into `/opt/hexapod-ai/venv` (never system pip).

## Verify

```sh
# PC/offline: parser + contract checks
python3 selftest.py

# PC/offline: decision chain over a canned corpus (no broker/models needed)
python3 ai_service.py --mock --no-llm

# On the Pi, after install:
systemctl logs -f -u hexapod-ai          # wait for "AI service up"
# from any MQTT client:
mosquitto_pub -t hexapod/hexapod-s3-01/ai -m '{"type":"text","role":"user","content":"do a spin"}'
mosquitto_sub -t 'hexapod/hexapod-s3-01/#' -v   # watch: cmd motion, ai reply, tts frames, ai/status
```

## Env knobs (systemd `EnvironmentFile=/etc/hexapod-ai/ai.env`)

`DEVICE_ID` (default `hexapod-s3-01`), `LLM_MODEL`, `LLM_BASE_URL`,
`LLM_ENABLED` (`0` = deterministic only), `AI_MODEL_DIR`.