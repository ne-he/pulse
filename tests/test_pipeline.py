"""Unit tests for the pure-Python pieces of the loop (no Redis needed)."""
from __future__ import annotations

import random
import tempfile

import pandas as pd

from common.health import aqi_category, pm25_to_aqi
from ml.modelcard.generate import generate_card
from ml.monitoring.drift import check_drift
from ml.online.anomaly import AnomalyDetector
from ml.online.model import StationModel, exog_features
from ml.registry.registry import Registry


def test_aqi_conversion_monotonic_and_categories():
    assert pm25_to_aqi(0) == 0
    assert pm25_to_aqi(10) <= 50
    assert pm25_to_aqi(40) > 100
    assert aqi_category(30)[0] == "Good"
    assert aqi_category(160)[0] == "Unhealthy"
    # higher pm => higher (or equal) aqi
    assert pm25_to_aqi(80) >= pm25_to_aqi(20)


def test_station_model_learns_and_bands_bracket_point():
    model = StationModel("jaksel")
    for i in range(60):
        ev = {"pm25": 20 + (i % 5), "ts": "2026-06-22T08:00:00+00:00",
              "temp": 29, "humidity": 70, "wind_speed": 1.0}
        exog = exog_features(ev)
        point, lo, hi = model.forecast(exog)
        assert lo <= point <= hi
        model.observe(ev["pm25"], exog)
    assert model.n == 60
    assert model.mae is not None


def test_anomaly_detector_flags_spike_on_realistic_signal():
    """A 10x spike must be flagged on the very event it happens.

    The input carries small noise on purpose. That is what a real feed looks
    like, and it is the case the detector is deployed against.
    """
    rng = random.Random(0)
    det = AnomalyDetector("jaksel", threshold=0.7)
    flagged_at_spike = False
    for i in range(120):
        pm = 200.0 if i == 100 else 20.0 + rng.gauss(0, 2)
        _score, is_anom = det.score({"pm25": pm, "wind_speed": 1.0})
        if i == 100:
            flagged_at_spike = is_anom
    assert flagged_at_spike, "a 10x jump should be flagged on the event itself"


def test_anomaly_detector_flags_spike_on_constant_signal_but_late():
    """Same spike on a perfectly flat signal is caught, but two events late.

    This is not a nit. `score_one` runs BEFORE `learn_one`, and river's
    MinMaxScaler returns 0 while min == max. On a dead-flat warmup the scaler
    has seen one distinct value, so the spike is scaled to 0 and is invisible
    to HalfSpaceTrees at the moment it arrives. The scaler only learns the new
    range afterwards, so the alarm lands on the events that follow.

    Measured: event 100 scores 0.0000, event 102 scores 0.9155.

    A dead-flat sensor is not a real air-quality feed, so this lag is accepted
    rather than fixed. It is pinned here so that if someone reorders score/learn
    or swaps the scaler, the change in behaviour shows up as a failing test
    instead of a silent regression in detection latency.
    """
    det = AnomalyDetector("jaksel", threshold=0.7)
    fired_at = [i for i in range(120)
                if det.score({"pm25": 200.0 if i == 100 else 20.0, "wind_speed": 1.0})[1]]
    assert fired_at, "a 10x jump should be flagged eventually"
    assert 100 <= fired_at[0] <= 105, (
        f"detection latency regressed: first flag at event {fired_at[0]}, expected within 100-105"
    )


def test_drift_detects_shift():
    import numpy as np
    rng = np.random.default_rng(0)
    ref = pd.DataFrame({"pm25": rng.normal(20, 3, 200), "temp": rng.normal(29, 1, 200),
                        "humidity": rng.normal(70, 5, 200), "wind_speed": rng.normal(1.5, 0.4, 200)})
    cur = pd.DataFrame({"pm25": rng.normal(120, 5, 200), "temp": rng.normal(35, 1, 200),
                        "humidity": rng.normal(45, 5, 200), "wind_speed": rng.normal(0.3, 0.1, 200)})
    report = check_drift(ref, cur)
    assert report["share_drifted"] > 0.5
    assert report["dataset_drift"] is True
    assert "per_feature" in report


def test_drift_stable_no_false_positive():
    import numpy as np
    rng = np.random.default_rng(1)
    cols = {c: rng.normal(20, 3, 200) for c in ("pm25", "temp", "humidity", "wind_speed")}
    ref, cur = pd.DataFrame(cols), pd.DataFrame({c: rng.normal(20, 3, 200) for c in cols})
    assert check_drift(ref, cur)["dataset_drift"] is False


def test_registry_and_model_card_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        reg = Registry(base_path=tmp)
        rec = reg.register(metrics={"mae": 3.1, "rmse": 4.2, "n_seen": 100}, kind="river", reason="test")
        assert rec["version"] == "v1"
        assert reg.active()["version"] == "v1"
        card = generate_card(rec)
        reg.save_card("v1", card)
        assert "Model Card" in reg.get_card("v1")
        rec2 = reg.register(metrics={"mae": 2.9}, kind="river", reason="retrain")
        assert rec2["version"] == "v2"
        assert len(reg.list_versions()) == 2
