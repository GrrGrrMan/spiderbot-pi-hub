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

try:
    import yt_dlp
    _HAS_YTDLP = True
except ImportError:
    _HAS_YTDLP = False

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

    def _resolve_source_url(self, query: str) -> tuple[Optional[str], str, str]:
        clean_q = query.strip()
        local_music = os.path.join(MUSIC_DIR, clean_q if clean_q.endswith((".mp3", ".wav", ".flac", ".ogg")) else f"{clean_q}.mp3")
        local_sfx = os.path.join(SFX_DIR, clean_q if clean_q.endswith((".mp3", ".wav", ".flac", ".ogg")) else f"{clean_q}.wav")

        if os.path.exists(local_music):
            return local_music, os.path.basename(local_music), "local"
        if os.path.exists(local_sfx):
            return local_sfx, os.path.basename(local_sfx), "local"

        if clean_q.startswith(("http://", "https://")):
            return clean_q, clean_q, "web"

        # 1. In-process Python yt_dlp extraction
        if _HAS_YTDLP:
            ydl_opts = {
                "format": "bestaudio/best",
                "noplaylist": True,
                "quiet": True,
                "no_warnings": True,
                "default_search": "ytsearch1",
                "extract_flat": False,
                "ignoreerrors": False,
            }
            try:
                search_target = clean_q if clean_q.startswith("ytsearch") else f"ytsearch1:{clean_q}"
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(search_target, download=False)
                    if info:
                        if "entries" in info and info["entries"]:
                            entry = info["entries"][0]
                            if entry and "url" in entry:
                                return entry["url"], entry.get("title", clean_q), "stream"
                        elif "url" in info:
                            return info["url"], info.get("title", clean_q), "stream"
            except Exception as e:
                log.warning("yt-dlp Python extraction error for '%s': %s", clean_q, e)

        # 2. Subprocess fallback
        try:
            yt_cmd = [
                "yt-dlp",
                "--default-search", "ytsearch1",
                "--print", "%(title)s",
                "--print", "%(url)s",
                "--format", "bestaudio/best",
                "--no-playlist",
                clean_q,
            ]
            res = subprocess.run(yt_cmd, capture_output=True, text=True, timeout=12.0)
            if res.returncode == 0:
                lines = [line.strip() for line in res.stdout.strip().split("\n") if line.strip()]
                if len(lines) >= 2:
                    return lines[1], lines[0], "stream"
                if len(lines) == 1 and lines[0].startswith(("http://", "https://")):
                    return lines[0], f"Search: {clean_q}", "stream"
            else:
                log.warning("yt-dlp CLI returned exit code %d: %s", res.returncode, res.stderr.strip())
        except Exception as e:
            log.warning("yt-dlp CLI resolution error for '%s': %s", clean_q, e)

        return None, clean_q, "not_found"

    def _stream_worker(self, source_url: str, flow_id: int):
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel", "error",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", source_url,
            "-f", "s16le",
            "-acodec", "pcm_s16le",
            "-ar", "22050",
            "-ac", "1",
            "pipe:1",
        ]

        try:
            with self._lock:
                self._ffmpeg_proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    bufsize=4096,
                )
        except Exception as e:
            log.error("Failed to launch ffmpeg audio transcode: %s", e)
            return

        seq = 0
        chunk_size = 4096
        sample_rate = 22050
        start_time = time.monotonic()
        total_samples_sent = 0
        remainder = b""

        try:
            while not self._stop_stream_event.is_set():
                self._pause_event.wait()
                
                # Trick the ESP32 into thinking a brand new stream has started after a pause
                if getattr(self, "_force_new_flow", False):
                    seq = 0
                    flow_id = random.randint(1, 0xFFFFFFFF)
                    self._force_new_flow = False
                    
                if self._stop_stream_event.is_set():
                        break

                with self._lock:
                    proc = self._ffmpeg_proc
                if not proc or not proc.stdout:
                    break

                raw_bytes = proc.stdout.read(chunk_size)
                if not raw_bytes:
                    break
                
                raw_bytes = remainder + raw_bytes
                if len(raw_bytes) % 2 != 0:
                    remainder = raw_bytes[-1:]
                    raw_bytes = raw_bytes[:-1]
                else:
                    remainder = b""
                    
                if not raw_bytes:
                    continue

                vol_pct = self.duck_volume if self.is_ducked else self.current_volume
                gain = max(0.0, min(1.0, vol_pct / 100.0))

                if gain < 0.99:
                    samples = np.frombuffer(raw_bytes, dtype=np.int16)
                    scaled_samples = (samples * gain).astype(np.int16)
                    payload_bytes = scaled_samples.tobytes()
                else:
                    payload_bytes = raw_bytes

                # Wrap sequence to 1 (not 0) so we don't accidentally trigger the ESP32's TTS_START logic
                safe_seq = (seq % 65534) + 1 if seq > 0 else 0
                header = struct.pack("<B B I H H", 0xAA, 0x00, flow_id, safe_seq, 0)
                frame = header + payload_bytes

                if self.publish_frame_fn:
                    self.publish_frame_fn(frame)

                seq += 1
                total_samples_sent += len(payload_bytes) // 2

                # Monotonic clock accumulator pacing to eliminate OS scheduler sleep drift
                expected_elapsed = total_samples_sent / sample_rate
                actual_elapsed = time.monotonic() - start_time
                sleep_needed = expected_elapsed - actual_elapsed
                if sleep_needed > 0.001:
                    time.sleep(sleep_needed)

        except Exception as e:
            log.warning("Audio streaming worker encountered exception: %s", e)
        finally:
            with self._lock:
                if self._ffmpeg_proc:
                    try:
                        _, errs = self._ffmpeg_proc.communicate(timeout=0.5)
                        if errs:
                            log.debug("FFmpeg stderr: %s", errs.decode("utf-8", errors="ignore").strip())
                    except Exception:
                        pass
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
        if not source_url:
            log.warning("Could not resolve media stream for query: '%s'", clean_q)
            return {"status": "error", "message": f"Could not find or stream audio for '{clean_q}'"}

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
        self._force_new_flow = True
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