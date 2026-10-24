from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ktrs.cli import main
from ktrs.errors import ApiTransportError


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "config.toml"
        self.api_key = self.root / "api-key"
        self.api_key.write_text("test-key\n", encoding="utf-8")
        self.config.write_text(
            f'''favorite_tags = ["work:z"]
[komga]
url = "http://komga.example:25600"
api_key_file = {json.dumps(str(self.api_key))}
timeout_seconds = 5
page_size = 10
[storage]
state_file = {json.dumps(str(self.root / "state.json"))}
lock_file = {json.dumps(str(self.root / "ktrs.lock"))}
''',
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def invoke(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(["--config", str(self.config), *args])
        return code, stdout.getvalue(), stderr.getvalue()

    @mock.patch("ktrs.cli.KomgaClient")
    def test_add_list_remove(self, client_type):
        client_type.return_value.list_book_tags.return_value = ["character:a", "work:z"]
        code, out, _ = self.invoke("add", "character:a")
        self.assertEqual(code, 0)
        self.assertIn("added", out)
        code, out, _ = self.invoke("add", "character:a")
        self.assertEqual(code, 0)
        self.assertIn("unchanged", out)
        self.assertEqual(client_type.call_count, 1)
        code, out, _ = self.invoke("list")
        self.assertEqual(out.splitlines(), ["character:a", "work:z"])
        code, out, _ = self.invoke("remove", "character:a")
        self.assertEqual(code, 0)
        self.assertIn("removed", out)

    @mock.patch("ktrs.cli.KomgaClient")
    def test_add_rejects_missing_tag_with_suggestion_and_preserves_config(self, client_type):
        client_type.return_value.list_book_tags.return_value = ["work:girls'_frontline", "work:bang_dream!"]
        before = self.config.read_bytes()
        code, out, err = self.invoke("add", "work:girls'frontline")
        self.assertEqual(code, 2)
        self.assertEqual(out, "")
        self.assertIn("tag not found: work:girls'frontline", err)
        self.assertIn("Did you mean: work:girls'_frontline", err)
        self.assertIn("No changes made.", err)
        self.assertEqual(self.config.read_bytes(), before)

    @mock.patch("ktrs.cli.KomgaClient")
    def test_add_rejects_case_and_symbol_mismatches(self, client_type):
        actual = "work:tokidoki_bosotto_roshia-go_de_dereru_tonari_no_alya-san"
        client_type.return_value.list_book_tags.return_value = [actual, "work:sono_bisque_doll_wa_koi_wo_suru"]
        for invalid in (
            "work:Tokidoki_bosotto_roshia-Go_de_dereru_tonari_no_alya-San",
            "work:sono_bisque_doll_wa_ko_wo_suru",
        ):
            before = self.config.read_bytes()
            code, _, err = self.invoke("add", invalid)
            self.assertEqual(code, 2)
            self.assertIn("tag not found", err)
            self.assertEqual(self.config.read_bytes(), before)

    @mock.patch("ktrs.cli.KomgaClient")
    def test_add_api_failure_preserves_config(self, client_type):
        client_type.return_value.list_book_tags.side_effect = ApiTransportError("offline")
        before = self.config.read_bytes()
        code, _, err = self.invoke("add", "work:bang_dream!")
        self.assertEqual(code, 2)
        self.assertIn("offline", err)
        self.assertEqual(self.config.read_bytes(), before)

    @mock.patch("ktrs.cli.KomgaClient")
    def test_existing_add_is_idempotent_without_api_call(self, client_type):
        code, out, err = self.invoke("add", "work:z")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("unchanged", out)
        client_type.assert_not_called()

    def test_status_without_state(self):
        code, out, _ = self.invoke("status")
        self.assertEqual(code, 0)
        self.assertIn("registered_tags: 1", out)
        self.assertIn("work:z: never", out)

    def test_unregistered_sync_tag_fails_before_network(self):
        code, _, err = self.invoke("sync", "--tag", "character:nope", "--dry-run")
        self.assertEqual(code, 2)
        self.assertIn("tag is not registered", err)


if __name__ == "__main__":
    unittest.main()
