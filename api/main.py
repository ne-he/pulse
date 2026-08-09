"""FastAPI app — REST snapshots/history + WebSocket live feed + demo control.

A background task tails the Redis streams, keeps an in-memory snapshot of the
latest state per station, and broadcasts every new frame to WebSocket clients.
The dashboard gets an instant snapshot on connect, then live deltas."""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from api.ws import ConnectionManager
from common.config import Streams, settings
from common.redis_bus import get_async_client, get_client, publish, recent, tail
from common.schemas import STATIONS, ControlCommand
from ml.registry.registry import Registry

manager = ConnectionManager()
registry = Registry()

# ── in-memory snapshot (rebuilt live from the streams) ──────────────────
state = {
    "latest_obs": {},      # sid -> AQEvent dict
    "latest_pred": {},     # sid -> Prediction dict
    "alerts": deque(maxlen=100),
    "incidents": deque(maxlen=100),
    "drift": None,         # last drift report
}

# map stream name -> WebSocket envelope "type"
_TYPE = {
    Streams.EVENTS: "observation",
    Streams.PREDICTIONS: "prediction",
    Streams.ALERTS: "alert",
    Streams.INCIDENTS: "incident",
}


async def _consume_streams():
    client = get_async_client()
    streams = [Streams.EVENTS, Streams.PREDICTIONS, Streams.ALERTS, Streams.INCIDENTS]
    async for stream, data in tail(client, streams):
        kind = _TYPE.get(stream, "unknown")
        sid = data.get("station_id")
        if stream == Streams.EVENTS and sid:
            state["latest_obs"][sid] = data
        elif stream == Streams.PREDICTIONS and sid:
            state["latest_pred"][sid] = data
        elif stream == Streams.ALERTS:
            state["alerts"].appendleft(data)
            if data.get("type") == "drift":
                state["drift"] = data.get("context", {}).get("drift")
        elif stream == Streams.INCIDENTS:
            state["incidents"].appendleft(data)
        await manager.broadcast({"type": kind, "data": data})


async def _hydrate() -> None:
    """Rebuild the snapshot from what is ALREADY on the bus before tailing it.

    `_consume_streams` reads from `$`, so without this the API only knows what
    happened after it personally started. Two things that breaks: an API restart
    shows a blank dashboard until fresh frames trickle in, and a demo pre-roll
    that warms the model before the server comes up is invisible. Bounded reads,
    so a long-running stream costs the same at startup as a short one."""
    try:
        client = get_async_client()
        for row in await recent(client, Streams.EVENTS, count=500):
            if row.get("station_id"):
                state["latest_obs"][row["station_id"]] = row
        for row in await recent(client, Streams.PREDICTIONS, count=500):
            if row.get("station_id"):
                state["latest_pred"][row["station_id"]] = row
        for row in await recent(client, Streams.ALERTS, count=50):
            state["alerts"].appendleft(row)
            if row.get("type") == "drift":
                state["drift"] = row.get("context", {}).get("drift")
        for row in await recent(client, Streams.INCIDENTS, count=50):
            state["incidents"].appendleft(row)
    except Exception as exc:  # noqa: BLE001 — never let a cold bus block startup
        print(f"[api] snapshot hydrate skipped ({exc})")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await _hydrate()
    task = asyncio.create_task(_consume_streams())
    yield
    task.cancel()


app = FastAPI(title="PULSE API", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


# ── REST ────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    try:
        get_client().ping()
        redis_ok = True
    except Exception:  # noqa: BLE001
        redis_ok = False
    return {"status": "ok", "redis": redis_ok, "ws_clients": len(manager.active)}


@app.get("/stations")
def stations():
    return list(STATIONS.values())


@app.get("/stations/{station_id}/latest")
def station_latest(station_id: str):
    return {
        "station": STATIONS.get(station_id),
        "observation": state["latest_obs"].get(station_id),
        "prediction": state["latest_pred"].get(station_id),
    }


@app.get("/predictions/latest")
def predictions_latest():
    return list(state["latest_pred"].values())


@app.get("/predictions/{station_id}/history")
async def predictions_history(station_id: str, limit: int = 120):
    client = get_async_client()
    rows = await recent(client, Streams.PREDICTIONS, count=1000)
    rows = [r for r in rows if r.get("station_id") == station_id]
    return rows[-limit:]


@app.get("/observations/{station_id}/history")
async def observations_history(station_id: str, limit: int = 120):
    client = get_async_client()
    rows = await recent(client, Streams.EVENTS, count=1000)
    rows = [r for r in rows if r.get("station_id") == station_id]
    return rows[-limit:]


@app.get("/incidents")
def incidents(limit: int = 30):
    return list(state["incidents"])[:limit]


@app.get("/alerts")
def alerts(limit: int = 30):
    return list(state["alerts"])[:limit]


@app.get("/model/active")
def model_active():
    return registry.active()


@app.get("/model/versions")
def model_versions():
    return registry.list_versions()


@app.get("/model/metrics")
def model_metrics():
    """Live per-station rolling error (drives the Model health panel)."""
    out = []
    for sid, pred in state["latest_pred"].items():
        out.append({
            "station_id": sid,
            "model_version": pred.get("model_version"),
            "mae_rolling": pred.get("mae_rolling"),
            "rmse_rolling": pred.get("rmse_rolling"),
            "n_seen": pred.get("n_seen"),
        })
    return out


@app.get("/modelcard/{version}")
def model_card(version: str):
    card = registry.get_card(version)
    return {"version": version, "markdown": card}


@app.get("/drift/status")
def drift_status():
    return {"latest": state["drift"], "threshold": settings.drift_threshold}


@app.post("/control")
def control(cmd: ControlCommand):
    """Demo control: play / pause / set_speed / seek / trigger_spike.
    Published to the replay engine via the control stream."""
    publish(get_client(), Streams.CONTROL, cmd)
    return {"ok": True, "sent": cmd.model_dump()}


# ── WebSocket ─────────────────────────────────────────────────────────────
@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await manager.connect(ws)
    # send an immediate snapshot so the dashboard paints without waiting
    await manager.send_personal(ws, {
        "type": "snapshot",
        "data": {
            "stations": list(STATIONS.values()),
            "predictions": list(state["latest_pred"].values()),
            "observations": list(state["latest_obs"].values()),
            "incidents": list(state["incidents"])[:20],
            "alerts": list(state["alerts"])[:20],
            "model": registry.active(),
            "drift": state["drift"],
        },
    })
    try:
        while True:
            await ws.receive_text()  # keepalive / ignore client messages
    except WebSocketDisconnect:
        await manager.disconnect(ws)


# ── Dashboard (mounted LAST so it never shadows an API route) ─────────────
# Serving the UI from the same origin as the API is what lets the demo be one
# URL instead of two: the dashboard's default backend is its own origin, so
# there is no CORS hop, no second server, and nothing to reconfigure.
_DASHBOARD = Path(__file__).resolve().parent.parent / "Frontend_pulse"
if _DASHBOARD.is_dir():
    app.mount("/", StaticFiles(directory=str(_DASHBOARD), html=True), name="dashboard")
