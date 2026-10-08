import json
from copy import deepcopy
from dataclasses import replace

import httpx
import pytest
from fastapi import FastAPI, Request
from mpp import Challenge, Credential, Receipt
from mpp.errors import InvalidChallengeError
from mpp.server.decorator import pay
from starlette.responses import JSONResponse

from inflowpay.mpp import MppCodecError, WireObject, decode
from inflowpay.mpp._requests import validate_payload, validate_request
from inflowpay.mpp.seller import MppSellerConfigurationError, Seller
from test_mpp_seller import CONFIG, PROBLEM, RECEIPT, Platform, options

KEY: WireObject = {
    "kty": "RSA",
    "alg": "RSA-OAEP-256",
    "use": "enc",
    "kid": "test-key",
    "n": "test_modulus",
    "e": "AQAB",
}
DETAILS: WireObject = {
    "merchantName": "Test Seller",
    "acceptedNetworks": ["visa"],
    "encryptionJwk": KEY,
}
TERMS: WireObject = {
    "amount": "125",
    "currency": "usd",
    "recipient": "test-recipient",
    "methodDetails": DETAILS,
}
PAYLOAD: WireObject = {
    "encryptedPayload": "opaque-test-only",
    "network": "visa",
    "panLastFour": "1234",
    "panExpirationMonth": "12",
    "panExpirationYear": "2030",
    "billingAddress": {"line1": "", "extension": True},
    "cardholderFullName": "Tester",
    "paymentAccountReference": "reference",
    "extension": {"kept": True},
}


def platform() -> Platform:
    result = Platform()
    config: WireObject = {
        **deepcopy(CONFIG),
        "supportedMethods": [
            {
                "id": "card",
                "supportedCurrencies": ["USD"],
                "supportedIntents": ["charge"],
                "methodDetails": {**deepcopy(DETAILS), "recipient": "test-recipient"},
            }
        ],
    }
    result.config = config
    return result


@pytest.mark.parametrize("billing", [None, False, True])
async def test_offer_authority(billing: bool | None) -> None:
    transport = platform()
    async with await Seller.create(options(transport), method="card") as seller:
        offer: WireObject = {
            "amount": "1.25",
            "currency": "eur",
            "recipient": "other",
            "methodDetails": {},
            "externalId": "",
            "description": "Report",
        }
        if billing is not None:
            offer["billingRequired"] = billing
        before = deepcopy(offer)
        result = seller.card_request(offer)
        details = deepcopy(DETAILS)
        if billing is not None:
            details["billingRequired"] = billing
        assert result == {
            **TERMS,
            "methodDetails": details,
            "externalId": "",
            "description": "Report",
        }
        assert offer == before
        result["methodDetails"] = {}
        assert seller.card_request({"amount": "1.25"}) == TERMS
        with pytest.raises(MppSellerConfigurationError, match="card_request"):
            seller.charge_request({"amount": "1.25"})


async def test_wrong_seller() -> None:
    async with await Seller.create(options(Platform())) as seller:
        with pytest.raises(MppSellerConfigurationError):
            seller.card_request({"amount": "1"})


@pytest.mark.parametrize(
    "field,value",
    [
        ("supportedCurrencies", None),
        ("supportedCurrencies", []),
        ("supportedIntents", None),
        ("supportedIntents", []),
        ("methodDetails", None),
        ("id", "other"),
    ],
)
async def test_missing_capability(field: str, value: object) -> None:
    transport = platform()
    config = json.loads(json.dumps(transport.config))
    config["supportedMethods"][0][field] = value
    transport.config = config
    with pytest.raises(MppSellerConfigurationError):
        await Seller.create(options(transport), method="card")
    assert transport.closed


@pytest.mark.parametrize(
    "path,value",
    [
        (("amount",), "49"),
        (("amount",), "1.25"),
        (("currency",), "eur"),
        (("recipient",), "x" * 256),
        (("recipient",), ""),
        (("externalId",), "x" * 256),
        (("externalId",), False),
        (("description",), 3),
        (("methodDetails", "merchantName"), "x" * 256),
        (("methodDetails", "acceptedNetworks"), None),
        (("methodDetails", "acceptedNetworks"), []),
        (("methodDetails", "acceptedNetworks"), ["other"]),
        (("methodDetails", "billingRequired"), 1),
        *[
            (("methodDetails", "encryptionJwk", field), value)
            for field, value in [
                ("kty", "EC"),
                ("alg", "RSA"),
                ("use", "sig"),
                ("kid", ""),
                ("n", "bad!"),
                ("e", "bad!"),
            ]
        ],
    ],
)
def test_request_rejection(path: tuple[str, ...], value: object) -> None:
    terms = json.loads(json.dumps(TERMS))
    target = terms
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = value
    with pytest.raises(MppCodecError):
        validate_request("card", "charge", terms)


