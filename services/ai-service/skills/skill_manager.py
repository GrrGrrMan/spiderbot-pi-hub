# services/ai-service/skills/skill_manager.py
import logging
from typing import Any, Callable, Dict, List, Optional
from .time_skill import TimeSkill
from .weather_skill import WeatherSkill
from .search_skill import SearchSkill
from .media_skill import MediaSkill

log = logging.getLogger("ai.skills")


class SkillManager:
    def __init__(
        self,
        on_alarm_trigger: Optional[Callable[[str], None]] = None,
        on_speak_alert: Optional[Callable[[str], None]] = None,
        publish_audio_frame_fn: Optional[Callable[[bytes], None]] = None,
        fetch_snapshot_fn: Optional[Callable[[], Optional[str]]] = None,
    ):
        self.on_alarm_trigger = on_alarm_trigger
        self.on_speak_alert = on_speak_alert
        self.publish_audio_frame_fn = publish_audio_frame_fn
        self.fetch_snapshot_fn = fetch_snapshot_fn

        self.time_skill = TimeSkill(on_timer_fired=self._handle_timer_expired)
        self.weather_skill = WeatherSkill()
        self.search_skill = SearchSkill()
        self.media_skill = MediaSkill(publish_frame_fn=self.publish_audio_frame_fn)

    def duck_audio(self):
        """Pause media streaming to yield the MQTT audio topic to TTS."""
        self.media_skill.pause()

    def unduck_audio(self):
        """Resume media streaming after TTS completes."""
        self.media_skill.resume()

    def _handle_timer_expired(self, timer_data: Dict[str, Any]):
        label = timer_data.get("label", "Timer")
        alert_msg = f"Beep beep! Your {label} is done!"
        log.info("Triggering Timer Alarm -> %s", alert_msg)

        if self.on_alarm_trigger:
            self.on_alarm_trigger("startle")
        if self.on_speak_alert:
            self.on_speak_alert(alert_msg)

    def get_live_skills_state_block(self) -> str:
        """Constructs an instant zero-latency clock, location, active timers, and media status for the LLM prompt."""
        t_info = self.time_skill.get_current_time()
        active_timers = self.time_skill.list_active_timers()
        home_loc = self.weather_skill.default_location
        media_info = self.media_skill.get_status()

        lines = [
            f"- System Clock: {t_info['date']} — {t_info['time_12h']} ({t_info['time_24h']})",
            f"- Home/Default Location: {home_loc}",
            f"- Background Media Player: Track='{media_info['track']}' ({media_info['state'].upper()}, Vol: {media_info['volume']}%)",
        ]
        if active_timers:
            t_strs = [f"{t['label']} ({t['remaining_formatted']} remaining)" for t in active_timers]
            lines.append(f"- Active Timers: [{', '.join(t_strs)}]")
        else:
            lines.append("- Active Timers: None")

        return "\n### LIVE TIME & SYSTEM TOOLS STATE:\n" + "\n".join(lines) + "\n"

    def execute_skill(self, name: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Dispatches dynamic tool execution."""
        clean_name = (name or "").strip().lower()
        args = args or {}

        try:
            if clean_name in ("get_current_time", "get_time", "time"):
                return self.time_skill.get_current_time(args.get("timezone"))
            elif clean_name in ("set_timer", "timer"):
                dur = args.get("duration_seconds") or args.get("duration_s") or (args.get("minutes", 0) * 60) or 60
                label = args.get("label") or "Timer"
                return self.time_skill.set_timer(int(dur), label=label)
            elif clean_name in ("cancel_timer", "stop_timer"):
                target = args.get("timer_id") or args.get("label") or "all"
                return self.time_skill.cancel_timer(target)
            elif clean_name in ("list_timers", "get_timers"):
                return {"active_timers": self.time_skill.list_active_timers()}
            elif clean_name in ("get_weather", "weather"):
                loc = args.get("location") or args.get("city")
                return self.weather_skill.get_weather(loc)
            elif clean_name in ("web_search", "search", "search_web", "news_search", "google"):
                q = args.get("query") or args.get("q") or args.get("keyword") or ""
                return self.search_skill.search(q)
            elif clean_name in ("play_music", "play_audio", "play_song", "play_media", "play"):
                query = args.get("query") or args.get("song") or args.get("track") or args.get("sound") or ""
                return self.media_skill.play(query)
            elif clean_name in ("pause_music", "pause_audio", "pause"):
                return self.media_skill.pause()
            elif clean_name in ("resume_music", "resume_audio", "resume"):
                return self.media_skill.resume()
            elif clean_name in ("stop_music", "stop_audio", "stop_media"):
                return self.media_skill.stop()
            elif clean_name in ("set_media_volume", "music_volume"):
                vol = args.get("volume") or args.get("level", 50)
                return self.media_skill.set_volume(int(vol))
            elif clean_name in ("inspect_scene", "camera_snapshot", "get_snapshot"):
                if self.fetch_snapshot_fn:
                    img_b64 = self.fetch_snapshot_fn()
                    if img_b64:
                        return {"status": "success", "image_b64": img_b64, "message": "Camera frame acquired."}
                return {"status": "error", "message": "Camera feed unavailable or frame buffer empty."}
            else:
                return {"error": f"Unknown skill tool '{clean_name}'"}
        except Exception as e:
            log.error("Error executing skill '%s': %s", clean_name, e)
            return {"error": str(e)}