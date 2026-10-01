import asyncio
import json
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from typing import Literal

import httpx
import pytest
from mpp import Challenge

from inflowpay import ClientOptions, InflowApiError
from inflowpay.mpp.buyer import BuyerMethod
from inflowpay.mpp.seller import Seller as MppSeller
from inflowpay.x402.buyer import Buyer
from inflowpay.x402.facilitator import Facilitator
from inflowpay.x402.seller import Seller as X402Seller
from test_mpp_seller import CONFIG, ID, TERMS, wire_credential
from test_runtime_native import native_server
from test_x402_buyer import REQUIREMENT, RESOURCE, SUPPORTED
from test_x402_seller import PAYLOAD, REQUIREMENTS

KINDS = ("mpp-buyer", "mpp-seller", "x402-buyer", "x402-facilitator")
PATHS = {
    "mpp-buyer": "/v1/transactions/mpp",
    "mpp-seller": "/v1/mpp/broadcast",
    "x402-buyer": "/v1/transactions/x402",
    "x402-facilitator": "/v1/x402/settle",
}


@asynccontextmanager
async def payment_operation(
    kind: str, options: ClientOptions
) -> AsyncIterator[Callable[[], Coroutine[object, object, object]]]:
    if kind == "mpp-buyer":
        challenge = Challenge.create(
            method="inflow",
            intent="charge",
            request={**TERMS, "recipient": ID},
            realm="merchant.example",
            secret_key="test-only-challenge-key",
        )
        async with BuyerMethod(options) as method:
            yield lambda: method.create_credential(challenge)
    elif kind == "mpp-seller":
        async with await MppSeller.create(options) as seller:
            yield lambda: seller.broadcast(wire_credential(), TERMS)
    elif kind == "x402-buyer":
        async with await Buyer.create(options) as buyer:
            yield lambda: buyer.prepare(REQUIREMENT, RESOURCE)
    else:
        assert kind == "x402-facilitator"
        async with await Facilitator.create(options) as facilitator:
            yield lambda: facilitator.settle(PAYLOAD, REQUIREMENTS)


def reply(writer: asyncio.StreamWriter, status: int, value: object) -> None:
    body = json.dumps(value).encode()
    writer.write(
        f"HTTP/1.1 {status} Test\r\nContent-Length: {len(body)}\r\n"
        "Content-Type: application/json\r\nSet-Cookie: platform-session=secret\r\n"
        "Connection: close\r\n\r\n".encode()
        + body
    )


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
async def test_payment_entry_points_refuse_redirects(kind: str, status: int) -> None:
    forwarded: list[str] = []
    requests: list[tuple[str, dict[str, str]]] = []

    async def target(
        path: str, headers: dict[str, str], body: bytes, writer: asyncio.StreamWriter
    ) -> None:
        forwarded.append(path)
        reply(writer, 200, {})

    async with native_server(target) as destination:

        async def platform(
            path: str, headers: dict[str, str], body: bytes, writer: asyncio.StreamWriter
        ) -> None:
            requests.append((path, headers))
            if path != PATHS[kind]:
                reply(writer, 200, CONFIG if path.endswith("/config") else SUPPORTED)
                return
            writer.write(
                f"HTTP/1.1 {status} Redirect\r\nLocation: {destination}/stolen\r\n"
                "Content-Length: 0\r\nConnection: close\r\n\r\n".encode()
            )

        async with (
            native_server(platform) as base,
            payment_operation(kind, ClientOptions(base_url=base, api_key="test-key")) as pay,
        ):
            with pytest.raises(InflowApiError) as error:
                await pay()
            assert error.value.http_status == status
    assert forwarded == []
    assert sum(path == PATHS[kind] for path, _ in requests) == 1
    assert all(headers["x-api-key"] == "test-key" for _, headers in requests)
    assert all(
        "cookie" not in headers and "authorization" not in headers for _, headers in requests
    )


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("status", [401, 403, 409, 503])
async def test_payment_error_preservation_and_replay(kind: str, status: int) -> None:
    requests: list[tuple[dict[str, str], bytes]] = []
    code = (
        "SELLER_ACCOUNT_REQUIRED" if status == 403 and kind == "mpp-seller" else "PAYMENT_REJECTED"
    )
    message = (
        "A Seller account is required."
        if code == "SELLER_ACCOUNT_REQUIRED"
        else "Payment rejected."
    )

    async def platform(
        path: str, headers: dict[str, str], body: bytes, writer: asyncio.StreamWriter
    ) -> None:
        if path != PATHS[kind]:
            reply(writer, 200, CONFIG if path.endswith("/config") else SUPPORTED)
            return
        requests.append((headers, body))
        reply(writer, status, {"errors": [{"code": code, "message": message}]})

    async with (
        native_server(platform) as base,
        payment_operation(kind, ClientOptions(base_url=base, api_key="test-key")) as pay,
    ):
        with pytest.raises(InflowApiError) as error:
            await pay()
    assert (error.value.code, str(error.value), error.value.http_status) == (code, message, status)
    assert error.value.endpoint == PATHS[kind]
    assert error.value.body == {"errors": [{"code": code, "message": message}]}
    assert "set-cookie" not in error.value.headers
    assert len(requests) == (4 if kind == "mpp-seller" and status == 503 else 1)
    assert all(body == requests[0][1] for _, body in requests)
    if kind == "mpp-seller":
        keys = {headers["idempotency-key"] for headers, _ in requests}
        assert len(keys) == 1


