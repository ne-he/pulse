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


def check_drift(reference: pd.DataFrame, current: pd.DataFrame) -> dict:
    """Return a drift report. Tries Evidently, falls back to PSI on any error."""
    try:
        return _drift_evidently(reference, current)
    except Exception as exc:  # noqa: BLE001
        print(f"[drift] Evidently unavailable ({exc}); using PSI fallback")
        return _drift_psi(reference, current)
