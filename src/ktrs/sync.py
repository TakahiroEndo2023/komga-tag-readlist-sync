from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any

from .errors import ApiNotFoundError, KtrsError, SafetyError
from .komga import KomgaClient
from .models import SyncResult
from .ownership import (
    OWNERSHIP_MODE,
    format_summary,
    ownership_proof,
    summary_minute,
    timestamp_minute_jst,
    verify_local_proof,
)
from .state import StateStore


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def readlist_name(tag: str) -> str:
    return tag


def legacy_ownership_summary(tag: str, token: str) -> str:
    return f"Managed by KTRS. tag={tag}; ownership={token}"


# Compatibility import for older tests and callers. New records never use it.
ownership_summary = legacy_ownership_summary


class SyncEngine:
    def __init__(self, client: KomgaClient, store: StateStore, *, dry_run: bool = False, clock=lambda: datetime.now(timezone.utc)):
        self.client = client
        self.store = store
        self.dry_run = dry_run
        self.clock = clock

    @staticmethod
    def _assert_owned(readlist: dict[str, Any], record: dict[str, Any]) -> None:
        if record.get("ownership_mode") != OWNERSHIP_MODE:
            raise SafetyError("managed readlist uses legacy visible ownership; run the explicit summary migration")
        verify_local_proof(record)
        if (
            readlist["name"] != record["name"]
            or readlist["summary"] != record["summary"]
            or readlist["ordered"] is not True
            or readlist["createdDate"] != record["created_date"]
        ):
            raise SafetyError("managed readlist identity or ownership metadata mismatch")

    def _resolve_pending(self, record: dict[str, Any]) -> dict[str, Any]:
        readlist_id = record.get("readlist_id")
        if readlist_id:
            try:
                candidate = self.client.get_readlist(readlist_id)
            except ApiNotFoundError as exc:
                raise SafetyError("creation_pending readlist ID no longer exists; human review required") from exc
            self._assert_owned(candidate, record)
            return candidate
        raise SafetyError(
            "creation_pending has no confirmed Readlist ID; refusing to create or claim by visible fields"
        )

    def _verify_after(self, readlist_id: str, record: dict[str, Any], desired: list[str]) -> dict[str, Any]:
        after = self.client.get_readlist(readlist_id)
        self._assert_owned(after, record)
        if after["bookIds"] != desired:
            raise SafetyError("Komga readlist GET verification failed")
        return after

    def sync_tag(self, state: dict[str, Any], tag: str) -> SyncResult:
        desired = self.client.search_book_ids_by_exact_tag(tag)
        if not desired:
            raise SafetyError("exact tag search succeeded with 0 Books; empty create/update requires human confirmation")
        records = state["managed_readlists"]
        record = records.get(tag)
        if record is None:
            name = readlist_name(tag)
            collisions = [item for item in self.client.list_readlists() if item["name"] == name]
            if collisions:
                raise SafetyError("an unmanaged or conflicting readlist already has the KTRS name")
            if self.dry_run:
                return SyncResult(tag, None, len(desired), len(desired), 0, "created", "planned; no mutation sent", True)
            token = secrets.token_urlsafe(24)
            changed_at = self.clock()
            record = {
                "status": "creation_pending",
                "tag": tag,
                "name": name,
                "summary": format_summary(tag, changed_at),
                "ownership_token": token,
                "readlist_id": None,
            }
            records[tag] = record
            self.store.save(state)
            created = self.client.create_readlist(name=name, summary=record["summary"], book_ids=desired)
            if created["name"] != name or created["summary"] != record["summary"] or created["ordered"] is not True:
                raise SafetyError("Komga create response failed ownership verification")
            record["readlist_id"] = created["id"]
            record["created_date"] = created["createdDate"]
            record["ownership_mode"] = OWNERSHIP_MODE
            record["ownership_proof"] = ownership_proof(token, created["id"], tag, created["createdDate"])
            self.store.save(state)
            after = self._verify_after(created["id"], record, desired)
            if summary_minute(record["summary"]) != timestamp_minute_jst(after["lastModifiedDate"]):
                raise SafetyError("Komga creation timestamp minute does not match the managed summary")
            record["status"] = "managed"
            self.store.save(state)
            return SyncResult(tag, created["id"], len(desired), len(desired), 0, "created")

        if record["status"] == "creation_pending":
            current = self._resolve_pending(record)
            self._assert_owned(current, record)
            current_ids = current["bookIds"]
            if current_ids != desired:
                if self.dry_run:
                    return self._difference_result(tag, current["id"], desired, current_ids, "updated", True, "planned pending recovery")
                changed_summary = format_summary(tag, self.clock())
                self.client.update_readlist(current["id"], book_ids=desired, summary=changed_summary)
                candidate_record = dict(record)
                candidate_record["summary"] = changed_summary
                after = self._verify_after(current["id"], candidate_record, desired)
                if summary_minute(changed_summary) != timestamp_minute_jst(after["lastModifiedDate"]):
                    raise SafetyError("Komga update timestamp minute does not match the managed summary")
                record["summary"] = changed_summary
            if not self.dry_run:
                record["readlist_id"] = current["id"]
                record["status"] = "managed"
                self.store.save(state)
            action = "unchanged" if current_ids == desired else "updated"
            return self._difference_result(tag, current["id"], desired, current_ids, action, self.dry_run, "creation_pending resolved")

        readlist_id = record["readlist_id"]
        try:
            current = self.client.get_readlist(readlist_id)
        except ApiNotFoundError as exc:
            raise SafetyError("managed readlist was deleted in Komga; automatic recreation is forbidden") from exc
        self._assert_owned(current, record)
        current_ids = current["bookIds"]
        if current_ids == desired:
            return SyncResult(tag, readlist_id, len(desired), 0, 0, "unchanged", dry_run=self.dry_run)
        result = self._difference_result(tag, readlist_id, desired, current_ids, "updated", self.dry_run, "planned; no mutation sent" if self.dry_run else "")
        if self.dry_run:
            return result
        changed_summary = format_summary(tag, self.clock())
        self.client.update_readlist(readlist_id, book_ids=desired, summary=changed_summary)
        candidate_record = dict(record)
        candidate_record["summary"] = changed_summary
        after = self._verify_after(readlist_id, candidate_record, desired)
        if summary_minute(changed_summary) != timestamp_minute_jst(after["lastModifiedDate"]):
            raise SafetyError("Komga update timestamp minute does not match the managed summary")
        record["summary"] = changed_summary
        self.store.save(state)
        return result

    def migrate_summaries(self, state: dict[str, Any], tags: list[str]) -> list[SyncResult]:
        """Explicitly migrate verified legacy token summaries to local HMAC ownership."""
        results: list[SyncResult] = []
        for tag in tags:
            record = state["managed_readlists"].get(tag)
            if not record or record.get("status") != "managed":
                raise SafetyError(f"{tag}: managed state record is missing")
            if record.get("ownership_mode") == OWNERSHIP_MODE:
                live = self.client.get_readlist(record["readlist_id"])
                self._assert_owned(live, record)
                results.append(SyncResult(tag, live["id"], len(live["bookIds"]), 0, 0, "unchanged", "ownership already migrated"))
                continue
            live = self.client.get_readlist(record["readlist_id"])
            token = record.get("ownership_token")
            expected_legacy = legacy_ownership_summary(tag, token)
            if (
                not isinstance(token, str)
                or not token
                or live["id"] != record["readlist_id"]
                or live["name"] != tag
                or live["summary"] != expected_legacy
                or record.get("summary") != expected_legacy
                or live["ordered"] is not True
            ):
                raise SafetyError(f"{tag}: legacy ownership verification failed; refusing migration")
            changed_summary = format_summary(tag, self.clock())
            self.client.update_readlist(live["id"], summary=changed_summary)
            after = self.client.get_readlist(live["id"])
            if (
                after["id"] != live["id"]
                or after["name"] != tag
                or after["summary"] != changed_summary
                or after["ordered"] is not True
                or after["bookIds"] != live["bookIds"]
                or after["createdDate"] != live["createdDate"]
                or summary_minute(changed_summary) != timestamp_minute_jst(after["lastModifiedDate"])
            ):
                raise SafetyError(f"{tag}: migrated summary GET verification failed")
            record.update({
                "summary": changed_summary,
                "created_date": live["createdDate"],
                "ownership_mode": OWNERSHIP_MODE,
                "ownership_proof": ownership_proof(token, live["id"], tag, live["createdDate"]),
            })
            state["schema_version"] = 2
            self.store.save(state)
            results.append(SyncResult(tag, live["id"], len(live["bookIds"]), 0, 0, "updated", "summary ownership migrated"))
        return results

    @staticmethod
    def _difference_result(tag: str, readlist_id: str, desired: list[str], current: list[str], action: str, dry_run: bool, detail: str) -> SyncResult:
        desired_set = set(desired)
        current_set = set(current)
        return SyncResult(
            tag,
            readlist_id,
            len(desired),
            len(desired_set - current_set),
            len(current_set - desired_set),
            action,
            detail,
            dry_run,
        )

    def run(self, state: dict[str, Any], tags: list[str]) -> tuple[list[SyncResult], bool]:
        results: list[SyncResult] = []
        had_failure = False
        for tag in tags:
            try:
                result = self.sync_tag(state, tag)
            except KtrsError as exc:
                had_failure = True
                result = SyncResult(tag, state["managed_readlists"].get(tag, {}).get("readlist_id"), 0, 0, 0, "failed", str(exc), self.dry_run)
            results.append(result)
            if not self.dry_run:
                state["sync_results"][tag] = {
                    "at": utc_now(),
                    "result": result.result,
                    "book_count": result.book_count,
                    "detail": result.detail,
                }
                self.store.save(state)
            if had_failure:
                break
        if not self.dry_run:
            state["last_sync_at"] = utc_now()
            self.store.save(state)
        return results, had_failure
