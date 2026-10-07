
"""
Feature: flights_specific_area
Description: Real-time flight radar tracking. Detects and displays active aircraft, callsigns, speed, altitude, and flight paths in a specific area, district, city, or over the user's local region with a live visual radar map. Call whenever the user asks about flights, planes in the sky, airlines, or air traffic.
"""

FEATURE_METADATA = {
    "name": "flights_specific_area",
    "description": "Real-time flight radar tracking. Detects and displays active aircraft, callsigns, speed, altitude, and flight paths in a specific area, district, city, or over the user's local region with a live visual radar map. Call whenever the user asks about flights, planes in the sky, airlines, or air traffic.",
    "parameters": {"type": "OBJECT", "properties": {"area": {"type": "STRING", "description": "City, district, region, or area name (e.g. 'Kalyan', 'Mumbai', 'London', or 'my area'). Uses current device location for local requests."}}, "required": []},
    "version": "1.1.0",
    "active": True
}

import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

MAX_NETWORK_RESPONSE_BYTES = 4 * 1024 * 1024
_ALLOWED_REMOTE_HOSTS = frozenset({
    "geocoding-api.open-meteo.com",
    "opensky-network.org",
})


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.URLError("Redirects are disabled for flight-radar requests")


def _validate_remote_host(url: str) -> None:
    """Validate the fixed flight service host without performing a second DNS lookup."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_REMOTE_HOSTS:
        raise ValueError("Flight-radar remote host is not allowed.")


def _fetch_json(url: str, timeout: float) -> dict:
    _validate_remote_host(url)
    status, raw = fetch_public_bytes(
        url,
        timeout=timeout,
        max_response_bytes=MAX_NETWORK_RESPONSE_BYTES,
        headers={"User-Agent": "BrahmaAI-FlightRadar/1.0"},
    )
    if status >= 400:
        raise RuntimeError(f"Flight-radar service returned HTTP {status}.")
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Flight-radar service returned an unexpected response shape.")
    return data


def _validate_coordinates(latitude: float, longitude: float) -> tuple[float, float]:
    lat = float(latitude)
    lon = float(longitude)
    if not math.isfinite(lat) or not math.isfinite(lon):
        raise ValueError("Flight-radar coordinates must be finite.")
    if not -90.0 <= lat <= 90.0 or not -180.0 <= lon <= 180.0:
        raise ValueError("Flight-radar coordinates are out of range.")
    return lat, lon


def _geocode_location(area_name: str):
    lowered = str(area_name or "").lower().strip()
    if lowered in {'my area', 'local', 'here', 'current', 'device', 'current location'}:
        try:
            from core.device_location import get_device_location
            loc = get_device_location()
            if loc:
                lat, lon = _validate_coordinates(loc.get('latitude'), loc.get('longitude'))
                return lat, lon, loc.get('city') or 'Local Area'
        except Exception as exc:
            raise RuntimeError("Current device location is unavailable or invalid.") from exc
        raise RuntimeError("Current device location is unavailable.")

    clean_area = lowered.replace('district', '').replace('area', '').strip()
    if not clean_area:
        raise RuntimeError("A flight-radar area is required.")

    known_areas = {
        "kalyan": (19.23, 73.12, "Kalyan"),
        "dombiv": (19.23, 73.12, "Kalyan"),
        "mumbai": (19.07, 72.87, "Mumbai"),
        "bombay": (19.07, 72.87, "Mumbai"),
        "delhi": (28.61, 77.20, "Delhi"),
        "bangalore": (12.97, 77.59, "Bengaluru"),
        "bengaluru": (12.97, 77.59, "Bengaluru"),
    }
    for key, value in known_areas.items():
        if key in clean_area:
            return value

    url = f"https://geocoding-api.open-meteo.com/v1/search?name={urllib.parse.quote(clean_area)}&count=1"
    try:
        data = _fetch_json(url, timeout=5)
        results = data.get('results', []) if isinstance(data, dict) else []
        if results:
            lat, lon = _validate_coordinates(results[0]['latitude'], results[0]['longitude'])
            name = str(results[0].get('name') or clean_area.title())[:200]
            return lat, lon, name
    except Exception as exc:
        raise RuntimeError(f"Could not resolve flight-radar area: {exc}") from exc

    raise RuntimeError(f"Could not resolve flight-radar area '{area_name}'.")


def execute(**kwargs):
    area_input = kwargs.get('area') or kwargs.get('location') or kwargs.get('city') or 'local'
    try:
        center_lat, center_lon, area_name = _geocode_location(str(area_input))
    except Exception as exc:
        return {'error': str(exc)}

    delta = 0.6
    min_latitude = round(center_lat - delta, 3)
    max_latitude = round(center_lat + delta, 3)
    min_longitude = round(center_lon - delta, 3)
    max_longitude = round(center_lon + delta, 3)

    url = (f"https://opensky-network.org/api/states/all?"
           f"lamin={min_latitude}&lomin={min_longitude}&"
           f"lamax={max_latitude}&lomax={max_longitude}")

    flights = []
    try:
        data = _fetch_json(url, timeout=8)
        states = data.get('states') if isinstance(data, dict) else None
        if states is not None and not isinstance(states, list):
            raise ValueError("OpenSky returned malformed flight-state data.")
        if states:
            for s in states:
                if not isinstance(s, list) or len(s) < 11:
                    continue
                if s[6] is not None and s[5] is not None:
                    lat, lon = _validate_coordinates(s[6], s[5])
                    callsign = str(s[1]).strip()[:32] if s[1] else 'UNKNOWN'
                    alt_m = s[7]
                    vel_mps = s[9]
                    heading = s[10]
                    alt_ft = round(float(alt_m) * 3.28084) if alt_m is not None else None
                    vel_kmh = round(float(vel_mps) * 3.6) if vel_mps is not None else None
                    flights.append({
                        'icao24': str(s[0])[:32],
                        'callsign': callsign,
                        'origin_country': str(s[2])[:100] if s[2] else 'Unknown',
                        'latitude': lat,
                        'longitude': lon,
                        'altitude_ft': alt_ft,
                        'speed_kmh': vel_kmh,
                        'heading_deg': round(float(heading), 1) if heading is not None else None,
                        'on_ground': bool(s[8])
                    })
    except Exception as e:
        return {'error': f'Failed to query OpenSky live flight data: {e}'}

    num_flights = len(flights)
    image_path = None

    try:
        plt.style.use('dark_background')
        fig, ax = plt.subplots(figsize=(8, 6))
        fig.set_facecolor('#0B0F19')
        ax.set_facecolor('#0B0F19')

        ax.plot(center_lon, center_lat, marker='*', color='#F59E0B', markersize=14, label=f'{area_name} Center')
        ax.text(center_lon, center_lat - 0.05, f' {area_name}', color='#F59E0B', fontsize=10, fontweight='bold', ha='center')

        if num_flights > 0:
            lats = [f['latitude'] for f in flights]
            lons = [f['longitude'] for f in flights]
            ax.scatter(lons, lats, color='#00F0FF', s=80, alpha=0.85, edgecolors='white', linewidth=1, label=f'Active Aircraft ({num_flights})')
            for f in flights[:8]:
                ax.text(f['longitude'], f['latitude'] + 0.03, f['callsign'], color='#10B981', fontsize=8, ha='center')

        ax.set_title(f'Live Air Radar: {area_name} ({num_flights} Aircraft Tracked)', color='white', fontsize=12, fontweight='bold')
        ax.set_xlabel('Longitude', color='#94A3B8')
        ax.set_ylabel('Latitude', color='#94A3B8')
        ax.grid(True, linestyle='--', alpha=0.3, color='#1E293B')
        ax.legend(facecolor='#0F172A', edgecolor='#334155', loc='upper right')

        deliverables_dir = os.path.join(os.environ.get('LOCALAPPDATA', os.path.expanduser('~')), 'BrahmaAI', 'deliverables')
        os.makedirs(deliverables_dir, exist_ok=True)
        image_path = os.path.join(deliverables_dir, 'flights_radar_output.png')
        plt.savefig(image_path, bbox_inches='tight', dpi=140)
        plt.close(fig)
    except Exception:
        pass

    flight_names = ", ".join(f['callsign'] for f in flights[:5]) if flights else ""
    summary_text = (
        f"Tracked {num_flights} active aircraft over {area_name}. "
        + (f"Key flights: {flight_names}." if flight_names else "No airborne flights detected in immediate airspace right now.")
    )

    return {
        'title': f'Live Air Radar: {area_name}',
        'summary': summary_text,
        'flight_count': num_flights,
        'flights': flights[:10],
        'image_path': image_path
    }
