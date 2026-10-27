import asyncio
import base64
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Any, Literal

import httpx
import pytest
from fastapi import FastAPI, Request
from starlette.responses import JSONResponse
from x402 import x402ResourceServer
from x402.http.middleware.fastapi import payment_middleware
from x402.schemas import PaymentPayload, PaymentRequirements

from inflowpay.mpp.seller import Seller as MppSeller
from inflowpay.x402 import declare_payment_identifier
from inflowpay.x402._seller import SellerConfig, build_offers
from inflowpay.x402.facilitator import Facilitator
from inflowpay.x402.seller import Seller
from test_mpp_seller import Platform as MppPlatform
from test_mpp_seller import app_for, credential_for, options
from test_x402_seller import CONFIG, Platform


@pytest.mark.parametrize(
    "scheme,network,method,allowed",
    [
        ("exact", "eip155:8453", None, True),
        ("exact", "eip155:8453", "eip3009", True),
        ("exact", "eip155:8453", "permit2", True),
        ("exact", "eip155:8453", "other", False),
        ("exact", "solana:mainnet", None, True),
        ("exact", "solana:mainnet", "solana", True),
        ("exact", "solana:mainnet", "permit2", False),
        ("exact", "other:1", None, False),
        ("balance", "inflow:1", None, True),
        ("instrument", "inflow:1", "default", True),
        ("balance", "inflow:2", None, False),
        ("balance", "inflow:1", "other", False),
        ("upto", "eip155:8453", "permit2", False),
        ("other", "inflow:1", None, False),
    ],
)
async def test_flow_configuration(
    scheme: str, network: str, method: str | None, allowed: bool
) -> None:
    platform = Platform()
    platform.config["assets"] = []
    platform.config["wallets"] = []
    platform.config["paymentMethods"] = [
        {
            "scheme": scheme,
            "network": network,
            "payTo": "seller",
            "decimals": 18,
            "extra": {} if method is None else {"assetTransferMethod": method},
        }
    ]
    before = deepcopy(platform.config)
    async with await Seller.create(platform.options()) as seller:
        options: dict[str, Any] = {"schemes": [scheme]}
        price = "$1" if scheme == "instrument" else "1 USDC"
        omitted = await seller.offers(price, **options)
        explicit = await seller.offers(price, payment_flow="authorization", **options)
        assert omitted == explicit and len(omitted) == 1
        assert "paymentFlow" not in (omitted[0].extra or {})
        if allowed:
            offers = await seller.offers(price, payment_flow="upfront", **options)
            route = await seller.route(price, payment_flow="upfront", **options)
            assert route.accepts == offers
            assert offers[0].extra is not None and offers[0].extra["paymentFlow"] == "upfront"
        else:
            with pytest.raises(ValueError, match="Upfront is not supported"):
                await seller.offers(price, payment_flow="upfront", **options)
        registrations = await seller.scheme_registrations(schemes=[scheme])
        for flow in registrations[0]["server"].payment_flows.values():
            assert flow["default"] == "authorization"
            assert ("upfront" in flow["supported"]) is allowed
        assert (await seller.config()).model_dump(by_alias=True) == SellerConfig.model_validate(
            before
        ).model_dump(by_alias=True)
    assert platform.config == before


async def test_invalid_and_mixed_flows() -> None:
    config = SellerConfig.model_validate(CONFIG)
    invalid: Any = "escrow"
    with pytest.raises(ValueError, match="payment_flow must be"):
        build_offers(config, "$1", payment_flow=invalid)
    config.payment_methods[0].scheme = "unsupported"
    with pytest.raises(ValueError, match="Upfront is not supported"):
        build_offers(config, "$1", payment_flow="upfront")
    config = SellerConfig.model_validate(CONFIG)
    before = config.model_copy(deep=True)
    assert all(
        (offer.extra or {}).get("paymentFlow") == "upfront"
        for offer in build_offers(config, "$1", payment_flow="upfront")
    )
    assert config == before


class FlowPlatform(Platform):
    def __init__(self, outcome: str = "success") -> None:
        super().__init__()
        self.outcome = outcome
        self.events: list[str] = []
        self.settlements: list[dict[str, Any]] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/verify"):
            self.events.append("verify")
        if request.url.path.endswith("/settle"):
            self.events.append("settle")
            self.settlements.append(json.loads(request.content))
            self.started.set()
            if self.outcome == "held":
                await self.release.wait()
            if self.outcome in ("pending", "pending-resolved") and (
                self.outcome == "pending" or len(self.settlements) == 1
            ):
                return httpx.Response(
                    409, json={"errorReason": "idempotency_pending"}, headers={"Retry-After": "0"}
                )
            if self.outcome == "uncertain":
                raise httpx.ReadError("Connection lost after sending payment")
            if self.outcome == "invalid":
                return httpx.Response(200, content=b"{")
            if self.outcome == "empty":
                return httpx.Response(200, json={})
            if self.outcome == "rejected":
                return httpx.Response(
                    200,
                    json={"success": False, "transaction": "", "errorReason": "invalid_payload"},
                )
        return await super().handle_async_request(request)


