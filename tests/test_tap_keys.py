import asyncio
import base64
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from inflowpay.tap.seller import TapVerificationError, TapVerifier, VisaTapKeyResolver
from test_runtime_http import Exchange, server
from test_tap_seller import KEY, NOW, signed

JWK = {
    "kid": "test-key",
    "alg": "Ed25519",
    "kty": "OKP",
    "crv": "Ed25519",
    "use": "sig",
    "x": base64.urlsafe_b64encode(KEY.public_key().public_bytes_raw()).decode().rstrip("="),
}


class Keys(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.calls = 0
        self.closed = False
        self.payload: object = {"keys": [JWK]}
        self.error: Exception | None = None
        self.status = 200
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()
        self.cancelled = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        assert request.url == "https://mcp.visa.com/.well-known/jwks"
        assert request.headers["accept"] == "application/json"
        assert "authorization" not in request.headers and "x-api-key" not in request.headers
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        if self.error:
            raise self.error
        return httpx.Response(self.status, json=self.payload)

    async def aclose(self) -> None:
        self.closed = True


async def test_cache_replacement_and_negative_cache() -> None:
    clock = NOW
    transport = Keys()
    async with VisaTapKeyResolver(
        transport=transport, clock=lambda: clock, cache_ttl=10
    ) as resolver:
        assert await resolver.resolve("test-key", "rsa") is None
        key = await resolver.resolve("test-key", "ed25519")
        assert isinstance(key, Ed25519PublicKey)
        assert await resolver.resolve("test-key", "ed25519") is key
        assert transport.calls == 1
        assert await resolver.resolve("missing", "ed25519") is None
        assert await resolver.resolve("missing", "ed25519") is None
        assert transport.calls == 2
        transport.payload = {"keys": [{**JWK, "kid": "missing"}]}
        clock += 11
        assert await resolver.resolve("test-key", "ed25519") is None
        assert await resolver.resolve("missing", "ed25519") is not None
        assert transport.calls == 3
    assert transport.closed
    with pytest.raises(RuntimeError, match="closed"):
        await resolver.resolve("test-key", "ed25519")
    with pytest.raises(RuntimeError, match="closed"):
        await resolver.__aenter__()
    await resolver.aclose()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {"keys": None},
        {"keys": {}},
        {"keys": [JWK, JWK]},
        {"keys": [{**JWK, "x": None}]},
        {"keys": [{**JWK, "x": "bad"}]},
        {"keys": [{**JWK, "x": "!" * 43}]},
        {"keys": [{**JWK, "x": JWK["x"] + "="}]},
    ],
)
async def test_invalid_keysets_do_not_establish_trust(payload: object) -> None:
    transport = Keys()
    transport.payload = payload
    async with VisaTapKeyResolver(transport=transport) as resolver:
        with pytest.raises(TapVerificationError) as error:
            await resolver.resolve("test-key", "ed25519")
        assert error.value.code == "KEY_RETRIEVAL_FAILED"
        assert isinstance(error.value.__cause__, ValueError)


@pytest.mark.parametrize(
    "key",
    [
        None,
        "invalid",
        {},
        {**JWK, "kid": None},
        {**JWK, "alg": "rsa"},
        {**JWK, "kty": "RSA"},
        {**JWK, "crv": "P-256"},
        {**JWK, "use": "enc"},
    ],
)
async def test_unrelated_keys_ignored(key: object) -> None:
    transport = Keys()
    transport.payload = {
        "keys": [key, {name: value for name, value in JWK.items() if name != "use"}]
    }
    async with VisaTapKeyResolver(transport=transport) as resolver:
        assert await resolver.resolve("test-key", "ed25519") is not None


async def test_absent_keys_is_empty_keyset() -> None:
    transport = Keys()
    transport.payload = {}
    async with VisaTapKeyResolver(transport=transport) as resolver:
        assert await resolver.resolve("test-key", "ed25519") is None


