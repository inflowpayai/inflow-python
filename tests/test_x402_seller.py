import asyncio
import base64
import hashlib
import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from pydantic import ValidationError
from starlette.responses import JSONResponse
from x402 import x402ResourceServer
from x402.http.middleware.fastapi import payment_middleware, set_settlement_overrides
from x402.http.types import PaymentOption, RouteConfig
from x402.schemas import AssetAmount, PaymentPayload, PaymentRequirements, SupportedKind
from x402.schemas.v1 import PaymentPayloadV1, PaymentRequirementsV1

from inflowpay import ClientOptions, InflowApiError
from inflowpay.x402._seller import SellerConfig, build_offers
from inflowpay.x402.facilitator import Facilitator
from inflowpay.x402.seller import Seller

CASES: list[dict[str, Any]] = json.loads(
    Path(__file__).with_name("fixtures").joinpath("x402-seller.json").read_text()
)["cases"]
CONFIG = next(
    case["input"]["config"] for case in CASES if case["operation"] == "x402.seller.offers"
)
REQUIREMENTS = PaymentRequirements.model_validate(CASES[0]["input"]["payment_requirements"])
PAYLOAD = PaymentPayload.model_validate(CASES[0]["input"]["payment_payload"])
SUPPORTED = {"kinds": CONFIG["supported"], "signers": {"eip155:*": ["wildcard"], "inflow:1": []}}


class Platform(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.config: Any = deepcopy(CONFIG)
        self.supported: Any = deepcopy(SUPPORTED)
        self.verification: Any = {"isValid": True}
        self.settlement: Any = {"success": True, "transaction": "tx", "network": "inflow:1"}
        self.requests: list[httpx.Request] = []
        self.block = ""
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.closed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        operation = request.url.path.rsplit("/", 1)[-1]
        if operation == self.block:
            self.started.set()
            await self.release.wait()
        value = {
            "config": self.config,
            "supported": self.supported,
            "verify": self.verification,
            "settle": self.settlement,
        }[operation]
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, httpx.Response) else httpx.Response(200, json=value)

    async def aclose(self) -> None:
        self.closed = True

    def options(self, *, anonymous: bool = False) -> ClientOptions:
        return ClientOptions(api_key=None if anonymous else "seller-key", transport=self)


def wire_offer(offer: PaymentOption) -> dict[str, Any]:
    assert isinstance(offer.price, AssetAmount)
    return {
        "scheme": offer.scheme,
        "network": offer.network,
        "payTo": offer.pay_to,
        "price": offer.price.model_dump(exclude_none=True),
        "maxTimeoutSeconds": offer.max_timeout_seconds,
        "extra": offer.extra,
    }


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
async def test_shared_seller_vectors(case: dict[str, Any]) -> None:
    data = case["input"]
    platform = Platform()
    exchanges = deepcopy(case.get("platform", {}).get("exchanges", []))

    async def transport(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json=platform.config if request.url.path.endswith("config") else platform.supported,
            )
        expected = exchanges.pop(0)
        assert request.method == expected["request"]["method"]
        assert request.url.path == expected["request"]["path"]
        for name, value in expected["request"]["headers"].items():
            assert request.headers[name] == value
        assert json.loads(request.content) == expected["request"]["json"]
        response = expected["response"]
        return httpx.Response(
            response["status"], json=response["json"], headers=response.get("headers")
        )

    options = ClientOptions(
        api_key=data.get("api_key", "seller-key"), transport=httpx.MockTransport(transport)
    )

    async def execute() -> Any:
        operation = case["operation"].rsplit(".", 1)[-1]
        if operation in ("offers", "route"):
            platform.config = data["config"]
            platform.supported = data.get("supported", SUPPORTED)
            arguments = data["options"]
            async with await Seller.create(options) as seller:
                kwargs = {
                    key: arguments[key] for key in ("schemes", "networks") if key in arguments
                }
                if operation == "offers":
                    return [
                        wire_offer(offer)
                        for offer in await seller.offers(arguments["price"], **kwargs)
                    ]
                route = await seller.route(
                    arguments["price"],
                    permit2=arguments.get("assetTransferMethod") == "permit2",
                    **kwargs,
                )
                assert isinstance(route.accepts, list)
                result: dict[str, Any] = {"accepts": [wire_offer(offer) for offer in route.accepts]}
                if route.extensions is not None:
                    result["extensions"] = route.extensions
                return result
        anonymous = "api_key" not in data
        options_without_key = ClientOptions(
            api_key=data.get("api_key"), transport=httpx.MockTransport(transport)
        )
        async with await Facilitator.create(
            options_without_key, anonymous=anonymous
        ) as facilitator:
            payment = PaymentPayload.model_validate(data["payment_payload"])
            required = PaymentRequirements.model_validate(data["payment_requirements"])
            if operation == "verify-settle":
                verified = await facilitator.verify(payment, required)
                results = {"verification": verified.model_dump(exclude_none=True)}
                if verified.is_valid:
                    results["settlement"] = (
                        await facilitator.settle(payment, required)
                    ).model_dump(exclude_none=True)
                return results
            response = await (
                facilitator.verify(payment, required)
                if operation == "verify"
                else facilitator.settle(payment, required)
            )
            return response.model_dump(exclude_none=True)

    expected = case["expect"]
    if "error" in expected:
        with pytest.raises((InflowApiError, ValueError)) as caught:
            await execute()
        if isinstance(caught.value, InflowApiError):
            assert caught.value.http_status == expected["error"]["http_status"]
    else:
        actual = await execute()
        if case["id"] == "x402.seller.sponsorship-prefers-eip2612":
            # Upstream Python and Node word schema descriptions differently; assert
            # identical declaration fields and validation constraints, not copy text.
            def constraints(value: Any) -> Any:
                if isinstance(value, dict):
                    return {
                        key: constraints(item)
                        for key, item in value.items()
                        if key != "description"
                    }
                if isinstance(value, list):
                    return [constraints(item) for item in value]
                return value

            assert constraints(actual) == constraints(expected["result"])
        else:
            assert actual == expected["result"]
    assert not exchanges


