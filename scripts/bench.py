"""REPLAY-mode benchmark: the three numbers the README quotes, measured.

Runs the real `Engine` (forecast → learn_one → anomaly → drift → retrain) over the
sample dataset on an in-memory bus, and reports:

  1. throughput          events per second
  2. latency             event → prediction, per event, p50/p95/p99
  3. retrain rate        retrains per hour of replayed data, and per wall-clock hour

Redis is deliberately out of the loop. These measure the ML pipeline, not the network,
which is the honest thing to quote for "how fast does the brain run". The numbers are
therefore an upper bound on end-to-end throughput; Redis Streams add a hop on both sides.

    python -m scripts.bench
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import statistics
import tempfile
import time

import pandas as pd

from common.config import settings

SAMPLE_PATH = os.environ.get("SAMPLE_PATH", "./data/sample_aq.csv")
MAX_EVENTS = int(os.environ.get("BENCH_MAX_EVENTS", "0"))  # 0 = the whole file


def _load() -> pd.DataFrame:
    if not os.path.exists(SAMPLE_PATH):
        print(f"[bench] no sample at {SAMPLE_PATH}; generating one…")
        from ingestion import gen_sample
        gen_sample.main()
    return pd.read_csv(SAMPLE_PATH).sort_values("ts").reset_index(drop=True)


def _percentile(values: list[float], pct: float) -> float:
    ordered = sorted(values)
    k = min(len(ordered) - 1, max(0, int(round(pct / 100 * (len(ordered) - 1)))))
    return ordered[k]


def main() -> None:
    df = _load()
    if MAX_EVENTS:
        df = df.head(MAX_EVENTS)

    events = df.to_dict("records")
    span_hours = (
        pd.Timestamp(df.ts.max()) - pd.Timestamp(df.ts.min())
    ).total_seconds() / 3600.0

    # isolate the registry so a benchmark never pollutes ./data/registry
    with tempfile.TemporaryDirectory() as tmp:
        settings.registry_path = tmp
        from common.redis_bus import InMemoryClient  # imported after settings is set
        from ml.online.consumer import Engine
        from ml.registry.registry import Registry

        client = InMemoryClient()
        engine = Engine()
        registry = Registry(base_path=tmp)
        baseline_versions = len(registry.list_versions())  # the bootstrap version

        print(f"[bench] replaying {len(events):,} events "
              f"({df.station_id.nunique()} stations, {span_hours:.1f}h of data)…")

        latencies: list[float] = []
        # Console logging is a demo feature, not pipeline work. Capture it so the
        # timings measure the model, not the terminal.
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink):
            wall_start = time.perf_counter()
            for event in events:
                t0 = time.perf_counter()
                engine.process(client, event)
                latencies.append((time.perf_counter() - t0) * 1000.0)
            wall = time.perf_counter() - wall_start

        versions = registry.list_versions()
        retrains = len(versions) - baseline_versions
        predictions = len(client.messages("aq.predictions"))
        anomalies = sum(1 for a in client.messages("aq.alerts") if a["type"] == "anomaly")

    throughput = len(events) / wall
    replay_wall_hours = span_hours / settings.replay_speed  # at REPLAY_SPEED sim-sec/real-sec

    results = {
        "events": len(events),
        "stations": int(df.station_id.nunique()),
        "data_span_hours": round(span_hours, 2),
        "wall_seconds": round(wall, 3),
        "events_per_second": round(throughput, 1),
        "latency_ms": {
            "mean": round(statistics.fmean(latencies), 3),
            "p50": round(_percentile(latencies, 50), 3),
            "p95": round(_percentile(latencies, 95), 3),
            "p99": round(_percentile(latencies, 99), 3),
            "max": round(max(latencies), 3),
        },
        "predictions_emitted": predictions,
        "anomalies_flagged": anomalies,
        "retrains": retrains,
        "retrains_per_hour_of_data": round(retrains / span_hours, 4) if span_hours else None,
        "retrains_per_wall_hour_at_replay_speed": (
            round(retrains / replay_wall_hours, 2) if replay_wall_hours else None
        ),
        "replay_speed": settings.replay_speed,
        "drift_window": settings.drift_window,
    }

    print(f"""
[bench] ── results ──────────────────────────────────────────────
  events processed          {results['events']:,} across {results['stations']} stations
  wall time                 {results['wall_seconds']}s
  THROUGHPUT                {results['events_per_second']:,} events/sec
  LATENCY event→prediction  p50 {results['latency_ms']['p50']} ms · \
p95 {results['latency_ms']['p95']} ms · p99 {results['latency_ms']['p99']} ms
  predictions emitted       {results['predictions_emitted']:,}
  anomalies flagged         {results['anomalies_flagged']:,}
  RETRAINS                  {results['retrains']} over {results['data_span_hours']}h of data
                            = {results['retrains_per_hour_of_data']} per hour of replayed data
                            = {results['retrains_per_wall_hour_at_replay_speed']} per wall-clock \
hour at REPLAY_SPEED={results['replay_speed']:.0f}
[bench] ─────────────────────────────────────────────────────────""")

    out = os.path.join("docs", "bench.json")
    os.makedirs("docs", exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"[bench] wrote {out}")


if __name__ == "__main__":
    main()
