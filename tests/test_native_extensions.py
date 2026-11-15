from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import cast

import httpx
import pytest
from eth_account import Account
from fastapi import FastAPI
from x402 import x402ResourceServer
from x402.extensions.builder_code import (
    BuilderCodeClientExtension,
    BuilderCodeFacilitatorExtension,
    declare_builder_code_extension,
    parse_builder_code_suffix_from_calldata,
)
from x402.extensions.sign_in_with_x import (
    CreateSIWxClientExtensionOptions,
    CreateSIWxResourceServerExtensionOptions,
    InMemorySIWxStorage,
    create_siwx_client_extension,
    create_siwx_resource_server_extension,
    declare_siwx_extension,
    encode_siwx_header,
    parse_siwx_header,
)
from x402.http import x402HTTPClient
from x402.http.clients.httpx import x402AsyncTransport
from x402.http.types import RouteConfig
from x402.http.utils import (
    decode_payment_required_header,
    decode_payment_response_header,
    encode_payment_signature_header,
)
from x402.mechanisms.evm.exact import ExactEvmScheme
from x402.schemas import PaymentPayload, PaymentRequired
from x402.schemas.extensions import ClientExtension

from inflowpay import ClientOptions
from inflowpay.x402.buyer import Buyer
from inflowpay.x402.facilitator import Facilitator
from inflowpay.x402.fastapi import payment_middleware
from inflowpay.x402.seller import Seller
from test_x402_upfront import FlowPlatform

WALLET = Account.from_key("0x" + "11" * 32)
ORIGIN = "https://seller.example"
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


class NonceStorage(InMemorySIWxStorage):
    # Exercise the upstream storage hooks; the supplied in-memory store has no nonce tracking.
    def __init__(self) -> None:
        super().__init__()
        self.nonces: set[str] = set()

    def has_used_nonce(self, nonce: str) -> bool:
        return nonce in self.nonces

    def record_nonce(self, nonce: str) -> None:
        self.nonces.add(nonce)


@asynccontextmanager
async def merchant(
    platform: FlowPlatform, storage: NonceStorage, *, dynamic: bool = False
) -> AsyncIterator[FastAPI]:
    platform.config["assets"][0]["assetId"] = USDC
    async with (
        await Seller.create(platform.options()) as seller,
        await Facilitator.create(platform.options()) as facilitator,
    ):
        resource = x402ResourceServer(facilitator)
        for registration in await seller.scheme_registrations():
            resource.register(registration["network"], registration["server"])
        resource.register_extension(
            create_siwx_resource_server_extension(
                CreateSIWxResourceServerExtensionOptions(origin=ORIGIN, storage=storage)
            )
        )
        route = await seller.route("0.10 USDC", schemes=["exact"])
        route.extensions = declare_siwx_extension()

        def resolve(_: object) -> RouteConfig:
            return route

        app = FastAPI()
        app.middleware("http")(
            payment_middleware({"GET /report": resolve if dynamic else route}, resource)
        )

        @app.get("/report")
        async def report() -> dict[str, bool]:
            platform.events.append("handler")
            return {"ok": True}

        yield app


@asynccontextmanager
async def external_buyer() -> AsyncIterator[Buyer]:
    def platform(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/transactions/x402-supported"
        return httpx.Response(200, json={"kinds": []})

    async with await Buyer.create(
        ClientOptions(api_key="synthetic", transport=httpx.MockTransport(platform))
    ) as buyer:
        buyer.register("eip155:8453", ExactEvmScheme(WALLET))
        buyer.register_extension(
            create_siwx_client_extension(CreateSIWxClientExtensionOptions(signers=[WALLET]))
        )
        yield buyer


@pytest.mark.parametrize("dynamic", [False, True])
async def test_native_siwx_http_authentication_without_payment(dynamic: bool) -> None:
    storage = NonceStorage()
    storage.record_payment("/report", WALLET.address)
    platform = FlowPlatform()
    async with merchant(platform, storage, dynamic=dynamic) as app, external_buyer() as buyer:
        # A policy rejecting all payments still permits native wallet authentication.
        buyer.register_policy(lambda version, requirements: [])
        async with httpx.AsyncClient(
            transport=x402AsyncTransport(buyer, httpx.ASGITransport(app)), base_url=ORIGIN
        ) as client:
            response = await client.get("/report")
        assert response.status_code == 200
        assert platform.events == ["handler"]
        assert len(storage.nonces) == 1


async def test_siwx_origin_unpaid_wallet_and_nonce_replay() -> None:
    platform = FlowPlatform()
    storage = NonceStorage()
    async with (
        merchant(platform, storage) as app,
        external_buyer() as buyer,
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=ORIGIN) as client,
    ):
        response = await client.get("/report")
        required = decode_payment_required_header(response.headers["payment-required"])
        assert isinstance(required, PaymentRequired)
        http = x402HTTPClient(buyer)
        assert await http.handle_payment_required(required, "https://other.example/report") is None
        headers = await http.handle_payment_required(required, ORIGIN + "/report")
        assert headers
        assert (await client.get("/report", headers=headers)).status_code == 402
        assert not storage.nonces
        storage.record_payment("/report", WALLET.address)
        assert (await client.get("/report", headers=headers)).status_code == 200
        assert (await client.get("/report", headers=headers)).status_code == 402
        assert platform.events == ["handler"]