@asynccontextmanager
async def merchant(
    platform: FlowPlatform,
    *,
    flow: Literal["authorization", "upfront"] = "upfront",
    scheme: str = "balance",
    handler_status: int = 200,
    throws: bool = False,
) -> AsyncIterator[tuple[httpx.AsyncClient, PaymentPayload]]:
    # Only platform HTTP is simulated. Registration, matching, settlement ordering,
    # receipts and the application dispatch use the actual upstream middleware.
    async with (
        await Seller.create(platform.options()) as seller,
        await Facilitator.create(platform.options()) as facilitator,
    ):
        resource = x402ResourceServer(facilitator)
        for registration in await seller.scheme_registrations():
            resource.register(registration["network"], registration["server"])
        route = await seller.route("$1", schemes=[scheme], payment_flow=flow)
        app = FastAPI()
        app.middleware("http")(payment_middleware({"GET /paid": route}, resource))

        @app.get("/paid")
        async def paid(request: Request) -> JSONResponse:
            platform.events.append("handler")
            assert request.headers["authorization"] == "Bearer application"
            assert "x-api-key" not in request.headers
            if throws:
                raise ValueError("Application failed after payment")
            return JSONResponse({"paid": True}, status_code=handler_status)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app, raise_app_exceptions=False),
            base_url="https://seller.example",
            headers={"authorization": "Bearer application"},
        ) as client:
            response = await client.get("/paid")
            assert response.status_code == 402 and platform.events == []
            challenge = json.loads(base64.b64decode(response.headers["payment-required"]))
            yield (
                client,
                PaymentPayload(
                    accepted=PaymentRequirements.model_validate(challenge["accepts"][0]),
                    payload={"transactionId": "same-payment"},
                ),
            )


def payment_headers(payment: PaymentPayload) -> dict[str, str]:
    return {
        "payment-signature": base64.b64encode(
            payment.model_dump_json(by_alias=True, exclude_none=True).encode()
        ).decode()
    }


@pytest.mark.parametrize(
    "outcome,attempts,handler_status,throws,runs",
    [
        ("success", 1, 200, False, True),
        ("success", 1, 500, False, True),
        ("success", 1, 500, True, True),
        ("rejected", 1, 200, False, False),
        ("pending", 5, 200, False, False),
        ("pending-resolved", 2, 200, False, True),
        ("uncertain", 1, 200, False, False),
        ("invalid", 1, 200, False, False),
        ("empty", 1, 200, False, False),
    ],
)
async def test_upfront_lifecycle(
    outcome: str, attempts: int, handler_status: int, throws: bool, runs: bool
) -> None:
    platform = FlowPlatform(outcome)
    async with merchant(platform, handler_status=handler_status, throws=throws) as (http, payment):
        response = await http.get("/paid", headers=payment_headers(payment))
    assert platform.events == ["settle"] * attempts + (["handler"] if runs else [])
    assert all(value == platform.settlements[0] for value in platform.settlements)
    sent = platform.settlements[0]
    assert sent["paymentRequirements"]["extra"]["paymentFlow"] == "upfront"
    assert sent["paymentPayload"]["accepted"]["extra"]["paymentFlow"] == "upfront"
    assert sent["paymentPayload"]["extensions"]["payment-identifier"]["info"]["id"]
    if runs:
        assert response.status_code == handler_status
        receipt = json.loads(base64.b64decode(response.headers["payment-response"]))
        assert receipt["success"] is True and receipt["transaction"] == "tx"
        assert "private" in response.headers["cache-control"]
    else:
        assert response.status_code >= 400 and '"paid":true' not in response.text
    assert payment.extensions is None


@pytest.mark.parametrize("success", [True, False])
async def test_handler_waits_for_settlement(success: bool) -> None:
    platform = FlowPlatform("held")
    platform.settlement = {"success": success, "transaction": "tx", "network": "inflow:1"}
    async with merchant(platform) as (http, payment):
        task = asyncio.create_task(http.get("/paid", headers=payment_headers(payment)))
        try:
            await asyncio.wait_for(platform.started.wait(), 3)
            assert platform.events == ["settle"] and not task.done()
        finally:
            platform.release.set()
            response = await asyncio.wait_for(task, 3)
        assert platform.events == ["settle"] + (["handler"] if success else [])
        assert (response.status_code == 200) is success


