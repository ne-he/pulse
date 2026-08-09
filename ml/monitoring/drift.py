"""Drift monitoring — closes the loop. When the live feature distribution drifts
away from the reference window, this fires the signal that triggers retraining.

Primary engine: Evidently (DataDriftPreset). If Evidently's API differs across
versions, we fall back to a dependency-light PSI (Population Stability Index) so
the closed loop keeps working. Either way we return a consistent shape."""
from __future__ import annotations

import math

import pandas as pd

from common.config import settings

FEATURES = ["pm25", "temp", "humidity", "wind_speed"]


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
            is_drift = score > 0.2  # standard PSI rule of thumb
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

    report = Report(metrics=[DataDriftPreset()])
    report.run(reference_data=reference[FEATURES], current_data=current[FEATURES])
    result = report.as_dict()["metrics"][0]["result"]
    share = float(result.get("share_of_drifted_columns", 0.0))
    per_feature = {
        col: {
            "score": round(float(info.get("drift_score", 0.0)), 4),
            "drifted": bool(info.get("drift_detected", False)),
        }
        for col, info in result.get("drift_by_columns", {}).items()
    }
    return {
        "engine": "evidently",
        "share_drifted": round(share, 3),
        "n_drifted": int(result.get("number_of_drifted_columns", 0)),
        "per_feature": per_feature,
        "dataset_drift": share >= settings.drift_threshold,
    }


_evidently_warned = False


def check_drift(reference: pd.DataFrame, current: pd.DataFrame) -> dict:
    """Return a drift report. Tries Evidently, falls back to PSI on any error."""
    global _evidently_warned
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
