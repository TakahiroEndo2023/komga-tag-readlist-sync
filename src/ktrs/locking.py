from __future__ import annotations

import fcntl
import os
from pathlib import Path

from .errors import LockBusyError, StateError


class ProcessLock:
    def __init__(self, path: Path):
        self.path = path.expanduser()
        self._fd: int | None = None

    def __enter__(self) -> "ProcessLock":
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if self.path.is_symlink():
                raise StateError(f"refusing symlink lock path: {self.path}")
            self._fd = os.open(
                self.path,
                os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600,
            )
            os.fchmod(self._fd, 0o600)
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            os.ftruncate(self._fd, 0)
            os.write(self._fd, f"{os.getpid()}\n".encode())
            os.fsync(self._fd)
        except BlockingIOError as exc:
            self._close()
            raise LockBusyError(f"another KTRS process holds {self.path}") from exc
        except OSError as exc:
            self._close()
            raise StateError(f"cannot acquire lock {self.path}: {exc}") from exc
        return self

    def _close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __exit__(self, exc_type, exc, traceback) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        self._close()
