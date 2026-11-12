import asyncio
import threading
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from typing import Literal, TypedDict

import httpx
import pytest
from fastapi import FastAPI, Request
from mpp import Challenge, Credential, Receipt
from mpp.server import Mpp
from mpp.server import Request as PaymentRequest
from mpp.server.decorator import pay
from mpp.server.method import RequestContext
from starlette.responses import JSONResponse
from x402 import x402ResourceServer
from x402.extensions.bazaar import declare_discovery_extension
from x402.http import HTTPFacilitatorClient
from x402.http.types import (
    HTTPRequestContext,
    HTTPResponseBody,
    ProcessSettleResult,
    RouteConfig,
    RouteConfigurationError,
)
from x402.http.utils import decode_payment_required_header, encode_payment_signature_header
from x402.schemas import AssetAmount, PaymentPayload, PaymentRequired
from x402.schemas.hooks import SettleContext, VerifyContext

from inflowpay.mpp import WireObject
from inflowpay.mpp.seller import MppSellerConfigurationError
from inflowpay.mpp.seller import Seller as MppSeller
from inflowpay.x402.facilitator import Facilitator
from inflowpay.x402.fastapi import RouteResolver, _observe_initialization, payment_middleware
from inflowpay.x402.seller import Seller
from test_card_seller import platform as card_platform
from test_instrument_parity import card_config
from test_mpp_seller import ID, Platform, options
from test_stripe_seller import platform as stripe_platform
from test_x402_seller import CASES
from test_x402_upfront import FlowPlatform


class PriceSpec(TypedDict):
    amount: str
    currency: str


DynamicPrice = Callable[[Request], str | PriceSpec | Awaitable[str | PriceSpec]]


@asynccontextmanager
async def merchant(
    platform: FlowPlatform,
    price: DynamicPrice,
    *,
    flow: Literal["authorization", "upfront"] = "authorization",
    schemes: list[str] | None = None,
    status: int = 200,
) -> AsyncIterator[httpx.AsyncClient]:
    async with (
        await Seller.create(platform.options()) as seller,
        await Facilitator.create(platform.options()) as facilitator,
    ):
        resource = x402ResourceServer(facilitator)
        for registration in await seller.scheme_registrations():
            resource.register(registration["network"], registration["server"])

        async def route(request: Request) -> RouteConfig:
            value = price(request)
            selected = await value if isinstance(value, Awaitable) else value
            amount = selected["amount"] if isinstance(selected, dict) else selected
            currency = selected["currency"] if isinstance(selected, dict) else None
            return await seller.route(amount, currency=currency, schemes=schemes, payment_flow=flow)

        fixed = await seller.route("3 USDC", schemes=["balance"])
        app = FastAPI()
        app.middleware("http")(
            payment_middleware({"GET /paid": route, "GET /fixed": fixed}, resource)
        )

        @app.get("/paid")
        async def paid() -> JSONResponse:
            platform.events.append("handler")
            return JSONResponse({"paid": True}, status_code=status)

        @app.get("/free")
        async def free() -> dict[str, bool]:
            return {"free": True}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app, raise_app_exceptions=False),
            base_url="https://seller.example",
        ) as client:
            yield client


