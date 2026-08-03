"""Online forecaster — the '1 hal baru': TRUE incremental learning.

`learn_one` is called on EVERY event (not batch retraining in disguise). Each
station keeps its own model. We use river's SNARIMAX with weather + time-of-day
exogenous features, and estimate an uncertainty band from the rolling residual
std. If river misbehaves, we fall back to a last-value baseline so the walking
skeleton NEVER breaks — exactly the philosophy in the build plan."""
from __future__ import annotations

import math
from collections import deque

from common.config import settings

try:  # river is the core dependency, but stay defensive for the skeleton
    from river import linear_model, optim, preprocessing, time_series
    _HAS_RIVER = True
except Exception:  # noqa: BLE001
    _HAS_RIVER = False

# No PM2.5 reading on Earth has ever come close to this. Anything above it is a
# diverged model, not weather, and must never reach the dashboard.
_MAX_PLAUSIBLE_PM25 = 2000.0


def exog_features(event: dict) -> dict:
    """Weather + cyclical time-of-day features the model can regress on."""
    ts = event.get("ts", "")
    hour = 0
    try:
        hour = int(ts[11:13]) if len(ts) >= 13 else 0
    except ValueError:
        hour = 0
    return {
        "temp": float(event.get("temp") or 28.0),
        "humidity": float(event.get("humidity") or 70.0),
        "wind_speed": float(event.get("wind_speed") or 1.0),
        "hour_sin": math.sin(2 * math.pi * hour / 24),
        "hour_cos": math.cos(2 * math.pi * hour / 24),
    }


