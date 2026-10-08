"""Subscription-scoped authority and bounded, provider-only Push transport.

Possession of the browser's existing subscription keys authorizes management.
No registration reset, global trading authority, or stored-key migration occurs.
"""
from __future__ import annotations

import base64
import asyncio
import hashlib
import hmac
import re
import secrets
import threading
import time
from urllib.parse import urlsplit

import requests
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import HTTPException
from starlette.responses import JSONResponse
from starlette.responses import HTMLResponse

PUBLIC_ORIGIN = "https://edgex-analysis-terminal-production.up.railway.app"
MAX_BODY = 16384
BODY_TIMEOUT = 10
PROVIDERS = {
    "fcm.googleapis.com": ("/fcm/send/", "/wp/"),
    "updates.push.services.mozilla.com": ("/wpush/v1/", "/wpush/v2/"),
    "web.push.apple.com": ("/",),
}
CSP_BASE = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; worker-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"


def html_response(html):
    nonce = secrets.token_urlsafe(24)
    return HTMLResponse(html.replace("<script>", f'<script nonce="{nonce}">'), headers={
        "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "Pragma": "no-cache",
        "Content-Security-Policy": CSP_BASE + f"; script-src 'nonce-{nonce}'",
    })


def validate_endpoint(endpoint):
    if not isinstance(endpoint, str) or not 10 <= len(endpoint) <= 4096:
        raise HTTPException(400, "Invalid Push endpoint")
    if any(ord(c) <= 32 or ord(c) == 127 or c == "\\" for c in endpoint):
        raise HTTPException(400, "Invalid Push endpoint")
    try:
        url = urlsplit(endpoint)
        prefixes = PROVIDERS.get(url.hostname, ())
        valid = (
            url.scheme == "https" and url.netloc == url.hostname
            and not url.query and not url.fragment
            and re.fullmatch(r"/[A-Za-z0-9_./%~:=\-]+", url.path)
            and any(url.path.startswith(p) and len(url.path) > len(p) for p in prefixes)
        )
    except ValueError:
        valid = False
    if not valid:
        raise HTTPException(400, "Unsupported Push endpoint")
    return endpoint


def _decode(value, size):
    try:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+={0,2}", value):
            raise ValueError()
        raw = base64.b64decode(value.rstrip("=") + "=" * (-len(value.rstrip("=")) % 4), altchars=b"-_", validate=True)
        if len(raw) != size:
            raise ValueError()
        return raw
    except (ValueError, TypeError):
        raise HTTPException(400, "Invalid Push keys") from None


def subscription_identity(subscription):
    if not isinstance(subscription, dict):
        raise HTTPException(400, "Invalid Push subscription")
    endpoint = validate_endpoint(subscription.get("endpoint"))
    keys = subscription.get("keys")
    if not isinstance(keys, dict):
        raise HTTPException(400, "Invalid Push keys")
    public = _decode(keys.get("p256dh"), 65)
    auth = _decode(keys.get("auth"), 16)
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public)
    except ValueError:
        raise HTTPException(400, "Invalid Push keys") from None
    return endpoint, public, auth


def management_token(subscription):
    endpoint, public, auth = subscription_identity(subscription)
    return hmac.new(auth, b"edgex-push-management-v1\0" + endpoint.encode() + b"\0" + public, hashlib.sha256).hexdigest()


def prove_subscription(stored, supplied):
    # Compare derived values instead of accepting endpoint possession alone.
    if not hmac.compare_digest(management_token(stored), management_token(supplied)):
        raise HTTPException(403, "Push ownership required")


def authorize(stored, authorization):
    if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Push ownership required")
    token = authorization[7:]
    if not re.fullmatch(r"[0-9a-f]{64}", token) or not hmac.compare_digest(management_token(stored), token):
        raise HTTPException(403, "Push ownership required")


class ProviderSession(requests.Session):
    """Recheck every send, retain TLS verification, and never follow redirects."""
    def request(self, method, url, **kwargs):
        validate_endpoint(url)
        kwargs.update(allow_redirects=False, verify=True)
        return super().request(method, url, **kwargs)


class RateLimiter:
    def __init__(self):
        self.counts = {}
        self.lock = threading.Lock()

    def check(self, key, limit, *, now=None):
        minute = int((time.monotonic() if now is None else now) // 60)
        digest = hashlib.sha256(key.encode()).digest()
        with self.lock:
            # Bounded memory. Keep the global bucket when refusing new keys.
            if len(self.counts) >= 2048:
                self.counts = {k: v for k, v in self.counts.items() if v[0] == minute}
            previous = self.counts.get(digest, (minute, 0))
            count = previous[1] if previous[0] == minute else 0
            if count >= limit or (digest not in self.counts and len(self.counts) >= 2048):
                raise HTTPException(429, "Too many requests", headers={"Retry-After": "60"})
            self.counts[digest] = (minute, count + 1)


LIMITER = RateLimiter()


class BrowserSecurityMiddleware:
    """Small-body / cross-site mutation guard, plus response hardening.

    Subscription authorization remains in handlers; same-origin alone is not auth.
    Limits are process-local: the production service has one replica.
    """
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        headers = dict(scope.get("headers", []))
        sensitive = path.startswith("/api/push/") or path.startswith("/api/custom-alerts") or path == "/api/watchlist"

        async def hardened(message):
            if message["type"] == "http.response.start":
                extra = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"x-frame-options", b"DENY"),
                    (b"referrer-policy", b"no-referrer"),
                ]
                if not any(k.lower() == b"content-security-policy" for k, _ in message.get("headers", [])):
                    extra.append((b"content-security-policy", (CSP_BASE + "; script-src 'self'").encode()))
                if path.startswith("/api/"):
                    extra.append((b"cache-control", b"no-store"))
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)

        try:
            if sensitive and scope["method"] in {"POST", "PUT", "PATCH", "DELETE"}:
                origin = headers.get(b"origin")
                if (origin is not None and origin != PUBLIC_ORIGIN.encode()) or headers.get(b"sec-fetch-site") == b"cross-site":
                    raise HTTPException(403, "Cross-site request rejected")
                LIMITER.check("global", 300)
                client = (scope.get("client") or ("unknown", 0))[0]
                LIMITER.check("client:" + client, 60)
                body = bytearray()
                try:
                    async with asyncio.timeout(BODY_TIMEOUT):
                        while True:
                            message = await receive()
                            if message["type"] == "http.disconnect":
                                return
                            chunk = message.get("body", b"")
                            if len(body) + len(chunk) > MAX_BODY:
                                raise HTTPException(413, "Request too large")
                            body.extend(chunk)
                            if not message.get("more_body"):
                                break
                except TimeoutError:
                    raise HTTPException(408, "Request body timed out") from None
                messages = [{"type": "http.request", "body": bytes(body), "more_body": False}]

                async def replay():
                    return messages.pop(0) if messages else await receive()
                return await self.app(scope, replay, hardened)
            return await self.app(scope, receive, hardened)
        except HTTPException as exc:
            response = JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
            await response(scope, receive, hardened)
