import asyncio
import base64
import json
from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from x402.schemas import (
    AbortResult,
    NoMatchingRequirementsError,
    PaymentAbortedError,
    PaymentCreatedContext,
    PaymentCreationContext,
    PaymentCreationFailureContext,
    PaymentPayload,
    PaymentRequired,
    PaymentRequirements,
    RecoveredPayloadResult,
    ResourceInfo,
)

from inflowpay import ClientOptions, InflowApiError
from inflowpay.x402.buyer import Buyer, X402PaymentError
from test_mpp_buyer import Exchange, server

# JSON fixtures retain the public contract's wire field names.
CASES: list[dict[str, Any]] = json.loads(
    Path(__file__).with_name("fixtures").joinpath("x402-buyer.json").read_text()
)["cases"]
REQUIREMENT = PaymentRequirements.model_validate(CASES[0]["input"]["requirement"])
RESOURCE = ResourceInfo(url="https://seller.example/data", mime_type="application/json")
SUPPORTED = {"kinds": [{"scheme": "balance", "network": "inflow:1", "x402Version": 2}]}
CREATED = {"transactionId": "transaction", "approvalId": "approval", "approvalStatus": "PENDING"}


def ready() -> dict[str, object]:
    payload = PaymentPayload(accepted=REQUIREMENT, payload={"transactionId": "transaction"})
    data = payload.model_dump(by_alias=True, exclude_none=True)
    return {
        "status": "PENDING",
        "encodedPayload": base64.b64encode(json.dumps(data).encode()).decode(),
        "paymentPayload": data,
    }


def required(*requirements: PaymentRequirements) -> PaymentRequired:
    return PaymentRequired(accepts=list(requirements or (REQUIREMENT,)), resource=RESOURCE)


