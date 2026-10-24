from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from .errors import SafetyError

TOKYO = ZoneInfo("Asia/Tokyo")
OWNERSHIP_MODE = "local_hmac_v1"


def format_summary(tag: str, changed_at: datetime) -> str:
    local = changed_at.astimezone(TOKYO)
    return f"Tag: {tag}\nDate: {local:%Y-%m-%d %H:%M} JST\nManaged by KTRS"


def parse_komga_datetime(value: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise SafetyError("Komga timestamp is missing")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        result = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise SafetyError("Komga timestamp is invalid") from exc
    if result.tzinfo is None:
        raise SafetyError("Komga timestamp has no timezone")
    return result


def summary_minute(summary: str) -> str:
    lines = summary.splitlines()
    if len(lines) != 3 or not lines[1].startswith("Date: ") or not lines[1].endswith(" JST"):
        raise SafetyError("managed summary does not have the required three-line format")
    return lines[1][6:-4]


def timestamp_minute_jst(value: str) -> str:
    return parse_komga_datetime(value).astimezone(TOKYO).strftime("%Y-%m-%d %H:%M")


def ownership_proof(token: str, readlist_id: str, tag: str, created_date: str) -> str:
    message = f"ktrs-readlist-v1\0{readlist_id}\0{tag}\0{created_date}".encode()
    return hmac.new(token.encode(), message, hashlib.sha256).hexdigest()


def verify_local_proof(record: dict[str, object]) -> None:
    required = ("ownership_token", "readlist_id", "tag", "created_date", "ownership_proof")
    if any(not isinstance(record.get(field), str) or not record[field] for field in required):
        raise SafetyError("local ownership proof record is incomplete")
    expected = ownership_proof(
        str(record["ownership_token"]),
        str(record["readlist_id"]),
        str(record["tag"]),
        str(record["created_date"]),
    )
    if not hmac.compare_digest(expected, str(record["ownership_proof"])):
        raise SafetyError("local ownership proof mismatch")
