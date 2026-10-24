from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from ktrs.cli import main


class CliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = self.root / "config.toml"
        self.config.write_text(
            f'''favorite_tags = ["work:z"]
[komga]
url = "http://komga.example:25600"
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

    def test_add_list_remove(self):
        code, out, _ = self.invoke("add", "character:a")
        self.assertEqual(code, 0)
        self.assertIn("added", out)
        code, out, _ = self.invoke("add", "character:a")
        self.assertEqual(code, 0)
        self.assertIn("unchanged", out)
        code, out, _ = self.invoke("list")
        self.assertEqual(out.splitlines(), ["character:a", "work:z"])
        code, out, _ = self.invoke("remove", "character:a")
        self.assertEqual(code, 0)
        self.assertIn("removed", out)

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
