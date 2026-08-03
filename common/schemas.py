"""Pydantic event schemas — the typed contract that flows through the streams.

Every message on the bus is one of these, serialized to JSON. The dashboard's
TypeScript types should mirror these exactly (see Frontend_pulse/FRONTEND_SPEC.md)."""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, Field


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ── Jakarta stations (static metadata; real OpenAQ ids can be wired in live mode) ──
STATIONS: dict[str, dict] = {
    "jaksel": {"id": "jaksel", "name": "Jakarta Selatan", "lat": -6.2615, "lon": 106.8106},
    "jakpus": {"id": "jakpus", "name": "Jakarta Pusat", "lat": -6.1862, "lon": 106.8341},
    "jakbar": {"id": "jakbar", "name": "Jakarta Barat", "lat": -6.1683, "lon": 106.7588},
    "jaktim": {"id": "jaktim", "name": "Jakarta Timur", "lat": -6.2250, "lon": 106.9004},
    "jakut":  {"id": "jakut",  "name": "Jakarta Utara", "lat": -6.1214, "lon": 106.7741},
}


class AQEvent(BaseModel):
    """One raw observation from a station at a point in time."""
    station_id: str
    station_name: str
    ts: str = Field(default_factory=_now_iso)        # ISO-8601 UTC
    pm25: float                                       # µg/m³ — our forecast target
    pm10: float | None = None
    no2: float | None = None
    o3: float | None = None
    # weather exogenous features
    temp: float | None = None                      # °C
    humidity: float | None = None                  # %
    wind_speed: float | None = None                # m/s
    source: Literal["live", "replay"] = "replay"


class Prediction(BaseModel):
    """Model output for one station: forecast + uncertainty + live health metrics."""
    station_id: str
    ts: str = Field(default_factory=_now_iso)
    pm25_now: float                                   # latest observed pm2.5
    aqi_now: int                                      # derived US AQI
    horizon_min: int                                  # forecast lead time (minutes)
    pm25_forecast: float
    aqi_forecast: int
    forecast_lower: float                             # uncertainty band (pm2.5)
    forecast_upper: float
    category_now: str                                 # Good / Moderate / Unhealthy ...
    category_forecast: str
    trend: Literal["up", "down", "flat"]
    model_version: str
    mae_rolling: float | None = None               # live error (drives "learning…")
    rmse_rolling: float | None = None
    n_seen: int = 0                                    # how many events learned so far


class Alert(BaseModel):
    """Machine-readable flag. The agent turns these into human incident cards."""
    alert_id: str
    station_id: str
    ts: str = Field(default_factory=_now_iso)
    type: Literal["anomaly", "drift"]
    severity: Literal["info", "warning", "critical"]
    score: float                                      # anomaly score or drift share
    pm25: float | None = None
    context: dict = Field(default_factory=dict)       # extra signals for the LLM


class Incident(BaseModel):
    """Natural-language card written by the Gemini agent from an Alert."""
    incident_id: str
    alert_id: str
    station_id: str
    ts: str = Field(default_factory=_now_iso)
    title: str
    body: str                                         # the narrative
    severity: Literal["info", "warning", "critical"]
    forecast_summary: str | None = None
    generated_by: Literal["gemini", "template"] = "template"


class ControlCommand(BaseModel):
    """Demo-mode control from the dashboard (the secret weapon)."""
    action: Literal["play", "pause", "seek", "trigger_spike", "set_speed"]
    station_id: str | None = None
    value: float | None = None                     # seek timestamp / speed / spike magnitude
