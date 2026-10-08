import asyncio
import json
from copy import deepcopy

import httpx
import pytest

from inflowpay import ClientOptions, InflowApiError
from inflowpay.mpp import (
    WireObject,
    decode_credential,
    encode,
    render_challenge_header,
    to_pympp_challenge,
)
from inflowpay.mpp._wire import JsonValue
from inflowpay.mpp.buyer import (
    BuyerMethod,
    MppMalformedCredentialError,
    MppPaymentExpiredError,
    MppPaymentFailedError,
    payment_transport,
)
from test_card_seller import PAYLOAD, TERMS
from test_mpp_buyer import ID, PENDING, Exchange, server

MERCHANT: WireObject = {"name": "Test Seller", "url": "https://seller.example", "countryCode": "US"}
CHALLENGE: WireObject = {
    "id": "card-test",
    "realm": "seller.example",
    "method": "card",
    "intent": "charge",
    "request": encode(TERMS),
    "description": "Test purchase",
    "expires": "2099-01-01T00:00:00Z",
    "opaque": encode({"item": "report"}),
    "digest": "sha-256=test-only",
}
CREDENTIAL: WireObject = {"challenge": CHALLENGE, "payload": PAYLOAD, "source": "did:example:buyer"}


def ready(credential: WireObject = CREDENTIAL) -> WireObject:
    return {"state": "ready", "credential": encode(credential)}


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", " "),
        ("name", "x" * 201),
        ("name", None),
        ("url", "/relative"),
        ("url", "ftp://seller.example"),
        ("url", "https://"),
        ("url", "https://seller.example/" + "x" * 2048),
        ("url", "https://host:bad"),
        ("countryCode", "USA"),
        ("countryCode", "12"),
    ],
)
def test_invalid_merchant(field: str, value: JsonValue) -> None:
    with pytest.raises(ValueError):
        BuyerMethod(ClientOptions(), method="card", merchant={**MERCHANT, field: value})


def test_required_and_misplaced_merchant() -> None:
    with pytest.raises(ValueError):
        BuyerMethod(ClientOptions(), method="card")
    with pytest.raises(ValueError, match="merchant applies"):
        BuyerMethod(ClientOptions(), merchant=MERCHANT)
    with pytest.raises(ValueError, match="hyphenated UUID"):
        BuyerMethod(
            ClientOptions(), method="card", merchant=MERCHANT, instrument_id=ID.replace("-", "")
        )


@pytest.mark.parametrize("selected", [False, True])
async def test_options_snapshot_and_credential(selected: bool) -> None:
    merchant = deepcopy(MERCHANT)
    calls: list[WireObject] = []

    def handle(request: httpx.Request) -> httpx.Response:
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=ready())

    async with BuyerMethod(
        ClientOptions(transport=httpx.MockTransport(handle)),
        method="card",
        merchant=merchant,
        instrument_id=ID if selected else None,
    ) as buyer:
        merchant["name"] = "Changed by caller"
        credential = await buyer.create_credential(to_pympp_challenge(CHALLENGE))
    assert calls == [
        {
            "challenge": CHALLENGE,
            "options": {"merchant": MERCHANT, **({"instrumentId": ID} if selected else {})},
        }
    ]
    assert decode_credential(credential.to_authorization()[8:]) == CREDENTIAL


@pytest.mark.parametrize(
    "field,value",
    [
        ("id", "other"),
        ("realm", "other"),
        ("method", "inflow"),
        ("intent", "subscription"),
        ("description", "Other purchase"),
        ("opaque", encode({"item": "other"})),
        ("expires", "2098-01-01T00:00:00Z"),
        ("digest", "sha-256=other"),
        ("request", encode({**TERMS, "amount": "200"})),
    ],
)
async def test_rejects_changed_challenge(field: str, value: JsonValue) -> None:
    response = ready({**CREDENTIAL, "challenge": {**CHALLENGE, field: value}})
    async with BuyerMethod(
        ClientOptions(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=response))),
        method="card",
        merchant=MERCHANT,
    ) as buyer:
        with pytest.raises(MppMalformedCredentialError):
            await buyer.create_credential(to_pympp_challenge(CHALLENGE))


@pytest.mark.parametrize(
    "response",
    [
        {"state": "ready"},
        ready({**CREDENTIAL, "payload": {**PAYLOAD, "encryptedPayload": ""}}),
        ready({**CREDENTIAL, "payload": {**PAYLOAD, "network": "mastercard"}}),
        ready(
            {
                **CREDENTIAL,
                "challenge": {
                    key: value for key, value in CHALLENGE.items() if key != "description"
                },
            }
        ),
    ],
)
async def test_invalid_ready_cancels_approval(response: WireObject) -> None:
    paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/cancel"):
            return httpx.Response(503)
        return httpx.Response(200, json={**response, "approvalId": "approval"})

    async with BuyerMethod(
        ClientOptions(transport=httpx.MockTransport(handle)),
        method="card",
        merchant=MERCHANT,
    ) as buyer:
        with pytest.raises(MppMalformedCredentialError):
            await buyer.create_credential(to_pympp_challenge(CHALLENGE))
    assert paths == ["/v1/transactions/mpp", "/v1/approvals/approval/cancel"]