@pytest.mark.parametrize("anonymous,key", [(False, None), (True, "key")])
async def test_facilitator_requires_explicit_auth_mode(anonymous: bool, key: str | None) -> None:
    with pytest.raises(ValueError):
        await Facilitator.create(ClientOptions(api_key=key), anonymous=anonymous)


async def test_anonymous_rejects_token_provider() -> None:
    async def token() -> str:
        return "token"

    with pytest.raises(ValueError):
        await Facilitator.create(ClientOptions(access_token=token), anonymous=True)
    with pytest.raises(ValueError):
        await Facilitator.create(ClientOptions(api_key_provider=token), anonymous=True)
    with pytest.raises(ValueError):
        await Seller.create(ClientOptions())


@pytest.mark.parametrize("kind", ["seller", "facilitator"])
async def test_setup_failure_closes_transport(kind: str) -> None:
    platform = Platform()
    platform.supported = {"invalid": True}
    with pytest.raises(ValidationError):
        await (
            Seller.create(platform.options())
            if kind == "seller"
            else Facilitator.create(platform.options())
        )
    assert platform.closed


async def test_seller_cache_refresh_isolation_and_signers() -> None:
    platform = Platform()
    async with await Seller.create(platform.options()) as seller:
        config = await seller.config()
        config.assets.clear()
        assert (await seller.config()).assets
        assert len(platform.requests) == 2
        assert await seller.get_signer_addresses("inflow:1") == []
        assert await seller.get_signer_addresses("eip155:8453") == ["wildcard"]
        for network in ("unknown:1", "unknown", ":bad"):
            assert await seller.get_signer_addresses(network) == []
        (await seller.get_supported()).kinds.clear()
        assert (await seller.get_supported()).kinds
        platform.config["assets"] = []
        assert (await seller.config(refresh=True)).assets == []
        seller._supported.expires = 0
        assert (await seller.get_supported()).kinds
    with pytest.raises(RuntimeError):
        await seller.config()
    with pytest.raises(RuntimeError):
        await seller.__aenter__()


async def test_refresh_concurrency_cancellation_and_failure() -> None:
    platform = Platform()
    seller = await Seller.create(platform.options())
    platform.block = "config"
    first = asyncio.create_task(seller.config(refresh=True))
    second = asyncio.create_task(seller.config(refresh=True))
    await platform.started.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    platform.release.set()
    await second
    assert sum(r.url.path.endswith("config") for r in platform.requests) == 2
    platform.config = httpx.Response(503, json={"code": "unavailable"})
    with pytest.raises(InflowApiError):
        await seller.config(refresh=True)
    assert (await seller.config()).assets
    platform.release.clear()
    task = asyncio.create_task(seller.config(refresh=True))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    await seller.aclose()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert platform.closed