@pytest.mark.parametrize("flow", ["authorization", "upfront"])
@pytest.mark.parametrize("scheme", ["exact", "balance", "instrument"])
@pytest.mark.parametrize("outcome", ["success", "rejected", "handler-error"])
async def test_dynamic_x402_execution(
    flow: Literal["authorization", "upfront"], scheme: str, outcome: str
) -> None:
    platform = FlowPlatform("rejected" if outcome == "rejected" else "success")
    if scheme == "instrument":
        platform.config = card_config().model_dump(by_alias=True)
        platform.supported = {
            "kinds": [{"scheme": scheme, "network": "inflow:1", "x402Version": 2}]
        }
    calls: list[str] = []

    def price(context: Request) -> PriceSpec:
        calls.append(str(context.url))
        return {"amount": "1.25", "currency": "USD" if scheme == "instrument" else "USDC"}

    async with merchant(
        platform,
        price,
        schemes=[scheme],
        flow=flow,
        status=500 if outcome == "handler-error" else 200,
    ) as client:
        assert not calls
        assert (await client.get("/free")).status_code == 200
        assert not calls
        response = await client.get("/paid")
        assert response.status_code == 402
        required = decode_payment_required_header(response.headers["payment-required"])
        assert isinstance(required, PaymentRequired)
        assert len(required.accepts) == 1
        accepted = required.accepts[0]
        assert accepted.amount == str(
            125 * 10 ** ({"exact": 6, "balance": 8, "instrument": 18}[scheme] - 2)
        )
        assert len(calls) == 1
        payment = PaymentPayload(accepted=accepted, payload={"transactionId": ID})
        response = await client.get(
            "/paid", headers={"payment-signature": encode_payment_signature_header(payment)}
        )
        assert len(calls) == 2
        if flow == "upfront":
            assert platform.events == (
                ["settle"] if outcome == "rejected" else ["settle", "handler"]
            )
        else:
            assert platform.events == (
                ["verify", "handler"]
                if outcome == "handler-error"
                else ["verify", "handler", "settle"]
            )
        assert response.status_code == (
            402 if outcome == "rejected" else 500 if outcome == "handler-error" else 200
        )


async def test_currency_changes_are_request_local() -> None:
    platform = FlowPlatform()
    platform.config["assets"].append(
        {
            **platform.config["assets"][0],
            "assetId": "0xabcdef",
            "currency": "USDT",
            "assetName": "USDT",
        }
    )
    entered = asyncio.Event()
    calls = 0

    async def price(context: Request) -> PriceSpec:
        nonlocal calls
        calls += 1
        if calls == 2:
            entered.set()
        await entered.wait()
        currency = context.query_params.get("currency") or "USDC"
        assert isinstance(currency, str)
        return {"amount": "1" if currency == "USDC" else "2", "currency": currency}

    async with merchant(platform, price) as client:
        responses = await asyncio.gather(
            client.get("/paid?currency=USDC"), client.get("/paid?currency=USDT")
        )
        for response, currency, amount in zip(responses, ("USDC", "USDT"), ("1", "2"), strict=True):
            assert response.status_code == 402
            required = decode_payment_required_header(response.headers["payment-required"])
            assert isinstance(required, PaymentRequired)
            offers = required.accepts
            assert len(offers) == 2
            assert {offer.extra["assetName"] for offer in offers} == {currency}
            assert offers[0].amount == amount + "000000"
            assert offers[1].amount == amount + "00000000"
        assert calls == 2
        assert not platform.events


@pytest.mark.parametrize("flow", ["authorization", "upfront"])
@pytest.mark.parametrize("change", ["amount", "currency"])
async def test_x402_paid_retry_must_match_current_price(
    flow: Literal["authorization", "upfront"], change: str
) -> None:
    platform = FlowPlatform()
    platform.config["assets"].append(
        {**platform.config["assets"][0], "assetId": "0xabcdef", "currency": "USDT"}
    )
    current: PriceSpec = {"amount": "1", "currency": "USDC"}
    async with merchant(platform, lambda context: current, flow=flow) as client:
        response = await client.get("/paid")
        required = decode_payment_required_header(response.headers["payment-required"])
        assert isinstance(required, PaymentRequired)
        payment = PaymentPayload(accepted=required.accepts[0], payload={"transactionId": ID})
        headers = {"payment-signature": encode_payment_signature_header(payment)}
        if change == "amount":
            current["amount"] = "2"
        else:
            current["currency"] = "USDT"
        assert (await client.get("/paid", headers=headers)).status_code == 402
        assert not platform.events
        current.update({"amount": "1", "currency": "USDC"})
        assert (await client.get("/paid", headers=headers)).status_code == 200
        assert platform.events == (
            ["settle", "handler"] if flow == "upfront" else ["verify", "handler", "settle"]
        )


async def test_cancelled_price_resolution_does_not_charge_or_poison_next_request() -> None:
    platform = FlowPlatform()
    started = asyncio.Event()
    release = asyncio.Event()

    async def price(context: Request) -> str:
        started.set()
        await release.wait()
        return "$1"

    async with merchant(platform, price) as client:
        pending = asyncio.create_task(client.get("/paid"))
        await started.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert not platform.events
        release.set()
        assert (await client.get("/paid")).status_code == 402
        assert not platform.events


