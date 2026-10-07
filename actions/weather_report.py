# actions/weather_report.py

import json
import urllib.request
import webbrowser
from core.network_safety import open_fixed_https, read_bounded
from urllib.parse import quote_plus
from typing import Dict, Any, Optional


def get_live_weather(city: Optional[str] = None) -> Dict[str, Any]:
    """
    Fetches real-time live weather data using wttr.in with zero API keys required.
    Auto-detects device physical location dynamically via hardware/Wi-Fi and IP when no city is passed.
    Returns structured metrics: city, temp_c, condition, humidity, wind, and summary.
    """
    target_city = city.strip() if city and city.strip() else None
    if target_city and target_city.lower() in ("here", "my location", "current location", "auto", "local", "device"):
        target_city = None

    detected_lat = None
    detected_lon = None

    # Dynamically auto-detect physical device location
    if not target_city:
        try:
            from core.device_location import get_device_location
            loc = get_device_location()
            target_city = loc.get("city")
            detected_lat = loc.get("latitude")
            detected_lon = loc.get("longitude")
        except Exception as e:
            print(f"[Weather] Device location auto-detect notice: {e}")
            target_city = None

    if detected_lat is not None and detected_lon is not None:
        url = f"https://wttr.in/{detected_lat:.4f},{detected_lon:.4f}?format=j1"
    elif target_city:
        encoded_city = quote_plus(target_city)
        url = f"https://wttr.in/{encoded_city}?format=j1"
    else:
        url = "https://wttr.in/?format=j1"

    fallback = {
        "status": "unavailable",
        "city": target_city or "Local Area",
        "temp_c": None,
        "condition": "Unavailable",
        "humidity": None,
        "wind": None,
        "feels_like": None,
        "summary": "Weather telemetry temporarily offline.",
    }

    try:
        from urllib.parse import urlsplit
        if urlsplit(url).hostname not in {"wttr.in", "www.wttr.in"}:
            raise ValueError("Weather destination is outside the fixed host policy.")
        with open_fixed_https(
            url,
            allowed_hosts={"wttr.in", "www.wttr.in"},
            timeout=3.5,
            headers={"User-Agent": "curl/7.68.0"},
        ) as resp:
            raw = read_bounded(resp, 64 * 1024)
            data = json.loads(raw.decode("utf-8"))

            if not isinstance(data, dict):
                raise ValueError("Weather service returned an unexpected payload.")

            current_items = data.get("current_condition")
            if not isinstance(current_items, list) or not current_items:
                raise ValueError("Weather service omitted current conditions.")
            current = current_items[0]
            if not isinstance(current, dict):
                raise ValueError("Weather service returned invalid current conditions.")

            temp_raw = current.get("temp_C")
            feels_like_raw = current.get("FeelsLikeC", temp_raw)
            desc_items = current.get("weatherDesc")
            if temp_raw is None or not isinstance(desc_items, list) or not desc_items:
                raise ValueError("Weather service omitted required telemetry fields.")

            desc = desc_items[0] if isinstance(desc_items[0], dict) else {}
            condition = desc.get("value")
            humidity_raw = current.get("humidity")
            wind_raw = current.get("windspeedKmph")
            if condition in (None, "") or humidity_raw is None or wind_raw is None:
                raise ValueError("Weather service omitted required telemetry fields.")

            temp_c = int(temp_raw)
            feels_like = int(feels_like_raw)
            humidity = f"{humidity_raw}%"
            wind_speed = f"{wind_raw} km/h"

            detected_city = target_city
            if not detected_city:
                nearest_items = data.get("nearest_area")
                if isinstance(nearest_items, list) and nearest_items and isinstance(nearest_items[0], dict):
                    area_names = nearest_items[0].get("areaName")
                    if isinstance(area_names, list) and area_names and isinstance(area_names[0], dict):
                        detected_city = area_names[0].get("value")
            detected_city = detected_city or "Current Location"

            return {
                "status": "success",
                "city": detected_city,
                "temp_c": temp_c,
                "condition": str(condition),
                "humidity": humidity,
                "wind": wind_speed,
                "feels_like": feels_like,
                "summary": f"{temp_c}°C, {condition} in {detected_city}",
            }
    except Exception as e:
        print(f"[Weather] Live weather fetch notice: {e}")
        return fallback


def weather_action(
    parameters: dict,
    player=None,
    session_memory=None
):
    """
    Weather report action.
    Fetches real-time live weather metrics and optionally opens Google Weather.
    """
    city = parameters.get("city") if parameters else None
    time_param = parameters.get("time", "today") if parameters else "today"

    weather = get_live_weather(city)
    city_name = weather.get("city", "your area")
    search_query = f"weather in {city_name} {time_param}"

    if weather.get("status") != "success":
        msg = f"Live weather data for {city_name} is currently unavailable."
    else:
        temp = weather["temp_c"]
        cond = weather["condition"]
        msg = f"The weather in {city_name} is currently {temp} degrees Celsius with {cond}."

    _speak_and_log(msg, player)

    # Opening the browser is optional, but an explicitly requested launch must
    # not be reported as successful when the browser backend rejects it.
    if parameters and parameters.get("open_browser", False):
        try:
            encoded_query = quote_plus(search_query)
            opened = webbrowser.open(f"https://www.google.com/search?q={encoded_query}")
            if opened is not True:
                return f"{msg} I also couldn't open the browser automatically."
        except Exception:
            return f"{msg} I also couldn't open the browser automatically."

    if session_memory:
        try:
            session_memory.set_last_search(query=search_query, response=msg)
        except Exception:
            pass

    return msg


def _speak_and_log(message: str, player=None):
    if player:
        try:
            player.write_log(f"Brahma AI: {message}")
        except Exception:
            pass