@pytest.mark.parametrize("operation", ["config", "supported"])
@pytest.mark.parametrize("remaining_waiter", [False, True])
async def test_cancelled_refresh_waiter_does_not_log_late_failure(
    operation: str, remaining_waiter: bool
) -> None:
    platform = Platform()
    observed: list[dict[str, Any]] = []
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    loop.set_exception_handler(lambda _, event: observed.append(event))
    try:
        async with await Seller.create(platform.options()) as seller:
            platform.block = operation
            refresh = seller.config if operation == "config" else seller.get_supported
            first = asyncio.create_task(refresh(refresh=True))
            await platform.started.wait()
            cache = seller._config if operation == "config" else seller._supported
            shared = cache.task
            assert shared is not None
            second = asyncio.create_task(refresh(refresh=True)) if remaining_waiter else None
            await asyncio.sleep(0)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            assert not shared.done()
            setattr(platform, operation, httpx.Response(503))
            platform.release.set()
            await asyncio.gather(shared, return_exceptions=True)
            if second is not None:
                with pytest.raises(InflowApiError) as failure:
                    await second
                assert failure.value is shared.exception()
            await asyncio.sleep(0)
            assert observed == []
            assert cache.task is None
            assert sum(r.url.path.endswith(operation) for r in platform.requests) == 2
            await refresh()
            assert len(platform.requests) == 3
    finally:
        loop.set_exception_handler(previous)


async def test_fixed_facilitator_snapshot_and_recreation() -> None:
    platform = Platform()
    async with await Facilitator.create(platform.options()) as facilitator:
        assert facilitator.get_supported().kinds
        facilitator.get_supported().kinds.clear()
        platform.supported = {"kinds": []}
        assert facilitator.get_supported().kinds
        assert len(platform.requests) == 1
    async with await Facilitator.create(platform.options()) as replacement:
        assert replacement.get_supported().kinds == []


@pytest.mark.parametrize(
    "inner", [{}, {"transactionId": ""}, {"transactionId": 4}, {"unknown": "é"}]
)
async def test_identifier_fallback_and_caller_isolation(inner: dict[str, object]) -> None:
    platform = Platform()
    payload = PAYLOAD.model_copy(deep=True)
    payload.payload = inner
    payload.extensions = {"other": {"info": {"value": "keep"}}}
    before = payload.model_dump()
    async with await Facilitator.create(platform.options()) as facilitator:
        await facilitator.verify(payload, REQUIREMENTS)
        await facilitator.settle(payload, REQUIREMENTS)
    requests = [json.loads(r.content) for r in platform.requests if r.method == "POST"]
    assert requests[0] == requests[1]
    material = "payload:" + json.dumps(inner, separators=(",", ":"), ensure_ascii=False)
    assert (
        requests[0]["paymentPayload"]["extensions"]["payment-identifier"]["info"]["id"]
        == "pay_" + hashlib.sha256(material.encode()).hexdigest()[:32]
    )
    assert payload.model_dump() == before


async def test_invalid_versions_are_rejected_before_http() -> None:
    platform = Platform()
    async with await Facilitator.create(platform.options()) as facilitator:
        bad = PAYLOAD.model_copy(update={"x402_version": 1})
        with pytest.raises(ValueError):
            await facilitator.verify(bad, REQUIREMENTS)
        old = PaymentRequirementsV1(
            scheme="exact",
            network="base",
            max_amount_required="1",
            resource="https://test",
            description="",
            mime_type="",
            pay_to="pay",
            max_timeout_seconds=60,
            asset="USDC",
        )
        with pytest.raises(ValueError):
            await facilitator.settle(PAYLOAD, old)
        payload = PaymentPayloadV1(x402_version=1, scheme="exact", network="base", payload={})
        with pytest.raises(ValueError):
            await facilitator.verify(payload, old)
    assert len(platform.requests) == 1


