from __future__ import annotations

import json
import unittest
from urllib.parse import parse_qs, urlsplit

from ktrs.errors import ApiResponseError, ApiTransportError
from ktrs.komga import KomgaClient, Response


def page(content, number, total, pages):
    return {
        "content": content,
        "number": number,
        "numberOfElements": len(content),
        "totalElements": total,
        "totalPages": pages,
        "last": pages == 0 or number == pages - 1,
    }


def book(book_id: str, *tags: str, created: str = "2026-10-24T04:00:00Z"):
    return {"id": book_id, "created": created, "metadata": {"tags": list(tags)}}


def readlist(readlist_id: str = "r1"):
    return {
        "id": readlist_id,
        "name": "name",
        "summary": "summary",
        "ordered": True,
        "bookIds": [],
        "createdDate": "2026-10-24T04:00:00Z",
        "lastModifiedDate": "2026-10-24T04:00:00Z",
    }


class RecordingTransport:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append((method, url, headers, body, timeout))
        return self.handler(method, url, headers, body, timeout)


class KomgaClientTests(unittest.TestCase):
    def client(self, transport, page_size=2):
        return KomgaClient("http://komga.example:25600", "secret", page_size=page_size, transport=transport)

    def test_exact_tag_search_paginates_and_sorts_by_created_descending(self) -> None:
        tag = "character:a"

        def handler(method, url, headers, body, timeout):
            self.assertEqual(method, "POST")
            self.assertEqual(headers["X-API-Key"], "secret")
            self.assertEqual(json.loads(body), {"condition": {"tag": {"operator": "is", "value": tag}}})
            number = int(parse_qs(urlsplit(url).query)["page"][0])
            values = [
                [book("b2", tag, created="2026-10-24T04:00:00Z"), book("b1", tag, created="2026-10-24T05:00:00Z")],
                [book("b3", tag, created="2026-10-24T03:00:00Z")],
            ][number]
            return Response(200, json.dumps(page(values, number, 3, 2)).encode(), url)

        transport = RecordingTransport(handler)
        self.assertEqual(self.client(transport).search_book_ids_by_exact_tag(tag), ["b1", "b2", "b3"])
        self.assertEqual(len(transport.calls), 2)

    def test_same_created_timestamp_uses_book_id_ascending(self) -> None:
        tag = "character:a"

        def handler(method, url, headers, body, timeout):
            values = [book("b2", tag), book("b1", tag), book("b3", tag)]
            return Response(200, json.dumps(page(values, 0, 3, 1)).encode(), url)

        self.assertEqual(self.client(handler, page_size=3).search_book_ids_by_exact_tag(tag), ["b1", "b2", "b3"])

    def test_missing_or_invalid_created_timestamp_is_rejected(self) -> None:
        tag = "character:a"
        for created in (None, "", "2026-10-24", "2026-10-24T04:00:00+09:00"):
            def handler(method, url, headers, body, timeout, value=created):
                item = book("b1", tag)
                item["created"] = value
                return Response(200, json.dumps(page([item], 0, 1, 1)).encode(), url)

            with self.subTest(created=created), self.assertRaises(ApiResponseError):
                self.client(handler).search_book_ids_by_exact_tag(tag)

    def test_list_book_tags_uses_unpaged_and_requires_unique_complete_strings(self) -> None:
        def handler(method, url, headers, body, timeout):
            self.assertEqual(method, "GET")
            query = parse_qs(urlsplit(url).query)
            self.assertEqual(query["include"], ["BOOK"])
            self.assertEqual(query["unpaged"], ["true"])
            values = ["work:b", "work:a", "character:c"]
            return Response(200, json.dumps(page(values, 0, 3, 1)).encode(), url)

        self.assertEqual(self.client(handler).list_book_tags(), ["character:c", "work:a", "work:b"])

        def duplicate(method, url, headers, body, timeout):
            return Response(200, json.dumps(page(["work:a", "work:a"], 0, 2, 1)).encode(), url)

        with self.assertRaises(ApiResponseError):
            self.client(duplicate).list_book_tags()

    def test_search_rejects_partial_or_missing_tag(self) -> None:
        tag = "character:a"

        def handler(method, url, headers, body, timeout):
            return Response(200, json.dumps(page([book("b1", "character:ab")], 0, 1, 1)).encode(), url)

        with self.assertRaises(ApiResponseError):
            self.client(handler).search_book_ids_by_exact_tag(tag)

    def test_search_rejects_duplicate_ids_and_count_change(self) -> None:
        tag = "work:a"

        def duplicate(method, url, headers, body, timeout):
            return Response(200, json.dumps(page([book("b1", tag), book("b1", tag)], 0, 2, 1)).encode(), url)

        with self.assertRaises(ApiResponseError):
            self.client(duplicate).search_book_ids_by_exact_tag(tag)

        def changed(method, url, headers, body, timeout):
            number = int(parse_qs(urlsplit(url).query)["page"][0])
            value = page([book(f"b{number}", tag)], number, 2 if number == 0 else 3, 2)
            return Response(200, json.dumps(value).encode(), url)

        with self.assertRaises(ApiResponseError):
            self.client(changed, page_size=1).search_book_ids_by_exact_tag(tag)

    def test_readlist_update_uses_patch_and_no_retry(self) -> None:
        def handler(method, url, headers, body, timeout):
            self.assertEqual(method, "PATCH")
            self.assertEqual(json.loads(body), {"bookIds": ["b1"]})
            return Response(204, b"", url)

        transport = RecordingTransport(handler)
        self.client(transport).update_readlist_books("r1", ["b1"])
        self.assertEqual(len(transport.calls), 1)

    def test_get_readlist_rejects_identity_mismatch(self) -> None:
        def handler(method, url, headers, body, timeout):
            value = readlist("other")
            return Response(200, json.dumps(value).encode(), url)

        with self.assertRaises(ApiResponseError):
            self.client(handler).get_readlist("r1")

    def test_redirect_to_other_origin_is_rejected(self) -> None:
        def handler(method, url, headers, body, timeout):
            return Response(200, b"{}", "http://evil.example:25600/api/v1/readlists/r1")

        with self.assertRaises(ApiResponseError):
            self.client(handler).get_readlist("r1")

    def test_transport_failure_propagates(self) -> None:
        def handler(method, url, headers, body, timeout):
            raise ApiTransportError("offline")

        with self.assertRaises(ApiTransportError):
            self.client(handler).list_readlists()


if __name__ == "__main__":
    unittest.main()
