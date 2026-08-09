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

# How long an injected spike keeps shaping the feed, in frames, and how fast it
# fades. A single impulse reads as a glitch rather than an event: it raises one
# anomaly, then the very next real frame falls back to baseline and raises a
# second one, so the demo shows a spike card instantly buried under a drop card.
#
# The fade must also be gentle enough that IT does not alarm, or the demo invents
# fake incidents about its own synthetic decay. Measured on jakbar (onset 170
# µg/m³, alerts raised over the whole episode):
#
#     0.72 / 8f  -> 3 alerts   170 122  88  63  46  33  24
#     0.80 / 12f -> 3 alerts   170 136 109  87  70  56  45
#     0.88 / 18f -> 1 alert    170 150 132 116 102  90  79   <- onset only
#     0.94 / 34f -> 1 alert    but drags the episode past a minute
#
# 0.88 over 18 frames is the cheapest setting that alarms exactly once, and at
# the default replay speed it plays out over about 18 seconds, which is roughly
# how long it takes to talk through what just happened.
SPIKE_FRAMES = 18
SPIKE_DECAY = 0.88


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


def control_start_offset(client) -> str:
    """Where to start reading the control stream from.

    NOT "$". `XREAD` with "$" means "ids greater than the stream's max AT CALL
    TIME", and this loop polls without BLOCK, so there is no window in which a
    newer id can appear: the call returns empty every time and the offset never
    advances. Every control command was therefore dropped, which silently
    disabled play/pause, speed, seek and the [SPIKE] button, the one control the
    demo script actually depends on. Fixed 2026-08-09, pinned by
    tests/test_replay_control.py.

    Seeding from the last existing id keeps the original intent (ignore commands
    issued before this worker started) while remaining readable.
    """
    last = client.xrevrange(Streams.CONTROL, count=1)
    return last[0][0] if last else "0-0"


def run(start_index: int = 0) -> None:
    """Stream frames forever from `start_index`.

    `start_index` exists so a warm-up pass can push history onto the bus and the
    live stream can then pick up exactly where it stopped. Restarting at frame 0
    would replay events the model has already learned from and make the dashboard
    chart jump backwards in the middle of a demo."""
    client = get_client()
    df = _ensure_sample()
    df = df.sort_values("ts").reset_index(drop=True)
    timestamps = sorted(df["ts"].unique())
    frames = {ts: df[df["ts"] == ts] for ts in timestamps}

    speed = settings.replay_speed
    paused = False
    i = start_index % len(timestamps)
    control_offset = control_start_offset(client)
    spike_station, spike_bump, spike_left = None, 0.0, 0

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
                    sid = cmd.get("station_id")
                    spike_station = sid if sid in STATIONS else next(iter(STATIONS))
                    spike_bump = float(cmd.get("value") or 120.0)
                    spike_left = SPIKE_FRAMES
                    _inject_spike(client, spike_station, spike_bump)

        if paused:
            time.sleep(0.2)
            continue

        # ── publish the current frame (all stations at this timestamp) ───
        ts = timestamps[i]
        for _, row in frames[ts].iterrows():
            event = _event_from_row(row)
            if spike_left > 0 and event.station_id == spike_station:
                event = _apply_spike(event, spike_bump)
            publish(client, Streams.EVENTS, event)
        if spike_left > 0:
            spike_left -= 1
            spike_bump *= SPIKE_DECAY

        # ── pace to the next frame, time-compressed, capped so demo stays snappy ─
        nxt = (i + 1) % len(timestamps)
        if nxt > 0:
            gap_sec = (pd.Timestamp(timestamps[nxt]) - pd.Timestamp(ts)).total_seconds()
            time.sleep(min(5.0, max(0.05, gap_sec / speed)))
        else:
            print("[replay] reached end, looping back to start")
        i = nxt


def _apply_spike(event: AQEvent, bump: float) -> AQEvent:
    """Overlay a decaying pollution episode on a real frame.

    `max` rather than replace, so the episode fades out on its own: once the
    decaying bump falls below the station's real reading it stops having any
    effect and the feed is back to the recorded data with no visible seam."""
    pm = max(event.pm25, bump)
    return event.model_copy(update={
        "pm25": round(pm, 2),
        "pm10": round(pm * 1.5, 2),
        "no2": round(pm * 0.6, 2),
        # Episodes come with stagnant air. This is also what the incident card
        # reads to justify "weak wind limiting dispersion" as the likely cause.
        "wind_speed": min(event.wind_speed if event.wind_speed is not None else 1.0, 0.6),
    })


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