@pytest.mark.parametrize(
    "header,expected", [(None, 5), ("invalid", 5), ("10", 5), ("2", 2), ("0", 0)]
)
async def test_pending_delay_and_cancellation(
    monkeypatch: pytest.MonkeyPatch, header: str | None, expected: int
) -> None:
    platform = Platform()
    platform.settlement = httpx.Response(
        409,
        json={"errorReason": "idempotency_pending"},
        headers={} if header is None else {"Retry-After": header},
    )
    async with await Facilitator.create(platform.options()) as facilitator:
        real_sleep = asyncio.sleep

        async def sleep(delay: float) -> None:
            if delay == 0 and expected != 0:
                await real_sleep(0)
                return
            assert delay == expected
            raise asyncio.CancelledError

        # Patch only facilitator sleep, leaving the transport's scheduling yield intact.
        monkeypatch.setattr("inflowpay.x402.facilitator.asyncio", SimpleNamespace(sleep=sleep))
        with pytest.raises(asyncio.CancelledError):
            await facilitator.settle(PAYLOAD, REQUIREMENTS)
    assert len(platform.requests) == 2


@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError("offline"),
        httpx.Response(503),
        httpx.Response(409, json={"errorReason": "conflict"}),
    ],
)
async def test_settlement_never_retries_uncertain_failure(failure: object) -> None:
    platform = Platform()
    platform.settlement = failure
    async with await Facilitator.create(platform.options()) as facilitator:
        with pytest.raises(InflowApiError):
            await facilitator.settle(PAYLOAD, REQUIREMENTS)
    assert len(platform.requests) == 2


async def test_missing_network_is_not_silently_repaired() -> None:
    platform = Platform()
    platform.settlement = {"success": False, "transaction": "", "errorReason": "settlement_failed"}
    async with await Facilitator.create(platform.options()) as facilitator:
        with pytest.raises(ValidationError):
            await facilitator.settle(PAYLOAD, REQUIREMENTS)


@pytest.mark.parametrize(
    "price,currency,amount",
    [
        ("0", "USDC", "0"),
        ("1.00000000", "USDC", "1000000"),
        ("$1", "USDT", None),
        ("1.00 USDC", "USDC", "1000000"),
    ],
)
async def test_price_precision_and_explicit_currency(
    price: str, currency: str, amount: str | None
) -> None:
    async with await Seller.create(Platform().options()) as seller:
        offers = await seller.offers(price, currency=currency, schemes=["exact"])
        if amount is None:
            assert offers == []
        else:
            assert wire_offer(offers[0])["price"]["amount"] == amount


@pytest.mark.parametrize(
    "price", ["$1 USDC", "1.000000001 USDC", " 1 USDC", "1 USDC ", "NaN", "+1 USDC"]
)
async def test_malformed_price_is_not_coerced(price: str) -> None:
    async with await Seller.create(Platform().options()) as seller:
        with pytest.raises(ValueError):
            await seller.offers(price)


async def test_mixed_assets_and_passthrough_registration() -> None:
    platform = Platform()
    platform.config["assets"].append(deepcopy(platform.config["assets"][0]))
    platform.config["assets"][1].update(
        blockchain="SOLANA", network="solana:test", assetTransferMethod="solana", permit2Proxy=None
    )
    platform.config["assets"].append(deepcopy(platform.config["assets"][0]))
    platform.config["wallets"].append(
        {"blockchain": "SOLANA", "address": "solana-receiver", "feePayer": "payer"}
    )
    async with await Seller.create(platform.options()) as seller:
        offers = await seller.offers("$1")
        assert len(offers) == 4
        assert offers[2].extra == {
            "assetName": "USDC",
            "name": "USDC",
            "version": "2",
            "assetTransferMethod": "solana",
            "feePayer": "payer",
        }
        registrations = await seller.scheme_registrations(schemes=["balance"])
        assert len(registrations) == 1
        scheme = registrations[0]["server"]
        assert scheme.default_asset_transfer_method == "default"
        with pytest.raises(ValueError):
            scheme.parse_price("$1", "inflow:1")
        value = AssetAmount(asset="USDC", amount="1", extra={"nested": {"a": 1}})
        copied = scheme.parse_price(value, "inflow:1")
        assert copied == value and copied is not value
        all_schemes = await seller.scheme_registrations()
        assert len(all_schemes) == 3
        assert set(all_schemes[0]["server"].payment_flows) == {"eip3009", "permit2"}
        assert (
            scheme.enhance_payment_requirements(
                REQUIREMENTS,
                SupportedKind(scheme="balance", network="inflow:1", x402_version=2),
                [],
            )
            is not REQUIREMENTS
        )
        assert await seller.scheme_registrations(schemes=[]) == []


