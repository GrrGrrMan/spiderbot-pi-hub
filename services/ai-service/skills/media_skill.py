# services/ai-service/skills/media_skill.py
import logging
import os
import random
import struct
import subprocess
import threading
import time
from typing import Any, Callable, Dict, List, Optional
import numpy as np

log = logging.getLogger("ai.skills.media")

MEDIA_ROOT = "/opt/hexapod-ai/media"
MUSIC_DIR = os.path.join(MEDIA_ROOT, "music")
SFX_DIR = os.path.join(MEDIA_ROOT, "sfx")


class MediaSkill:
    def __init__(
        self,
        publish_frame_fn: Optional[Callable[[bytes], None]] = None,
        default_volume: int = 50,
        duck_volume: int = 15,
    ):
        self.publish_frame_fn = publish_frame_fn
        self.default_volume = default_volume
        self.duck_volume = duck_volume
        self.current_volume = default_volume
        self.is_ducked = False
        self.current_track_title = "None"

        self._ffmpeg_proc: Optional[subprocess.Popen] = None
        self._stream_thread: Optional[threading.Thread] = None
        self._stop_stream_event = threading.Event()
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._lock = threading.Lock()

        self._ensure_directories()

    def _ensure_directories(self):
        for path in (MUSIC_DIR, SFX_DIR):
            os.makedirs(path, exist_ok=True)

    def _resolve_source_url(self, query: str) -> tuple[str, str, str]:
        clean_q = query.strip()
        local_music = os.path.join(MUSIC_DIR, clean_q if clean_q.endswith((".mp3", ".wav", ".flac", ".ogg")) else f"{clean_q}.mp3")
        local_sfx = os.path.join(SFX_DIR, clean_q if clean_q.endswith((".mp3", ".wav", ".flac", ".ogg")) else f"{clean_q}.wav")

        if os.path.exists(local_music):
            return local_music, os.path.basename(local_music), "local"
        if os.path.exists(local_sfx):
            return local_sfx, os.path.basename(local_sfx), "local"

        if clean_q.startswith(("http://", "https://")):
            return clean_q, clean_q, "web"

        try:
            yt_cmd = [
                "yt-dlp",
                "--default-search", "ytsearch1",
                "--get-url",
                "--get-title",
                "--format", "bestaudio/best",
                clean_q,
            ]
            res = subprocess.run(yt_cmd, capture_output=True, text=True, timeout=12.0)
            lines = [line.strip() for line in res.stdout.strip().split("\n") if line.strip()]
            if len(lines) >= 2:
                return lines[1], lines[0], "stream"
            if len(lines) == 1:
                return lines[0], f"Search: {clean_q}", "stream"
        except Exception as e:
            log.warning("yt-dlp resolution error for '%s': %s", clean_q, e)

        return clean_q, clean_q, "stream"

    def _stream_worker(self, source_url: str, flow_id: int):
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel", "error",
            "-i", source_url,
            "-f", "s16le",
            "-acodec", "pcm_s16le",
            "-ar", "22050",
            "-ac", "1",
            "pipe:1",
        ]

        try:
            self._ffmpeg_proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=4096,
            )
        except Exception as e:
            log.error("Failed to launch ffmpeg audio transcode: %s", e)
            return

        seq = 0
        chunk_size = 4096

        try:
            while not self._stop_stream_event.is_set():
                self._pause_event.wait()
                if self._stop_stream_event.is_set():
                    break

                raw_bytes = self._ffmpeg_proc.stdout.read(chunk_size)
                if not raw_bytes:
                    break

                if len(raw_bytes) % 2 != 0:
                    raw_bytes = raw_bytes[:-1]
                if not raw_bytes:
                    break

                vol_pct = self.duck_volume if self.is_ducked else self.current_volume
                gain = max(0.0, min(1.0, vol_pct / 100.0))

                if gain < 0.99:
                    samples = np.frombuffer(raw_bytes, dtype=np.int16)
                    scaled_samples = (samples * gain).astype(np.int16)
                    payload_bytes = scaled_samples.tobytes()
                else:
                    payload_bytes = raw_bytes

                header = struct.pack("<B B I H H", 0xAA, 0x00, flow_id, seq % 65535, 0)
                frame = header + payload_bytes

                if self.publish_frame_fn:
                    self.publish_frame_fn(frame)

                seq += 1
                time.sleep(len(payload_bytes) / 44100.0)

        except Exception as e:
            log.warning("Audio streaming worker encountered exception: %s", e)
        finally:
            if self._ffmpeg_proc:
                try:
                    self._ffmpeg_proc.terminate()
                    self._ffmpeg_proc.wait(timeout=1.0)
                except Exception:
                    self._ffmpeg_proc.kill()
                self._ffmpeg_proc = None
            log.info("Media streaming worker stopped (Flow: %d)", flow_id)

    def play(self, query: str) -> Dict[str, Any]:
        clean_q = str(query or "").strip()
        if not clean_q:
            return {"error": "Empty play query"}

        self.stop()

        source_url, track_name, source_type = self._resolve_source_url(clean_q)
        self.current_track_title = track_name
        self._stop_stream_event.clear()
        self._pause_event.set()

        flow_id = random.randint(1, 0xFFFFFFFF)
        self._stream_thread = threading.Thread(
            target=self._stream_worker,
            args=(source_url, flow_id),
            daemon=True,
            name="media-streamer",
        )
        self._stream_thread.start()

        log.info("Media stream dispatched to ESP32-S3 -> %s (%s)", track_name, source_type)
        return {
            "status": "playing",
            "track": track_name,
            "source": source_type,
        }

    def pause(self) -> Dict[str, Any]:
        self._pause_event.clear()
        return {"status": "paused"}

    def resume(self) -> Dict[str, Any]:
        self._pause_event.set()
        return {"status": "resumed"}

    def stop(self) -> Dict[str, Any]:
        self._stop_stream_event.set()
        self._pause_event.set()
        if self._ffmpeg_proc:
            try:
                self._ffmpeg_proc.terminate()
            except Exception:
                pass
        if self._stream_thread and self._stream_thread.is_alive():
            self._stream_thread.join(timeout=1.0)
        self._stream_thread = None
        self._ffmpeg_proc = None
        self.current_track_title = "None"
        return {"status": "stopped"}

    def set_volume(self, volume: int) -> Dict[str, Any]:
        vol = max(0, min(100, int(volume)))
        self.current_volume = vol
        return {"status": "volume_set", "volume": vol}

    def duck(self):
        self.is_ducked = True

    def unduck(self):
        self.is_ducked = False

    def get_status(self) -> Dict[str, Any]:
        is_playing = bool(self._stream_thread and self._stream_thread.is_alive() and self._pause_event.is_set())
        is_paused = bool(self._stream_thread and self._stream_thread.is_alive() and not self._pause_event.is_set())
        state = "playing" if is_playing else "paused" if is_paused else "idle"
        return {
            "track": self.current_track_title,
            "state": state,
            "volume": self.current_volume,
            "is_ducked": self.is_ducked,
        }

    def list_local_files(self) -> Dict[str, List[str]]:
        music_files = [f for f in os.listdir(MUSIC_DIR) if f.endswith((".mp3", ".wav", ".flac", ".ogg"))] if os.path.exists(MUSIC_DIR) else []
        sfx_files = [f for f in os.listdir(SFX_DIR) if f.endswith((".mp3", ".wav", ".flac", ".ogg"))] if os.path.exists(SFX_DIR) else []
        return {"music": music_files, "sfx": sfx_files}