"""Model registry — local JSON now, Supabase later (swap only this file).

Every promotion writes an immutable version record (metrics + reason + timestamp)
and updates `active.json`. This is the 'what happened after deploy' paper trail:
version history with metrics and promotion timestamps."""
from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any

from common.config import settings


class Registry:
    def __init__(self, base_path: str | None = None):
        self.base = base_path or settings.registry_path
        self.versions_dir = os.path.join(self.base, "versions")
        self.cards_dir = os.path.join(self.base, "cards")
        os.makedirs(self.versions_dir, exist_ok=True)
        os.makedirs(self.cards_dir, exist_ok=True)
        self.active_file = os.path.join(self.base, "active.json")

    # ── reads ──────────────────────────────────────────────
    def list_versions(self) -> list[dict[str, Any]]:
        out = []
        for fn in os.listdir(self.versions_dir):
            if fn.endswith(".json"):
                with open(os.path.join(self.versions_dir, fn), encoding="utf-8") as f:
                    out.append(json.load(f))
        return sorted(out, key=lambda v: v["created_at"])

    def get(self, version: str) -> dict[str, Any] | None:
        path = os.path.join(self.versions_dir, f"{version}.json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return json.load(f)

    def active(self) -> dict[str, Any] | None:
        if not os.path.exists(self.active_file):
            return None
        with open(self.active_file, encoding="utf-8") as f:
            return json.load(f)

    def next_version(self) -> str:
        return f"v{len(self.list_versions()) + 1}"

    # ── writes ─────────────────────────────────────────────
    def register(self, metrics: dict, kind: str, reason: str, promote: bool = True) -> dict:
        version = self.next_version()
        record = {
            "version": version,
            "kind": kind,
            "metrics": metrics,
            "reason": reason,
            "created_at": datetime.now(UTC).isoformat(),
            "promoted": promote,
        }
        with open(os.path.join(self.versions_dir, f"{version}.json"), "w", encoding="utf-8") as f:
            json.dump(record, f, indent=2)
        if promote:
            with open(self.active_file, "w", encoding="utf-8") as f:
                json.dump(record, f, indent=2)
        return record

    def save_card(self, version: str, markdown: str) -> str:
        path = os.path.join(self.cards_dir, f"{version}.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(markdown)
        return path

    def get_card(self, version: str) -> str | None:
        path = os.path.join(self.cards_dir, f"{version}.md")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as f:
            return f.read()
