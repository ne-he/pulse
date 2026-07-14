"""Incident agent — consumes alerts, emits human-readable incident cards.

Gemini path: structured prompt → narrative. Template path (no key / API error):
deterministic, grounded card. Either way the dashboard's incident feed stays alive,
which is the whole point of the 'agentic + streaming is real' signal."""
from __future__ import annotations

import uuid

from agent import prompts
from common.config import Streams, settings
from common.redis_bus import StreamReader, get_client, publish
from common.schemas import Incident

# Optional Gemini client — only used when a key is configured.
_gemini = None
if settings.gemini_api_key:
    try:
        import google.generativeai as genai

        genai.configure(api_key=settings.gemini_api_key)
        _gemini = genai.GenerativeModel(settings.gemini_model, system_instruction=prompts.SYSTEM)
        print(f"[agent] Gemini enabled ({settings.gemini_model})")
    except Exception as exc:  # noqa: BLE001
        print(f"[agent] Gemini init failed ({exc}); using template fallback")
        _gemini = None
else:
    print("[agent] no GEMINI_API_KEY — using deterministic template cards")


def _gemini_text(user_prompt: str) -> str | None:
    if _gemini is None:
        return None
    try:
        resp = _gemini.generate_content(user_prompt)
        return (resp.text or "").strip()
    except Exception as exc:  # noqa: BLE001
        print(f"[agent] Gemini call failed ({exc}); falling back to template")
        return None


# ── deterministic fallbacks (also a good baseline / offline demo) ───────
def _template_anomaly(a: dict) -> tuple[str, str, str]:
    ctx = a.get("context", {})
    name = ctx.get("station_name", a["station_id"])
    pm = a.get("pm25")
    wind = ctx.get("wind_speed")
    cause = "weak wind limiting dispersion" if (wind is not None and wind < 1.5) else "elevated local emissions (likely traffic)"
    title = f"PM2.5 spike at {name}"
    body = (
        f"PM2.5 jumped to {pm} µg/m³ at {name} (AQI {ctx.get('aqi_now')}, "
        f"{ctx.get('category')}), an anomalous reading. Likely driven by {cause}. "
        f"Forecast for the next {ctx.get('horizon_min')} min: ~{ctx.get('forecast')} µg/m³ "
        f"({ctx.get('category_forecast')})."
    )
    return title, body, f"Forecast {ctx.get('horizon_min')}min: {ctx.get('category_forecast')}"


def _template_drift(a: dict) -> tuple[str, str, str]:
    ctx = a.get("context", {})
    ver = ctx.get("new_version", "?")
    body = (
        f"Feature distribution drifted (share {a.get('score')}); the online model "
        f"auto-retrained and promoted {ver}. No action needed — monitoring continues "
        f"against the new baseline."
    )
    return f"Model retrained → {ver}", body, None


def build_incident(alert: dict) -> Incident:
    if alert.get("type") == "drift":
        title, body, summary = _template_drift(alert)
        text = _gemini_text(prompts.DRIFT_USER.format(
            score=alert.get("score"), new_version=alert.get("context", {}).get("new_version")))
    else:
        title, body, summary = _template_anomaly(alert)
        ctx = alert.get("context", {})
        text = _gemini_text(prompts.ANOMALY_USER.format(
            station_name=ctx.get("station_name", alert["station_id"]),
            station_id=alert["station_id"], pm25=alert.get("pm25"),
            aqi_now=ctx.get("aqi_now"), category=ctx.get("category"),
            score=alert.get("score"), wind_speed=ctx.get("wind_speed"),
            forecast=ctx.get("forecast"), category_forecast=ctx.get("category_forecast"),
            horizon_min=ctx.get("horizon_min")))

    return Incident(
        incident_id=str(uuid.uuid4())[:8], alert_id=alert.get("alert_id", "?"),
        station_id=alert["station_id"], ts=alert.get("ts"),
        title=title, body=text or body, severity=alert.get("severity", "info"),
        forecast_summary=summary, generated_by="gemini" if text else "template",
    )


def run() -> None:
    client = get_client()
    reader = StreamReader(client, [Streams.ALERTS])
    print("[agent] listening for alerts…")
    while True:
        for _stream, _id, alert in reader.read(block_ms=5000):
            try:
                incident = build_incident(alert)
                publish(client, Streams.INCIDENTS, incident)
                print(f"[agent] 📝 {incident.generated_by}: {incident.title}")
            except Exception as exc:  # noqa: BLE001
                print(f"[agent] error: {exc}")


if __name__ == "__main__":
    run()
