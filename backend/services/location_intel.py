"""
location_intel.py — Location intelligence using free APIs only.
No Google Places, no HERE Maps, no credit card required.

APIs used:
  - Overpass API (OpenStreetMap) — POI counting, competitor detection. NO KEY.
  - Nominatim (OpenStreetMap) — Geocoding. NO KEY.
  - LocationIQ — Geocoding + reverse geocoding. FREE KEY (pk.xxx).
  - Geoapify — Place search + autocomplete. FREE KEY.
  - Aladhan API — Prayer times from GPS. NO KEY.
  - Open-Meteo — Weather forecast. NO KEY.
"""
from __future__ import annotations
import os
import time
import httpx
from typing import Optional

LOCATIONIQ_KEY = os.getenv("LOCATIONIQ_API_KEY", "")
GEOAPIFY_KEY   = os.getenv("GEOAPIFY_API_KEY", "")

# Nominatim requires a User-Agent header — set your app name
NOMINATIM_UA = "WasteWise-AI/2.0 (contact@wastewise.my)"

# Rate limiting — Nominatim allows 1 req/sec max
_last_nominatim_call = 0.0


def _rate_limit_nominatim() -> None:
    global _last_nominatim_call
    elapsed = time.time() - _last_nominatim_call
    if elapsed < 1.1:
        time.sleep(1.1 - elapsed)
    _last_nominatim_call = time.time()


# ── Geocoding ──────────────────────────────────────────────────────────────

def geocode_address(address: str) -> Optional[dict]:
    """
    Convert an address string to GPS coordinates.
    Tries LocationIQ first (faster), falls back to Nominatim.
    Returns {'lat': float, 'lon': float, 'display_name': str} or None.
    """
    # Try LocationIQ first (5000 free req/day, no card)
    if LOCATIONIQ_KEY:
        try:
            r = httpx.get(
                "https://us1.locationiq.com/v1/search",
                params={
                    "key": LOCATIONIQ_KEY,
                    "q": address,
                    "format": "json",
                    "countrycodes": "my",
                    "limit": 1,
                },
                timeout=8,
            )
            if r.status_code == 200:
                data = r.json()
                if data:
                    return {
                        "lat": float(data[0]["lat"]),
                        "lon": float(data[0]["lon"]),
                        "display_name": data[0].get("display_name", ""),
                    }
        except Exception as e:
            print(f"[LocationIQ] Geocode failed: {e}")

    # Fallback: Nominatim (free, no key, 1 req/sec limit)
    try:
        _rate_limit_nominatim()
        r = httpx.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": address,
                "format": "json",
                "countrycodes": "my",
                "limit": 1,
            },
            headers={"User-Agent": NOMINATIM_UA},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            if data:
                return {
                    "lat": float(data[0]["lat"]),
                    "lon": float(data[0]["lon"]),
                    "display_name": data[0].get("display_name", ""),
                }
    except Exception as e:
        print(f"[Nominatim] Geocode failed: {e}")

    return None


def reverse_geocode(lat: float, lon: float) -> Optional[dict]:
    """
    Convert GPS coordinates to a human-readable address.
    Used to show restaurant area on the marketplace without revealing exact address.
    """
    if LOCATIONIQ_KEY:
        try:
            r = httpx.get(
                "https://us1.locationiq.com/v1/reverse",
                params={"key": LOCATIONIQ_KEY, "lat": lat, "lon": lon, "format": "json"},
                timeout=8,
            )
            if r.status_code == 200:
                data = r.json()
                addr = data.get("address", {})
                return {
                    "suburb": addr.get("suburb", ""),
                    "city": addr.get("city", addr.get("town", addr.get("village", ""))),
                    "state": addr.get("state", ""),
                    "display_name": data.get("display_name", ""),
                }
        except Exception as e:
            print(f"[LocationIQ] Reverse geocode failed: {e}")

    try:
        _rate_limit_nominatim()
        r = httpx.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "json"},
            headers={"User-Agent": NOMINATIM_UA},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            addr = data.get("address", {})
            return {
                "suburb": addr.get("suburb", ""),
                "city": addr.get("city", addr.get("town", addr.get("village", ""))),
                "state": addr.get("state", ""),
                "display_name": data.get("display_name", ""),
            }
    except Exception as e:
        print(f"[Nominatim] Reverse geocode failed: {e}")

    return None


# ── Points of Interest (Overpass API — NO KEY) ─────────────────────────────

def count_nearby_pois(lat: float, lon: float, radius_m: int = 1000) -> dict:
    """
    Count points of interest within radius_m metres using OpenStreetMap Overpass API.
    No API key required. Returns category counts used for area classification.
    """
    query = f"""
    [out:json][timeout:25];
    (
      node["amenity"~"school|university|college"](around:{radius_m},{lat},{lon});
      node["amenity"~"place_of_worship"](around:{radius_m},{lat},{lon});
      node["building"~"office|commercial"](around:{radius_m},{lat},{lon});
      node["shop"="mall"](around:{radius_m},{lat},{lon});
      node["amenity"~"hospital|clinic"](around:{radius_m},{lat},{lon});
      node["public_transport"~"station|stop_position"](around:{radius_m},{lat},{lon});
      node["amenity"~"restaurant|cafe|food_court|fast_food"](around:{radius_m},{lat},{lon});
    );
    out count;
    """
    try:
        r = httpx.post(
            "https://overpass-api.de/api/interpreter",
            data={"data": query},
            timeout=30,
        )
        if r.status_code == 200:
            data = r.json()
            tags = data.get("elements", [{}])[0].get("tags", {})
            return {
                "education":       int(tags.get("nodes", 0)),
                "worship":         int(tags.get("nodes", 0)),
                "offices":         int(tags.get("nodes", 0)),
                "malls":           int(tags.get("nodes", 0)),
                "medical":         int(tags.get("nodes", 0)),
                "transit":         int(tags.get("nodes", 0)),
                "food_competitors": int(tags.get("nodes", 0)),
            }
    except Exception as e:
        print(f"[Overpass] POI count failed: {e}")
    return {}


