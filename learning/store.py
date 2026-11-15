from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import uuid

from learning.codec import digest, encode


class Conflict(ValueError):
    pass


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE IF NOT EXISTS analyses (id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL, request_hash TEXT NOT NULL, request_json TEXT NOT NULL, revision TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP)")

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def cached(self, request):
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM analyses WHERE request_key=?", (request["request_key"],)).fetchone()
        if row is None:
            return None
        if row["request_hash"] != digest(request):
            raise Conflict("请求编号已对应其他分析条件")
        return self.decode(row)

    def save(self, request, revision, result):
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM analyses WHERE request_key=?", (request["request_key"],)).fetchone()
            if row:
                if row["request_hash"] != digest(request):
                    raise Conflict("请求编号已对应其他分析条件")
                return self.decode(row)
            analysis_id = uuid.uuid4().hex
            connection.execute("INSERT INTO analyses(id,request_key,request_hash,request_json,revision,result_json) VALUES (?,?,?,?,?,?)", (
                analysis_id, request["request_key"], digest(request), encode(request).decode(), revision, encode(result).decode(),
            ))
            return self.decode(connection.execute("SELECT * FROM analyses WHERE id=?", (analysis_id,)).fetchone())

    def get(self, analysis_id):
        with self.connection() as connection:
            row = connection.execute("SELECT * FROM analyses WHERE id=?", (analysis_id,)).fetchone()
        return self.decode(row) if row else None

    def list(self):
        with self.connection() as connection:
            return [self.decode(row) for row in connection.execute("SELECT * FROM analyses ORDER BY created_at,id")]

    @staticmethod
    def decode(row):
        return {"id": row["id"], "request": json.loads(row["request_json"]), "revision": row["revision"], "result": json.loads(row["result_json"])}
