# services/ai-service/skills/weather_skill.py
import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Any, Dict, Optional

log = logging.getLogger("ai.skills.weather")

WMO_WEATHER_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    71: "Slight snow fall", 73: "Moderate snow fall", 75: "Heavy snow fall",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail"
}


import os


class WeatherSkill:
    def __init__(self, default_location: Optional[str] = None, cache_ttl_s: int = 1200):
        self.cache_ttl_s = cache_ttl_s
        self._cache: Dict[str, Dict[str, Any]] = {}
        self.default_location = default_location or os.environ.get("DEFAULT_LOCATION") or self._auto_detect_location()

    def _auto_detect_location(self) -> str:
        """Auto-detects city name via free IP geolocation with safe fallback."""
        try:
            req = urllib.request.Request("http://ip-api.com/json/?fields=city,country", headers={"User-Agent": "HexapodAI/2.0"})
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                city = data.get("city")
                country = data.get("country")
                if city:
                    loc = f"{city}, {country}" if country else city
                    log.info("Auto-detected default location via IP: %s", loc)
                    return loc
        except Exception as e:
            log.debug("IP location lookup skipped: %s", e)
        return "Auckland, New Zealand"

    def get_weather(self, location: Optional[str] = None) -> Dict[str, Any]:
        """Fetches live weather from Open-Meteo (zero API keys required)."""
        raw_loc = str(location or "").strip()
        # Trap generic phrases like 'current location', 'here', 'local'
        if not raw_loc or raw_loc.lower() in ("current location", "here", "my location", "local", "none", "default", "city"):
            loc_name = self.default_location
        else:
            loc_name = raw_loc

        cache_key = loc_name.lower()
        now = time.time()

        if cache_key in self._cache:
            entry = self._cache[cache_key]
            if now - entry["cached_at"] < self.cache_ttl_s:
                return entry["data"]

        try:
            # 1. Geocode City Name -> Lat/Long
            geo_url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(loc_name)}&count=1&language=en&format=json"
            req = urllib.request.Request(geo_url, headers={"User-Agent": "HexapodRobot/2.0"})
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                geo_data = json.loads(resp.read().decode("utf-8"))

            results = geo_data.get("results")
            if not results:
                return {"error": f"Could not find coordinates for '{loc_name}'"}

            place = results[0]
            lat = place["latitude"]
            lon = place["longitude"]
            resolved_name = f"{place.get('name')}, {place.get('country', '')}".strip(", ")

            # 2. Fetch Meteorological Conditions
            meteo_url = (
                f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
                f"&current=temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m"
                f"&temperature_unit=celsius&wind_speed_unit=kmh"
            )
            req = urllib.request.Request(meteo_url, headers={"User-Agent": "HexapodRobot/2.0"})
            with urllib.request.urlopen(req, timeout=3.0) as resp:
                meteo_data = json.loads(resp.read().decode("utf-8"))

            current = meteo_data.get("current", {})
            w_code = current.get("weather_code", 0)
            condition = WMO_WEATHER_CODES.get(w_code, "Partly Cloudy")

            weather_res = {
                "location": resolved_name,
                "temperature_c": current.get("temperature_2m"),
                "feels_like_c": current.get("apparent_temperature"),
                "humidity_pct": current.get("relative_humidity_2m"),
                "condition": condition,
                "wind_speed_kmh": current.get("wind_speed_10m"),
                "precipitation_mm": current.get("precipitation"),
            }

            # Cache the result
            self._cache[cache_key] = {"cached_at": now, "data": weather_res}
            return weather_res

        except Exception as e:
            log.warning("Weather fetch failed for '%s': %s", loc_name, e)
            if cache_key in self._cache:
                old = self._cache[cache_key]["data"]
                old["note"] = "Cached data (network currently offline)"
                return old
            return {"error": f"Weather service unavailable: {e}"}