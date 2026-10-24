from __future__ import annotations

import copy
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ktrs.errors import ApiNotFoundError, ApiTransportError
from ktrs.state import StateStore, empty_state
from ktrs.ownership import OWNERSHIP_MODE, format_summary, ownership_proof
from ktrs.sync import SyncEngine, readlist_name

FIXED_NOW = datetime(2026, 10, 24, 4, 0, tzinfo=timezone.utc)
FIXED_CREATED = "2026-10-24T03:59:00Z"


class FakeKomga:
    def __init__(self):
        self.books_by_tag = {}
        self.created_by_book = {}
        self.readlists = {}
        self.next_id = 1
        self.mutations = []
        self.fail_search = False
        self.fail_verification = False

    def search_book_ids_by_exact_tag(self, tag):
        if self.fail_search:
            raise ApiTransportError("network unavailable")
        ids = sorted(set(self.books_by_tag.get(tag, [])))
        return sorted(ids, key=lambda book_id: self.created_by_book.get(book_id, FIXED_CREATED), reverse=True)

    def list_readlists(self):
        return copy.deepcopy(list(self.readlists.values()))

    def get_readlist(self, readlist_id):
        if readlist_id not in self.readlists:
            raise ApiNotFoundError("not found")
        result = copy.deepcopy(self.readlists[readlist_id])
        if self.fail_verification and self.mutations:
            result["bookIds"] = ["verification-mismatch"]
        return result

    def create_readlist(self, *, name, summary, book_ids):
        readlist_id = f"r{self.next_id}"
        self.next_id += 1
        value = {
            "id": readlist_id,
            "name": name,
            "summary": summary,
            "ordered": True,
            "bookIds": list(book_ids),
            "createdDate": "2026-10-24T04:00:00Z",
            "lastModifiedDate": "2026-10-24T04:00:00Z",
        }
        self.readlists[readlist_id] = value
        self.mutations.append(("POST", readlist_id, list(book_ids)))
        return copy.deepcopy(value)

    def update_readlist(self, readlist_id, *, book_ids=None, summary=None):
        self.mutations.append(("PATCH", readlist_id, list(book_ids) if book_ids is not None else None, summary))
        if book_ids is not None:
            self.readlists[readlist_id]["bookIds"] = list(book_ids)
        if summary is not None:
            self.readlists[readlist_id]["summary"] = summary
            minute = summary.splitlines()[1].removeprefix("Date: ").removesuffix(" JST")
            local = datetime.strptime(minute, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("Asia/Tokyo"))
            self.readlists[readlist_id]["lastModifiedDate"] = local.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def update_readlist_books(self, readlist_id, book_ids):
        self.update_readlist(readlist_id, book_ids=book_ids)


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = StateStore(Path(self.temp.name) / "state.json")
        self.state = empty_state()
        self.client = FakeKomga()
        self.tag = "character:a"

    def tearDown(self):
        self.temp.cleanup()

    def engine(self, dry_run=False):
        return SyncEngine(self.client, self.store, dry_run=dry_run, clock=lambda: FIXED_NOW)

    def manage(self, tag=None, readlist_id="r1", ids=None):
        tag = tag or self.tag
        ids = ids or ["b1"]
        token = "token-123"
        summary = format_summary(tag, FIXED_NOW)
        record = {
            "status": "managed",
            "tag": tag,
            "name": readlist_name(tag),
            "summary": summary,
            "ownership_token": token,
            "readlist_id": readlist_id,
            "created_date": FIXED_CREATED,
            "ownership_mode": OWNERSHIP_MODE,
            "ownership_proof": ownership_proof(token, readlist_id, tag, FIXED_CREATED),
        }
        self.state["managed_readlists"][tag] = record
        self.client.readlists[readlist_id] = {
            "id": readlist_id,
            "name": record["name"],
            "summary": record["summary"],
            "ordered": True,
            "bookIds": list(ids),
            "createdDate": FIXED_CREATED,
            "lastModifiedDate": "2026-10-24T04:00:00Z",
        }
        return record

    def test_initial_create_then_idempotent_unchanged(self):
        self.client.books_by_tag[self.tag] = ["b2", "b1", "b2"]
        result = self.engine().sync_tag(self.state, self.tag)
        self.assertEqual(result.result, "created")
        self.assertEqual(result.book_count, 2)
        self.assertEqual([call[0] for call in self.client.mutations], ["POST"])
        record = self.state["managed_readlists"][self.tag]
        self.assertEqual(record["status"], "managed")
        self.assertEqual(record["readlist_id"], result.readlist_id)
        self.assertEqual(record["summary"], "Tag: character:a\nDate: 2026-10-24 13:00 JST\nManaged by KTRS")
        self.assertEqual(record["ownership_mode"], OWNERSHIP_MODE)
        self.assertTrue(record["ownership_proof"])
        again = self.engine().sync_tag(self.state, self.tag)
        self.assertEqual(again.result, "unchanged")
        self.assertEqual(len(self.client.mutations), 1)

    def test_readlist_name_exactly_matches_tag(self):
        self.assertEqual(readlist_name("no-chara-tag"), "no-chara-tag")
        self.assertNotIn("[KTRS]", readlist_name("no-work-tag"))

    def test_existing_update_book_add_remove_and_tag_change(self):
        self.manage(ids=["b1", "b3"])
        self.client.books_by_tag[self.tag] = ["b1", "b2"]
        result = self.engine().sync_tag(self.state, self.tag)
        self.assertEqual((result.result, result.added_count, result.removed_count), ("updated", 1, 1))
        self.assertEqual(self.client.readlists["r1"]["bookIds"], ["b1", "b2"])
        self.assertEqual([call[0] for call in self.client.mutations], ["PATCH"])

    def test_changed_updates_date_but_unchanged_preserves_it_and_skips_patch(self):
        record = self.manage(ids=["b1"])
        self.client.books_by_tag[self.tag] = ["b1", "b2"]
        changed_at = datetime(2026, 10, 24, 4, 1, tzinfo=timezone.utc)
        result = SyncEngine(self.client, self.store, clock=lambda: changed_at).sync_tag(self.state, self.tag)
        self.assertEqual(result.result, "updated")
        expected = "Tag: character:a\nDate: 2026-10-24 13:01 JST\nManaged by KTRS"
        self.assertEqual(record["summary"], expected)
        self.assertEqual(self.client.readlists["r1"]["summary"], expected)
        self.client.mutations.clear()
        later = datetime(2026, 10, 24, 4, 2, tzinfo=timezone.utc)
        result = SyncEngine(self.client, self.store, clock=lambda: later).sync_tag(self.state, self.tag)
        self.assertEqual(result.result, "unchanged")
        self.assertEqual(record["summary"], expected)
        self.assertEqual(self.client.mutations, [])

    def test_same_book_set_in_different_order_is_updated(self):
        record = self.manage(ids=["b1", "b2"])
        self.client.books_by_tag[self.tag] = ["b1", "b2"]
        self.client.created_by_book = {
            "b1": "2026-10-24T03:00:00Z",
            "b2": "2026-10-24T04:00:00Z",
        }
        changed_at = datetime(2026, 10, 24, 4, 5, tzinfo=timezone.utc)
        result = SyncEngine(self.client, self.store, clock=lambda: changed_at).sync_tag(self.state, self.tag)
        self.assertEqual((result.result, result.added_count, result.removed_count), ("updated", 0, 0))
        self.assertEqual(self.client.readlists["r1"]["bookIds"], ["b2", "b1"])
        self.assertIn("Date: 2026-10-24 13:05 JST", record["summary"])
        mutation_count = len(self.client.mutations)
        again = SyncEngine(self.client, self.store, clock=lambda: changed_at).sync_tag(self.state, self.tag)
        self.assertEqual(again.result, "unchanged")
        self.assertEqual(len(self.client.mutations), mutation_count)

    def test_add_remove_rebuilds_full_created_descending_order(self):
        self.manage(ids=["b1", "b3"])
        self.client.books_by_tag[self.tag] = ["b1", "b2"]
        self.client.created_by_book = {
            "b1": "2026-10-24T03:00:00Z",
            "b2": "2026-10-24T05:00:00Z",
        }
        result = self.engine().sync_tag(self.state, self.tag)
        self.assertEqual((result.added_count, result.removed_count), (1, 1))
        self.assertEqual(self.client.readlists["r1"]["bookIds"], ["b2", "b1"])

    def test_order_only_dry_run_reports_update_without_patch(self):
        self.manage(ids=["b1", "b2"])
        self.client.books_by_tag[self.tag] = ["b1", "b2"]
        self.client.created_by_book = {
            "b1": "2026-10-24T03:00:00Z",
            "b2": "2026-10-24T04:00:00Z",
        }
        before = copy.deepcopy(self.state)
        result = self.engine(dry_run=True).sync_tag(self.state, self.tag)
        self.assertEqual((result.result, result.added_count, result.removed_count), ("updated", 0, 0))
        self.assertEqual(self.client.mutations, [])
        self.assertEqual(self.state, before)

    def test_zero_search_never_empties_existing_readlist(self):
        self.manage(ids=["b1"])
        self.client.books_by_tag[self.tag] = []
        results, failed = self.engine().run(self.state, [self.tag])
        self.assertTrue(failed)
        self.assertEqual(results[0].result, "failed")
        self.assertEqual(self.client.readlists["r1"]["bookIds"], ["b1"])
        self.assertEqual(self.client.mutations, [])

    def test_search_failure_never_mutates(self):
        self.manage(ids=["b1"])
        self.client.fail_search = True
        results, failed = self.engine().run(self.state, [self.tag])
        self.assertTrue(failed)
        self.assertIn("network unavailable", results[0].detail)
        self.assertEqual(self.client.mutations, [])

    def test_deleted_managed_readlist_is_not_recreated(self):
        self.manage()
        del self.client.readlists["r1"]
        self.client.books_by_tag[self.tag] = ["b1"]
        results, failed = self.engine().run(self.state, [self.tag])
        self.assertTrue(failed)
        self.assertIn("automatic recreation is forbidden", results[0].detail)
        self.assertEqual(self.client.mutations, [])

    def test_manual_name_collision_is_protected(self):
        self.client.books_by_tag[self.tag] = ["b1"]
        self.client.readlists["manual"] = {
            "id": "manual", "name": readlist_name(self.tag), "summary": "human", "ordered": True, "bookIds": ["b1"],
            "createdDate": FIXED_CREATED, "lastModifiedDate": FIXED_CREATED,
        }
        results, failed = self.engine().run(self.state, [self.tag])
        self.assertTrue(failed)
        self.assertIn("unmanaged or conflicting", results[0].detail)
        self.assertEqual(self.client.mutations, [])

    def test_managed_ownership_mismatch_is_protected(self):
        self.manage()
        self.client.readlists["r1"]["summary"] = "changed by human"
        self.client.books_by_tag[self.tag] = ["b1", "b2"]
        results, failed = self.engine().run(self.state, [self.tag])
        self.assertTrue(failed)
        self.assertIn("ownership", results[0].detail)
        self.assertEqual(self.client.mutations, [])

    def test_dry_run_has_zero_komga_and_local_mutations(self):
        self.manage(ids=["b1"])
        self.client.books_by_tag[self.tag] = ["b1", "b2"]
        before = copy.deepcopy(self.state)
        results, failed = self.engine(dry_run=True).run(self.state, [self.tag])
        self.assertFalse(failed)
        self.assertEqual(results[0].result, "updated")
        self.assertTrue(results[0].dry_run)
        self.assertEqual(self.client.mutations, [])
        self.assertEqual(self.state, before)
        self.assertFalse(self.store.path.exists())

    def test_initial_dry_run_plans_create_without_pending_or_post(self):
        self.client.books_by_tag[self.tag] = ["b1"]
        before = copy.deepcopy(self.state)
        results, failed = self.engine(dry_run=True).run(self.state, [self.tag])
        self.assertFalse(failed)
        self.assertEqual(results[0].result, "created")
        self.assertIsNone(results[0].readlist_id)
        self.assertEqual(self.client.mutations, [])
        self.assertEqual(self.state, before)
        self.assertFalse(self.store.path.exists())

    def test_pending_without_confirmed_id_safe_stops(self):
        record = self.manage(ids=["b1"])
        record["status"] = "creation_pending"
        record["readlist_id"] = None
        record.pop("created_date")
        record.pop("ownership_proof")
        record.pop("ownership_mode")
        self.client.books_by_tag[self.tag] = ["b1"]
        results, failed = self.engine().run(self.state, [self.tag])
        self.assertTrue(failed)
        self.assertIn("no confirmed Readlist ID", results[0].detail)
        self.assertEqual(self.client.mutations, [])

    def test_unresolved_pending_safe_stops(self):
        token = "token-123"
        self.state["managed_readlists"][self.tag] = {
            "status": "creation_pending",
            "tag": self.tag,
            "name": readlist_name(self.tag),
            "summary": format_summary(self.tag, FIXED_NOW),
            "ownership_token": token,
            "readlist_id": None,
        }
        self.client.books_by_tag[self.tag] = ["b1"]
        results, failed = self.engine().run(self.state, [self.tag])
        self.assertTrue(failed)
        self.assertIn("no confirmed Readlist ID", results[0].detail)
        self.assertEqual(self.client.mutations, [])

    def test_explicit_legacy_summary_migration_preserves_books_and_builds_proof(self):
        token = "legacy-secret"
        summary = f"Managed by KTRS. tag={self.tag}; ownership={token}"
        self.state["schema_version"] = 1
        self.state["managed_readlists"][self.tag] = {
            "status": "managed", "tag": self.tag, "name": self.tag,
            "summary": summary, "ownership_token": token, "readlist_id": "r1",
        }
        self.client.readlists["r1"] = {
            "id": "r1", "name": self.tag, "summary": summary, "ordered": True,
            "bookIds": ["b1"], "createdDate": FIXED_CREATED, "lastModifiedDate": FIXED_CREATED,
        }
        results = self.engine().migrate_summaries(self.state, [self.tag])
        self.assertEqual(results[0].result, "updated")
        record = self.state["managed_readlists"][self.tag]
        self.assertEqual(record["ownership_mode"], OWNERSHIP_MODE)
        self.assertEqual(record["created_date"], FIXED_CREATED)
        self.assertTrue(record["ownership_proof"])
        self.assertNotIn(token, record["summary"])
        self.assertEqual(self.client.readlists["r1"]["bookIds"], ["b1"])
        self.assertEqual(self.state["schema_version"], 2)

    def test_update_get_verification_failure_is_reported(self):
        record = self.manage(ids=["b1"])
        old_summary = record["summary"]
        self.client.books_by_tag[self.tag] = ["b1", "b2"]
        self.client.fail_verification = True
        results, failed = self.engine().run(self.state, [self.tag])
        self.assertTrue(failed)
        self.assertIn("GET verification failed", results[0].detail)
        self.assertEqual([call[0] for call in self.client.mutations], ["PATCH"])
        self.assertEqual(record["summary"], old_summary)

    def test_remove_favorite_does_not_delete_readlist(self):
        self.manage()
        # Removal is a config-only operation. Excluding the tag from run leaves Komga untouched.
        results, failed = self.engine().run(self.state, [])
        self.assertFalse(failed)
        self.assertEqual(results, [])
        self.assertIn("r1", self.client.readlists)
        self.assertEqual(self.client.mutations, [])


if __name__ == "__main__":
    unittest.main()
