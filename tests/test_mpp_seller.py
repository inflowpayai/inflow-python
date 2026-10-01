import asyncio
import json
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI, Request
from mpp import Challenge, Credential, Receipt
from mpp.server.decorator import pay
from starlette.responses import JSONResponse

from inflowpay import ClientOptions, InflowApiError
from inflowpay.mpp import WireObject, decode
from inflowpay.mpp.seller import MppCredentialProblemError, MppSellerConfigurationError, Seller

ID = "11111111-1111-4111-8111-111111111111"
TERMS: WireObject = {"amount": "0.50", "currency": "USDC"}
CONFIG: WireObject = {
    "sellerId": ID,
    "featureFlags": {"idempotencyKeyEnabled": True},
    "supportedMethods": [
        {
            "id": "inflow",
            "methodDetails": {
                "currencyRails": {"USDC": {"rail": "balance"}, "USD": {"rail": "instrument"}}
            },
        }
    ],
}
RECEIPT = {
    "method": "inflow",
    "status": "success",
    "reference": "payment",
    "timestamp": "2026-09-30T12:00:00.123456789Z",
    "settlement": {"amount": "0.50", "currency": "USDC"},
    "externalId": "order",
    "custom": {"items": [1, True, None]},
}
PROBLEM = {
    "type": "https://paymentauth.org/problems/payment-required",
    "title": "Payment Required",
    "detail": "Payment was declined",
    "status": 402,
    "hint": "Choose another instrument",
    "details": {"reason": "declined"},
    "extensions": {"trace": {"id": "test"}, "status": 200, "challengeId": "wrong"},
}


class Platform(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.config: object = deepcopy(CONFIG)
        self.result: object = {"receipt": deepcopy(RECEIPT)}
        self.validation: object = "automatic"
        self.requests: list[httpx.Request] = []
        self.closed = False
        self.status = 200

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.headers["x-api-key"] == "secret"
        if request.url.path.endswith("config"):
            return httpx.Response(self.status, json=self.config)
        if request.url.path.endswith("validate"):
            wire = json.loads(request.content)["credential"]
            result = self.validation
            if result == "automatic":
                result = {
                    "success": True,
                    "credential": wire,
                    "challenge": wire["challenge"],
                    "method": wire["challenge"]["method"],
                    "intent": wire["challenge"]["intent"],
                    "source": wire["source"],
                    "request": decode(wire["challenge"]["request"]),
                    "details": {"approved": True},
                }
            return httpx.Response(200, json=result)
        assert request.url.path.endswith("broadcast")
        return httpx.Response(200, json=self.result)

    async def aclose(self) -> None:
        self.closed = True


def options(platform: Platform) -> ClientOptions:
    return ClientOptions(api_key="secret", transport=platform)


def app_for(seller: Seller, *, requires_auth: bool = False) -> FastAPI:
    app = FastAPI()

    @app.get("/paid")
    @pay(
        intent=seller,
        request=seller.charge_request(TERMS),
        method=seller.method,
        realm="seller.example",
        secret_key="local-test-secret",
        requires_auth=requires_auth,
    )
    async def paid(request: Request, credential: Credential, receipt: Receipt) -> JSONResponse:
        return JSONResponse({"ok": True}, headers={"Payment-Receipt": receipt.to_payment_receipt()})

    @app.get("/other")
    @pay(
        intent=seller,
        request=seller.charge_request({**TERMS, "amount": "2"}),
        method=seller.method,
        realm="seller.example",
        secret_key="local-test-secret",
    )
    async def other(request: Request, credential: Credential, receipt: Receipt) -> JSONResponse:
        return JSONResponse({"ok": True})

    return app


async def credential_for(client: httpx.AsyncClient) -> str:
    response = await client.get("/paid")
    assert response.status_code == 402
    challenge = Challenge.from_www_authenticate(response.headers["www-authenticate"])
    assert challenge.request["amount"] == "0.50"
    assert challenge.request["recipient"] == ID
    return Credential(
        challenge=challenge.to_echo(), payload={"transactionId": ID}
    ).to_authorization()


@pytest.mark.parametrize("requires_auth", [False, True])
async def test_framework_roundtrip(requires_auth: bool) -> None:
    platform = Platform()
    async with await Seller.create(options(platform)) as seller:
        original = deepcopy(TERMS)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app_for(seller, requires_auth=requires_auth)),
            base_url="https://seller.example",
        ) as client:
            credential = await credential_for(client)
            header = "Payment-Authorization" if requires_auth else "Authorization"
            response = await client.get("/paid", headers={header: credential})
            assert response.status_code == 200
            assert decode(response.headers["payment-receipt"]) == RECEIPT
            paths = [r.url.path for r in platform.requests]
            assert paths == ["/v1/mpp/config", "/v1/mpp/validate", "/v1/mpp/broadcast"]
            assert json.loads(platform.requests[1].content)["credential"]["source"] == ""
            assert "idempotency-key" not in platform.requests[1].headers
            assert "idempotency-key" in platform.requests[2].headers
            assert (
                await client.get("/other", headers={"Authorization": credential})
            ).status_code == 402
            assert len(platform.requests) == 3
        assert original == TERMS
    assert platform.closed


