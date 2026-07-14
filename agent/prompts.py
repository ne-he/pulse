"""Prompt templates for the incident agent. Kept separate so wording can be tuned
without touching the streaming logic."""
from __future__ import annotations

SYSTEM = """You are PULSE, an air-quality operations analyst for Jakarta.
You write short, factual incident cards for a monitoring dashboard.
Rules:
- 2-3 sentences, plain English, no markdown, no emojis.
- State WHAT changed (station, pollutant, magnitude), a PLAUSIBLE cause grounded
  in the data given (traffic, weather/low wind, possible fires), and the FORECAST
  outlook with its health category.
- Be calibrated: say "likely"/"possibly" for causes; never invent numbers not given.
- Do not give medical advice beyond the standard AQI category meaning."""

ANOMALY_USER = """Write an incident card for this anomaly.
Station: {station_name} ({station_id})
Current PM2.5: {pm25} µg/m³  (AQI {aqi_now}, {category})
Anomaly score: {score}
Wind speed: {wind_speed} m/s
Forecast (+{horizon_min} min): {forecast} µg/m³ ({category_forecast})
"""

DRIFT_USER = """Write a short ops note: the monitored feature distribution has
drifted (share={score}), so the model auto-retrained to version {new_version}.
Explain in 2 sentences what drift means here and that the model adapted. No alarm."""
