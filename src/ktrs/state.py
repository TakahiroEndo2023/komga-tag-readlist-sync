from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from .atomic import atomic_write_json, reject_symlink
from .errors import StateError

SCHEMA_VERSION = 2


def empty_state() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "managed_readlists": {},
        "last_sync_at": None,
        "sync_results": {},
    }


def validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or value.get("schema_version") not in {1, SCHEMA_VERSION}:
        raise StateError("unsupported or invalid state schema")
    managed = value.get("managed_readlists")
    results = value.get("sync_results")
    if not isinstance(managed, dict) or not isinstance(results, dict):
        raise StateError("state managed_readlists and sync_results must be objects")
    for tag, record in managed.items():
        if not isinstance(tag, str) or not isinstance(record, dict):
            raise StateError("invalid managed readlist record")
        if record.get("tag") != tag:
            raise StateError(f"managed record tag mismatch: {tag}")
        if record.get("status") not in {"creation_pending", "managed"}:
            raise StateError(f"invalid managed status: {tag}")
        for field in ("name", "ownership_token", "summary"):
            if not isinstance(record.get(field), str) or not record[field]:
                raise StateError(f"managed record missing {field}: {tag}")
        readlist_id = record.get("readlist_id")
        if record["status"] == "managed" and (not isinstance(readlist_id, str) or not readlist_id):
            raise StateError(f"managed record missing readlist_id: {tag}")
        if readlist_id is not None and (not isinstance(readlist_id, str) or not readlist_id):
            raise StateError(f"invalid readlist_id: {tag}")
        mode = record.get("ownership_mode")
        if mode is not None and mode != "local_hmac_v1":
            raise StateError(f"invalid ownership mode: {tag}")
        if mode == "local_hmac_v1":
            for field in ("created_date", "ownership_proof"):
                if not isinstance(record.get(field), str) or not record[field]:
                    raise StateError(f"managed record missing {field}: {tag}")
    return deepcopy(value)


class StateStore:
    def __init__(self, path: Path):
        self.path = path.expanduser()

    def load(self) -> dict[str, Any]:
        reject_symlink(self.path)
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return empty_state()
        except OSError as exc:
            raise StateError(f"cannot read state file {self.path}: {exc}") from exc
        try:
            return validate_state(json.loads(raw))
        except json.JSONDecodeError as exc:
            raise StateError(f"invalid state JSON: {exc}") from exc

    def save(self, state: dict[str, Any]) -> None:
        atomic_write_json(self.path, validate_state(state))