@pytest.mark.parametrize(
    "response", [None, {}, {"success": False, "problem": PROBLEM}, {"success": True}]
)
async def test_validation_failure_prevents_broadcast(response: object) -> None:
    platform = Platform()
    platform.validation = response
    async with (
        await Seller.create(options(platform)) as seller,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app_for(seller)), base_url="https://seller.example"
        ) as client,
    ):
        credential = await credential_for(client)
        result = await client.get("/paid", headers={"Authorization": credential})
        assert result.status_code == 402
        assert len(platform.requests) == 2
        if isinstance(response, dict) and "problem" in response:
            body = result.json()
            assert body["detail"] == PROBLEM["detail"]
            assert body["trace"] == {"id": "test"}
            assert body["challengeId"] != "wrong"
            assert body["status"] == 402


@pytest.mark.parametrize("response", [None, {}, {"receipt": {}}, {"problem": PROBLEM}])
async def test_broadcast_failure(response: object) -> None:
    platform = Platform()
    platform.result = response
    async with (
        await Seller.create(options(platform)) as seller,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app_for(seller)), base_url="https://seller.example"
        ) as client,
    ):
        credential = await credential_for(client)
        result = await client.get("/paid", headers={"Authorization": credential})
        assert result.status_code == 402
        assert len(platform.requests) == 3


async def test_setup_failure_and_retry() -> None:
    platform = Platform()
    platform.status = 403
    with pytest.raises(InflowApiError):
        await Seller.create(options(platform))
    assert platform.closed
    recovered = Platform()
    async with await Seller.create(options(recovered)) as seller:
        for _ in range(2):
            assert seller.charge_request(TERMS)["recipient"] == ID
        assert len(recovered.requests) == 1


@pytest.mark.parametrize("config", [None, {}, {**CONFIG, "supportedMethods": None}])
async def test_malformed_config_closes_transport(config: object) -> None:
    platform = Platform()
    platform.config = config
    with pytest.raises(ValueError):
        await Seller.create(options(platform))
    assert platform.closed


async def test_invalid_setup() -> None:
    with pytest.raises(MppSellerConfigurationError):
        await Seller.create(ClientOptions())
    with pytest.raises(MppSellerConfigurationError):
        await Seller.create(ClientOptions(api_key="secret"), method="other")


async def test_tempo_request() -> None:
    platform = Platform()
    async with await Seller.create(options(platform), method="tempo") as seller:
        value: WireObject = {
            "amount": "500000",
            "currency": "0x" + "a" * 40,
            "recipient": "0x" + "b" * 40,
        }
        assert seller.charge_request(value)["methodDetails"] == {
            "feePayer": False,
            "supportedModes": ["pull"],
        }
        assert "methodDetails" not in value
        with pytest.raises(ValueError):
            seller.charge_request({**value, "decimals": 6})