class Platform(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.polls: list[object] = [ready()]
        self.closed = False
        self.created = deepcopy(CREATED)
        self.supported: object = deepcopy(SUPPORTED)
        self.balances: object = {"balances": []}
        self.poll_started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if path.endswith("x402-supported"):
            result = self.supported
        elif path == "/v1/transactions/x402":
            result = self.created
        elif path.endswith("/cancel"):
            return httpx.Response(204)
        elif path == "/v1/balances":
            result = self.balances
        else:
            self.poll_started.set()
            if self.block:
                await self.release.wait()
            result = self.polls.pop(0)
        if isinstance(result, Exception):
            raise result
        if isinstance(result, httpx.Response):
            return result
        return httpx.Response(200, json=result)

    async def aclose(self) -> None:
        self.closed = True


async def buyer(platform: Platform) -> Buyer:
    return await Buyer.create(
        ClientOptions(api_key="test-key", transport=platform), poll_interval=0, pending_timeout=1
    )


@pytest.mark.parametrize("case", CASES, ids=[case["id"] for case in CASES])
async def test_shared_buyer(case: dict[str, Any]) -> None:
    data = case["input"]
    before = deepcopy(case)
    async with (
        server(cast(list[Exchange], case["platform"]["exchanges"])) as base,
        await Buyer.create(
            ClientOptions(api_key=data["api_key"], base_url=base),
            poll_interval=data.get("poll_interval_ms", 1) / 1000,
            pending_timeout=data.get("timeout_ms", 2000) / 1000,
        ) as client,
    ):
        try:
            payment = await client.prepare(
                PaymentRequirements.model_validate(data["requirement"]),
                ResourceInfo.model_validate(data["context"]["resource"]),
                payment_id=data["payment_id"],
            )
            if case["operation"] == "x402.buyer.cancel":
                await payment.cancel()
            if case["operation"] == "x402.buyer.concurrent-await":
                first, second = await asyncio.gather(
                    payment.await_payload(), payment.await_payload()
                )
                assert first == second
                result = first
            else:
                result = await payment.await_payload()
            observed: dict[str, object] = {
                "result": {
                    "encodedPayload": result.encoded_payload,
                    "paymentPayload": result.payment_payload.model_dump(
                        by_alias=True, exclude_none=True
                    ),
                    "transactionId": result.transaction_id,
                }
            }
        except X402PaymentError as error:
            failure: dict[str, object] = {"code": error.code}
            if error.status is not None:
                failure["details"] = {"status": error.status}
            observed = {"error": failure}
        except InflowApiError as error:
            observed = {
                "error": {
                    "code": "api-error",
                    "http_status": error.http_status,
                    "details": {"body": error.body},
                }
            }
        except ValueError:
            observed = {"error": {"code": "invalid-input"}}
        expected = deepcopy(case["expect"])
        if "error" in expected:
            expected["error"].pop("message")
        assert observed == expected
    assert case == before


async def test_control_scopes_and_hooks() -> None:
    platform = Platform()
    calls: list[str] = []
    async with await buyer(platform) as client:
        client.set_spend_controls({"allowed_assets": [], "max_amount_per_payment": "$0"})

        def policy(version: int, entries: list[Any]) -> list[Any]:
            assert version == 2
            calls.append("policy")
            return entries

        async def before(context: PaymentCreationContext) -> None:
            calls.append("before")
            assert isinstance(context.selected_requirements, PaymentRequirements)
            context.selected_requirements.amount = "999"

        def after(context: PaymentCreatedContext) -> None:
            calls.append("after")
            context.payment_payload.payload.clear()

        client.register_policy(policy).on_before_payment_creation(before).on_after_payment_creation(
            after
        )
        payload = await client.create_payment_payload(required())
        assert payload.payload == {"transactionId": "transaction"}
        assert calls == ["policy", "before", "after"]
        creation = json.loads(platform.requests[1].content)
        assert creation["accept"]["amount"] == REQUIREMENT.amount
    assert platform.closed


@pytest.mark.parametrize("output", [[], [REQUIREMENT.model_copy(update={"network": "elsewhere"})]])
async def test_policy_rejection_and_unsupported_policy_output(output: list[Any]) -> None:
    platform = Platform()
    async with await buyer(platform) as client:
        client.register_policy(lambda _v, _r: output)
        with pytest.raises(NoMatchingRequirementsError):
            await client.create_payment_payload(required())
        assert len(platform.requests) == 1


async def test_before_abort() -> None:
    platform = Platform()
    async with await buyer(platform) as client:
        client.on_before_payment_creation(lambda _: AbortResult(reason="declined"))
        with pytest.raises(PaymentAbortedError):
            await client.prepare(REQUIREMENT, RESOURCE)
        assert len(platform.requests) == 1


async def test_shared_waits_and_independent_results() -> None:
    platform = Platform()
    platform.block = True
    async with await buyer(platform) as client:
        count = 0

        def after(_: PaymentCreatedContext) -> None:
            nonlocal count
            count += 1

        client.on_after_payment_creation(after)
        prepared = await client.prepare(REQUIREMENT, RESOURCE)
        first = asyncio.create_task(prepared.await_payload())
        await platform.poll_started.wait()
        second = asyncio.create_task(prepared.await_payload(timeout=0))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        platform.release.set()
        result = await second
        result.payment_payload.payload.clear()
        assert (await prepared.await_payload()).payment_payload.payload
        assert count == 1
        assert len(platform.requests) == 3


async def test_cancel_wait_and_resume_without_server_cancel() -> None:
    platform = Platform()
    platform.block = True
    async with await buyer(platform) as client:
        prepared = await client.prepare(REQUIREMENT, RESOURCE)
        task = asyncio.create_task(prepared.await_payload())
        await platform.poll_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        platform.release.set()
        assert (await prepared.await_payload()).transaction_id == "transaction"
        assert not any(r.url.path.endswith("/cancel") for r in platform.requests)


async def test_explicit_cancel_and_close() -> None:
    platform = Platform()
    platform.block = True
    client = await buyer(platform)
    prepared = await client.prepare(REQUIREMENT, RESOURCE)
    task = asyncio.create_task(prepared.await_payload())
    await platform.poll_started.wait()
    await asyncio.gather(prepared.cancel(), prepared.cancel())
    with pytest.raises(X402PaymentError, match="cancelled"):
        await task
    await client.aclose()
    assert sum(r.url.path.endswith("/cancel") for r in platform.requests) == 1
    assert platform.closed


async def test_one_shot_failure_cleanup_and_recovery() -> None:
    platform = Platform()
    platform.polls = [{"status": "DECLINED"}]
    async with await buyer(platform) as client:

        def recover(context: PaymentCreationFailureContext) -> RecoveredPayloadResult:
            assert isinstance(context.error, X402PaymentError)
            return RecoveredPayloadResult(
                PaymentPayload(accepted=REQUIREMENT, payload={"recovered": True})
            )

        client.on_payment_creation_failure(lambda _: None).on_payment_creation_failure(recover)
        assert (await client.create_payment_payload(required())).payload == {"recovered": True}
    assert platform.requests[-1].url.path.endswith("/cancel")


async def test_after_failure_not_repeated() -> None:
    platform = Platform()
    calls = 0
    async with await buyer(platform) as client:

        def after(_: PaymentCreatedContext) -> None:
            nonlocal calls
            calls += 1
            raise RuntimeError("hook failed")

        client.on_after_payment_creation(after)
        prepared = await client.prepare(REQUIREMENT, RESOURCE)
        for _ in range(2):
            with pytest.raises(RuntimeError, match="hook failed"):
                await prepared.await_payload()
        assert calls == 1


async def test_failure_hooks_preserve_exception_and_isolate_context() -> None:
    class ApplicationError(Exception):
        def __init__(self, code: int, detail: str) -> None:
            self.code = code
            super().__init__(detail)

    error = ApplicationError(7, "application failure")
    platform = Platform()
    platform.polls = [error]
    seen: list[Exception] = []
    async with await buyer(platform) as client:

        def first(context: PaymentCreationFailureContext) -> None:
            seen.append(context.error)
            assert isinstance(context.selected_requirements, PaymentRequirements)
            context.selected_requirements.amount = "999"

        def second(context: PaymentCreationFailureContext) -> None:
            seen.append(context.error)
            assert isinstance(context.selected_requirements, PaymentRequirements)
            assert context.selected_requirements.amount == REQUIREMENT.amount

        client.on_payment_creation_failure(first).on_payment_creation_failure(second)
        with pytest.raises(ApplicationError) as caught:
            await client.create_payment_payload(required())
        assert caught.value is error
        assert seen == [error, error]


@pytest.mark.parametrize("blocked", [False, True])
async def test_external_wallet_spend_controls_with_real_signer(blocked: bool) -> None:
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    from x402.mechanisms.evm.exact import ExactEvmScheme

    account = Account.from_key("0x" + "11" * 32)
    requirement = PaymentRequirements(
        scheme="exact",
        network="eip155:8453",
        asset="0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        amount="100000",
        pay_to="0x" + "22" * 20,
        max_timeout_seconds=60,
        extra={"name": "USD Coin", "version": "2"},
    )
    platform = Platform()
    async with await buyer(platform) as client:
        client.register(requirement.network, ExactEvmScheme(account))
        if blocked:
            client.set_spend_controls({"max_amount_per_payment": "$0"})
            with pytest.raises(NoMatchingRequirementsError, match="spend_controls"):
                await client.create_payment_payload(required(requirement))
        else:
            payment = await client.create_payment_payload(required(requirement))
            authorization = payment.payload["authorization"]
            message = {
                "from": authorization["from"],
                "to": authorization["to"],
                "value": int(authorization["value"]),
                "validAfter": int(authorization["validAfter"]),
                "validBefore": int(authorization["validBefore"]),
                "nonce": authorization["nonce"],
            }
            typed = encode_typed_data(
                domain_data={
                    "name": "USD Coin",
                    "version": "2",
                    "chainId": 8453,
                    "verifyingContract": requirement.asset,
                },
                message_types={
                    "TransferWithAuthorization": [
                        {"name": "from", "type": "address"},
                        {"name": "to", "type": "address"},
                        {"name": "value", "type": "uint256"},
                        {"name": "validAfter", "type": "uint256"},
                        {"name": "validBefore", "type": "uint256"},
                        {"name": "nonce", "type": "bytes32"},
                    ]
                },
                message_data=message,
            )
            assert (
                Account.recover_message(typed, signature=payment.payload["signature"])
                == account.address
            )
            assert message["value"] == 100000
        assert len(platform.requests) == 1


@pytest.mark.parametrize("setting", [float("inf"), float("nan"), -1])
async def test_invalid_wait_settings(setting: float) -> None:
    with pytest.raises(ValueError):
        await Buyer.create(ClientOptions(), poll_interval=setting)


async def test_setup_failure_closes_transport() -> None:
    platform = Platform()
    platform.supported = httpx.Response(403, json={"code": "denied"})
    with pytest.raises(InflowApiError):
        await buyer(platform)
    assert platform.closed


async def test_capability_refresh_and_copy_isolation() -> None:
    platform = Platform()
    client = await buyer(platform)
    snapshot = await client.get_supported()
    snapshot.kinds.clear()
    assert client.supports(REQUIREMENT)
    platform.supported = {"kinds": []}
    await client.get_supported(refresh=True)
    assert not client.supports(REQUIREMENT)
    platform.supported = httpx.Response(503)
    with pytest.raises(InflowApiError):
        await client.get_supported(refresh=True)
    assert (await client.get_supported()).kinds == []
    await client.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await client.get_supported()


@pytest.mark.parametrize(
    ("balances", "expected"),
    [
        (
            {
                "balances": [
                    {"currency": "USDC", "available": "0.5"},
                    {"currency": "USDT", "available": "2.0"},
                ]
            },
            "USDT",
        ),
        (
            {
                "balances": [
                    {"currency": "USDC", "available": "-1"},
                    {"currency": "USDT", "available": "bad"},
                    {},
                ]
            },
            "USDC",
        ),
        ({"balances": [{"currency": "USDC", "available": "1"}]}, "USDC"),
        ({"balances": []}, "USDC"),
        ({}, "USDC"),
        ([], "USDC"),
        (httpx.Response(503), "USDC"),
    ],
)
async def test_balance_selection(balances: object, expected: str) -> None:
    platform = Platform()
    platform.balances = balances
    first = REQUIREMENT.model_copy(
        update={"amount": "1000000000000000000", "extra": {"assetName": "USDC"}}
    )
    second = first.model_copy(update={"extra": {"assetName": "USDT"}})
    async with await buyer(platform) as client:
        selected = await client.select_inflow_requirement(required(first, second))
        assert selected is not None
        assert selected.extra["assetName"] == expected


async def test_balance_selection_invalid_amount_and_preference() -> None:
    platform = Platform()
    platform.balances = {"balances": [{"currency": "USDC", "available": "2"}]}
    first = REQUIREMENT.model_copy(update={"amount": "bad", "extra": {"assetName": "USDC"}})
    second = REQUIREMENT.model_copy(update={"extra": {"assetName": "USDC"}})
    async with await buyer(platform) as client:
        assert await client.select_inflow_requirement(required(first, second)) == second
        client._prefer = ("unsupported", "balance")
        assert await client.select_inflow_requirement(required()) == REQUIREMENT


async def test_preparation_body_and_status() -> None:
    platform = Platform()
    platform.polls = [{"status": "APPROVED"}, ready()]
    async with await buyer(platform) as client:
        payment = await client.prepare(
            REQUIREMENT,
            RESOURCE,
            transaction_request_extensions={
                "serviceId": "service",
                "accept": "bad",
                "x402Version": 1,
            },
        )
        body = json.loads(platform.requests[1].content)
        assert body["serviceId"] == "service"
        assert body["accept"]["scheme"] == "balance"
        assert body["x402Version"] == 2
        assert await payment.status() == "APPROVED"
        assert await client.get_x402_payload("transaction") == ready()


async def test_unsupported_version_and_overrides() -> None:
    platform = Platform()
    async with await buyer(platform) as client:
        incompatible = required().model_copy(update={"x402_version": 3})
        with pytest.raises(ValueError):
            await client.create_payment_payload(incompatible)
        with pytest.raises(ValueError):
            await client.select_inflow_requirement(incompatible)
        alternate = ResourceInfo(url="https://merchant.example/other")
        await client.create_payment_payload(required(), resource=alternate, extensions={})
        assert json.loads(platform.requests[1].content)["resource"]["url"] == alternate.url


async def test_extension_hooks_only_when_advertised() -> None:
    from types import SimpleNamespace

    from x402.schemas.extensions import ClientExtension

    platform = Platform()
    calls: list[str] = []

    async def hook(declaration: object, context: PaymentCreationContext) -> None:
        calls.append("extension")
        assert declaration == {"info": {"value": 1}}

    # Upstream accepts duck-typed extensions with optional hook members.
    extension = cast(
        ClientExtension,
        SimpleNamespace(key="example", hooks=SimpleNamespace(on_before_payment_creation=hook)),
    )
    async with await buyer(platform) as client:
        client.register_extension(extension)
        await client.create_payment_payload(
            required(), extensions={"example": {"info": {"value": 1}}}
        )
    assert calls == ["extension"]


async def test_one_shot_cancelled_wait_cancels_approval() -> None:
    platform = Platform()
    platform.block = True
    async with await buyer(platform) as client:
        task = asyncio.create_task(client.create_payment_payload(required()))
        await platform.poll_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert platform.requests[-1].url.path.endswith("/cancel")


async def test_after_hook_failure_does_not_cancel_signed_payment() -> None:
    platform = Platform()
    async with await buyer(platform) as client:

        def after(_: PaymentCreatedContext) -> None:
            raise RuntimeError("application hook")

        client.on_after_payment_creation(after)
        with pytest.raises(RuntimeError, match="application hook"):
            await client.create_payment_payload(required())
        assert not any(r.url.path.endswith("/cancel") for r in platform.requests)


async def test_close_stops_pending_wait_without_cancelling_server_approval() -> None:
    platform = Platform()
    platform.block = True
    client = await buyer(platform)
    payment = await client.prepare(REQUIREMENT, RESOURCE)
    waiting = asyncio.create_task(payment.await_payload())
    await platform.poll_started.wait()
    await client.aclose()
    with pytest.raises(X402PaymentError, match="cancelled"):
        await waiting
    assert not any(r.url.path.endswith("/cancel") for r in platform.requests)


@pytest.mark.parametrize(
    "response",
    [
        [],
        {"status": "APPROVED", "paymentPayload": {}, "encodedPayload": ""},
        {
            "status": "APPROVED",
            "paymentPayload": {
                "x402Version": 3,
                "accepted": REQUIREMENT.model_dump(by_alias=True),
                "payload": {},
            },
            "encodedPayload": "eA==",
        },
    ],
)
async def test_invalid_payload(response: object) -> None:
    platform = Platform()
    platform.polls = [response]
    async with await buyer(platform) as client:
        with pytest.raises(X402PaymentError):
            await client.create_payment_payload(required())


async def test_bounded_wait_and_native_provider_timeout() -> None:
    platform = Platform()
    calls = 0

    async def token() -> str:
        nonlocal calls
        calls += 1
        if calls > 2:
            raise TimeoutError("provider timeout")
        return "test-token"

    async with await Buyer.create(ClientOptions(access_token=token, transport=platform)) as client:
        payment = await client.prepare(REQUIREMENT, RESOURCE)
        with pytest.raises(X402PaymentError, match="timed out"):
            await payment.await_payload(timeout=0)
        with pytest.raises(TimeoutError, match="provider timeout"):
            await payment.await_payload()


@pytest.mark.parametrize("status", [200, 402, 307])
async def test_upstream_http_payment_replay(status: int) -> None:
    from x402.http.clients.httpx import x402AsyncTransport

    platform = Platform()
    requests: list[httpx.Request] = []

    async def merchant(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert "x-api-key" not in request.headers
        assert request.headers["authorization"] == "Bearer application-token"
        assert request.content == b'{"query":"example"}'
        if len(requests) == 1:
            return httpx.Response(
                402,
                headers={
                    "PAYMENT-REQUIRED": base64.b64encode(
                        required().model_dump_json(by_alias=True).encode()
                    ).decode()
                },
            )
        payment = json.loads(base64.b64decode(request.headers["payment-signature"]))
        assert payment["payload"]["transactionId"] == "transaction"
        return httpx.Response(
            status, headers={"Location": "https://other.example/"}, content=b"result"
        )

    async def body() -> Any:
        yield b'{"query":'
        yield b'"example"}'

    async with (
        await buyer(platform) as client,
        httpx.AsyncClient(
            transport=x402AsyncTransport(client, httpx.MockTransport(merchant)),
            follow_redirects=False,
        ) as http,
    ):
        response = await http.post(
            RESOURCE.url, content=body(), headers={"Authorization": "Bearer application-token"}
        )
        assert response.status_code == status
    assert len(requests) == 2
    assert sum(r.url.path == "/v1/transactions/x402" for r in platform.requests) == 1


async def test_upstream_mcp_payment_flow() -> None:
    from datetime import timedelta

    from mcp.types import CallToolResult
    from x402.mcp.client import x402MCPSession
    from x402.mcp.constants import MCP_PAYMENT_META_KEY

    calls: list[dict[str, Any] | None] = []

    class Session:
        async def call_tool(
            self,
            name: str,
            arguments: dict[str, Any],
            read_timeout_seconds: timedelta,
            meta: dict[str, Any] | None = None,
        ) -> CallToolResult:
            assert name == "search" and arguments == {"query": "example"}
            calls.append(meta)
            if meta is None:
                return CallToolResult(
                    content=[],
                    isError=True,
                    structuredContent=required().model_dump(by_alias=True, exclude_none=True),
                )
            assert meta[MCP_PAYMENT_META_KEY]["payload"] == {"transactionId": "transaction"}
            return CallToolResult(content=[], isError=False)

    async with await buyer(Platform()) as client:
        session = x402MCPSession(Session(), client)
        result = await session.call_tool("search", {"query": "example"})
        assert result.payment_made and not result.is_error
    assert len(calls) == 2


async def test_approved_creation_polls_again_without_interval_delay() -> None:
    platform = Platform()
    platform.created["approvalStatus"] = "APPROVED"
    platform.polls = [{"status": "PENDING"}, ready()]
    async with await buyer(platform) as client:
        payment = await client.prepare(REQUIREMENT, RESOURCE)
        assert (
            await payment.await_payload(poll_interval=60, timeout=1)
        ).transaction_id == "transaction"


async def test_creation_failure_reaches_failure_hook_without_cancellation() -> None:
    class FailingPlatform(Platform):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/transactions/x402":
                self.requests.append(request)
                return httpx.Response(503)
            return await super().handle_async_request(request)

    platform = FailingPlatform()
    async with await buyer(platform) as client:
        with pytest.raises(InflowApiError):
            await client.create_payment_payload(required())
    assert len(platform.requests) == 2


async def test_public_cancel_approval() -> None:
    platform = Platform()
    async with await buyer(platform) as client:
        await client.cancel_approval("approval")
    assert platform.requests[-1].url.path.endswith("/approval/cancel")


async def test_refresh_requests_share_successful_fetch() -> None:
    class BlockingPlatform(Platform):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("x402-supported") and self.requests:
                self.poll_started.set()
                await self.release.wait()
            return await super().handle_async_request(request)

    platform = BlockingPlatform()
    async with await buyer(platform) as client:
        first = asyncio.create_task(client.get_supported(refresh=True))
        await platform.poll_started.wait()
        second = asyncio.create_task(client.get_supported(refresh=True))
        await asyncio.sleep(0)
        platform.release.set()
        assert await first == await second
    assert len(platform.requests) == 2


async def test_late_transport_success_is_not_a_successful_wait() -> None:
    class SlowPlatform(Platform):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/x402") and request.method == "GET":
                with suppress(asyncio.CancelledError):
                    await asyncio.sleep(5)
            return await super().handle_async_request(request)

    async with await buyer(SlowPlatform()) as client:
        payment = await client.prepare(REQUIREMENT, RESOURCE)
        with pytest.raises(X402PaymentError, match="timed out"):
            await payment.await_payload(timeout=0.01)


@pytest.mark.parametrize("outcome", ["failure", "cancel-waiter", "close"])
async def test_concurrent_refresh_lifecycle(outcome: str) -> None:
    class BlockingPlatform(Platform):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("x402-supported") and self.requests:
                self.poll_started.set()
                await self.release.wait()
            return await super().handle_async_request(request)

    platform = BlockingPlatform()
    async with await buyer(platform) as client:
        first = asyncio.create_task(client.get_supported(refresh=True))
        await platform.poll_started.wait()
        second = asyncio.create_task(client.get_supported(refresh=True))
        await asyncio.sleep(0)
        if outcome == "failure":
            platform.supported = httpx.Response(503)
            platform.release.set()
            results = await asyncio.gather(first, second, return_exceptions=True)
            assert isinstance(results[0], InflowApiError)
            assert results[0] is results[1]
            assert len(platform.requests) == 2
            assert client.supports(REQUIREMENT)
            platform.supported = SUPPORTED
            await client.get_supported(refresh=True)
            assert len(platform.requests) == 3
        elif outcome == "cancel-waiter":
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            platform.release.set()
            assert (await second).kinds
            assert len(platform.requests) == 2
        else:
            await client.aclose()
            for task in (first, second):
                with pytest.raises(asyncio.CancelledError):
                    await task


@pytest.mark.parametrize("remaining_waiter", [False, True])
async def test_cancelled_refresh_waiter_does_not_log_late_failure(remaining_waiter: bool) -> None:
    class BlockingPlatform(Platform):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("x402-supported") and self.requests:
                self.poll_started.set()
                await self.release.wait()
            return await super().handle_async_request(request)

    platform = BlockingPlatform()
    observed: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _, event: observed.append(event))
    try:
        async with await buyer(platform) as client:
            first = asyncio.create_task(client.get_supported(refresh=True))
            await platform.poll_started.wait()
            shared = client._refresh
            assert shared is not None
            second = (
                asyncio.create_task(client.get_supported(refresh=True))
                if remaining_waiter
                else None
            )
            await asyncio.sleep(0)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert not shared.done()
            platform.supported = httpx.Response(503)
            platform.release.set()
            await asyncio.gather(shared, return_exceptions=True)
            if second is not None:
                with pytest.raises(InflowApiError) as failure:
                    await second
                assert failure.value is shared.exception()
            await asyncio.sleep(0)
            assert observed == []
            assert client._refresh is None
            assert client.supports(REQUIREMENT)
            assert len(platform.requests) == 2
            platform.supported = SUPPORTED
            await client.get_supported(refresh=True)
            assert len(platform.requests) == 3
    finally:
        loop.set_exception_handler(previous)


@pytest.mark.parametrize("remaining_waiter", [False, True])
async def test_cancelled_payment_waiter_preserves_late_hook_error(remaining_waiter: bool) -> None:
    platform = Platform()
    started, release = asyncio.Event(), asyncio.Event()
    failure = RuntimeError("application hook failed")
    calls = 0
    observed: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _, event: observed.append(event))
    try:
        async with await buyer(platform) as client:

            async def after(_: PaymentCreatedContext) -> None:
                nonlocal calls
                calls += 1
                started.set()
                await release.wait()
                raise failure

            client.on_after_payment_creation(after)
            payment = await client.prepare(REQUIREMENT, RESOURCE)
            first = asyncio.create_task(payment.await_payload())
            await started.wait()
            completion = payment._completion
            assert completion is not None
            second = asyncio.create_task(payment.await_payload()) if remaining_waiter else None
            await asyncio.sleep(0)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert not completion.done()
            release.set()
            await asyncio.gather(completion, return_exceptions=True)
            if second is not None:
                with pytest.raises(RuntimeError) as caught:
                    await second
                assert caught.value is failure
            await asyncio.sleep(0)
            assert observed == []
            for _ in range(2):
                with pytest.raises(RuntimeError) as caught:
                    await payment.await_payload()
                assert caught.value is failure
            assert calls == 1
            assert payment._completion is completion
            assert not any(r.url.path.endswith("/cancel") for r in platform.requests)
    finally:
        loop.set_exception_handler(previous)


