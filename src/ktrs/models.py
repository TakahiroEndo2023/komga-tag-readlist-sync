from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SyncResult:
    tag: str
    readlist_id: str | None
    book_count: int
    added_count: int
    removed_count: int
    result: str
    detail: str = ""
    dry_run: bool = False