@pytest.mark.parametrize(
    ("capabilities", "details", "expected"),
    [
        ({"currencyRails": {}}, {}, None),
        ({"intentCurrencyRails": {"subscription": {"USDC": [{"rail": "balance"}]}}}, {}, None),
        (
            {
                "intentCurrencyRails": {
                    "charge": {"USDC": [{"rail": "balance"}, {"rail": "instrument"}]}
                }
            },
            {},
            None,
        ),
        ({"currencyRails": {"USDC": {"rail": "other"}}}, {}, None),
        ({"currencyRails": {"USDC": {"rail": "balance"}}}, {"rail": "instrument"}, None),
        ({"currencyRails": {"USDC": {"rail": "instrument", "instrumentId": "required"}}}, {}, None),
        (
            {"currencyRails": {"USDC": {"rail": "instrument", "instrumentId": "required"}}},
            {"instrumentId": ID},
            {"rail": "instrument", "instrumentId": ID},
        ),
        (
            {
                "intentCurrencyRails": {
                    "charge": {"USDC": [{"rail": "balance"}, {"rail": "instrument"}]}
                }
            },
            {"rail": "balance"},
            {"rail": "balance"},
        ),
        ({"intentCurrencyRails": {"charge": {"USDC": "bad"}}}, {}, None),
        ({"intentCurrencyRails": {"charge": {"USDC": [None]}}}, {}, None),
    ],
)
async def test_capabilities(
    capabilities: WireObject, details: WireObject, expected: object
) -> None:
    platform = Platform()
    config: WireObject = {
        **CONFIG,
        "supportedMethods": [{"id": "inflow", "methodDetails": capabilities}],
    }
    platform.config = config
    async with await Seller.create(options(platform)) as seller:
        if expected is None:
            with pytest.raises(MppSellerConfigurationError):
                seller.charge_request({**TERMS, "methodDetails": details})
        else:
            assert (
                seller.charge_request({**TERMS, "methodDetails": details})["methodDetails"]
                == expected
            )


@pytest.mark.parametrize("methods", [[], [None, {"id": "other"}]])
async def test_missing_method(methods: object) -> None:
    platform = Platform()
    platform.config = {**CONFIG, "supportedMethods": methods}
    async with await Seller.create(options(platform)) as seller:
        with pytest.raises(MppSellerConfigurationError):
            seller.charge_request(TERMS)


def test_problem_copy_and_render() -> None:
    original = deepcopy(PROBLEM)
    error = MppCredentialProblemError(original)
    original["detail"] = "changed"
    assert error.problem["detail"] == PROBLEM["detail"]
    assert error.to_problem_details("challenge")["challengeId"] == "challenge"
    for value in (None, {**PROBLEM, "status": 999}, {**PROBLEM, "type": ""}):
        assert MppCredentialProblemError(value).to_problem_details()["status"] == 402
    conflict = MppCredentialProblemError({**PROBLEM, "status": 409})
    assert conflict.to_problem_details()["status"] == 409


def wire_credential() -> Credential:
    challenge = Challenge.create(
        method="inflow",
        intent="charge",
        request={**TERMS, "recipient": ID},
        realm="seller.example",
        secret_key="local-secret",
    )
    return Credential(
        challenge=challenge.to_echo(), payload={"transactionId": ID}, source="did:test:buyer"
    )


@pytest.mark.parametrize(
    "field", ["challenge", "credential", "method", "intent", "source", "request", "details"]
)
async def test_validation_response_binding(field: str) -> None:
    platform = Platform()
    credential = wire_credential()
    wire = decode(credential.to_authorization()[8:])
    assert isinstance(wire, dict)
    challenge = wire["challenge"]
    assert isinstance(challenge, dict)
    platform.validation = {
        "success": True,
        "challenge": challenge,
        "credential": wire,
        "method": "inflow",
        "intent": "charge",
        "source": credential.source,
        "request": {},
        "details": {},
        field: "wrong",
    }
    async with await Seller.create(options(platform)) as seller:
        with pytest.raises(MppCredentialProblemError):
            await seller.validate(credential, TERMS)
        assert len(platform.requests) == 2