async def test_expired_capabilities_refresh() -> None:
    platform = Platform()
    async with await buyer(platform) as client:
        client._expires = 0
        platform.supported = {"kinds": []}
        assert (await client.get_supported()).kinds == []
        assert len(platform.requests) == 2


async def test_cancel_during_hook_even_if_hook_swallows_cancellation() -> None:
    started = asyncio.Event()
    platform = Platform()
    async with await buyer(platform) as client:

        async def after(_: PaymentCreatedContext) -> None:
            started.set()
            with suppress(asyncio.CancelledError):
                await asyncio.Event().wait()
            await asyncio.sleep(0)

        client.on_after_payment_creation(after)
        payment = await client.prepare(REQUIREMENT, RESOURCE)
        task = asyncio.create_task(payment.await_payload())
        await started.wait()
        await payment.cancel()
        with pytest.raises(X402PaymentError, match="cancelled"):
            await task
        assert payment._completion is not None and not payment._completion.cancelled()


@pytest.mark.parametrize("outcome", ["missing-identifier", "identifier", "version"])
async def test_external_extension_output(outcome: str) -> None:
    from eth_account import Account
    from x402.mechanisms.evm.exact import ExactEvmScheme

    from inflowpay.x402 import declare_payment_identifier, generate_payment_id

    class Extension:
        key = "payment-identifier"
        hooks = None
        transport_hooks = None

        def enrich_payment_payload(self, payment_payload: Any, payment_required: Any) -> Any:
            if outcome == "version":
                return payment_payload.model_copy(update={"x402_version": 3})
            if outcome == "identifier":
                payment_payload.extensions[self.key]["info"]["id"] = generate_payment_id()
            return payment_payload

    declaration = declare_payment_identifier()
    declaration["info"]["required"] = True
    offer = REQUIREMENT.model_copy(
        update={
            "scheme": "exact",
            "network": "eip155:8453",
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "amount": "1",
            "pay_to": "0x" + "22" * 20,
            "extra": {"name": "USD Coin", "version": "2"},
        }
    )
    async with await buyer(Platform()) as client:
        client.register(offer.network, ExactEvmScheme(Account.from_key("0x" + "11" * 32)))
        client.register_extension(Extension())
        if outcome == "identifier":
            payment = await client.create_payment_payload(
                required(offer), extensions={"payment-identifier": declaration}
            )
            assert isinstance(payment, PaymentPayload) and payment.extensions
        else:
            with pytest.raises(X402PaymentError):
                await client.create_payment_payload(
                    required(offer), extensions={"payment-identifier": declaration}
                )


