"""通过本地真实 HTTP 核对文档中的接口，不连接 Telegram。"""
import asyncio
import json
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import main
import uvicorn


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_routes_and_business_contract(self):
        main._pow_challenges.clear()
        main._pow_ip_hits.clear()
        config = uvicorn.Config(main.app, host="127.0.0.1", port=0,
                                log_level="error", access_log=False, proxy_headers=False)
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve())

        def request(path, body=None):
            headers = {"X-Forwarded-For": "198.51.100.100"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(f"http://127.0.0.1:{port}" + path, headers=headers,
                                         data=json.dumps(body).encode() if body is not None else None)
            try:
                with urllib.request.urlopen(req, timeout=3) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as error:
                return error.code, error.read()

        try:
            for _ in range(200):
                if server.started:
                    break
                if task.done():
                    await task
                    self.fail("Server stopped before startup")
                await asyncio.sleep(0.01)
            self.assertTrue(server.started)
            port = server.servers[0].sockets[0].getsockname()[1]
            with patch.object(main, "POW_MAXNUMBER", 10), patch.object(main, "ADMIN_TOKEN", "synthetic-admin"):
                status, body = await asyncio.to_thread(request, "/health")
                self.assertEqual((status, json.loads(body)), (200, {"status": "ok"}))
                record = json.loads((Path(__file__).resolve().parents[1] / "docs/dependencies.json").read_text(encoding="utf-8"))
                banner = ("@version " + record["vendored_javascript"]["version"]).encode()
                for route, expected in [("/", b'maxlength="4096"'), ("/sha256.min.js", banner)]:
                    status, body = await asyncio.to_thread(request, route)
                    self.assertEqual(status, 200)
                    self.assertIn(expected, body)
                _, body = await asyncio.to_thread(request, "/api/pow")
                data = json.loads(body)["data"]
                self.assertGreater(data["expires_at"], 0)
                challenge = main._pow_challenges[data["challenge_id"]]
                self.assertEqual(challenge["ip"], "127.0.0.1")
                _, body = await asyncio.to_thread(request, "/api/send-code", {
                    "phone": "invalid", "challenge_id": data["challenge_id"], "nonce": challenge["number"],
                })
                self.assertEqual(json.loads(body)["code"], main.E_PHONE_INVALID)
                _, body = await asyncio.to_thread(request, "/api/create-app", {"request_id": "missing", "code": "AbCD123"})
                self.assertEqual(json.loads(body)["code"], main.E_STATE_EXPIRED)
                self.assertEqual((await asyncio.to_thread(request, "/api/stats"))[0], 404)
                status, body = await asyncio.to_thread(request, "/api/stats?token=synthetic-admin")
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["code"], 0)
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=5)


if __name__ == "__main__":
    unittest.main()
