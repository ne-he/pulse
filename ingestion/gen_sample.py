"""Generate a realistic *synthetic* Jakarta air-quality dataset so the whole
loop runs with no internet and no API keys. Replay streams this file as "live".

The signal is deliberately structured (diurnal traffic peaks, weekly rhythm,
wind-driven dispersion, a few injected pollution spikes) so the online model has
something real to learn and the anomaly detector has something to catch."""
from __future__ import annotations

import os

import numpy as np
import pandas as pd

from common.schemas import STATIONS

OUT_PATH = os.environ.get("SAMPLE_PATH", "./data/sample_aq.csv")
DAYS = int(os.environ.get("SAMPLE_DAYS", "14"))
FREQ_MIN = int(os.environ.get("SAMPLE_FREQ_MIN", "10"))


def _diurnal(hours: np.ndarray) -> np.ndarray:
    """Two traffic peaks (~7am, ~7pm) on a baseline."""
    morning = np.exp(-((hours - 7) ** 2) / 6)
    evening = np.exp(-((hours - 19) ** 2) / 8)
    return 0.5 + 0.9 * morning + 1.1 * evening


def generate() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    periods = int(DAYS * 24 * 60 / FREQ_MIN)
    idx = pd.date_range(end=pd.Timestamp.utcnow().floor("min"), periods=periods, freq=f"{FREQ_MIN}min")

    rows = []
    for sid, meta in STATIONS.items():
        hours = idx.hour.to_numpy() + idx.minute.to_numpy() / 60
        dow = idx.dayofweek.to_numpy()

        # weather: temperature diurnal, humidity inverse, wind random-walk
        temp = 28 + 4 * np.sin((hours - 15) / 24 * 2 * np.pi) + rng.normal(0, 0.6, periods)
        humidity = np.clip(75 - (temp - 28) * 3 + rng.normal(0, 4, periods), 40, 99)
        wind = np.clip(np.abs(np.cumsum(rng.normal(0, 0.15, periods))) % 6 + 0.5, 0.2, 8)

        # base pm2.5: station offset + diurnal + weekday factor − wind dispersion
        station_base = 18 + 6 * list(STATIONS).index(sid)
        weekday_factor = np.where(dow < 5, 1.0, 0.7)
        pm = station_base * _diurnal(hours) * weekday_factor
        pm = pm - wind * 2.5 + rng.normal(0, 3, periods)

        # inject 2–3 spikes per station (fires/traffic jams) for the anomaly story
        for _ in range(rng.integers(2, 4)):
            start = rng.integers(0, periods - 30)
            dur = rng.integers(6, 18)
            pm[start:start + dur] += rng.uniform(40, 90)

        pm = np.clip(pm, 2, None)
        pm10 = pm * rng.uniform(1.4, 1.8, periods)
        no2 = np.clip(pm * 0.6 + rng.normal(0, 5, periods), 1, None)
        o3 = np.clip(40 - pm * 0.2 + rng.normal(0, 6, periods), 1, None)

        for i, t in enumerate(idx):
            rows.append({
                "ts": t.isoformat(),
                "station_id": sid,
                "station_name": meta["name"],
                "pm25": round(float(pm[i]), 2),
                "pm10": round(float(pm10[i]), 2),
                "no2": round(float(no2[i]), 2),
                "o3": round(float(o3[i]), 2),
                "temp": round(float(temp[i]), 2),
                "humidity": round(float(humidity[i]), 2),
                "wind_speed": round(float(wind[i]), 2),
            })

    df = pd.DataFrame(rows).sort_values("ts").reset_index(drop=True)
    return df


def main() -> None:
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    df = generate()
    df.to_csv(OUT_PATH, index=False)
    print(f"[gen_sample] wrote {len(df):,} rows for {df.station_id.nunique()} stations -> {OUT_PATH}")


if __name__ == "__main__":
    main()
