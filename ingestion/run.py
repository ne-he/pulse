"""Ingestion entrypoint — picks replay (demo) or live based on INGEST_MODE."""
from __future__ import annotations

from common.config import settings


def main() -> None:
    if settings.ingest_mode == "live":
        from ingestion import producer
        producer.run()
    else:
        from ingestion import replay
        replay.run()


if __name__ == "__main__":
    main()
