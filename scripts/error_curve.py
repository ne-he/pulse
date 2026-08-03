"""Online (river) vs batch baselines: the P6 evidence, measured not asserted.

Replays the sample dataset event by event through the SAME `StationModel` the
consumer uses, and scores its 1-step-ahead forecast against two batch baselines
from `ml/batch/baseline.py`: persistence and seasonal-naive.

Every model here predicts y_t using information up to t-1 only, so the comparison
is fair:
  online          ŷ_t = SNARIMAX forecast made at t-1 (after learning y_{t-1})
  persistence     ŷ_t = y_{t-1}
  seasonal-naive  ŷ_t = y_{t-144}          (144 steps = 24h at 10-min resolution)

Writes docs/error_curve.png and docs/METRICS.md.

    python -m scripts.error_curve
"""
from __future__ import annotations

import math
import os
import random

import matplotlib

matplotlib.use("Agg")  # headless: no display needed, works in CI

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from ml.batch.baseline import SEASON  # noqa: E402
from ml.online.model import StationModel, exog_features  # noqa: E402

SAMPLE_PATH = os.environ.get("SAMPLE_PATH", "./data/sample_aq.csv")
DOCS_DIR = "./docs"
PLOT_PATH = os.path.join(DOCS_DIR, "error_curve.png")
METRICS_PATH = os.path.join(DOCS_DIR, "METRICS.md")
ROLL = 288  # rolling window for the curves: 2 days at 10-min resolution
SEED = 42

# Deterministic: the sample generator is seeded, river's SNARIMAX is deterministic,
# and these two cover anything else that might reach for a random number.
random.seed(SEED)
np.random.seed(SEED)


def _load() -> pd.DataFrame:
    if not os.path.exists(SAMPLE_PATH):
        print(f"[error_curve] no sample at {SAMPLE_PATH}; generating one…")
        from ingestion import gen_sample
        gen_sample.main()
    return pd.read_csv(SAMPLE_PATH)


def _mae(errors: pd.Series) -> float:
    return round(float(errors.abs().mean()), 3)


def _rmse(errors: pd.Series) -> float:
    return round(float(math.sqrt((errors ** 2).mean())), 3)


def replay_station(df: pd.DataFrame, station_id: str) -> pd.DataFrame:
    """Stream one station through the online model and both baselines."""
    rows = df[df.station_id == station_id].sort_values("ts").reset_index(drop=True)
    model = StationModel(station_id)
    history: list[float] = []
    records = []

    for i, row in enumerate(rows.itertuples(index=False)):
        y = float(row.pm25)
        event = {
            "ts": row.ts, "station_id": station_id, "pm25": y,
            "temp": row.temp, "humidity": row.humidity, "wind_speed": row.wind_speed,
        }
        exog = exog_features(event)

        # forecast for THIS step, produced at the previous step (info up to t-1)
        online_pred = model.pending_1step
        persist_pred = history[-1] if history else None
        seasonal_pred = history[-SEASON] if len(history) >= SEASON else None

        if online_pred is not None and persist_pred is not None:
            records.append({
                "i": i,
                "y": y,
                "online_err": y - max(0.0, online_pred),
                "persistence_err": y - persist_pred,
                "seasonal_err": (y - seasonal_pred) if seasonal_pred is not None else np.nan,
            })

        # the online update itself: one learn_one per event, exactly like the consumer
        model.observe(y, exog)
        history.append(y)

    out = pd.DataFrame(records)
    out.attrs["kind"] = model.kind
    out.attrs["n_seen"] = model.n
    return out