async def test_failed_refresh_is_atomic_and_fallback_has_age_bound() -> None:
    now = NOW
    transport = Keys()
    async with VisaTapKeyResolver(
        transport=transport, clock=lambda: now, cache_ttl=1, cache_max_age=10
    ) as resolver:
        old = await resolver.resolve("test-key", "ed25519")
        now += 2
        transport.payload = {"keys": [{**JWK, "kid": "new"}, {**JWK, "x": "bad"}]}
        assert await resolver.resolve("test-key", "ed25519") is old
        with pytest.raises(TapVerificationError):
            await resolver.resolve("new", "ed25519")
        now += 9
        with pytest.raises(TapVerificationError):
            await resolver.resolve("test-key", "ed25519")
        transport.payload = {"keys": []}
        assert await resolver.resolve("test-key", "ed25519") is None
        now += 2
        transport.error = httpx.ConnectError("offline")
        with pytest.raises(TapVerificationError):
            await resolver.resolve("test-key", "ed25519")


async def test_fallback_limit_does_not_expire_fresh_cache() -> None:
    now = NOW
    transport = Keys()
    async with VisaTapKeyResolver(
        transport=transport, clock=lambda: now, cache_ttl=100, cache_max_age=10
    ) as resolver:
        old = await resolver.resolve("test-key", "ed25519")
        now += 11
        transport.error = httpx.ConnectError("offline")
        assert await resolver.resolve("test-key", "ed25519") is old
        assert transport.calls == 1


async def test_single_refresh_and_waiter_cancellation() -> None:
    transport = Keys()
    transport.release.clear()
    async with VisaTapKeyResolver(transport=transport) as resolver:
        first = asyncio.create_task(resolver.resolve("test-key", "ed25519"))
        await transport.started.wait()
        second = asyncio.create_task(resolver.resolve("test-key", "ed25519"))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not transport.cancelled.is_set()
        transport.release.set()
        assert await second is not None
        assert transport.calls == 1


async def test_close_drains_refresh_and_waiters() -> None:
    transport = Keys()
    transport.release.clear()
    resolver = VisaTapKeyResolver(transport=transport)
    task = asyncio.create_task(resolver.resolve("test-key", "ed25519"))
    await transport.started.wait()
    await resolver.aclose()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport.cancelled.is_set() and transport.closed


async def test_refresh_exception_is_observed_after_all_waiters_leave() -> None:
    transport = Keys()
    transport.release.clear()
    transport.error = httpx.ConnectError("offline")
    observed: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _, event: observed.append(event))
    try:
        async with VisaTapKeyResolver(transport=transport) as resolver:
            task = asyncio.create_task(resolver.resolve("test-key", "ed25519"))
            await transport.started.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            transport.release.set()
            await asyncio.sleep(0)
            await asyncio.sleep(0)
        assert not observed
    finally:
        loop.set_exception_handler(previous)


async def test_total_timeout_cancels_retrieval() -> None:
    transport = Keys()
    transport.release.clear()
    async with VisaTapKeyResolver(transport=transport, timeout=0.01) as resolver:
        with pytest.raises(TapVerificationError) as error:
            await resolver.resolve("test-key", "ed25519")
        assert isinstance(error.value.__cause__, TimeoutError)
        assert transport.cancelled.is_set()


async def test_real_http_resolver_and_verifier() -> None:
    exchanges: list[Exchange] = [
        {
            "request": {"method": "GET", "path": "/keys", "headers": {}},
            "response": {"status": 200, "json": {"keys": [JWK]}},
        }
    ]
    async with server(exchanges) as url, VisaTapKeyResolver(url=url + "/keys") as resolver:
        async with TapVerifier(key_resolver=resolver, clock=lambda: NOW) as verifier:
            assert (await verifier.verify(signed())).verified
        # The application owns an explicitly supplied resolver, not the verifier.
        assert await resolver.resolve("test-key", "ed25519") is not None


async def test_real_http_redirect_is_not_followed() -> None:
    exchanges: list[Exchange] = [
        {
            "request": {"method": "GET", "path": "/keys", "headers": {}},
            "response": {"status": 302, "headers": {"Location": "/different"}},
        }
    ]
    async with server(exchanges) as url, VisaTapKeyResolver(url=url + "/keys") as resolver:
        with pytest.raises(TapVerificationError) as error:
            await resolver.resolve("test-key", "ed25519")
        assert isinstance(error.value.__cause__, httpx.HTTPStatusError)
