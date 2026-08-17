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

## Full conversation memory (2026-08-17)

The web-ui sends its visible chat log as a `history` array on every `hexapod/{id}/ai`
text/audio message, giving the LLM **full conversation memory** (not just the last
turn):

```json
{
  "type": "text",
  "role": "user",
  "content": "and what did you say before?",
  "history": [
    { "role": "user",      "content": "hello robot" },
    { "role": "assistant", "content": "Hi! I'm Hexa, your six-legged companion." }
  ]
}
```

- `history` is optional (back-compat with older web-ui builds). If absent → `[]`.
- It is **prior** turns only — the caller must NOT include the current `content` in
  `history` (the service appends it as the final user message itself).
- Roles are sanitized to `user|assistant|system`; anything else becomes `user`.
- `providers/llm.py` sends the **last `MAX_LLM_HISTORY` (50)** messages inside the
  request; the web-ui also caps at 50 and persists up to `MAX_PERSISTED_MESSAGES`
  (200) in `sessionStorage`. This fits llama-3.3-70b's 128 k token context.
- Non-list / malformed `history` is ignored with a warning (defensive).

## Env knobs (systemd `EnvironmentFile=/etc/hexapod-ai/ai.env`)

`DEVICE_ID` (default `hexapod-s3-01`), `LLM_MODEL`, `LLM_BASE_URL`,
`LLM_ENABLED` (`0` = deterministic only), `AI_MODEL_DIR`.

## LLM response cache (2026-08-18)

`providers/llm.py` keeps a small in-process TTL+LRU cache of
`(action_id, reply)` pairs keyed on `sha256(text + last-2-history + system)`.
Stage-1 keyword matching already short-circuits ~80% of phrases for free;
this catches the "user repeated themselves" / "same turn after page reload"
cases for the LLM-bound ~20%, saving a Groq round-trip.

Knobs (env, optional): `AI_LLM_CACHE_TTL` (default 60s),
`AI_LLM_CACHE_MAXSIZE` (default 256). Set `AI_LLM_CACHE_TTL=0` to disable.

Live stats (`hits`, `misses`, `evictions`, `size`) ride along on the
`hexapod/{id}/ai/status` heartbeat as `llm.cache` so the web-ui can show the
hit rate without an extra MQTT round-trip.