def get_nearby_competitors(lat: float, lon: float, radius_m: int = 1000) -> list:
    """
    Find nearby food businesses using Overpass API (OpenStreetMap).
    Returns list of competitor dicts with name, type, lat, lon.
    No API key required.
    """
    query = f"""
    [out:json][timeout:30];
    (
      node["amenity"~"restaurant|cafe|food_court|fast_food|bar"](around:{radius_m},{lat},{lon});
      node["shop"="food"](around:{radius_m},{lat},{lon});
    );
    out body;
    """
    competitors = []
    try:
        r = httpx.post(
            "https://overpass-api.de/api/interpreter",
            data={"data": query},
            timeout=35,
        )
        if r.status_code == 200:
            elements = r.json().get("elements", [])
            for el in elements[:50]:  # Cap at 50 competitors
                tags = el.get("tags", {})
                competitors.append({
                    "name": tags.get("name", "Unnamed"),
                    "type": tags.get("amenity", "restaurant"),
                    "cuisine": tags.get("cuisine", ""),
                    "lat": el.get("lat", lat),
                    "lon": el.get("lon", lon),
                    "source": "openstreetmap",
                })
    except Exception as e:
        print(f"[Overpass] Competitor search failed: {e}")
    return competitors


def classify_area_type(poi_counts: dict) -> str:
    """
    Classify the restaurant's area type based on POI counts.
    Returns: 'university', 'office_district', 'community', 'tourist', 'general'
    """
    edu = poi_counts.get("education", 0)
    offices = poi_counts.get("offices", 0)
    transit = poi_counts.get("transit", 0)

    if edu >= 3:
        return "university"
    if offices >= 50:
        return "office_district"
    if transit >= 5:
        return "transit_hub"
    return "community"


# ── Prayer Times (Aladhan API — NO KEY) ───────────────────────────────────

def get_prayer_times(lat: float, lon: float, date: str = None) -> Optional[dict]:
    """
    Get Malaysian prayer times for a GPS location using Aladhan API.
    Uses JAKIM calculation method (method=17 is Malaysia).
    No API key required.
    """
    import datetime as dt
    if not date:
        date = dt.date.today().strftime("%d-%m-%Y")

    try:
        r = httpx.get(
            "https://api.aladhan.com/v1/timings/" + date,
            params={"latitude": lat, "longitude": lon, "method": 17},
            timeout=10,
        )
        if r.status_code == 200:
            timings = r.json().get("data", {}).get("timings", {})
            return {
                "fajr":    timings.get("Fajr", ""),
                "syuruk":  timings.get("Sunrise", ""),
                "dhuhr":   timings.get("Dhuhr", ""),
                "asr":     timings.get("Asr", ""),
                "maghrib": timings.get("Maghrib", ""),
                "isyak":   timings.get("Isha", ""),
            }
    except Exception as e:
        print(f"[Aladhan] Prayer times failed: {e}")
    return None


# ── Weather (Open-Meteo — NO KEY) ─────────────────────────────────────────

def get_weather_forecast(lat: float, lon: float) -> Optional[dict]:
    """
    Get 3-day weather forecast from Open-Meteo (completely free, no key).
    Returns rain probability and temperature for today + next 2 days.
    """
    try:
        r = httpx.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "daily": "precipitation_probability_max,temperature_2m_max,temperature_2m_min",
                "timezone": "Asia/Kuala_Lumpur",
                "forecast_days": 3,
            },
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            daily = data.get("daily", {})
            dates = daily.get("time", [])
            rain = daily.get("precipitation_probability_max", [])
            temp_max = daily.get("temperature_2m_max", [])
            return {
                "today": {
                    "date": dates[0] if dates else "",
                    "rain_probability": rain[0] if rain else 0,
                    "temp_max": temp_max[0] if temp_max else 30,
                },
                "tomorrow": {
                    "date": dates[1] if len(dates) > 1 else "",
                    "rain_probability": rain[1] if len(rain) > 1 else 0,
                    "temp_max": temp_max[1] if len(temp_max) > 1 else 30,
                },
            }
    except Exception as e:
        print(f"[Open-Meteo] Weather failed: {e}")
    return None


# ── Address Autocomplete (Geoapify — FREE KEY) ─────────────────────────────

def autocomplete_address(query: str, lat: float = 3.1390, lon: float = 101.6869) -> list:
    """
    Address autocomplete for registration form.
    Uses Geoapify (3000 free req/day, no card required).
    Biased towards Malaysia (KL coordinates as default bias).
    """
    if not GEOAPIFY_KEY or len(query) < 3:
        return []

    try:
        r = httpx.get(
            "https://api.geoapify.com/v1/geocode/autocomplete",
            params={
                "text": query,
                "apiKey": GEOAPIFY_KEY,
                "filter": "countrycode:my",
                "bias": f"proximity:{lon},{lat}",
                "limit": 5,
                "lang": "en",
            },
            timeout=8,
        )
        if r.status_code == 200:
            features = r.json().get("features", [])
            return [
                {
                    "display": f["properties"].get("formatted", ""),
                    "lat": f["geometry"]["coordinates"][1],
                    "lon": f["geometry"]["coordinates"][0],
                    "city": f["properties"].get("city", ""),
                    "state": f["properties"].get("state", ""),
                }
                for f in features
                if f.get("geometry")
            ]
    except Exception as e:
        print(f"[Geoapify] Autocomplete failed: {e}")
    return []