@pytest.mark.parametrize("fault", ["signature", "origin", "expired"])
async def test_siwx_rejects_invalid_proofs_without_handler(fault: str) -> None:
    platform = FlowPlatform()
    storage = NonceStorage()
    storage.record_payment("/report", WALLET.address)
    async with (
        merchant(platform, storage) as app,
        external_buyer() as buyer,
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=ORIGIN) as client,
    ):
        response = await client.get("/report")
        required = decode_payment_required_header(response.headers["payment-required"])
        headers = await x402HTTPClient(buyer).handle_payment_required(required, ORIGIN + "/report")
        assert headers
        proof = parse_siwx_header(headers["sign-in-with-x"])
        if fault == "signature":
            proof.signature = "0x" + "00" * 65
        elif fault == "origin":
            proof.domain = "other.example"
        else:
            proof.expiration_time = "2020-01-01T00:00:00Z"
        response = await client.get(
            "/report", headers={"sign-in-with-x": encode_siwx_header(proof)}
        )
        assert response.status_code == 402
        assert not storage.nonces
        assert not platform.events


@pytest.mark.parametrize("success", [False, True])
async def test_only_successful_settlement_records_wallet_access(success: bool) -> None:
    platform = FlowPlatform()
    platform.settlement.update(payer=WALLET.address, success=success, network="eip155:8453")
    if not success:
        platform.settlement.update(transaction="", errorReason="invalid_payload")
    storage = NonceStorage()
    async with (
        merchant(platform, storage) as app,
        external_buyer() as buyer,
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=ORIGIN) as client,
    ):
        response = await client.get("/report")
        required = decode_payment_required_header(response.headers["payment-required"])
        payload = await buyer.create_payment_payload(required)
        response = await client.get(
            "/report", headers={"payment-signature": encode_payment_signature_header(payload)}
        )
        assert response.status_code == (200 if success else 402)
        assert storage.has_paid("/report", WALLET.address) is success
        receipt = decode_payment_response_header(response.headers["payment-response"])
        assert receipt.success is success
        assert platform.events == ["verify", "handler", "settle"]


async def test_builder_codes_with_native_external_signing_and_suffix_encoding() -> None:
    platform = FlowPlatform()
    async with merchant(platform, NonceStorage()) as app, external_buyer() as buyer:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url=ORIGIN) as client:
            response = await client.get("/report")
        required = decode_payment_required_header(response.headers["payment-required"])
        assert isinstance(required, PaymentRequired)
        required.extensions = {
            "builder-code": declare_builder_code_extension("seller_app", ["seller_service"])
        }
        before = deepcopy(required)
        # Upstream's protocol requires hooks that its own builder extension omits;
        # registration and execution use getattr for these optional hooks.
        buyer.register_extension(cast(ClientExtension, BuilderCodeClientExtension("buyer_service")))
        payload = await buyer.create_payment_payload(required)
        assert isinstance(payload, PaymentPayload)
        assert payload.payload["signature"].startswith("0x")
        assert payload.extensions is not None
        assert payload.extensions["builder-code"]["info"] == {
            "a": "seller_app",
            "s": ["buyer_service", "seller_service"],
        }
        facilitator = BuilderCodeFacilitatorExtension(
            builder_code="facilitator_wallet", service_code="facilitator_service"
        )
        suffix = facilitator.build_data_suffix(payload, payload.accepted)
        assert suffix is not None
        decoded = parse_builder_code_suffix_from_calldata(suffix)
        assert decoded is not None
        assert decoded.a == "seller_app"
        assert decoded.w == "facilitator_wallet"
        assert decoded.s == ["buyer_service", "seller_service", "facilitator_service"]
        assert required == before
        required.extensions = {}
        async with external_buyer() as plain_buyer:
            plain = await plain_buyer.create_payment_payload(required)
        assert isinstance(plain, PaymentPayload)
        assert not (plain.extensions or {}).get("builder-code")