@pytest.mark.parametrize("method", ["inflow", "instrument", "tempo", "stripe", "card"])
async def test_mpp_helpers_resolve_current_request(method: str) -> None:
    platform = (
        stripe_platform()
        if method == "stripe"
        else card_platform()
        if method == "card"
        else Platform()
    )
    if method == "tempo":
        assert isinstance(platform.config, dict)
        platform.config["supportedMethods"] = [{"id": "tempo"}]
    calls = 0

    def price(request: PaymentRequest) -> WireObject:
        nonlocal calls
        calls += 1
        large = httpx.URL(request.url).params.get("bundle") == "large"
        if method == "tempo":
            return {
                "amount": "2000000" if large else "1000000",
                "currency": "0x" + "1" * 40,
                "recipient": "0x" + "2" * 40,
            }
        return {
            "amount": "2" if large else "1",
            "currency": "USD" if method == "instrument" else "USDC",
        }

    async with await MppSeller.create(
        options(platform), method="inflow" if method == "instrument" else method
    ) as seller:
        app = FastAPI()

        @app.get("/paid")
        @pay(
            Mpp(
                methods=[seller.priced(price)], realm="seller.example", secret_key="test-only"
            ).charge()
        )
        async def paid(request: Request, credential: Credential, receipt: Receipt) -> JSONResponse:
            return JSONResponse({"paid": True})

        assert calls == 0
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://seller.example"
        ) as client:
            for suffix, multiplier in (("", 1), ("?bundle=large", 2)):
                response = await client.get("/paid" + suffix)
                assert response.status_code == 402
                challenge = Challenge.from_www_authenticate(response.headers["www-authenticate"])
                scale = 100 if method in ("stripe", "card") else 1000000 if method == "tempo" else 1
                assert challenge.request["amount"] == str(multiplier * scale)
            assert calls == 2
            assert len(platform.requests) == 1


async def test_mpp_paid_retry_rejects_a_changed_price() -> None:
    platform = Platform()
    amount = "1"
    calls = 0
    handled = 0

    def price(request: PaymentRequest) -> WireObject:
        nonlocal calls
        calls += 1
        return {"amount": amount, "currency": "USDC"}

    async with await MppSeller.create(options(platform)) as seller:
        app = FastAPI()

        @app.get("/paid")
        @pay(
            Mpp(
                methods=[seller.priced(price)], realm="seller.example", secret_key="test-only"
            ).charge()
        )
        async def paid(request: Request, credential: Credential, receipt: Receipt) -> JSONResponse:
            nonlocal handled
            handled += 1
            return JSONResponse({"paid": True})

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://seller.example"
        ) as client:
            challenge = Challenge.from_www_authenticate(
                (await client.get("/paid")).headers["www-authenticate"]
            )
            authorization = Credential(
                challenge=challenge.to_echo(), payload={"transactionId": ID}
            ).to_authorization()
            amount = "2"
            assert (
                await client.get("/paid", headers={"authorization": authorization})
            ).status_code == 402
            assert len(platform.requests) == 1
            amount = "1"
            assert (
                await client.get("/paid", headers={"authorization": authorization})
            ).status_code == 200
            assert [request.url.path for request in platform.requests][-2:] == [
                "/v1/mpp/validate",
                "/v1/mpp/broadcast",
            ]
            assert handled == 1
            assert calls == 3


async def test_mpp_callback_failure_and_unsupported_currency_do_not_pay() -> None:
    platform = Platform()
    mode = "raises"
    supplied: WireObject = {"amount": "1", "currency": "UNKNOWN", "methodDetails": {}}
    before = deepcopy(supplied)

    def price(request: PaymentRequest) -> WireObject:
        if mode == "raises":
            raise RuntimeError("Price unavailable")
        return supplied

    async with await MppSeller.create(options(platform)) as seller:
        app = FastAPI()

        @app.get("/paid")
        @pay(
            Mpp(
                methods=[seller.priced(price)], realm="seller.example", secret_key="test-only"
            ).charge()
        )
        async def paid(request: Request, credential: Credential, receipt: Receipt) -> JSONResponse:
            pytest.fail("Invalid pricing must not reach the paid handler")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app, raise_app_exceptions=False),
            base_url="https://seller.example",
        ) as client:
            assert (await client.get("/paid")).status_code == 500
            mode = "unsupported"
            assert (await client.get("/paid")).status_code == 500
            assert len(platform.requests) == 1
            assert supplied == before


