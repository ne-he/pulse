"""Streaming anomaly detection with river HalfSpaceTrees (per station).

Scores every event in [0,1]; a score above the threshold flags a sudden spike.
This is what turns a quiet feed into an incident → the agent then narrates it."""
from __future__ import annotations

from common.config import settings

try:
    from river import anomaly, compose, preprocessing
    _HAS_RIVER = True
except Exception:  # noqa: BLE001
    _HAS_RIVER = False


class AnomalyDetector:
    def __init__(self, station_id: str, threshold: float | None = None):
        self.station_id = station_id
        self.threshold = threshold if threshold is not None else settings.anomaly_threshold
        self._last_pm25 = None
        if _HAS_RIVER:
            # scale features online, then half-space trees
            self.model = compose.Pipeline(
                preprocessing.MinMaxScaler(),
                anomaly.HalfSpaceTrees(n_trees=10, height=8, window_size=50, seed=42),
            )
        else:
            self.model = None

    def _features(self, event: dict) -> dict:
        pm25 = float(event.get("pm25") or 0)
        delta = 0.0 if self._last_pm25 is None else pm25 - self._last_pm25
        return {"pm25": pm25, "delta": delta, "wind": float(event.get("wind_speed") or 1.0)}

    def score(self, event: dict) -> tuple[float, bool]:
        """Return (anomaly_score, is_anomaly). Learns from the event too."""
        x = self._features(event)
        if self.model is None:
            # crude fallback: z-style jump detector on delta
            score = min(1.0, abs(x["delta"]) / 50.0)
        else:
            score = float(self.model.score_one(x))
            self.model.learn_one(x)
        self._last_pm25 = x["pm25"]
        return score, score >= self.threshold
