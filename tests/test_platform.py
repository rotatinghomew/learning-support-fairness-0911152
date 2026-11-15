from concurrent.futures import ThreadPoolExecutor
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from learning.client import SourceClient
from learning.codec import digest, numeric, unique
from learning.domain import ANALYSES, validate
from learning.provider import make_server as source_server
from learning.service import make_server as analysis_server
from learning.store import Conflict, Store


class CodecTests(unittest.TestCase):
    def test_canonical_hash(self):
        self.assertEqual(digest({"a": 1, "b": 2}), digest({"b": 2, "a": 1}))

    def test_nonfinite_rejected(self):
        for value in (True, None, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                numeric(value, "数值")

    def test_duplicate_business_key(self):
        with self.assertRaises(ValueError):
            unique([{"id": 1}, {"id": 1}], ["id"])


class PlatformTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.source = source_server("127.0.0.1", 0, "data/releases.json")
        cls.source_url = f"http://127.0.0.1:{cls.source.server_port}"
        cls.application = analysis_server("127.0.0.1", 0, cls.source_url, Path(cls.temporary.name) / "history.sqlite3")
        cls.base_url = f"http://127.0.0.1:{cls.application.server_port}"
        cls.threads = []
        for server in (cls.source, cls.application):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            cls.threads.append(thread)

    @classmethod
    def tearDownClass(cls):
        for server in (cls.application, cls.source):
            server.shutdown()
            server.server_close()
        for thread in cls.threads:
            thread.join()
        cls.temporary.cleanup()

    def request(self, path, value=None):
        request = Request(self.base_url + path, data=json.dumps(value).encode() if value is not None else None,
                          headers={"Content-Type": "application/json"})
        with urlopen(request, timeout=10) as response:
            return response.status, json.load(response)

    def test_source_manifest_and_pagination(self):
        tables, revision = SourceClient(self.source_url).load("term-a")
        self.assertEqual(revision, digest(tables))
        validate(tables)
        self.assertGreater(sum(map(len, tables.values())), 41)

    def test_every_release_valid(self):
        client = SourceClient(self.source_url)
        for release in client.releases():
            with self.subTest(release=release):
                tables, revision = client.load(release)
                validate(tables)
                self.assertEqual(revision, digest(tables))
                self.assertIsInstance(ANALYSES["overview"](tables, {}), dict)

    def test_catalog(self):
        status, catalog = self.request("/catalog")
        self.assertEqual(status, 200)
        self.assertIn("overview", catalog["analyses"])

    def test_save_retrieve_and_replay(self):
        request = {"request_key": "replay", "release": "term-a", "analysis": "overview"}
        status, record = self.request("/analyses", request)
        self.assertEqual(status, 201)
        self.assertEqual(record, self.request("/analyses/" + record["id"])[1])
        self.assertEqual(record, self.request("/analyses", request)[1])

    def test_changed_conditions_conflict(self):
        request = {"request_key": "conflict", "release": "term-a", "analysis": "overview"}
        self.request("/analyses", request)
        with self.assertRaises(HTTPError) as raised:
            self.request("/analyses", {**request, "release": "term-b"})
        self.assertEqual(raised.exception.code, 409)

    def test_concurrent_idempotency(self):
        request = {"request_key": "concurrent", "release": "term-a", "analysis": "overview"}
        with ThreadPoolExecutor(max_workers=4) as pool:
            records = list(pool.map(lambda unused: self.request("/analyses", request)[1], range(4)))
        self.assertEqual(len({record["id"] for record in records}), 1)

    def test_unknown_analysis_is_not_success(self):
        with self.assertRaises(HTTPError) as raised:
            self.request("/analyses", {"request_key": "unknown", "release": "term-a", "analysis": "not-registered"})
        self.assertEqual(raised.exception.code, 422)

    def test_missing_release(self):
        with self.assertRaises(ValueError):
            SourceClient(self.source_url).load("missing")

    def test_bad_pagination(self):
        with self.assertRaises(HTTPError) as raised:
            urlopen(self.source_url + "/releases/term-a/tables/missing?limit=0")
        self.assertEqual(raised.exception.code, 404)

    def test_wrong_revision(self):
        table = next(iter(SourceClient(self.source_url).load("term-a")[0]))
        with self.assertRaises(HTTPError) as raised:
            urlopen(self.source_url + f"/releases/term-a/tables/{table}?revision=bad")
        self.assertEqual(raised.exception.code, 409)

    def test_restart_preserves_history(self):
        request = {"request_key": "restart", "release": "term-a", "analysis": "overview"}
        record = self.request("/analyses", request)[1]
        reopened = Store(Path(self.temporary.name) / "history.sqlite3")
        self.assertEqual(record, reopened.get(record["id"]))


if __name__ == "__main__":
    unittest.main()
