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


# Stop calling a backend that is already telling us no. An exhausted key or a
# rate limit fails on EVERY alert, and each failure costs a network round trip
# before the card can be written. During a live demo that turns into visible lag
# on every incident plus a console full of the same stack trace, so after this
# many consecutive failures the agent commits to templates for the rest of the run.
_MAX_CONSECUTIVE_FAILURES = 3
_failures = 0


def _gemini_text(user_prompt: str) -> str | None:
    global _failures
    if _gemini is None or _failures >= _MAX_CONSECUTIVE_FAILURES:
        return None
    try:
        resp = _gemini.generate_content(user_prompt)
        _failures = 0
        return (resp.text or "").strip()
    except Exception as exc:  # noqa: BLE001
        _failures += 1
        first_line = str(exc).splitlines()[0][:160]
        if _failures >= _MAX_CONSECUTIVE_FAILURES:
            print(f"[agent] Gemini failed {_failures}x ({first_line}); "
                  "using template cards for the rest of this run")
        else:
            print(f"[agent] Gemini call failed ({first_line}); falling back to template")
        return None


# ── deterministic fallbacks (also a good baseline / offline demo) ───────
def _template_anomaly(a: dict) -> tuple[str, str, str]:
    ctx = a.get("context", {})
    name = ctx.get("station_name", a["station_id"])
    pm = a.get("pm25")
    wind = ctx.get("wind_speed")
    rising = ctx.get("direction") != "down"

    # Only claim a cause the data supports. Blaming low wind for a FALL in PM2.5
    # reads as nonsense to anyone who knows the domain, which on a portfolio piece
    # is exactly the person you least want to lose.
    if rising:
        cause = ("weak wind limiting dispersion" if (wind is not None and wind < 1.5)
                 else "elevated local emissions (likely traffic)")
    else:
        cause = ("improving dispersion as wind picks up" if (wind is not None and wind >= 1.5)
                 else "the source easing or rainfall scavenging particulates")

    verb = "spike" if rising else "drop"
    title = f"PM2.5 {verb} at {name}"

    prev, sigmas = ctx.get("pm25_prev"), ctx.get("sigmas")
    movement = (f"PM2.5 moved from {prev} to {pm} µg/m³ at {name}"
                if prev is not None else f"PM2.5 reached {pm} µg/m³ at {name}")
    rarity = f" That step is about {sigmas} sigmas past this station's normal." if sigmas else ""

    body = (
        f"{movement} (AQI {ctx.get('aqi_now')}, {ctx.get('category')}).{rarity} "
        f"Likely driven by {cause}. Forecast for the next {ctx.get('horizon_min')} min: "
        f"~{ctx.get('forecast')} µg/m³ ({ctx.get('category_forecast')})."
    )
    return title, body, f"Forecast {ctx.get('horizon_min')}min: {ctx.get('category_forecast')}"


def _template_drift(a: dict) -> tuple[str, str, str]:
    ctx = a.get("context", {})
    ver = ctx.get("new_version", "?")
    body = (
        f"Feature distribution drifted (share {a.get('score')}); the online model "
        f"auto-retrained and promoted {ver}. No action needed: monitoring continues "
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
            horizon_min=ctx.get("horizon_min"),
            direction=ctx.get("direction", "up"), pm25_prev=ctx.get("pm25_prev"),
            delta=ctx.get("delta"), sigmas=ctx.get("sigmas")))

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
