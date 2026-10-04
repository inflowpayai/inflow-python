from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import cast
from urllib.parse import quote

from x402.schemas import PaymentPayload

from .._runtime import Client
from ..errors import InflowApiError


class X402PaymentError(Exception):
    def __init__(self, code: str, message: str, *, status: str | None = None) -> None:
        self.code = code
        self.status = status
        super().__init__(message)


def response_object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise X402PaymentError("invalid-response", "Expected an x402 response object")
    return cast(dict[str, object], value)


def response_string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise X402PaymentError("invalid-response", "Missing x402 response field")
    return value


def validate_wait(poll_interval: float, timeout: float) -> None:
    if any(not math.isfinite(value) or value < 0 for value in (poll_interval, timeout)):
        raise ValueError("Polling settings must be finite and nonnegative")


@dataclass(frozen=True)
class EncodedPayment:
    encoded_payload: str
    payment_payload: PaymentPayload
    transaction_id: str


def _observe_completion(task: asyncio.Task[EncodedPayment]) -> None:
    # Hooks may finish after the last waiter cancels. Keep their error available for
    # a later wait without emitting an unobserved-task exception in the meantime.
    if not task.cancelled():
        task.exception()


class PreparedPayment:
    def __init__(
        self,
        client: Client,
        created: dict[str, object],
        *,
        poll_interval: float,
        timeout: float,
        after: Callable[[PaymentPayload], Awaitable[None]],
    ) -> None:
        self.transaction_id = response_string(created.get("transactionId"))
        self.approval_id = response_string(created.get("approvalId"))
        self._approved = created.get("approvalStatus") == "APPROVED"
        self._client = client
        self._poll_interval = poll_interval
        self._timeout = timeout
        self._after = after
        self._completion: asyncio.Task[EncodedPayment] | None = None
        self._received = False
        self._cancelled = False
        self._cancellation: asyncio.Task[None] | None = None
        self._waiters = 0

    async def status(self) -> str:
        return response_string((await self._read()).get("status"))

    async def _read(self) -> dict[str, object]:
        return response_object(
            await self._client.request(
                "GET", f"/v1/transactions/{quote(self.transaction_id, safe='')}/x402"
            )
        )

    async def cancel(self) -> None:
        self._cancelled = True
        if self._completion is not None and not self._completion.done():
            self._completion.cancel()
        if self._cancellation is None:
            self._cancellation = asyncio.create_task(self._client.cancel_approval(self.approval_id))
        await asyncio.shield(self._cancellation)

    async def await_payload(
        self, *, poll_interval: float | None = None, timeout: float | None = None
    ) -> EncodedPayment:
        if self._cancelled:
            raise X402PaymentError("payment-cancelled", "x402 payment cancelled")
        interval = self._poll_interval if poll_interval is None else poll_interval
        budget = self._timeout if timeout is None else timeout
        validate_wait(interval, budget)
        if self._completion is None:
            self._completion = asyncio.create_task(self._resolve(interval, budget))
            self._completion.add_done_callback(_observe_completion)
        completion = self._completion
        self._waiters += 1
        try:
            # Keep cancellation local to this waiter without shield() logging late
            # hook errors on Python 3.14; _observe_completion already observes them.
            await asyncio.wait((completion,))
            result = completion.result()
            return EncodedPayment(
                result.encoded_payload,
                result.payment_payload.model_copy(deep=True),
                result.transaction_id,
            )
        except asyncio.CancelledError:
            if self._cancelled:
                raise X402PaymentError("payment-cancelled", "x402 payment cancelled") from None
            raise
        finally:
            self._waiters -= 1
            if self._waiters == 0 and not self._received and not completion.done():
                completion.cancel()
                await asyncio.gather(completion, return_exceptions=True)
            if completion.done() and not self._received and self._completion is completion:
                self._completion = None

    async def _resolve(self, interval: float, timeout: float) -> EncodedPayment:
        deadline = asyncio.get_running_loop().time() + timeout
        budget = asyncio.timeout_at(deadline)
        first = self._approved
        try:
            async with budget:
                while True:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise X402PaymentError("payment-timeout", "x402 payment wait timed out")
                    try:
                        response = await self._read()
                    except InflowApiError as error:
                        if error.http_status not in (0, 429) and error.http_status < 500:
                            raise
                        response = {}
                    if asyncio.get_running_loop().time() >= deadline:
                        raise X402PaymentError("payment-timeout", "x402 payment wait timed out")
                    if (
                        response.get("encodedPayload") is not None
                        and response.get("paymentPayload") is not None
                    ):
                        result = EncodedPayment(
                            response_string(response["encodedPayload"]),
                            PaymentPayload.model_validate(response["paymentPayload"]),
                            self.transaction_id,
                        )
                        if result.payment_payload.x402_version != 2:
                            raise X402PaymentError("invalid-response", "Expected x402 version 2")
                        self._received = True
                        break
                    status = response.get("status")
                    if status in ("DECLINED", "EXPIRED", "GENERAL_ERROR", "INSUFFICIENT_FUNDS"):
                        raise X402PaymentError(
                            "payment-failed", "x402 payment failed", status=status
                        )
                    if first:
                        first = False
                    else:
                        await asyncio.sleep(interval)
        except TimeoutError as error:
            if budget.expired():
                raise X402PaymentError("payment-timeout", "x402 payment wait timed out") from error
            raise
        # Cache completion, including hook errors: repeated waits must not repeat hook side effects.
        await self._after(result.payment_payload.model_copy(deep=True))
        if self._cancelled:
            raise X402PaymentError("payment-cancelled", "x402 payment cancelled")
        return result

    async def _close(self) -> None:
        # Closing the client stops local waits; the caller still owns two-phase approvals.
        self._cancelled = True
        if self._completion is not None and not self._completion.done():
            self._completion.cancel()
        if self._completion is not None:
            await asyncio.gather(self._completion, return_exceptions=True)
        if self._cancellation is not None:
            await asyncio.shield(self._cancellation)
