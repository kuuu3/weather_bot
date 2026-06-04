#!/usr/bin/env python3
"""CWA weather monitor for LiDAR collection windows in Xizhi, New Taipei."""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from math import asin, cos, radians, sin, sqrt
from pathlib import Path
from typing import Any


TAIPEI_TZ = timezone(timedelta(hours=8))
DEFAULT_STATE_FILE = "state.json"
DEFAULT_CWA_BASE_URL = "https://opendata.cwa.gov.tw/api/v1/rest/datastore"
ORDINARY_RAIN_WORDS = ("小雨", "短暫雨", "陣雨", "有雨", "降雨", "rain")
HEAVY_RAIN_WORDS = (
    "豪雨",
    "大豪雨",
    "超大豪雨",
    "torrential rain",
    "extremely heavy rain",
)
SHORT_INTENSE_RAIN_WORDS = ("短時強降雨", "短延時強降雨", "劇烈降雨", "暴雨", "flash flood")
THUNDER_WORDS = ("雷", "雷雨", "雷陣雨", "thunder", "lightning")
TYPHOON_WORDS = ("颱風", "typhoon", "tropical cyclone")
DENSE_FOG_WORDS = ("濃霧", "dense fog")
WIND_WORDS = ("強風", "陣風", "strong wind", "gust", "wind advisory")


class file_lock:
    def __init__(self, path: Path, timeout_seconds: int = 10) -> None:
        self.path = path
        self.timeout_seconds = timeout_seconds
        self.fd: int | None = None

    def __enter__(self) -> "file_lock":
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(self.fd, str(os.getpid()).encode("utf-8"))
                return self
            except OSError as exc:
                if exc.errno != errno.EEXIST:
                    raise
                if time.monotonic() >= deadline:
                    try:
                        self.path.unlink()
                    except FileNotFoundError:
                        pass
                    deadline = time.monotonic() + self.timeout_seconds
                time.sleep(0.1)

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


