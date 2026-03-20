import pathlib
import sys
import unittest
from unittest.mock import AsyncMock, patch


PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app as checker_app  # noqa: E402


class NormalizeUidTests(unittest.TestCase):
    def test_normalize_uid_accepts_numeric_uid(self):
        self.assertEqual(checker_app.normalize_uid("100041775009544"), "100041775009544")

    def test_normalize_uid_rejects_non_numeric_or_short(self):
        self.assertEqual(checker_app.normalize_uid("abc123"), "")
        self.assertEqual(checker_app.normalize_uid("1234567"), "")


class ExtractUidTests(unittest.TestCase):
    def test_extract_uid_from_profile_query_param(self):
        uid = checker_app.extract_uid_from_url("https://www.facebook.com/profile.php?id=100041775009544")
        self.assertEqual(uid, "100041775009544")

    def test_extract_uid_from_people_pattern(self):
        uid = checker_app.extract_uid_from_url("https://www.facebook.com/people/Test-Name/100041775009544/")
        self.assertEqual(uid, "100041775009544")

    def test_extract_uid_from_numeric_path(self):
        uid = checker_app.extract_uid_from_url("facebook.com/100041775009544")
        self.assertEqual(uid, "100041775009544")

    def test_extract_uid_ignores_non_facebook_url(self):
        uid = checker_app.extract_uid_from_url("https://example.com/profile.php?id=100041775009544")
        self.assertEqual(uid, "")


class CookieHelperTests(unittest.TestCase):
    def test_parse_cookie_json_filters_blank_values(self):
        raw = '{"c_user":"1000", "xs":"abc", "empty":""}'
        self.assertEqual(checker_app.parse_cookie_json(raw), {"c_user": "1000", "xs": "abc"})

    def test_parse_cookie_pool_json_returns_only_valid_dicts(self):
        raw = '[{"c_user":"1000","xs":"abc"}, {"c_user":"", "xs":"x"}, "invalid"]'
        self.assertEqual(
            checker_app.parse_cookie_pool_json(raw),
            [{"c_user": "1000", "xs": "abc"}, {"xs": "x"}],
        )

    def test_build_cookie_candidates_uses_priority_and_dedup(self):
        with patch.object(checker_app, "DEFAULT_FB_COOKIE_POOL", [{"c_user": "2000", "xs": "pool"}]), patch.object(
            checker_app,
            "DEFAULT_FB_COOKIES",
            {"c_user": "3000", "xs": "default"},
        ):
            request_cookie = {"c_user": "1000", "xs": "req"}
            candidates = checker_app.build_cookie_candidates(
                request_cookie,
                [
                    {"c_user": "1000", "xs": "req"},
                    {"c_user": "2000", "xs": "pool"},
                ],
            )

        self.assertEqual(len(candidates), 3)
        self.assertEqual(candidates[0]["source"], "request_cookie")
        self.assertEqual(candidates[1]["source"], "request_pool_2")
        self.assertEqual(candidates[2]["source"], "env_default")

    def test_should_try_next_cookie_for_checkpoint_or_auth_wall(self):
        checkpoint = {"status": "CHECKPOINT", "reason": "checkpoint_detected"}
        auth_wall = {"status": "UNKNOWN", "reason": "mbasic_auth_wall"}
        stable_live = {"status": "LIVE", "reason": "ok"}
        self.assertTrue(checker_app.should_try_next_cookie(checkpoint))
        self.assertTrue(checker_app.should_try_next_cookie(auth_wall))
        self.assertFalse(checker_app.should_try_next_cookie(stable_live))


