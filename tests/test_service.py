"""离线回归测试：不访问 Telegram；代理与会话均为模拟配置。"""
import asyncio
import contextlib
import json
import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
import main as service
import mytelegram as telegram


class Request:
    def __init__(self, body=None, ip="192.0.2.1", headers=None):
        self.body = body or {}
        self.client = SimpleNamespace(host=ip)
        self.headers = headers or {}

    async def json(self):
        return self.body


def response_code(response):
    return json.loads(response.body)["code"]


def client():
    return SimpleNamespace(
        send_code=AsyncMock(return_value="synthetic"),
        login=AsyncMock(),
        create_app=AsyncMock(return_value=telegram.CreateResult(123, "a" * 32, True)),
        close=AsyncMock(),
    )


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        for data in (service._state, service._pow_challenges, service._pow_ip_hits,
                     service._ip_hits, service._phone_last):
            data.clear()
        service._flow_slots = asyncio.BoundedSemaphore(2)
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(patch.object(service, "build_proxy", return_value="socks5h://synthetic"))
        self.stack.enter_context(patch.object(service, "POW_MAXNUMBER", 10))
        self.addCleanup(self.stack.close)

    async def make_flow(self, instance=None):
        instance = instance or client()
        await service._flow_slots.acquire()
        state = dict(client=instance, slot=service._flow_slots, created_at=time.time(),
                     phone="+12025550123", random_hash="synthetic", code_attempts=0,
                     processing=False, closed=False)
        service._state["flow"] = state
        return instance, state

    def create_request(self):
        return Request(dict(request_id="flow", code="AbCde123"))

    async def send(self, instance):
        with patch.object(service, "TelegramWebClient", return_value=instance), \
             patch.object(service, "_check_pow", return_value=True):
            return await service.send_code(Request(dict(phone="+12025550123")))

    async def test_duplicate_create_is_rejected_and_slot_released_once(self):
        instance, state = await self.make_flow()
        entered, finish = asyncio.Event(), asyncio.Event()

        async def login(*args):
            entered.set()
            await finish.wait()

        instance.login.side_effect = login
        task = asyncio.create_task(service.create_app(self.create_request()))
        await entered.wait()
        duplicate = await service.create_app(self.create_request())
        self.assertEqual(response_code(duplicate), service.E_BUSY)
        instance.close.assert_not_awaited()
        finish.set()
        self.assertEqual(response_code(await task), 0)
        instance.login.assert_awaited_once()
        instance.close.assert_awaited_once()
        await service._close_flow(state)
        self.assertEqual(service._flow_slots._value, 2)

    async def test_cleanup_does_not_close_an_active_submission(self):
        instance, state = await self.make_flow()
        state["processing"] = True
        state["created_at"] -= service.TTL_SECONDS + 1
        await service._cleanup_expired()
        instance.close.assert_not_awaited()
        self.assertIn("flow", service._state)
        state["processing"] = False
        await service._cleanup_expired()
        self.assertNotIn("flow", service._state)
        instance.close.assert_awaited_once()
        self.assertEqual(service._flow_slots._value, 2)

    async def test_send_success_retains_session_until_create_finishes(self):
        instance = client()
        response = await self.send(instance)
        request_id = json.loads(response.body)["data"]["request_id"]
        self.assertEqual(service._flow_slots._value, 1)
        instance.close.assert_not_awaited()
        result = await service.create_app(Request(dict(request_id=request_id, code="AbCde123")))
        self.assertEqual(response_code(result), 0)
        instance.close.assert_awaited_once()
        self.assertEqual(service._flow_slots._value, 2)

    async def test_unexpected_send_error_releases_resources(self):
        instance = client()
        instance.send_code.side_effect = ValueError("synthetic")
        self.assertEqual(response_code(await self.send(instance)), service.E_TELEGRAM)
        instance.close.assert_awaited_once()
        self.assertEqual(service._flow_slots._value, 2)
        self.assertFalse(service._state)

    async def test_proxy_construction_error_releases_slot(self):
        with patch.object(service, "build_proxy", side_effect=ValueError("synthetic")):
            self.assertEqual(response_code(await self.send(client())), service.E_TELEGRAM)
        self.assertEqual(service._flow_slots._value, 2)

    async def test_network_error_does_not_retry_or_change_proxy(self):
        instance = client()
        instance.send_code.side_effect = telegram.MyTelegramError("network", "synthetic")
        self.assertEqual(response_code(await self.send(instance)), service.E_NETWORK)
        instance.send_code.assert_awaited_once()
        instance.close.assert_awaited_once()

    async def test_cancelled_send_releases_resources(self):
        instance, entered = client(), asyncio.Event()

        async def send(*args):
            entered.set()
            await asyncio.Event().wait()

        instance.send_code.side_effect = send
        task = asyncio.create_task(self.send(instance))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        instance.close.assert_awaited_once()
        self.assertEqual(service._flow_slots._value, 2)
        self.assertFalse(service._state)

    async def test_create_error_and_cancellation_release_resources(self):
        for error in (ValueError("synthetic"), asyncio.CancelledError()):
            with self.subTest(error=type(error).__name__):
                instance, _ = await self.make_flow()
                instance.login.side_effect = error
                if isinstance(error, asyncio.CancelledError):
                    with self.assertRaises(asyncio.CancelledError):
                        await service.create_app(self.create_request())
                else:
                    self.assertEqual(response_code(await service.create_app(self.create_request())), service.E_TELEGRAM)
                instance.close.assert_awaited_once()
                self.assertFalse(service._state)
                self.assertEqual(service._flow_slots._value, 2)

    async def test_invalid_code_retains_only_allowed_attempts(self):
        instance, _ = await self.make_flow()
        instance.login.side_effect = telegram.MyTelegramError("code_invalid", "synthetic")
        with patch.object(service, "CODE_ATTEMPT_LIMIT", 2):
            first = await service.create_app(self.create_request())
            self.assertEqual(response_code(first), service.E_CODE_INVALID)
            instance.close.assert_not_awaited()
            self.assertFalse(service._state["flow"]["processing"])
            second = await service.create_app(self.create_request())
            self.assertIn("2", json.loads(second.body)["message"])
            instance.close.assert_awaited_once()
            self.assertFalse(service._state)
            self.assertEqual(service._flow_slots._value, 2)

    async def test_close_failure_still_returns_slot(self):
        instance, state = await self.make_flow()
        instance.close.side_effect = RuntimeError("synthetic")
        await service._close_flow(state)
        await service._close_flow(state)
        self.assertEqual(service._flow_slots._value, 2)
        instance.close.assert_awaited_once()

    async def test_pow_spam_is_limited_without_blocking_another_ip(self):
        for _ in range(40):
            await service.pow_challenge(Request())
        self.assertEqual(len(service._pow_challenges), service.POW_IP_PENDING_LIMIT)
        self.assertEqual(response_code(await service.pow_challenge(Request())), service.E_IP_LIMITED)
        self.assertEqual(response_code(await service.pow_challenge(Request(ip="192.0.2.2"))), 0)

    async def test_pow_rate_limit_applies_even_after_challenges_are_consumed(self):
        with patch.object(service, "POW_IP_LIMIT", 2):
            for _ in range(2):
                response = await service.pow_challenge(Request())
                cid = json.loads(response.body)["data"]["challenge_id"]
                answer = service._pow_challenges[cid]["number"]
                self.assertTrue(service._check_pow(cid, str(answer), "192.0.2.1"))
            self.assertEqual(response_code(await service.pow_challenge(Request())), service.E_IP_LIMITED)

    async def test_pow_is_ip_bound_single_use_and_expires(self):
        response = await service.pow_challenge(Request())
        cid = json.loads(response.body)["data"]["challenge_id"]
        answer = str(service._pow_challenges[cid]["number"])
        self.assertFalse(service._check_pow(cid, answer, "192.0.2.2"))
        self.assertTrue(service._check_pow(cid, answer, "192.0.2.1"))
        self.assertFalse(service._check_pow(cid, answer, "192.0.2.1"))
        await service.pow_challenge(Request())
        for item in service._pow_challenges.values():
            item["created"] -= service.POW_TTL + 1
        await service._cleanup_expired()
        self.assertFalse(service._pow_challenges)

    async def test_numeric_zero_nonce_is_accepted_by_send_route(self):
        with patch.object(service.random, "SystemRandom") as random:
            random.return_value.randrange.return_value = 0
            response = await service.pow_challenge(Request())
        cid = json.loads(response.body)["data"]["challenge_id"]
        with patch.object(service, "TelegramWebClient", return_value=client()):
            sent = await service.send_code(Request(dict(phone="+12025550123", challenge_id=cid, nonce=0)))
        self.assertEqual(response_code(sent), 0)

    async def test_pow_pool_limit_is_exact(self):
        with patch.object(service, "POW_POOL_LIMIT", 1):
            self.assertEqual(response_code(await service.pow_challenge(Request())), 0)
            self.assertEqual(response_code(await service.pow_challenge(Request(ip="192.0.2.2"))), service.E_BUSY)
            self.assertEqual(len(service._pow_challenges), 1)

    async def test_public_expiry_matches_configured_server_lifetime(self):
        with patch.object(service, "POW_TTL", 45), patch.object(service, "TTL_SECONDS", 75):
            response = await service.pow_challenge(Request())
            data = json.loads(response.body)["data"]
            self.assertEqual(data["expires_at"], service._pow_challenges[data["challenge_id"]]["created"] + 45)
            sent = await self.send(client())
            data = json.loads(sent.body)["data"]
            self.assertEqual(data["expires_at"], service._state[data["request_id"]]["created_at"] + 75)

    async def test_malformed_code_never_reaches_telegram(self):
        instance, _ = await self.make_flow()
        response = await service.create_app(Request(dict(request_id="flow", code="???")))
        self.assertEqual(response_code(response), service.E_CODE_INVALID)
        instance.login.assert_not_awaited()

    def test_forwarded_header_cannot_override_asgi_client_ip(self):
        request = Request(headers={"x-forwarded-for": "198.51.100.100"})
        self.assertEqual(service._client_ip(request), "192.0.2.1")

    def test_complete_messages_and_plain_codes_are_extracted(self):
        for value in ("AbCD123", "Code is AbCD123", "验证码：AbCD123", "Your login code: AbCD123. Do not share it."):
            self.assertEqual(service.extract_code(value), "AbCD123")
        self.assertEqual(service.extract_code("codeAbCD123"), "codeAbCD123")
        with self.assertRaises(telegram.MyTelegramError):
            service.extract_code("unrecognizable message")


