"""Replay engine — THE demo weapon (frontend feature #9).

Streams a historical/synthetic CSV onto the bus as if it were happening live,
time-compressed by REPLAY_SPEED. Listens on the control stream so the dashboard
can pause/play, change speed, seek, and — crucially — TRIGGER A SPIKE on demand,
so the spike→anomaly→agent-card moment is reproducible whenever a recruiter looks.
"""
from __future__ import annotations

import json
import os
import time

import pandas as pd

from common.config import Streams, settings
from common.redis_bus import get_client, publish
from common.schemas import STATIONS, AQEvent
from ingestion import gen_sample

SAMPLE_PATH = os.environ.get("SAMPLE_PATH", "./data/sample_aq.csv")


def _ensure_sample() -> pd.DataFrame:
    if not os.path.exists(SAMPLE_PATH):
        print(f"[replay] no sample at {SAMPLE_PATH}; generating one…")
        gen_sample.main()
    return pd.read_csv(SAMPLE_PATH)


def _event_from_row(row: pd.Series, source: str = "replay") -> AQEvent:
    return AQEvent(
        station_id=row["station_id"],
        station_name=row["station_name"],
        ts=str(row["ts"]),
        pm25=float(row["pm25"]),
        pm10=float(row.get("pm10")) if pd.notna(row.get("pm10")) else None,
        no2=float(row.get("no2")) if pd.notna(row.get("no2")) else None,
        o3=float(row.get("o3")) if pd.notna(row.get("o3")) else None,
        temp=float(row.get("temp")) if pd.notna(row.get("temp")) else None,
        humidity=float(row.get("humidity")) if pd.notna(row.get("humidity")) else None,
        wind_speed=float(row.get("wind_speed")) if pd.notna(row.get("wind_speed")) else None,
        source=source,
    )


def run() -> None:
    client = get_client()
    df = _ensure_sample()
    df = df.sort_values("ts").reset_index(drop=True)
    timestamps = sorted(df["ts"].unique())
    frames = {ts: df[df["ts"] == ts] for ts in timestamps}

    speed = settings.replay_speed
    paused = False
    i = 0
    control_offset = "$"  # only react to commands issued after we start

    print(f"[replay] streaming {len(timestamps)} frames @ speed={speed} (loops forever)")
    while True:
        # ── drain control commands (non-blocking) ───────────────────────
        resp = client.xread({Streams.CONTROL: control_offset}, count=20)  # no block = immediate
        for _stream, messages in resp or []:
            for msg_id, fields in messages:
                control_offset = msg_id
                cmd = json.loads(fields["data"])
                action = cmd.get("action")
                if action == "pause":
                    paused = True
                elif action == "play":
                    paused = False
                elif action == "set_speed" and cmd.get("value"):
                    speed = max(1.0, float(cmd["value"]))
                elif action == "seek" and cmd.get("value") is not None:
                    i = int(max(0, min(len(timestamps) - 1, cmd["value"])))
                elif action == "trigger_spike":
                    _inject_spike(client, cmd.get("station_id"), cmd.get("value"))

        if paused:
            time.sleep(0.2)
            continue

        # ── publish the current frame (all stations at this timestamp) ───
        ts = timestamps[i]
        for _, row in frames[ts].iterrows():
            publish(client, Streams.EVENTS, _event_from_row(row))

        # ── pace to the next frame, time-compressed, capped so demo stays snappy ─
        nxt = (i + 1) % len(timestamps)
        if nxt > 0:
            gap_sec = (pd.Timestamp(timestamps[nxt]) - pd.Timestamp(ts)).total_seconds()
            time.sleep(min(5.0, max(0.05, gap_sec / speed)))
        else:
            print("[replay] reached end, looping back to start")
        i = nxt


def _inject_spike(client, station_id: str | None, magnitude: float | None) -> None:
    """Force an on-demand pollution spike so the agent card fires reproducibly."""
    sid = station_id if station_id in STATIONS else next(iter(STATIONS))
    meta = STATIONS[sid]
    bump = float(magnitude) if magnitude else 120.0
    ev = AQEvent(
        station_id=sid, station_name=meta["name"],
        pm25=bump, pm10=bump * 1.5, no2=bump * 0.6, o3=10,
        temp=30, humidity=80, wind_speed=0.5, source="replay",
    )
    publish(client, Streams.EVENTS, ev)
    print(f"[replay] 💥 injected spike pm2.5={bump} at {sid}")


if __name__ == "__main__":
    run()
