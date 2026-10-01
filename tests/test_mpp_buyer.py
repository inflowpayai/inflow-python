import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import NotRequired, TypedDict, cast

import httpx
import pytest
from mcp.shared.exceptions import McpError
from mcp.types import CallToolResult, ErrorData
from mpp import Challenge
from mpp.errors import PaymentOutcomeUnknownError
from mpp.extensions.mcp import McpClient
from mpp.extensions.mcp.constants import META_CREDENTIAL
from mpp.runtime import PaymentRuntime

from inflowpay import ClientOptions, InflowApiError
from inflowpay.mpp import (
    WireObject,
    decode_credential,
    encode,
    from_pympp_challenge,
    render_challenge_header,
    to_pympp_challenge,
)
from inflowpay.mpp.buyer import (
    BuyerMethod,
    MppMalformedCredentialError,
    MppPaymentExpiredError,
    MppPaymentFailedError,
    MppPaymentTimeoutError,
    payment_transport,
)


class Request(TypedDict):
    method: str
    path: str
    headers: dict[str, str]
    json: NotRequired[object]


class Response(TypedDict):
    status: int
    json: NotRequired[object]
    delay_ms: NotRequired[int]
    partial_body: NotRequired[bool]
    headers: NotRequired[dict[str, str]]


class Exchange(TypedDict):
    request: Request
    response: Response


class Input(TypedDict):
    challenge: WireObject
    context: dict[str, str]
    api_key: str
    timeout_ms: NotRequired[int]


class Platform(TypedDict):
    exchanges: list[Exchange]


class Case(TypedDict):
    id: str
    input: Input
    operation: str
    expect: dict[str, object]
    platform: Platform


# Generated shared corpus: only its JSON decoding needs a typing boundary.
CASES = cast(
    list[Case],
    json.loads(Path(__file__).with_name("fixtures").joinpath("mpp-buyer.json").read_text())[
        "cases"
    ],
)
ID = "11111111-1111-4111-8111-111111111111"
WIRE: WireObject = {
    "id": "test",
    "realm": "seller.example",
    "method": "inflow",
    "intent": "charge",
    "request": encode({"amount": "1", "currency": "USD"}),
}
PENDING = {
    "state": "pending",
    "transactionId": "transaction",
    "approvalId": "approval",
    "retryAfterSeconds": 0,
}


def ready(wire: WireObject = WIRE) -> dict[str, object]:
    return {
        "state": "ready",
        "credential": encode(
            {
                "challenge": wire,
                "payload": {"transactionId": "transaction"},
                "source": "did:inflow:buyer",
            }
        ),
    }


def subscription() -> Challenge:
    return to_pympp_challenge(
        {
            **WIRE,
            "intent": "subscription",
            "request": encode(
                {
                    "amount": "1",
                    "currency": "USD",
                    "periodUnit": "month",
                    "periodCount": 1,
                    "subscriptionExpires": "2099-01-01T00:00:00Z",
                }
            ),
        }
    )