class SepayRelayHelperTests(unittest.TestCase):
    def test_build_forward_url_preserves_existing_query(self):
        self.assertEqual(
            checker_app.build_forward_url("https://example.com/exec?foo=1", "bar=2"),
            "https://example.com/exec?foo=1&bar=2",
        )
        self.assertEqual(
            checker_app.build_forward_url("https://example.com/exec", "bar=2"),
            "https://example.com/exec?bar=2",
        )

    def test_get_forwardable_sepay_headers_filters_non_whitelisted_headers(self):
        headers = {
            "Authorization": "Apikey test-secret",
            "Content-Type": "application/json",
            "Host": "uid-checker-service.onrender.com",
            "X-Api-Key": "abc123",
        }
        filtered = checker_app.get_forwardable_sepay_headers(headers)
        self.assertEqual(
            filtered,
            {
                "Authorization": "Apikey test-secret",
                "Content-Type": "application/json",
                "X-Api-Key": "abc123",
            },
        )

    def test_augment_query_string_with_sepay_key_from_authorization_header(self):
        self.assertEqual(
            checker_app.augment_query_string_with_sepay_key(
                "",
                {"Authorization": "Apikey test-secret"},
            ),
            "sepay_key=test-secret",
        )

    def test_augment_query_string_with_sepay_key_from_lowercase_authorization_header(self):
        self.assertEqual(
            checker_app.augment_query_string_with_sepay_key(
                "",
                {"authorization": "Apikey test-secret"},
            ),
            "sepay_key=test-secret",
        )

    def test_augment_query_string_keeps_existing_key(self):
        self.assertEqual(
            checker_app.augment_query_string_with_sepay_key(
                "foo=1&sepay_key=already-set",
                {"Authorization": "Apikey ignored-secret"},
            ),
            "foo=1&sepay_key=already-set",
        )


class TelegramRelayHelperTests(unittest.TestCase):
    def test_get_forwardable_telegram_headers_filters_non_whitelisted_headers(self):
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "TelegramBot (like TwitterBot)",
            "X-Telegram-Bot-Api-Secret-Token": "secret-token",
            "Host": "uid-checker-service.onrender.com",
        }
        filtered = checker_app.get_forwardable_telegram_headers(headers)
        self.assertEqual(
            filtered,
            {
                "Content-Type": "application/json",
                "User-Agent": "TelegramBot (like TwitterBot)",
                "X-Telegram-Bot-Api-Secret-Token": "secret-token",
            },
        )


