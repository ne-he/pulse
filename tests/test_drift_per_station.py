"""Drift is per station, not pooled across Jakarta.

Verified problem (12 Jul, re-verified 1 Aug): `ml/monitoring/drift.py` did not mention
`station_id` at all, and `Engine` kept one shared window for every station. Jakarta's
five monitoring stations have structurally different PM2.5 baselines, so pooling them
either invents drift (when the station mix in the window shifts) or dilutes a real
local event into the crowd.

These tests pin down both halves: the detector localises drift, and the closed loop
re-baselines only the station that actually moved.
"""
from __future__ import annotations

import pandas as pd

from common.config import Streams, settings
from common.redis_bus import InMemoryClient
from ml.monitoring.drift import FEATURES, check_drift, check_drift_by_station
from ml.online.consumer import Engine
from tests.conftest import calm_events, interleave, polluted_events

# Same structural spread the sample generator uses: station_base = 18 + 6 * index
STATION_BASE = {"jaksel": 18.0, "jakpus": 24.0, "jakbar": 30.0, "jaktim": 36.0, "jakut": 42.0}
HOT_STATION = "jakut"


def _rows(events: list[dict], base: float | None = None) -> pd.DataFrame:
    df = pd.DataFrame([{f: e.get(f) for f in FEATURES} for e in events])
    if base is not None:
        df["pm25"] = df["pm25"] - 20.0 + base  # shift to this station's own baseline
    return df


def _calm_city(seed_offset: int, n: int) -> dict[str, pd.DataFrame]:
    return {
        sid: _rows(calm_events(sid, n, seed=seed_offset + i), base)
        for i, (sid, base) in enumerate(STATION_BASE.items())
    }


# ── 1. localisation: pooling dilutes, per-station resolves ─────────────
def test_pooling_dilutes_a_local_event_that_per_station_resolves():
    """One station's regime collapses. Pooled PSI loses the PM2.5 signal; per-station keeps it.

    The numbers below are the real point. When one of five stations jumps from roughly
    42 to roughly 125 µg/m³, the POOLED PM2.5 PSI comes out at about 0.19, which is
    under the 0.2 rule-of-thumb, so the pooled report does not even flag PM2.5 as
    drifted. It only trips the overall flag on weather columns, marginally, and it
    cannot say which station is on fire. Per-station scores that same event at 1.0 for
    the affected station and 0.0 for the other four.
    """
    window = settings.drift_window
    reference = _calm_city(10, window)
    current = _calm_city(90, window)
    current[HOT_STATION] = _rows(polluted_events(HOT_STATION, window, seed=555))

    pooled = check_drift(
        pd.concat(reference.values(), ignore_index=True),
        pd.concat(current.values(), ignore_index=True),
    )
    per_station = check_drift_by_station(reference, current)

    # pooled: the pollutant that actually moved is diluted below the threshold
    assert pooled["per_feature"]["pm25"]["drifted"] is False, (
        "pooled PM2.5 unexpectedly survived dilution; the premise of this test changed"
    )
    assert "per_station" not in pooled  # pooled cannot localise at all

    # per-station: exact localisation
    assert per_station["dataset_drift"] is True
    assert per_station["drifted_stations"] == [HOT_STATION]
    assert per_station["worst_station"] == HOT_STATION
    assert per_station["per_station"][HOT_STATION]["per_feature"]["pm25"]["drifted"] is True
    assert per_station["n_stations"] == len(STATION_BASE)
    assert per_station["n_stations_drifted"] == 1
    assert per_station["share_stations_drifted"] == 0.2
    for sid in STATION_BASE:
        if sid != HOT_STATION:
            assert per_station["per_station"][sid]["dataset_drift"] is False


def test_per_station_report_keeps_the_legacy_contract():
    """The aggregate report is still shaped like a `check_drift` report.

    The API, the dashboard, and the model card all read `share_drifted`, `n_drifted`,
    `per_feature`, `dataset_drift`, and `engine`. Per-station drift must not break them.
    """
    window = settings.drift_window
    reference = _calm_city(10, window)
    current = _calm_city(90, window)
    current[HOT_STATION] = _rows(polluted_events(HOT_STATION, window, seed=555))

    report = check_drift_by_station(reference, current)
    for key in ("engine", "share_drifted", "n_drifted", "per_feature", "dataset_drift"):
        assert key in report, f"legacy consumers read `{key}`"
    assert set(report["per_feature"]) == set(FEATURES)
    assert report["scope"] == "per_station"


def test_calm_city_produces_no_drift_anywhere():
    window = settings.drift_window
    report = check_drift_by_station(_calm_city(10, window), _calm_city(90, window))
    assert report["dataset_drift"] is False
    assert report["drifted_stations"] == []
    assert report["n_stations"] == len(STATION_BASE)


def test_stations_with_too_little_data_are_skipped():
    """A station that just came online must not be judged on 3 rows."""
    window = settings.drift_window
    reference = _calm_city(10, window)
    current = _calm_city(90, window)
    reference["newcomer"] = _rows(calm_events("newcomer", 3, seed=1))
    current["newcomer"] = _rows(polluted_events("newcomer", 3, seed=2))

    report = check_drift_by_station(reference, current)
    assert "newcomer" not in report["per_station"]
    assert report["dataset_drift"] is False


