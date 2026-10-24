from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from ktrs.config import ConfigError, load_config, save_config, with_added_tag, with_removed_tag
from ktrs.errors import LockBusyError, StateError
from ktrs.locking import ProcessLock
from ktrs.state import StateStore, empty_state


def write_config(path: Path, tags: list[str] | None = None) -> None:
    tags = tags or []
    rendered = ", ".join(json.dumps(tag) for tag in tags)
    path.write_text(
        f'''favorite_tags = [{rendered}]
[komga]
url = "http://komga.example:25600"
timeout_seconds = 5
page_size = 2
[storage]
state_file = "state.json"
lock_file = "ktrs.lock"
''',
        encoding="utf-8",
    )


class ConfigStateLockTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config.toml"

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_add_remove_are_idempotent_sorted_and_atomic(self) -> None:
        write_config(self.config_path, ["work:z"])
        config = load_config(self.config_path)
        updated, changed = with_added_tag(config, "character:a")
        self.assertTrue(changed)
        save_config(self.config_path, updated)
        self.assertEqual(load_config(self.config_path).favorite_tags, ("character:a", "work:z"))
        mode = stat.S_IMODE(self.config_path.stat().st_mode)
        self.assertEqual(mode, 0o600)
        same, changed = with_added_tag(load_config(self.config_path), "character:a")
        self.assertFalse(changed)
        removed, changed = with_removed_tag(same, "character:a")
        self.assertTrue(changed)
        save_config(self.config_path, removed)
        missing, changed = with_removed_tag(load_config(self.config_path), "character:a")
        self.assertFalse(changed)
        self.assertEqual(missing.favorite_tags, ("work:z",))

    def test_duplicate_and_invalid_tags_are_rejected(self) -> None:
        write_config(self.config_path, ["work:a", "work:a"])
        with self.assertRaises(ConfigError):
            load_config(self.config_path)

    def test_no_tag_markers_are_accepted(self) -> None:
        write_config(self.config_path, ["no-work-tag", "no-chara-tag"])
        self.assertEqual(
            load_config(self.config_path).favorite_tags,
            ("no-chara-tag", "no-work-tag"),
        )
        write_config(self.config_path, ["series:a"])
        with self.assertRaises(ConfigError):
            load_config(self.config_path)

    def test_url_requires_explicit_port(self) -> None:
        write_config(self.config_path)
        self.config_path.write_text(self.config_path.read_text().replace(":25600", ""), encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(self.config_path)

    def test_state_roundtrip_and_schema_validation(self) -> None:
        store = StateStore(self.root / "state.json")
        state = empty_state()
        store.save(state)
        self.assertEqual(store.load(), state)
        self.assertEqual(stat.S_IMODE(store.path.stat().st_mode), 0o600)
        store.path.write_text('{"schema_version":99}', encoding="utf-8")
        with self.assertRaises(StateError):
            store.load()

    def test_symlink_state_is_rejected(self) -> None:
        target = self.root / "target"
        target.write_text("{}", encoding="utf-8")
        link = self.root / "state.json"
        link.symlink_to(target)
        with self.assertRaises(StateError):
            StateStore(link).load()

    def test_nonblocking_process_lock(self) -> None:
        path = self.root / "ktrs.lock"
        with ProcessLock(path):
            with self.assertRaises(LockBusyError):
                with ProcessLock(path):
                    self.fail("second lock unexpectedly acquired")
        with ProcessLock(path):
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
