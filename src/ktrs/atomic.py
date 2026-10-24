from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any

from .errors import StateError


def reject_symlink(path: Path) -> None:
    try:
        if path.is_symlink():
            raise StateError(f"refusing symlink path: {path}")
    except OSError as exc:
        raise StateError(f"cannot inspect path: {path}: {exc}") from exc


def atomic_write_text(path: Path, text: str, *, mode: int = 0o600) -> None:
    """Write a file durably with a same-directory replace and directory fsync."""
    path = path.expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    reject_symlink(path)
    temp = path.parent / f".{path.name}.tmp.{os.getpid()}.{secrets.token_hex(6)}"
    fd = -1
    try:
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            fd = -1
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError as exc:
        raise StateError(f"atomic write failed for {path}: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
