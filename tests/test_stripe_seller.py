import json
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest
import uvicorn
from examples import mpp_seller
from fastapi import FastAPI, Request
from mpp import Challenge, Credential, Receipt
from mpp.errors import InvalidChallengeError
from mpp.server.decorator import pay
from starlette.responses import JSONResponse

from inflowpay.mpp import MppCodecError, WireObject, decode
from inflowpay.mpp.seller import MppSellerConfigurationError, Seller
from test_mpp_seller import CONFIG, PROBLEM, RECEIPT, Platform, options


def platform() -> Platform:
    result = Platform()
    config: WireObject = {
        **deepcopy(CONFIG),
        "supportedMethods": [
            {
                "id": "stripe",
                "supportedCurrencies": ["USD"],
                "supportedIntents": ["charge"],
                "methodDetails": {
                    "networkId": "profile_test",
                    "paymentMethodTypes": ["card", "link"],
                },
            }
        ],
    }
    result.config = config
    return result


@pytest.mark.parametrize(
    "amount,cents", [("0.50", "50"), ("1", "100"), ("1.25", "125"), ("999999.99", "99999999")]
)
async def test_prepare(amount: str, cents: str) -> None:
    transport = platform()
    request: WireObject = {
        "amount": amount,
        "currency": "eur",
        "decimals": 18,
        "networkId": "untrusted",
        "paymentMethodTypes": ["other"],
        "externalId": "",
        "recipient": "seller",
        "description": "Report",
        "metadata": {"purpose": ""},
    }
    before = deepcopy(request)
    async with await Seller.create(options(transport), method="stripe") as seller:
        prepared = seller.stripe_request(request)
        assert prepared == {
            "amount": cents,
            "currency": "usd",
            "externalId": "",
            "recipient": "seller",
            "description": "Report",
            "methodDetails": {
                "networkId": "profile_test",
                "paymentMethodTypes": ["card", "link"],
                "metadata": {"purpose": ""},
            },
        }
        assert request == before
        prepared["methodDetails"] = {}
        assert seller.stripe_request({"amount": amount})["methodDetails"] != {}
        with pytest.raises(MppSellerConfigurationError, match="stripe_request"):
            seller.charge_request({"amount": "125"})
    assert transport.closed


async def test_wrong_method() -> None:
    async with await Seller.create(options(Platform())) as seller:
        with pytest.raises(MppSellerConfigurationError, match="Stripe Seller"):
            seller.stripe_request({"amount": "1"})


@pytest.mark.parametrize("payload", [{}, {"spt": 3}, {"spt": "spt_test", "externalId": None}])
async def test_invalid_payload(payload: dict[str, object]) -> None:
    transport = platform()
    async with await Seller.create(options(transport), method="stripe") as seller:
        terms = seller.stripe_request({"amount": "1"})
        challenge = Challenge.create(
            secret_key="test-only", realm="test", method="stripe", intent="charge", request=terms
        )
        credential = Credential(challenge=challenge.to_echo(), payload=payload)
        with pytest.raises(InvalidChallengeError):
            await seller.validate(credential, terms)
    assert len(transport.requests) == 1


async def test_stripe_example() -> None:
    transport = platform()
    async with (
        mpp_seller.application(options(transport), "test-only", method="stripe") as app,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://seller.example"
        ) as client,
    ):
        response = await client.get("/api/widgets")
        challenge = Challenge.from_www_authenticate(response.headers["www-authenticate"])
        assert challenge.method == "stripe"
        terms = decode(challenge.request_b64)
        assert isinstance(terms, dict)
        assert terms["amount"] == "125"