@pytest.mark.parametrize("kind", KINDS)
async def test_connection_loss_preserves_operation_retry_policy(kind: str) -> None:
    requests: list[tuple[dict[str, str], bytes]] = []

    async def platform(
        path: str, headers: dict[str, str], body: bytes, writer: asyncio.StreamWriter
    ) -> None:
        if path != PATHS[kind]:
            reply(writer, 200, CONFIG if path.endswith("/config") else SUPPORTED)
        else:
            requests.append((headers, body))
            # Close after receiving the request, before any response: payment outcome is unknown.

    async with (
        native_server(platform) as base,
        payment_operation(kind, ClientOptions(base_url=base, api_key="test-key")) as pay,
    ):
        with pytest.raises(InflowApiError) as error:
            await pay()
    assert error.value.code == "NETWORK_ERROR"
    assert len(requests) == (4 if kind == "mpp-seller" else 1)
    assert all(body == requests[0][1] for _, body in requests)
    if kind == "mpp-seller":
        assert len({headers["idempotency-key"] for headers, _ in requests}) == 1


@pytest.mark.parametrize("kind", KINDS)
async def test_payment_cancellation_stops_requests(kind: str) -> None:
    started = asyncio.Event()
    calls = 0

    async def platform(
        path: str, headers: dict[str, str], body: bytes, writer: asyncio.StreamWriter
    ) -> None:
        nonlocal calls
        if path != PATHS[kind]:
            reply(writer, 200, CONFIG if path.endswith("/config") else SUPPORTED)
        else:
            calls += 1
            started.set()
            await asyncio.Event().wait()

    async with (
        native_server(platform) as base,
        payment_operation(kind, ClientOptions(base_url=base, api_key="test-key")) as pay,
    ):
        task = asyncio.create_task(pay())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert calls == 1


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("environment", ["production", "sandbox"])
async def test_public_clients_use_selected_environment(
    kind: str, environment: Literal["production", "sandbox"]
) -> None:
    urls: list[httpx.URL] = []

    def handle(request: httpx.Request) -> httpx.Response:
        urls.append(request.url)
        if request.url.path == PATHS[kind]:
            return httpx.Response(401)
        return httpx.Response(
            200, json=CONFIG if request.url.path.endswith("/config") else SUPPORTED
        )

    options = ClientOptions(
        environment=environment, api_key="test-key", transport=httpx.MockTransport(handle)
    )
    async with payment_operation(kind, options) as pay:
        with pytest.raises(InflowApiError):
            await pay()
    assert urls
    assert all(
        url.scheme == "https"
        and url.host
        == ("api.inflowpay.ai" if environment == "production" else "sandbox.inflowpay.ai")
        for url in urls
    )


@pytest.mark.parametrize("kind", KINDS)
async def test_payment_errors_do_not_expose_api_key_or_credentials(kind: str) -> None:
    secret = "test-only-private-api-key"

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path != PATHS[kind]:
            return httpx.Response(
                200, json=CONFIG if request.url.path.endswith("/config") else SUPPORTED
            )
        return httpx.Response(
            403,
            json={
                "errors": [{"code": "DENIED", "message": f"Denied {secret}"}],
                "diagnostics": {"credential": "payment-proof", "signature": "signed-payload"},
            },
            headers={"X-Request-ID": "trace-id", "X-Info": secret, "Set-Cookie": "secret-cookie"},
        )

    async with payment_operation(
        kind, ClientOptions(api_key=secret, transport=httpx.MockTransport(handle))
    ) as pay:
        with pytest.raises(InflowApiError) as error:
            await pay()
    assert str(error.value) == "Denied [REDACTED]"
    assert error.value.code == "DENIED"
    assert error.value.request_id == "trace-id"
    assert error.value.headers["x-info"] == "[REDACTED]"
    assert "set-cookie" not in error.value.headers
    assert error.value.body == {
        "errors": [{"code": "DENIED", "message": "Denied [REDACTED]"}],
        "diagnostics": {"credential": "[REDACTED]", "signature": "[REDACTED]"},
    }


@pytest.mark.parametrize("protocol", ["mpp", "x402"])
async def test_seller_setup_preserves_account_role_error(protocol: str) -> None:
    paths: list[str] = []
    message = (
        "The supplied credentials belong to a Developer account. "
        "This endpoint requires a Seller account."
    )

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/config"):
            return httpx.Response(
                403, json={"errors": [{"code": "SELLER_ACCOUNT_REQUIRED", "message": message}]}
            )
        return httpx.Response(200, json=SUPPORTED)

    options = ClientOptions(api_key="developer-key", transport=httpx.MockTransport(handle))
    with pytest.raises(InflowApiError) as error:
        if protocol == "mpp":
            await MppSeller.create(options)
        else:
            await X402Seller.create(options)
    assert error.value.code == "SELLER_ACCOUNT_REQUIRED"
    assert str(error.value) == message
    assert paths.count(f"/v1/{protocol}/config") == 1


@pytest.mark.parametrize("kind", ["mpp-buyer", "x402-buyer"])
async def test_buyer_preserves_bearer_authentication(kind: str) -> None:
    requests: list[httpx.Request] = []
    tokens: list[str] = []

    async def token() -> str:
        value = f"test-only-token-{len(tokens)}"
        tokens.append(value)
        return value

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return (
            httpx.Response(401)
            if request.url.path == PATHS[kind]
            else httpx.Response(200, json=SUPPORTED)
        )

    async with payment_operation(
        kind, ClientOptions(access_token=token, transport=httpx.MockTransport(handle))
    ) as pay:
        with pytest.raises(InflowApiError) as error:
            await pay()
    assert error.value.http_status == 401
    assert len(requests) == len(tokens)
    assert len([request for request in requests if request.url.path == PATHS[kind]]) == 1
    for request, value in zip(requests, tokens, strict=True):
        assert request.headers["authorization"] == f"Bearer {value}"
        assert "x-api-key" not in request.headers
