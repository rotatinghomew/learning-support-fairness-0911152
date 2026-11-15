import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json

from learning.client import SourceClient
from learning.codec import encode
from learning.domain import ANALYSES, PRODUCT, validate
from learning.store import Conflict, Store


class AnalysisHandler(BaseHTTPRequestHandler):
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
        if self.path == "/health":
            return self.send_json(200, {"status": "ok"})
        if self.path == "/catalog":
            try:
                return self.send_json(200, {"product": PRODUCT, "analyses": sorted(ANALYSES), "releases": self.server.source.releases()})
            except ValueError as error:
                return self.send_json(502, {"error": str(error)})
        if self.path == "/analyses":
            return self.send_json(200, {"analyses": self.server.store.list()})
        if self.path.startswith("/analyses/"):
            record = self.server.store.get(self.path.rsplit("/", 1)[-1])
            return self.send_json(200 if record else 404, record or {"error": "分析不存在"})
        return self.send_json(404, {"error": "接口不存在"})

    def do_POST(self):
        if self.path != "/analyses":
            return self.send_json(404, {"error": "接口不存在"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise ValueError("请求体长度不合法")
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict) or not {"request_key", "release", "analysis"} <= request.keys():
                raise ValueError("缺少请求编号、资料版本或分析类型")
            if any(not isinstance(request[name], str) or not request[name] for name in ("request_key", "release", "analysis")):
                raise ValueError("分析请求标识不合法")
            if not isinstance(request.get("parameters", {}), dict):
                raise ValueError("分析参数不是对象")
            cached = self.server.store.cached(request)
            if cached:
                return self.send_json(200, cached)
            if request["analysis"] not in ANALYSES:
                raise ValueError("分析类型未登记")
            tables, revision = self.server.source.load(request["release"])
            validate(tables)
            result = ANALYSES[request["analysis"]](tables, request.get("parameters", {}))
            return self.send_json(201, self.server.store.save(request, revision, result))
        except Conflict as error:
            return self.send_json(409, {"error": str(error)})
        except (ValueError, KeyError, TypeError) as error:
            return self.send_json(422, {"error": str(error)})


def make_server(host, port, source_url, database):
    server = ThreadingHTTPServer((host, port), AnalysisHandler)
    server.daemon_threads = True
    server.source = SourceClient(source_url)
    server.store = Store(database)
    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--source", default="http://127.0.0.1:8765")
    parser.add_argument("--database", default="var/analyses.sqlite3")
    args = parser.parse_args()
    with make_server("127.0.0.1", args.port, args.source, args.database) as server:
        server.serve_forever()


if __name__ == "__main__":
    main()
