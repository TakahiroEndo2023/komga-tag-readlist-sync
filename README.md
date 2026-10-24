# Komga Tag Readlist Sync (KTRS)

KTRS is a small, on-demand CLI that finds Komga Books with an exact favorite
tag and maintains a protected, cross-library Komga Readlist for that tag.

It does not access the Komga database, run as a daemon, or depend on Media
Ingest (MI) or Komga Auto Tagger (KAT).

## Safety model

- Book discovery uses Komga 1.29.1 `POST /api/v1/books/list` with the exact
  `tag / is / value` search condition.
- Every page is checked for a stable total, stable page count, consistent
  `last` flag, complete Book metadata, unique IDs, and the exact requested tag.
- Desired Books are ordered by Komga's top-level `BookDto.created` registration
  timestamp descending. Equal timestamps are ordered by Book ID ascending.
  Missing or invalid registration timestamps stop synchronization; file
  modification and publication dates are never used as fallbacks.
- Existing Readlist IDs are retained. An unchanged ordered ID list is never
  PATCHed.
- A PATCH is followed by GET verification of ownership metadata and the exact
  ordered Book ID list.
- A successful zero-result search never empties or creates a Readlist. It is
  reported as requiring human confirmation.
- A deleted managed Readlist is not recreated automatically.
- A matching name alone never establishes KTRS ownership.
- Network requests are bounded by a timeout and are never retried.
- `sync --dry-run` sends the read-only Book-search POST and GET requests, but
  sends no Komga create/update/delete request and makes no local state change.
- A nonblocking `flock` prevents concurrent KTRS commands that can mutate
  configuration, state, or Komga.

KTRS never sends a DELETE request to Komga.

## Managed Readlist identity

The display name is exactly the tag text:

```text
character:suzuki_hana
```

The summary is intentionally human-readable and contains no ownership secret:

```text
Tag: character:suzuki_hana
Date: 2026-10-24 13:35 JST
Managed by KTRS
```

`Date` is the actual creation or managed-content modification minute in
Asia/Tokyo. An unchanged synchronization neither changes the date nor sends a
PATCH.

Komga 1.29.1 exposes no hidden/custom Readlist metadata field. KTRS therefore
keeps a random secret only in the mode-0600 local state and stores an HMAC-SHA256
proof bound to the Readlist ID, tag, and immutable Komga `createdDate`. Normal
synchronization requires all of the following to agree:

- favorite tag
- stored Readlist ID
- expected display name
- local ownership proof
- exact three-line summary
- immutable Komga `createdDate`
- `ordered=true`

Before the initial Komga POST, KTRS durably writes a `creation_pending` record.
After a successful response it saves the returned ID, GET-verifies the
Readlist, and only then marks it `managed`. If execution is interrupted,
another Readlist is not blindly created. A pending record is accepted only
when its confirmed ID and local proof can be verified. Missing or contradictory
identity data stops for human review.

Version 0.2.0 includes an explicit one-time `migrate-summaries` command. It
first verifies the legacy visible token against state and Komga, then changes
only the summary, GET-verifies the result, and replaces visible-token ownership
with the local HMAC proof. Normal `sync` refuses legacy records so migration
cannot happen implicitly.

Do not edit `state.json` to adopt a manually created Readlist.

## Requirements

- Python 3.11 or later
- A Komga 1.29.1-compatible endpoint with an explicit port
- A dedicated Komga API key belonging to an ADMIN user (create and update
  Readlist operations require ADMIN)

KTRS has no mandatory third-party Python dependencies.

## Installation

The recommended production layout is an isolated virtual environment:

```bash
python3 -m venv /home/ai/.local/share/ktrs/venv
/home/ai/.local/share/ktrs/venv/bin/pip install /home/ai/projects/komga-tag-readlist-sync
/home/ai/.local/share/ktrs/venv/bin/ktrs --version
```

Systemd unit templates are included in `deploy/systemd`; installation remains
an explicit administrator operation.

For source-tree development:

```bash
cd /home/ai/projects/komga-tag-readlist-sync
PYTHONPATH=src python3 -m ktrs --help
```

## Configuration

Copy [`config/config.example.toml`](config/config.example.toml) to the default
location:

```text
~/.config/ktrs/config.toml
```

Minimal per-user example:

