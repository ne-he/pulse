"""Auto model card — every promotion publishes a governance document.

Model cards (Mitchell et al., 2019) are the rigor signal: intended use, data,
metrics, ethical considerations, limitations. Generated automatically so the
registry and the card never drift apart."""
from __future__ import annotations

from datetime import UTC, datetime


def generate_card(record: dict, drift: dict | None = None) -> str:
    """Render a markdown model card from a registry record (+ optional drift report)."""
    m = record.get("metrics", {})
    version = record.get("version", "v?")
    created = record.get("created_at", datetime.now(UTC).isoformat())
    reason = record.get("reason", "initial training")
    kind = record.get("kind", "river")

    mae = m.get("mae")
    rmse = m.get("rmse")
    n_seen = m.get("n_seen", "—")

    drift_section = ""
    if drift:
        rows = "\n".join(
            f"| {feat} | {info['score']} | {'⚠️ yes' if info['drifted'] else 'no'} |"
            for feat, info in drift.get("per_feature", {}).items()
        )
        drift_section = f"""
## Drift at promotion
Engine: `{drift.get('engine')}` · share drifted: **{drift.get('share_drifted')}** · dataset drift: **{drift.get('dataset_drift')}**

| Feature | Score | Drifted |
|---|---|---|
{rows}
"""

    return f"""# Model Card — PULSE Air-Quality Forecaster `{version}`

**Created:** {created}
**Promotion reason:** {reason}
**Model type:** `{kind}` (online / incremental)

## Intended use
Short-horizon PM2.5 forecasting for Jakarta monitoring stations, updated per-event
via online learning. Forecasts include an uncertainty band. For situational
awareness and operational alerting — **not** regulatory reporting or health
diagnosis.

## Training data
- Source: OpenAQ (Jakarta stations) + Open-Meteo weather, streamed live; or
  synthetic replay for demos.
- Features: lagged PM2.5 (SNARIMAX), weather exogenous (temp, humidity, wind),
  cyclical time-of-day.
- Learning: `learn_one()` on every incoming observation (true online learning).

## Metrics (rolling, live)
| Metric | Value |
|---|---|
| MAE | {mae if mae is not None else '—'} |
| RMSE | {rmse if rmse is not None else '—'} |
| Observations seen | {n_seen} |

> Metrics are computed online from 1-step-ahead residuals over a rolling window,
> so they reflect *current* production performance, not a frozen test set.
{drift_section}
## Limitations & ethical considerations
- Future exogenous weather is approximated by the latest reading; horizons beyond
  a few steps degrade.
- Sensor outages / API gaps can bias the online update; anomaly flags help surface this.
- AQI conversion uses the US EPA PM2.5 scale; local standards may differ.
- Not a substitute for official BMKG / KLHK air-quality guidance.

## Lifecycle
This card is auto-generated on every model promotion. Drift in the monitored
feature distribution triggers retraining, a new version, and a fresh card —
keeping documentation in lockstep with the deployed model.
"""
