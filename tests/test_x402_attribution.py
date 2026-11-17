import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace
from typing import Any, cast

import httpx
import pytest
from x402.extensions.builder_code import BuilderCodeClientExtension, declare_builder_code_extension
from x402.schemas import PaymentCreatedContext, PaymentPayload, PaymentRequired
from x402.schemas.extensions import ClientExtension

from inflowpay.x402.buyer import X402PaymentError
from test_x402_buyer import REQUIREMENT, RESOURCE, Platform, buyer, required


class AttributionPlatform(Platform):
    def __init__(self) -> None:
        super().__init__()
        self.extensions: dict[str, Any] | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/transactions/x402":
            self.extensions = json.loads(request.content).get("extensions")
        if request.url.path == "/v1/transactions/transaction/x402":
            # This service fixture retains submitted metadata; the actual upstream
            # merger, not this fixture, combines seller and buyer attribution.
            payload = PaymentPayload(
                accepted=REQUIREMENT, payload={"proof": "signed"}, extensions=self.extensions
            )
            self.polls.append(
                {
                    "encodedPayload": "canonical-server-bytes",
                    "paymentPayload": payload.model_dump(by_alias=True, exclude_none=True),
                    "status": "PENDING",
                }
            )
            self.polls = self.polls[-1:]
        return await super().handle_async_request(request)


@pytest.mark.parametrize("seller", [False, True])
@pytest.mark.parametrize("service", [False, True])
@pytest.mark.parametrize("prepared", [False, True])
async def test_native_managed_attribution(seller: bool, service: bool, prepared: bool) -> None:
    platform = AttributionPlatform()
    declarations: dict[str, Any] = {"unrelated": {"info": {"omit": True}}}
    if seller:
        declarations["builder-code"] = declare_builder_code_extension("shop")
        declarations["builder-code"]["info"]["s"] = ["shop_service", "buyer_service"]
    original = deepcopy(declarations)
    after_calls = 0

    async with await buyer(platform) as client:
        if service:
            # Upstream's extension omits optional hook attributes required by its protocol type.
            client.register_extension(
                cast(ClientExtension, BuilderCodeClientExtension("buyer_service"))
            )

        def after(context: PaymentCreatedContext) -> None:
            nonlocal after_calls
            after_calls += 1
            assert isinstance(context.payment_payload, PaymentPayload)
            context.payment_payload.extensions = {"caller_mutation": True}

        client.on_after_payment_creation(after)
        if prepared:
            payment = await client.prepare(
                REQUIREMENT,
                RESOURCE,
                extensions=declarations,
                payment_id="pay_12345678901234567890123456789012",
            )
            results = await asyncio.gather(*(payment.await_payload() for _ in range(4)))
            for result in results:
                assert result.encoded_payload == "canonical-server-bytes"
                assert result.payment_payload.extensions == platform.extensions
            again = await payment.await_payload()
            assert again.encoded_payload == "canonical-server-bytes"
        else:
            result_payload = await client.create_payment_payload(
                required(), extensions=declarations
            )
            assert isinstance(result_payload, PaymentPayload)
            assert result_payload.extensions == platform.extensions

        assert after_calls == 1
        posts = [r for r in platform.requests if r.method == "POST"]
        assert len(posts) == 1
        if prepared:
            assert json.loads(posts[0].content)["remotePaymentId"] == (
                "pay_12345678901234567890123456789012"
            )
        assert len([r for r in platform.requests if r.url.path.endswith("/transaction/x402")]) == 1
        assert declarations == original
        if seller or service:
            assert platform.extensions is not None and set(platform.extensions) == {"builder-code"}
            info = platform.extensions["builder-code"]["info"]
            if seller:
                assert info["a"] == "shop"
                assert "schema" in platform.extensions["builder-code"]
            if service:
                assert info["s"] == ["buyer_service", *(["shop_service"] if seller else [])]
        else:
            assert platform.extensions is None

    # A new client has no registered extension. Retrieval must return the same
    # server-owned bytes and attribution, without client-side reconstruction.
    async with await buyer(platform) as fresh:
        restored = await fresh.get_x402_payload("transaction")
        assert restored["encodedPayload"] == "canonical-server-bytes"
        assert restored["paymentPayload"] == PaymentPayload(
            accepted=REQUIREMENT, payload={"proof": "signed"}, extensions=platform.extensions
        ).model_dump(by_alias=True, exclude_none=True)


@pytest.mark.parametrize(
    "outcome", ["success", "error", "cancel", "swallow-cancel", "self-cancel", "empty", "invalid"]
)
@pytest.mark.parametrize("prepared", [False, True])
async def test_async_enrichment_before_approval(outcome: str, prepared: bool) -> None:
    platform = AttributionPlatform()
    entered = asyncio.Event()
    release = asyncio.Event()
    hook_calls = 0
    swallowed = False

    def before(_: object, __: object) -> None:
        nonlocal hook_calls
        hook_calls += 1

    async def enrich(payload: PaymentPayload, _: PaymentRequired) -> PaymentPayload:
        nonlocal swallowed
        entered.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            if outcome != "swallow-cancel":
                raise
            swallowed = True
        if outcome == "error":
            raise ValueError("attribution failed")
        if outcome == "invalid":
            return payload.model_copy(update={"x402_version": 3})
        if outcome == "empty":
            return payload.model_copy(update={"extensions": None})
        if outcome == "self-cancel":
            current = asyncio.current_task()
            assert current is not None
            current.cancel()
        return BuilderCodeClientExtension("buyer").enrich_payment_payload(payload, _)

    async with await buyer(platform) as client:
        client.register_extension(
            SimpleNamespace(
                key="builder-code",
                hooks=SimpleNamespace(
                    before_payment_creation=None, on_before_payment_creation=before
                ),
                enrich_payment_payload=enrich,
            )
        )
        # Unrelated enrichment must not execute for managed signing.
        client.register_extension(
            SimpleNamespace(key="other", enrich_payment_payload=lambda *_: pytest.fail("unrelated"))
        )
        declarations = (
            {} if outcome == "empty" else {"builder-code": declare_builder_code_extension("shop")}
        )
        task = asyncio.create_task(
            client.prepare(REQUIREMENT, RESOURCE, extensions=declarations)
            if prepared
            else client.create_payment_payload(required(), extensions=declarations)
        )
        await entered.wait()
        assert not any(r.method == "POST" for r in platform.requests)
        if outcome in ("cancel", "swallow-cancel"):
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            release.set()
            if outcome == "self-cancel":
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif outcome in ("error", "invalid"):
                with pytest.raises((ValueError, X402PaymentError)):
                    await task
            else:
                await task
        assert hook_calls == (0 if outcome == "empty" else 1)
        assert swallowed == (outcome == "swallow-cancel")
        assert len([r for r in platform.requests if r.method == "POST"]) == (
            1 if outcome in ("success", "empty") else 0
        )


async def test_builder_hook_without_enrichment() -> None:
    platform = AttributionPlatform()
    async with await buyer(platform) as client:
        client.register_extension(SimpleNamespace(key="builder-code"))
        await client.prepare(REQUIREMENT, RESOURCE)
        assert platform.extensions is None