@pytest.mark.parametrize(
    "update", [{"permit2Proxy": None}, {"network": "solana:test"}, {"permit2Proxy": "wrong"}]
)
async def test_permit2_only_does_not_emit_unsupported_onchain_offer(
    update: dict[str, object],
) -> None:
    platform = Platform()
    platform.config["assets"][0].update(update)
    async with await Seller.create(platform.options()) as seller:
        route = await seller.route("$1", schemes=["exact"], permit2=True)
        assert route.accepts == []
        assert route.extensions is None
        assert await seller.offers("$1", schemes=["upto"]) == []


async def test_declared_permit2_without_proxy_preserves_server_config() -> None:
    config = SellerConfig.model_validate(CONFIG)
    config.assets[0].asset_transfer_method = "permit2"
    config.assets[0].permit2_proxy = None
    offers = build_offers(config, "$1", schemes=["exact"])
    assert "permit2Proxy" not in (offers[0].extra or {})
    config.wallets[0].blockchain = "OTHER"
    assert build_offers(config, "$1", schemes=["exact"]) == []


async def test_sponsorship_never_declared_for_metered_routes() -> None:
    case = next(case for case in CASES if case["id"] == "x402.seller.metered-explicit")
    platform = Platform()
    platform.config = deepcopy(case["input"]["config"])
    async with await Seller.create(platform.options()) as seller:
        route = await seller.route("$1", schemes=["upto"])
        assert route.extensions is None
        registrations = await seller.scheme_registrations(schemes=["upto"])
        assert len(registrations) == 1
        assert registrations[0]["server"].scheme == "upto"


@pytest.mark.parametrize(
    "failure", ["missing-name", "missing-version", "wrong-kind", "wrong-proxy"]
)
async def test_sponsorship_requires_matching_domain_and_facilitator(failure: str) -> None:
    case = next(case for case in CASES if case["id"] == "x402.seller.sponsorship-prefers-eip2612")
    platform = Platform()
    platform.config, platform.supported = (
        deepcopy(case["input"]["config"]),
        deepcopy(case["input"]["supported"]),
    )
    if failure == "missing-name":
        platform.config["assets"][0]["tokenName"] = ""
    elif failure == "missing-version":
        platform.config["assets"][0]["tokenVersion"] = ""
    elif failure == "wrong-kind":
        platform.supported["kinds"][0]["x402Version"] = 1
    else:
        platform.config["assets"][0]["permit2Proxy"] = "wrong"
    platform.config["assets"][0]["supportsEip7702"] = False
    async with await Seller.create(platform.options()) as seller:
        assert (await seller.route("$1", schemes=["exact"], permit2=True)).extensions is None


async def test_sponsorship_schemas_are_independent_between_routes() -> None:
    case = next(case for case in CASES if case["id"] == "x402.seller.sponsorship-prefers-eip2612")
    platform = Platform()
    platform.config = deepcopy(case["input"]["config"])
    platform.supported = deepcopy(case["input"]["supported"])
    async with await Seller.create(platform.options()) as seller:
        first = await seller.route("$1", schemes=["exact"], permit2=True)
        second = await seller.route("$1", schemes=["exact"], permit2=True)
        assert first.extensions is not None
        expected = deepcopy(second.extensions)
        first.extensions["eip2612GasSponsoring"]["schema"]["properties"].clear()
        assert second.extensions == expected
        assert (await seller.route("$1", schemes=["exact"], permit2=True)).extensions == expected


async def test_setup_cancellation_closes_pending_requests() -> None:
    platform = Platform()
    platform.block = "config"
    task = asyncio.create_task(Seller.create(platform.options()))
    await platform.started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert platform.closed


