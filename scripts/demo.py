import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import Request, urlopen


def call(url, value=None):
    request = Request(url, data=json.dumps(value).encode() if value is not None else None,
                      headers={"Content-Type": "application/json"})
    with urlopen(request, timeout=15) as response:
        return json.load(response)


def main():
    root = Path(__file__).resolve().parents[1]
    processes = []
    started = time.monotonic()
    with tempfile.TemporaryDirectory() as temporary:
        try:
            for arguments in (
                ["-m", "learning.provider"],
                ["-m", "learning.service", "--database", str(Path(temporary) / "analyses.sqlite3")],
            ):
                processes.append(subprocess.Popen([sys.executable, *arguments], cwd=root))
            deadline = started + 65
            for port in (8765, 8766):
                while True:
                    try:
                        call(f"http://127.0.0.1:{port}/health")
                        break
                    except (URLError, TimeoutError):
                        if time.monotonic() >= deadline:
                            raise RuntimeError("服务未在启动期限内就绪")
                        time.sleep(.1)
            print(json.dumps(call("http://127.0.0.1:8766/catalog"), ensure_ascii=False, indent=2))
            record = call("http://127.0.0.1:8766/analyses", {
                "request_key": "local-overview", "release": "term-a", "analysis": "overview", "parameters": {},
            })
            print(json.dumps(record, ensure_ascii=False, indent=2))
            loaded = call("http://127.0.0.1:8766/analyses/" + record["id"])
            if loaded != record:
                raise RuntimeError("保存的分析与返回值不一致")
            print(f"本地分析完成，用时 {time.monotonic() - started:.2f} 秒")
        finally:
            for process in processes:
                process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    main()