```toml
favorite_tags = ["no-chara-tag", "no-work-tag"]

[komga]
url = "http://komga.home.arpa:80"
api_key_file = "/home/USER/.config/ktrs/api-key"
timeout_seconds = 30
page_size = 100

[storage]
state_file = "/home/USER/.local/state/ktrs/state.json"
lock_file = "/home/USER/.local/state/ktrs/ktrs.lock"
```

The URL must include a port. Credentials embedded in the URL are rejected.

The API key is loaded in this order:

1. `KTRS_KOMGA_API_KEY`
2. `komga.api_key_file`

Protect the configuration and API key:

```bash
chmod 700 ~/.config/ktrs ~/.local/state/ktrs
chmod 600 ~/.config/ktrs/config.toml ~/.config/ktrs/api-key
```

Do not commit a real API key. The sample contains no credential.

An alternate config can be selected before the subcommand:

```bash
ktrs --config /etc/ktrs/config.toml status
```

## CLI

```bash
ktrs add character:suzuki_hana
ktrs remove character:suzuki_hana
ktrs list
ktrs status
ktrs sync
ktrs sync --tag character:suzuki_hana
ktrs sync --dry-run
ktrs migrate-summaries
ktrs --help
ktrs --version
```

Non-empty `character:<value>` and `work:<value>` tags are accepted, together
with the exact marker tags `no-chara-tag` and `no-work-tag`.
Favorites are stored uniquely in sorted order. Repeating `add` or `remove` is
safe. Removing a favorite only removes it from future synchronization; it does
not delete or modify its existing Komga Readlist or local ownership record.

Before adding a new favorite, `ktrs add` retrieves all Komga Book tags through
`GET /api/v2/tags?include=BOOK`, requires an exact case-sensitive match, and
only then atomically updates the configuration. A missing tag, API failure, or
incomplete tag response leaves the configuration unchanged. Close matches may
be displayed as suggestions but are never selected automatically. Adding an
already registered tag remains an offline, idempotent operation.

`sync --tag` only accepts a currently registered favorite.

Example output:

```text
tag: character:suzuki_hana
  readlist_id: 01ABCDEF
  books: 12
  added: 2
  removed: 1
  result: updated
```

Results are `created`, `updated`, `unchanged`, or `failed`. Dry-run planned
actions retain the corresponding result and add `(dry-run)`.

Expected command or safety failures return exit status 2. Success returns 0.

## Atomic local storage

Configuration and state updates use a private same-directory temporary file,
file `fsync`, `os.replace`, and parent-directory `fsync`. Files are written
with mode `0600`. Symlink configuration, state, and lock targets are rejected.

The state includes:

- schema version
- tag-to-Readlist ownership records
- `creation_pending` or `managed` status
- last synchronization time
- each tag's last result and Book count

## Tests

The test suite uses only temporary directories and mock Komga clients or HTTP
transports. It never connects to a real Komga instance.

```bash
cd /home/ai/projects/komga-tag-readlist-sync
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Coverage includes:

- first creation and second-run idempotency
- existing Readlist update
- tag add/remove idempotency
- Book add/remove and tag-result changes
- zero-result protection
- pagination and response consistency
- API transport failure
- missing or contradictory managed Readlist identity
- manual Readlist name collision
- dry-run with zero mutation calls
- nonblocking concurrent execution protection
- post-update GET verification failure
- atomic state/config writes and symlink rejection

## Daily systemd synchronization

The supplied timer runs once daily at 16:00 Asia/Tokyo. It is persistent, has
no restart policy, and runs independently from MI and KAT. KTRS's own
nonblocking lock remains the concurrency boundary.

```bash
sudo install -m 0644 deploy/systemd/ktrs-sync.service /etc/systemd/system/
sudo install -m 0644 deploy/systemd/ktrs-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ktrs-sync.timer
systemctl list-timers ktrs-sync.timer
journalctl -u ktrs-sync.service
```

## Production rollout gates

Before any production write:

1. Install with a dedicated KTRS API key and private local paths.
2. Run the complete mock test suite.
3. Perform a real-Komga GET/read-only-search connection check.
4. Run `ktrs sync --dry-run` and review the exact Book count and collision
   checks.
5. Obtain explicit approval for one tag Canary.
6. Run the same one-tag sync twice; the second run must be `unchanged` with no
   PATCH.
7. Confirm in the Komga UI that the Readlist spans the expected series and
   libraries.

No production Canary is part of Phase 2.