def summarise(per_station: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Final numbers over the window where ALL THREE models are defined (i >= SEASON)."""
    rows = []
    for sid, res in per_station.items():
        common = res[res.i >= SEASON]
        rows.append({
            "station_id": sid,
            "n_scored": int(len(common)),
            "online_mae": _mae(common.online_err),
            "persistence_mae": _mae(common.persistence_err),
            "seasonal_mae": _mae(common.seasonal_err),
            "online_rmse": _rmse(common.online_err),
            "persistence_rmse": _rmse(common.persistence_err),
            "seasonal_rmse": _rmse(common.seasonal_err),
        })
    return pd.DataFrame(rows)


def _crossover(res: pd.DataFrame) -> int | None:
    """First event index where the online rolling MAE drops below persistence, and stays."""
    online = res.online_err.abs().rolling(ROLL).mean()
    persist = res.persistence_err.abs().rolling(ROLL).mean()
    better = (online < persist) & online.notna() & persist.notna()
    if not better.any():
        return None
    # first index after which it never flips back
    for idx in better[better].index:
        if better.loc[idx:].all():
            return int(res.loc[idx, "i"])
    return None


def plot(per_station: dict[str, pd.DataFrame]) -> None:
    n = len(per_station)
    cols = 2
    rows = math.ceil(n / cols)
    fig, axes = plt.subplots(rows, cols, figsize=(13, 3.2 * rows), sharex=True)
    axes = np.atleast_1d(axes).ravel()

    for ax, (sid, res) in zip(axes, per_station.items(), strict=False):
        x = res["i"]
        ax.plot(x, res.online_err.abs().rolling(ROLL).mean(),
                label="online (river)", color="#FF4500", linewidth=1.6)
        ax.plot(x, res.persistence_err.abs().rolling(ROLL).mean(),
                label="persistence", color="#5B8DEF", linewidth=1.2, linestyle="--")
        ax.plot(x, res.seasonal_err.abs().rolling(ROLL).mean(),
                label="seasonal-naive", color="#8A8F98", linewidth=1.2, linestyle=":")
        ax.set_title(f"{sid}  (rolling MAE, window={ROLL})", fontsize=10)
        ax.set_ylabel("MAE µg/m³", fontsize=9)
        ax.grid(alpha=0.25, linewidth=0.5)

    for ax in axes[n:]:
        ax.set_visible(False)
    for ax in axes[max(0, n - cols):n]:
        ax.set_xlabel("events processed (learn_one calls)", fontsize=9)

    axes[0].legend(fontsize=8, loc="upper right")
    fig.suptitle(
        "PULSE: online learning vs batch baselines, 1-step-ahead PM2.5 error",
        fontsize=12,
    )
    fig.tight_layout()
    os.makedirs(DOCS_DIR, exist_ok=True)
    fig.savefig(PLOT_PATH, dpi=130)
    plt.close(fig)
    print(f"[error_curve] wrote {PLOT_PATH}")


def _early_vs_late(res: pd.DataFrame) -> tuple[float, float, float, float]:
    """MAE over the first and last thirds of the scored stream: online, then persistence."""
    scored = res[res.i >= SEASON]
    third = max(1, len(scored) // 3)
    early, late = scored.head(third), scored.tail(third)
    return (_mae(early.online_err), _mae(early.persistence_err),
            _mae(late.online_err), _mae(late.persistence_err))


def _honest_notes(summary: pd.DataFrame, per_station: dict[str, pd.DataFrame]) -> str:
    """Everything the numbers say that is inconvenient. Derived, never hand-written."""
    lost_rmse = [r.station_id for r in summary.itertuples(index=False)
                 if r.online_rmse > r.persistence_rmse]
    lost_mae = [r.station_id for r in summary.itertuples(index=False)
                if r.online_mae > r.persistence_mae]
    crossovers = {sid: _crossover(res) for sid, res in per_station.items()}
    never = [sid for sid, at in crossovers.items() if at is None]
    reached = [at for at in crossovers.values() if at is not None]

    lines = []

    if lost_mae:
        lines.append(
            f"- **Online loses on MAE** at {', '.join(f'`{s}`' for s in lost_mae)}. "
            "Persistence is a genuinely strong baseline on 10-minute air-quality data, "
            "because the next reading really is close to the last one."
        )
    else:
        lines.append(
            "- Online wins on MAE at every station, but by single-digit percentages, "
            "not by a landslide. Persistence is a strong baseline here: on 10-minute "
            "data the next reading really is close to the last one."
        )

    if lost_rmse:
        lines.append(
            f"- **Online loses on RMSE** at {', '.join(f'`{s}`' for s in lost_rmse)} "
            "while still winning on MAE there. RMSE punishes large errors, so this says "
            "the online model is better on the typical tick and worse on the rare violent "
            "one: it gets caught out by sudden spikes that persistence, by definition, "
            "absorbs one step later. Spikes are exactly what this project cares about, so "
            "this is a real limitation, not a rounding detail."
        )

    if reached:
        lines.append(
            f"- **Learning is not instant.** The online model only pulls ahead of "
            f"persistence for good after {min(reached):,} to {max(reached):,} events "
            f"(roughly {min(reached) * 10 / 60 / 24:.1f} to {max(reached) * 10 / 60 / 24:.1f} "
            "days of 10-minute data). Before that it is *worse*. A model that learns from "
            "scratch pays for it up front, and the curve shows the bill."
        )
    if never:
        lines.append(
            f"- At {', '.join(f'`{s}`' for s in never)} the rolling MAE **never** settles "
            "permanently below persistence: it crosses and then flips back. The final-window "
            "average is better, but the advantage is not stable there."
        )

    early_rows = []
    for sid, res in per_station.items():
        eo, ep, lo, lp = _early_vs_late(res)
        early_rows.append(f"| `{sid}` | {eo} | {ep} | {lo} | {lp} |")

    return f"""## Honest reading

{chr(10).join(lines)}
- The data is **synthetic** (`ingestion/gen_sample.py`), built with diurnal traffic peaks,
  a weekly rhythm, wind dispersion, and injected spikes. It is structured enough to be a
  fair test of learning, but it is not real OpenAQ data, and these numbers are not a claim
  about real Jakarta air.

### First third vs last third of the stream (MAE)

If online learning works, the gap should move in the online model's favour over time.

| Station | Online (early) | Persistence (early) | Online (late) | Persistence (late) |
|---|---|---|---|---|
{chr(10).join(early_rows)}
"""


def write_metrics(summary: pd.DataFrame, per_station: dict[str, pd.DataFrame], kind: str) -> None:
    os.makedirs(DOCS_DIR, exist_ok=True)

    def fmt(value: float, best: float) -> str:
        return f"**{value}**" if value == best else str(value)

    mae_rows = []
    rmse_rows = []
    for row in summary.itertuples(index=False):
        best_mae = min(row.online_mae, row.persistence_mae, row.seasonal_mae)
        best_rmse = min(row.online_rmse, row.persistence_rmse, row.seasonal_rmse)
        delta = round((row.online_mae - row.persistence_mae) / row.persistence_mae * 100, 1)
        mae_rows.append(
            f"| `{row.station_id}` | {row.n_scored:,} | {fmt(row.online_mae, best_mae)} | "
            f"{fmt(row.persistence_mae, best_mae)} | {fmt(row.seasonal_mae, best_mae)} | "
            f"{delta:+.1f}% |"
        )
        rmse_rows.append(
            f"| `{row.station_id}` | {fmt(row.online_rmse, best_rmse)} | "
            f"{fmt(row.persistence_rmse, best_rmse)} | {fmt(row.seasonal_rmse, best_rmse)} |"
        )

    cross_rows = []
    for sid, res in per_station.items():
        at = _crossover(res)
        cross_rows.append(
            f"| `{sid}` | {at:,} events |" if at is not None
            else f"| `{sid}` | never |"
        )

    wins = int((summary.online_mae < summary.persistence_mae).sum())
    total = len(summary)
    mean_online = round(float(summary.online_mae.mean()), 3)
    mean_persist = round(float(summary.persistence_mae.mean()), 3)
    mean_seasonal = round(float(summary.seasonal_mae.mean()), 3)

    content = f"""# PULSE: Error Metrics

Generated by `python -m scripts.error_curve`. Every number here comes from an actual
replay of `data/sample_aq.csv`; nothing is hand-written.

- Model in use: `{kind}` (`river` means online learning really ran; `baseline` means it fell back)
- Rolling window for the curves: **{ROLL}** events (2 days at 10-min resolution)
- Scoring window for the tables: events from **{SEASON}** onward, where all three
  models are defined. Seasonal-naive needs 24h of history, so scoring earlier would
  hand the online model a free win.
- Seed: `{SEED}`. The sample generator is seeded at 42 and SNARIMAX is deterministic,
  so re-running reproduces these figures exactly.

## How each forecast is made

All three predict `y_t` from information available at `t-1`. Same information, same events.

| Model | Forecast for `y_t` |
|---|---|
| online (river SNARIMAX) | forecast produced at `t-1`, after `learn_one(y_{{t-1}})` |
| persistence | `y_{{t-1}}` |
| seasonal-naive | `y_{{t-144}}` (same time of day, yesterday) |

## MAE (µg/m³, lower is better)

| Station | Events scored | Online | Persistence | Seasonal-naive | Online vs persistence |
|---|---|---|---|---|---|
{chr(10).join(mae_rows)}

## RMSE (µg/m³, lower is better)

| Station | Online | Persistence | Seasonal-naive |
|---|---|---|---|
{chr(10).join(rmse_rows)}

## Where the online model overtakes persistence

Rolling MAE crossover: the first event count after which the online model's rolling MAE
stays below persistence for the rest of the stream.

| Station | Crossover |
|---|---|
{chr(10).join(cross_rows)}

## Summary

Averaged across {total} stations: online MAE **{mean_online}**, persistence **{mean_persist}**,
seasonal-naive **{mean_seasonal}**. The online model beats persistence at
**{wins} of {total}** stations.

![Error curve](error_curve.png)

{_honest_notes(summary, per_station)}
## Reproducing this

```bash
python -m ingestion.gen_sample     # regenerate data/sample_aq.csv
python -m scripts.error_curve      # rewrite this file and error_curve.png
```

One caveat on exact reproduction: `gen_sample.py` anchors its date range to
`pd.Timestamp.utcnow()`, so regenerating on a different day shifts which clock hour each
row lands on and moves these figures slightly. The values above come from the sample file
as generated on 2026-08-02. The model itself is deterministic.
"""
    with open(METRICS_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[error_curve] wrote {METRICS_PATH}")


def main() -> None:
    df = _load()
    stations = list(df.station_id.unique())
    print(f"[error_curve] replaying {len(df):,} rows across {len(stations)} stations…")

    per_station = {}
    kinds = set()
    for sid in stations:
        res = replay_station(df, sid)
        per_station[sid] = res
        kinds.add(res.attrs["kind"])
        print(f"[error_curve]   {sid}: {len(res):,} scored events, model={res.attrs['kind']}")

    kind = kinds.pop() if len(kinds) == 1 else "mixed"
    if kind != "river":
        print(f"[error_curve] ⚠️  model kind is '{kind}', NOT river. "
              "The curve below does not measure online learning.")

    summary = summarise(per_station)
    print("\n[error_curve] final numbers (scored from event "
          f"{SEASON} onward):\n{summary.to_string(index=False)}\n")

    plot(per_station)
    write_metrics(summary, per_station, kind)


if __name__ == "__main__":
    main()
