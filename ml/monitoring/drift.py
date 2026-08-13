"""Drift monitoring — closes the loop. When the live feature distribution drifts
away from the reference window, this fires the signal that triggers retraining.

Two engines are implemented and `DRIFT_ENGINE` picks one explicitly. The default
is the PSI (Population Stability Index) implementation in this file; Evidently is
available with `DRIFT_ENGINE=evidently`. Both return the same shape, and `engine`
in the report says which one produced the numbers.

Why the in-house one is the default, since Evidently is the industry-standard
tool and shipping it would read better on paper:

1. Left to itself Evidently picks a stat test per column from the sample size,
   and at our window it picks two-sample K-S, whose `drift_score` is a p-value.
   That inverts the meaning of the number every consumer reads: under PSI a HIGH
   score means drift, under a p-value a LOW one does. It also flagged all four
   pooled features and a station that had not moved, because at n=1000 K-S
   rejects on differences too small to act on. So the Evidently path below pins
   `stattest="psi"` at the same threshold instead of accepting the default.
2. Even pinned to PSI the two disagree, because Evidently bins differently while
   `_psi` cuts on reference QUANTILES. Measured on six cases from the sample
   generator (five calm-vs-calm windows that must not drift, one calm-vs-polluted
   that must), quantile PSI got 6/6 right with every calm score at most 0.190,
   just under the 0.2 line. Evidently's PSI got 5/6, flagging calm `jaksel` at
   humidity 0.496 and wind_speed 0.236. At drift_window=200 the quantile version
   simply has the tighter null distribution, and a false retrain costs a bogus
   model version plus a re-baselined reference window.

Evidently stays wired up and tested (`tests/test_drift_engines.py`) so the claim
is checked rather than asserted, and so the choice can be revisited with data."""
from __future__ import annotations

import math

import pandas as pd

from common.config import settings

FEATURES = ["pm25", "temp", "humidity", "wind_speed"]

# Standard PSI rule of thumb. Shared by both engines so they cannot drift apart.
PSI_DRIFT_THRESHOLD = 0.2


def _psi(ref: pd.Series, cur: pd.Series, bins: int = 10) -> float:
    """Population Stability Index between two samples of one feature."""
    ref = ref.dropna()
    cur = cur.dropna()
    if len(ref) < 5 or len(cur) < 5:
        return 0.0
    quantiles = [i / bins for i in range(bins + 1)]
    edges = ref.quantile(quantiles).unique().astype(float)
    if len(edges) < 3:
        return 0.0  # ~constant reference: PSI undefined, treat as no drift
    # open the outer bins to ±inf so shifted current values land in the extremes
    edges[0], edges[-1] = float("-inf"), float("inf")
    ref_hist = pd.cut(ref, bins=edges).value_counts(normalize=True).sort_index()
    cur_hist = pd.cut(cur, bins=edges).value_counts(normalize=True).sort_index()
    psi = 0.0
    for r, c in zip(ref_hist, cur_hist, strict=False):
        r = max(r, 1e-4)
        c = max(c, 1e-4)
        psi += (c - r) * math.log(c / r)
    return float(psi)


def _drift_psi(reference: pd.DataFrame, current: pd.DataFrame) -> dict:
    per_feature = {}
    drifted = 0
    for col in FEATURES:
        if col in reference and col in current:
            score = _psi(reference[col], current[col])
            is_drift = score > PSI_DRIFT_THRESHOLD
            per_feature[col] = {"score": round(score, 4), "drifted": is_drift}
            drifted += int(is_drift)
    share = drifted / max(1, len(per_feature))
    return {
        "engine": "psi",
        "share_drifted": round(share, 3),
        "n_drifted": drifted,
        "per_feature": per_feature,
        "dataset_drift": share >= settings.drift_threshold,
    }


def _drift_evidently(reference: pd.DataFrame, current: pd.DataFrame) -> dict:
    from evidently.metric_preset import DataDriftPreset
    from evidently.report import Report

    report = Report(
        metrics=[
            DataDriftPreset(
                stattest="psi",
                stattest_threshold=PSI_DRIFT_THRESHOLD,
            )
        ]
    )
    report.run(reference_data=reference[FEATURES], current_data=current[FEATURES])

    # DataDriftPreset expands into several metrics and their order is not part of
    # Evidently's contract. Only DataDriftTable carries `drift_by_columns`, and it is
    # not the first one: DatasetDriftMetric is, and that one has no feature table at
    # all. Reading `metrics[0]` therefore returned an empty `per_feature` every time
    # Evidently actually ran, which the model card, the API and the dashboard all
    # read. Select the metric by the key we need, never by position.
    result = next(
        (
            m["result"]
            for m in report.as_dict()["metrics"]
            if "drift_by_columns" in m.get("result", {})
        ),
        None,
    )
    if result is None:
        raise KeyError("no Evidently metric exposed `drift_by_columns`")

    share = float(result.get("share_of_drifted_columns", 0.0))
    per_feature = {
        col: {
            "score": round(float(info.get("drift_score", 0.0)), 4),
            "drifted": bool(info.get("drift_detected", False)),
        }
        for col, info in result["drift_by_columns"].items()
    }
    if not per_feature:
        # An empty table silently breaks every consumer downstream. Fail here so
        # `check_drift` falls back to PSI, which always returns a populated table.
        raise ValueError("Evidently returned an empty feature table")

    return {
        "engine": "evidently",
        "share_drifted": round(share, 3),
        "n_drifted": int(result.get("number_of_drifted_columns", 0)),
        "per_feature": per_feature,
        "dataset_drift": share >= settings.drift_threshold,
    }