@pytest.mark.parametrize("body_enabled", [False, True])
async def test_mpp_async_body_and_concurrent_prices(body_enabled: bool) -> None:
    platform = Platform()
    entered = asyncio.Event()
    calls = 0
    seen: list[bytes | None] = []

    async def price(request: PaymentRequest) -> WireObject:
        nonlocal calls
        calls += 1
        seen.append(request.body)
        if calls == 2:
            entered.set()
        await entered.wait()
        return {"amount": httpx.URL(request.url).params["amount"], "currency": "USDC"}

    async with await MppSeller.create(options(platform)) as seller:
        method = seller.priced(price, needs_request_body=body_enabled)
        app = FastAPI()

        @app.post("/paid")
        @pay(Mpp(methods=[method], realm="seller.example", secret_key="test-only").charge())
        async def paid(request: Request, credential: Credential, receipt: Receipt) -> JSONResponse:
            return JSONResponse({"body": (await request.body()).decode()})

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://seller.example"
        ) as client:
            responses = await asyncio.gather(
                *[
                    client.post("/paid?amount=" + amount, content=b"original")
                    for amount in ("1", "2")
                ]
            )
            assert [
                Challenge.from_www_authenticate(r.headers["www-authenticate"]).request["amount"]
                for r in responses
            ] == ["1", "2"]
            assert seen == ([b"original"] * 2 if body_enabled else [None] * 2)
            challenge = Challenge.from_www_authenticate(responses[0].headers["www-authenticate"])
            credential = Credential(challenge=challenge.to_echo(), payload={"transactionId": ID})
            paid_response = await client.post(
                "/paid?amount=1",
                content=b"original",
                headers={"authorization": credential.to_authorization()},
            )
            assert paid_response.status_code == 200
            assert paid_response.json() == {"body": "original"}
            assert calls == 3
        assert method.resolve_request is not None
        with pytest.raises(MppSellerConfigurationError, match="HTTP request"):
            value = method.resolve_request(RequestContext(request={}))
            assert isinstance(value, Awaitable)
            await value


@pytest.mark.parametrize("mode", ["raises", "unsupported", "empty", "unregistered"])
async def test_x402_invalid_route_fails_closed(mode: str) -> None:
    platform = FlowPlatform()
    async with (
        await Seller.create(platform.options()) as seller,
        await Facilitator.create(platform.options()) as facilitator,
    ):
        resource = x402ResourceServer(facilitator)
        if mode != "unregistered":
            for registration in await seller.scheme_registrations():
                resource.register(registration["network"], registration["server"])

        async def route(request: Request) -> RouteConfig:
            if mode == "raises":
                raise RuntimeError("private pricing error")
            if mode == "empty":
                return RouteConfig(accepts=[])
            return await seller.route(
                "1 UNKNOWN" if mode == "unsupported" else "1 USDC", schemes=["exact"]
            )

        app = FastAPI()
        app.middleware("http")(payment_middleware({"GET /paid": route}, resource))

        @app.get("/paid")
        async def paid() -> None:
            pytest.fail("Unpriced requests must not reach the application")

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app, raise_app_exceptions=False),
            base_url="https://seller.example",
        ) as client:
            response = await client.get("/paid")
            assert response.status_code == 500
            assert response.text == "Internal Server Error"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://seller.example"
        ) as client:
            with pytest.raises((ValueError, RuntimeError, RouteConfigurationError)):
                await client.get("/paid")
        assert not platform.events