@dataclass(frozen=True)
class Alert:
    category: str
    title: str
    reason: str
    severity: str
    start_time: str | None = None
    end_time: str | None = None
    source: str = "CWA"

    @property
    def dedupe_key(self) -> str:
        raw = "|".join(
            [
                self.category,
                self.title,
                self.reason,
                self.start_time or "",
                self.end_time or "",
                self.source,
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


class WeatherMonitor:
    def __init__(self, dry_run: bool = False) -> None:
        load_dotenv()
        self.dry_run = dry_run
        self.cwa_api_key = must_getenv("CWA_API_KEY")
        self.telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        self.base_url = os.getenv("CWA_BASE_URL", DEFAULT_CWA_BASE_URL).rstrip("/")
        self.location_name = os.getenv("LOCATION_NAME", "汐止區")
        self.county_name = os.getenv("COUNTY_NAME", "新北市")
        self.forecast_dataset_id = os.getenv("CWA_FORECAST_DATASET_ID", "F-D0047-069")
        self.all_township_forecast_dataset_id = os.getenv("CWA_ALL_TOWNSHIP_FORECAST_DATASET_ID", "F-D0047-093")
        self.warning_dataset_ids = csv_env(
            "CWA_WARNING_DATASET_IDS",
            "W-C0033-001,W-C0033-002",
        )
        self.lightning_dataset_id = os.getenv("CWA_LIGHTNING_DATASET_ID", "O-A0039-001")
        self.location_lat = float(os.getenv("LOCATION_LAT", "25.0642"))
        self.location_lon = float(os.getenv("LOCATION_LON", "121.6586"))
        self.lightning_alert_radius_km = float(os.getenv("LIGHTNING_ALERT_RADIUS_KM", "15"))
        self.lightning_alert_minutes = int(os.getenv("LIGHTNING_ALERT_MINUTES", "30"))
        self.rain_dataset_ids = csv_env("CWA_RAIN_DATASET_IDS", "O-A0002-001,O-A0001-001")
        self.rain_observation_radius_km = float(os.getenv("RAIN_OBSERVATION_RADIUS_KM", "8"))
        self.rain_active_threshold_mm = float(os.getenv("RAIN_ACTIVE_THRESHOLD_MM", "0.5"))
        self.rain_stop_threshold_mm = float(os.getenv("RAIN_STOP_THRESHOLD_MM", "0.1"))
        self.rain_sustained_minutes = int(os.getenv("RAIN_SUSTAINED_MINUTES", "60"))
        self.wet_ground_window_minutes = int(os.getenv("WET_GROUND_WINDOW_MINUTES", "120"))
        self.state_file = Path(os.getenv("STATE_FILE", DEFAULT_STATE_FILE))
        self.summary_interval_hours = int(os.getenv("SUMMARY_INTERVAL_HOURS", "8"))
        self.summary_interval_minutes = int(
            os.getenv("SUMMARY_INTERVAL_MINUTES", str(self.summary_interval_hours * 60))
        )
        self.summary_start_time = os.getenv("SUMMARY_START_TIME", "").strip()
        self.weather_reply_cooldown_seconds = int(os.getenv("WEATHER_REPLY_COOLDOWN_SECONDS", "20"))
        self.forecast_hours = int(os.getenv("FORECAST_HOURS", "12"))
        self.wind_alert_mps = float(os.getenv("WIND_ALERT_MPS", "10.8"))
        self.beaufort_alert = int(os.getenv("BEAUFORT_ALERT", "6"))
        self.visibility_alert_m = float(os.getenv("VISIBILITY_ALERT_M", "3000"))
        self.request_timeout = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "20"))

    def run(self, force_summary: bool = False) -> int:
        state = self.load_state()
        weather = self.collect_weather(state)
        self.update_rain_history(state, weather)
        alerts = self.detect_alerts(weather, state)
        new_alerts = [alert for alert in alerts if alert.dedupe_key not in state["sent_alerts"]]
        summary_will_send = force_summary or self.summary_due(state)

        if new_alerts:
            self.send_telegram(format_immediate_alerts(new_alerts, weather))
            for alert in new_alerts:
                state["sent_alerts"][alert.dedupe_key] = {
                    "category": alert.category,
                    "title": alert.title,
                    "sent_at": now_iso(),
                }

        if summary_will_send:
            self.send_telegram(format_summary(weather, alerts))
            state["last_summary_at"] = now_iso()

        state["last_run_at"] = now_iso()
        state["last_status"] = {
            "location": f"{weather['county']}{weather['location']}",
            "alerts_detected": len(alerts),
            "new_alerts_sent": len(new_alerts),
            "summary_sent": summary_will_send,
            "rain_history_points": len(state.get("rain_history", [])),
        }
        state["sent_alerts"] = prune_sent_alerts(state["sent_alerts"])

        if self.dry_run:
            print(format_console_report(weather, alerts, new_alerts))
        else:
            self.save_state(state)
        return 0

    def run_bot(self, monitor_interval_seconds: int = 900, poll_seconds: int = 3) -> int:
        if not self.telegram_bot_token or not self.telegram_chat_id:
            raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required for --bot")
        state = self.load_state()
        update_offset = state.get("telegram_update_offset")
        if update_offset is None:
            update_offset = self.telegram_latest_update_offset()
            state["telegram_update_offset"] = update_offset
            self.save_state(state)
        next_monitor_at = datetime.now(TAIPEI_TZ)
        print("Telegram bot mode started. Send /weather or 即時天氣.", flush=True)

        while True:
            try:
                now = datetime.now(TAIPEI_TZ)
                if now >= next_monitor_at:
                    self.run(force_summary=False)
                    next_monitor_at = now + timedelta(seconds=monitor_interval_seconds)

                updates = self.telegram_get_updates(update_offset, timeout=25)
                for update in updates:
                    update_id = update.get("update_id")
                    if isinstance(update_id, int):
                        update_offset = update_id + 1
                    self.handle_telegram_update(update)
                if updates:
                    state = self.load_state()
                    state["telegram_update_offset"] = update_offset
                    self.save_state(state)
            except KeyboardInterrupt:
                print("Telegram bot mode stopped.", flush=True)
                return 0
            except Exception as exc:
                print(f"telegram-bot error: {exc}", file=sys.stderr, flush=True)
                time.sleep(max(poll_seconds, 1))

    def telegram_get_updates(self, offset: int | None, timeout: int = 25) -> list[dict[str, Any]]:
        url = f"https://api.telegram.org/bot{self.telegram_bot_token}/getUpdates"
        params = {"timeout": str(timeout)}
        if offset is not None:
            params["offset"] = str(offset)
        payload = http_get_json(url, params, timeout + self.request_timeout)
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram getUpdates failed: {payload}")
        return payload.get("result", [])

    def telegram_latest_update_offset(self) -> int | None:
        url = f"https://api.telegram.org/bot{self.telegram_bot_token}/getUpdates"
        payload = http_get_json(url, {"timeout": "0"}, self.request_timeout)
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram getUpdates failed: {payload}")
        updates = payload.get("result", [])
        update_ids = [update.get("update_id") for update in updates if isinstance(update.get("update_id"), int)]
        if not update_ids:
            return None
        return max(update_ids) + 1

    def handle_telegram_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or update.get("edited_message") or {}
        text = str(message.get("text") or "").strip()
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        if not text or not chat_id:
            return
        if chat_id != str(self.telegram_chat_id):
            return

        weather_location = parse_weather_location_request(text)
        if weather_location:
            if weather_location.get("error"):
                self.send_telegram(str(weather_location["error"]), chat_id=chat_id)
            elif self.weather_reply_is_on_cooldown(chat_id):
                return
            else:
                self.reply_current_weather(chat_id, weather_location)
        else:
            location_update = parse_location_settings_request(text)
            if location_update:
                if location_update.get("error"):
                    self.send_telegram(str(location_update["error"]), chat_id=chat_id)
                else:
                    self.apply_location_settings(chat_id, location_update)
            elif is_location_settings_query(text):
                state = self.load_state()
                self.send_telegram(format_location_settings(state, self), chat_id=chat_id)
            else:
                summary_update = parse_summary_settings_request(text)
                if summary_update:
                    if summary_update.get("error"):
                        self.send_telegram(str(summary_update["error"]), chat_id=chat_id)
                    else:
                        self.apply_summary_settings(chat_id, summary_update)
                elif is_summary_settings_query(text):
                    state = self.load_state()
                    self.send_telegram(format_summary_settings(state, self), chat_id=chat_id)
                elif is_weather_request(text):
                    if self.weather_reply_is_on_cooldown(chat_id):
                        return
                    self.reply_current_weather(chat_id)
                elif text.startswith("/start") or text.startswith("/help") or "說明" in text:
                    self.send_telegram(format_bot_help(), chat_id=chat_id)

    def reply_current_weather(self, chat_id: str, location_override: dict[str, Any] | None = None) -> None:
        state = self.load_state()
        weather_state = dict(state)
        if location_override:
            weather_state["location_settings"] = location_override
        weather = self.collect_weather(weather_state)
        if not location_override:
            self.update_rain_history(state, weather)
        alert_state = {"rain_history": []} if location_override else state
        alerts = self.detect_alerts(weather, alert_state)
        state["last_weather_request_at"] = now_iso()
        if location_override:
            state["last_weather_request_at"] = now_iso()
        else:
            self.save_state(state)
        self.send_telegram(format_on_demand_weather(weather, alerts), chat_id=chat_id)

    def apply_summary_settings(self, chat_id: str, update: dict[str, Any]) -> None:
        state = self.load_state()
        settings = state.setdefault("summary_settings", {})
        if "interval_minutes" in update:
            settings["interval_minutes"] = update["interval_minutes"]
        if "start_time" in update:
            settings["start_time"] = update["start_time"]
        state["summary_settings_updated_at"] = now_iso()
        self.save_state(state)
        self.send_telegram(format_summary_settings(state, self), chat_id=chat_id)

    def apply_location_settings(self, chat_id: str, update: dict[str, Any]) -> None:
        state = self.load_state()
        settings = state.setdefault("location_settings", {})
        settings["county"] = update["county"]
        settings["location"] = update["location"]
        settings["forecast_dataset_id"] = update.get("forecast_dataset_id", self.all_township_forecast_dataset_id)
        state["location_settings_updated_at"] = now_iso()
        state["rain_history"] = []
        state["sent_alerts"] = {}
        self.save_state(state)
        self.send_telegram(format_location_settings(state, self), chat_id=chat_id)

    def weather_reply_is_on_cooldown(self, chat_id: str) -> bool:
        state = self.load_state()
        cooldowns = state.setdefault("weather_reply_cooldowns", {})
        last_at = parse_iso(cooldowns.get(chat_id))
        now = datetime.now(TAIPEI_TZ)
        if last_at and now - last_at < timedelta(seconds=self.weather_reply_cooldown_seconds):
            return True
        cooldowns[chat_id] = now_iso()
        self.save_state(state)
        return False

    def collect_weather(self, state: dict[str, Any] | None = None) -> dict[str, Any]:
        location_settings = effective_location_settings(state or {}, self)
        forecast = self.fetch_forecast(location_settings)
        location_lat = parse_number(forecast.get("latitude")) or location_settings["lat"]
        location_lon = parse_number(forecast.get("longitude")) or location_settings["lon"]
        warnings = self.fetch_warnings(location_settings["county"], location_settings["location"])
        observations = self.fetch_observations(location_lat, location_lon, location_settings["county"], location_settings["location"])
        return {
            "location": location_settings["location"],
            "county": location_settings["county"],
            "latitude": location_lat,
            "longitude": location_lon,
            "generated_at": now_iso(),
            "forecast": forecast,
            "warnings": warnings,
            "observations": observations,
        }

    def fetch_forecast(self, location_settings: dict[str, Any]) -> dict[str, Any]:
        data = self.cwa_get(
            location_settings["forecast_dataset_id"],
            {
                "LocationName": location_settings["location"],
                "format": "JSON",
            },
        )
        location = first_location(data, location_settings["location"], location_settings["county"])
        if not location:
            raise RuntimeError(f"CWA forecast did not include {location_settings['county']}{location_settings['location']}")
        return parse_forecast_location(location, self.forecast_hours)

    def fetch_warnings(self, county_name: str, location_name: str) -> list[dict[str, Any]]:
        warnings: list[dict[str, Any]] = []
        for dataset_id in self.warning_dataset_ids:
            try:
                data = self.cwa_get(dataset_id, {"format": "JSON"})
                warnings.extend(extract_warning_items(data, dataset_id, county_name, location_name))
            except Exception as exc:
                warnings.append(
                    {
                        "dataset_id": dataset_id,
                        "title": f"{dataset_id} fetch failed",
                        "description": str(exc),
                        "area": "",
                        "start_time": None,
                        "end_time": None,
                        "fetch_error": True,
                    }
                )
        return warnings

    def fetch_observations(self, location_lat: float, location_lon: float, county_name: str, location_name: str) -> list[dict[str, Any]]:
        observations: list[dict[str, Any]] = []
        if not self.lightning_dataset_id:
            lightning = []
        else:
            lightning = self.fetch_lightning_observations(location_lat, location_lon)
        observations.extend(lightning)
        observations.extend(self.fetch_rain_observations(location_lat, location_lon, county_name, location_name))
        return observations

    def fetch_lightning_observations(self, location_lat: float, location_lon: float) -> list[dict[str, Any]]:
        try:
            data = self.cwa_get(self.lightning_dataset_id, {"format": "JSON"})
            return extract_lightning_observations(
                data,
                location_lat,
                location_lon,
                self.lightning_alert_radius_km,
                self.lightning_alert_minutes,
            )
        except Exception as exc:
            return [
                {
                    "dataset_id": self.lightning_dataset_id,
                    "title": f"{self.lightning_dataset_id} fetch failed",
                    "description": str(exc),
                    "fetch_error": True,
                }
            ]

    def fetch_rain_observations(self, location_lat: float, location_lon: float, county_name: str, location_name: str) -> list[dict[str, Any]]:
        observations: list[dict[str, Any]] = []
        for dataset_id in self.rain_dataset_ids:
            try:
                data = self.cwa_get(dataset_id, {"format": "JSON"})
                observations.extend(
                    extract_rain_observations(
                        data,
                        dataset_id,
                        county_name,
                        location_name,
                        location_lat,
                        location_lon,
                        self.rain_observation_radius_km,
                    )
                )
            except Exception as exc:
                observations.append(
                    {
                        "type": "rain",
                        "dataset_id": dataset_id,
                        "title": f"{dataset_id} fetch failed",
                        "description": str(exc),
                        "fetch_error": True,
                    }
                )
        return observations

    def cwa_get(self, dataset_id: str, params: dict[str, str]) -> dict[str, Any]:
        url = f"{self.base_url}/{dataset_id}"
        merged = {"Authorization": self.cwa_api_key, **params}
        payload = http_get_json(url, merged, self.request_timeout)
        if payload.get("success") is False:
            raise RuntimeError(f"CWA returned success=false for {dataset_id}")
        return payload

    def send_telegram(self, text: str, chat_id: str | None = None) -> None:
        if self.dry_run:
            print("\n--- Telegram message preview ---")
            print(text)
            return
        if not self.telegram_bot_token or not self.telegram_chat_id:
            raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required outside --dry-run")
        url = f"https://api.telegram.org/bot{self.telegram_bot_token}/sendMessage"
        http_post_json(
            url,
            {
                "chat_id": chat_id or self.telegram_chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=self.request_timeout,
        )

    def load_state(self) -> dict[str, Any]:
        if not self.state_file.exists():
            return {"sent_alerts": {}, "last_summary_at": None, "last_run_at": None}
        with self.state_file.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
        state.setdefault("sent_alerts", {})
        state.setdefault("last_summary_at", None)
        state.setdefault("last_run_at", None)
        return state

    def save_state(self, state: dict[str, Any]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file = self.state_file.with_suffix(".lock")
        with file_lock(lock_file):
            with self.state_file.open("w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")

    def summary_due(self, state: dict[str, Any]) -> bool:
        last_summary_at = parse_iso(state.get("last_summary_at"))
        interval_minutes = effective_summary_interval_minutes(state, self)
        anchor_time = effective_summary_start_time(state, self)
        if anchor_time:
            latest_slot = latest_summary_slot(datetime.now(TAIPEI_TZ), anchor_time, interval_minutes)
            if latest_slot is None:
                return False
            if last_summary_at is None:
                return latest_slot <= datetime.now(TAIPEI_TZ)
            return last_summary_at < latest_slot <= datetime.now(TAIPEI_TZ)
        if last_summary_at is None:
            return True
        return datetime.now(TAIPEI_TZ) - last_summary_at >= timedelta(minutes=interval_minutes)

    def update_rain_history(self, state: dict[str, Any], weather: dict[str, Any]) -> None:
        rain_points = [
            item for item in weather.get("observations", [])
            if item.get("type") == "rain" and not item.get("fetch_error")
        ]
        if not rain_points:
            state.setdefault("rain_history", [])
            return
        current_mm = max(float(item.get("rain_mm", 0)) for item in rain_points)
        history = list(state.get("rain_history", []))
        history.append({"time": now_iso(), "rain_mm": current_mm})
        cutoff = datetime.now(TAIPEI_TZ) - timedelta(minutes=self.wet_ground_window_minutes + 60)
        state["rain_history"] = [
            item for item in history
            if parse_iso(item.get("time")) is None or parse_iso(item.get("time")) >= cutoff
        ]

    def detect_alerts(self, weather: dict[str, Any], state: dict[str, Any]) -> list[Alert]:
        alerts: list[Alert] = []
        alerts.extend(alerts_from_forecast(weather["forecast"], self))
        alerts.extend(alerts_from_warnings(weather["warnings"]))
        alerts.extend(alerts_from_observations(weather["observations"]))
        alerts.extend(alerts_from_wet_ground(state, self))
        return sorted(alerts, key=lambda alert: (severity_rank(alert.severity), alert.category))


def parse_forecast_location(location: dict[str, Any], forecast_hours: int) -> dict[str, Any]:
    elements = location.get("WeatherElement") or location.get("weatherElement") or []
    by_name = {element_name(item): item for item in elements if element_name(item)}
    return {
        "name": location.get("LocationName") or location.get("locationName"),
        "latitude": location.get("Latitude") or location.get("latitude"),
        "longitude": location.get("Longitude") or location.get("longitude"),
        "weather": parse_times(by_name.get("天氣現象") or by_name.get("Wx"), forecast_hours),
        "description": parse_times(by_name.get("天氣預報綜合描述") or by_name.get("WeatherDescription"), forecast_hours),
        "rain_probability": parse_times(by_name.get("6小時降雨機率") or by_name.get("12小時降雨機率") or by_name.get("PoP6h") or by_name.get("PoP"), forecast_hours),
        "wind_speed": parse_times(by_name.get("風速") or by_name.get("WindSpeed"), forecast_hours),
        "beaufort": parse_times(by_name.get("蒲福風級") or by_name.get("BeaufortScale"), forecast_hours),
        "wind_direction": parse_times(by_name.get("風向") or by_name.get("WindDirection"), forecast_hours),
        "visibility": parse_times(by_name.get("能見度") or by_name.get("Visibility"), forecast_hours),
        "raw_element_names": sorted(by_name.keys()),
    }


def parse_times(element: dict[str, Any] | None, forecast_hours: int) -> list[dict[str, Any]]:
    if not element:
        return []
    cutoff = datetime.now(TAIPEI_TZ) + timedelta(hours=forecast_hours)
    parsed: list[dict[str, Any]] = []
    for item in element.get("Time") or element.get("time") or []:
        start = item.get("StartTime") or item.get("startTime") or item.get("DataTime") or item.get("dataTime")
        end = item.get("EndTime") or item.get("endTime")
        value = element_value(item)
        start_dt = parse_iso(start)
        if start_dt and start_dt > cutoff:
            continue
        parsed.append({"start_time": start, "end_time": end, "value": value})
    return parsed


def element_value(item: dict[str, Any]) -> str:
    values = item.get("ElementValue") or item.get("elementValue") or []
    if isinstance(values, list) and values:
        first = values[0]
        if isinstance(first, dict):
            for key in (
                "Weather",
                "WeatherDescription",
                "ProbabilityOfPrecipitation",
                "WindSpeed",
                "BeaufortScale",
                "WindDirection",
                "Visibility",
                "Value",
                "value",
            ):
                if first.get(key) not in (None, ""):
                    return str(first[key])
            return " ".join(str(value) for value in first.values() if value not in (None, ""))
        return str(first)
    if isinstance(values, dict):
        return " ".join(str(value) for value in values.values() if value not in (None, ""))
    return ""


def alerts_from_forecast(forecast: dict[str, Any], monitor: WeatherMonitor) -> list[Alert]:
    alerts: list[Alert] = []
    text_slots = forecast.get("weather", []) + forecast.get("description", [])
    for slot in text_slots:
        text = slot.get("value", "")
        lower = text.lower()
        if contains_any(lower, TYPHOON_WORDS):
            alerts.append(make_forecast_alert("typhoon", "Typhoon-related weather forecast", text, "high", slot))
        elif contains_any(lower, HEAVY_RAIN_WORDS):
            alerts.append(make_forecast_alert("heavy_rain", "豪雨以上預報", text, "high", slot))
        elif contains_any(lower, SHORT_INTENSE_RAIN_WORDS):
            alerts.append(make_forecast_alert("short_intense_rain", "短時強降雨預報", text, "high", slot))
        elif contains_any(lower, DENSE_FOG_WORDS):
            alerts.append(make_forecast_alert("dense_fog", "濃霧預報", text, "medium", slot))
        elif contains_any(lower, WIND_WORDS):
            alerts.append(make_forecast_alert("wind", "強風預報", text, "medium", slot))

    for slot in forecast.get("wind_speed", []):
        speed = parse_number(slot.get("value"))
        if speed is not None and speed >= monitor.wind_alert_mps:
            alerts.append(make_forecast_alert("wind", "風速達 LiDAR 門檻", f"{speed:g} m/s", "medium", slot))

    for slot in forecast.get("beaufort", []):
        beaufort = parse_number(slot.get("value"))
        if beaufort is not None and beaufort >= monitor.beaufort_alert:
            alerts.append(make_forecast_alert("wind", "蒲福風級達 LiDAR 門檻", f"蒲福 {beaufort:g} 級", "medium", slot))

    for slot in forecast.get("visibility", []):
        visibility = parse_number(slot.get("value"))
        if visibility is not None and visibility <= monitor.visibility_alert_m:
            alerts.append(make_forecast_alert("visibility", "能見度達 LiDAR 門檻", f"{visibility:g} m", "medium", slot))

    return unique_alerts(alerts)


def alerts_from_observations(observations: list[dict[str, Any]]) -> list[Alert]:
    alerts: list[Alert] = []
    for observation in observations:
        if observation.get("fetch_error"):
            continue
        if observation.get("type") != "lightning":
            continue
        distance = observation.get("distance_km")
        time_text = human_time(observation.get("time"))
        distance_text = f"{distance:.1f} 公里" if isinstance(distance, (int, float)) else "附近"
        alerts.append(
            Alert(
                category="observed_thunderstorm",
                title="實際觀測到雷雨/閃電",
                reason=f"中央氣象署閃電觀測資料顯示，汐止周邊 {distance_text} 內有落雷紀錄，觀測時間：{time_text}。",
                severity="high",
                start_time=observation.get("time"),
                end_time=None,
                source=str(observation.get("dataset_id", "CWA lightning observation")),
            )
        )
    return unique_alerts(alerts)


def alerts_from_wet_ground(state: dict[str, Any], monitor: WeatherMonitor) -> list[Alert]:
    history = [
        item for item in state.get("rain_history", [])
        if parse_iso(item.get("time")) is not None
    ]
    if len(history) < 2:
        return []

    history.sort(key=lambda item: parse_iso(item["time"]) or datetime.min.replace(tzinfo=TAIPEI_TZ))
    latest = history[-1]
    latest_time = parse_iso(latest.get("time"))
    if latest_time is None or float(latest.get("rain_mm", 0)) > monitor.rain_stop_threshold_mm:
        return []

    sustained_cutoff = latest_time - timedelta(minutes=monitor.rain_sustained_minutes)
    wet_window_cutoff = latest_time - timedelta(minutes=monitor.wet_ground_window_minutes)
    rainy_points = [
        item for item in history
        if parse_iso(item.get("time")) is not None
        and parse_iso(item.get("time")) >= sustained_cutoff
        and float(item.get("rain_mm", 0)) >= monitor.rain_active_threshold_mm
    ]
    earlier_rain = [
        item for item in history
        if parse_iso(item.get("time")) is not None
        and parse_iso(item.get("time")) >= wet_window_cutoff
        and float(item.get("rain_mm", 0)) >= monitor.rain_active_threshold_mm
    ]
    if not rainy_points and not earlier_rain:
        return []
    first_rain_time = parse_iso(earlier_rain[0].get("time"))
    if first_rain_time is None:
        return []
    wet_minutes = int((latest_time - first_rain_time).total_seconds() // 60)
    if wet_minutes < monitor.rain_sustained_minutes:
        return []
    return [
        Alert(
            category="wet_ground_after_rain",
            title="雨後濕地面",
            reason=(
                f"前面約 {wet_minutes} 分鐘有持續降雨紀錄，現在雨勢已降到 "
                f"{float(latest.get('rain_mm', 0)):g} mm，地面可能仍濕，適合 LiDAR 雨後濕地面採集。"
            ),
            severity="medium",
            start_time=first_rain_time.isoformat(timespec="seconds"),
            end_time=latest_time.isoformat(timespec="seconds"),
            source="CWA rainfall observation + state.json",
        )
    ]


def alerts_from_warnings(warnings: list[dict[str, Any]]) -> list[Alert]:
    alerts: list[Alert] = []
    for warning in warnings:
        if warning.get("fetch_error"):
            continue
        if warning_is_expired(warning):
            continue
        text = " ".join(
            str(warning.get(key, ""))
            for key in ("title", "description", "instruction", "event", "phenomena")
        ).lower()
        if not text:
            continue
        if warning_is_summary_only(warning, text):
            continue
        category = None
        severity = "medium"
        if contains_any(text, TYPHOON_WORDS):
            category, severity = "typhoon", "high"
        elif contains_any(text, THUNDER_WORDS):
            category, severity = "thunderstorm", "high"
        elif contains_any(text, SHORT_INTENSE_RAIN_WORDS):
            category, severity = "short_intense_rain", "high"
        elif contains_any(text, HEAVY_RAIN_WORDS):
            category, severity = "heavy_rain", "high"
        elif contains_any(text, DENSE_FOG_WORDS):
            category = "dense_fog"
        elif contains_any(text, WIND_WORDS):
            if warning_has_wind_threshold(text):
                category = "wind"
        elif "能見度" in text:
            category = "visibility"
        if not category:
            continue
        alerts.append(
            Alert(
                category=category,
                title=warning_display_title(warning),
                reason=format_local_warning_reason(warning),
                severity=severity,
                start_time=warning.get("start_time"),
                end_time=warning.get("end_time"),
                source=str(warning.get("dataset_id", "CWA warning")),
            )
        )
    return unique_alert_events(alerts)


def warning_is_expired(warning: dict[str, Any]) -> bool:
    now = datetime.now(TAIPEI_TZ)
    end_time = parse_iso(warning.get("end_time"))
    if end_time is not None:
        return end_time < now - timedelta(hours=1)
    start_time = parse_iso(warning.get("start_time"))
    if start_time is not None:
        return start_time < now - timedelta(days=2)
    return False


def extract_warning_items(
    payload: dict[str, Any],
    dataset_id: str,
    county_name: str,
    location_name: str,
) -> list[dict[str, Any]]:
    found = []
    for node, context in walk_dicts_with_context(payload):
        text = json.dumps(node, ensure_ascii=False)
        if county_name not in text and location_name not in text:
            continue
        phenomena = pick_first(node, "phenomena") or context.get("phenomena", "")
        significance = pick_first(node, "significance") or context.get("significance", "")
        title = (
            join_title(phenomena, significance)
            or pick_first(node, "headline", "event", "title", "identifier", "dataset")
            or context.get("title", "")
        )
        description = pick_first(node, "description", "senderName", "web", "contentText")
        area = pick_first(node, "areaDesc", "areaName", "locationName", "geocodeName") or context.get("area", "")
        start_time = pick_first(node, "effective", "onset", "sent", "startTime") or context.get("start_time", "")
        end_time = pick_first(node, "expires", "endTime") or context.get("end_time", "")
        local_reason = summarize_local_warning(
            title=title,
            description=description or text,
            area=area,
            phenomena=phenomena,
            significance=significance,
            county_name=county_name,
            location_name=location_name,
        )
        if title or description or area:
            found.append(
                {
                    "dataset_id": dataset_id,
                    "title": title or warning_title_from_text(text),
                    "description": description or text[:500],
                    "local_reason": local_reason,
                    "area": area,
                    "start_time": start_time,
                    "end_time": end_time,
                    "event": pick_first(node, "event") or context.get("event", ""),
                    "instruction": pick_first(node, "instruction"),
                    "phenomena": phenomena,
                }
            )
    return [item for item in dedupe_dicts(found) if not warning_is_expired(item)]


def extract_lightning_observations(
    payload: dict[str, Any],
    location_lat: float,
    location_lon: float,
    radius_km: float,
    recent_minutes: int,
) -> list[dict[str, Any]]:
    observations = []
    cutoff = datetime.now(TAIPEI_TZ) - timedelta(minutes=recent_minutes)
    for node, context in walk_dicts_with_context(payload):
        lat = pick_coordinate(node, context, "lat")
        lon = pick_coordinate(node, context, "lon")
        if lat is None or lon is None:
            continue
        distance = haversine_km(location_lat, location_lon, lat, lon)
        if distance > radius_km:
            continue
        event_time = (
            pick_first(node, "DateTime", "DataTime", "ObsTime", "Time", "time", "datetime")
            or context.get("time", "")
        )
        parsed_time = parse_iso(event_time)
        if parsed_time is not None and parsed_time < cutoff:
            continue
        observations.append(
            {
                "type": "lightning",
                "dataset_id": "O-A0039-001",
                "time": event_time,
                "lat": lat,
                "lon": lon,
                "distance_km": distance,
                "raw": compact(json.dumps(node, ensure_ascii=False), 500),
            }
        )
    return dedupe_dicts(observations)


def extract_rain_observations(
    payload: dict[str, Any],
    dataset_id: str,
    county_name: str,
    location_name: str,
    location_lat: float,
    location_lon: float,
    radius_km: float,
) -> list[dict[str, Any]]:
    observations = []
    for node, context in walk_dicts_with_context(payload):
        text = json.dumps(node, ensure_ascii=False)
        in_target_area = county_name in text or location_name in text
        lat = pick_coordinate(node, context, "lat")
        lon = pick_coordinate(node, context, "lon")
        distance = None
        if lat is not None and lon is not None:
            distance = haversine_km(location_lat, location_lon, lat, lon)
            in_target_area = in_target_area or distance <= radius_km
        if not in_target_area:
            continue
        rain_reading = pick_recent_rain_reading(node)
        rain_mm = rain_reading["rain_mm"] if rain_reading else None
        if rain_mm is None or rain_mm < 0:
            continue
        observations.append(
            {
                "type": "rain",
                "dataset_id": dataset_id,
                "time": pick_first(node, "DateTime", "DataTime", "ObsTime", "Time", "time", "datetime") or context.get("time", ""),
                "station": pick_first(node, "StationName", "stationName", "StationId", "stationId"),
                "rain_mm": rain_mm,
                "rain_label": rain_reading.get("label", "近時雨量"),
                "distance_km": distance,
            }
        )
    return dedupe_dicts(observations)


def first_location(payload: dict[str, Any], location_name: str, county_name: str = "") -> dict[str, Any] | None:
    fallback = None
    for node in walk_dicts(payload):
        if node.get("LocationName") == location_name or node.get("locationName") == location_name:
            if county_name and county_name not in json.dumps(node, ensure_ascii=False):
                if fallback is None and (node.get("WeatherElement") or node.get("weatherElement")):
                    fallback = node
                continue
            if node.get("WeatherElement") or node.get("weatherElement"):
                return node
    return fallback


def walk_dicts(value: Any) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    if isinstance(value, dict):
        nodes.append(value)
        for child in value.values():
            nodes.extend(walk_dicts(child))
    elif isinstance(value, list):
        for child in value:
            nodes.extend(walk_dicts(child))
    return nodes


def walk_dicts_with_context(value: Any, context: dict[str, str] | None = None) -> list[tuple[dict[str, Any], dict[str, str]]]:
    context = dict(context or {})
    output: list[tuple[dict[str, Any], dict[str, str]]] = []
    if isinstance(value, dict):
        next_context = dict(context)
        title = pick_first(value, "headline", "event", "title", "identifier")
        area = pick_first(value, "areaDesc", "areaName", "locationName", "geocodeName")
        start_time = pick_first(value, "effective", "onset", "sent", "startTime")
        end_time = pick_first(value, "expires", "endTime")
        event_time = pick_first(value, "DateTime", "DataTime", "ObsTime", "Time", "time", "datetime")
        event = pick_first(value, "event")
        phenomena = pick_first(value, "phenomena")
        significance = pick_first(value, "significance")
        if title:
            next_context["title"] = title
        if area:
            next_context["area"] = area
        if start_time:
            next_context["start_time"] = start_time
        if end_time:
            next_context["end_time"] = end_time
        if event_time:
            next_context["time"] = event_time
        if event:
            next_context["event"] = event
        if phenomena:
            next_context["phenomena"] = phenomena
        if significance:
            next_context["significance"] = significance
        output.append((value, next_context))
        for child in value.values():
            output.extend(walk_dicts_with_context(child, next_context))
    elif isinstance(value, list):
        for child in value:
            output.extend(walk_dicts_with_context(child, context))
    return output


def format_immediate_alerts(alerts: list[Alert], weather: dict[str, Any]) -> str:
    lines = [
        f"LiDAR 特殊天氣即時提醒：{weather['county']}{weather['location']}",
        f"檢查時間：{human_time(weather['generated_at'])}",
        "",
    ]
    for alert in alerts:
        lines.extend(
            [
                f"[{severity_label(alert.severity)}] {display_alert_title(alert)}",
                f"原因：{compact(alert.reason, 260)}",
                f"影響時段：{format_window(alert.start_time, alert.end_time)}",
                "",
            ]
        )
    return "\n".join(lines).strip()


def format_summary(weather: dict[str, Any], alerts: list[Alert]) -> str:
    forecast = weather["forecast"]
    lines = [
        f"LiDAR 定期天氣摘要：{weather['county']}{weather['location']}",
        f"檢查時間：{human_time(weather['generated_at'])}",
        f"特殊天氣項目：{len(alerts)}",
        "",
        "預報重點：",
    ]
    lines.extend(format_slots("天氣", forecast.get("weather", []), 4))
    lines.extend(format_slots("降雨機率", forecast.get("rain_probability", []), 4))
    lines.extend(format_slots("風況", combine_wind(forecast), 4))
    lines.extend(format_slots("能見度", forecast.get("visibility", []), 3))

    forecast_thunder = thunder_forecast_slots(forecast)
    if forecast_thunder:
        lines.append("")
        lines.append("預報提到雷陣雨，僅列入摘要：")
        for slot in forecast_thunder[:4]:
            lines.append(f"- {human_time(slot.get('start_time'))}：{compact(str(slot.get('value', '')), 120)}")

    active_warnings = display_warnings(weather.get("warnings", []))
    lines.append("")
    lines.append(f"中央氣象署相關警特報：{len(active_warnings)}")
    for item in active_warnings[:5]:
        title = warning_display_title(item)
        reason = format_local_warning_reason(item)
        if reason:
            lines.append(f"- {title}：{compact(reason, 100)}")
        else:
            lines.append(f"- {title}")
    if not active_warnings:
        lines.append("- 目前沒有相關警特報。")

    if alerts:
        lines.append("")
        lines.append("適合 LiDAR 留意的特殊項目：")
        for alert in alerts[:8]:
            lines.append(f"- {display_alert_title(alert)}: {compact(alert.reason, 120)}")
    else:
        lines.append("")
        lines.append("目前沒有偵測到 LiDAR 特殊天氣；若有一般降雨，只列入摘要，不發即時通知。")

    return "\n".join(lines)


def format_on_demand_weather(weather: dict[str, Any], alerts: list[Alert]) -> str:
    forecast = weather["forecast"]
    lines = [
        f"汐止即時天氣查詢：{weather['county']}{weather['location']}",
        f"查詢時間：{human_time(weather['generated_at'])}",
        "",
    ]
    if alerts:
        lines.append(f"目前符合 LiDAR 即時條件：{len(alerts)}")
        for alert in alerts[:6]:
            lines.append(f"- [{severity_label(alert.severity)}] {display_alert_title(alert)}：{compact(alert.reason, 120)}")
    else:
        lines.append("目前沒有符合 LiDAR 即時通知條件。")

    lines.append("")
    lines.append("近期預報：")
    lines.extend(format_slots("天氣", forecast.get("weather", []), 3))
    lines.extend(format_slots("降雨機率", forecast.get("rain_probability", []), 2))
    lines.extend(format_slots("風況", combine_wind(forecast), 2))
    lines.extend(format_slots("能見度", forecast.get("visibility", []), 2))

    rain = latest_rain_observation(weather.get("observations", []))
    if rain:
        station = rain.get("station") or "附近測站"
        label = rain.get("rain_label") or "近時雨量"
        lines.append("")
        lines.append(f"雨量觀測：{station}，{label} {float(rain.get('rain_mm', 0)):g} mm")

    forecast_thunder = thunder_forecast_slots(forecast)
    if forecast_thunder:
        lines.append("")
        lines.append("預報提到雷陣雨，僅列入摘要，不代表即時警報：")
        for slot in forecast_thunder[:2]:
            lines.append(f"- {human_time(slot.get('start_time'))}：{compact(str(slot.get('value', '')), 100)}")

    lines.append("")
    lines.append("你也可以傳：/weather、即時天氣、天氣")
    return "\n".join(lines)


def format_bot_help() -> str:
    return "\n".join(
        [
            "汐止 LiDAR 天氣 Bot",
            "",
            "即時查詢：",
            "- /weather",
            "- 即時天氣",
            "- 天氣",
            "- 天氣 臺北市 信義區：臨時查詢指定地區，不改監測地區",
            "- /weather 新北市 汐止區：同上",
            "",
            "地區設定：",
            "- 地區設定：查看目前監測地區",
            "- 地區 新北市 汐止區：切換監測地區",
            "- 設定地區 臺北市 信義區：同上",
            "- /set_location 桃園市 龜山區：同上",
            "",
            "摘要排程：",
            "- 摘要設定：查看目前摘要間隔、起始時間、下次摘要時間",
            "- 摘要每 6 小時：修改摘要間隔",
            "- 摘要每 90 分鐘：也可以用分鐘",
            "- 摘要起始 08:00：設定每天對齊的起始時間",
            "",
            "說明：",
            "- /help：顯示這份功能清單",
            "- /start：顯示這份功能清單",
            "",
            "主動通知條件：",
            "- 豪雨以上",
            "- 短時強降雨",
            "- 雷雨即時警報或附近落雷觀測",
            "- 颱風影響",
            "- 濃霧",
            "- 能見度 <= 3000 m",
            "- 風速 >= 10.8 m/s",
            "- 雨後濕地面，前提是前面有持續降雨",
        ]
    )


def format_console_report(weather: dict[str, Any], alerts: list[Alert], new_alerts: list[Alert]) -> str:
    return (
        f"\n乾跑完成：{weather['county']}{weather['location']}\n"
        f"偵測到的特殊天氣：{len(alerts)}\n"
        f"若正式執行會發送的新即時提醒：{len(new_alerts)}\n"
    )


def format_slots(label: str, slots: list[dict[str, Any]], limit: int) -> list[str]:
    if not slots:
        return [f"- {label}：無資料"]
    return [
        f"- {label} {human_time(slot.get('start_time'))}：{compact(str(slot.get('value', '')), 100)}"
        for slot in slots[:limit]
    ]


def combine_wind(forecast: dict[str, Any]) -> list[dict[str, Any]]:
    speeds = forecast.get("wind_speed", [])
    directions = forecast.get("wind_direction", [])
    combined = []
    for index, speed in enumerate(speeds):
        direction = directions[index]["value"] if index < len(directions) else ""
        combined.append({**speed, "value": " ".join(str(part) for part in (direction, speed.get("value")) if part)})
    return combined


def thunder_forecast_slots(forecast: dict[str, Any]) -> list[dict[str, Any]]:
    slots = []
    for slot in forecast.get("weather", []) + forecast.get("description", []):
        if contains_any(str(slot.get("value", "")).lower(), THUNDER_WORDS):
            slots.append(slot)
    return slots


def latest_rain_observation(observations: list[dict[str, Any]]) -> dict[str, Any] | None:
    rain = [
        item for item in observations
        if item.get("type") == "rain" and not item.get("fetch_error")
    ]
    if not rain:
        return None
    return max(rain, key=lambda item: float(item.get("rain_mm", 0)))


def is_weather_request(text: str) -> bool:
    normalized = text.strip().lower()
    commands = {"/weather", "/weather@xizhi_weather_bot", "/now", "/now@xizhi_weather_bot"}
    if normalized in commands:
        return True
    return any(keyword in text for keyword in ("即時天氣", "現在天氣", "目前天氣", "天氣查詢", "天氣"))


def is_summary_settings_query(text: str) -> bool:
    normalized = text.strip().lower()
    return normalized in {"/summary", "/summary_settings"} or text.strip() in {"摘要設定", "摘要排程", "摘要時間"}


def is_location_settings_query(text: str) -> bool:
    normalized = text.strip().lower()
    return normalized in {"/location", "/location_settings"} or text.strip() in {"地區設定", "地區", "目前地區"}


def parse_weather_location_request(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    normalized = stripped.lower()
    prefixes = ("/weather", "/now", "天氣", "即時天氣", "現在天氣", "目前天氣", "天氣查詢")
    if not normalized.startswith(("/weather", "/now")) and not any(stripped.startswith(prefix) for prefix in prefixes[2:]):
        return None

    if normalized.startswith(("/weather", "/now")):
        parts = stripped.split()
        if len(parts) >= 3:
            return normalize_location_update(parts[1], parts[2])
        return None

    pattern = r"^(?:天氣|即時天氣|現在天氣|目前天氣|天氣查詢)\s*[:：]?\s*([^\s,，]+[市縣])\s*([^\s,，]+[區鄉鎮市])$"
    match = re.search(pattern, stripped)
    if match:
        return normalize_location_update(match.group(1), match.group(2))
    return None


def parse_location_settings_request(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    normalized = stripped.lower()
    if normalized.startswith(("/set_location", "/location_set")):
        parts = stripped.split()
        if len(parts) >= 3:
            return normalize_location_update(parts[1], parts[2])
        return {"error": "格式請用：/set_location 新北市 汐止區"}

    match = re.search(r"(?:設定地區|地區設定|改地區|地區)\s*[:：]?\s*([^\s,，]+[市縣])\s+([^\s,，]+[區鄉鎮市])", stripped)
    if match:
        return normalize_location_update(match.group(1), match.group(2))

    compact_match = re.search(r"(?:設定地區|改地區|地區)\s*[:：]?\s*([^\s,，]+?[市縣])([^\s,，]+[區鄉鎮市])", stripped)
    if compact_match:
        return normalize_location_update(compact_match.group(1), compact_match.group(2))

    return None


def normalize_location_update(county: str, location: str, forecast_dataset_id: str = "F-D0047-093") -> dict[str, Any]:
    county = normalize_taiwan_name(county)
    location = normalize_taiwan_name(location)
    if not county.endswith(("市", "縣")):
        return {"error": "縣市格式不完整，例如：新北市、臺北市、桃園市。"}
    if not location.endswith(("區", "鄉", "鎮", "市")):
        return {"error": "鄉鎮市區格式不完整，例如：汐止區、信義區、竹北市。"}
    return {
        "county": county,
        "location": location,
        "forecast_dataset_id": forecast_dataset_id,
    }


def normalize_taiwan_name(value: str) -> str:
    return value.strip().replace("台", "臺")


def effective_location_settings(state: dict[str, Any], monitor: WeatherMonitor) -> dict[str, Any]:
    settings = state.get("location_settings", {})
    county = normalize_taiwan_name(str(settings.get("county") or monitor.county_name))
    location = normalize_taiwan_name(str(settings.get("location") or monitor.location_name))
    forecast_dataset_id = str(settings.get("forecast_dataset_id") or monitor.forecast_dataset_id)
    if settings:
        forecast_dataset_id = str(settings.get("forecast_dataset_id") or monitor.all_township_forecast_dataset_id)
    return {
        "county": county,
        "location": location,
        "forecast_dataset_id": forecast_dataset_id,
        "lat": float(settings.get("lat") or monitor.location_lat),
        "lon": float(settings.get("lon") or monitor.location_lon),
    }


def format_location_settings(state: dict[str, Any], monitor: WeatherMonitor) -> str:
    settings = effective_location_settings(state, monitor)
    return "\n".join(
        [
            "LiDAR 監測地區設定",
            f"目前地區：{settings['county']}{settings['location']}",
            f"預報資料集：{settings['forecast_dataset_id']}",
            "",
            "可傳：",
            "- 地區 新北市 汐止區",
            "- 設定地區 臺北市 信義區",
            "- /set_location 桃園市 龜山區",
            "",
            "切換地區後，雨後濕地面歷史與已發警報會重置，避免不同地區互相污染。",
        ]
    )


def parse_summary_settings_request(text: str) -> dict[str, Any] | None:
    stripped = text.strip()
    update: dict[str, Any] = {}

    interval_match = re.search(r"(?:摘要)?每\s*(\d{1,4})\s*(小時|小时|hr|hrs|hour|hours|分鐘|分钟|min|mins|minute|minutes)", stripped, re.IGNORECASE)
    if interval_match:
        value = int(interval_match.group(1))
        unit = interval_match.group(2).lower()
        if unit in {"小時", "小时", "hr", "hrs", "hour", "hours"}:
            minutes = value * 60
        else:
            minutes = value
        if minutes < 15 or minutes > 1440:
            return {"error": "摘要間隔請設定在 15 分鐘到 24 小時之間。"}
        update["interval_minutes"] = minutes

    start_match = re.search(r"(?:摘要)?(?:起始|開始|开始|從|从|時間|时间)\s*(\d{1,2}):(\d{2})", stripped)
    if start_match:
        hour = int(start_match.group(1))
        minute = int(start_match.group(2))
        if hour > 23 or minute > 59:
            return {"error": "起始時間格式請用 HH:MM，例如 08:00。"}
        update["start_time"] = f"{hour:02d}:{minute:02d}"

    if update:
        return update
    if stripped.lower().startswith(("/summary_every", "/summary_interval")):
        parts = stripped.split()
        if len(parts) >= 2 and parts[1].isdigit():
            minutes = int(parts[1]) * 60
            if minutes < 15 or minutes > 1440:
                return {"error": "摘要間隔請設定在 15 分鐘到 24 小時之間。"}
            return {"interval_minutes": minutes}
    if stripped.lower().startswith(("/summary_start", "/summary_at")):
        parts = stripped.split()
        if len(parts) >= 2:
            parsed = parse_hhmm(parts[1])
            if parsed:
                return {"start_time": parts[1]}
            return {"error": "起始時間格式請用 HH:MM，例如 08:00。"}
    return None


def format_summary_settings(state: dict[str, Any], monitor: WeatherMonitor) -> str:
    settings = state.get("summary_settings", {})
    if settings.get("error"):
        return str(settings["error"])
    interval_minutes = effective_summary_interval_minutes(state, monitor)
    start_time = effective_summary_start_time(state, monitor)
    next_time = next_summary_time(datetime.now(TAIPEI_TZ), state, monitor)
    lines = [
        "LiDAR 摘要排程設定",
        f"摘要間隔：{format_interval(interval_minutes)}",
        f"起始時間：{start_time or '未設定，依上次摘要時間往後計算'}",
    ]
    if next_time:
        lines.append(f"下次摘要約：{next_time.strftime('%Y-%m-%d %H:%M')}")
    lines.extend(
        [
            "",
            "可傳：",
            "- 摘要每 6 小時",
            "- 摘要每 90 分鐘",
            "- 摘要起始 08:00",
        ]
    )
    return "\n".join(lines)


def effective_summary_interval_minutes(state: dict[str, Any], monitor: WeatherMonitor) -> int:
    value = state.get("summary_settings", {}).get("interval_minutes")
    try:
        minutes = int(value if value is not None else monitor.summary_interval_minutes)
    except (TypeError, ValueError):
        minutes = monitor.summary_interval_minutes
    return max(15, min(minutes, 1440))


def effective_summary_start_time(state: dict[str, Any], monitor: WeatherMonitor) -> str:
    value = state.get("summary_settings", {}).get("start_time") or monitor.summary_start_time
    return value if parse_hhmm(str(value)) else ""


def parse_hhmm(value: str) -> tuple[int, int] | None:
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value).strip())
    if not match:
        return None
    hour = int(match.group(1))
    minute = int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour, minute


def latest_summary_slot(now: datetime, start_time: str, interval_minutes: int) -> datetime | None:
    parsed = parse_hhmm(start_time)
    if not parsed:
        return None
    hour, minute = parsed
    anchor = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    while anchor > now:
        anchor -= timedelta(minutes=interval_minutes)
    while anchor + timedelta(minutes=interval_minutes) <= now:
        anchor += timedelta(minutes=interval_minutes)
    return anchor


def next_summary_time(now: datetime, state: dict[str, Any], monitor: WeatherMonitor) -> datetime | None:
    interval_minutes = effective_summary_interval_minutes(state, monitor)
    start_time = effective_summary_start_time(state, monitor)
    last_summary_at = parse_iso(state.get("last_summary_at"))
    if start_time:
        latest_slot = latest_summary_slot(now, start_time, interval_minutes)
        if latest_slot and (last_summary_at is None or last_summary_at < latest_slot):
            return latest_slot
        return (latest_slot or now) + timedelta(minutes=interval_minutes)
    if last_summary_at is None:
        return now
    return last_summary_at + timedelta(minutes=interval_minutes)


def format_interval(minutes: int) -> str:
    if minutes % 60 == 0:
        return f"{minutes // 60} 小時"
    if minutes > 60:
        return f"{minutes // 60} 小時 {minutes % 60} 分鐘"
    return f"{minutes} 分鐘"


def make_forecast_alert(category: str, title: str, reason: str, severity: str, slot: dict[str, Any]) -> Alert:
    return Alert(
        category=category,
        title=title,
        reason=reason,
        severity=severity,
        start_time=slot.get("start_time"),
        end_time=slot.get("end_time"),
        source="CWA township forecast",
    )


def unique_alerts(alerts: list[Alert]) -> list[Alert]:
    by_key = {}
    for alert in alerts:
        by_key[alert.dedupe_key] = alert
    return list(by_key.values())


def unique_alert_events(alerts: list[Alert]) -> list[Alert]:
    by_key = {}
    for alert in alerts:
        event_key = "|".join(
            [
                alert.category,
                normalize_warning_title(alert.title),
                alert.start_time or "",
                alert.end_time or "",
            ]
        )
        if event_key not in by_key:
            by_key[event_key] = alert
    return list(by_key.values())


def prune_sent_alerts(sent_alerts: dict[str, Any], keep_days: int = 14) -> dict[str, Any]:
    cutoff = datetime.now(TAIPEI_TZ) - timedelta(days=keep_days)
    pruned = {}
    for key, value in sent_alerts.items():
        sent_at = parse_iso(value.get("sent_at") if isinstance(value, dict) else None)
        if sent_at is None or sent_at >= cutoff:
            pruned[key] = value
    return pruned


def dedupe_dicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen = set()
    output = []
    for item in items:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        output.append(item)
    return output


def element_name(item: dict[str, Any]) -> str:
    return str(item.get("ElementName") or item.get("elementName") or "")


def contains_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word.lower() in text for word in words)


def parse_number(value: Any) -> float | None:
    if value is None:
        return None
    cleaned = "".join(ch if ch.isdigit() or ch in ".-" else " " for ch in str(value))
    for part in cleaned.split():
        try:
            return float(part)
        except ValueError:
            continue
    return None


def pick_coordinate(node: dict[str, Any], context: dict[str, str], axis: str) -> float | None:
    candidates = []
    for source in (node, context):
        for key, value in source.items():
            key_lower = str(key).lower()
            if axis == "lat" and ("lat" in key_lower or "緯度" in str(key)):
                candidates.append(value)
            if axis == "lon" and (
                "lon" in key_lower
                or "lng" in key_lower
                or "long" in key_lower
                or "經度" in str(key)
            ):
                candidates.append(value)
    for value in candidates:
        number = parse_number(value)
        if number is not None:
            return number
    return None


def pick_recent_rain_reading(node: dict[str, Any]) -> dict[str, Any] | None:
    preferred_patterns = (
        ("Past10Min", "10 分鐘雨量"),
        ("10Min", "10 分鐘雨量"),
        ("Past1hr", "1 小時雨量"),
        ("Past1Hour", "1 小時雨量"),
        ("Hour", "1 小時雨量"),
        ("Now", "目前雨量"),
        ("Precipitation", "近時雨量"),
    )
    excluded_patterns = ("24", "Day", "Daily", "Accum", "Total", "累積", "日雨量")
    candidates = []
    for item in walk_dicts(node):
        for key, value in item.items():
            key_text = str(key)
            if any(pattern.lower() in key_text.lower() for pattern in excluded_patterns):
                continue
            label = ""
            for pattern, pattern_label in preferred_patterns:
                if pattern.lower() in key_text.lower():
                    label = pattern_label
                    break
            if not label and "雨量" in key_text:
                label = "近時雨量"
            if not label:
                continue
            number = parse_number(value)
            if number is not None and number >= 0:
                priority = 0 if "10 分鐘" in label or "目前" in label else 1 if "1 小時" in label else 2
                candidates.append({"rain_mm": number, "label": label, "priority": priority})
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item["priority"], -float(item["rain_mm"])))
    best = candidates[0]
    return {"rain_mm": float(best["rain_mm"]), "label": best["label"]}