async def test_metered_fastapi_route_preserves_authorized_maximum() -> None:
    case = next(case for case in CASES if case["id"] == "x402.seller.metered-explicit")
    seller_platform, platform = Platform(), Platform()
    seller_platform.config = deepcopy(case["input"]["config"])
    platform.supported = {"kinds": seller_platform.config["supported"]}
    platform.settlement = {
        "success": True,
        "transaction": "tx",
        "network": "eip155:8453",
        "amount": "250000",
    }
    async with (
        await Seller.create(seller_platform.options()) as seller,
        await Facilitator.create(platform.options()) as facilitator,
    ):
        resource = x402ResourceServer(facilitator)
        for entry in await seller.scheme_registrations(schemes=["upto"]):
            resource.register(entry["network"], entry["server"])
        route = await seller.route("$1", schemes=["upto"])
        app = FastAPI()
        app.middleware("http")(payment_middleware({"GET /paid": route}, resource))

        @app.get("/paid")
        async def paid() -> JSONResponse:
            response = JSONResponse({"used": 250000})
            set_settlement_overrides(response, {"amount": "250000"})
            return response

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://seller.test"
        ) as http:
            challenge = await http.get("/paid")
            assert challenge.status_code == 402
            required = json.loads(base64.b64decode(challenge.headers["payment-required"]))
            payload = PaymentPayload(
                accepted=PaymentRequirements.model_validate(required["accepts"][0]),
                payload={"signature": "test-signature"},
            )
            response = await http.get(
                "/paid",
                headers={
                    "payment-signature": base64.b64encode(
                        payload.model_dump_json(exclude_none=True).encode()
                    ).decode()
                },
            )
        assert response.status_code == 200
        sent = [json.loads(r.content) for r in platform.requests if r.method == "POST"]
        assert len(sent) == 2
        assert sent[0]["paymentRequirements"]["amount"] == "1000000"
        assert sent[1]["paymentRequirements"]["amount"] == "250000"
        assert sent[1]["paymentPayload"]["accepted"]["amount"] == "1000000"
        assert sent[0]["paymentPayload"]["extensions"] == sent[1]["paymentPayload"]["extensions"]
        assert not any("override" in name for name in response.headers)


@pytest.mark.parametrize(
    "outcome", ["success", "verify-reject", "settle-reject", "handler-error", "handler-throws"]
)
async def test_actual_fastapi_payment_flow(outcome: str) -> None:
    platform = Platform()
    if outcome == "verify-reject":
        platform.verification = {"isValid": False, "invalidReason": "invalid_payload"}
    if outcome == "settle-reject":
        platform.settlement = {
            "success": False,
            "errorReason": "settlement_failed",
            "transaction": "",
            "network": "inflow:1",
        }
    async with (
        await Seller.create(Platform().options()) as seller,
        await Facilitator.create(platform.options()) as facilitator,
    ):
        resource = x402ResourceServer(facilitator)
        for registration in await seller.scheme_registrations():
            resource.register(registration["network"], registration["server"])
        route = RouteConfig(accepts=await seller.offers("$1", schemes=["balance"]))
        app = FastAPI()
        app.middleware("http")(payment_middleware({"GET /paid": route}, resource))
        called = []

        @app.get("/paid")
        async def paid(request: Request) -> JSONResponse:
            called.append(request.state.payment_requirements)
            if outcome == "handler-throws":
                raise ValueError("handler failed")
            return JSONResponse(
                {"paid": True}, status_code=500 if outcome == "handler-error" else 200
            )

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app, raise_app_exceptions=False),
            base_url="https://seller.example",
        ) as http:
            challenge = await http.get("/paid")
            assert challenge.status_code == 402
            wire = json.loads(base64.b64decode(challenge.headers["payment-required"]))
            selected = PaymentRequirements.model_validate(wire["accepts"][0])
            payload = PaymentPayload(accepted=selected, payload={"transactionId": "tx"})
            encoded = base64.b64encode(
                json.dumps(payload.model_dump(exclude_none=True)).encode()
            ).decode()
            response = await http.get("/paid", headers={"payment-signature": encoded})
        assert (
            response.status_code
            == {
                "success": 200,
                "verify-reject": 402,
                "settle-reject": 402,
                "handler-error": 500,
                "handler-throws": 500,
            }[outcome]
        )
        assert len(called) == (0 if outcome == "verify-reject" else 1)
        posts = [r.url.path for r in platform.requests if r.method == "POST"]
        assert posts == ["/v1/x402/verify"] + (
            ["/v1/x402/settle"] if outcome in ("success", "settle-reject") else []
        )
        if outcome in ("success", "settle-reject"):
            receipt = json.loads(base64.b64decode(response.headers["payment-response"]))
            assert receipt["network"] == "inflow:1"
            assert receipt["success"] is (outcome == "success")
