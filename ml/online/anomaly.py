"""Streaming anomaly detection from one-step forecast surprise (per station).

An anomaly here is defined as "the feed did something the recent past does not
explain": the one-step persistence error, standardised by a robust estimate of
how large that error normally is. High score => the reading is many sigmas away
from what the last few hours of this station would predict.

    resid = pm25_t - pm25_(t-1)
    sigma = 1.4826 * MAD(recent resid)          robust, spike-resistant
    z     = |resid - median(recent resid)| / sigma
    score = z / (1 + z)                          squashed into [0, 1)

WHY NOT HalfSpaceTrees, which this module used until 2026-08-09: its score is
not usable as a decision variable on this feed. Measured over the full 10,080
event sample, the HST score had mean 0.82 and median 0.90, so it compressed
almost every event into the top decile and `score >= 0.85` fired on 58.3% of
events (the number the README carried as a known defect). Both a fixed
threshold and a rolling-quantile gate sat on a knife edge there: the on-demand
demo spike of 120 ug/m3 scored 0.9832 while ordinary events scored up to 0.9964,
so the injected spike was not even in the top percentile. Rescaling the features
did not help, it made saturation worse (mean 0.96). The problem was the
statistic, not the threshold, so the statistic is what changed.

The mapping `score = z / (1 + z)` is chosen so the existing [0, 1] threshold
knob keeps working and keeps its meaning, while the number underneath is now
monotone in sigmas rather than saturating:

    threshold 0.70 <=> z >= 2.33        threshold 0.85 <=> z >= 5.67
    threshold 0.80 <=> z >= 4.00        threshold 0.90 <=> z >= 9.00

Measured on the same 10,080 event sample at the default 0.85: 0.50% of events
flagged (was 58.3%), zero false alarms over 500 events of a calm clean station,
and the 10x spike caught on the event it happens rather than two events later.

This is calibration by construction, not calibration against labels. There is
still no labelled incident set for this feed, so the honest claim is "the score
now means something in sigmas and the rate is plausible", not "the detector is
correct". See docs/TEST_GAP_MAP.md section 6b.
"""
from __future__ import annotations

import statistics
from collections import deque

from common.config import settings
from common.health import pm25_to_aqi

# Median absolute deviation to standard deviation, for a normal distribution.
MAD_TO_SIGMA = 1.4826


class AnomalyDetector:
    """One detector per station. `score()` both scores and learns from the event."""

    def __init__(
        self,
        station_id: str,
        threshold: float | None = None,
        warmup: int | None = None,
        aqi_floor: int | None = None,
        window: int | None = None,
    ):
        self.station_id = station_id
        self.threshold = threshold if threshold is not None else settings.anomaly_threshold
        self.warmup = warmup if warmup is not None else settings.anomaly_warmup
        self.aqi_floor = aqi_floor if aqi_floor is not None else settings.anomaly_aqi_floor
        self.residuals: deque[float] = deque(
            maxlen=window if window is not None else settings.anomaly_window
        )
        self._last_pm25: float | None = None
        self.n = 0
        # Read by the consumer to describe the event: the reading it jumped from,
        # the size of the jump, and which way. Set on every scored event.
        self.prev_pm25: float | None = None
        self.last_resid = 0.0
        self.direction = "flat"

    # ── robust spread of the recent one-step errors ─────────────────────
    def _center_and_sigma(self, pm25: float) -> tuple[float, float]:
        """Median and robust sigma of recent residuals.

        The 0.5 ug/m3 floor matters: sensors quantise, and a perfectly flat feed
        gives MAD == 0, which would divide by zero and make every later reading
        infinitely surprising."""
        if len(self.residuals) < 3:
            return 0.0, max(0.15 * max(pm25, 1.0), 0.5)
        center = statistics.median(self.residuals)
        mad = statistics.median([abs(r - center) for r in self.residuals])
        return center, max(MAD_TO_SIGMA * mad, 0.5)

    def score(self, event: dict) -> tuple[float, bool]:
        """Return (anomaly_score, is_anomaly). Learns from the event too."""
        pm25 = float(event.get("pm25") or 0.0)

        if self._last_pm25 is None:          # nothing to be surprised by yet
            self._last_pm25 = pm25
            self.n += 1
            return 0.0, False

        resid = pm25 - self._last_pm25
        center, sigma = self._center_and_sigma(pm25)
        z = abs(resid - center) / sigma

        self.residuals.append(resid)
        self.prev_pm25 = self._last_pm25
        self.last_resid = resid
        self._last_pm25 = pm25
        self.n += 1
        self.direction = "up" if resid > 0 else ("down" if resid < 0 else "flat")

        score = z / (1.0 + z)
        is_anomaly = score >= self.threshold

        # A detector with no sense of normal yet has no business raising alarms.
        if self.n <= self.warmup:
            is_anomaly = False

        # Statistically odd but harmless air is not an incident. Without this the
        # feed narrates "spike" over readings of 2 ug/m3, which is clean air, and
        # the incident list stops being something an ops team would act on.
        if self.aqi_floor and pm25_to_aqi(pm25) < self.aqi_floor:
            is_anomaly = False

        return score, is_anomaly
