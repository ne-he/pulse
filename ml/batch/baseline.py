"""M1 batch baseline — the honest comparison point for the online model.

Trains a simple batch forecaster (persistence + seasonal-naive) on the sample
CSV and reports MAE/RMSE, so the README can show 'online learning beats / matches
batch' rather than claiming it without evidence. Run: python -m ml.batch.baseline"""
from __future__ import annotations

import math
import os

import pandas as pd

SAMPLE_PATH = os.environ.get("SAMPLE_PATH", "./data/sample_aq.csv")
SEASON = 144  # 24h at 10-min resolution


def evaluate(df: pd.DataFrame, station_id: str) -> dict:
    s = df[df.station_id == station_id].sort_values("ts")["pm25"].reset_index(drop=True)
    if len(s) <= SEASON + 1:
        return {"station_id": station_id, "n": len(s)}

    # persistence: ŷ_t = y_{t-1}
    persist_err = (s.diff().dropna()).abs()
    # seasonal-naive: ŷ_t = y_{t-season}
    seasonal_err = (s - s.shift(SEASON)).dropna().abs()

    def mae(x):
        return round(float(x.mean()), 3)

    def rmse(x):
        return round(float(math.sqrt((x ** 2).mean())), 3)

    return {
        "station_id": station_id,
        "n": int(len(s)),
        "persistence": {"mae": mae(persist_err), "rmse": rmse(persist_err)},
        "seasonal_naive": {"mae": mae(seasonal_err), "rmse": rmse(seasonal_err)},
    }


def main() -> None:
    if not os.path.exists(SAMPLE_PATH):
        from ingestion import gen_sample
        gen_sample.main()
    df = pd.read_csv(SAMPLE_PATH)
    print("[baseline] batch baselines per station (lower = better):\n")
    for sid in df.station_id.unique():
        print(evaluate(df, sid))


if __name__ == "__main__":
    main()
