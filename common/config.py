"""Central config — every service reads settings from here (12-factor: env-driven).

Defaults are chosen so the walking skeleton runs with ZERO setup: replay mode,
no API keys, local JSON registry. Override via .env for live data / Gemini."""
from __future__ import annotations

import sys

from pydantic_settings import BaseSettings, SettingsConfigDict

# Make emoji/Unicode logs safe on Windows consoles (cp1252) for local dev.
# Every service imports this module, so this runs everywhere once.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # ── Bus ────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── Ingestion ──────────────────────────────────────────
    ingest_mode: str = "replay"          # replay | live
    poll_interval_sec: int = 60
    replay_speed: float = 600.0          # sim seconds per real second (~1 frame/sec on 10-min data)
    openaq_api_key: str = ""
    openaq_base_url: str = "https://api.openaq.org/v3"
    weather_base_url: str = "https://api.open-meteo.com/v1/forecast"

    # ── Online model ───────────────────────────────────────
    model_kind: str = "river"            # river | baseline
    forecast_horizon: int = 6
    uncertainty_z: float = 1.96

    # ── Anomaly ────────────────────────────────────────────
    anomaly_threshold: float = 0.85

    # ── Drift / retrain ────────────────────────────────────
    drift_window: int = 200
    drift_threshold: float = 0.5

    # ── Agent ──────────────────────────────────────────────
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash"

    # ── Registry ───────────────────────────────────────────
    registry_path: str = "./data/registry"

    # ── API ────────────────────────────────────────────────
    api_port: int = 8000


# Single import-time instance. `from common.config import settings`.
settings = Settings()


# ── Redis Stream names — THE contract. Don't hardcode these elsewhere. ──
class Streams:
    EVENTS = "aq.events"            # raw observations from ingestion
    PREDICTIONS = "aq.predictions"  # forecasts + uncertainty + live metrics
    ALERTS = "aq.alerts"           # anomaly / drift flags (machine-readable)
    INCIDENTS = "aq.incidents"      # natural-language cards from the agent
    CONTROL = "aq.control"         # demo/replay control commands (play/spike/seek)
