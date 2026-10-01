from __future__ import annotations

import asyncio
import hashlib
import json
import re
from types import TracebackType
from typing import Self

from x402.schemas import (
    PaymentPayload,
    PaymentPayloadV1,
    PaymentRequirements,
    PaymentRequirementsV1,
    SettleResponse,
    SupportedResponse,
    VerifyResponse,
)

from .._runtime import Client
from ..errors import InflowApiError
from ..options import ClientOptions
from ._core import (
    PAYMENT_IDENTIFIER,
    declare_payment_identifier,
    read_payment_identifier,
    validate_payment_id,
)


def _request(
    payload: PaymentPayload | PaymentPayloadV1,
    requirements: PaymentRequirements | PaymentRequirementsV1,
) -> dict[str, object]:
    if not isinstance(payload, PaymentPayload) or payload.x402_version != 2:
        raise ValueError("InFlow facilitator requires x402 version 2")
    if not isinstance(requirements, PaymentRequirements):
        raise ValueError("InFlow facilitator requires version 2 requirements")
    payment = payload.model_copy(deep=True)
    entry = read_payment_identifier((payment.extensions or {}).get(PAYMENT_IDENTIFIER))
    if entry is None or not validate_payment_id(entry["info"].get("id")):
        material = "payload:" + json.dumps(
            payment.payload, separators=(",", ":"), ensure_ascii=False
        )
        for key in ("transactionId", "transaction", "signature"):
            value = payment.payload.get(key)
            if isinstance(value, str) and value:
                material = key + ":" + value
                break
        entry = declare_payment_identifier()
        entry["info"]["id"] = "pay_" + hashlib.sha256(material.encode()).hexdigest()[:32]
        payment.extensions = {**(payment.extensions or {}), PAYMENT_IDENTIFIER: entry}
    return {
        "x402Version": 2,
        "paymentPayload": payment.model_dump(by_alias=True, exclude_none=True),
        "paymentRequirements": requirements.model_dump(by_alias=True, exclude_none=True),
    }


class Facilitator:
    def __init__(self, client: Client, supported: SupportedResponse) -> None:
        self._client = client
        self._supported = supported.model_copy(deep=True)

    @classmethod
    async def create(cls, options: ClientOptions, *, anonymous: bool = False) -> Self:
        if anonymous:
            if options.api_key is not None or options.access_token is not None:
                raise ValueError("Anonymous facilitator must not receive credentials")
        elif options.api_key is None:
            raise ValueError("Facilitator setup requires a Seller API key or anonymous=True")
        client = Client(options)
        try:
            supported = SupportedResponse.model_validate(
                await client.request("GET", "/v1/x402/supported")
            )
            return cls(client, supported)
        except BaseException:
            await client.aclose()
            raise

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def get_supported(self) -> SupportedResponse:
        # The upstream interface is synchronous. Read capabilities loaded during async
        # setup; network refresh requires a new adapter and resource server.
        return self._supported.model_copy(deep=True)

    async def verify(
        self,
        payload: PaymentPayload | PaymentPayloadV1,
        requirements: PaymentRequirements | PaymentRequirementsV1,
    ) -> VerifyResponse:
        try:
            response = await self._client.request(
                "POST", "/v1/x402/verify", body=_request(payload, requirements)
            )
        except InflowApiError as error:
            body = error.body
            if not (
                error.http_status == 412
                and isinstance(body, dict)
                and body.get("isValid") is False
                and body.get("invalidReason") == "permit2_allowance_required"
            ):
                raise
            response = body
        return VerifyResponse.model_validate(response)

    async def settle(
        self,
        payload: PaymentPayload | PaymentPayloadV1,
        requirements: PaymentRequirements | PaymentRequirementsV1,
    ) -> SettleResponse:
        body = _request(payload, requirements)
        attempt = 1
        while True:
            try:
                response = await self._client.request("POST", "/v1/x402/settle", body=body)
                return SettleResponse.model_validate(response)
            except InflowApiError as error:
                if not (
                    error.http_status == 409
                    and isinstance(error.body, dict)
                    and error.body.get("errorReason") == "idempotency_pending"
                    and attempt < 5
                ):
                    raise
                # Only an explicit pending response permits resubmission. Reuse the
                # same request and identifier; a transport failure may follow payment.
                value = error.headers.get("retry-after", "")
                delay = min(float(value), 5) if re.fullmatch(r"[0-9]+", value) else 5
                await asyncio.sleep(delay)
                attempt += 1
