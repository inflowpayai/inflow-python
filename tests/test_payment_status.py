import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from inflowpay import ClientOptions, InflowApiError
from inflowpay.mpp.buyer import BuyerMethod
from inflowpay.x402.buyer import Buyer
from test_runtime_native import native_server


@asynccontextmanager
async def client(
    protocol: str, transport: httpx.AsyncBaseTransport
) -> AsyncIterator[BuyerMethod | Buyer]:
    options = ClientOptions(api_key="test-only", transport=transport)
    buyer = BuyerMethod(options) if protocol == "mpp" else await Buyer.create(options)
    async with buyer:
        yield buyer


class Platform(httpx.AsyncBaseTransport):
    def __init__(self, responses: list[httpx.Response]) -> None:
        self.responses = responses
        self.requests: list[httpx.Request] = []
        self.started = asyncio.Event()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("x402-supported"):
            return httpx.Response(200, json={"kinds": []})
        self.requests.append(request)
        self.started.set()
        assert request.method == "GET"
        assert request.headers["X-API-KEY"] == "test-only"
        if not self.responses:
            await asyncio.Event().wait()
        return self.responses.pop(0)


@pytest.mark.parametrize("protocol", ["mpp", "x402"])
async def test_fresh_status_preserves_action_and_unknown_states(protocol: str) -> None:
    action = {"type": "authenticate_card", "url": "https://dashboard.example/verify/"}
    snapshots = [
        {"transactionId": "original", "status": "PENDING", "nextAction": action},
        {"transactionId": "original", "status": "GENERAL_ERROR"},
        {"transactionId": "original", "status": "future-status"},
    ]
    platform = Platform([httpx.Response(200, json=value) for value in snapshots])
    async with client(protocol, platform) as buyer:
        for expected in snapshots:
            assert await buyer.get_payment_status("original") == expected
    assert len(platform.requests) == 3
    assert all(request.url.path == "/v1/transactions/original" for request in platform.requests)
    with pytest.raises(RuntimeError, match="closed"):
        await buyer.get_payment_status("original")


@pytest.mark.parametrize("protocol", ["mpp", "x402"])
@pytest.mark.parametrize("status", [302, 400, 401, 404, 503])
async def test_failure_is_not_retried_or_replaced(protocol: str, status: int) -> None:
    platform = Platform([httpx.Response(status, headers={"Location": "https://other.example/"})])
    async with client(protocol, platform) as buyer:
        with pytest.raises(InflowApiError) as caught:
            await buyer.get_payment_status("original")
    assert caught.value.http_status == status
    assert len(platform.requests) == 1


@pytest.mark.parametrize("protocol", ["mpp", "x402"])
async def test_encoded_identifier_and_explicit_retry(protocol: str) -> None:
    snapshot = {"transactionId": "original", "status": "COMPLETED"}
    platform = Platform([httpx.Response(503), httpx.Response(200, json=snapshot)])
    async with client(protocol, platform) as buyer:
        assert await buyer.get_payment_status("a/b?c#d", retries=1) == snapshot
    assert len(platform.requests) == 2
    assert all(
        request.url.raw_path == b"/v1/transactions/a%2Fb%3Fc%23d" for request in platform.requests
    )


@pytest.mark.parametrize("protocol", ["mpp", "x402"])
async def test_cancel_read_does_not_cancel_payment(protocol: str) -> None:
    platform = Platform([])
    async with client(protocol, platform) as buyer:
        task = asyncio.create_task(buyer.get_payment_status("original"))
        await platform.started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert len(platform.requests) == 1


@pytest.mark.parametrize("protocol", ["mpp", "x402"])
async def test_non_object_response(protocol: str) -> None:
    platform = Platform([httpx.Response(200, json=[])])
    async with client(protocol, platform) as buyer:
        with pytest.raises(ValueError, match="Payment status response must be an object"):
            await buyer.get_payment_status("original")


@pytest.mark.parametrize("protocol", ["mpp", "x402"])
@pytest.mark.parametrize("bearer", [False, True])
async def test_redirect_never_sends_credentials_to_second_origin(
    protocol: str, bearer: bool
) -> None:
    received: list[dict[str, str]] = []
    original: list[str] = []

    async def destination(
        path: str, headers: dict[str, str], body: bytes, writer: asyncio.StreamWriter
    ) -> None:
        received.append(headers)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}")

    async def token() -> str:
        return "test-only"

    async with native_server(destination) as other:

        async def redirect(
            path: str, headers: dict[str, str], body: bytes, writer: asyncio.StreamWriter
        ) -> None:
            assert headers["authorization" if bearer else "x-api-key"] == (
                "Bearer test-only" if bearer else "test-only"
            )
            if path.endswith("x402-supported"):
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 12\r\n"
                    b'Connection: close\r\n\r\n{"kinds":[]}'
                )
                return
            original.append(path)
            writer.write(
                (
                    f"HTTP/1.1 302 Found\r\nLocation: {other}/leak\r\n"
                    "Content-Length: 0\r\nConnection: close\r\n\r\n"
                ).encode()
            )

        async with native_server(redirect) as base:
            options = ClientOptions(
                base_url=base,
                api_key=None if bearer else "test-only",
                access_token=token if bearer else None,
            )
            buyer = BuyerMethod(options) if protocol == "mpp" else await Buyer.create(options)
            async with buyer:
                with pytest.raises(InflowApiError) as caught:
                    await buyer.get_payment_status("original")
                assert caught.value.http_status == 302
    assert original == ["/v1/transactions/original"]
    assert received == []