@pytest.mark.parametrize("flow", ["authorization", "upfront"])
@pytest.mark.parametrize("scheme", ["balance", "instrument", "exact", "solana", "permit2"])
async def test_order_and_matching(flow: Literal["authorization", "upfront"], scheme: str) -> None:
    platform = FlowPlatform()
    if scheme == "permit2":
        scheme = "exact"
        platform.config["assets"][0]["assetTransferMethod"] = "permit2"
        platform.config["assets"] = platform.config["assets"][:1]
    if scheme == "instrument":
        platform.config["paymentMethods"].append(
            {"scheme": scheme, "network": "inflow:1", "payTo": "seller", "decimals": 18}
        )
        platform.supported["kinds"].append(
            {"scheme": scheme, "network": "inflow:1", "x402Version": 2}
        )
    if scheme == "solana":
        scheme = "exact"
        platform.config["assets"][0].update(network="solana:mainnet", assetTransferMethod="solana")
        platform.config["assets"] = platform.config["assets"][:1]
        platform.supported["kinds"] = [
            {"scheme": scheme, "network": "solana:mainnet", "x402Version": 2}
        ]
    async with merchant(platform, flow=flow, scheme=scheme) as (http, payment):
        for mutation in ("amount", "malformed", "flow", "missing-flow"):
            if flow == "authorization" and mutation in ("flow", "missing-flow"):
                continue
            altered = payment.model_copy(deep=True)
            if mutation == "amount":
                altered.accepted.amount = "1"
            elif mutation in ("flow", "missing-flow"):
                assert altered.accepted.extra is not None
                if mutation == "flow":
                    altered.accepted.extra["paymentFlow"] = "escrow"
                else:
                    del altered.accepted.extra["paymentFlow"]
            headers = payment_headers(altered)
            if mutation == "malformed":
                headers["payment-signature"] = "invalid"
            response = await http.get("/paid", headers=headers)
            assert response.status_code >= 400 and platform.events == []
        response = await http.get("/paid", headers=payment_headers(payment))
        assert response.status_code == 200
        assert platform.events == (
            ["verify", "handler", "settle"] if flow == "authorization" else ["settle", "handler"]
        )


async def test_repeated_and_concurrent_submissions() -> None:
    platform = FlowPlatform()
    async with merchant(platform, handler_status=500) as (http, payment):
        identifier = declare_payment_identifier()
        identifier["info"]["id"] = "pay_12345678901234567890123456789012"
        payment.extensions = {"payment-identifier": identifier}
        before = payment.model_copy(deep=True)
        responses = await asyncio.gather(
            *(http.get("/paid", headers=payment_headers(payment)) for _ in range(3))
        )
        responses.append(await http.get("/paid", headers=payment_headers(payment)))
        assert all(response.status_code == 500 for response in responses)
        assert all("payment-response" in response.headers for response in responses)
        assert len(platform.settlements) == platform.events.count("handler") == 4
        assert all(value == platform.settlements[0] for value in platform.settlements)
        assert platform.settlements[0]["paymentPayload"]["extensions"] == payment.extensions
        assert payment == before


async def test_cancelled_settlement_does_not_run_handler() -> None:
    platform = FlowPlatform("held")
    async with merchant(platform) as (http, payment):
        task = asyncio.create_task(http.get("/paid", headers=payment_headers(payment)))
        try:
            await asyncio.wait_for(platform.started.wait(), 3)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert platform.events == ["settle"]
        finally:
            platform.release.set()


async def test_mpp_and_x402_are_alternatives() -> None:
    platform, mpp_platform = FlowPlatform(), MppPlatform()
    async with (
        merchant(platform) as (xclient, payment),
        await MppSeller.create(options(mpp_platform)) as mpp_seller,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app_for(mpp_seller)), base_url="https://seller.example"
        ) as mclient,
    ):
        credential = await credential_for(mclient)

        # The application chooses one protocol. Both payment handlers are real;
        # this selector is not an SDK-provided combined-route wrapper.
        async def route(request: httpx.Request) -> httpx.Response:
            if request.headers.get("authorization", "").startswith("Payment "):
                return await mclient.get("/paid", headers=dict(request.headers))
            if "payment-signature" in request.headers:
                return await xclient.get("/paid", headers=dict(request.headers))
            mresponse, xresponse = await mclient.get("/paid"), await xclient.get("/paid")
            return httpx.Response(
                402,
                headers={
                    "www-authenticate": mresponse.headers["www-authenticate"],
                    "payment-required": xresponse.headers["payment-required"],
                },
            )

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(route), base_url="https://seller.example"
        ) as client:
            challenge = await client.get("/paid")
            assert challenge.status_code == 402
            assert (
                "www-authenticate" in challenge.headers and "payment-required" in challenge.headers
            )
            mresponse = await client.get("/paid", headers={"authorization": credential})
            assert mresponse.status_code == 200 and "payment-receipt" in mresponse.headers
            assert platform.events == []
            mpp_calls = len(mpp_platform.requests)
            xresponse = await client.get("/paid", headers=payment_headers(payment))
            assert xresponse.status_code == 200 and "payment-response" in xresponse.headers
            assert platform.events == ["settle", "handler"]
            assert len(mpp_platform.requests) == mpp_calls
