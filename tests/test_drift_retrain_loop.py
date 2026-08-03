"""The closed loop: drift → retrain → new version → new model card.

README claims this loop closes automatically. Until now the suite tested the two
ends (`check_drift` on DataFrames, `Registry.register` on disk) and simply assumed
the bridge between them existed. `Engine._track_drift` and `Engine._retrain` had
zero coverage. These tests walk the bridge with a real `Engine`, driven by an
in-memory bus so no Redis is required.
"""
from __future__ import annotations

import pandas as pd
import pytest

from common.config import Streams, settings
from common.redis_bus import InMemoryClient
from ml.monitoring.drift import FEATURES, check_drift
from ml.online.consumer import Engine
from tests.conftest import calm_events, polluted_events

STATION = "jaksel"


def _rows(events: list[dict]) -> pd.DataFrame:
    return pd.DataFrame([{f: e.get(f) for f in FEATURES} for e in events])


# ── 1. detection ───────────────────────────────────────────────────────
def test_drift_detected_on_shifted_distribution():
    """Shift the PM2.5 regime on purpose; `check_drift` must say dataset_drift True."""
    reference = _rows(calm_events(STATION, 200, seed=1))
    current = _rows(polluted_events(STATION, 200, seed=2))

    report = check_drift(reference, current)

    assert report["per_feature"]["pm25"]["drifted"] is True, "the PM2.5 shift was missed"
    assert report["dataset_drift"] is True
    assert report["share_drifted"] >= settings.drift_threshold
    assert set(report["per_feature"]) == set(FEATURES)


def test_pm25_only_shift_flags_the_feature_but_not_the_dataset():
    """A documented sharp edge, pinned so it cannot change by accident.

    `dataset_drift` is `share_of_drifted_features >= drift_threshold`. With 4 features
    and a 0.5 threshold, PM2.5 moving ALONE gives share 0.25, so the dataset flag stays
    False even though PM2.5 clearly drifted. That is why the realistic regime shift above
    also moves wind and humidity. If someone later changes FEATURES or the threshold,
    this test tells them what they just changed.
    """
    calm = calm_events(STATION, 200, seed=1)
    spiked = [{**e, "pm25": e["pm25"] + 100.0} for e in calm]

    report = check_drift(_rows(calm), _rows(spiked))

    assert report["per_feature"]["pm25"]["drifted"] is True
    assert report["share_drifted"] == pytest.approx(0.25, abs=0.01)
    assert report["dataset_drift"] is False


def test_stable_stream_does_not_trigger_drift():
    """No false positive on two draws from the same regime."""
    report = check_drift(
        _rows(calm_events(STATION, 200, seed=1)),
        _rows(calm_events(STATION, 200, seed=2)),
    )
    assert report["dataset_drift"] is False


# ── 2. drift → retrain → new version ───────────────────────────────────
def _run_until_retrain(engine: Engine, client: InMemoryClient) -> None:
    """Feed a calm regime (builds the baseline), then a polluted one (drifts it)."""
    window = settings.drift_window
    for event in calm_events(STATION, window * 2, seed=1):
        engine.process(client, event)
    for event in polluted_events(STATION, window * 2, seed=2):
        engine.process(client, event)


def test_drift_triggers_retrain_and_new_version(isolated_registry):
    client = InMemoryClient()
    engine = Engine()

    bootstrap = engine.version
    assert bootstrap == "v1", "expected the bootstrap version before any drift"
    assert len(engine.registry.list_versions()) == 1

    # calm half only: baseline established, nothing should be promoted
    for event in calm_events(STATION, settings.drift_window * 2, seed=1):
        engine.process(client, event)
    assert engine.version == bootstrap, "a stable stream must not trigger a retrain"

    # now shift the regime
    for event in polluted_events(STATION, settings.drift_window * 2, seed=2):
        engine.process(client, event)

    versions = engine.registry.list_versions()
    assert len(versions) >= 2, "drift did not produce a new registered version"
    assert engine.version != bootstrap
    assert engine.registry.active()["version"] == engine.version

    newest = engine.registry.get(engine.version)
    assert "drift-triggered retrain" in newest["reason"]
    assert newest["promoted"] is True
    assert newest["metrics"]["n_seen"] > 0

    # the loop also announces itself on the bus, which is what the agent narrates
    drift_alerts = [a for a in client.messages(Streams.ALERTS) if a["type"] == "drift"]
    assert drift_alerts, "no drift alert was published"
    assert drift_alerts[-1]["context"]["new_version"] == engine.version
    assert drift_alerts[-1]["context"]["drifted_stations"] == [STATION]


def test_retrain_rebaselines_so_the_same_shift_does_not_refire(isolated_registry):
    """After promotion the drifted station is measured against its NEW regime."""
    client = InMemoryClient()
    engine = Engine()
    _run_until_retrain(engine, client)

    promoted = len(engine.registry.list_versions())
    assert promoted >= 2

    # keep feeding the SAME polluted regime: it is no longer a surprise
    for event in polluted_events(STATION, settings.drift_window * 2, seed=3):
        engine.process(client, event)

    assert len(engine.registry.list_versions()) == promoted, (
        "the same, already-adopted regime retriggered a retrain"
    )


# ── 3. the model card ──────────────────────────────────────────────────
def test_model_card_generated_correctly(isolated_registry):
    """After a retrain the card exists and its fields match the registry record."""
    client = InMemoryClient()
    engine = Engine()
    _run_until_retrain(engine, client)

    version = engine.version
    record = engine.registry.get(version)
    assert record is not None

    card = engine.registry.get_card(version)
    assert card, f"no model card was written for {version}"

    # version
    assert f"`{version}`" in card
    # metrics, exactly as recorded
    assert str(record["metrics"]["mae"]) in card
    assert str(record["metrics"]["rmse"]) in card
    assert str(record["metrics"]["n_seen"]) in card
    # retrain reason
    assert record["reason"] in card
    assert "drift-triggered retrain" in card
    # creation timestamp
    assert record["created_at"] in card
    # the drift evidence that justified the promotion
    assert "## Drift at promotion" in card
    assert "pm25" in card
    # governance sections that make this a model card and not a log line
    for heading in ("Intended use", "Training data", "Metrics", "Limitations", "Lifecycle"):
        assert heading in card

    # and the bootstrap card is still a valid card, without drift evidence
    first = engine.registry.get_card("v1")
    assert "Model Card" in first
    assert "## Drift at promotion" not in first


def test_model_card_records_which_station_drifted(isolated_registry):
    client = InMemoryClient()
    engine = Engine()
    _run_until_retrain(engine, client)

    card = engine.registry.get_card(engine.version)
    assert "### Drift per station" in card
    assert f"`{STATION}`" in card
    assert "Retrain was triggered by" in card
