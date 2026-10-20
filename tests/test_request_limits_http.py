"""Two actual HTTP server processes share ingress state and keep published data intact."""

import socket
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from server_support import IdentityFixture

from chipload.quotas import QuotaPolicy

SERVER = """
import socket
import sys
from pathlib import Path
import uvicorn
from chipload.auth import ServerSettings
from chipload.server import create_app
settings = ServerSettings.model_validate_json(Path(sys.argv[1]).read_bytes())
server = uvicorn.Server(uvicorn.Config(
    create_app(settings), proxy_headers=False, access_log=False, log_level="warning",
))
server.run(sockets=[socket.socket(fileno=int(sys.argv[2]))])
"""


class RequestLimitHTTPTests(unittest.TestCase):
    def test_competing_servers_and_restarted_process_share_one_allowance(self):
        with tempfile.TemporaryDirectory() as directory:
            fixture = IdentityFixture(directory)
            self.addCleanup(fixture.client.close)
            operations = fixture.operations()
            before = operations.published.catalog("thermal-example")
            children = []
            streams = []

            def stop(process):
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)

            def start(expected):
                listener = socket.socket()
                listener.bind(("127.0.0.1", 0))
                base = f"http://127.0.0.1:{listener.getsockname()[1]}"
                config = Path(directory) / f"server-{len(children)}.json"
                config.write_text(
                    fixture.settings.model_copy(
                        update={"resource_url": base + "/mcp"}
                    ).model_dump_json()
                )
                errors = tempfile.TemporaryFile()
                streams.append(errors)
                try:
                    process = subprocess.Popen(
                        [sys.executable, "-c", SERVER, str(config), str(listener.fileno())],
                        pass_fds=(listener.fileno(),),
                        stdout=subprocess.DEVNULL,
                        stderr=errors,
                    )
                    children.append(process)
                finally:
                    listener.close()
                with httpx.Client(trust_env=False, timeout=2) as client:
                    for _ in range(100):
                        if process.poll() is not None:
                            errors.seek(0)
                            self.fail(errors.read(8192).decode())
                        try:
                            response = client.get(base + "/healthz")
                            if response.status_code == expected:
                                return process, base
                        except httpx.TransportError:
                            pass
                        time.sleep(0.05)
                self.fail("HTTP server did not reach the expected admission state")

            try:
                _, first = start(200)
                second_process, second = start(200)
                repository = operations.service.repository
                status = repository.read_quotas()
                status["policy"]["requests"]["deployment"] = {"per_minute": 1, "burst": 5}
                repository.set_quotas(
                    QuotaPolicy.model_validate(status["policy"]),
                    expected_generation=status["generation"],
                )
                with httpx.Client(trust_env=False, timeout=5) as client:
                    with ThreadPoolExecutor(max_workers=6) as pool:
                        replies = list(
                            pool.map(
                                client.get,
                                [
                                    base + "/v1/shops/thermal-example/catalog"
                                    for base in (first, second) * 10
                                ],
                            )
                        )
                self.assertEqual(sum(r.status_code == 200 for r in replies), 5)
                limited = [r for r in replies if r.status_code != 200]
                self.assertEqual(len(limited), 15)
                for reply in limited:
                    self.assertIn(reply.status_code, (429, 503), reply.text)
                    if reply.status_code == 429:
                        self.assertEqual(reply.json()["error"]["limit"], "deployment")
                        self.assertEqual(reply.json()["error"]["code"], "rate_limited")
                    else:
                        self.assertEqual(reply.json()["error"]["code"], "request_capacity")
                    self.assertGreater(int(reply.headers["Retry-After"]), 0)
                stop(second_process)
                start(429)
                self.assertEqual(operations.published.catalog("thermal-example"), before)
                self.assertEqual(
                    repository.read_quotas()["usage"]["deployment"]["retained_builds"], 1
                )
            finally:
                for process in children:
                    stop(process)
                for stream in streams:
                    stream.close()


if __name__ == "__main__":
    unittest.main()