async def test_normalized_validation_request_and_omitted_details() -> None:
    platform = Platform()
    credential = wire_credential()
    wire = decode(credential.to_authorization()[8:])
    assert isinstance(wire, dict)
    platform.validation = {
        "success": True,
        "challenge": wire["challenge"],
        "credential": wire,
        "method": "inflow",
        "intent": "charge",
        "source": credential.source,
        "request": {"amount": "0.5"},
    }
    async with await Seller.create(options(platform)) as seller:
        terms = deepcopy(TERMS)
        result = await seller.validate(credential, terms)
        assert result.details == {}
        assert result.request == TERMS
        result.request["amount"] = "changed"
        assert terms == TERMS
        assert len(platform.requests) == 2


@pytest.mark.parametrize("enabled", [True, False])
async def test_broadcast_retry_and_concurrent_keys(enabled: bool) -> None:
    class RetryPlatform(Platform):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("broadcast") and not any(
                r.url.path.endswith("broadcast") for r in self.requests
            ):
                self.requests.append(request)
                return httpx.Response(503)
            return await super().handle_async_request(request)

    platform = RetryPlatform()
    platform.config = {**CONFIG, "featureFlags": {"idempotencyKeyEnabled": enabled}}
    async with await Seller.create(options(platform)) as seller:
        credential = wire_credential()
        await seller.broadcast(credential, TERMS)
        first = platform.requests[1:]
        assert len(first) == 2
        assert first[0].content == first[1].content
        assert first[0].headers.get("idempotency-key") == first[1].headers.get("idempotency-key")
        await asyncio.gather(
            seller.broadcast(credential, TERMS), seller.broadcast(credential, TERMS)
        )
        keys = [r.headers.get("idempotency-key") for r in platform.requests[2:]]
        assert len(set(keys)) == (3 if enabled else 1)
        if not enabled:
            assert keys == [None, None, None]


@pytest.mark.parametrize("path", ["config", "validate", "broadcast"])
async def test_cancellation_closes_or_reuses(path: str) -> None:
    entered = asyncio.Event()
    cancelled = asyncio.Event()

    class WaitingPlatform(Platform):
        waiting = True

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if self.waiting and request.url.path.endswith(path):
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    raise
            return await super().handle_async_request(request)

    platform = WaitingPlatform()
    if path == "config":
        task = asyncio.create_task(Seller.create(options(platform)))
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert platform.closed
    else:
        async with await Seller.create(options(platform)) as seller:
            operation = seller.validate if path == "validate" else seller.broadcast
            pending = asyncio.create_task(operation(wire_credential(), TERMS))
            await asyncio.wait_for(entered.wait(), 2)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
            platform.waiting = False
            await operation(wire_credential(), TERMS)
    assert cancelled.is_set()


async def test_real_http_cancellation() -> None:
    entered = asyncio.Event()
    finished = asyncio.Event()

    async def connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            await reader.readuntil(b"\r\n\r\n")
            entered.set()
            await reader.read()
        finally:
            writer.close()
            await writer.wait_closed()
            finished.set()

    async with await asyncio.start_server(connection, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        task = asyncio.create_task(
            Seller.create(ClientOptions(api_key="secret", base_url=f"http://127.0.0.1:{port}"))
        )
        await asyncio.wait_for(entered.wait(), 2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.wait_for(finished.wait(), 2)


async def test_closed_seller_and_http_failure() -> None:
    platform = Platform()
    seller = await Seller.create(options(platform))
    await seller.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await seller.validate(wire_credential(), TERMS)

    async def missing(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("config"):
            return httpx.Response(200, json=CONFIG)
        return httpx.Response(404)

    async with await Seller.create(
        replace(options(Platform()), transport=httpx.MockTransport(missing))
    ) as other:
        with pytest.raises(InflowApiError) as error:
            await other.validate(wire_credential(), TERMS)
        assert error.value.http_status == 404