@pytest.mark.parametrize("method", ["stripe", "unknown"])
async def test_example_startup(method: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MPP_METHOD", method)
    monkeypatch.setenv("INFLOW_API_KEY", "secret")
    monkeypatch.setenv("MPP_SECRET_KEY", "test-only")
    transport = platform()
    monkeypatch.setattr(httpx._client, "AsyncHTTPTransport", lambda **kwargs: transport)
    served = []

    async def serve(server: uvicorn.Server, sockets: object = None) -> None:
        served.append(server.config.port)

    monkeypatch.setattr(uvicorn.Server, "serve", serve)
    if method == "unknown":
        with pytest.raises(ValueError, match="MPP_METHOD"):
            await mpp_seller.run()
        assert not transport.requests
    else:
        await mpp_seller.run()
        assert served == [3000]
        assert transport.closed


@pytest.mark.parametrize(
    "offer",
    [
        {},
        {"amount": 1},
        {"amount": "NaN"},
        {"amount": "1.001"},
        {"amount": "00.50"},
        {"amount": "0"},
        {"amount": "0.49"},
        {"amount": "1000000"},
        {"amount": "1", "metadata": []},
        {"amount": "1", "metadata": {str(i): "v" for i in range(46)}},
        *[
            {"amount": "1", "metadata": {key: "v"}}
            for key in (
                " ",
                "k" * 41,
                "bad[",
                "bad]",
                "externalId",
                "inflowMppTransactionId",
                "mppChallengeId",
                "mppIntent",
                "mppMethod",
                "stripeNetworkProfile",
            )
        ],
        {"amount": "1", "metadata": {"k": 3}},
        {"amount": "1", "metadata": {"k": "v" * 501}},
        {"amount": "1", "externalId": "x" * 256},
        {"amount": "1", "externalId": 3},
        {"amount": "1", "description": None},
        {"amount": "1", "recipient": False},
    ],
)
async def test_bad_offer(offer: WireObject) -> None:
    transport = platform()
    async with await Seller.create(options(transport), method="stripe") as seller:
        with pytest.raises(MppCodecError):
            seller.stripe_request(offer)
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("supportedCurrencies", None),
        ("supportedCurrencies", ["EUR"]),
        ("supportedIntents", None),
        ("supportedIntents", ["subscription"]),
        ("methodDetails", None),
        ("networkId", None),
        ("networkId", " "),
        ("paymentMethodTypes", None),
        ("paymentMethodTypes", []),
        ("paymentMethodTypes", [" "]),
        ("paymentMethodTypes", [3]),
        ("id", "other"),
    ],
)
async def test_unavailable_configuration(field: str, value: object) -> None:
    transport = platform()
    config = json.loads(json.dumps(transport.config))
    entry = config["supportedMethods"][0]
    if field in ("networkId", "paymentMethodTypes"):
        entry["methodDetails"][field] = value
    else:
        entry[field] = value
    transport.config = config
    with pytest.raises(MppSellerConfigurationError):
        await Seller.create(options(transport), method="stripe")
    assert transport.closed


@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "validation",
        "pending",
        "method",
        "challenge",
        "missing",
        "reference",
        "signature",
        "expiry",
        "route",
    ],
)
async def test_protected_route(outcome: str) -> None:
    transport = platform()
    async with await Seller.create(options(transport), method="stripe") as seller:
        app = FastAPI()
        delivered = []
        terms = seller.stripe_request({"amount": "1.25", "externalId": "order"})

        @app.get("/paid")
        @pay(
            intent=seller,
            method="stripe",
            request=lambda _: terms,
            realm="seller.example",
            secret_key="test-only-secret",
        )
        async def paid(request: Request, credential: Credential, receipt: Receipt) -> JSONResponse:
            delivered.append(True)
            return JSONResponse(
                {"ok": True}, headers={"Payment-Receipt": receipt.to_payment_receipt()}
            )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://seller.example"
        ) as client:
            initial = await client.get("/paid")
            challenge = Challenge.from_www_authenticate(initial.headers["www-authenticate"])
            if outcome == "expiry":
                challenge = Challenge.create(
                    secret_key="test-only-secret",
                    realm="seller.example",
                    method="stripe",
                    intent="charge",
                    request=json.loads(json.dumps(decode(challenge.request_b64))),
                    expires="2020-01-01T00:00:00Z",
                )
            credential = Credential(
                challenge=challenge.to_echo(), payload={"spt": "spt_test", "externalId": "order"}
            )
            receipt = {
                **RECEIPT,
                "method": "stripe",
                "challengeId": challenge.id,
                "reference": "pi_test",
                "settlement": {"amount": "125", "currency": "usd"},
            }
            if outcome == "validation":
                transport.validation = {"success": False, "problem": PROBLEM}
            if outcome == "pending":
                transport.result = {"problem": PROBLEM}
            else:
                if outcome == "method":
                    receipt["method"] = "inflow"
                if outcome == "challenge":
                    receipt["challengeId"] = "other"
                if outcome == "missing":
                    del receipt["challengeId"]
                transport.result = {"receipt": receipt}
            if outcome == "reference":
                credential = replace(credential, payload={"spt": "spt_test", "externalId": "other"})
            if outcome == "signature":
                credential = replace(
                    credential, challenge=replace(credential.challenge, id="tampered")
                )
            if outcome == "route":
                terms = seller.stripe_request({"amount": "2", "externalId": "order"})
            response = await client.get(
                "/paid", headers={"Authorization": credential.to_authorization()}
            )
            assert response.status_code == (200 if outcome == "success" else 402)
            assert delivered == ([True] if outcome == "success" else [])
            if outcome == "success":
                assert decode(response.headers["payment-receipt"]) == receipt
                wire = json.loads(transport.requests[1].content)["credential"]
                assert wire["source"] == ""
                assert wire["payload"]["spt"] == "spt_test"
            else:
                assert "payment-receipt" not in response.headers
            expected = (
                1
                if outcome in ("reference", "signature", "expiry", "route")
                else 2
                if outcome == "validation"
                else 3
            )
            assert len(transport.requests) == expected