@pytest.mark.parametrize("root", ["", "/mounted"])
async def test_x402_body_replay_static_routes_and_matching(root: str) -> None:
    platform = FlowPlatform()
    calls = []
    async with (
        await Seller.create(platform.options()) as seller,
        await Facilitator.create(platform.options()) as facilitator,
    ):
        resource = x402ResourceServer(facilitator)
        for registration in await seller.scheme_registrations():
            resource.register(registration["network"], registration["server"])
        fixed = await seller.route("1 USDC", schemes=["balance"])
        before = deepcopy(fixed)

        async def route(request: Request) -> RouteConfig:
            calls.append(await request.body())
            return fixed

        app = FastAPI()
        app.middleware("http")(
            payment_middleware({"POST /paid/:id": route, "GET /fixed": fixed}, resource)
        )

        @app.post("/paid/{item_id}")
        async def paid(request: Request, item_id: str) -> JSONResponse:
            return JSONResponse({"body": (await request.body()).decode()})

        parent = FastAPI()
        if root:
            parent.mount(root, app)
        else:
            parent = app

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(parent), base_url="https://seller.example"
        ) as client:
            response = await client.post(root + "/paid/123", content=b"original")
            assert response.status_code == 402
            required = decode_payment_required_header(response.headers["payment-required"])
            assert isinstance(required, PaymentRequired)
            payload = PaymentPayload(accepted=required.accepts[0], payload={"transactionId": ID})
            response = await client.post(
                root + "/paid/123",
                content=b"original",
                headers={"payment-signature": encode_payment_signature_header(payload)},
            )
            assert response.status_code == 200
            assert response.json() == {"body": "original"}
            assert calls == [b"original", b"original"]
            assert (await client.get(root + "/paid/123")).status_code == 405
            assert (await client.get(root + "/fixed")).status_code == 402
            assert (await client.head(root + "/fixed")).status_code == 402
            assert (await client.get(root + "/%66ixed")).status_code == 402
        assert fixed == before


async def test_sponsorship_follows_selected_currency_through_verification_and_settlement() -> None:
    case = next(case for case in CASES if case["id"] == "x402.seller.sponsorship-prefers-eip2612")
    platform = FlowPlatform()
    platform.config = deepcopy(case["input"]["config"])
    platform.supported = deepcopy(case["input"]["supported"])
    asset = platform.config["assets"][0]
    platform.config["assets"].extend(
        [
            {
                **asset,
                "assetId": "0x4444444444444444444444444444444444444444",
                "currency": "USDT",
                "supportsEip2612": False,
                "supportsEip7702": False,
            },
            {
                **asset,
                "assetId": "0x5555555555555555555555555555555555555555",
                "currency": "DAI",
                "supportsEip2612": False,
                "supportsEip7702": True,
            },
        ]
    )
    calls: list[str] = []

    async def price(context: Request) -> str:
        currency = context.query_params.get("currency")
        assert isinstance(currency, str)
        calls.append(currency)
        await asyncio.sleep(0.001 if currency == "USDC" else 0)
        return "1 " + currency

    async with (
        await Seller.create(platform.options()) as seller,
        await Facilitator.create(platform.options()) as facilitator,
    ):
        resource = x402ResourceServer(facilitator)
        for registration in await seller.scheme_registrations():
            resource.register(registration["network"], registration["server"])
        original = {"custom": {"info": {"keep": True}}}

        async def route(request: Request) -> RouteConfig:
            resolved = await seller.route(await price(request), schemes=["exact"], permit2=True)
            resolved.extensions = {**(resolved.extensions or {}), **deepcopy(original)}
            return resolved

        observed: list[tuple[str, object]] = []

        def verifying(context: VerifyContext) -> None:
            observed.append(("verify", deepcopy(dict(context.declared_extensions or {}))))

        def settling(context: SettleContext) -> None:
            observed.append(("settle", deepcopy(dict(context.declared_extensions or {}))))

        resource.on_before_verify(verifying)
        resource.on_before_settle(settling)
        routes: dict[str, RouteConfig | RouteResolver] = {"GET /paid": route}
        currencies = ["USDC", "USDT", "DAI", "USD"]
        for currency in currencies:
            fixed_route = await seller.route("1 " + currency, schemes=["exact"], permit2=True)
            fixed_route.extensions = {**(fixed_route.extensions or {}), **original}
            routes["GET /fixed/" + currency] = fixed_route
        app = FastAPI()
        app.middleware("http")(payment_middleware(routes, resource))

        @app.get("/paid")
        async def paid() -> dict[str, bool]:
            return {"paid": True}

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://seller.example"
        ) as client:
            responses = await asyncio.gather(
                *[client.get("/paid?currency=" + currency) for currency in currencies]
            )
            for currency, response in zip(currencies, responses, strict=True):
                assert response.status_code == 402
                required = decode_payment_required_header(response.headers["payment-required"])
                fixed_response = await client.get("/fixed/" + currency)
                fixed = decode_payment_required_header(fixed_response.headers["payment-required"])
                assert isinstance(required, PaymentRequired) and isinstance(fixed, PaymentRequired)
                assert required.accepts == fixed.accepts
                assert required.extensions == fixed.extensions
                expected_key = {
                    "USDC": "eip2612GasSponsoring",
                    "DAI": "inflowEip7702GasSponsoring",
                }.get(currency)
                assert set(required.extensions or {}) == (
                    {"custom", expected_key} if expected_key else {"custom"}
                )
                payload = PaymentPayload(
                    accepted=required.accepts[0], payload={"transactionId": ID}
                )
                paid_response = await client.get(
                    "/paid?currency=" + currency,
                    headers={"payment-signature": encode_payment_signature_header(payload)},
                )
                assert paid_response.status_code == 200
                assert observed[-2:] == [
                    ("verify", required.extensions),
                    ("settle", required.extensions),
                ]
                assert original == {"custom": {"info": {"keep": True}}}
        assert len(calls) == 8