class StationModel:
    """One online model + live error tracking for a single station."""

    FREQ_MIN = 10  # nominal minutes between observations (for horizon labeling)

    def __init__(self, station_id: str, kind: str | None = None, horizon: int | None = None):
        self.station_id = station_id
        self.kind = kind or settings.model_kind
        self.horizon = horizon or settings.forecast_horizon
        self.n = 0
        self.last_y: float | None = None
        self.pending_1step: float | None = None       # forecast made last tick for THIS tick
        self.residuals: deque[float] = deque(maxlen=300)
        self.recent_pm25: deque[float] = deque(maxlen=settings.drift_window)

        self.model = None
        self._diverged = False
        if self.kind == "river" and _HAS_RIVER:
            try:
                self.model = self._build_river()
            except Exception as exc:  # noqa: BLE001
                print(f"[model] river init failed for {station_id} ({exc}); using baseline")
                self.model = None
        if self.model is None:
            self.kind = "baseline"

    @staticmethod
    def _build_river():
        """SNARIMAX tuned for this data. Every parameter below was measured, not guessed.

        m is the SEASONAL PERIOD and river's "no seasonality" value is 1, not 0. It was
        m=0, which is not "off" but invalid: SNARIMAX builds lag features with
        range(m - 1, m * sp, m), and a step of 0 raises ValueError on the FIRST
        learn_one call. `observe()` then caught it and dropped the station to the
        persistence baseline, permanently and silently, so online learning never ran at
        all. Caught by tests/test_online_learning.py, fixed 2026-08-02.

        With learning switched on for real, the remaining defaults turned out to diverge.
        The MA term (q=1) feeds the model's own residuals back in as features, so at
        river's default learning rate the errors compound: replaying the 14-day sample
        gave a mean 1-step MAE around 5.5e10 µg/m³ and forecasts up to 1.8e11.

        Measured on the full sample (10,080 events, mean 1-step MAE across 5 stations,
        persistence baseline = 3.42):

            p=2 d=0 q=1, river defaults           5.5e10   diverged
            p=3 d=1 q=1, SGD(0.005)               5.0e10   diverged
            p=2 d=1 q=0, river defaults            5.71    stable, worse than persistence
            p=2 d=1 q=1, SGD(0.002) ilr=0.005      3.28    stable, beats persistence

        d=1 makes the target stationary and the slower learning rates stop the MA
        feedback loop from compounding. Re-checked over 30,000 events (the sample looped
        three times): MAE 3.27, largest forecast 144 µg/m³, no divergence.
        """
        return time_series.SNARIMAX(
            p=2, d=1, q=1, m=1,
            regressor=(
                preprocessing.StandardScaler()
                | linear_model.LinearRegression(
                    optimizer=optim.SGD(0.002), intercept_lr=0.005,
                )
            ),
        )

    # ── error metrics (computed online from residuals) ──────────────────
    @property
    def mae(self) -> float | None:
        return sum(abs(r) for r in self.residuals) / len(self.residuals) if self.residuals else None

    @property
    def rmse(self) -> float | None:
        if not self.residuals:
            return None
        return math.sqrt(sum(r * r for r in self.residuals) / len(self.residuals))

    def _resid_std(self) -> float:
        if len(self.residuals) < 3:
            return max(2.0, (self.last_y or 10.0) * 0.15)  # sensible prior band
        mean = sum(self.residuals) / len(self.residuals)
        var = sum((r - mean) ** 2 for r in self.residuals) / len(self.residuals)
        return math.sqrt(var)

    # ── forecasting ─────────────────────────────────────────────────────
    def _point_forecast(self, exog: dict, steps: int) -> float:
        if self.model is not None:
            try:
                preds = self.model.forecast(horizon=steps, xs=[exog] * steps)
                point = float(preds[-1])
                # Divergence guard. The tuned config is stable over 30k events, but an
                # online model that runs forever gets no such guarantee, and a NaN or a
                # 1e11 µg/m³ "forecast" reaching the dashboard is worse than no forecast.
                # Fall back to last-value for this tick and say so, loudly, once.
                if not math.isfinite(point) or abs(point) > _MAX_PLAUSIBLE_PM25:
                    if not self._diverged:
                        self._diverged = True
                        print(f"[model] ⚠️ {self.station_id}: implausible forecast "
                              f"{point:.3g} µg/m³ after {self.n} events; using last-value "
                              "for this tick. The online model may have diverged.")
                else:
                    self._diverged = False
                    return point
            except Exception:  # noqa: BLE001
                pass  # fall through to baseline
        return float(self.last_y if self.last_y is not None else exog.get("pm25", 20.0))

    def forecast(self, exog: dict) -> tuple[float, float, float]:
        """Return (point, lower, upper) for `horizon` steps ahead."""
        point = max(0.0, self._point_forecast(exog, self.horizon))
        z = settings.uncertainty_z
        std = self._resid_std()
        return point, max(0.0, point - z * std), point + z * std

    # ── incremental learning (called on EVERY event) ────────────────────
    def observe(self, y: float, exog: dict) -> None:
        # 1) score the 1-step forecast we made last tick against the actual now
        if self.pending_1step is not None:
            self.residuals.append(y - self.pending_1step)
        # 2) TRUE online update
        if self.model is not None:
            try:
                self.model.learn_one(y, x=exog)
            except Exception as exc:  # noqa: BLE001
                # Keep the never-break philosophy: the loop degrades instead of dying.
                # But say so out loud. This branch swallowed a fatal river bug for six
                # weeks while the README advertised online learning, because a silent
                # fallback looks exactly like a working system.
                print(f"[model] ⚠️ learn_one failed for {self.station_id} after "
                      f"{self.n} events ({exc}); degrading to baseline")
                self.model = None  # degrade to baseline permanently if it breaks
                self.kind = "baseline"
        self.last_y = y
        self.recent_pm25.append(y)
        self.n += 1
        # 3) stash a fresh 1-step forecast to be scored next tick
        self.pending_1step = self._point_forecast(exog, 1)

    def trend(self, forecast: float) -> str:
        if self.last_y is None:
            return "flat"
        delta = forecast - self.last_y
        if abs(delta) < max(1.0, 0.05 * self.last_y):
            return "flat"
        return "up" if delta > 0 else "down"

    def horizon_minutes(self) -> int:
        return self.horizon * self.FREQ_MIN
