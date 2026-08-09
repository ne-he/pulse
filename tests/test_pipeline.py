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
from tests.conftest import calm_events, polluted_events


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


def test_anomaly_detector_flags_spike_on_constant_signal_without_lag():
    """Same spike on a perfectly flat signal, caught on the event itself.

    This used to fire two events late. The detector scored a HalfSpaceTrees
    pipeline whose MinMaxScaler returns 0 while min == max, so on a dead-flat
    warmup the spike was scaled to 0 and was invisible at the moment it arrived.
    Scoring the one-step residual instead has no such blind spot: a flat history
    gives a residual spread of 0, which the 0.5 ug/m3 floor turns into a very
    large z rather than a divide-by-zero.

    Pinned at exactly 100 so that any future detector that reintroduces a
    detection lag fails here instead of regressing silently.
    """
    det = AnomalyDetector("jaksel", threshold=0.7)
    fired_at = [i for i in range(120)
                if det.score({"pm25": 200.0 if i == 100 else 20.0, "wind_speed": 1.0})[1]]
    assert fired_at, "a 10x jump should be flagged"
    assert fired_at[0] == 100, (
        f"detection latency regressed: first flag at event {fired_at[0]}, expected 100"
    )


def test_anomaly_detector_is_quiet_on_a_calm_feed():
    """Specificity, the half the suite used to be missing.

    Every anomaly test here only ever asked "does it catch a spike". Nothing
    asked "does it stay quiet otherwise", so a detector that fired on 58.3% of
    the real 10,080 event sample sat in the repo with a green suite and a note
    in the README admitting it. Business-as-usual air must produce no incidents:
    at PM2.5 ~ N(20, 3) the AQI is already Moderate, so the materiality floor is
    not what keeps this quiet, the statistic is.
    """
    det = AnomalyDetector("jaksel")
    fired = sum(det.score(e)[1] for e in calm_events("jaksel", 500))
    assert fired == 0, f"{fired} false alarms on a calm feed, expected none"


def test_anomaly_detector_rate_stays_selective_across_regimes():
    """A feed that shifts regime should alarm on the TRANSITION, not throughout.

    Calm air followed by a pollution episode is the case the dashboard exists
    for. The detector should mark the jump and then settle, because a sustained
    high regime is drift's job (ml/monitoring/drift.py), not the anomaly
    detector's. The ceiling is deliberately loose: this pins "selective" as a
    property, it does not pretend to be a calibration against labelled events.
    """
    events = calm_events("jaksel", 250) + polluted_events("jaksel", 250, start=250)
    det = AnomalyDetector("jaksel")
    flags = [i for i, e in enumerate(events) if det.score(e)[1]]

    assert flags, "the calm -> polluted transition should raise at least one alert"
    assert flags[0] == 250, f"expected the alert on the regime change, got event {flags[0]}"
    assert len(flags) / len(events) < 0.05, (
        f"alert rate {len(flags) / len(events):.1%} is not selective "
        f"({len(flags)} of {len(events)} events)"
    )


def test_anomaly_detector_ignores_odd_but_clean_air():
    """A statistically odd reading in Good air is not an incident.

    Without this floor the incident feed narrates "PM2.5 spike" over readings of
    a few ug/m3, which is clean air. That is the difference between a list an ops
    team acts on and a list they mute.
    """
    det = AnomalyDetector("jaksel")
    for _ in range(60):                       # settle on a very clean baseline
        det.score({"pm25": 3.0, "wind_speed": 2.0})
    score, is_anom = det.score({"pm25": 9.0, "wind_speed": 2.0})   # AQI 38, still Good
    assert score > 0.85, "the jump is genuinely surprising in statistical terms"
    assert not is_anom, "but Good air must not be reported as an incident"


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
