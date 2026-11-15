import base64
import json

import httpx
import pytest

from inflowpay import InflowApiError
from inflowpay.x402.facilitator import Facilitator
from test_x402_seller import PAYLOAD, REQUIREMENTS, Platform


@pytest.mark.parametrize("operation", ["verify", "settle"])
@pytest.mark.parametrize(
    "header", [None, "invalid", "W10=", "bnVsbA==", "e30=", "eyJ0ZXN0Ijp7Im9rIjp0cnVlfX0="]
)
async def test_extension_metadata_is_for_seller_not_buyer(
    operation: str, header: str | None
) -> None:
    platform = Platform()
    body = platform.verification if operation == "verify" else platform.settlement
    response = httpx.Response(
        200, json=body, headers={} if header is None else {"EXTENSION-RESPONSES": header}
    )
    if operation == "verify":
        platform.verification = response
    else:
        platform.settlement = response
    async with await Facilitator.create(platform.options()) as facilitator:
        result = await getattr(facilitator, operation)(PAYLOAD, REQUIREMENTS)
    expected = (
        {}
        if header == "e30="
        else {"test": {"ok": True}}
        if header == "eyJ0ZXN0Ijp7Im9rIjp0cnVlfX0="
        else None
    )
    assert result.extension_responses == expected
    encoded = result.model_dump(by_alias=True, exclude_none=True)
    assert "extensionResponses" not in encoded
    assert "extension_responses" not in encoded
    assert encoded == body


@pytest.mark.parametrize("operation", ["verify", "settle"])
async def test_metadata_does_not_turn_failure_into_success(operation: str) -> None:
    platform = Platform()
    response = httpx.Response(
        403,
        json={"code": "REJECTED"},
        headers={
            "EXTENSION-RESPONSES": base64.b64encode(
                json.dumps({"test": {"ok": True}}).encode()
            ).decode()
        },
    )
    if operation == "verify":
        platform.verification = response
    else:
        platform.settlement = response
    async with await Facilitator.create(platform.options()) as facilitator:
        with pytest.raises(InflowApiError) as failure:
            await getattr(facilitator, operation)(PAYLOAD, REQUIREMENTS)
    assert failure.value.http_status == 403


@pytest.mark.parametrize("header", ["invalid", "eyJ0ZXN0Ijp7Im9rIjp0cnVlfX0="])
async def test_allowance_required_keeps_seller_metadata(header: str) -> None:
    platform = Platform()
    body = {"isValid": False, "invalidReason": "permit2_allowance_required"}
    platform.verification = httpx.Response(412, json=body, headers={"EXTENSION-RESPONSES": header})
    async with await Facilitator.create(platform.options()) as facilitator:
        result = await facilitator.verify(PAYLOAD, REQUIREMENTS)
    assert not result.is_valid
    assert result.invalid_reason == "permit2_allowance_required"
    assert result.extension_responses == (None if header == "invalid" else {"test": {"ok": True}})
    assert result.model_dump(by_alias=True, exclude_none=True) == body