# ── 2. the closed loop, one station at a time ──────────────────────────
def test_drift_in_one_station_triggers_retrain_and_new_card(isolated_registry):
    """Inject drift at ONE station: a new version registers and a new card is published."""
    window = settings.drift_window
    client = InMemoryClient()
    engine = Engine()
    bootstrap = engine.version

    # phase 1: the whole city is calm; both windows fill for both stations
    for event in interleave(
        calm_events("jaksel", window * 2, seed=1),
        calm_events("jakpus", window * 2, seed=2),
    ):
        engine.process(client, event)
    assert engine.version == bootstrap, "a calm city must not trigger a retrain"

    # phase 2: jaksel's regime collapses, jakpus carries on exactly as before
    for event in interleave(
        polluted_events("jaksel", window * 2, seed=3),
        calm_events("jakpus", window * 2, seed=4),
    ):
        engine.process(client, event)

    # a NEW version is registered
    assert engine.version != bootstrap, "local drift did not close the loop"
    assert engine.registry.active()["version"] == engine.version

    # a NEW card is published, and it names the station that caused it
    card = engine.registry.get_card(engine.version)
    assert card, "no model card published for the drift-triggered version"
    assert "### Drift per station" in card
    assert "Retrain was triggered by" in card
    assert "`jaksel`" in card

    # the blame is correct: jaksel, never jakpus
    drift_alerts = [a for a in client.messages(Streams.ALERTS) if a["type"] == "drift"]
    assert drift_alerts
    for alert in drift_alerts:
        assert alert["context"]["drifted_stations"] == ["jaksel"], (
            "a calm station was blamed for another station's drift"
        )
    assert "jaksel" in engine.registry.get(engine.version)["reason"]


def test_retrain_rebaselines_only_the_drifted_station(isolated_registry):
    """Point 4 of the brief: reset the reference window ONLY for the new regime."""
    window = settings.drift_window
    client = InMemoryClient()
    engine = Engine()

    for event in interleave(
        calm_events("jaksel", window * 2, seed=1),
        calm_events("jakpus", window * 2, seed=2),
    ):
        engine.process(client, event)
    for event in interleave(
        polluted_events("jaksel", window * 2, seed=3),
        calm_events("jakpus", window * 2, seed=4),
    ):
        engine.process(client, event)

    assert engine.version != "v1", "expected a retrain to have happened"

    # windows are kept per station, not pooled
    assert set(engine.reference_rows) == {"jaksel", "jakpus"}

    jaksel_ref = pd.DataFrame(engine.reference_rows["jaksel"])["pm25"].mean()
    jakpus_ref = pd.DataFrame(engine.reference_rows["jakpus"])["pm25"].mean()

    # jaksel was re-baselined onto the polluted regime it just adopted
    assert jaksel_ref > 80, f"jaksel reference was not reset to the new regime (mean {jaksel_ref:.1f})"
    # jakpus never drifted, so its evidence was left alone
    assert jakpus_ref < 40, f"jakpus reference was reset without drifting (mean {jakpus_ref:.1f})"


def test_one_stations_drift_does_not_corrupt_another_stations_window(isolated_registry):
    """`_track_drift` may only touch the window of the event's own station."""
    client = InMemoryClient()
    engine = Engine()

    for event in calm_events("jaksel", 50, seed=1):
        engine.process(client, event)
    assert list(engine.reference_rows) == ["jaksel"]
    assert len(engine.reference_rows["jaksel"]) == 50

    for event in polluted_events("jakpus", 20, seed=2):
        engine.process(client, event)

    assert len(engine.reference_rows["jaksel"]) == 50, "another station's events leaked in"
    assert len(engine.reference_rows["jakpus"]) == 20
    jaksel_mean = pd.DataFrame(engine.reference_rows["jaksel"])["pm25"].mean()
    assert jaksel_mean < 40, "jakpus pollution contaminated jaksel's reference window"


def test_malformed_event_without_station_id_does_not_kill_the_loop(isolated_registry):
    """Never-break philosophy: a bad event is skipped, not fatal."""
    client = InMemoryClient()
    engine = Engine()
    for event in calm_events("jaksel", 10, seed=1):
        engine.process(client, event)

    engine._track_drift(client, {"pm25": 20.0, "temp": 29.0})  # no station_id

    assert len(engine.reference_rows["jaksel"]) == 10
    assert "" not in engine.reference_rows
    assert None not in engine.reference_rows


def test_drift_alert_still_publishes_to_the_bus(isolated_registry):
    """The agent narrates drift from the alert stream; that contract must hold."""
    window = settings.drift_window
    client = InMemoryClient()
    engine = Engine()
    for event in calm_events("jaksel", window * 2, seed=1):
        engine.process(client, event)
    for event in polluted_events("jaksel", window * 2, seed=2):
        engine.process(client, event)

    alerts = [a for a in client.messages(Streams.ALERTS) if a["type"] == "drift"]
    assert alerts
    alert = alerts[-1]
    assert alert["station_id"] == "ALL"
    assert alert["severity"] == "warning"
    assert alert["context"]["drift"]["scope"] == "per_station"
    assert alert["context"]["metrics"]["n_seen"] > 0
    # predictions kept flowing the whole time
    assert len(client.messages(Streams.PREDICTIONS)) == window * 4