def warning_has_wind_threshold(text: str) -> bool:
    if "平均風6級以上" in text or "平均風 6 級以上" in text:
        return True
    if "陣風8級以上" in text or "陣風 8 級以上" in text:
        return True
    if "風速每秒" in text and parse_number(text) is not None and parse_number(text) >= 10.8:
        return True
    return False


def warning_is_summary_only(warning: dict[str, Any], text: str) -> bool:
    title = warning_display_title(warning)
    phenomena = str(warning.get("phenomena") or "")
    if ("大雨" in title or "大雨" in phenomena) and not (
        contains_any(text, HEAVY_RAIN_WORDS) or contains_any(text, SHORT_INTENSE_RAIN_WORDS)
    ):
        return True
    if title == "中央氣象署警特報" and str(warning.get("local_reason") or "").strip() in {"影響地區：新北市", "影響地區：汐止區"}:
        return True
    return is_rain_warning_summary_only(text)


def is_rain_warning_summary_only(text: str) -> bool:
    has_rain_warning = "大雨特報" in text or "大雨" in text
    has_lidar_rain = contains_any(text, HEAVY_RAIN_WORDS) or contains_any(text, SHORT_INTENSE_RAIN_WORDS)
    return has_rain_warning and not has_lidar_rain


def warning_display_title(warning: dict[str, Any]) -> str:
    title = str(warning.get("title") or warning.get("event") or "").strip()
    if title and title != "CWA warning":
        return title
    phenomena = str(warning.get("phenomena") or "").strip()
    significance = str(warning.get("significance") or "").strip()
    from_parts = join_title(phenomena, significance)
    if from_parts:
        return from_parts
    return warning_title_from_text(
        " ".join(str(warning.get(key, "")) for key in ("description", "local_reason", "instruction"))
    )


