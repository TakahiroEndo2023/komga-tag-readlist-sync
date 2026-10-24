from __future__ import annotations

import unittest
from datetime import datetime, timezone

from ktrs.errors import SafetyError
from ktrs.ownership import format_summary, ownership_proof, summary_minute, timestamp_minute_jst, verify_local_proof


class OwnershipTests(unittest.TestCase):
    def test_three_line_summary_uses_tokyo_timezone(self):
        value = format_summary("no-work-tag", datetime(2026, 10, 24, 4, 35, 59, tzinfo=timezone.utc))
        self.assertEqual(value, "Tag: no-work-tag\nDate: 2026-10-24 13:35 JST\nManaged by KTRS")
        self.assertEqual(summary_minute(value), "2026-10-24 13:35")
        self.assertEqual(timestamp_minute_jst("2026-10-24T04:35:01Z"), "2026-10-24 13:35")

    def test_local_hmac_proof_detects_identity_tampering(self):
        record = {
            "ownership_token": "secret", "readlist_id": "r1", "tag": "no-work-tag",
            "created_date": "2026-10-24T04:00:00Z",
        }
        record["ownership_proof"] = ownership_proof(
            record["ownership_token"], record["readlist_id"], record["tag"], record["created_date"]
        )
        verify_local_proof(record)
        record["readlist_id"] = "r2"
        with self.assertRaises(SafetyError):
            verify_local_proof(record)


if __name__ == "__main__":
    unittest.main()
