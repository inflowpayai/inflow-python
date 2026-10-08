import json
from copy import deepcopy

import httpx
import pytest
from fastapi import FastAPI, Request
from mpp import Challenge, Credential, Receipt
from mpp.server.decorator import pay
from starlette.responses import JSONResponse
from x402.schemas import AssetAmount

from inflowpay import ClientOptions, InflowApiError
from inflowpay.mpp import to_pympp_challenge
from inflowpay.mpp.buyer import BuyerMethod, MppPaymentFailedError
from inflowpay.mpp.seller import MppCredentialProblemError, Seller
from inflowpay.x402._seller import SellerConfig, build_offers
from inflowpay.x402.buyer import Buyer
from test_mpp_buyer import WIRE
from test_mpp_seller import RECEIPT, Platform, options
from test_x402_buyer import REQUIREMENT, RESOURCE
from test_x402_buyer import Platform as BuyerPlatform


@pytest.mark.parametrize("scheme", ["instrument", "balance", "exact"])
@pytest.mark.parametrize("selected", [None, "selected-card"])
@pytest.mark.parametrize("extension", [False, True])
async def test_buyer_instrument_selection(
    scheme: str, selected: str | None, extension: bool
) -> None:
    platform = BuyerPlatform()
    platform.supported = {"kinds": [{"scheme": scheme, "network": "inflow:1", "x402Version": 2}]}
    requirement = REQUIREMENT.model_copy(update={"scheme": scheme})
    extra: dict[str, object] = {"custom": {"keep": True}}
    if extension:
        extra["instrumentId"] = "extension-card"
    before = deepcopy(extra)
    async with await Buyer.create(
        ClientOptions(api_key="test-only", transport=platform), instrument_id=selected
    ) as buyer:
        await buyer.prepare(requirement, RESOURCE, transaction_request_extensions=extra)
    body = json.loads(platform.requests[1].content)
    assert body.get("instrumentId") == (
        selected if scheme == "instrument" and selected else "extension-card" if extension else None
    )
    assert body["accept"]["scheme"] == scheme
    assert extra == before


async def test_rejected_instrument_does_not_fallback() -> None:
    creates = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal creates
        if request.method == "GET":
            return httpx.Response(
                200,
                json={"kinds": [{"scheme": "instrument", "network": "inflow:1", "x402Version": 2}]},
            )
        creates += 1
        assert json.loads(request.content)["instrumentId"] == "selected-card"
        return httpx.Response(
            400, json={"errors": [{"code": "PARAMETER_INVALID", "message": "Card unavailable"}]}
        )

    async with await Buyer.create(
        ClientOptions(transport=httpx.MockTransport(handle)), instrument_id="selected-card"
    ) as buyer:
        with pytest.raises(InflowApiError):
            await buyer.prepare(REQUIREMENT.model_copy(update={"scheme": "instrument"}), RESOURCE)
    assert creates == 1


def card_config() -> SellerConfig:
    return SellerConfig.model_validate(
        {
            "sellerId": "seller",
            "assets": [],
            "wallets": [],
            "supported": [],
            "paymentMethods": [
                {
                    "scheme": "instrument",
                    "network": "inflow:1",
                    "payTo": "seller",
                    "decimals": 18,
                    "extra": {"keep": True},
                }
            ],
        }
    )


@pytest.mark.parametrize(
    "price,cents", [("$0.50", 50), ("$1.2500", 125), ("$92233720368547758.07", 9223372036854775807)]
)
def test_instrument_offers(price: str, cents: int) -> None:
    config = card_config()
    before = config.model_dump()
    assert build_offers(config, price) == []
    assert build_offers(config, price, schemes=["instrument"], networks=["other"]) == []
    assert build_offers(config, "1 USDC", schemes=["instrument"]) == []
    offer = build_offers(config, price, schemes=["instrument"])[0]
    assert isinstance(offer.price, AssetAmount)
    assert offer.price.amount == str(cents * 10**16)
    assert offer.price.asset == "USD"
    assert offer.extra == {"keep": True, "assetName": "USD"}
    assert config.model_dump() == before


@pytest.mark.parametrize("price", ["$0.49", "$0", "$1.001", "$92233720368547758.08"])
def test_instrument_invalid_amount(price: str) -> None:
    with pytest.raises(ValueError):
        build_offers(card_config(), price, schemes=["instrument"])


@pytest.mark.parametrize("mode", ["success", "missing", "id", "method"])
async def test_instrument_receipt_binding(mode: str) -> None:
    platform = Platform()
    challenge = Challenge.create(
        secret_key="test-only",
        realm="seller.example",
        method="inflow",
        intent="charge",
        request={"amount": "1", "currency": "USD", "methodDetails": {"rail": "instrument"}},
    )
    receipt = deepcopy(RECEIPT)
    if mode != "missing":
        receipt["challengeId"] = "other" if mode == "id" else challenge.id
    if mode == "method":
        receipt["method"] = "tempo"
    platform.result = {"receipt": receipt}
    credential = Credential(challenge=challenge.to_echo(), payload={"transactionId": "test"})
    async with await Seller.create(options(platform)) as seller:
        if mode == "success":
            result = await seller.broadcast(credential, challenge.request)
            assert result.method == "inflow"
        else:
            with pytest.raises(MppCredentialProblemError):
                await seller.broadcast(credential, challenge.request)
    assert [r.url.path for r in platform.requests] == ["/v1/mpp/config", "/v1/mpp/broadcast"]


@pytest.mark.parametrize("identifier", [None, "original-transaction"])
async def test_failed_payment_keeps_response_identifier(identifier: str | None) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"state": "failed", "transactionId": identifier, "problem": {"title": "Declined"}},
        )

    async with BuyerMethod(ClientOptions(transport=httpx.MockTransport(handle))) as buyer:
        with pytest.raises(MppPaymentFailedError) as result:
            await buyer.create_credential(to_pympp_challenge(WIRE))
    assert result.value.transaction_id == identifier
    assert result.value.problem == {"title": "Declined"}


@pytest.mark.parametrize("matches", [True, False])
async def test_instrument_route_requires_matching_receipt(matches: bool) -> None:
    platform = Platform()
    served = 0
    async with await Seller.create(options(platform)) as seller:
        app = FastAPI()

        @app.get("/paid")
        @pay(
            intent=seller,
            request=seller.charge_request({"amount": "1", "currency": "USD"}),
            method="inflow",
            realm="seller.example",
            secret_key="test-only-secret",
        )
        async def resource(
            request: Request, credential: Credential, receipt: Receipt
        ) -> JSONResponse:
            nonlocal served
            served += 1
            return JSONResponse({"ok": True})

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://seller.example"
        ) as client:
            first = await client.get("/paid")
            challenge = Challenge.from_www_authenticate(first.headers["www-authenticate"])
            platform.result = {
                "receipt": {**RECEIPT, "challengeId": challenge.id if matches else "another"}
            }
            credential = Credential(
                challenge=challenge.to_echo(),
                payload={"transactionId": "test", "type": "instrument"},
            )
            result = await client.get(
                "/paid", headers={"Authorization": credential.to_authorization()}
            )
            assert result.status_code == (200 if matches else 402)
            assert served == int(matches)
            if not matches:
                assert "payment-receipt" not in result.headers
    assert [r.url.path for r in platform.requests] == [
        "/v1/mpp/config",
        "/v1/mpp/validate",
        "/v1/mpp/broadcast",
    ]
