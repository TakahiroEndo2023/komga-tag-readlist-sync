from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

from . import __version__
from .config import default_config_path, load_config, save_config, validate_tag, with_added_tag, with_removed_tag
from .errors import KtrsError
from .komga import KomgaClient
from .locking import ProcessLock
from .state import StateStore
from .sync import SyncEngine


def _tag_suggestions(tag: str, available: list[str]) -> list[str]:
    prefix = tag.partition(":")[0] + ":" if ":" in tag else ""
    candidates = [item for item in available if not prefix or item.startswith(prefix)]
    return difflib.get_close_matches(tag, candidates, n=3, cutoff=0.6)


def _validate_tag_exists(client: KomgaClient, tag: str) -> None:
    available = client.list_book_tags()
    if tag in available:
        return
    lines = [f"tag not found: {tag}"]
    suggestions = _tag_suggestions(tag, available)
    if suggestions:
        lines.append(f"Did you mean: {', '.join(suggestions)}")
    lines.append("No changes made.")
    raise KtrsError("\n".join(lines))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ktrs", description="Synchronize Komga Book tags to protected readlists")
    parser.add_argument("--config", type=Path, default=default_config_path(), help="configuration file path")
    parser.add_argument("--version", action="version", version=f"ktrs {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("add", "remove"):
        child = sub.add_parser(command, help=f"{command} a favorite tag")
        child.add_argument("tag")
    sub.add_parser("list", help="list favorite tags")
    sub.add_parser("status", help="show local synchronization status")
    sync = sub.add_parser("sync", help="synchronize managed readlists")
    sync.add_argument("--tag", help="synchronize one registered favorite tag")
    sync.add_argument("--dry-run", action="store_true", help="calculate changes without Komga mutation requests")
    sub.add_parser("migrate-summaries", help="explicitly migrate verified legacy ownership summaries")
    return parser


def _print_results(results) -> None:
    for result in results:
        print(f"tag: {result.tag}")
        print(f"  readlist_id: {result.readlist_id or '-'}")
        print(f"  books: {result.book_count}")
        print(f"  added: {result.added_count}")
        print(f"  removed: {result.removed_count}")
        suffix = " (dry-run)" if result.dry_run else ""
        print(f"  result: {result.result}{suffix}")
        if result.detail:
            print(f"  detail: {result.detail}")


def _status(config) -> None:
    state = StateStore(config.state_path).load()
    managed = state["managed_readlists"]
    print(f"registered_tags: {len(config.favorite_tags)}")
    print(f"managed_readlists: {sum(1 for item in managed.values() if item['status'] == 'managed')}")
    print(f"pending_creations: {sum(1 for item in managed.values() if item['status'] == 'creation_pending')}")
    print(f"last_sync_at: {state['last_sync_at'] or '-'}")
    for tag in config.favorite_tags:
        previous = state["sync_results"].get(tag)
        if previous:
            print(f"{tag}: {previous['result']}, books={previous['book_count']}, at={previous['at']}")
        else:
            print(f"{tag}: never")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "list":
            for tag in config.favorite_tags:
                print(tag)
            return 0
        if args.command == "status":
            _status(config)
            return 0
        with ProcessLock(config.lock_path):
            if args.command == "add":
                updated, changed = with_added_tag(config, args.tag)
                if changed:
                    client = KomgaClient(
                        config.komga_url,
                        config.api_key(),
                        timeout=config.timeout_seconds,
                        page_size=config.page_size,
                    )
                    _validate_tag_exists(client, validate_tag(args.tag))
                    save_config(args.config, updated)
                print(f"{'added' if changed else 'unchanged'}: {validate_tag(args.tag)}")
                return 0
            if args.command == "remove":
                updated, changed = with_removed_tag(config, args.tag)
                if changed:
                    save_config(args.config, updated)
                print(f"{'removed' if changed else 'unchanged'}: {validate_tag(args.tag)}")
                return 0
            if args.command == "migrate-summaries":
                store = StateStore(config.state_path)
                state = store.load()
                client = KomgaClient(config.komga_url, config.api_key(), timeout=config.timeout_seconds, page_size=config.page_size)
                results = SyncEngine(client, store).migrate_summaries(state, list(config.favorite_tags))
                _print_results(results)
                return 0
            tag = validate_tag(args.tag) if args.tag else None
            if tag is not None and tag not in config.favorite_tags:
                raise KtrsError(f"tag is not registered: {tag}")
            tags = [tag] if tag else list(config.favorite_tags)
            if not tags:
                raise KtrsError("no favorite tags are registered")
            store = StateStore(config.state_path)
            state = store.load()
            client = KomgaClient(
                config.komga_url,
                config.api_key(),
                timeout=config.timeout_seconds,
                page_size=config.page_size,
            )
            results, failed = SyncEngine(client, store, dry_run=args.dry_run).run(state, tags)
            _print_results(results)
            return 2 if failed else 0
    except KtrsError as exc:
        print(f"ktrs: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