class ParsingTests(unittest.TestCase):
    def test_dom_parser_handles_attribute_order_and_rejects_ambiguous_ids(self):
        for attrs in ('id="api_hash_value" data-value="' + "a" * 32 + '"',
                      "data-value='" + "a" * 32 + "' id='api_hash_value'"):
            page = '<span class="vv">123</span><div ' + attrs + "></div>"
            self.assertEqual(telegram._extract_credentials(page), (123, "a" * 32))
            self.assertIsNone(telegram._extract_credentials(page + '<span class="vv">456</span>'))
        parser = telegram._parse_app_page("<input value='form' type='hidden' name='hash'>")
        self.assertEqual(parser.form_hashes, ["form"])

    def test_debug_files_and_errors_use_the_same_redaction(self):
        secret = "a" * 32
        html = f'<div data-value="{secret}" id="api_hash_value"></div> +12025550123'
        with patch.object(telegram, "Path") as path:
            output = MagicMock()
            path.return_value.parent.__truediv__.return_value.__truediv__.return_value = output
            telegram._dump_debug_page(html, "synthetic")
            written = output.write_text.call_args.args[0]
        self.assertNotIn(secret, written)
        self.assertNotIn("+12025550123", written)
        self.assertEqual(telegram.MyTelegramError("telegram_error", "synthetic", html).original, written)

    def test_bad_numeric_configuration_fails_explicitly(self):
        for value in ("invalid", "0", "-1"):
            with patch.dict(os.environ, {"TEST_POSITIVE": value}):
                with self.assertRaises(ValueError):
                    service._int_env("TEST_POSITIVE", 5)

    def test_proxy_has_one_configuration_path_and_never_connects_directly(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                service.build_proxy("session")
        with patch.dict(os.environ, {"PROXY_HOST": "proxy.invalid", "PROXY_PORT": "1080",
                                   "PROXY_USER_TEMPLATE": "user-{session}", "PROXY_PASS": "p@ss",
                                   "PROXY": "http://ignored.invalid", "PROXY_USER": "ignored"}, clear=True):
            self.assertEqual(service.build_proxy("abc"), "socks5h://user-abc:p%40ss@proxy.invalid:1080")


class TelegramClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_posts_once_then_uses_configuration_page(self):
        instance = object.__new__(telegram.TelegramWebClient)
        credentials = '<span class="vv">123</span><div id="api_hash_value" data-value="' + "a" * 32 + '"></div>'
        instance._request = AsyncMock(side_effect=[
            SimpleNamespace(text='<input name="hash" value="form">', status_code=200),
            SimpleNamespace(text="", status_code=200),
            SimpleNamespace(text=credentials, status_code=200),
        ])
        result = await instance.create_app()
        self.assertEqual((result.api_id, result.existed), (123, False))
        self.assertEqual([call.args for call in instance._request.await_args_list],
                         [("GET", "/apps"), ("POST", "/apps/create"), ("GET", "/apps")])

    async def test_existing_app_never_posts_a_new_application(self):
        instance = object.__new__(telegram.TelegramWebClient)
        page = '<span class="vv">123</span><div id="api_hash_value" data-value="' + "a" * 32 + '"></div>'
        instance._request = AsyncMock(return_value=SimpleNamespace(text=page, status_code=200))
        self.assertTrue((await instance.create_app()).existed)
        instance._request.assert_awaited_once_with("GET", "/apps")


if __name__ == "__main__":
    unittest.main()