async def test_x402_sync_bazaar_route_keeps_parameters() -> None:
    platform = FlowPlatform()
    async with (
        await Seller.create(platform.options()) as seller,
        await Facilitator.create(platform.options()) as facilitator,
    ):
        resource = x402ResourceServer(facilitator)
        for registration in await seller.scheme_registrations():
            resource.register(registration["network"], registration["server"])
        fixed = await seller.route("1 USDC")
        fixed.extensions = declare_discovery_extension()
        before = deepcopy(fixed)
        calls = []

        def route(request: Request) -> RouteConfig:
            calls.append(request.url.path)
            return fixed

        app = FastAPI()
        app.middleware("http")(payment_middleware({"GET /items/:id": route}, resource))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://seller.example"
        ) as client:
            response = await client.get("/items/123")
            assert response.status_code == 402
            required = decode_payment_required_header(response.headers["payment-required"])
            assert isinstance(required, PaymentRequired)
            assert required.extensions is not None
            bazaar = required.extensions["bazaar"]
            assert bazaar["routeTemplate"] == "/items/:id"
            assert bazaar["info"]["input"]["pathParams"] == {"id": "123"}
            assert (await client.options("/items/123")).status_code == 404
            assert calls == ["/items/123"]
        assert fixed == before


async def test_mpp_cancelled_resolution_does_not_pay() -> None:
    platform = Platform()
    entered, release = asyncio.Event(), asyncio.Event()

    async def price(request: PaymentRequest) -> WireObject:
        entered.set()
        await release.wait()
        return {"amount": "1", "currency": "USDC"}

    async with await MppSeller.create(options(platform)) as seller:
        handler = Mpp(
            methods=[seller.priced(price)], realm="seller.example", secret_key="test-only"
        ).charge()
        request = PaymentRequest(method="GET", url="https://seller.example/paid")
        pending = asyncio.create_task(handler(request))
        await entered.wait()
        pending.cancel()
        with pytest.raises(asyncio.CancelledError):
            await pending
        assert len(platform.requests) == 1
        release.set()
        assert (await handler(request)).status == 402
        assert len(platform.requests) == 1