_evidently_warned = False


def check_drift(reference: pd.DataFrame, current: pd.DataFrame) -> dict:
    """Return a drift report from the configured engine, PSI if it is not reachable.

    The engine is read from config rather than discovered at runtime. It used to be
    "try Evidently, fall back to PSI on any error", which made the detector depend on
    the environment instead of on a decision: this repo keeps its venv inside the
    project directory, and one of Evidently's transitive imports refuses to load from
    the working directory, so every local run silently took the PSI branch while CI,
    Docker and Render took the Evidently one. Same commit, two different detectors,
    and the difference only ever showed up as a red CI job.
    """
    global _evidently_warned
    if settings.drift_engine != "evidently":
        return _drift_psi(reference, current)
    try:
        return _drift_evidently(reference, current)
    except Exception as exc:  # noqa: BLE001
        # Once per process, not once per check. A per-station check runs this for
        # every ready station on every window, so the un-suppressed version buried
        # the actual drift and retrain lines under repeats of the same warning.
        if not _evidently_warned:
            _evidently_warned = True
            print(f"[drift] Evidently unavailable ({exc}); using the PSI fallback "
                  "for the rest of this run")
        return _drift_psi(reference, current)


# ── per-station drift ──────────────────────────────────────────────────
# Why this exists: PM2.5 in Jakarta is not one distribution, it is five. Station
# baselines differ structurally (traffic density, coastline, industry), so pooling
# every station into one window does two bad things at once. It invents drift when
# the mix of stations in the window shifts, and it hides drift when one station goes
# haywire while the other four stay calm. The second failure is the expensive one:
# a fire in Jakarta Utara is exactly the event this system claims to catch.
_MIN_ROWS = 5  # below this, PSI/Evidently have nothing to say


def check_drift_by_station(
    reference: dict[str, pd.DataFrame],
    current: dict[str, pd.DataFrame],
) -> dict:
    """Run `check_drift` independently per station and fold the results into one report.

    Returns the same keys as `check_drift` (so every existing consumer, the model card,
    the API, and the dashboard keep working) plus `per_station`, `drifted_stations`,
    and the station-level counts.

    ── Trigger rule: retrain when ANY station drifts ──
    The brief offered two rules: any-station, or share-of-stations >= drift_threshold.
    Any-station is the defensible one here, and the reason is cost asymmetry, not taste.

    In PULSE, "retrain" does not mean an expensive training job. The online models have
    already adapted event by event, so a retrain snapshots that adapted state into an
    immutable version, writes a card, and re-baselines the drifted station's reference
    window. The cost of firing once too often is one registry row and one markdown file.
    The cost of NOT firing is that the model card and the monitoring baseline keep
    describing a regime that no longer exists at that station, silently, which is the
    precise failure mode this project exists to argue against.

    The share rule needs 3 of 5 Jakarta stations to move before it reacts. That threshold
    is only reachable by a city-wide event, so it would systematically miss local ones.

    The obvious objection to any-station is churn: one flaky sensor minting versions
    forever. That is damped structurally rather than by a threshold, because after a
    retrain only the drifted stations get their reference reset (see Engine._retrain).
    A station that has genuinely moved to a new regime is immediately re-baselined
    against that new regime, so it stops re-triggering on the same shift.

    `share_stations_drifted` is reported anyway, so switching to the share rule later
    is a one-line change in the caller, not a rewrite.
    """
    per_station: dict[str, dict] = {}
    for sid in sorted(set(reference) & set(current)):
        ref, cur = reference[sid], current[sid]
        if len(ref) < _MIN_ROWS or len(cur) < _MIN_ROWS:
            continue  # not enough evidence for this station yet
        per_station[sid] = check_drift(ref, cur)

    drifted = [sid for sid, rep in per_station.items() if rep["dataset_drift"]]
    n_stations = len(per_station)
    share_stations = round(len(drifted) / n_stations, 3) if n_stations else 0.0

    # The flat fields describe the WORST station, so a model card that renders only
    # `per_feature` still shows the feature table that actually caused the promotion.
    worst_sid = max(per_station, key=lambda s: per_station[s]["share_drifted"], default=None)
    worst = per_station.get(worst_sid, {}) if worst_sid else {}
    engines = {rep["engine"] for rep in per_station.values()}

    return {
        "engine": engines.pop() if len(engines) == 1 else ("mixed" if engines else "none"),
        "scope": "per_station",
        "dataset_drift": bool(drifted),          # ← the any-station rule, argued above
        "share_drifted": worst.get("share_drifted", 0.0),
        "n_drifted": worst.get("n_drifted", 0),
        "per_feature": worst.get("per_feature", {}),
        "worst_station": worst_sid,
        "per_station": per_station,
        "drifted_stations": drifted,
        "n_stations": n_stations,
        "n_stations_drifted": len(drifted),
        "share_stations_drifted": share_stations,
    }
