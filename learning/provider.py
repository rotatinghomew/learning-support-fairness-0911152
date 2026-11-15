import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from learning.codec import digest, encode


class SourceHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        return

    def send_json(self, status, value):
        body = encode(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlsplit(self.path)
        pieces = url.path.strip("/").split("/")
        if pieces == ["health"]:
            return self.send_json(200, {"status": "ok"})
        if pieces == ["releases"]:
            return self.send_json(200, {"releases": sorted(self.server.releases)})
        if len(pieces) < 2 or pieces[0] != "releases" or pieces[1] not in self.server.releases:
            return self.send_json(404, {"error": "资料版本不存在"})
        release_id = pieces[1]
        release = self.server.releases[release_id]
        revision = digest(release)
        if len(pieces) == 2:
            return self.send_json(200, {
                "release": release_id, "revision": revision,
                "tables": {name: {"rows": len(rows), "sha256": digest(rows)} for name, rows in release.items()},
            })
        if len(pieces) != 4 or pieces[2] != "tables" or pieces[3] not in release:
            return self.send_json(404, {"error": "资料表不存在"})
        query = parse_qs(url.query)
        try:
            cursor = int(query.get("cursor", ["0"])[0])
            limit = int(query.get("limit", ["41"])[0])
            if cursor < 0 or not 1 <= limit <= 200:
                raise ValueError()
        except ValueError:
            return self.send_json(400, {"error": "分页参数不合法"})
        if query.get("revision", [revision])[0] != revision:
            return self.send_json(409, {"error": "资料版本已变更"})
        rows = release[pieces[3]]
        page = rows[cursor:cursor + limit]
        next_cursor = cursor + len(page) if cursor + len(page) < len(rows) else None
        return self.send_json(200, {"release": release_id, "revision": revision, "rows": page, "next": next_cursor})


def make_server(host, port, data):
    server = ThreadingHTTPServer((host, port), SourceHandler)
    server.daemon_threads = True
    server.releases = json.loads(Path(data).read_text(encoding="utf-8"))
    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data", default="data/releases.json")
    args = parser.parse_args()
    with make_server("127.0.0.1", args.port, args.data) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
