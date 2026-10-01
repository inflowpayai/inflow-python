from __future__ import annotations

import asyncio
import inspect
import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from copy import deepcopy
from types import TracebackType
from typing import Any, Self, cast
from urllib.parse import quote
from weakref import WeakSet

from x402 import x402Client
from x402.schemas import (
    AbortResult,
    NoMatchingRequirementsError,
    PaymentAbortedError,
    PaymentCreatedContext,
    PaymentCreationContext,
    PaymentCreationFailureContext,
    PaymentPayload,
    PaymentPayloadV1,
    PaymentRequired,
    PaymentRequiredV1,
    PaymentRequirements,
    RecoveredPayloadResult,
    ResourceInfo,
    SupportedResponse,
)

from .._runtime import Client
from ..errors import InflowApiError
from ..options import ClientOptions
from ._core import PAYMENT_IDENTIFIER, read_payment_identifier, validate_payment_id
from ._payment import (
    EncodedPayment,
    PreparedPayment,
    X402PaymentError,
    response_object,
    validate_wait,
)

__all__ = ["Buyer", "EncodedPayment", "PreparedPayment", "X402PaymentError"]


def _atomic(value: str) -> int | None:
    matched = re.fullmatch(r"(-?)([0-9]+)(?:\.([0-9]+))?", value.strip())
    if matched is None:
        return None
    fraction = ((matched[3] or "") + "0" * 18)[:18]
    return int(matched[1] + matched[2] + fraction)


