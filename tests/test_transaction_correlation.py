import base64
import json
from copy import deepcopy
from dataclasses import replace
from typing import Literal

import httpx
import pytest
from mpp import Receipt

from inflowpay.mpp import JsonValue, WireObject, decode_receipt, encode
from inflowpay.mpp import get_inflow_transaction_id as mpp_transaction_id
from inflowpay.mpp.seller import Seller
from inflowpay.x402 import INFLOW_TRANSACTION, SettleResponse
from inflowpay.x402 import get_inflow_transaction_id as x402_transaction_id
from mpp_support import broadcast
from test_mpp_seller import (
    RECEIPT,
    TERMS,
    Platform,
    app_for,
    credential_for,
    options,
    wire_credential,
)
from test_x402_upfront import FlowPlatform, merchant, payment_headers

TRANSACTION_ID = "abcdefab-1234-4567-89ab-abcdef123456"
EXTENSION = {
    "info": {"transactionId": TRANSACTION_ID},
    "schema": {
        "type": "object",
        "properties": {"transactionId": {"type": "string", "format": "uuid"}},
        "required": ["transactionId"],
    },
}
INVALID_IDS: list[JsonValue] = [
    None,
    True,
    42,
    [],
    {},
    "",
    "not-a-uuid",
    "x" * 36,
    "x" * 20000,
    TRANSACTION_ID + "\n",
]


@pytest.mark.parametrize("value", [*INVALID_IDS, TRANSACTION_ID, TRANSACTION_ID.upper()])
async def test_mpp_optional_transaction_id(value: JsonValue) -> None:
    wire: WireObject = {**RECEIPT, "inflowTransactionId": value}
    before = deepcopy(wire)
    expected = value if value in (TRANSACTION_ID, TRANSACTION_ID.upper()) else None
    assert mpp_transaction_id(wire) == expected
    assert decode_receipt(encode(wire)).get("inflowTransactionId") == value
    platform = Platform()
    platform.result = {"receipt": wire}
    async with await Seller.create(options(platform)) as seller:
        receipt = await broadcast(seller, wire_credential(), TERMS)
        assert mpp_transaction_id(receipt) == expected
        header = receipt.to_payment_receipt()
        decoded = decode_receipt(header)
        assert mpp_transaction_id(decoded) == expected
        assert ("inflowTransactionId" in decoded) is (expected is not None)
        assert receipt.reference == RECEIPT["reference"]
        assert decoded["custom"] == RECEIPT["custom"]
        assert Receipt.from_payment_receipt(header).reference == receipt.reference
        assert len(platform.requests) == 2
    assert wire == before


def test_mpp_native_receipt_does_not_infer_transaction_id() -> None:
    receipt = Receipt.from_payment_receipt(encode({**RECEIPT, "reference": TRANSACTION_ID}))
    assert mpp_transaction_id(receipt) is None
    assert (
        mpp_transaction_id(replace(receipt, extra={"inflowTransactionId": TRANSACTION_ID})) is None
    )
    assert mpp_transaction_id({**RECEIPT, "reference": TRANSACTION_ID}) is None
    assert mpp_transaction_id({"status": "failed", "inflowTransactionId": TRANSACTION_ID}) is None
    assert mpp_transaction_id({"inflowTransactionId": TRANSACTION_ID}) is None


@pytest.mark.parametrize("present", [True, False])
async def test_mpp_framework_preserves_transaction_id(present: bool) -> None:
    platform = Platform()
    wire = {**RECEIPT, **({"inflowTransactionId": TRANSACTION_ID} if present else {})}
    platform.result = {"receipt": wire}
    async with (
        await Seller.create(options(platform)) as seller,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app_for(seller)), base_url="https://seller.example"
        ) as client,
    ):
        credential = await credential_for(client)
        for _ in range(2):
            response = await client.get("/paid", headers={"Authorization": credential})
            assert response.status_code == 200
            assert response.json() == {"ok": True}
            decoded = decode_receipt(response.headers["payment-receipt"])
            assert decoded == wire
            assert mpp_transaction_id(decoded) == (TRANSACTION_ID if present else None)
        assert [r.url.path for r in platform.requests] == ["/v1/mpp/config"] + [
            "/v1/mpp/validate",
            "/v1/mpp/broadcast",
        ] * 2


