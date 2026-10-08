from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self
from urllib.parse import quote
from uuid import UUID

import httpx
from mpp import Challenge, Credential
from mpp.client.transport import PaymentTransport
from mpp.runtime import Method

from .._runtime import Client
from ..options import ClientOptions
from ._pympp import from_pympp_challenge
from ._requests import validate_payload, validate_request
from ._wire import (
    MppCodecError,
    WireObject,
    decode,
    decode_credential,
    encode,
    object_value,
    string,
)

__all__ = [
    "BuyerMethod",
    "MppMalformedCredentialError",
    "MppPaymentExpiredError",
    "MppPaymentFailedError",
    "MppPaymentTimeoutError",
    "payment_transport",
]


class MppMalformedCredentialError(ValueError):
    pass


class MppPaymentFailedError(Exception):
    def __init__(self, problem: WireObject | None, transaction_id: str | None = None) -> None:
        self.problem = deepcopy(problem)
        self.transaction_id = transaction_id
        details = problem or {}
        super().__init__(details.get("detail") or details.get("title") or "MPP payment failed")


class MppPaymentExpiredError(Exception):
    def __init__(self, transaction_id: str | None) -> None:
        self.transaction_id = transaction_id
        super().__init__("MPP transaction expired")


class MppPaymentTimeoutError(TimeoutError):
    def __init__(self, timeout: float, transaction_id: str | None) -> None:
        self.timeout = timeout
        self.transaction_id = transaction_id
        super().__init__(f"MPP transaction not ready within {timeout} seconds")


@dataclass(frozen=True)
class _WireCredential(Credential):
    _wire: WireObject = field(default_factory=dict, repr=False, compare=False)

    def to_authorization(self) -> str:
        # pympp's serializer drops extensions; retain them alongside its public fields.
        standard = decode_credential(super().to_authorization()[8:])
        challenge = {**object_value(self._wire["challenge"]), **object_value(standard["challenge"])}
        wire = {**self._wire, **standard, "challenge": challenge}
        if self.source is None:
            wire.pop("source", None)
        return "Payment " + encode(wire)


def _credential(response: WireObject, challenge: WireObject) -> Credential:
    try:
        wire = decode_credential(string(response.get("credential")))
        if challenge["method"] == "card":
            # CARD binds the issued encrypted credential to the complete requested challenge.
            if encode(wire["challenge"]) != encode(challenge):
                raise MppMalformedCredentialError("CARD credential does not match the challenge")
            validate_payload("card", wire["payload"])
        else:
            # InFlow and Tempo echo the selected challenge, as their Node methods do.
            wire["challenge"] = deepcopy(challenge)
        parsed = Credential.from_authorization("Payment " + encode(wire))
        source = wire.get("source")
        return _WireCredential(
            parsed.challenge, parsed.payload, source if isinstance(source, str) else None, wire
        )
    except ValueError as error:
        raise MppMalformedCredentialError("Missing or malformed MPP credential") from error


def _response(value: object) -> WireObject:
    try:
        return object_value(value)
    except MppCodecError as error:
        raise MppMalformedCredentialError("Expected an MPP response object") from error


def _identifier(value: WireObject, key: str) -> str | None:
    item = value.get(key)
    return item if isinstance(item, str) and item else None


def _card_merchant(value: WireObject | None) -> WireObject:
    merchant = object_value(value)
    name, url, country = (string(merchant.get(key)) for key in ("name", "url", "countryCode"))
    if not name.strip() or len(name.encode("utf-16-le")) // 2 > 200:
        raise ValueError("CARD merchant name must contain 1 to 200 characters")
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL as error:
        raise ValueError("Invalid CARD merchant url") from error
    if (
        len(url.encode("utf-16-le")) // 2 > 2048
        or parsed.scheme not in ("http", "https")
        or not parsed.host
    ):
        raise ValueError("CARD merchant url must be an absolute HTTP or HTTPS URL")
    if not re.fullmatch(r"[A-Za-z]{2}", country):
        raise ValueError("CARD merchant countryCode must contain two letters")
    return {"name": name, "url": url, "countryCode": country}