def display_alert_title(alert: Alert) -> str:
    title = alert.title.strip()
    if title and title != "CWA warning":
        return title
    return warning_title_from_text(alert.reason)


def warning_title_from_text(text: str) -> str:
    if "大雷雨" in text:
        return "大雷雨即時訊息"
    if "短時強降雨" in text or "短延時強降雨" in text:
        return "短時強降雨"
    if "豪雨" in text:
        return "豪雨特報"
    if "大雨" in text:
        return "大雨特報"
    if "陸上強風" in text or "強風" in text:
        return "陸上強風特報"
    if "濃霧" in text:
        return "濃霧特報"
    if "颱風" in text:
        return "颱風警報"
    return "中央氣象署警特報"


def display_warnings(warnings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_title = {}
    for warning in warnings:
        if warning.get("fetch_error") or warning_is_expired(warning):
            continue
        title = warning_display_title(warning)
        key = normalize_warning_title(title)
        if key not in by_title:
            by_title[key] = warning
            continue
        current_reason = format_local_warning_reason(by_title[key])
        next_reason = format_local_warning_reason(warning)
        if warning_reason_rank(next_reason) < warning_reason_rank(current_reason):
            by_title[key] = warning
    return list(by_title.values())


def warning_reason_rank(reason: str) -> tuple[int, int]:
    if "汐止" in reason:
        specificity = 0
    elif "新北市，請以汐止現地狀況判斷" in reason:
        specificity = 1
    elif "新北市" in reason:
        specificity = 2
    else:
        specificity = 3
    return (specificity, len(reason))


def format_local_warning_reason(warning: dict[str, Any]) -> str:
    local_reason = warning.get("local_reason")
    if local_reason:
        return str(local_reason)
    area = warning.get("area")
    phenomena = warning.get("phenomena")
    if area or phenomena:
        return "，".join(str(part) for part in (area, phenomena) if part)
    return compact(str(warning.get("description") or warning.get("instruction") or ""), 180)


def summarize_local_warning(
    title: str,
    description: str,
    area: str,
    phenomena: str,
    significance: str,
    county_name: str,
    location_name: str,
) -> str:
    local_parts = []
    title_text = join_title(phenomena, significance) or title
    if area:
        local_parts.append(f"影響地區：{area}")
    elif location_name in description:
        local_parts.append(f"影響地區：{location_name}")
    elif county_name in description:
        local_parts.append(f"影響地區：{county_name}，請以汐止現地狀況判斷")
    if title_text:
        local_parts.append(f"警特報：{title_text}")

    local_sentence = extract_local_sentence(description, location_name)
    if local_sentence:
        local_parts.append(local_sentence)

    return "；".join(local_parts) if local_parts else compact(description, 180)


def extract_local_sentence(text: str, keyword: str) -> str:
    if not keyword or keyword not in text:
        return ""
    normalized = text.replace("\\n", " ").replace("。", "。\n").replace("；", "；\n")
    for line in normalized.splitlines():
        if keyword in line:
            return compact(line.strip(), 160)
    return ""


def join_title(phenomena: str, significance: str) -> str:
    if phenomena and significance:
        return f"{phenomena}{significance}"
    return phenomena or significance


def normalize_warning_title(title: str) -> str:
    normalized = title.replace("CWA warning", "").strip()
    if "大雨" in normalized and "豪雨" not in normalized:
        return "大雨"
    if "強風" in normalized:
        return "強風"
    if "雷" in normalized:
        return "雷雨"
    if "豪雨" in normalized:
        return "豪雨"
    if "濃霧" in normalized:
        return "濃霧"
    if "颱風" in normalized:
        return "颱風"
    return normalized or title


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    earth_radius_km = 6371.0
    d_lat = radians(lat2 - lat1)
    d_lon = radians(lon2 - lon1)
    a = sin(d_lat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(d_lon / 2) ** 2
    return 2 * earth_radius_km * asin(sqrt(a))


def severity_rank(severity: str) -> int:
    return {"high": 0, "medium": 1, "low": 2}.get(severity, 3)


def severity_label(severity: str) -> str:
    return {"high": "高", "medium": "中", "low": "低"}.get(severity, severity)


def source_label(source: str) -> str:
    labels = {
        "CWA township forecast": "中央氣象署鄉鎮預報",
        "CWA": "中央氣象署",
    }
    return labels.get(source, source.replace("CWA", "中央氣象署"))


def pick_first(item: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def compact(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TAIPEI_TZ)
    return parsed.astimezone(TAIPEI_TZ)


def now_iso() -> str:
    return datetime.now(TAIPEI_TZ).isoformat(timespec="seconds")


def human_time(value: str | None) -> str:
    parsed = parse_iso(value)
    if parsed is None:
        return "unknown"
    return parsed.strftime("%Y-%m-%d %H:%M")


def format_window(start_time: str | None, end_time: str | None) -> str:
    start = human_time(start_time)
    end = human_time(end_time)
    if end == "unknown":
        return start
    return f"{start} 至 {end}"


def csv_env(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [part.strip() for part in raw.split(",") if part.strip()]


def load_dotenv(path: str = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    with env_path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)


def http_get_json(url: str, params: dict[str, str], timeout: int) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    full_url = f"{url}?{query}"
    request = urllib.request.Request(full_url, headers={"Accept": "application/json"})
    return http_json(request, timeout)


def http_post_json(url: str, payload: dict[str, Any], timeout: int) -> dict[str, Any]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    return http_json(request, timeout)


def http_json(request: urllib.request.Request, timeout: int) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    return json.loads(body)


def must_getenv(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise RuntimeError(f"{name} is required. Add it to .env.")
    return value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor CWA weather for Taiwan LiDAR collection.")
    parser.add_argument("--dry-run", action="store_true", help="Print Telegram messages instead of sending them.")
    parser.add_argument("--summary-now", action="store_true", help="Send/preview the regular summary immediately.")
    parser.add_argument("--bot", action="store_true", help="Run Telegram bot mode for /weather and 即時天氣 queries.")
    parser.add_argument("--monitor-interval-seconds", type=int, default=900, help="Monitoring interval in --bot mode.")
    parser.add_argument("--bot-poll-seconds", type=int, default=3, help="Retry delay after bot polling errors.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        monitor = WeatherMonitor(dry_run=args.dry_run)
        if args.bot:
            return monitor.run_bot(
                monitor_interval_seconds=args.monitor_interval_seconds,
                poll_seconds=args.bot_poll_seconds,
            )
        return monitor.run(force_summary=args.summary_now)
    except Exception as exc:
        print(f"weather-monitor error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
