from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

from .atomic import atomic_write_text, reject_symlink
from .errors import ConfigError, StateError

TAG_RE = re.compile(r"^(?:(?:character|work):[^\s\x00-\x1f\x7f]+|no-chara-tag|no-work-tag)$")


def default_config_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "ktrs" / "config.toml"


def default_state_path() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "ktrs" / "state.json"


@dataclass(frozen=True)
class Config:
    komga_url: str
    api_key_file: Path | None
    timeout_seconds: float
    page_size: int
    favorite_tags: tuple[str, ...]
    state_path: Path
    lock_path: Path

    def api_key(self) -> str:
        from_env = os.environ.get("KTRS_KOMGA_API_KEY")
        if from_env:
            return from_env
        if self.api_key_file is None:
            raise ConfigError("KTRS_KOMGA_API_KEY or komga.api_key_file is required")
        reject_symlink(self.api_key_file)
        try:
            value = self.api_key_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"cannot read API key file: {self.api_key_file}: {exc}") from exc
        if not value:
            raise ConfigError("Komga API key file is empty")
        return value


def validate_tag(tag: str) -> str:
    if not isinstance(tag, str) or not TAG_RE.fullmatch(tag):
        raise ConfigError(
            "tag must be character:<value>, work:<value>, no-chara-tag, or no-work-tag"
        )
    return tag


def _absolute(value: str | Path, *, relative_to: Path) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (relative_to / path).resolve()


def load_config(path: Path) -> Config:
    path = path.expanduser()
    reject_symlink(path)
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot load config file {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError("config root must be a TOML table")
    komga = raw.get("komga", {})
    storage = raw.get("storage", {})
    if not isinstance(komga, dict) or not isinstance(storage, dict):
        raise ConfigError("komga and storage must be TOML tables")
    url = os.environ.get("KTRS_KOMGA_URL", komga.get("url"))
    if not isinstance(url, str) or not url:
        raise ConfigError("komga.url is required")
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.port is None:
        raise ConfigError("komga.url must be http(s), include a host and explicit port")
    if parsed.path.rstrip("/") or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ConfigError("komga.url must not contain credentials, path, query, or fragment")
    key_file_value = komga.get("api_key_file")
    if key_file_value is not None and not isinstance(key_file_value, str):
        raise ConfigError("komga.api_key_file must be a path string")
    timeout = komga.get("timeout_seconds", 30)
    page_size = komga.get("page_size", 100)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 300:
        raise ConfigError("komga.timeout_seconds must be in (0, 300]")
    if isinstance(page_size, bool) or not isinstance(page_size, int) or not 1 <= page_size <= 500:
        raise ConfigError("komga.page_size must be in [1, 500]")
    tags = raw.get("favorite_tags", [])
    if not isinstance(tags, list) or any(not isinstance(item, str) for item in tags):
        raise ConfigError("favorite_tags must be an array of strings")
    validated = [validate_tag(tag) for tag in tags]
    if len(set(validated)) != len(validated):
        raise ConfigError("favorite_tags contains duplicates")
    relative_to = path.parent
    state_value = storage.get("state_file", str(default_state_path()))
    lock_value = storage.get("lock_file", str(default_state_path().with_name("ktrs.lock")))
    if not isinstance(state_value, str) or not isinstance(lock_value, str):
        raise ConfigError("storage paths must be strings")
    try:
        state_path = _absolute(state_value, relative_to=relative_to)
        lock_path = _absolute(lock_value, relative_to=relative_to)
        key_path = _absolute(key_file_value, relative_to=relative_to) if key_file_value else None
    except (OSError, RuntimeError) as exc:
        raise ConfigError(f"invalid path in config: {exc}") from exc
    return Config(
        komga_url=url.rstrip("/"),
        api_key_file=key_path,
        timeout_seconds=float(timeout),
        page_size=page_size,
        favorite_tags=tuple(sorted(validated)),
        state_path=state_path,
        lock_path=lock_path,
    )


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def save_config(path: Path, config: Config) -> None:
    tags = ", ".join(_quote(tag) for tag in sorted(config.favorite_tags))
    lines = [
        "# Komga Tag Readlist Sync configuration",
        f"favorite_tags = [{tags}]",
        "",
        "[komga]",
        f"url = {_quote(config.komga_url)}",
        f"timeout_seconds = {config.timeout_seconds:g}",
        f"page_size = {config.page_size}",
    ]
    if config.api_key_file is not None:
        lines.append(f"api_key_file = {_quote(str(config.api_key_file))}")
    lines.extend(
        [
            "",
            "[storage]",
            f"state_file = {_quote(str(config.state_path))}",
            f"lock_file = {_quote(str(config.lock_path))}",
            "",
        ]
    )
    try:
        atomic_write_text(path, "\n".join(lines))
    except StateError as exc:
        raise ConfigError(str(exc)) from exc


def with_added_tag(config: Config, tag: str) -> tuple[Config, bool]:
    tag = validate_tag(tag)
    if tag in config.favorite_tags:
        return config, False
    return replace(config, favorite_tags=tuple(sorted((*config.favorite_tags, tag)))), True


def with_removed_tag(config: Config, tag: str) -> tuple[Config, bool]:
    tag = validate_tag(tag)
    if tag not in config.favorite_tags:
        return config, False
    return replace(config, favorite_tags=tuple(value for value in config.favorite_tags if value != tag)), True