class Buyer(x402Client):
    def __init__(
        self,
        client: Client,
        supported: SupportedResponse,
        *,
        prefer: Sequence[str],
        poll_interval: float,
        pending_timeout: float,
    ) -> None:
        super().__init__()
        self._client = client
        self._supported = supported.model_copy(deep=True)
        self._expires = asyncio.get_running_loop().time() + 3600
        self._refresh: asyncio.Task[None] | None = None
        self._prefer = tuple(prefer)
        self._poll_interval = poll_interval
        self._pending_timeout = pending_timeout
        self._payments: WeakSet[PreparedPayment] = WeakSet()
        self._closed = False

    @classmethod
    async def create(
        cls,
        options: ClientOptions,
        *,
        prefer: Sequence[str] = ("balance", "exact"),
        poll_interval: float = 5,
        pending_timeout: float = 900,
    ) -> Self:
        validate_wait(poll_interval, pending_timeout)
        client = Client(options)
        try:
            supported = SupportedResponse.model_validate(
                await client.request("GET", "/v1/transactions/x402-supported")
            )
            return cls(
                client,
                supported,
                prefer=prefer,
                poll_interval=poll_interval,
                pending_timeout=pending_timeout,
            )
        except BaseException:
            await client.aclose()
            raise

    async def __aenter__(self) -> Self:
        self._check_open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("x402 Buyer is closed")

    async def aclose(self) -> None:
        self._closed = True
        try:
            if self._refresh is not None:
                self._refresh.cancel()
                await asyncio.gather(self._refresh, return_exceptions=True)
            await asyncio.gather(*(payment._close() for payment in tuple(self._payments)))
        finally:
            self._payments.clear()
            await self._client.aclose()

    async def get_supported(self, *, refresh: bool = False) -> SupportedResponse:
        self._check_open()
        if refresh or asyncio.get_running_loop().time() >= self._expires:
            if self._refresh is None:
                self._refresh = asyncio.create_task(self._refresh_supported())
                self._refresh.add_done_callback(self._finish_refresh)
            await asyncio.shield(self._refresh)
        return self._supported.model_copy(deep=True)

    async def _refresh_supported(self) -> None:
        fresh = SupportedResponse.model_validate(
            await self._client.request("GET", "/v1/transactions/x402-supported")
        )
        self._supported = fresh
        self._expires = asyncio.get_running_loop().time() + 3600

    def _finish_refresh(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()
        self._refresh = None

    def supports(self, requirement: PaymentRequirements) -> bool:
        return requirement.extra.get("assetTransferMethod") != "permit2" and any(
            kind.x402_version == 2
            and kind.scheme == requirement.scheme
            and kind.network == requirement.network
            for kind in self._supported.kinds
        )

    async def select_inflow_requirement(
        self, required: PaymentRequired
    ) -> PaymentRequirements | None:
        self._check_open()
        if required.x402_version != 2:
            raise ValueError("InFlow managed payments require x402 version 2")
        candidates = [
            r.model_copy(deep=True)
            for r in required.accepts
            if r.scheme in self._prefer and self.supports(r)
        ]
        if not candidates:
            return None
        # Match Node: upstream spend controls govern external wallets. Managed payments
        # use registered policies here and InFlow's account approval/policies on the server.
        for policy in tuple(self._policies):
            candidates = [
                PaymentRequirements.model_validate(r) for r in policy(2, [r for r in candidates])
            ]
            if not candidates:
                raise NoMatchingRequirementsError(
                    "All managed requirements filtered out by policies"
                )
        for scheme in self._prefer:
            matches = [r for r in candidates if r.scheme == scheme and self.supports(r)]
            if matches:
                if scheme == "balance" and len(matches) > 1:
                    affordable = await self._affordable(matches)
                    if affordable is not None:
                        return affordable
                return matches[0]
        raise NoMatchingRequirementsError(
            "Payment policies returned no supported InFlow requirement"
        )

    async def _affordable(
        self, requirements: list[PaymentRequirements]
    ) -> PaymentRequirements | None:
        try:
            response = response_object(await self._client.request("GET", "/v1/balances"))
        except (InflowApiError, X402PaymentError):
            return None
        balances = response.get("balances")
        if not isinstance(balances, list):
            return None
        available: dict[str, int] = {}
        for balance in balances:
            if (
                isinstance(balance, dict)
                and isinstance(balance.get("currency"), str)
                and isinstance(balance.get("available"), str)
            ):
                amount = _atomic(balance["available"])
                if amount is not None:
                    available[balance["currency"]] = amount
        for requirement in requirements:
            currency = requirement.extra.get("assetName")
            if isinstance(currency, str) and currency in available:
                try:
                    amount = int(requirement.amount)
                except ValueError:
                    continue
                if available[currency] >= amount:
                    return requirement
        return None

    async def _hooks(self, phase: str, context: PaymentCreationContext) -> AsyncIterator[object]:
        # Upstream types use Any for extension hooks. Do not run external scheme hooks
        # on a payment signed by InFlow rather than that external scheme.
        hooks: list[Callable[..., Any]] = list(getattr(self, f"_{phase}_hooks"))
        # Managed contexts are constructed exclusively from V2 PaymentRequired.
        declarations = cast(PaymentRequired, context.payment_required).extensions or {}
        for extension in self.get_extensions():
            hook = getattr(
                getattr(extension, "hooks", None), "on_" + phase.removeprefix("on_"), None
            )
            if extension.key in declarations and hook is not None:
                declaration = declarations[extension.key]
                hooks.append(
                    lambda ctx, hook=hook, declaration=declaration: hook(deepcopy(declaration), ctx)
                )
        for hook in hooks:
            # Preserve the original exception; application exceptions need not support copying.
            memo = (
                {id(context.error): context.error}
                if isinstance(context, PaymentCreationFailureContext)
                else {}
            )
            result = hook(deepcopy(context, memo))
            yield await result if inspect.isawaitable(result) else result

    async def _before(self, context: PaymentCreationContext) -> None:
        async for result in self._hooks("before_payment_creation", context):
            if isinstance(result, AbortResult):
                raise PaymentAbortedError(result.reason)

    async def _after(self, context: PaymentCreationContext, payload: PaymentPayload) -> None:
        created = PaymentCreatedContext(
            payment_required=context.payment_required,
            selected_requirements=context.selected_requirements,
            payment_payload=payload,
        )
        async for _ in self._hooks("after_payment_creation", created):
            pass

    async def prepare(
        self,
        requirement: PaymentRequirements,
        resource: ResourceInfo,
        *,
        payment_id: str | None = None,
        extensions: Mapping[str, object] | None = None,
        transaction_request_extensions: Mapping[str, object] | None = None,
    ) -> PreparedPayment:
        self._check_open()
        if not self.supports(requirement):
            raise X402PaymentError(
                "unsupported-capability", "InFlow cannot sign this payment requirement"
            )
        if payment_id is not None and not validate_payment_id(payment_id):
            raise ValueError("Invalid payment identifier")
        selected = requirement.model_copy(deep=True)
        required = PaymentRequired(
            accepts=[selected],
            resource=resource.model_copy(deep=True),
            extensions=deepcopy(dict(extensions or {})),
        )
        context = PaymentCreationContext(payment_required=required, selected_requirements=selected)
        await self._before(context)
        return await self._prepare(context, payment_id, transaction_request_extensions)

    async def _prepare(
        self,
        context: PaymentCreationContext,
        payment_id: str | None,
        extra: Mapping[str, object] | None,
    ) -> PreparedPayment:
        body = deepcopy(dict(extra or {}))
        required = cast(PaymentRequired, context.payment_required)
        body.update(
            accept=context.selected_requirements.model_dump(by_alias=True, exclude_none=True),
            resource=required.resource.model_dump(by_alias=True, exclude_none=True)
            if required.resource
            else None,
            x402Version=2,
        )
        if payment_id is not None:
            body["remotePaymentId"] = payment_id
        # Creation is not retried: without a remotePaymentId it creates a second approval.
        created = response_object(
            await self._client.request("POST", "/v1/transactions/x402", body=body)
        )

        async def after(payload: PaymentPayload) -> None:
            await self._after(context, payload)

        prepared = PreparedPayment(
            self._client,
            created,
            poll_interval=self._poll_interval,
            timeout=self._pending_timeout,
            after=after,
        )
        self._payments.add(prepared)
        return prepared

    async def get_x402_payload(self, transaction_id: str) -> dict[str, object]:
        self._check_open()
        return response_object(
            await self._client.request(
                "GET", f"/v1/transactions/{quote(transaction_id, safe='')}/x402"
            )
        )

    async def cancel_approval(self, approval_id: str) -> None:
        await self._client.cancel_approval(approval_id)

    async def create_payment_payload(
        self,
        payment_required: PaymentRequired | PaymentRequiredV1,
        resource: ResourceInfo | None = None,
        extensions: dict[str, Any] | None = None,
    ) -> PaymentPayload | PaymentPayloadV1:
        self._check_open()
        if not isinstance(payment_required, PaymentRequired) or payment_required.x402_version != 2:
            raise ValueError("InFlow Buyer requires x402 version 2")
        required = payment_required.model_copy(deep=True)
        if resource is not None:
            required.resource = resource.model_copy(deep=True)
        if extensions is not None:
            required.extensions = deepcopy(extensions)
        selected = await self.select_inflow_requirement(required)
        if selected is None:
            # Preserve upstream wallet execution; its Solana network calls are synchronous.
            payload = await super().create_payment_payload(required)
            if not isinstance(payload, PaymentPayload) or payload.x402_version != 2:
                raise X402PaymentError("invalid-response", "Expected x402 version 2")
            declaration = read_payment_identifier(
                (required.extensions or {}).get(PAYMENT_IDENTIFIER)
            )
            if declaration is not None and declaration["info"]["required"] is True:
                entry = read_payment_identifier((payload.extensions or {}).get(PAYMENT_IDENTIFIER))
                if entry is None or not validate_payment_id(entry["info"].get("id")):
                    raise X402PaymentError(
                        "invalid-input", "Required payment identifier was not produced"
                    )
            return payload
        context = PaymentCreationContext(payment_required=required, selected_requirements=selected)
        await self._before(context)
        prepared: PreparedPayment | None = None
        try:
            prepared = await self._prepare(context, None, None)
            return (await prepared.await_payload()).payment_payload
        except BaseException as error:
            if prepared is not None:
                # An after-hook failure does not undo a payment already signed by InFlow.
                if not prepared._received:
                    await prepared.cancel()
                self._payments.discard(prepared)
            if isinstance(error, Exception):
                failed = PaymentCreationFailureContext(
                    payment_required=required, selected_requirements=selected, error=error
                )
                async for result in self._hooks("on_payment_creation_failure", failed):
                    if isinstance(result, RecoveredPayloadResult):
                        return result.payload
            raise
