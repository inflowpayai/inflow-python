import asyncio
import json
import sys
from copy import deepcopy
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from mpp import Challenge, Credential, Receipt
from mpp.errors import InvalidChallengeError
from mpp.server.decorator import pay
from mpp.server.intent import broadcast_credential
from starlette.responses import JSONResponse
from x402.schemas import PaymentPayload, PaymentRequirements, ResourceInfo

from conformance.tap import tap_execute
from inflowpay import ClientOptions, InflowApiError, mpp, x402
from inflowpay.mpp.buyer import (
    BuyerMethod,
    MppMalformedCredentialError,
    MppPaymentExpiredError,
    MppPaymentFailedError,
    MppPaymentTimeoutError,
)
from inflowpay.mpp.seller import MppCredentialProblemError, MppSellerConfigurationError
from inflowpay.mpp.seller import Seller as MppSeller
from inflowpay.x402.buyer import Buyer, X402PaymentError
from inflowpay.x402.facilitator import Facilitator
from inflowpay.x402.seller import Seller

# JSON messages are validated by the shared runner. Dynamic mappings stay at this test boundary.
Data = dict[str, Any]


def options(data: Data, transport: httpx.AsyncBaseTransport | None = None) -> ClientOptions:
    base = data["base_url"]
    parsed = urlsplit(base)
    if (
        parsed.scheme != "http"
        or parsed.hostname != "127.0.0.1"
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise RuntimeError("Adapter requires a loopback platform URL")
    return ClientOptions(base_url=base, api_key=data.get("api_key"), transport=transport)


def classify(error: BaseException, operation: str, data: Data) -> Data:
    details: Data = {}
    if isinstance(error, InflowApiError):
        if operation.startswith("x402.") or operation.endswith(".payment-status"):
            return {
                "code": "api-error",
                "message": "InFlow API request failed.",
                "http_status": error.http_status,
                "details": {"body": error.body},
            }
        return {"code": error.code, "message": str(error), "http_status": error.http_status}
    if isinstance(error, (MppPaymentExpiredError, MppPaymentTimeoutError)):
        code = "payment-expired" if isinstance(error, MppPaymentExpiredError) else "payment-timeout"
        if error.transaction_id is not None:
            details["transaction_id"] = error.transaction_id
    elif isinstance(error, MppPaymentFailedError):
        code = "payment-failed"
        if error.transaction_id is not None:
            details["transaction_id"] = error.transaction_id
        if error.problem is not None:
            details["problem"] = error.problem
    elif isinstance(error, MppCredentialProblemError):
        code = "payment-failed"
        if data.get("include_problem", True):
            details["problem"] = error.problem
    elif isinstance(error, (MppMalformedCredentialError, InvalidChallengeError)):
        code = "invalid-credential"
    elif isinstance(error, mpp.MppCodecError):
        code = (
            "invalid-credential" if operation == "mpp.core.decode-credential" else "invalid-input"
        )
    elif isinstance(error, MppSellerConfigurationError):
        code = "unsupported-capability"
    elif isinstance(error, asyncio.CancelledError) and operation == "mpp.buyer.cancel":
        code = "payment-cancelled"
    elif isinstance(error, X402PaymentError):
        code = error.code
        if error.status is not None:
            details["status"] = error.status
    elif (
        (
            type(error) is ValueError
            and operation in ("x402.seller.offers", "x402.seller.route")
            and str(error)
            in (
                "Price must be '$1.00', '1.00 USDC', or a plain amount with currency",
                "A currency is required for a plain amount",
                "Price cannot be represented in the asset's decimal precision",
                "Instrument payments require USD 0.50-92233720368547758.07",
            )
        )
        or (
            type(error) is ValueError
            and operation == "mpp.buyer.fulfil"
            and data["challenge"]["method"] == "card"
        )
        or (
            type(error) is ValueError
            and operation == "x402.buyer.sign"
            and str(error) == "Invalid payment identifier"
        )
    ):
        code = "invalid-input"
    else:
        raise error
    messages = {
        "invalid-input": "Invalid input.",
        "invalid-credential": "Invalid credential.",
        "payment-failed": "Payment failed.",
        "payment-expired": "Payment expired.",
        "payment-timeout": "Payment timed out.",
        "payment-cancelled": "Payment cancelled.",
        "unsupported-capability": "Unsupported payment capability.",
    }
    if code not in messages:
        raise error
    return {"code": code, "message": messages[code], **({"details": details} if details else {})}


async def mpp_execute(operation: str, data: Data) -> object:
    match operation:
        case "mpp.core.encode":
            return mpp.encode(data["value"])
        case "mpp.core.decode":
            return mpp.decode(data["value"])
        case "mpp.core.decode-credential":
            return mpp.decode_credential(data["value"])
        case "mpp.core.decode-receipt":
            return mpp.decode_receipt(data["value"])
        case "mpp.core.parse-challenges":
            return mpp.parse_challenge_headers(data["headers"])
    if operation in ("mpp.buyer.fulfil", "mpp.buyer.cancel"):
        challenge = mpp.to_pympp_challenge(data["challenge"])
        payment: asyncio.Task[Credential]

        class Observe(httpx.AsyncHTTPTransport):
            async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                response = await super().handle_async_request(request)
                await response.aread()
                if operation == "mpp.buyer.cancel" and request.url.path.endswith(
                    "/transactions/mpp"
                ):
                    asyncio.get_running_loop().call_soon(payment.cancel)
                return response

        async with BuyerMethod(
            options(data, Observe()),
            method=challenge.method,
            intent=challenge.intent,
            poll_interval=0,
            pending_timeout=data.get("timeout_ms", 5000) / 1000,
            instrument_id=data["context"].get("instrumentId"),
            subscription_id=data["context"].get("subscriptionId"),
            merchant=data["context"].get("merchant"),
        ) as buyer:
            payment = asyncio.create_task(buyer.create_credential(challenge))
            return mpp.decode_credential((await payment).to_authorization()[8:])
    allowed = (
        "mpp.seller.prepare",
        "mpp.seller.validate",
        "mpp.seller.verify",
        "mpp.seller.route-binding",
    )
    if operation not in allowed:
        raise RuntimeError("Unknown MPP operation")
    wire = data.get("credential")
    method = wire["challenge"]["method"] if wire else data["method"]
    async with await MppSeller.create(options(data), method=method) as seller:
        if operation == "mpp.seller.prepare":
            if method == "card":
                return await route_binding(seller, data, prepare_only=True)
            return (seller.stripe_request if method == "stripe" else seller.charge_request)(
                data["request"]
            )
        if operation == "mpp.seller.route-binding":
            return await route_binding(seller, data)
        credential = Credential.from_authorization("Payment " + mpp.encode(wire))
        request = mpp.decode(credential.challenge.request)
        if not isinstance(request, dict):
            raise RuntimeError("Expected an object request")
        if operation == "mpp.seller.verify":
            receipt = await broadcast_credential(
                intent=seller, credential=credential, request=request
            )
            return mpp.decode_receipt(receipt.to_payment_receipt())
        value = await seller.validate(credential, request)
        observed = mpp.decode_credential(value.credential.to_authorization()[8:])
        observed.setdefault("source", "")
        return {
            "success": True,
            "challenge": observed["challenge"],
            "credential": observed,
            "details": value.details,
            "method": method,
            "intent": value.intent,
            "request": value.request,
            "source": observed.get("source", ""),
        }


async def route_binding(seller: MppSeller, data: Data, *, prepare_only: bool = False) -> object:
    app = FastAPI()
    prepare = {"stripe": seller.stripe_request, "card": seller.card_request}.get(
        seller.method, seller.charge_request
    )
    terms = prepare(data["request"])

    @app.get("/test")
    @pay(
        intent=seller,
        method=seller.method,
        request=lambda _: terms,
        realm="seller.example",
        secret_key="test-only-binding-secret-at-least-32-bytes",
    )
    async def handler(request: Request, credential: Credential, receipt: Receipt) -> JSONResponse:
        return JSONResponse({"ok": True})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://seller.example"
    ) as client:
        initial = await client.get("/test")
        challenge = Challenge.from_www_authenticate(initial.headers["www-authenticate"])
        if prepare_only:
            prepared = mpp.decode(challenge.request_b64)
            if not isinstance(prepared, dict):
                raise RuntimeError("Expected framework request object")
            # Framework resource binding is not part of the shared offer projection.
            return {key: value for key, value in prepared.items() if key != "_mppx_scope"}
        credential = Credential(
            challenge=challenge.to_echo(),
            payload=data["credential_payload"],
            source=data.get("source"),
        )
        terms = prepare(data["replacement_request"])
        response = await client.get(
            "/test", headers={"Authorization": credential.to_authorization()}
        )
        return {"status": response.status_code}


