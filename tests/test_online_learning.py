"""Does `learn_one()` actually learn?

README's headline claim is "true online learning, not batch retraining in disguise".
The old suite could not tell the difference: `StationModel` silently degrades to a
last-value baseline the first time river raises (model.py `observe`), and the only
existing assertions (`model.n == 60`, `model.mae is not None`) stay green in that
degraded mode. So the project's central claim could be false with a green suite.

These tests pin the claim down: river is really in use, `learn_one` runs once per
event, and the model's internal state moves because of it.
"""
from __future__ import annotations

import pytest

from ml.online.model import StationModel, exog_features
from tests.conftest import CountingRiverModel, calm_events, numeric_state

river = pytest.importorskip("river", reason="river is the core dependency under test")


def test_online_learning_changes_state():
    """Snapshot river's internals before and after N events, prove they moved."""
    model = StationModel("jaksel")
    assert model.kind == "river", (
        "model fell back to the baseline, so nothing below tests online learning"
    )
    assert model.model is not None

    # count learn_one calls without changing production code
    model.model = CountingRiverModel(model.model)
    before = numeric_state(model.model._inner)
    assert before, "expected river to expose some numeric state to compare"

    events = calm_events("jaksel", 80)
    for event in events:
        exog = exog_features(event)
        model.forecast(exog)              # predict first, exactly like the consumer
        model.observe(float(event["pm25"]), exog)

    after = numeric_state(model.model._inner)

    # 1. learn_one ran once per event: per-event updates, not a batch pass
    assert model.model.learn_calls == len(events), (
        f"expected {len(events)} learn_one calls, got {model.model.learn_calls}"
    )
    # 2. it never silently degraded mid-stream
    assert model.kind == "river"
    # 3. the learned state is genuinely different
    assert after != before, "river state is identical after 80 events: nothing was learned"
    changed = sum(1 for a, b in zip(before, after, strict=False) if a != b)
    assert changed > 0, "no individual state value moved"


def test_online_learning_is_incremental_not_restarted():
    """State keeps evolving across the stream, and each event contributes."""
    model = StationModel("jakpus")
    snapshots = []
    events = calm_events("jakpus", 60, seed=7)

    for i, event in enumerate(events, start=1):
        exog = exog_features(event)
        model.observe(float(event["pm25"]), exog)
        if i in (10, 20, 40, 60):
            snapshots.append(numeric_state(model.model))

    assert model.kind == "river"
    # every checkpoint differs from the one before it: the model is still moving,
    # not converged-and-frozen after a single warm-up batch
    for earlier, later in zip(snapshots, snapshots[1:], strict=False):
        assert earlier != later, "state stopped changing partway through the stream"
    assert model.n == len(events)


def test_online_learning_shifts_the_forecast():
    """The functional consequence: a learned model predicts differently."""
    exog = exog_features({"ts": "2026-08-01T08:00:00+00:00", "temp": 29,
                          "humidity": 70, "wind_speed": 1.5})

    fresh = StationModel("jakbar")
    fresh_point, _, _ = fresh.forecast(exog)

    trained = StationModel("jakbar")
    for event in calm_events("jakbar", 100, seed=11):
        trained.observe(float(event["pm25"]), exog_features(event))
    trained_point, _, _ = trained.forecast(exog)

    assert trained.kind == "river"
    assert trained_point != fresh_point, (
        "an untrained and a 100-event-trained model produced the identical forecast"
    )


def test_baseline_fallback_is_visible_not_silent():
    """The fallback path still works, and it is honest about being the fallback.

    This is the trap the old suite fell into: `kind` is the only signal that river
    is gone. Pinning it means a future silent degradation shows up as a red test.
    """
    model = StationModel("jaktim", kind="baseline")
    for event in calm_events("jaktim", 30, seed=3):
        model.observe(float(event["pm25"]), exog_features(event))

    assert model.kind == "baseline"
    assert model.model is None
    assert model.n == 30
    assert model.mae is not None  # note: these two pass in BOTH modes, which is the point
