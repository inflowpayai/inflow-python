import base64
import json
import runpy
from collections.abc import Iterator
from copy import deepcopy
from typing import Any

import httpx
import httpx._client
import pytest
import uvicorn
from examples import mpp_buyer, mpp_seller, x402_buyer, x402_seller
from fastapi import FastAPI
from x402.schemas import PaymentPayload, PaymentRequirements

from inflowpay import ClientOptions
from inflowpay.mpp import encode
from test_card_buyer import MERCHANT
from test_card_seller import PAYLOAD as CARD_PAYLOAD
from test_card_seller import platform as card_platform
from test_instrument_parity import card_config
from test_mpp_seller import ID, PROBLEM
from test_mpp_seller import Platform as MppPlatform
from test_stripe_seller import platform as stripe_platform
from test_x402_seller import Platform as X402Platform

MODULES = (mpp_buyer, mpp_seller, x402_buyer, x402_seller)


@pytest.fixture(autouse=True)
def example_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name in (
        "INFLOW_API_KEY",
        "MPP_SECRET_KEY",
        "TARGET_URL",
        "INFLOW_BASE_URL",
        "MPP_METHOD",
        "X402_SCHEME",
        "INFLOW_INSTRUMENT_ID",
        "CARD_MERCHANT_NAME",
        "CARD_MERCHANT_URL",
        "CARD_MERCHANT_COUNTRY",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.mark.parametrize("module", MODULES)
def test_command_entry_requires_credentials(
    module: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as error:
        runpy.run_path(module.__file__, run_name="__main__")
    assert error.value.code == 1
    assert "INFLOW_API_KEY" in capsys.readouterr().err


@pytest.mark.parametrize("module", MODULES)
@pytest.mark.parametrize("cancel", [False, True])
def test_main_success_and_interrupt(
    module: Any, cancel: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def run() -> None:
        if cancel:
            raise KeyboardInterrupt

    monkeypatch.setattr(module, "run", run)
    assert module.main() == (130 if cancel else 0)


@pytest.mark.parametrize(
    "protocol,outcome",
    [
        (protocol, outcome)
        for protocol in ("mpp", "x402", "card", "instrument")
        for outcome in ("paid", "free", "rejected", "missing", "malformed")
    ]
    + [("x402", "settle-failed"), ("x402", "legacy-receipt")],
)
async def test_example_pair(
    protocol: str, outcome: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mode = protocol
    protocol = "mpp" if mode == "card" else "x402" if mode == "instrument" else protocol
    platform = MppPlatform() if protocol == "mpp" else X402Platform()
    if mode == "card":
        platform = card_platform()
        monkeypatch.setenv("MPP_METHOD", "card")
        monkeypatch.setenv("CARD_MERCHANT_NAME", "Test Seller")
        monkeypatch.setenv("CARD_MERCHANT_URL", "https://seller.example")
        monkeypatch.setenv("CARD_MERCHANT_COUNTRY", "US")
    if mode == "instrument":
        assert isinstance(platform, X402Platform)
        platform.config = card_config().model_dump(by_alias=True)
        platform.supported = {
            "kinds": [{"scheme": "instrument", "network": "inflow:1", "x402Version": 2}]
        }
        monkeypatch.setenv("X402_SCHEME", "instrument")
        monkeypatch.setenv("INFLOW_INSTRUMENT_ID", ID)
    if outcome == "rejected":
        if isinstance(platform, MppPlatform):
            platform.validation = {"success": False, "problem": PROBLEM}
        else:
            platform.verification = {"isValid": False, "invalidReason": "invalid_payload"}
    created: list[dict[str, Any]] = []
    merchant_requests: list[httpx.Request] = []
    owned: list[httpx.AsyncBaseTransport] = []
    app_transport: httpx.ASGITransport

    class RoutingTransport(httpx.AsyncBaseTransport):
        closed = False

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.host == "platform.test":
                assert request.headers["x-api-key"] == "secret"
                path = request.url.path
                if path == "/v1/transactions/x402-supported":
                    assert isinstance(platform, X402Platform)
                    return httpx.Response(200, json=platform.supported)
                if path in ("/v1/transactions/mpp", "/v1/transactions/x402"):
                    value = json.loads(request.content)
                    created.append(value)
                    if protocol == "mpp":
                        credential = {
                            "challenge": value["challenge"],
                            "payload": CARD_PAYLOAD if mode == "card" else {"transactionId": ID},
                        }
                        return httpx.Response(
                            200, json={"state": "ready", "credential": encode(credential)}
                        )
                    return httpx.Response(200, json={"transactionId": ID, "approvalId": ID})
                if path == f"/v1/transactions/{ID}/x402":
                    payment = PaymentPayload(
                        accepted=PaymentRequirements.model_validate(created[0]["accept"]),
                        payload={"transactionId": ID},
                    ).model_dump(by_alias=True, exclude_none=True)
                    return httpx.Response(
                        200,
                        json={
                            "encodedPayload": base64.b64encode(
                                json.dumps(payment).encode()
                            ).decode(),
                            "paymentPayload": payment,
                        },
                    )
                if mode == "card" and path.endswith("/broadcast"):
                    assert isinstance(platform, MppPlatform)
                    challenge_id = json.loads(request.content)["credential"]["challenge"]["id"]
                    platform.result = {
                        "receipt": {
                            "method": "card",
                            "status": "success",
                            "challengeId": challenge_id,
                            "reference": "card-test",
                            "timestamp": "2026-10-08T00:00:00Z",
                        }
                    }
                return await platform.handle_async_request(request)
            assert "x-api-key" not in request.headers
            merchant_requests.append(request)
            response = await app_transport.handle_async_request(request)
            await response.aread()
            paid = "payment-signature" in request.headers or "authorization" in request.headers
            if paid:
                header = "payment-receipt" if protocol == "mpp" else "payment-response"
                if outcome == "rejected":
                    assert response.status_code == 402
                    assert "widgets" not in response.text
                if outcome == "missing":
                    response.headers.pop(header)
                if outcome == "malformed":
                    response.headers[header] = "not-a-receipt"
                if outcome == "legacy-receipt":
                    response.headers["x-payment-response"] = response.headers.pop(header)
                if outcome == "settle-failed" and protocol == "x402":
                    response.headers[header] = base64.b64encode(
                        json.dumps(
                            {
                                "success": False,
                                "network": "inflow:1",
                                "transaction": "",
                                "errorReason": "settlement_failed",
                            }
                        ).encode()
                    ).decode()
            return response

        async def aclose(self) -> None:
            self.closed = True

    def transport(*args: object, **kwargs: object) -> RoutingTransport:
        value = RoutingTransport()
        owned.append(value)
        return value

    monkeypatch.setattr(httpx, "AsyncHTTPTransport", transport)
    monkeypatch.setattr(httpx._client, "AsyncHTTPTransport", transport)
    monkeypatch.setenv("INFLOW_API_KEY", "secret")
    monkeypatch.setenv("INFLOW_BASE_URL", "https://platform.test")
    if outcome == "free":
        monkeypatch.setenv("TARGET_URL", "http://merchant.test/free")
    options = ClientOptions(
        environment="sandbox", api_key="secret", base_url="https://platform.test"
    )
    application = (
        mpp_seller.application(
            options, "test-secret", method="card" if mode == "card" else "inflow"
        )
        if protocol == "mpp"
        else x402_seller.application(options, instrument=mode == "instrument")
    )
    async with application as app, httpx.ASGITransport(app) as app_transport:
        buyer = mpp_buyer if protocol == "mpp" else x402_buyer
        fails = outcome in ("rejected", "malformed") or (
            outcome == "settle-failed" and protocol == "x402"
        )
        if fails:
            with pytest.raises((httpx.HTTPStatusError, ValueError)):
                await buyer.run()
        else:
            await buyer.run()
    assert len(created) == (0 if outcome == "free" else 1)
    if created and mode == "card":
        assert created[0]["options"] == {"merchant": MERCHANT}
    if created and mode == "instrument":
        assert created[0]["instrumentId"] == ID
        assert created[0]["accept"]["scheme"] == "instrument"
    assert len(merchant_requests) == (1 if outcome == "free" else 2)
    assert all(isinstance(value, RoutingTransport) and value.closed for value in owned)
    if outcome == "rejected":
        assert not any(
            request.url.path.endswith(("broadcast", "settle")) for request in platform.requests
        )
    output = capsys.readouterr().out
    assert "HTTP" in output and "secret" not in output
    if outcome in ("free", "missing"):
        assert "No seller receipt" in output
    elif outcome == "paid":
        assert "Seller receipt:" in output if protocol == "mpp" else "Seller settlement:" in output


@pytest.mark.parametrize("module,port", [(mpp_seller, 3000), (x402_seller, 3001)])
async def test_seller_run_serves_actual_app(
    module: Any, port: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("INFLOW_API_KEY", "secret")
    monkeypatch.setenv("MPP_SECRET_KEY", "test-secret")
    platform = MppPlatform() if module is mpp_seller else X402Platform()
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: platform)
    monkeypatch.setattr(httpx._client, "AsyncHTTPTransport", lambda **kwargs: platform)
    called = []

    async def serve(server: uvicorn.Server, sockets: object = None) -> None:
        called.append(server.config.port)
        assert server.config.host == "127.0.0.1"
        assert isinstance(server.config.app, FastAPI)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(server.config.app), base_url="http://localhost"
        ) as http:
            assert (await http.get("/free")).json() == {"ok": True}
            assert (await http.get("/api/widgets")).status_code == 402

    monkeypatch.setattr(uvicorn.Server, "serve", serve)
    await module.run()
    assert called == [port]
    assert platform.closed


async def test_x402_seller_rejects_empty_offers(monkeypatch: pytest.MonkeyPatch) -> None:
    platform = X402Platform()
    platform.config = deepcopy(platform.config)
    platform.config["assets"] = []
    platform.config["paymentMethods"] = []
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: platform)
    monkeypatch.setattr(httpx._client, "AsyncHTTPTransport", lambda **kwargs: platform)
    with pytest.raises(ValueError, match="no matching"):
        async with x402_seller.application(ClientOptions(api_key="secret")):
            pytest.fail("empty offers must not start a server")
    assert platform.closed


async def test_mpp_seller_requires_challenge_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INFLOW_API_KEY", "secret")
    with pytest.raises(ValueError, match="MPP_SECRET_KEY"):
        await mpp_seller.run()


@pytest.mark.parametrize("module", MODULES)
async def test_invalid_example_method(module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INFLOW_API_KEY", "test-key")
    monkeypatch.setenv("MPP_SECRET_KEY", "test-secret")
    monkeypatch.setenv("MPP_METHOD", "unsupported")
    monkeypatch.setenv("X402_SCHEME", "unsupported")
    with pytest.raises(ValueError, match="must be"):
        await module.run()


@pytest.mark.parametrize("mode", ["stripe", "card", "instrument"])
async def test_card_seller_startup(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("INFLOW_API_KEY", "secret")
    monkeypatch.setenv("MPP_SECRET_KEY", "test-secret")
    if mode == "instrument":
        platform: MppPlatform | X402Platform = X402Platform()
        assert isinstance(platform, X402Platform)
        platform.config = card_config().model_dump(by_alias=True)
        platform.supported = {
            "kinds": [{"scheme": "instrument", "network": "inflow:1", "x402Version": 2}]
        }
        monkeypatch.setenv("X402_SCHEME", mode)
    else:
        platform = stripe_platform() if mode == "stripe" else card_platform()
        monkeypatch.setenv("MPP_METHOD", mode)
    monkeypatch.setattr(httpx, "AsyncHTTPTransport", lambda **kwargs: platform)
    monkeypatch.setattr(httpx._client, "AsyncHTTPTransport", lambda **kwargs: platform)
    called = []

    async def serve(server: uvicorn.Server, sockets: object = None) -> None:
        called.append(True)
        assert isinstance(server.config.app, FastAPI)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(server.config.app), base_url="http://localhost"
        ) as client:
            response = await client.get("/api/widgets")
            assert response.status_code == 402
            if mode != "instrument":
                from mpp import Challenge

                challenge = Challenge.from_www_authenticate(response.headers["www-authenticate"])
                assert challenge.method == mode
                assert challenge.request["amount"] == "125"

    monkeypatch.setattr(uvicorn.Server, "serve", serve)
    await (x402_seller if mode == "instrument" else mpp_seller).run()
    assert called == [True] and platform.closed
