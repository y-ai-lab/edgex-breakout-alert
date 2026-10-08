"""No external delivery: exercise actual API authority and outbound boundary."""
import base64
import asyncio
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from httpx import ASGITransport, AsyncClient
from analysis_terminal import push_security as security, server

# Legacy storage fixtures restore sys.modules after importing FastAPI.
HTTPException = security.HTTPException


def subscription(endpoint="https://web.push.apple.com/security-test", auth=b"test-only-auth16"):
    public = ec.derive_private_key(1, ec.SECP256R1()).public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    encode = lambda b: base64.urlsafe_b64encode(b).decode().rstrip("=")
    return {"endpoint": endpoint, "keys": {"p256dh": encode(public), "auth": encode(auth)}}


class PushSecurityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        for name, value in (("DB_PATH", Path(self.directory.name) / "test.db"), ("VAPID_PRIVATE_KEY", "")):
            p = patch.object(server, name, value)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(security, "LIMITER", security.RateLimiter())
        p.start()
        self.addCleanup(p.stop)
        server._init_db()
        self.sub = subscription()
        server._save_push_subscription(server.PushSubscriptionRequest(subscription=self.sub))
        self.token = security.management_token(self.sub)
        self.header = {"Authorization": "Bearer " + self.token}
        self.client = AsyncClient(transport=ASGITransport(app=server.app), base_url="https://test")
        self.addAsyncCleanup(self.client.aclose)

    async def test_endpoint_alone_never_authorizes_read_or_mutation(self):
        before = dict(server._get_push_subscription(self.sub["endpoint"]))
        get = await self.client.get("/api/push/preferences", params={"endpoint": self.sub["endpoint"]})
        self.assertEqual(get.status_code, 401)
        with patch.object(server, "_send_push_sync", Mock()) as send:
            for route in ("preferences", "unsubscribe", "test"):
                r = await self.client.post("/api/push/" + route, json={"endpoint": self.sub["endpoint"]})
                self.assertEqual(r.status_code, 401)
            send.assert_not_called()
        self.assertEqual(dict(server._get_push_subscription(self.sub["endpoint"])), before)

    async def test_existing_browser_bootstrap_is_silent_and_preserves_every_field(self):
        server._update_push_preferences(server.PushPreferenceRequest(endpoint=self.sub["endpoint"], candidate_alerts=False, quiet_start="23:00", quiet_end="07:00", snooze_until_ms=2000000000000))
        before = dict(server._get_push_subscription(self.sub["endpoint"]))
        with patch.object(server, "_send_push_sync", Mock()) as send:
            for _ in range(2):
                r = await self.client.post("/api/push/session", json={"subscription": self.sub})
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["management_token"], self.token)
                server._init_db()
            send.assert_not_called()
        self.assertEqual(dict(server._get_push_subscription(self.sub["endpoint"])), before)
        prefs = await self.client.get("/api/push/preferences", params={"endpoint": self.sub["endpoint"]}, headers=self.header)
        self.assertEqual(prefs.status_code, 200)
        self.assertFalse(prefs.json()["candidate_alerts"])

    async def test_wrong_keys_cannot_bootstrap_or_overwrite_subscription(self):
        forged = subscription(auth=b"0123456789abcdef")
        before = dict(server._get_push_subscription(self.sub["endpoint"]))
        with patch.object(server, "_push_enabled", return_value=True), patch.object(server, "_send_push_sync", Mock()) as send:
            for route in ("session", "subscribe"):
                r = await self.client.post("/api/push/" + route, json={"subscription": forged})
                self.assertEqual(r.status_code, 403)
            send.assert_not_called()
        self.assertEqual(dict(server._get_push_subscription(self.sub["endpoint"])), before)

    async def test_bearer_is_scoped_to_its_subscription(self):
        other = subscription("https://fcm.googleapis.com/fcm/send/other")
        server._save_push_subscription(server.PushSubscriptionRequest(subscription=other))
        for token in ("bad", "0" * 64, security.management_token(other)):
            r = await self.client.post("/api/push/unsubscribe", json={"endpoint": self.sub["endpoint"]}, headers={"Authorization": "Bearer " + token})
            self.assertEqual(r.status_code, 403)
        self.assertEqual(server._subscription_count(), 2)

    async def test_owned_preference_update_and_unsubscribe(self):
        r = await self.client.post("/api/push/preferences", json={"endpoint": self.sub["endpoint"], "candidate_alerts": False}, headers=self.header)
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["candidate_alerts"])
        r = await self.client.post("/api/push/unsubscribe", json={"endpoint": self.sub["endpoint"]}, headers=self.header)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(server._subscription_count(), 0)
        r = await self.client.post("/api/push/session", json={"subscription": self.sub})
        self.assertEqual(r.status_code, 403)

    async def test_cross_site_blocks_even_a_valid_bearer(self):
        for headers in ({"Origin": "https://attacker.invalid"}, {"Sec-Fetch-Site": "cross-site"}):
            r = await self.client.post("/api/push/unsubscribe", json={"endpoint": self.sub["endpoint"]}, headers={**self.header, **headers})
            self.assertEqual(r.status_code, 403)
        r = await self.client.post("/api/push/preferences", json={"endpoint": self.sub["endpoint"]}, headers={**self.header, "Origin": security.PUBLIC_ORIGIN})
        self.assertEqual(r.status_code, 200)

    async def test_oversized_streamed_body_never_reaches_registration(self):
        async def chunks():
            yield b'{"subscription":'
            yield b"x" * security.MAX_BODY
        r = await self.client.post("/api/push/subscribe", content=chunks())
        self.assertEqual(r.status_code, 413)
        self.assertEqual(server._subscription_count(), 1)

    async def test_slow_body_times_out_before_handler(self):
        app = AsyncMock()
        middleware = security.BrowserSecurityMiddleware(app)
        messages = []
        async def receive():
            await asyncio.Event().wait()
        async def send(message):
            messages.append(message)
        with patch.object(security, "BODY_TIMEOUT", .001):
            await middleware({"type": "http", "method": "POST", "path": "/api/push/session", "headers": [], "client": ("test", 0)}, receive, send)
        self.assertEqual(messages[0]["status"], 408)
        app.assert_not_called()

    async def test_session_and_test_rate_limits(self):
        with patch.object(server, "_send_push_sync", return_value=True) as send:
            for _ in range(3):
                r = await self.client.post("/api/push/test", json={"endpoint": self.sub["endpoint"]}, headers=self.header)
                self.assertEqual(r.status_code, 200)
            r = await self.client.post("/api/push/test", json={"endpoint": self.sub["endpoint"]}, headers=self.header)
            self.assertEqual(r.status_code, 429)
            self.assertEqual(r.headers["retry-after"], "60")
            self.assertEqual(send.call_count, 3)
        for _ in range(60):
            await self.client.post("/api/push/session", json={"subscription": self.sub})
        r = await self.client.post("/api/push/session", json={"subscription": self.sub})
        self.assertEqual(r.status_code, 429)

    async def test_capacity_keeps_existing_subscription_and_migration(self):
        for n in range(31):
            server._save_push_subscription(server.PushSubscriptionRequest(subscription=subscription(f"https://web.push.apple.com/test-{n}")))
        with self.assertRaises(HTTPException) as e:
            server._save_push_subscription(server.PushSubscriptionRequest(subscription=subscription("https://web.push.apple.com/overflow")))
        self.assertEqual(e.exception.status_code, 429)
        server._init_db()
        self.assertEqual(server._subscription_count(), 32)
        r = await self.client.post("/api/push/session", json={"subscription": self.sub})
        self.assertEqual(r.status_code, 200)

    async def test_approved_endpoint_bad_or_missing_keys_are_rejected(self):
        for keys in ({}, {"auth": "bad", "p256dh": "bad"}, {**self.sub["keys"], "p256dh": base64.urlsafe_b64encode(b"\4" + b"\0" * 64).decode()}, {**self.sub["keys"], "auth": None}):
            r = await self.client.post("/api/push/session", json={"subscription": {"endpoint": self.sub["endpoint"], "keys": keys}})
            self.assertEqual(r.status_code, 400)

    async def test_new_subscription_returns_scoped_token_and_repeat_does_not_send(self):
        sub = subscription("https://updates.push.services.mozilla.com/wpush/v2/new")
        with patch.object(server, "_push_enabled", return_value=True), patch.object(server, "_send_push_sync", return_value=True) as send:
            for _ in range(2):
                r = await self.client.post("/api/push/subscribe", json={"subscription": sub})
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.json()["management_token"], security.management_token(sub))
            self.assertEqual(send.call_count, 1)
        self.assertEqual(server._subscription_count(), 2)

    async def test_custom_alert_management_also_requires_ownership(self):
        r = await self.client.get("/api/custom-alerts", params={"endpoint": self.sub["endpoint"]})
        self.assertEqual(r.status_code, 401)
        r = await self.client.post("/api/custom-alerts/delete", json={"endpoint": self.sub["endpoint"], "alert_id": 1})
        self.assertEqual(r.status_code, 401)

    async def test_pages_have_matching_unique_nonce_and_no_inline_script_permission(self):
        nonces = []
        for path in ("/", "/", "/btc"):
            r = await self.client.get(path)
            self.assertEqual(r.status_code, 200)
            nonce = re.search(r'<script nonce="([^"]+)"', r.text).group(1)
            nonces.append(nonce)
            self.assertIn("script-src 'nonce-" + nonce + "'", r.headers["content-security-policy"])
            self.assertEqual(r.headers["x-frame-options"], "DENY")
            self.assertEqual(r.headers["x-content-type-options"], "nosniff")
            self.assertEqual(r.headers["referrer-policy"], "no-referrer")
            self.assertIn("no-store", r.headers["cache-control"])
            self.assertNotIn("script-src 'unsafe-inline'", r.headers["content-security-policy"])
        self.assertEqual(len(set(nonces)), 3)
        r = await self.client.get("/api/push/config")
        self.assertEqual(r.headers["cache-control"], "no-store")
        self.assertNotIn(self.token, r.text)

    def test_ssrf_and_host_url_tricks_fail_before_transport_or_storage(self):
        for endpoint in ("http://fcm.googleapis.com/fcm/send/x", "https://127.0.0.1/x", "https://169.254.169.254/x", "https://localhost/x", "https://fcm.googleapis.com.evil.invalid/fcm/send/x", "https://fcm.googleapis.com@evil.invalid/fcm/send/x", "https://fcm.googleapis.com:443/fcm/send/x", "https://fcm.googleapis.com/fcm/send/x?redirect=x", "https://web.push.apple.com/x#fragment", "https://web.push.apple.com/", "https://evil.invalid/x", "https://web.push.apple.com\\@evil.invalid/x"):
            with self.subTest(endpoint=endpoint), self.assertRaises(HTTPException):
                security.subscription_identity(subscription(endpoint))
        self.assertEqual(server._subscription_count(), 1)

    def test_all_supported_browser_providers(self):
        for endpoint in ("https://fcm.googleapis.com/fcm/send/x", "https://fcm.googleapis.com/wp/x", "https://updates.push.services.mozilla.com/wpush/v2/x", "https://web.push.apple.com/x"):
            self.assertEqual(security.validate_endpoint(endpoint), endpoint)

    def test_transport_never_redirects_or_disables_tls(self):
        with patch.object(security.requests.Session, "request", return_value=Mock(status_code=302)) as request:
            with security.ProviderSession() as session:
                session.post(self.sub["endpoint"], allow_redirects=True, verify=False)
                self.assertFalse(request.call_args.kwargs["allow_redirects"])
                self.assertTrue(request.call_args.kwargs["verify"])
                with self.assertRaises(HTTPException):
                    session.post("http://127.0.0.1/private")
                self.assertEqual(request.call_count, 1)

    def test_real_push_encryption_uses_guarded_transport_without_network(self):
        # Exercise the installed library, not only our mocked function boundary.
        from py_vapid import Vapid02
        from pywebpush import webpush
        vapid = Vapid02()
        vapid.generate_keys()
        path = Path(self.directory.name) / "test-only-vapid.pem"
        vapid.save_key(str(path))
        row = dict(server._get_push_subscription(self.sub["endpoint"]))
        response = Mock(status_code=201, text="", headers={})
        with patch.object(server, "webpush", webpush), patch.object(server, "VAPID_KEY_PATH", path), patch.object(server, "_push_enabled", return_value=True), patch.object(security.requests.Session, "request", return_value=response) as request:
            self.assertTrue(server._send_push_sync(row, {"notification_kind": "READY", "title": "test"}))
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.args[1], self.sub["endpoint"])
            self.assertFalse(request.call_args.kwargs["allow_redirects"])
            self.assertTrue(request.call_args.kwargs["verify"])
            self.assertIsInstance(request.call_args.kwargs["data"], bytes)
        self.assertIsNotNone(server._get_push_subscription(self.sub["endpoint"])["last_success_ms"])

    def test_existing_unsafe_destination_is_preserved_but_never_sent(self):
        info = subscription("http://169.254.169.254/metadata")
        row = {"endpoint": info["endpoint"], "payload": json.dumps(info)}
        with patch.object(server, "_push_enabled", return_value=True), patch.object(server, "webpush", Mock()) as send, redirect_stdout(io.StringIO()) as log:
            self.assertFalse(server._send_push_sync(row, {"notification_kind": "READY"}))
            send.assert_not_called()
            self.assertNotIn(info["endpoint"], log.getvalue())
            self.assertNotIn(info["keys"]["auth"], log.getvalue())

    def test_delivery_failure_logs_no_raw_exception_or_keys(self):
        row = dict(server._get_push_subscription(self.sub["endpoint"]))
        with patch.object(server, "_push_enabled", return_value=True), patch.object(server, "webpush", side_effect=RuntimeError("private-response-secret")), redirect_stdout(io.StringIO()) as log:
            self.assertFalse(server._send_push_sync(row, {"notification_kind": "READY"}))
            self.assertNotIn("private-response-secret", log.getvalue())
            self.assertNotIn(self.sub["endpoint"], log.getvalue())
        self.assertEqual(dict(server._get_push_subscription(self.sub["endpoint"])), row)

    def test_rate_limit_reset_and_bounded_memory(self):
        limiter = security.RateLimiter()
        for _ in range(3):
            limiter.check("x", 3, now=1)
        with self.assertRaises(HTTPException):
            limiter.check("x", 3, now=2)
        limiter.check("x", 3, now=61)
        for n in range(2047):
            limiter.check(str(n), 1, now=61)
        with self.assertRaises(HTTPException):
            limiter.check("overflow", 1, now=61)
        self.assertEqual(len(limiter.counts), 2048)
        limiter.check("next-minute", 1, now=121)
        self.assertEqual(len(limiter.counts), 1)

    async def test_public_rules_do_not_expose_account_values_or_allow_orders(self):
        config = server.live_execution.Config(risk_pct="3", daily_loss_usdc="DISABLED")
        with patch.object(server, "_live_execution_config", config):
            r = await self.client.get("/api/live-execution")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["safety"]["risk_budget_pct"], 3)
        self.assertFalse(r.json()["safety"]["loss_cap_guaranteed"])
        self.assertFalse(r.json()["safety"]["daily_loss_stop_enabled"])
        self.assertFalse(r.json()["account_details_exposed"])
        self.assertNotIn("equity", r.json())
        r = await self.client.post("/api/live-execution", json={"action": "arm"})
        self.assertEqual(r.status_code, 405)


if __name__ == "__main__":
    unittest.main()