@asynccontextmanager
async def server(
    exchanges: list[Exchange], started: asyncio.Event | None = None
) -> AsyncIterator[str]:
    seen: list[Request] = []
    tasks: set[asyncio.Task[None]] = set()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            first, *lines = (await reader.readuntil(b"\r\n\r\n")).decode().split("\r\n")
            method, path, _ = first.split(" ")
            headers = {
                name.lower(): value
                for name, value in (line.split(": ", 1) for line in lines if line)
            }
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            actual: Request = {"method": method, "path": path, "headers": headers}
            if body:
                actual["json"] = json.loads(body)
            index = len(seen)
            seen.append(actual)
            response = exchanges[index]["response"]
            if response.get("delay_ms", 0):
                if response.get("partial_body"):
                    writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n{"state":')
                    await writer.drain()
                if started is not None:
                    started.set()
                await asyncio.sleep(response["delay_ms"] / 1000)
            content = json.dumps(response["json"]).encode() if "json" in response else b""
            writer.write(
                (
                    f"HTTP/1.1 {response['status']} Test\r\n"
                    f"Content-Length: {len(content)}\r\nConnection: close\r\n"
                    + "".join(
                        f"{key}: {value}\r\n" for key, value in response.get("headers", {}).items()
                    )
                    + "\r\n"
                ).encode()
                + content
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        tasks.add(asyncio.create_task(handle(reader, writer)))

    listener = await asyncio.start_server(connect, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{listener.sockets[0].getsockname()[1]}"
    finally:
        listener.close()
        for task in tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        await listener.wait_closed()
        assert all(
            result is None or isinstance(result, asyncio.CancelledError) for result in results
        ), results
    assert len(seen) == len(exchanges)
    for actual, exchange in zip(seen, exchanges, strict=True):
        expected = exchange["request"]
        assert actual["method"] == expected["method"]
        assert actual["path"] == expected["path"]
        assert actual.get("json") == expected.get("json")
        for key, value in expected["headers"].items():
            assert actual["headers"].get(key, "") == value


def observation(error: BaseException) -> dict[str, object]:
    details: dict[str, object] = {}
    if isinstance(error, MppPaymentFailedError):
        code, message = "payment-failed", "Payment failed."
        if error.problem is not None:
            details["problem"] = error.problem
    elif isinstance(error, MppPaymentExpiredError):
        code, message = "payment-expired", "Payment expired."
        details["transaction_id"] = error.transaction_id
    elif isinstance(error, MppPaymentTimeoutError):
        code, message = "payment-timeout", "Payment timed out."
        details["transaction_id"] = error.transaction_id
    elif isinstance(error, MppMalformedCredentialError):
        code, message = "invalid-credential", "Invalid credential."
    elif isinstance(error, asyncio.CancelledError):
        code, message = "payment-cancelled", "Payment cancelled."
    else:
        raise error
    return {
        "error": {"code": code, "message": message, **({"details": details} if details else {})}
    }


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
async def test_shared_buyer(case: Case) -> None:
    data = case["input"]
    challenge = to_pympp_challenge(data["challenge"])
    async with server(case["platform"]["exchanges"]) as base:

        class Observe(httpx.AsyncHTTPTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                response = await super().handle_async_request(request)
                await response.aread()
                if case["operation"] == "mpp.buyer.cancel" and request.url.path.endswith(
                    "/transactions/mpp"
                ):
                    asyncio.get_running_loop().call_soon(payment.cancel)
                return response

        async with BuyerMethod(
            ClientOptions(base_url=base, api_key=data["api_key"], transport=Observe()),
            method=challenge.method,
            intent=challenge.intent,
            instrument_id=data["context"].get("instrumentId"),
            subscription_id=data["context"].get("subscriptionId"),
            poll_interval=0,
            pending_timeout=data.get("timeout_ms", 900000) / 1000,
        ) as buyer:
            payment = asyncio.create_task(buyer.create_credential(challenge))
            try:
                credential = await payment
                result: dict[str, object] = {
                    "result": decode_credential(credential.to_authorization()[8:])
                }
            except (
                MppMalformedCredentialError,
                MppPaymentFailedError,
                MppPaymentExpiredError,
                MppPaymentTimeoutError,
                asyncio.CancelledError,
            ) as error:
                result = observation(error)
            assert result == case["expect"]


@pytest.mark.parametrize(
    "method,intent,instrument,sub",
    [
        ("other", "charge", None, None),
        ("tempo", "charge", ID, None),
        ("inflow", "charge", None, ID),
        ("inflow", "charge", "bad", None),
    ],
)
def test_invalid_configuration(
    method: str, intent: str, instrument: str | None, sub: str | None
) -> None:
    with pytest.raises(ValueError):
        BuyerMethod(
            ClientOptions(),
            method=method,
            intent=intent,
            instrument_id=instrument,
            subscription_id=sub,
        )


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf")])
@pytest.mark.parametrize("setting", ["poll_interval", "pending_timeout"])
def test_invalid_times(value: float, setting: str) -> None:
    with pytest.raises(ValueError):
        BuyerMethod(
            ClientOptions(),
            poll_interval=value if setting == "poll_interval" else 5,
            pending_timeout=value if setting == "pending_timeout" else 900,
        )


async def test_wrong_challenge_closed_and_explicit_cancel() -> None:
    requests: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        return httpx.Response(204)

    buyer = BuyerMethod(ClientOptions(transport=httpx.MockTransport(handle)))
    with pytest.raises(ValueError, match="does not match"):
        await buyer.create_credential(subscription())
    await buyer.cancel_approval("approval")
    await buyer.aclose()
    await buyer.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await buyer.create_credential(to_pympp_challenge(WIRE))
    assert requests == ["/v1/approvals/approval/cancel"]


@pytest.mark.parametrize(
    "response",
    [
        None,
        [],
        {"state": "surprise"},
        {"state": "pending", "transactionId": "id", "retryAfterSeconds": "0"},
        {"state": "pending", "transactionId": "id", "retryAfterSeconds": True},
        {"state": "pending", "transactionId": "id", "retryAfterSeconds": float("inf")},
    ],
)
async def test_malformed_response(response: object) -> None:
    # HTTPX's json argument rejects infinity; a literal body tests the remote JSON boundary.
    async with BuyerMethod(
        ClientOptions(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, content=json.dumps(response))
            )
        )
    ) as buyer:
        with pytest.raises(MppMalformedCredentialError):
            await buyer.create_credential(to_pympp_challenge(WIRE))


async def test_error_details_and_missing_source() -> None:
    examples: list[tuple[WireObject | None, str]] = [
        (None, "MPP payment failed"),
        ({"title": "Denied"}, "Denied"),
        ({"title": "Denied", "detail": "Reason", "extensions": {"x": 1}}, "Reason"),
    ]
    for problem, message in examples:
        error = MppPaymentFailedError(problem)
        assert str(error) == message
        assert error.problem == problem
    assert MppPaymentExpiredError(None).transaction_id is None
    wire: WireObject = {
        **WIRE,
        "description": "Words",
        "extension": {"nested": 1},
        "opaque": encode({"a": "b"}),
    }
    original = deepcopy(wire)
    credential = {"challenge": wire, "payload": {"nested": [1]}, "extension": {"keep": True}}
    async with BuyerMethod(
        ClientOptions(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(
                    200, json={"state": "ready", "credential": encode(credential)}
                )
            )
        )
    ) as buyer:
        result = await buyer.create_credential(to_pympp_challenge(wire))
    assert decode_credential(result.to_authorization()[8:]) == credential
    result.payload["nested"].append(2)
    assert decode_credential(result.to_authorization()[8:])["payload"] == {"nested": [1, 2]}
    assert wire == original
    assert replace(result, source="did:test").source == "did:test"


@pytest.mark.parametrize("stage", ["create", "poll", "authorize"])
@pytest.mark.parametrize("mode", ["caller", "cleanup", "close"])
async def test_cancel_concurrent_and_reuse(stage: str, mode: str) -> None:
    started = asyncio.Event()
    requests: list[str] = []
    block = True
    in_progress = 0

    async def handle(request: httpx.Request) -> httpx.Response:
        nonlocal in_progress
        requests.append(request.url.path)
        if request.url.path.endswith("/cancel"):
            return httpx.Response(500)
        if block:
            if stage == "poll" and request.method == "POST":
                return httpx.Response(200, json=PENDING)
            in_progress += 1
            if in_progress == 2:
                started.set()
            await asyncio.Event().wait()
        return httpx.Response(200, json=ready())

    buyer = BuyerMethod(
        ClientOptions(transport=httpx.MockTransport(handle)),
        intent="subscription" if stage == "authorize" else "charge",
        subscription_id=ID if stage == "authorize" else None,
    )
    challenge = subscription() if stage == "authorize" else to_pympp_challenge(WIRE)
    payments = [asyncio.create_task(buyer.create_credential(challenge)) for _ in range(2)]
    await asyncio.wait_for(started.wait(), 2)
    if mode == "caller":
        for task in payments:
            task.cancel()
    elif mode == "cleanup":
        await buyer.cleanup()
    else:
        await buyer.aclose()
    results = await asyncio.gather(*payments, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert sum(path.endswith("/cancel") for path in requests) == (2 if stage == "poll" else 0)
    assert not any("/delete" in path for path in requests)
    if mode != "close":
        block = False
        assert (await buyer.create_credential(challenge)).source == "did:inflow:buyer"
    await buyer.aclose()


@pytest.mark.parametrize("stage", ["create", "poll", "authorize"])
@pytest.mark.parametrize("partial_body", [False, True])
async def test_real_http_cancellation(stage: str, partial_body: bool) -> None:
    wire = (
        WIRE
        if stage != "authorize"
        else {**WIRE, "intent": "subscription", "request": subscription().request_b64}
    )
    create: Exchange = {
        "request": {
            "method": "POST",
            "path": "/v1/transactions/mpp",
            "headers": {},
            "json": {"challenge": wire, "options": {}},
        },
        "response": {"status": 200, "json": PENDING},
    }
    slow: Exchange = {
        "request": {"method": "GET", "path": "/v1/transactions/transaction/mpp", "headers": {}},
        "response": {"status": 200, "json": ready(), "delay_ms": 10000},
    }
    cancel: Exchange = {
        "request": {"method": "POST", "path": "/v1/approvals/approval/cancel", "headers": {}},
        "response": {"status": 204},
    }
    if stage == "poll":
        exchanges = [create, slow, cancel]
    elif stage == "create":
        create["response"]["delay_ms"] = 10000
        exchanges = [create]
    else:
        slow["request"] = {
            "method": "POST",
            "path": f"/v1/subscriptions/{ID}/authorize",
            "headers": {},
            "json": {"challenge": wire},
        }
        exchanges = [slow]
    started = asyncio.Event()
    for exchange in exchanges:
        if exchange["response"].get("delay_ms"):
            exchange["response"]["partial_body"] = partial_body
    async with (
        server(exchanges, started) as base,
        BuyerMethod(
            ClientOptions(base_url=base),
            intent="subscription" if stage == "authorize" else "charge",
            subscription_id=ID if stage == "authorize" else None,
        ) as buyer,
    ):
        task = asyncio.create_task(buyer.create_credential(to_pympp_challenge(wire)))
        await asyncio.wait_for(started.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)


@pytest.mark.parametrize("state", ["ready", "pending"])
async def test_zero_budget(state: str) -> None:
    paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return (
            httpx.Response(204)
            if request.url.path.endswith("/cancel")
            else httpx.Response(200, json=ready() if state == "ready" else PENDING)
        )

    async with BuyerMethod(
        ClientOptions(transport=httpx.MockTransport(handle)), pending_timeout=0
    ) as buyer:
        if state == "ready":
            await buyer.create_credential(to_pympp_challenge(WIRE))
        else:
            with pytest.raises(MppPaymentTimeoutError):
                await buyer.create_credential(to_pympp_challenge(WIRE))
    assert not any(path.endswith("/transaction/mpp") for path in paths)


@pytest.mark.parametrize("stage", ["create", "poll", "authorize"])
@pytest.mark.parametrize("failure", ["status", "network"])
async def test_no_retries(stage: str, failure: str) -> None:
    paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/cancel"):
            return httpx.Response(204)
        if stage == "poll" and request.method == "POST":
            return httpx.Response(200, json=PENDING)
        if failure == "network":
            raise httpx.ReadError("response lost")
        return httpx.Response(503, json={"detail": "Unavailable", "code": "TEST"})

    async with BuyerMethod(
        ClientOptions(transport=httpx.MockTransport(handle)),
        intent="subscription" if stage == "authorize" else "charge",
        subscription_id=ID if stage == "authorize" else None,
    ) as buyer:
        with pytest.raises(InflowApiError) as error:
            await buyer.create_credential(
                subscription() if stage == "authorize" else to_pympp_challenge(WIRE)
            )
    assert error.value.http_status == (503 if failure == "status" else 0)
    assert len(paths) == (3 if stage == "poll" else 1)


@pytest.mark.parametrize("paid_status", [200, 402])
async def test_actual_pympp_http_flow(paid_status: int) -> None:
    challenge = Challenge.create(
        secret_key="synthetic-secret",
        realm="seller.example",
        method="inflow",
        intent="charge",
        request={"amount": "1", "currency": "USD"},
    )
    platform_requests: list[httpx.Request] = []
    seller_requests: list[httpx.Request] = []

    def platform(request: httpx.Request) -> httpx.Response:
        platform_requests.append(request)
        body = json.loads(request.content)
        return httpx.Response(200, json=ready(body["challenge"]))

    def seller(request: httpx.Request) -> httpx.Response:
        seller_requests.append(request)
        if len(seller_requests) == 1:
            return httpx.Response(
                402, headers={"www-authenticate": challenge.to_www_authenticate("seller.example")}
            )
        credential = decode_credential(request.headers["authorization"][8:])
        assert credential["payload"] == {"transactionId": "transaction"}
        assert credential["source"] == "did:inflow:buyer"
        assert "x-api-key" not in request.headers
        return httpx.Response(
            paid_status,
            headers={"www-authenticate": challenge.to_www_authenticate("seller.example")},
        )

    async with (
        BuyerMethod(
            ClientOptions(api_key="test-only", transport=httpx.MockTransport(platform))
        ) as buyer,
        httpx.AsyncClient(
            transport=payment_transport([buyer], inner=httpx.MockTransport(seller))
        ) as http,
    ):
        assert isinstance(PaymentRuntime([buyer]), PaymentRuntime)
        response = await http.post("https://seller.example/item", content=b"hello")
    assert response.status_code == paid_status
    assert len(platform_requests) == 1 and len(seller_requests) == 2
    assert seller_requests[1].content == b"hello"
    assert platform_requests[0].headers["x-api-key"] == "test-only"


@pytest.mark.parametrize("phase", ["wait", "poll"])
async def test_deadline_after_noncooperative_work(
    phase: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths: list[str] = []

    async def delayed_sleep(delay: float) -> None:
        # Simulate event-loop work that overruns a deadline before its timer callback runs.
        time.sleep(delay + 0.02)

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/cancel"):
            return httpx.Response(204)
        if request.method == "POST":
            return httpx.Response(200, json=PENDING)
        time.sleep(0.03)
        return httpx.Response(200, json=ready())

    if phase == "wait":
        monkeypatch.setattr(asyncio, "sleep", delayed_sleep)
    async with BuyerMethod(
        ClientOptions(transport=httpx.MockTransport(handle)), pending_timeout=0.01
    ) as buyer:
        with pytest.raises(MppPaymentTimeoutError) as failure:
            await buyer.create_credential(to_pympp_challenge(WIRE))
    assert failure.value.transaction_id == "transaction"
    assert failure.value.timeout == 0.01
    assert len(paths) == (2 if phase == "wait" else 3)


async def test_early_wake_rechecks_advice(monkeypatch: pytest.MonkeyPatch) -> None:
    sleep = asyncio.sleep
    calls: list[float] = []

    async def early_sleep(delay: float) -> None:
        if delay > 0:
            calls.append(delay)
        await sleep(0 if len(calls) == 1 else delay)

    monkeypatch.setattr(asyncio, "sleep", early_sleep)
    began = 0.0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal began
        if request.method == "POST":
            began = time.monotonic()
            return httpx.Response(200, json={**PENDING, "retryAfterSeconds": 0.03})
        assert time.monotonic() - began >= 0.03
        return httpx.Response(200, json=ready())

    async with BuyerMethod(ClientOptions(transport=httpx.MockTransport(handle))) as buyer:
        await buyer.create_credential(to_pympp_challenge(WIRE))
    assert len(calls) >= 2


async def test_token_timeout_is_not_pending_timeout() -> None:
    count = 0
    original = TimeoutError("token provider")

    async def token() -> str:
        nonlocal count
        count += 1
        if count == 2:
            raise original
        return "test"

    async with BuyerMethod(
        ClientOptions(
            access_token=token,
            transport=httpx.MockTransport(
                lambda request: (
                    httpx.Response(204)
                    if request.url.path.endswith("/cancel")
                    else httpx.Response(200, json=PENDING)
                )
            ),
        )
    ) as buyer:
        with pytest.raises(TimeoutError) as error:
            await buyer.create_credential(to_pympp_challenge(WIRE))
    assert error.value is original and count == 3


@pytest.mark.parametrize("phase", ["wait", "token"])
async def test_cancel_during_wait_or_token(phase: str) -> None:
    entered = asyncio.Event()
    paths: list[str] = []

    async def token() -> str:
        if phase == "token":
            entered.set()
            await asyncio.Event().wait()
        return "test"

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/cancel"):
            return httpx.Response(204)
        asyncio.get_running_loop().call_soon(entered.set)
        return httpx.Response(200, json={**PENDING, "retryAfterSeconds": 60})

    async with BuyerMethod(
        ClientOptions(access_token=token, transport=httpx.MockTransport(handle))
    ) as buyer:
        payment = asyncio.create_task(buyer.create_credential(to_pympp_challenge(WIRE)))
        await asyncio.wait_for(entered.wait(), 1)
        payment.cancel()
        with pytest.raises(asyncio.CancelledError):
            await payment
    assert len(paths) == (0 if phase == "token" else 2)


async def test_default_interval_and_failed_without_problem() -> None:
    began = 0.0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal began
        if request.url.path.endswith("/cancel"):
            return httpx.Response(204)
        if request.method == "POST":
            began = time.monotonic()
            return httpx.Response(
                200, json={"state": "pending", "transactionId": "id", "approvalId": "approval"}
            )
        assert time.monotonic() - began >= 0.02
        return httpx.Response(200, json={"state": "failed"})

    async with BuyerMethod(
        ClientOptions(transport=httpx.MockTransport(handle)), poll_interval=0.02
    ) as buyer:
        with pytest.raises(MppPaymentFailedError) as error:
            await buyer.create_credential(to_pympp_challenge(WIRE))
    assert error.value.problem is None


async def test_empty_source_is_preserved() -> None:
    wire = {"challenge": WIRE, "payload": {}, "source": ""}
    async with BuyerMethod(
        ClientOptions(
            transport=httpx.MockTransport(
                lambda _: httpx.Response(200, json={"state": "ready", "credential": encode(wire)})
            )
        )
    ) as buyer:
        result = await buyer.create_credential(to_pympp_challenge(WIRE))
    assert result.source == ""
    assert decode_credential(result.to_authorization()[8:]) == wire


@pytest.mark.parametrize("cancel", [False, True])
async def test_paid_retry_failure_is_unknown_not_repaid(cancel: bool) -> None:

    calls: list[str] = []

    def platform(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=ready())

    def seller(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if "authorization" not in request.headers:
            return httpx.Response(402, headers={"www-authenticate": render_challenge_header(WIRE)})
        if cancel:
            raise asyncio.CancelledError
        raise httpx.ReadError("lost response")

    async with (
        BuyerMethod(ClientOptions(transport=httpx.MockTransport(platform))) as buyer,
        httpx.AsyncClient(
            transport=payment_transport([buyer], inner=httpx.MockTransport(seller))
        ) as http,
    ):
        with pytest.raises(PaymentOutcomeUnknownError) as error:
            await http.get("https://seller.example/item")
    assert error.value.credential is not None
    assert calls == ["/item", "/v1/transactions/mpp", "/item"]


@pytest.mark.parametrize("kind", ["charge", "instrument", "subscription", "access", "tempo"])
@pytest.mark.parametrize("outcome", ["success", "platform-failure"])
async def test_pympp_over_two_real_http_origins(kind: str, outcome: str) -> None:
    intent = "subscription" if kind in ("subscription", "access") else "charge"
    method = "tempo" if kind == "tempo" else "inflow"
    request = (
        subscription().request if intent == "subscription" else {"amount": "1", "currency": "USD"}
    )
    if kind == "tempo":
        request = {"amount": "1", "currency": "0x" + "1" * 40}
    challenge = Challenge.create(
        secret_key="synthetic-secret",
        realm="seller.example",
        method=method,
        intent=intent,
        request=request,
    )
    wire = from_pympp_challenge(challenge)
    encoded = ready(wire)["credential"]
    assert isinstance(encoded, str)
    seller: list[Exchange] = [
        {
            "request": {
                "method": "GET",
                "path": "/item",
                "headers": {"accept-payment": f"{method}/{intent}", "x-api-key": ""},
            },
            "response": {
                "status": 402,
                "headers": {"www-authenticate": render_challenge_header(wire)},
            },
        },
        {
            "request": {
                "method": "GET",
                "path": "/item",
                "headers": {"authorization": "Payment " + encoded, "x-api-key": ""},
            },
            "response": {"status": 200, "json": {"result": "paid resource"}},
        },
    ]
    platform: list[Exchange] = [
        {
            "request": {
                "method": "POST",
                "path": f"/v1/subscriptions/{ID}/authorize"
                if kind == "access"
                else "/v1/transactions/mpp",
                "headers": {"x-api-key": "synthetic-key"},
                "json": {"challenge": wire}
                if kind == "access"
                else {
                    "challenge": wire,
                    "options": {"instrumentId": ID} if kind == "instrument" else {},
                },
            },
            "response": {"status": 200, "json": ready(wire)},
        },
    ]
    if outcome == "platform-failure":
        seller.pop()
        platform[0]["response"] = {
            "status": 403,
            "json": {"code": "DENIED", "message": "Payment denied"},
        }
    async with (
        server(platform) as platform_url,
        server(seller) as seller_url,
        BuyerMethod(
            ClientOptions(base_url=platform_url, api_key="synthetic-key"),
            method=method,
            intent=intent,
            subscription_id=ID if kind == "access" else None,
            instrument_id=ID if kind == "instrument" else None,
        ) as buyer,
        httpx.AsyncClient(transport=payment_transport([buyer])) as http,
    ):
        if outcome == "platform-failure":
            with pytest.raises(InflowApiError) as error:
                await http.get(seller_url + "/item")
            assert error.value.http_status == 403 and error.value.code == "DENIED"
        else:
            response = await http.get(seller_url + "/item")
            assert response.json() == {"result": "paid resource"}
    echoed = decode_credential(encoded)["challenge"]
    assert isinstance(echoed, dict)
    assert to_pympp_challenge(echoed).verify("synthetic-secret", "seller.example")


@pytest.mark.parametrize("kind", ["charge", "subscription", "access", "tempo"])
@pytest.mark.parametrize("outcome", ["success", "platform-failure", "retry-failure"])
async def test_actual_pympp_mcp_flow(kind: str, outcome: str) -> None:
    challenge = subscription() if kind in ("subscription", "access") else to_pympp_challenge(WIRE)
    if kind == "tempo":
        request = {"amount": "1", "currency": "0x" + "1" * 40}
        challenge = replace(challenge, method="tempo", request=request, request_b64=encode(request))
    calls: list[WireObject] = []
    paths: list[str] = []
    original_meta: WireObject = {"application": "test"}

    class Session:
        # The session supplies tool results; matching, conversion and payment retry are real pympp.
        async def call_tool(
            self, name: str, arguments: WireObject | None, *, meta: WireObject
        ) -> CallToolResult:
            assert name == "premium_tool" and arguments == {"query": "example"}
            calls.append(deepcopy(meta))
            if len(calls) == 1 or outcome == "retry-failure":
                raise McpError(
                    ErrorData(
                        code=-32042,
                        message="Payment required",
                        data={
                            "challenges": [
                                {
                                    "id": challenge.id,
                                    "realm": challenge.realm,
                                    "method": challenge.method,
                                    "intent": challenge.intent,
                                    "request": challenge.request,
                                }
                            ]
                        },
                    )
                )
            return CallToolResult(content=[])

    def platform(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        body = json.loads(request.content)
        if outcome == "platform-failure":
            return httpx.Response(200, json={"state": "failed", "problem": {"detail": "Denied"}})
        return httpx.Response(200, json=ready(body["challenge"]))

    async with BuyerMethod(
        ClientOptions(transport=httpx.MockTransport(platform)),
        method=challenge.method,
        intent=challenge.intent,
        subscription_id=ID if kind == "access" else None,
    ) as buyer:
        client = McpClient(Session(), methods=[buyer])
        if outcome == "success":
            assert (
                await client.call_tool("premium_tool", {"query": "example"}, meta=original_meta)
            ).receipt is None
        else:
            with pytest.raises(
                MppPaymentFailedError
                if outcome == "platform-failure"
                else PaymentOutcomeUnknownError
            ):
                await client.call_tool("premium_tool", {"query": "example"}, meta=original_meta)
    assert paths == [
        f"/v1/subscriptions/{ID}/authorize" if kind == "access" else "/v1/transactions/mpp"
    ]
    assert len(calls) == (1 if outcome == "platform-failure" else 2)
    if len(calls) == 2:
        credential = calls[1][META_CREDENTIAL]
        assert isinstance(credential, dict)
        assert credential["source"] == "did:inflow:buyer"
        assert credential["payload"] == {"transactionId": "transaction"}
    assert original_meta == {"application": "test"}