async def x402_execute(operation: str, data: Data) -> object:
    match operation:
        case "x402.core.identifier-valid":
            return x402.validate_payment_id(data["value"])
        case "x402.core.identifier-declaration":
            return x402.declare_payment_identifier()
        case "x402.core.identifier-entry":
            return x402.payment_identifier_entry(data["declaration"], data["payment_id"])
    if operation in ("x402.seller.offers", "x402.seller.route"):

        def configuration(request: httpx.Request) -> httpx.Response:
            if request.method != "GET" or request.url.path not in (
                "/v1/x402/config",
                "/v1/x402/supported",
            ):
                raise RuntimeError("Unexpected configuration request")
            return httpx.Response(
                200,
                json=data["config"]
                if request.url.path.endswith("config")
                else data.get("supported", {"kinds": []}),
            )

        async with await Seller.create(
            ClientOptions(
                api_key="test-only-seller-key", transport=httpx.MockTransport(configuration)
            )
        ) as seller:
            arguments = data["options"]
            kwargs = {
                key: arguments[key]
                for key in ("currency", "schemes", "networks")
                if key in arguments
            }
            if "maxTimeoutSeconds" in arguments:
                kwargs["max_timeout_seconds"] = arguments["maxTimeoutSeconds"]

            def offer(value: Any) -> Data:
                return {
                    "scheme": value.scheme,
                    "network": value.network,
                    "payTo": value.pay_to,
                    "price": value.price.model_dump(exclude_none=True),
                    "maxTimeoutSeconds": value.max_timeout_seconds,
                    "extra": value.extra,
                }

            if operation == "x402.seller.offers":
                return [offer(value) for value in await seller.offers(arguments["price"], **kwargs)]
            route = await seller.route(
                arguments["price"],
                permit2=arguments.get("assetTransferMethod") == "permit2",
                **kwargs,
            )
            if not isinstance(route.accepts, list):
                raise RuntimeError("Expected static offers")
            return {
                "accepts": [offer(value) for value in route.accepts],
                **({"extensions": route.extensions} if route.extensions is not None else {}),
            }
    if operation in ("x402.buyer.sign", "x402.buyer.cancel", "x402.buyer.concurrent-await"):
        async with await Buyer.create(
            options(data),
            instrument_id=data.get("instrument_id"),
            poll_interval=data.get("poll_interval_ms", 1) / 1000,
            pending_timeout=data.get("timeout_ms", 2000) / 1000,
        ) as buyer:
            payment = await buyer.prepare(
                PaymentRequirements.model_validate(data["requirement"]),
                ResourceInfo.model_validate(data["context"]["resource"]),
                payment_id=data["payment_id"],
            )
            if operation == "x402.buyer.cancel":
                await payment.cancel()
            if operation == "x402.buyer.concurrent-await":
                result, other = await asyncio.gather(
                    payment.await_payload(), payment.await_payload()
                )
                if result != other:
                    raise RuntimeError("Concurrent results differ")
            else:
                result = await payment.await_payload()
            return {
                "encodedPayload": result.encoded_payload,
                "paymentPayload": result.payment_payload.model_dump(
                    by_alias=True, exclude_none=True
                ),
                "transactionId": result.transaction_id,
            }
    if operation not in ("x402.seller.verify", "x402.seller.settle", "x402.seller.verify-settle"):
        raise RuntimeError("Unknown x402 operation")

    class Setup(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            # Python's synchronous upstream interface requires capabilities at construction.
            # These operations do not consume them; all verify/settle HTTP stays runner-owned.
            if request.method == "GET" and request.url.path == "/v1/x402/supported":
                return httpx.Response(200, json={"kinds": []})
            return await super().handle_async_request(request)

    async with await Facilitator.create(
        options(data, Setup()), anonymous="api_key" not in data
    ) as facilitator:
        payload = PaymentPayload.model_validate(data["payment_payload"])
        requirement = PaymentRequirements.model_validate(data["payment_requirements"])
        if operation == "x402.seller.settle":
            return (await facilitator.settle(payload, requirement)).model_dump(exclude_none=True)
        verified = await facilitator.verify(payload, requirement)
        if operation == "x402.seller.verify":
            return verified.model_dump(exclude_none=True)
        observations = {"verification": verified.model_dump(exclude_none=True)}
        if verified.is_valid:
            observations["settlement"] = (
                await facilitator.settle(payload, requirement)
            ).model_dump(exclude_none=True)
        return observations


async def runtime_call(product: str, config: ClientOptions) -> None:
    if product == "mpp-buyer":
        async with BuyerMethod(config) as buyer:
            await buyer.create_credential(
                mpp.to_pympp_challenge(
                    {
                        "id": "test",
                        "realm": "seller.example",
                        "method": "inflow",
                        "intent": "charge",
                        "request": mpp.encode({"amount": "1", "currency": "USD"}),
                    }
                )
            )
    elif product == "mpp-seller":
        async with await MppSeller.create(config):
            pass
    elif product == "x402-buyer":
        async with await Buyer.create(config):
            pass
    elif product == "x402-seller":
        async with await Seller.create(config):
            pass
    else:
        raise RuntimeError("Unknown runtime product")


async def runtime_execute(operation: str, data: Data) -> Data:
    if operation not in ("runtime.environment", "runtime.request"):
        raise RuntimeError("Unknown runtime operation")
    destinations = []
    token_calls = 0

    async def token() -> str:
        nonlocal token_calls
        value: str = data["tokens"][token_calls]
        token_calls += 1
        return value

    def capture(request: httpx.Request) -> httpx.Response:
        destinations.append(f"{request.method} {request.url}")
        return httpx.Response(403)

    class RuntimeTransport(httpx.AsyncHTTPTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            # Seller construction loads capabilities alongside config. The runtime cases
            # exercise config errors; payment capability behavior belongs to the x402 suite.
            if request.method == "GET" and request.url.path.endswith("/v1/x402/supported"):
                return httpx.Response(200, json={"kinds": []})
            if operation == "runtime.environment":
                return capture(request)
            return await super().handle_async_request(request)

    if operation == "runtime.request":
        options(data)  # Validate the runner's destination before allowing real HTTP.
    config = ClientOptions(
        environment=data.get("environment", "production"),
        base_url=data.get("base_url"),
        api_key=data.get("api_key"),
        access_token=token if "tokens" in data else None,
        transport=RuntimeTransport(),
    )
    try:
        await runtime_call(data["product"], config)
    except InflowApiError as error:
        if operation == "runtime.environment":
            return {"destinations": destinations}
        return {
            "code": error.code,
            "message": str(error),
            "http_status": error.http_status,
            "endpoint": error.endpoint,
            "request_id": error.request_id or "",
            "token_calls": token_calls,
            "sensitive_headers": [
                name
                for name in error.headers
                if name.lower() in ("authorization", "cookie", "set-cookie", "x-api-key")
            ],
        }
    raise RuntimeError("Expected the scripted runtime request to fail")


async def respond(request: Data) -> Data:
    envelope = {
        "adapter_version": "1",
        "sequence": request["sequence"],
        "case_id": request["case_id"],
    }
    try:
        if request["adapter_version"] != "1":
            raise RuntimeError("Unsupported adapter version")
        data, operation = request["input"], request["operation"]
        before = deepcopy(data)
        result: object
        observation: Data
        try:
            if operation.endswith(".buyer.payment-status"):

                async def token() -> str:
                    return str(data["access_token"])

                config = options(data)
                if "access_token" in data:
                    config = ClientOptions(base_url=config.base_url, access_token=token)
                buyer = (
                    BuyerMethod(config)
                    if operation.startswith("mpp.")
                    else await Buyer.create(config)
                )
                async with buyer:
                    snapshots = []
                    for _ in range(data.get("reads", 1)):
                        snapshot = await buyer.get_payment_status(
                            data["transaction_id"], retries=data.get("retries", 0)
                        )
                        snapshots.append(
                            {
                                key: snapshot[key]
                                for key in ("transactionId", "status", "nextAction")
                                if key in snapshot
                            }
                        )
                    result = snapshots
            elif operation.startswith("mpp."):
                result = await mpp_execute(operation, data)
            elif operation.startswith("x402."):
                result = await x402_execute(operation, data)
            elif operation.startswith("runtime."):
                result = await runtime_execute(operation, data)
            elif operation == "tap.seller.verify":
                if data.get("resolver") == "http":
                    options(data)
                result = await tap_execute(data)
            else:
                raise RuntimeError("Unknown operation")
            observation = {"result": result}
        except (Exception, asyncio.CancelledError) as error:
            observation = {"error": classify(error, operation, data)}
        if data != before:
            raise RuntimeError("Caller input was mutated")
        return {**envelope, **observation}
    except Exception as error:
        return {**envelope, "error": {"code": "ADAPTER_ERROR", "message": str(error)}}


async def main() -> None:
    for line in sys.stdin:
        request = json.loads(line)
        response = await respond(request)
        print(json.dumps(response, allow_nan=False), flush=True)


if __name__ == "__main__":
    asyncio.run(main())
