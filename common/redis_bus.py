"""Thin wrapper over Redis Streams — the only infra dependency of the loop.

Every message is stored as a single ``data`` field holding a JSON string, so any
pydantic model can flow through unchanged. Sync helpers power the ingestion / ml /
agent workers; the async tail powers the API's WebSocket fan-out."""
from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterable

import redis
import redis.asyncio as aredis
from pydantic import BaseModel

from common.config import settings

# Keep streams from growing unbounded in a long demo (approximate trim).
_MAXLEN = 5000


# ── Sync side (workers) ────────────────────────────────────────────────
def get_client() -> redis.Redis:
    return redis.Redis.from_url(settings.redis_url, decode_responses=True)


def publish(client: redis.Redis, stream: str, payload: BaseModel | dict) -> str:
    data = payload.model_dump() if isinstance(payload, BaseModel) else payload
    return client.xadd(stream, {"data": json.dumps(data)}, maxlen=_MAXLEN, approximate=True)


class StreamReader:
    """Blocking multi-stream reader that remembers the last id it saw.

    Starts from ``$`` (new messages only) unless ``from_start`` is set."""

    def __init__(self, client: redis.Redis, streams: Iterable[str], from_start: bool = False):
        self.client = client
        start = "0" if from_start else "$"
        self.offsets = {s: start for s in streams}

    def read(self, block_ms: int = 5000, count: int = 100):
        """Yield (stream, id, dict) tuples for each new message."""
        resp = self.client.xread(self.offsets, block=block_ms, count=count)
        for stream, messages in resp or []:
            for msg_id, fields in messages:
                self.offsets[stream] = msg_id
                yield stream, msg_id, json.loads(fields["data"])


# ── Async side (API WebSocket fan-out) ─────────────────────────────────
def get_async_client() -> aredis.Redis:
    return aredis.Redis.from_url(settings.redis_url, decode_responses=True)


async def tail(client: aredis.Redis, streams: list[str], block_ms: int = 5000) -> AsyncIterator[tuple[str, dict]]:
    """Async generator that yields (stream, dict) for every new message."""
    offsets = {s: "$" for s in streams}
    while True:
        resp = await client.xread(offsets, block=block_ms, count=100)
        for stream, messages in resp or []:
            for msg_id, fields in messages:
                offsets[stream] = msg_id
                yield stream, json.loads(fields["data"])


async def recent(client: aredis.Redis, stream: str, count: int = 50) -> list[dict]:
    """Most recent ``count`` messages from a stream (oldest→newest), for REST history."""
    resp = await client.xrevrange(stream, count=count)
    out = [json.loads(fields["data"]) for _id, fields in resp]
    out.reverse()
    return out
