from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .errors import ApiNotFoundError, ApiResponseError, ApiTransportError

MAX_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_PAGES = 10_000


@dataclass(frozen=True)
class Response:
    status: int
    body: bytes
    final_url: str


Transport = Callable[[str, str, dict[str, str], bytes | None, float], Response]


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None, timeout: float) -> Response:
    request = Request(url, headers=headers, data=body, method=method)
    try:
        with urlopen(request, timeout=timeout) as response:
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise ApiResponseError("Komga response exceeds the size limit")
            return Response(response.status, data, response.geturl())
    except HTTPError as exc:
        body_value = exc.read(MAX_RESPONSE_BYTES + 1)
        return Response(exc.code, body_value, exc.geturl())
    except (URLError, TimeoutError, socket.timeout, OSError) as exc:
        raise ApiTransportError(f"Komga request failed: {exc}") from exc


class KomgaClient:
    def __init__(self, base_url: str, api_key: str, *, timeout: float = 30, page_size: int = 100, transport: Transport = urllib_transport):
        parsed = urlsplit(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.port is None:
            raise ValueError("invalid Komga URL")
        if parsed.path.rstrip("/") or parsed.query or parsed.fragment or parsed.username or parsed.password:
            raise ValueError("invalid Komga URL")
        if not api_key:
            raise ValueError("Komga API key is required")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.page_size = page_size
        self.transport = transport
        self._origin = (parsed.scheme, parsed.hostname, parsed.port)

    def _request(self, method: str, path: str, *, payload: dict[str, Any] | None = None, expected: tuple[int, ...] = (200,)) -> Any:
        headers = {"Accept": "application/json", "X-API-Key": self.api_key}
        body = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        response = self.transport(method, self.base_url + path, headers, body, self.timeout)
        final = urlsplit(response.final_url)
        if (final.scheme, final.hostname, final.port) != self._origin:
            raise ApiResponseError("Komga redirected outside the configured origin")
        if response.status == 404:
            raise ApiNotFoundError(f"Komga returned HTTP 404 for {path.split('?')[0]}")
        if response.status not in expected:
            raise ApiResponseError(f"Komga returned HTTP {response.status} for {method} {path.split('?')[0]}")
        if response.status == 204:
            if response.body not in {b"", None}:
                raise ApiResponseError("Komga HTTP 204 response unexpectedly had a body")
            return None
        try:
            return json.loads(response.body)
        except (TypeError, ValueError) as exc:
            raise ApiResponseError("Komga returned invalid JSON") from exc

    @staticmethod
    def _validate_page(value: Any, page: int, expected_total: int | None, expected_pages: int | None) -> tuple[list[Any], int, int, bool]:
        if not isinstance(value, dict):
            raise ApiResponseError("Komga page response is not an object")
        content = value.get("content")
        number = value.get("number")
        total = value.get("totalElements")
        pages = value.get("totalPages")
        last = value.get("last")
        count = value.get("numberOfElements")
        if (
            not isinstance(content, list)
            or isinstance(number, bool) or not isinstance(number, int)
            or isinstance(total, bool) or not isinstance(total, int)
            or isinstance(pages, bool) or not isinstance(pages, int)
            or not isinstance(last, bool)
            or isinstance(count, bool) or not isinstance(count, int)
            or number != page or total < 0 or pages < 0 or count != len(content)
        ):
            raise ApiResponseError("Komga pagination response is invalid")
        if pages > MAX_PAGES:
            raise ApiResponseError("Komga pagination exceeds the safe page limit")
        if expected_total is not None and (total != expected_total or pages != expected_pages):
            raise ApiResponseError("Komga result changed during pagination")
        expected_last = pages == 0 or page == pages - 1
        if last != expected_last:
            raise ApiResponseError("Komga pagination last flag is inconsistent")
        return content, total, pages, last

    def search_book_ids_by_exact_tag(self, tag: str) -> list[str]:
        payload = {"condition": {"tag": {"operator": "is", "value": tag}}}
        books: dict[str, datetime] = {}
        page = 0
        expected_total: int | None = None
        expected_pages: int | None = None
        while True:
            query = urlencode({"page": page, "size": self.page_size, "sort": "id,asc"})
            value = self._request("POST", f"/api/v1/books/list?{query}", payload=payload)
            content, total, pages, last = self._validate_page(value, page, expected_total, expected_pages)
            if expected_total is None:
                expected_total, expected_pages = total, pages
            for book in content:
                if not isinstance(book, dict):
                    raise ApiResponseError("Komga book entry is invalid")
                book_id = book.get("id")
                created = book.get("created")
                metadata = book.get("metadata")
                tags = metadata.get("tags") if isinstance(metadata, dict) else None
                if not isinstance(book_id, str) or not book_id or book_id in books:
                    raise ApiResponseError("Komga book identity is missing or duplicated")
                if not isinstance(created, str):
                    raise ApiResponseError("Komga book created timestamp is missing or invalid")
                try:
                    created_at = datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                except ValueError as exc:
                    raise ApiResponseError("Komga book created timestamp is missing or invalid") from exc
                if not isinstance(tags, list) or any(not isinstance(item, str) for item in tags):
                    raise ApiResponseError("Komga book tags are incomplete")
                if tag not in tags:
                    raise ApiResponseError("Komga returned a book without the exact requested tag")
                books[book_id] = created_at
            if last:
                break
            page += 1
        if expected_total is None or len(books) != expected_total:
            raise ApiResponseError("Komga book search result is incomplete")
        by_id = sorted(books.items())
        return [book_id for book_id, _ in sorted(by_id, key=lambda item: item[1], reverse=True)]

    def list_book_tags(self) -> list[str]:
        tags: set[str] = set()
        query = urlencode({"include": "BOOK", "unpaged": "true"})
        value = self._request("GET", f"/api/v2/tags?{query}")
        content, total, pages, last = self._validate_page(value, 0, None, None)
        if not last or pages not in {0, 1}:
            raise ApiResponseError("Komga unpaged tag list has invalid pagination")
        for tag in content:
            if not isinstance(tag, str) or not tag or tag in tags:
                raise ApiResponseError("Komga tag list contains an invalid or duplicate tag")
            tags.add(tag)
        if len(tags) != total:
            raise ApiResponseError("Komga tag list is incomplete")
        return sorted(tags)

    @staticmethod
    def validate_readlist(value: Any, *, expected_id: str | None = None) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ApiResponseError("Komga readlist response is not an object")
        required = {
            "id": str,
            "name": str,
            "summary": str,
            "ordered": bool,
            "bookIds": list,
            "createdDate": str,
            "lastModifiedDate": str,
        }
        for field, field_type in required.items():
            if not isinstance(value.get(field), field_type):
                raise ApiResponseError(f"Komga readlist response has invalid {field}")
        if not value["id"] or not value["name"]:
            raise ApiResponseError("Komga readlist identity is empty")
        if expected_id is not None and value["id"] != expected_id:
            raise ApiResponseError("Komga readlist identity mismatch")
        book_ids = value["bookIds"]
        if any(not isinstance(item, str) or not item for item in book_ids) or len(set(book_ids)) != len(book_ids):
            raise ApiResponseError("Komga readlist contains invalid or duplicate Book IDs")
        return value

    def list_readlists(self) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        seen: set[str] = set()
        page = 0
        expected_total: int | None = None
        expected_pages: int | None = None
        while True:
            query = urlencode({"page": page, "size": self.page_size})
            value = self._request("GET", f"/api/v1/readlists?{query}")
            content, total, pages, last = self._validate_page(value, page, expected_total, expected_pages)
            if expected_total is None:
                expected_total, expected_pages = total, pages
            for item in content:
                readlist = self.validate_readlist(item)
                if readlist["id"] in seen:
                    raise ApiResponseError("Komga returned a duplicate readlist ID")
                seen.add(readlist["id"])
                values.append(readlist)
            if last:
                break
            page += 1
        if expected_total is None or len(values) != expected_total:
            raise ApiResponseError("Komga readlist result is incomplete")
        return values

    def get_readlist(self, readlist_id: str) -> dict[str, Any]:
        return self.validate_readlist(self._request("GET", f"/api/v1/readlists/{readlist_id}"), expected_id=readlist_id)

    def create_readlist(self, *, name: str, summary: str, book_ids: list[str]) -> dict[str, Any]:
        if not book_ids:
            raise ValueError("Komga requires at least one Book ID when creating a readlist")
        payload = {"name": name, "summary": summary, "ordered": True, "bookIds": book_ids}
        return self.validate_readlist(self._request("POST", "/api/v1/readlists", payload=payload))

    def update_readlist(self, readlist_id: str, *, book_ids: list[str] | None = None, summary: str | None = None) -> None:
        payload: dict[str, Any] = {}
        if book_ids is not None:
            payload["bookIds"] = book_ids
        if summary is not None:
            payload["summary"] = summary
        if not payload:
            raise ValueError("readlist update payload must not be empty")
        self._request("PATCH", f"/api/v1/readlists/{readlist_id}", payload=payload, expected=(204,))

    def update_readlist_books(self, readlist_id: str, book_ids: list[str]) -> None:
        self.update_readlist(readlist_id, book_ids=book_ids)