class EndpointLogicTests(unittest.IsolatedAsyncioTestCase):
    async def test_health_returns_ok(self):
        result = await checker_app.health()
        self.assertTrue(result["ok"])
        self.assertEqual(result["service"], checker_app.APP_NAME)
        self.assertIn("sepayRelayReady", result)
        self.assertIn("telegramRelayReady", result)

    async def test_check_invalid_uid_returns_unknown(self):
        req = checker_app.CheckRequest(uid="not-a-valid-uid")
        result = await checker_app.check(req, x_api_key=None)
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["reason"], "invalid_uid")
        self.assertEqual(result["httpCode"], 0)

    async def test_check_rejects_invalid_api_key(self):
        req = checker_app.CheckRequest(uid="100041775009544")
        with patch.object(checker_app, "API_KEY", "secret-key"):
            with self.assertRaises(checker_app.HTTPException) as ctx:
                await checker_app.check(req, x_api_key="wrong-key")
        self.assertEqual(ctx.exception.status_code, 401)

    async def test_check_calls_checker_with_extracted_uid_and_cookie_alias(self):
        req = checker_app.CheckRequest(
            uid=None,
            url="https://facebook.com/100041775009544",
            proxy="http://proxy.local:8080",
            cookies={"c_user": "1000", "xs": "abc"},
            cookies_pool=[{"c_user": "2000", "xs": "pool"}],
        )
        mocked_check = AsyncMock(
            return_value={
                "uid": "100041775009544",
                "status": "LIVE",
                "reason": "mocked",
                "httpCode": 200,
            }
        )

        with patch.object(checker_app, "API_KEY", "secret-key"), patch.object(checker_app, "check_uid", mocked_check):
            result = await checker_app.check(req, x_api_key="secret-key")

        mocked_check.assert_awaited_once_with(
            "100041775009544",
            "http://proxy.local:8080",
            {"c_user": "1000", "xs": "abc"},
            [{"c_user": "2000", "xs": "pool"}],
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "LIVE")
        self.assertEqual(result["uid"], "100041775009544")

    async def test_forward_sepay_webhook_requires_target_url(self):
        with self.assertRaises(checker_app.HTTPException) as ctx:
            await checker_app.forward_sepay_webhook("POST", "", b"{}", {}, "")
        self.assertEqual(ctx.exception.status_code, 503)

    async def test_forward_sepay_webhook_returns_upstream_response(self):
        class FakeResponse:
            def __init__(self):
                self.status = 201
                self.headers = {"Content-Type": "application/json"}

            async def read(self):
                return b'{"ok":true}'

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class FakeSession:
            def __init__(self, *args, **kwargs):
                self.request_args = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def request(self, method, url, data=None, headers=None, allow_redirects=None):
                self.request_args = {
                    "method": method,
                    "url": url,
                    "data": data,
                    "headers": headers,
                    "allow_redirects": allow_redirects,
                }
                return FakeResponse()

        fake_session = FakeSession()

        with patch.object(checker_app.aiohttp, "ClientSession", return_value=fake_session):
            result = await checker_app.forward_sepay_webhook(
                "POST",
                "https://script.google.com/macros/s/abc/exec",
                b'{"id":1}',
                {"Authorization": "Apikey secret"},
                "",
            )

        self.assertEqual(fake_session.request_args["method"], "POST")
        self.assertEqual(
            fake_session.request_args["url"],
            "https://script.google.com/macros/s/abc/exec?sepay_key=secret",
        )
        self.assertEqual(fake_session.request_args["data"], b'{"id":1}')
        self.assertEqual(fake_session.request_args["headers"], {"Authorization": "Apikey secret"})
        self.assertTrue(fake_session.request_args["allow_redirects"])
        self.assertEqual(result["status_code"], 201)
        self.assertEqual(result["body"], b'{"ok":true}')
        self.assertEqual(result["content_type"], "application/json")

    async def test_forward_telegram_webhook_requires_target_url(self):
        with self.assertRaises(checker_app.HTTPException) as ctx:
            await checker_app.forward_telegram_webhook("POST", "", b"{}", {}, "bot=buff")
        self.assertEqual(ctx.exception.status_code, 503)

    async def test_forward_telegram_webhook_returns_upstream_response(self):
        class FakeResponse:
            def __init__(self):
                self.status = 200
                self.headers = {"Content-Type": "application/json"}

            async def read(self):
                return b'{"ok":true,"source":"telegram"}'

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

        class FakeSession:
            def __init__(self, *args, **kwargs):
                self.request_args = None

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            def request(self, method, url, data=None, headers=None, allow_redirects=None):
                self.request_args = {
                    "method": method,
                    "url": url,
                    "data": data,
                    "headers": headers,
                    "allow_redirects": allow_redirects,
                }
                return FakeResponse()

        fake_session = FakeSession()

        with patch.object(checker_app.aiohttp, "ClientSession", return_value=fake_session):
            result = await checker_app.forward_telegram_webhook(
                "POST",
                "https://script.google.com/macros/s/abc/exec",
                b'{"update_id":1}',
                {"Content-Type": "application/json"},
                "bot=uid",
            )

        self.assertEqual(fake_session.request_args["method"], "POST")
        self.assertEqual(
            fake_session.request_args["url"],
            "https://script.google.com/macros/s/abc/exec?bot=uid",
        )
        self.assertEqual(fake_session.request_args["data"], b'{"update_id":1}')
        self.assertEqual(fake_session.request_args["headers"], {"Content-Type": "application/json"})
        self.assertTrue(fake_session.request_args["allow_redirects"])
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["body"], b'{"ok":true,"source":"telegram"}')
        self.assertEqual(result["content_type"], "application/json")


if __name__ == "__main__":
    unittest.main()
