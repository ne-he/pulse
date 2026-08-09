"""One command, one process, whole system: `python -m deploy.demo`.

Built for the case where PULSE is not hosted anywhere and gets switched on only
when somebody is about to watch it. That has two consequences the normal
docker-compose path does not handle:

  1. NOTHING may need installing at demo time. No Redis server, no Docker
     daemon, no second terminal for the dashboard. The bus runs in-process
     (`REDIS_URL=memory://`, see common/redis_bus.py) and the dashboard is
     served by the API itself, so the whole thing is one process on one port.

  2. IT MAY NOT LOOK EMPTY. A cold start has no history, no learned model, no
     incidents and no model versions, so the first minute of a live demo is a
     blank dashboard while somebody watches. `--warm` frames are pushed through
     the real Engine at full speed BEFORE the browser opens, so the dashboard
     paints with a populated chart, a model that has seen thousands of events,
     real error metrics, promoted versions and an incident feed. The live
     stream then continues from exactly where the warm-up stopped.

If a real Redis is reachable it is used instead, so the same command also works
against `docker compose up redis`.

    python -m deploy.demo                  # warm up, open browser, stream
    python -m deploy.demo --warm 0         # cold start, nothing pre-loaded
    python -m deploy.demo --no-browser     # for a second screen / recording
    python -m deploy.demo --fresh          # wipe the model registry first
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import threading
import time
import webbrowser

BANNER = r"""
   ___  __  ____   _____ ___
  / _ \/ / / / /  / ___// _ \    Jakarta air quality, live
 / ___/ /_/ / /___\__ \/  __/    online learning -> drift -> retrain -> agent
/_/   \____/_____/____/\___/
"""


def _pick_bus(force_memory: bool) -> str:
    """Use a real Redis if one is actually reachable, otherwise run in-process."""
    if force_memory:
        return "memory://"
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    if url.startswith("memory://"):
        return url
    try:
        import redis

        redis.Redis.from_url(url, socket_connect_timeout=0.7).ping()
        print(f"[demo] found a live Redis at {url}, using it")
        return url
    except Exception:  # noqa: BLE001
        print("[demo] no Redis reachable, running the bus in-process (memory://)")
        return "memory://"


def warm_up(frames: int):
    """Replay `frames` frames through the real Engine as fast as it will go.

    Returns (resume_index, engine). The Engine is handed to the consumer thread
    so the live model CONTINUES from what it just learned. Building a second one
    would reset n_seen to 0 and recompute MAE from scratch underneath a chart
    that already shows thousands of events, which is worse than not warming up.

    This is the same Engine, detector and registry the workers use: a
    fast-forward, not a fixture, so nothing on screen is faked.
    """
    from agent.incident import build_incident
    from common.config import Streams
    from common.redis_bus import get_client, publish
    from ingestion.replay import SAMPLE_PATH, _ensure_sample, _event_from_row
    from ml.online.consumer import Engine

    client = get_client()
    df = _ensure_sample().sort_values("ts").reset_index(drop=True)
    timestamps = sorted(df["ts"].unique())
    frames = min(frames, len(timestamps))
    print(f"[demo] warming up on {frames} frames from {SAMPLE_PATH} "
          f"({frames * df['station_id'].nunique()} events)…")

    engine = Engine()
    t0 = time.time()
    subset = df[df["ts"].isin(set(timestamps[:frames]))]
    for _, row in subset.iterrows():
        event = _event_from_row(row)
        publish(client, Streams.EVENTS, event)
        try:
            engine.process(client, event.model_dump())
        except Exception as exc:  # noqa: BLE001 — a bad row must not abort the demo
            print(f"[demo] warm-up skipped an event ({exc})")

    # Alerts only become incident cards when the agent sees them, and the agent
    # is not running yet. Narrate the most recent few so the feed is not empty.
    for _id, fields in reversed(client.xrevrange(Streams.ALERTS, count=6)):
        try:
            publish(client, Streams.INCIDENTS, build_incident(json.loads(fields["data"])))
        except Exception as exc:  # noqa: BLE001
            print(f"[demo] warm-up card skipped ({exc})")

    versions = len(engine.registry.list_versions())
    print(f"[demo] warm-up done in {time.time() - t0:.1f}s: "
          f"{client.xlen(Streams.PREDICTIONS)} predictions, "
          f"{client.xlen(Streams.ALERTS)} alerts, "
          f"{client.xlen(Streams.INCIDENTS)} incident cards, "
          f"{versions} model version(s), "
          f"{sum(m.n for m in engine.models.values())} events learned")
    return frames, engine


def start_workers(resume_from: int, engine=None) -> None:
    """Ingestion, ML and agent as daemon threads beside the API in this process."""
    from agent import incident
    from ingestion import replay
    from ml.online import consumer

    workers = [
        ("ingestion", lambda: replay.run(start_index=resume_from)),
        ("ml", lambda: consumer.run(engine)),
        ("agent", incident.run),
    ]
    for name, target in workers:
        def guarded(name=name, target=target):
            try:
                target()
            except Exception as exc:  # noqa: BLE001
                print(f"[demo] worker '{name}' stopped: {exc}")

        threading.Thread(target=guarded, name=name, daemon=True).start()
    print(f"[demo] workers running: {', '.join(n for n, _ in workers)}")


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m deploy.demo")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8000)))
    # 600 was measured, not guessed. Frames -> (alerts, model versions, cost):
    # 300 -> (0, 1) 7s | 400 -> (1, 2) 14s | 600 -> (5, 4) 17s | 1000 -> (15, 9) 27s.
    # Below 600 the incident feed opens empty and no drift-triggered retrain has
    # happened yet, which removes the two things a cold start cannot show.
    ap.add_argument("--warm", type=int, default=600,
                    help="frames to pre-load before opening the browser (0 = cold)")
    ap.add_argument("--speed", type=float, default=None,
                    help="replay speed, sim-seconds per real second (default 600)")
    ap.add_argument("--memory", action="store_true",
                    help="force the in-process bus even if Redis is reachable")
    ap.add_argument("--fresh", action="store_true", help="wipe the model registry first")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    # Settings are read at import time, so every env var must be set BEFORE the
    # first `common.config` import. Nothing above this line may import the app.
    os.environ["REDIS_URL"] = _pick_bus(args.memory)
    os.environ.setdefault("INGEST_MODE", "replay")
    if args.speed is not None:
        os.environ["REPLAY_SPEED"] = str(args.speed)

    from common.config import settings

    if args.fresh and os.path.isdir(settings.registry_path):
        shutil.rmtree(settings.registry_path, ignore_errors=True)
        print(f"[demo] registry wiped ({settings.registry_path})")

    print(BANNER)
    resume_from, engine = warm_up(args.warm) if args.warm > 0 else (0, None)
    start_workers(resume_from, engine)

    url = f"http://localhost:{args.port}"
    if not args.no_browser:
        # The server is not listening yet; open the tab a moment later so the
        # first request does not land on a closed port.
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()

    print(f"\n  dashboard  {url}")
    print(f"  api docs   {url}/docs")
    print(f"  health     {url}/health")
    print("\n  Press SPIKE on the dashboard to force an incident. Ctrl+C to stop.\n")

    import uvicorn

    from api.main import app

    try:
        uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")
    except KeyboardInterrupt:
        pass
    print("\n[demo] stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