@pytest.mark.parametrize("auth", ["api-key", "bearer"])
@pytest.mark.parametrize(
    "outcome", ["ready", "rejected", "failed", "expired", "mismatch", "unsupported"]
)
async def test_real_http_transport(auth: str, outcome: str) -> None:
    async def token() -> str:
        return "test-token"

    advertised = deepcopy(CHALLENGE)
    if outcome == "unsupported":
        advertised["request"] = encode({**TERMS, "currency": "eur"})
    credential = deepcopy(CREDENTIAL)
    if outcome == "mismatch":
        credential["challenge"] = {**CHALLENGE, "description": "Changed"}
    headers = (
        {"x-api-key": "test-key", "authorization": ""}
        if auth == "api-key"
        else {"authorization": "Bearer test-token", "x-api-key": ""}
    )
    final = (
        {"state": outcome, "transactionId": "transaction"}
        if outcome in ("failed", "expired")
        else ready(credential)
    )
    platform: list[Exchange] = (
        []
        if outcome == "unsupported"
        else [
            {
                "request": {
                    "method": "POST",
                    "path": "/v1/transactions/mpp",
                    "headers": headers,
                    "json": {"challenge": CHALLENGE, "options": {"merchant": MERCHANT}},
                },
                "response": {"status": 200, "json": PENDING},
            },
            {
                "request": {
                    "method": "GET",
                    "path": "/v1/transactions/transaction/mpp",
                    "headers": headers,
                },
                "response": {"status": 200, "json": final},
            },
        ]
    )
    if outcome in ("failed", "expired", "mismatch"):
        platform.append(
            {
                "request": {
                    "method": "POST",
                    "path": "/v1/approvals/approval/cancel",
                    "headers": headers,
                },
                "response": {"status": 204},
            }
        )
    seller: list[Exchange] = [
        {
            "request": {
                "method": "GET",
                "path": "/paid",
                "headers": {"authorization": "", "x-api-key": ""},
            },
            "response": {
                "status": 402,
                "headers": {"www-authenticate": render_challenge_header(advertised)},
            },
        },
    ]
    if outcome in ("ready", "rejected"):
        seller.append(
            {
                "request": {
                    "method": "GET",
                    "path": "/paid",
                    "headers": {"authorization": "Payment " + encode(CREDENTIAL), "x-api-key": ""},
                },
                "response": {
                    "status": 200 if outcome == "ready" else 402,
                    "headers": {"www-authenticate": render_challenge_header(advertised)},
                },
            }
        )
    async with (
        server(platform) as base,
        server(seller) as origin,
        BuyerMethod(
            ClientOptions(
                base_url=base,
                api_key="test-key" if auth == "api-key" else None,
                access_token=token if auth == "bearer" else None,
            ),
            method="card",
            merchant=MERCHANT,
        ) as buyer,
        httpx.AsyncClient(transport=payment_transport([buyer])) as http,
    ):
        if outcome in ("ready", "rejected"):
            assert (await http.get(origin + "/paid")).status_code == (
                200 if outcome == "ready" else 402
            )
        else:
            expected = {
                "failed": MppPaymentFailedError,
                "expired": MppPaymentExpiredError,
                "mismatch": MppMalformedCredentialError,
                "unsupported": ValueError,
            }[outcome]
            with pytest.raises(expected):
                await http.get(origin + "/paid")


async def test_cancel_and_unknown_creation_do_not_repay() -> None:
    entered = asyncio.Event()
    paths: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/cancel"):
            return httpx.Response(204)
        if request.method == "POST":
            return httpx.Response(200, json=PENDING)
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("cancelled poll resumed")

    async with BuyerMethod(
        ClientOptions(transport=httpx.MockTransport(handle)),
        method="card",
        merchant=MERCHANT,
    ) as buyer:
        task = asyncio.create_task(buyer.create_credential(to_pympp_challenge(CHALLENGE)))
        await asyncio.wait_for(entered.wait(), 2)
        await buyer.cleanup()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert paths == [
        "/v1/transactions/mpp",
        "/v1/transactions/transaction/mpp",
        "/v1/approvals/approval/cancel",
    ]

    attempts = 0

    def uncertain(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ReadError("uncertain", request=request)

    async with BuyerMethod(
        ClientOptions(transport=httpx.MockTransport(uncertain)),
        method="card",
        merchant=MERCHANT,
    ) as buyer:
        with pytest.raises(InflowApiError):
            await buyer.create_credential(to_pympp_challenge(CHALLENGE))
    assert attempts == 1
