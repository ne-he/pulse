"""Offline end-to-end smoke test — no Redis, no Docker, no keys required.

Runs the pure-Python pipeline (forecast → learn → anomaly → drift → retrain card
→ agent narrative) on synthetic data and asserts each stage produces sane output.
Great as a fast 'is the brain wired up?' check and as a CI gate.

    python -m scripts.smoke
"""
from __future__ import annotations

import math
import tempfile

import pandas as pd

from agent.incident import build_incident
from ml.modelcard.generate import generate_card
from ml.monitoring.drift import check_drift
from ml.online.anomaly import AnomalyDetector
from ml.online.model import StationModel, exog_features
from ml.registry.registry import Registry


def _synthetic_events(n=400):
    events = []
    for i in range(n):
        hour = (i // 6) % 24
        base = 20 + 12 * math.sin(2 * math.pi * (hour - 7) / 24)
        pm = max(3.0, base + 4 * math.sin(i / 5))
        if 250 <= i < 256:                      # injected spike
            pm += 90
        events.append({
            "station_id": "jaksel", "station_name": "Jakarta Selatan",
            "ts": f"2026-06-22T{hour:02d}:00:00+00:00",
            "pm25": round(pm, 2), "temp": 29.0, "humidity": 72.0, "wind_speed": 1.2,
        })
    return events


def main() -> None:
    events = _synthetic_events()
    model = StationModel("jaksel")
    detector = AnomalyDetector("jaksel")
    preds, flagged = [], 0

    for ev in events:
        exog = exog_features(ev)
        point, lo, hi = model.forecast(exog)
        score, is_anom = detector.score(ev)
        model.observe(float(ev["pm25"]), exog)
        preds.append(point)
        flagged += int(is_anom)
        assert lo <= point <= hi, "uncertainty band must bracket the point forecast"
        assert point >= 0, "forecast must be non-negative"

    assert model.mae is not None and model.mae >= 0, "rolling MAE should exist"
    print(f"[smoke] model={model.kind} n={model.n} MAE={model.mae:.2f} anomalies_flagged={flagged}")
    assert flagged >= 1, "the injected spike should trip the anomaly detector"

    # drift: compare calm window vs spiked window
    ref = pd.DataFrame([{"pm25": e["pm25"], "temp": 29, "humidity": 72, "wind_speed": 1.2} for e in events[:150]])
    cur = pd.DataFrame([{"pm25": e["pm25"] + 40, "temp": 33, "humidity": 55, "wind_speed": 0.4} for e in events[150:300]])
    report = check_drift(ref, cur)
    print(f"[smoke] drift engine={report['engine']} share={report['share_drifted']} drift={report['dataset_drift']}")

    # registry + model card
    with tempfile.TemporaryDirectory() as tmp:
        reg = Registry(base_path=tmp)
        rec = reg.register(metrics={"mae": round(model.mae, 2), "rmse": round(model.rmse, 2), "n_seen": model.n},
                           kind=model.kind, reason="smoke test")
        card = generate_card(rec, report)
        reg.save_card(rec["version"], card)
        assert "Model Card" in card and rec["version"] in card
        assert reg.active()["version"] == rec["version"]
        print(f"[smoke] registry promoted {rec['version']}, model card {len(card)} chars")

    # agent (template path — no Gemini key needed)
    incident = build_incident({
        "alert_id": "test", "station_id": "jaksel", "type": "anomaly", "severity": "warning",
        "pm25": 130, "ts": events[-1]["ts"],
        "context": {"station_name": "Jakarta Selatan", "aqi_now": 180, "category": "Unhealthy",
                    "wind_speed": 0.5, "forecast": 95, "category_forecast": "Unhealthy", "horizon_min": 60},
    })
    print(f"[smoke] incident ({incident.generated_by}): {incident.title} — {incident.body[:80]}…")
    assert incident.body, "agent must produce a narrative"

    print("\n[smoke] ✅ ALL STAGES PASSED — the loop is wired up.")


if __name__ == "__main__":
    main()