class BuyerMethod:
    def __init__(
        self,
        options: ClientOptions,
        *,
        method: str = "inflow",
        intent: str = "charge",
        instrument_id: str | None = None,
        subscription_id: str | None = None,
        merchant: WireObject | None = None,
        poll_interval: float = 5,
        pending_timeout: float = 900,
    ) -> None:
        if (method, intent) not in (
            ("inflow", "charge"),
            ("inflow", "subscription"),
            ("tempo", "charge"),
            ("card", "charge"),
        ):
            raise ValueError("Unsupported MPP Buyer method or intent")
        for value in (instrument_id, subscription_id):
            if value is not None:
                UUID(value)
        if instrument_id is not None and (method, intent) not in (
            ("inflow", "charge"),
            ("card", "charge"),
        ):
            raise ValueError("instrument_id applies only to InFlow or CARD charge")
        if (
            method == "card"
            and instrument_id is not None
            and not re.fullmatch(
                r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", instrument_id
            )
        ):
            raise ValueError("CARD instrument_id must be a hyphenated UUID")
        if merchant is not None and method != "card":
            raise ValueError("merchant applies only to CARD charge")
        self._merchant = _card_merchant(merchant) if method == "card" else None
        if subscription_id is not None and intent != "subscription":
            raise ValueError("subscription_id applies only to InFlow subscription")
        if any(not math.isfinite(value) or value < 0 for value in (poll_interval, pending_timeout)):
            raise ValueError("Polling settings must be finite and nonnegative")
        self.name = method
        # pympp's MCP matcher recognizes a mapping; its HTTP runtime also iterates these keys.
        self.intents = {intent: None}
        # pympp has no per-call context. Bind selectors per instance for concurrent callers.
        self._instrument_id = instrument_id
        self._subscription_id = subscription_id
        self._poll_interval = poll_interval
        self._pending_timeout = pending_timeout
        self._client = Client(options)
        self._active: set[asyncio.Task[Credential]] = set()
        self._closed = False

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
        self._closed = True
        try:
            await self.cleanup()
        finally:
            await self._client.aclose()

    async def cleanup(self) -> None:
        tasks = tuple(self._active)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def get_payment_status(
        self, transaction_id: str, *, retries: int = 0
    ) -> dict[str, object]:
        if self._closed:
            raise RuntimeError("MPP Buyer method is closed")
        return await self._client.get_payment_status(transaction_id, retries=retries)

    async def cancel_approval(self, approval_id: str) -> None:
        await self._client.cancel_approval(approval_id)

    async def create_credential(self, challenge: Challenge) -> Credential:
        if self._closed:
            raise RuntimeError("MPP Buyer method is closed")
        wire = from_pympp_challenge(challenge)
        if challenge.method != self.name or challenge.intent not in self.intents:
            raise ValueError("Challenge does not match this MPP Buyer method")
        validate_request(challenge.method, challenge.intent, decode(challenge.request_b64))
        task = asyncio.create_task(self._create(wire))
        self._active.add(task)
        try:
            return await task
        finally:
            self._active.discard(task)

    async def _create(self, challenge: WireObject) -> Credential:
        if self._subscription_id is not None:
            response = _response(
                await self._client.request(
                    "POST",
                    f"/v1/subscriptions/{quote(self._subscription_id, safe='')}/authorize",
                    body={"challenge": challenge},
                )
            )
            if "problem" in response:
                raise MppPaymentFailedError(object_value(response["problem"]))
            return _credential(response, challenge)
        options: WireObject = (
            {} if self._instrument_id is None else {"instrumentId": self._instrument_id}
        )
        if self._merchant is not None:
            options["merchant"] = deepcopy(self._merchant)
        response = _response(
            await self._client.request(
                "POST",
                "/v1/transactions/mpp",
                body={"challenge": challenge, "options": options},
            )
        )
        approval_id = _identifier(response, "approvalId")
        try:
            return await self._resolve(response, challenge)
        except BaseException:
            if approval_id is not None:
                await self._client.cancel_approval(approval_id)
            raise

    async def _resolve(self, response: WireObject, challenge: WireObject) -> Credential:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._pending_timeout
        transaction_id = _identifier(response, "transactionId")
        budget = asyncio.timeout_at(deadline)
        try:
            async with budget:
                while True:
                    state = response.get("state")
                    if state == "ready":
                        return _credential(response, challenge)
                    if state == "failed":
                        problem = response.get("problem")
                        raise MppPaymentFailedError(
                            None if problem is None else object_value(problem),
                            _identifier(response, "transactionId"),
                        )
                    if state == "expired":
                        raise MppPaymentExpiredError(_identifier(response, "transactionId"))
                    transaction_id = _identifier(response, "transactionId")
                    if state != "pending" or transaction_id is None:
                        raise MppMalformedCredentialError(
                            "Expected pending state with transactionId"
                        )
                    advised = response.get("retryAfterSeconds", self._poll_interval)
                    if (
                        isinstance(advised, bool)
                        or not isinstance(advised, (int, float))
                        or not math.isfinite(advised)
                    ):
                        raise MppMalformedCredentialError("Invalid retryAfterSeconds")
                    wait_until = min(deadline, loop.time() + max(0, advised))
                    while True:
                        await asyncio.sleep(max(0, wait_until - loop.time()))
                        if loop.time() >= wait_until:
                            break
                    if loop.time() >= deadline:
                        raise MppPaymentTimeoutError(self._pending_timeout, transaction_id)
                    response = _response(
                        await self._client.request(
                            "GET",
                            f"/v1/transactions/{quote(transaction_id, safe='')}/mpp",
                        )
                    )
                    if loop.time() >= deadline:
                        raise MppPaymentTimeoutError(self._pending_timeout, transaction_id)
        except TimeoutError as error:
            if budget.expired():
                raise MppPaymentTimeoutError(self._pending_timeout, transaction_id) from error
            raise


def payment_transport(
    methods: Sequence[Method],
    *,
    inner: httpx.AsyncBaseTransport | None = None,
) -> PaymentTransport:
    # pympp creates another credential on each retry. Limit automatic payment to one attempt.
    return PaymentTransport(methods=methods, inner=inner, max_payment_retries=1)
