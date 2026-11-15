import json
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import urlopen

from learning.codec import digest


class SourceClient:
    def __init__(self, base_url, timeout=5):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def read(self, path):
        try:
            with urlopen(self.base_url + path, timeout=self.timeout) as response:
                return json.load(response)
        except (HTTPError, URLError, TimeoutError) as error:
            raise ValueError("资料服务不可用或请求版本不存在") from error

    def releases(self):
        return self.read("/releases")["releases"]

    def load(self, release_id):
        prefix = "/releases/" + quote(release_id, safe="")
        manifest = self.read(prefix)
        result = {}
        for name, specification in manifest["tables"].items():
            rows = []
            cursor = 0
            visited = set()
            while cursor is not None:
                if cursor in visited:
                    raise ValueError("资料分页游标未前进")
                visited.add(cursor)
                query = urlencode({"revision": manifest["revision"], "cursor": cursor, "limit": 41})
                page = self.read(prefix + "/tables/" + quote(name, safe="") + "?" + query)
                if page["revision"] != manifest["revision"] or page["release"] != release_id:
                    raise ValueError("资料分页跨版本")
                rows.extend(page["rows"])
                cursor = page["next"]
            if len(rows) != specification["rows"] or digest(rows) != specification["sha256"]:
                raise ValueError("资料完整性不一致")
            result[name] = rows
        if digest(result) != manifest["revision"]:
            raise ValueError("资料清单校验失败")
        return result, manifest["revision"]