async def test_x402_preserves_application_callbacks() -> None:
    platform = FlowPlatform("rejected")
    async with (
        await Seller.create(platform.options()) as seller,
        await Facilitator.create(platform.options()) as facilitator,
    ):
        resource = x402ResourceServer(facilitator)
        for registration in await seller.scheme_registrations():
            resource.register(registration["network"], registration["server"])
        route = await seller.route("1 USDC", schemes=["balance"], payment_flow="upfront")
        assert isinstance(route.accepts, list)
        option = route.accepts[0]
        assert isinstance(option.price, AssetAmount) and isinstance(option.pay_to, str)
        amount, recipient = option.price, option.pay_to

        class Callbacks:
            def __init__(self) -> None:
                self.lock = threading.Lock()
                self.prices = 0
                self.recipients = 0
                self.unpaid_calls = 0
                self.failed_calls = 0

            def __call__(self, context: HTTPRequestContext) -> AssetAmount:
                self.prices += 1
                return amount

            def pay_to(self, context: HTTPRequestContext) -> str:
                self.recipients += 1
                return recipient

            def unpaid(self, context: HTTPRequestContext) -> HTTPResponseBody:
                self.unpaid_calls += 1
                return HTTPResponseBody("application/json", {"call": self.unpaid_calls})

            def failed(
                self, context: HTTPRequestContext, result: ProcessSettleResult
            ) -> HTTPResponseBody:
                self.failed_calls += 1
                return HTTPResponseBody("application/json", {"failure": self.failed_calls})

        callbacks = Callbacks()
        option.price, option.pay_to = callbacks, callbacks.pay_to
        route.accepts = option
        route.unpaid_response_body = callbacks.unpaid
        route.settlement_failed_response_body = callbacks.failed
        app = FastAPI()
        app.middleware("http")(payment_middleware({"GET /paid": route}, resource))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://seller.example"
        ) as client:
            for count in (1, 2):
                response = await client.get("/paid")
                assert response.status_code == 402 and response.json() == {"call": count}
            required = decode_payment_required_header(response.headers["payment-required"])
            assert isinstance(required, PaymentRequired)
            payload = PaymentPayload(accepted=required.accepts[0], payload={"transactionId": ID})
            response = await client.get(
                "/paid", headers={"payment-signature": encode_payment_signature_header(payload)}
            )
            assert response.status_code == 402 and response.json() == {"failure": 1}
        assert (callbacks.prices, callbacks.recipients) == (3, 3)
        assert (callbacks.unpaid_calls, callbacks.failed_calls) == (2, 1)
        assert route.accepts is option and option.price is callbacks
        assert platform.events == ["settle"]


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
async def test_x402_initializes_once_without_blocking(
    outcome: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    platform = FlowPlatform()
    entered, arrivals = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    event_thread = threading.get_ident()
    calls = 0

    def supported(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        assert threading.get_ident() != event_thread
        assert request.url.path.endswith("/supported")
        calls += 1
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        if outcome == "failure" and calls == 1:
            return httpx.Response(503, text="Temporarily unavailable")
        return httpx.Response(200, json=platform.supported)

    async with await Seller.create(platform.options()) as seller:
        facilitator = HTTPFacilitatorClient()
        monkeypatch.setattr(
            facilitator,
            "_get_sync_client",
            lambda: httpx.Client(transport=httpx.MockTransport(supported)),
        )
        resource = x402ResourceServer(facilitator)
        for registration in await seller.scheme_registrations():
            resource.register(registration["network"], registration["server"])
        fixed = await seller.route("1 USDC", schemes=["balance"])
        resolved = 0

        def route(request: Request) -> RouteConfig:
            nonlocal resolved
            resolved += 1
            if resolved == 2:
                arrivals.set()
            return fixed

        app = FastAPI()
        app.middleware("http")(payment_middleware({"GET /paid": route}, resource))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://seller.example"
        ) as client:
            assert (await client.get("/free")).status_code == 404 and calls == 0
            first = asyncio.create_task(client.get("/paid"))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                second = asyncio.create_task(client.get("/paid"))
                await asyncio.wait_for(arrivals.wait(), 2)
                if outcome == "cancel":
                    first.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await first
            finally:
                release.set()
            results = await asyncio.gather(first, second, return_exceptions=True)
            if outcome == "failure":
                assert all(isinstance(result, ValueError) for result in results)
            elif outcome == "cancel":
                assert isinstance(results[0], asyncio.CancelledError)
                assert isinstance(results[1], httpx.Response) and results[1].status_code == 402
            else:
                assert all(
                    isinstance(result, httpx.Response) and result.status_code == 402
                    for result in results
                )
            assert calls == 1
            for _ in range(2):
                assert (await client.get("/paid")).status_code == 402
            assert calls == (2 if outcome == "failure" else 1)
        await facilitator.aclose()
        assert not platform.events


async def test_x402_initialization_observer_accepts_shutdown_cancellation() -> None:
    task = asyncio.create_task(asyncio.sleep(0))
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    _observe_initialization(task)