@pytest.mark.parametrize("scheme", ["exact", "upto"])
async def test_real_external_permit2_and_eip2612(scheme: str) -> None:
    from eth_account import Account
    from eth_account.messages import encode_typed_data
    from x402.mechanisms.evm.exact import ExactEvmScheme
    from x402.mechanisms.evm.signers import EthAccountSigner
    from x402.mechanisms.evm.upto import UptoEvmScheme

    account = Account.from_key("0x" + "11" * 32)
    reads: list[str] = []

    class Signer(EthAccountSigner):
        def read_contract(self, address: str, abi: Any, function_name: str, *args: Any) -> Any:
            reads.append(function_name)
            return 7 if function_name == "nonces" else 0

    offer = REQUIREMENT.model_copy(
        update={
            "scheme": scheme,
            "network": "eip155:8453",
            "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "amount": "123",
            "pay_to": "0x" + "22" * 20,
            "extra": {
                "assetTransferMethod": "permit2",
                "name": "USD Coin",
                "version": "2",
                "facilitatorAddress": "0x" + "33" * 20,
            },
        }
    )
    declaration = {"eip2612GasSponsoring": {"info": {"version": "1"}, "schema": {"type": "object"}}}
    platform = Platform()
    async with await buyer(platform) as client:
        signer = Signer(account)
        client.register(
            offer.network, ExactEvmScheme(signer) if scheme == "exact" else UptoEvmScheme(signer)
        )
        payload = await client.create_payment_payload(required(offer), extensions=declaration)
        assert isinstance(payload, PaymentPayload) and payload.extensions
        assert payload.accepted.scheme == scheme
        permit = payload.extensions["eip2612GasSponsoring"]["info"]
        typed = encode_typed_data(
            domain_data={
                "name": "USD Coin",
                "version": "2",
                "chainId": 8453,
                "verifyingContract": offer.asset,
            },
            message_types={
                "Permit": [
                    {"name": "owner", "type": "address"},
                    {"name": "spender", "type": "address"},
                    {"name": "value", "type": "uint256"},
                    {"name": "nonce", "type": "uint256"},
                    {"name": "deadline", "type": "uint256"},
                ]
            },
            message_data={
                "owner": account.address,
                "spender": "0x000000000022D473030F116dDEE9F6B43aC78BA3",
                "value": 123,
                "nonce": 7,
                "deadline": int(permit["deadline"]),
            },
        )
        assert Account.recover_message(typed, signature=permit["signature"]) == account.address
        assert payload.extensions["eip2612GasSponsoring"]["schema"] == {"type": "object"}
        if scheme == "upto":
            assert (
                payload.payload["permit2Authorization"]["witness"]["facilitator"]
                == "0x" + "33" * 20
            )
    assert reads == ["allowance", "nonces"]
    assert len(platform.requests) == 1