@pytest.mark.parametrize(
    "extension",
    [
        None,
        True,
        42,
        [],
        "bad",
        {},
        {"info": None},
        {"info": []},
        {"info": "bad"},
        {"info": {}},
        *({"info": {"transactionId": value}} for value in INVALID_IDS),
        EXTENSION,
        {"info": {"transactionId": TRANSACTION_ID.upper()}},
    ],
)
def test_x402_transaction_id(extension: object) -> None:
    response = SettleResponse(
        success=True,
        transaction="blockchain-reference",
        network="eip155:8453",
        extensions={INFLOW_TRANSACTION: extension, "custom": {"items": [1, None]}},
    )
    before = response.model_copy(deep=True)
    expected = (
        TRANSACTION_ID
        if extension == EXTENSION
        else TRANSACTION_ID.upper()
        if extension == {"info": {"transactionId": TRANSACTION_ID.upper()}}
        else None
    )
    assert x402_transaction_id(response) == expected
    assert response == before
    response.success = False
    assert x402_transaction_id(response) is None


@pytest.mark.parametrize("extensions", [None, {}, {"other": EXTENSION}])
def test_x402_absence_does_not_infer_transaction_id(extensions: dict[str, object] | None) -> None:
    response = SettleResponse(
        success=True, transaction=TRANSACTION_ID, network="inflow:1", extensions=extensions
    )
    assert x402_transaction_id(response) is None


@pytest.mark.parametrize("flow", ["authorization", "upfront"])
@pytest.mark.parametrize("scheme", ["balance", "exact"])
@pytest.mark.parametrize("value", [TRANSACTION_ID, None, 42])
async def test_x402_framework_preserves_transaction_id(
    flow: Literal["authorization", "upfront"], scheme: str, value: JsonValue
) -> None:
    platform = FlowPlatform()
    extensions = {
        "custom": {"items": [1, None]},
        **(
            {INFLOW_TRANSACTION: {**EXTENSION, "info": {"transactionId": value}}}
            if value is not None
            else {}
        ),
    }
    platform.settlement["extensions"] = deepcopy(extensions)
    before = deepcopy(platform.settlement)
    async with merchant(platform, flow=flow, scheme=scheme) as (client, payment):
        for _ in range(2):
            response = await client.get("/paid", headers=payment_headers(payment))
            assert response.status_code == 200
            wire = json.loads(base64.b64decode(response.headers["payment-response"]))
            receipt = SettleResponse.model_validate(wire)
            assert receipt.success and receipt.transaction == "tx"
            assert receipt.extensions == extensions
            assert x402_transaction_id(receipt) == (
                TRANSACTION_ID if value == TRANSACTION_ID else None
            )
        events = (
            ["verify", "handler", "settle"] if flow == "authorization" else ["settle", "handler"]
        )
        assert platform.events == events * 2
        assert len(platform.settlements) == 2
        assert platform.settlements[0] == platform.settlements[1]
        assert INFLOW_TRANSACTION not in platform.settlements[0]["paymentPayload"]["extensions"]
        assert payment.extensions is None
    assert platform.settlement == before


@pytest.mark.parametrize("throws", [False, True])
async def test_x402_upfront_handler_failure_retains_transaction_id(throws: bool) -> None:
    platform = FlowPlatform()
    platform.settlement["extensions"] = {INFLOW_TRANSACTION: deepcopy(EXTENSION)}
    async with merchant(platform, handler_status=500, throws=throws) as (client, payment):
        response = await client.get("/paid", headers=payment_headers(payment))
        assert response.status_code == 500
        wire = json.loads(base64.b64decode(response.headers["payment-response"]))
        assert x402_transaction_id(SettleResponse.model_validate(wire)) == TRANSACTION_ID
        assert wire["transaction"] == "tx"
        assert platform.events == ["settle", "handler"]
