"""Live ingestion — poll real Jakarta air quality + weather, push to the bus.

Primary source: OpenAQ v3 (needs a free API key). Fallback: Open-Meteo's keyless
air-quality API, so `live` mode still works out of the box for a demo. Weather
(temp/humidity/wind) always comes from keyless Open-Meteo as exogenous features.
"""
from __future__ import annotations

import time

import httpx

from common.config import Streams, settings
from common.redis_bus import get_client, publish
from common.schemas import STATIONS, AQEvent

OPEN_METEO_AQ = "https://air-quality-api.open-meteo.com/v1/air-quality"


def _weather(lat: float, lon: float) -> dict:
    try:
        r = httpx.get(settings.weather_base_url, params={
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,relative_humidity_2m,wind_speed_10m",
        }, timeout=10)
        cur = r.json().get("current", {})
        return {
            "temp": cur.get("temperature_2m"),
            "humidity": cur.get("relative_humidity_2m"),
            "wind_speed": cur.get("wind_speed_10m"),
        }
    except Exception as exc:  # noqa: BLE001 — degrade gracefully, never crash the stream
        print(f"[producer] weather fetch failed: {exc}")
        return {"temp": None, "humidity": None, "wind_speed": None}


def _air_quality_openmeteo(lat: float, lon: float) -> dict:
    r = httpx.get(OPEN_METEO_AQ, params={
        "latitude": lat, "longitude": lon,
        "current": "pm2_5,pm10,nitrogen_dioxide,ozone",
    }, timeout=10)
    cur = r.json().get("current", {})
    return {
        "pm25": cur.get("pm2_5"),
        "pm10": cur.get("pm10"),
        "no2": cur.get("nitrogen_dioxide"),
        "o3": cur.get("ozone"),
    }


def _air_quality_openaq(lat: float, lon: float) -> dict | None:
    """Best-effort OpenAQ v3 latest near a coordinate. Returns None on any failure
    so we can fall back to Open-Meteo. Wire real location ids here for production."""
    if not settings.openaq_api_key:
        return None
    try:
        r = httpx.get(f"{settings.openaq_base_url}/locations", params={
            "coordinates": f"{lat},{lon}", "radius": 12000, "limit": 1,
        }, headers={"X-API-Key": settings.openaq_api_key}, timeout=10)
        results = r.json().get("results", [])
        if not results:
            return None
        sensors = {s["parameter"]["name"]: s.get("latest", {}).get("value")
                   for s in results[0].get("sensors", []) if s.get("parameter")}
        return {
            "pm25": sensors.get("pm25"), "pm10": sensors.get("pm10"),
            "no2": sensors.get("no2"), "o3": sensors.get("o3"),
        }
    except Exception as exc:  # noqa: BLE001
        print(f"[producer] OpenAQ failed ({exc}); falling back to Open-Meteo")
        return None


def poll_once(client) -> int:
    sent = 0
    for sid, meta in STATIONS.items():
        aq = _air_quality_openaq(meta["lat"], meta["lon"]) or _air_quality_openmeteo(meta["lat"], meta["lon"])
        if aq.get("pm25") is None:
            continue
        wx = _weather(meta["lat"], meta["lon"])
        ev = AQEvent(station_id=sid, station_name=meta["name"], source="live", **aq, **wx)
        publish(client, Streams.EVENTS, ev)
        sent += 1
    return sent


def run() -> None:
    client = get_client()
    print(f"[producer] live mode, polling every {settings.poll_interval_sec}s")
    while True:
        try:
            n = poll_once(client)
            print(f"[producer] pushed {n} events")
        except Exception as exc:  # noqa: BLE001
            print(f"[producer] poll error: {exc}")
        time.sleep(settings.poll_interval_sec)


if __name__ == "__main__":
    run()