@pytest.mark.parametrize(
    "field,value",
    [
        ("encryptedPayload", ""),
        ("encryptedPayload", "x" * 16385),
        ("network", "other"),
        ("panLastFour", "123"),
        ("panExpirationMonth", "13"),
        ("panExpirationYear", "30"),
        ("cardholderFullName", False),
        ("paymentAccountReference", 3),
        ("billingAddress", {"city": 3}),
        ("billingAddress", None),
    ],
)
async def test_payload_rejection(field: str, value: object) -> None:
    payload = json.loads(json.dumps(PAYLOAD))
    payload[field] = value
    transport = platform()
    async with await Seller.create(options(transport), method="card") as seller:
        challenge = Challenge.create(
            secret_key="test", realm="seller", method="card", intent="charge", request=TERMS
        )
        with pytest.raises(InvalidChallengeError):
            await seller.validate(Credential(challenge=challenge.to_echo(), payload=payload), TERMS)
    assert len(transport.requests) == 1


def test_payload_optional_fields() -> None:
    payload = {
        key: value
        for key, value in PAYLOAD.items()
        if key not in ("billingAddress", "cardholderFullName", "paymentAccountReference")
    }
    assert validate_payload("card", payload) == payload


@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "validation",
        "pending",
        "method",
        "challenge",
        "missing",
        "signature",
        "expiry",
        "amount",
        "billing",
        "reference",
    ],
)
async def test_route(outcome: str) -> None:
    transport = platform()
    async with await Seller.create(options(transport), method="card") as seller:
        terms = seller.card_request({"amount": "1.25", "description": "Report"})
        app = FastAPI()
        delivered = []

        @app.get("/paid")
        @pay(
            intent=seller,
            method="card",
            request=lambda _: terms,
            description="Report",
            realm="seller",
            secret_key="test-secret",
        )
        async def paid(request: Request, credential: Credential, receipt: Receipt) -> JSONResponse:
            delivered.append(True)
            return JSONResponse(
                {"ok": True}, headers={"Payment-Receipt": receipt.to_payment_receipt()}
            )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://seller"
        ) as client:
            initial = await client.get("/paid")
            challenge = Challenge.from_www_authenticate(initial.headers["www-authenticate"])
            assert challenge.description == "Report"
            if outcome == "expiry":
                challenge = Challenge.create(
                    secret_key="test-secret",
                    realm="seller",
                    method="card",
                    intent="charge",
                    request=json.loads(json.dumps(decode(challenge.request_b64))),
                    description="Report",
                    expires="2020-01-01T00:00:00Z",
                )
            credential = Credential(challenge=challenge.to_echo(), payload=deepcopy(PAYLOAD))
            receipt = {**RECEIPT, "method": "card", "challengeId": challenge.id}
            if outcome == "validation":
                transport.validation = {"success": False, "problem": PROBLEM}
            if outcome == "pending":
                transport.result = {"problem": PROBLEM}
            else:
                if outcome == "method":
                    receipt["method"] = "stripe"
                if outcome == "challenge":
                    receipt["challengeId"] = "other"
                if outcome == "missing":
                    del receipt["challengeId"]
                transport.result = {"receipt": receipt}
            if outcome == "signature":
                credential = replace(
                    credential, challenge=replace(credential.challenge, id="tampered")
                )
            if outcome in ("amount", "billing", "reference"):
                terms = seller.card_request(
                    {
                        "amount": "2" if outcome == "amount" else "1.25",
                        "billingRequired": outcome == "billing",
                        "externalId": "other" if outcome == "reference" else "",
                    }
                )
            response = await client.get(
                "/paid", headers={"Authorization": credential.to_authorization()}
            )
            assert response.status_code == (200 if outcome == "success" else 402)
            assert delivered == ([True] if outcome == "success" else [])
            if outcome == "success":
                assert decode(response.headers["payment-receipt"]) == receipt
                wire = json.loads(transport.requests[1].content)["credential"]
                assert wire["payload"] == PAYLOAD
                assert wire["source"] == ""
            else:
                assert "payment-receipt" not in response.headers
            expected = (
                1
                if outcome in ("signature", "expiry", "amount", "billing", "reference")
                else 2
                if outcome == "validation"
                else 3
            )
            assert len(transport.requests) == expected
