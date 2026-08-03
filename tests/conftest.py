"""Shared test helpers: deterministic event streams and an isolated Engine.

Everything here is seeded. No test may depend on wall-clock time, network, or Redis.
"""
from __future__ import annotations

from collections import deque

import numpy as np
import pytest

from common.config import settings

SEED = 20260801


# ── deterministic event streams ────────────────────────────────────────
def make_events(
    station_id: str,
    n: int,
    *,
    pm_mean: float = 20.0,
    pm_sd: float = 3.0,
    temp_mean: float = 29.0,
    humidity_mean: float = 70.0,
    wind_mean: float = 1.5,
    wind_sd: float = 0.4,
    seed: int = SEED,
    start: int = 0,
) -> list[dict]:
    """A seeded stream of AQ events for one station, shaped like the real feed."""
    rng = np.random.default_rng(seed)
    events = []
    for i in range(n):
        step = start + i
        hour = (step // 6) % 24
        events.append({
            "station_id": station_id,
            "station_name": station_id.upper(),
            "ts": f"2026-08-01T{hour:02d}:{(step % 6) * 10:02d}:00+00:00",
            "pm25": round(float(max(1.0, rng.normal(pm_mean, pm_sd))), 2),
            "temp": round(float(rng.normal(temp_mean, 1.0)), 2),
            "humidity": round(float(rng.normal(humidity_mean, 4.0)), 2),
            "wind_speed": round(float(max(0.05, rng.normal(wind_mean, wind_sd))), 2),
        })
    return events


def calm_events(station_id: str, n: int, *, seed: int = SEED, start: int = 0) -> list[dict]:
    """Business-as-usual Jakarta air: moderate PM2.5, normal breeze."""
    return make_events(station_id, n, seed=seed, start=start)


def polluted_events(station_id: str, n: int, *, seed: int = SEED + 1, start: int = 0) -> list[dict]:
    """A new regime: PM2.5 far higher, wind collapsed, air drier and hotter.

    This is the shape of a real Jakarta pollution episode (fire or a stagnant
    inversion), not just one column nudged, which is why several features move.
    """
    return make_events(
        station_id, n,
        pm_mean=125.0, pm_sd=8.0,
        temp_mean=34.0, humidity_mean=45.0,
        wind_mean=0.25, wind_sd=0.06,
        seed=seed, start=start,
    )


def interleave(*streams: list[dict]) -> list[dict]:
    """Round-robin several station streams, the way the replay engine emits frames."""
    out = []
    for frame in zip(*streams, strict=False):
        out.extend(frame)
    return out


# ── river state fingerprinting ─────────────────────────────────────────
def numeric_state(obj, _depth: int = 0, _seen: set[int] | None = None) -> list[float]:
    """Every number reachable inside an object, in a stable order.

    Used to prove the river model's internals actually moved. Walking the object
    graph rather than naming specific attributes keeps the test honest across river
    versions: it asserts "the learned state changed", not "this private field exists".
    """
    if _seen is None:
        _seen = set()
    if _depth > 6 or obj is None or isinstance(obj, str | bytes):
        return []
    if isinstance(obj, bool):
        return []
    if isinstance(obj, int | float | np.floating | np.integer):
        return [float(obj)]
    if id(obj) in _seen:
        return []
    _seen.add(id(obj))

    out: list[float] = []
    if isinstance(obj, dict):
        for key in sorted(obj, key=str):
            out += numeric_state(obj[key], _depth + 1, _seen)
        return out
    if isinstance(obj, list | tuple | set | frozenset | deque | np.ndarray):
        for item in obj:
            out += numeric_state(item, _depth + 1, _seen)
        return out
    attrs = getattr(obj, "__dict__", None)
    if attrs:
        for key in sorted(attrs, key=str):
            out += numeric_state(attrs[key], _depth + 1, _seen)
    return out


class CountingRiverModel:
    """Transparent proxy that counts `learn_one` calls on the wrapped river model."""

    def __init__(self, inner):
        self._inner = inner
        self.learn_calls = 0

    def learn_one(self, y, x=None):
        self.learn_calls += 1
        return self._inner.learn_one(y, x=x)

    def forecast(self, horizon, xs=None):
        return self._inner.forecast(horizon=horizon, xs=xs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


# ── fixtures ───────────────────────────────────────────────────────────
@pytest.fixture
def isolated_registry(tmp_path, monkeypatch):
    """Point the registry at tmp_path. Everything else stays at production settings.

    Deliberately does NOT shrink `drift_window`. The tempting speed-up (window=30)
    makes the whole suite lie: the PSI fallback bins the reference into 10 quantiles,
    so a 30-row window leaves 3 samples per bin and PSI fires on pure sampling noise.
    Measured on this data, calm-vs-calm windows report drift 12 times out of 12 at
    window=30 and 9 out of 12 at window=100, versus 0 out of 12 at the production
    default of 200. Testing at 30 would have "proved" the retrain loop works using
    drift that was not there. At ~7 ms/event the honest version costs a few seconds.
    """
    monkeypatch.setattr(settings, "registry_path", str(tmp_path / "registry"))
    return tmp_path
