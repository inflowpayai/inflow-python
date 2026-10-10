import asyncio
from collections.abc import Awaitable, Callable
from typing import cast

import httpx
import pytest

from inflowpay import ClientOptions
from inflowpay._runtime import Client
from inflowpay.mpp.seller import Seller as MppSeller
from inflowpay.x402.facilitator import Facilitator
from inflowpay.x402.seller import Seller as X402Seller
from test_mpp_seller import Platform as MppPlatform
from test_runtime_native import native_server
from test_x402_seller import Platform as X402Platform


@pytest.mark.parametrize("kind", ["mpp", "x402", "facilitator"])
async def test_seller_factories_accept_provider(kind: str) -> None:
    calls = 0

    async def provider() -> str:
        nonlocal calls
        calls += 1
        return "secret"

    platform = MppPlatform() if kind == "mpp" else X402Platform()
    options = ClientOptions(api_key_provider=provider, transport=platform)
    factory = MppSeller if kind == "mpp" else X402Seller if kind == "x402" else Facilitator
    async with await factory.create(options):
        assert calls > 0
        assert all(request.headers["X-API-KEY"] == "secret" for request in platform.requests)


async def test_provider_resolves_per_attempt() -> None:
    calls = 0
    received: list[str] = []

    async def provider() -> str:
        nonlocal calls
        calls += 1
        return f"key-{calls}"

    async def handle(
        path: str, headers: dict[str, str], body: bytes, writer: asyncio.StreamWriter
    ) -> None:
        received.append(headers["x-api-key"])
        assert "authorization" not in headers
        status = 503 if len(received) == 1 else 200
        writer.write(
            f"HTTP/1.1 {status} Test\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{{}}".encode()
        )

    async with (
        native_server(handle) as base,
        Client(ClientOptions(api_key_provider=provider, base_url=base)) as client,
    ):
        assert calls == 0
        await client.request("GET", "/example", retries=1)
        await client.request("GET", "/example")
    assert received == ["key-1", "key-2", "key-3"]


@pytest.mark.parametrize("value", ["", " ", "bad\nkey", "é"])
async def test_invalid_provider_result(value: str) -> None:
    async def provider() -> str:
        return value

    async def handle(request: httpx.Request) -> httpx.Response:
        pytest.fail("unexpected HTTP request")

    async with Client(
        ClientOptions(api_key_provider=provider, transport=httpx.MockTransport(handle))
    ) as client:
        with pytest.raises(ValueError):
            await client.request("GET", "/example", retries=3)


async def test_provider_error_is_not_retried() -> None:
    calls = 0
    failure = RuntimeError("provider unavailable")

    async def provider() -> str:
        nonlocal calls
        calls += 1
        raise failure

    async with Client(ClientOptions(api_key_provider=provider)) as client:
        with pytest.raises(RuntimeError) as raised:
            await client.request("GET", "/example", retries=3)
    assert raised.value is failure
    assert calls == 1


async def test_concurrent_requests_resolve_independently() -> None:
    calls = 0

    async def provider() -> str:
        nonlocal calls
        calls += 1
        key = f"key-{calls}"
        await asyncio.sleep(0)
        return key

    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=request.headers["X-API-KEY"])

    async with Client(
        ClientOptions(api_key_provider=provider, transport=httpx.MockTransport(handle))
    ) as client:
        one, two = await asyncio.gather(
            client.request("GET", "/one"), client.request("GET", "/two")
        )
        assert (one, two) == ("key-1", "key-2")


async def test_provider_cancellation_before_http() -> None:
    async def provider() -> str:
        task = asyncio.current_task()
        assert task is not None
        task.cancel()
        return "key"

    async def handle(request: httpx.Request) -> httpx.Response:
        pytest.fail("unexpected HTTP request")

    async with Client(
        ClientOptions(api_key_provider=provider, transport=httpx.MockTransport(handle))
    ) as client:
        task = asyncio.create_task(client.request("GET", "/example"))
        with pytest.raises(asyncio.CancelledError):
            await task


def test_conflicting_providers() -> None:
    async def provider() -> str:
        return "key"

    for options in (
        ClientOptions(api_key="key", api_key_provider=provider),
        ClientOptions(api_key_provider=provider, access_token=provider),
    ):
        with pytest.raises(ValueError, match="mutually exclusive"):
            Client(options)
    with pytest.raises(ValueError, match="callable"):
        Client(ClientOptions(api_key_provider=cast(Callable[[], Awaitable[str]], "not callable")))
