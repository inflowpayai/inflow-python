import httpx
import pytest
from fastapi import FastAPI, Request
from mpp import Challenge, Credential
from mpp.server import Mpp
from mpp.server.asgi import PaymentMiddleware
from x402 import x402ResourceServer
from x402.http.paywall import create_paywall, evm_paywall
from x402.http.types import RouteConfig

from inflowpay.mpp import decode
from inflowpay.mpp.seller import Seller as MppSeller
from inflowpay.x402.facilitator import Facilitator
from inflowpay.x402.fastapi import payment_middleware
from inflowpay.x402.seller import Seller
from test_mpp_seller import ID, RECEIPT, Platform, options
from test_x402_upfront import FlowPlatform


async def test_native_asgi_payment_preserves_application_authorization() -> None:
    platform = Platform()
    async with await MppSeller.create(options(platform)) as seller:
        server = Mpp(
            methods=[seller],
            realm="seller.example",
            secret_key="test-only-secret",
            requires_auth=True,
        )
        handler = server.charge(**seller.charge_request({"amount": "0.50", "currency": "USDC"}))
        app = FastAPI()
        calls: list[str] = []

        @app.get("/paid")
        async def paid(request: Request) -> dict[str, bool]:
            calls.append(request.headers["authorization"])
            return {"ok": True}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(PaymentMiddleware(app, handler)),
            base_url="https://seller.example",
            headers={"Authorization": "Bearer application"},
        ) as client:
            response = await client.get("/paid")
            assert response.status_code == 402
            assert not calls
            challenge = Challenge.from_www_authenticate(response.headers["www-authenticate"])
            assert challenge.header == "Payment-Authorization"
            credential = Credential(challenge=challenge.to_echo(), payload={"transactionId": ID})
            response = await client.get(
                "/paid", headers={"Payment-Authorization": credential.to_authorization()}
            )
            assert response.status_code == 200
            assert calls == ["Bearer application"]
            assert decode(response.headers["payment-receipt"]) == RECEIPT


@pytest.mark.parametrize("dynamic", [False, True])
async def test_native_evm_paywall_and_api_challenge(dynamic: bool) -> None:
    platform = FlowPlatform()
    async with (
        await Seller.create(platform.options()) as seller,
        await Facilitator.create(platform.options()) as facilitator,
    ):
        resource = x402ResourceServer(facilitator)
        for registration in await seller.scheme_registrations():
            resource.register(registration["network"], registration["server"])
        route = await seller.route("0.10 USDC", schemes=["exact"])

        def resolve(_: Request) -> RouteConfig:
            return route

        app = FastAPI()
        paywall = create_paywall().with_network(evm_paywall).with_config(app_name="Reports").build()
        app.middleware("http")(
            payment_middleware(
                {"GET /report": resolve if dynamic else route}, resource, paywall_provider=paywall
            )
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://seller.example"
        ) as client:
            browser = await client.get(
                "/report", headers={"Accept": "text/html", "User-Agent": "Mozilla/5.0"}
            )
            assert browser.status_code == 402
            assert "text/html" in browser.headers["content-type"]
            assert "Reports" in browser.text
            api = await client.get("/report", headers={"Accept": "application/json"})
            assert api.status_code == 402
            assert "payment-required" in api.headers
            assert not platform.events
