import asyncio
import base64
import json
from dataclasses import replace
from importlib.util import find_spec

import httpx
import pytest
from fastapi import FastAPI
from fastapi import Request as HttpRequest
from mpp import Challenge, Credential, Receipt
from mpp.errors import InvalidChallengeError
from mpp.events import CHALLENGE_RECEIVED, CREDENTIAL_CREATED
from mpp.extensions.mcp.types import MCPReceipt
from mpp.runtime import CredentialParams
from mpp.server import Mpp, PaymentRequired, Request, ServerMethod
from mpp.server.decorator import pay
from solders.hash import Hash
from solders.keypair import Keypair
from solders.transaction import VersionedTransaction
from starlette.responses import JSONResponse
from x402.mechanisms.svm import KeypairSigner
from x402.mechanisms.svm.constants import TOKEN_PROGRAM_ADDRESS, USDC_MAINNET_ADDRESS
from x402.mechanisms.svm.exact import ExactSvmClientScheme
from x402.schemas import PaymentPayload, PaymentRequirements

from inflowpay import ClientOptions, InflowApiError
from inflowpay.mpp import decode, decode_credential, encode, to_pympp_challenge
from inflowpay.mpp._wire import object_value
from inflowpay.mpp.buyer import BuyerMethod, _PaymentEvents, payment_transport
from inflowpay.mpp.seller import Seller
from test_mpp_buyer import WIRE
from test_mpp_seller import CONFIG, ID, PROBLEM, RECEIPT, TERMS, Platform, options
from test_runtime_native import native_server
from test_x402_buyer import Platform as BuyerPlatform
from test_x402_buyer import buyer, required


@pytest.mark.parametrize(
    "context", [{"subscriptionId": ID}, {"instrumentId": ID}, False, "invalid"]
)
async def test_unsupported_context_cannot_start_payment(context: object) -> None:
    def unexpected(request: httpx.Request) -> httpx.Response:
        pytest.fail("Unsupported context reached the payment service")

    async with BuyerMethod(ClientOptions(transport=httpx.MockTransport(unexpected))) as method:
        with pytest.raises(ValueError, match="configure selectors"):
            await method.create_credential(
                CredentialParams(challenge=to_pympp_challenge(WIRE), context=context)
            )


@pytest.mark.parametrize("outcome", ["success", "validation-rejected", "pending", "handler-failed"])
async def test_joined_buyer_and_seller(outcome: str) -> None:
    # Only the remote payment service is simulated; both SDKs and pympp execute normally.
    events: list[str] = []
    issued: list[object] = []

    def platform(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        assert request.headers["x-api-key"] == "synthetic-key"
        if path.endswith("config"):
            return httpx.Response(200, json=CONFIG)
        body = json.loads(request.content)
        if path == "/v1/transactions/mpp":
            events.append("create")
            credential = {
                "challenge": body["challenge"],
                "payload": {"transactionId": ID},
                "source": "",
            }
            issued.append(credential)
            return httpx.Response(200, json={"state": "ready", "credential": encode(credential)})
        assert body["credential"] in issued
        wire = body["credential"]
        if path.endswith("validate"):
            events.append("validate")
            if outcome == "validation-rejected":
                return httpx.Response(200, json={"success": False, "problem": PROBLEM})
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "credential": wire,
                    "challenge": wire["challenge"],
                    "method": "inflow",
                    "intent": "charge",
                    "source": "",
                    "request": decode(wire["challenge"]["request"]),
                },
            )
        assert path.endswith("broadcast")
        events.append("broadcast")
        if outcome == "pending":
            return httpx.Response(200, json={"problem": {**PROBLEM, "status": 503}})
        return httpx.Response(200, json={"receipt": RECEIPT})

    def settings() -> ClientOptions:
        return ClientOptions(api_key="synthetic-key", transport=httpx.MockTransport(platform))

    async with await Seller.create(settings()) as seller, BuyerMethod(settings()) as method:
        app = FastAPI()

        @app.get("/paid")
        @pay(
            Mpp(methods=[seller], realm="seller.test", secret_key="synthetic-secret").charge(
                **seller.charge_request(TERMS)
            )
        )
        async def paid(
            request: HttpRequest, credential: Credential, receipt: Receipt
        ) -> JSONResponse:
            events.append("handler")
            return JSONResponse(
                {"paid": True}, status_code=500 if outcome == "handler-failed" else 200
            )

        async with httpx.AsyncClient(
            transport=payment_transport([method], inner=httpx.ASGITransport(app)),
            base_url="https://seller.test",
        ) as client:
            response = await client.get("/paid")
        assert events == ["create", "validate"] + (
            []
            if outcome == "validation-rejected"
            else ["broadcast"] + ([] if outcome == "pending" else ["handler"])
        )
        assert (
            response.status_code
            == {"success": 200, "validation-rejected": 402, "pending": 402, "handler-failed": 500}[
                outcome
            ]
        )
        if outcome in ("success", "handler-failed"):
            assert decode(response.headers["payment-receipt"]) == RECEIPT
        else:
            assert "payment-receipt" not in response.headers


@pytest.mark.parametrize("context", [None, {}])
async def test_http_preserves_credentials_concurrently(context: object) -> None:
    def platform(request: httpx.Request) -> httpx.Response:
        wire = json.loads(request.content)["challenge"]
        return httpx.Response(
            200,
            json={
                "state": "ready",
                "credential": encode(
                    {
                        "challenge": wire,
                        "payload": {"transactionId": wire["id"]},
                        "source": "",
                        "extension": {"id": wire["id"]},
                    }
                ),
            },
        )

    def resource(request: httpx.Request) -> httpx.Response:
        if "authorization" not in request.headers:
            challenge = to_pympp_challenge({**WIRE, "id": request.url.path})
            return httpx.Response(
                402, headers={"www-authenticate": challenge.to_www_authenticate(challenge.realm)}
            )
        wire = decode_credential(request.headers["authorization"][8:])
        assert wire["source"] == ""
        assert wire["extension"] == {"id": request.url.path}
        assert wire["payload"] == {"transactionId": request.url.path}
        return httpx.Response(200)

    async with BuyerMethod(ClientOptions(transport=httpx.MockTransport(platform))) as method:
        await method.create_credential(
            CredentialParams(challenge=to_pympp_challenge(WIRE), context=context)
        )
        async with httpx.AsyncClient(
            transport=payment_transport([method], inner=httpx.MockTransport(resource))
        ) as client:
            responses = await asyncio.gather(
                *(client.get(f"https://seller.test/{i}") for i in range(3))
            )
        assert [r.status_code for r in responses] == [200, 200, 200]


async def test_dispatcher_preserves_other_listeners_and_methods() -> None:
    events = _PaymentEvents()
    supplied = Credential(challenge=to_pympp_challenge(WIRE).to_echo(), payload={})
    assert await events.emit(CREDENTIAL_CREATED, {}) is None
    assert await events.emit(CHALLENGE_RECEIVED, {}, first_result=True) is None
    async with BuyerMethod(ClientOptions()) as method:
        assert await events.emit(CHALLENGE_RECEIVED, {"method": method}, first_result=True) is None
        events.on(CHALLENGE_RECEIVED, lambda _: supplied)
        assert (
            await events.emit(CHALLENGE_RECEIVED, {"method": method}, first_result=True) is supplied
        )


async def test_payment_failure_is_not_swallowed_or_retried() -> None:
    calls = 0

    def platform(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, json={"code": "DENIED", "message": "Denied"})

    async with BuyerMethod(ClientOptions(transport=httpx.MockTransport(platform))) as method:
        challenge = to_pympp_challenge(WIRE)

        def resource(request: httpx.Request) -> httpx.Response:
            assert "authorization" not in request.headers
            return httpx.Response(
                402, headers={"www-authenticate": challenge.to_www_authenticate(challenge.realm)}
            )

        async with httpx.AsyncClient(
            transport=payment_transport([method], inner=httpx.MockTransport(resource))
        ) as client:
            with pytest.raises(InflowApiError):
                await client.get("https://seller.test/paid")
    assert calls == 1


async def test_native_split_lifecycle_and_receipt_metadata() -> None:
    platform = Platform()
    async with await Seller.create(options(platform)) as seller:
        assert isinstance(seller, ServerMethod)
        assert (seller.name, seller.intent, seller.method.key) == (
            "inflow",
            "charge",
            "inflow/charge",
        )
        engine = Mpp(methods=[seller], realm="seller.example", secret_key="test-only-secret")
        terms = seller.charge_request(TERMS)
        http_request = Request("GET", "https://seller.example/paid")
        offered = await engine.charge(**terms, description="Test purchase")(http_request)
        assert isinstance(offered, PaymentRequired)
        challenge = Challenge.from_www_authenticate(offered.challenge.headers["www-authenticate"])
        assert challenge.request["amount"] == "0.50"
        credential = Credential(challenge=challenge.to_echo(), payload={"transactionId": ID})
        for _ in range(2):
            validation = await engine.validate_credential(
                credential, request=terms, http_request=http_request
            )
            assert validation.challenge.description == "Test purchase"
            assert validation.details == {"approved": True}
        assert [r.url.path for r in platform.requests] == [
            "/v1/mpp/config",
            "/v1/mpp/validate",
            "/v1/mpp/validate",
        ]
        before = len(platform.requests)
        for invalid in (replace(credential, challenge=replace(credential.challenge, id="wrong")),):
            with pytest.raises(InvalidChallengeError):
                await engine.broadcast_credential(invalid, request=terms, http_request=http_request)
        with pytest.raises(InvalidChallengeError):
            await engine.broadcast_credential(
                credential,
                request=terms,
                http_request=Request("GET", "https://seller.example/other"),
            )
        with pytest.raises(InvalidChallengeError):
            await engine.broadcast_credential(
                credential, request={**terms, "amount": "2"}, http_request=http_request
            )
        assert len(platform.requests) == before
        receipt_wire = {
            **RECEIPT,
            "subscriptionId": "subscription",
            "fundingCurrency": "USD",
            "extra": {"order": "one"},
            "extension": {"preserve": True},
        }
        platform.result = {"receipt": receipt_wire}
        receipt = await engine.broadcast_credential(
            credential, request=terms, http_request=http_request
        )
        assert decode(receipt.to_payment_receipt()) == receipt_wire
        assert [r.url.path for r in platform.requests[before:]] == [
            "/v1/mpp/validate",
            "/v1/mpp/broadcast",
        ]
        for request in platform.requests[1:]:
            assert (
                json.loads(request.content)["credential"]["challenge"]["description"]
                == "Test purchase"
            )
        converted = MCPReceipt.from_core(
            receipt, challenge.id, "inflow", settlement=object_value(RECEIPT["settlement"])
        )
        parsed = MCPReceipt.from_meta(converted.to_meta())
        assert parsed is not None
        assert parsed.external_id == "order"
        assert parsed.subscription_id == "subscription"
        assert parsed.funding_currency == "USD"
        assert parsed.extra == {"order": "one"}
        assert parsed.extensions is not None and parsed.extensions["extension"] == {
            "preserve": True
        }
        core = parsed.to_core()
        assert core.method == "inflow"
        assert core.external_id == receipt.external_id
        assert core.subscription_id == receipt.subscription_id
        assert core.funding_currency == receipt.funding_currency
        assert core.extra == receipt.extra
        assert core.extensions == {
            "custom": {"items": [1, True, None]},
            "extension": {"preserve": True},
        }
        assert parsed.settlement == RECEIPT["settlement"]


@pytest.mark.parametrize("version", [0, 1])
async def test_solana_signing_uses_httpx_without_blocking_the_loop(version: int) -> None:
    assert find_spec("solana") is None
    signer = KeypairSigner(Keypair.from_seed(bytes([1]) * 32))
    receiver = Keypair.from_seed(bytes([2]) * 32).pubkey()
    fee_payer = Keypair.from_seed(bytes([3]) * 32).pubkey()
    methods: list[str] = []

    async def rpc(
        path: str, headers: dict[str, str], body: bytes, writer: asyncio.StreamWriter
    ) -> None:
        call = json.loads(body)
        methods.append(call["method"])
        # This server runs on the Buyer's event loop. A blocking signer cannot get its reply.
        if call["method"] == "getAccountInfo":
            data = bytearray(82)
            data[44] = 6
            value = {
                "data": [base64.b64encode(data).decode(), "base64"],
                "owner": TOKEN_PROGRAM_ADDRESS,
                "lamports": 1,
                "executable": False,
                "rentEpoch": 0,
            }
        else:
            assert call["method"] == "getLatestBlockhash"
            value = {"blockhash": str(Hash.default()), "lastValidBlockHeight": 100}
        response = json.dumps(
            {"jsonrpc": "2.0", "id": call["id"], "result": {"context": {"slot": 1}, "value": value}}
        ).encode()
        writer.write(
            (
                f"HTTP/1.1 200 OK\r\nContent-Length: {len(response)}\r\nConnection: close\r\n\r\n"
            ).encode()
            + response
        )

    platform = BuyerPlatform()
    async with native_server(rpc) as url, await buyer(platform) as client:
        scheme = ExactSvmClientScheme(signer, rpc_url=url)
        network = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
        requirement = PaymentRequirements(
            scheme="exact",
            network=network,
            amount="500000",
            asset=USDC_MAINNET_ADDRESS,
            pay_to=str(receiver),
            max_timeout_seconds=60,
            extra={"feePayer": str(fee_payer), "transactionVersions": [version]},
        )
        client.register(network, scheme)
        try:
            payload = await asyncio.wait_for(
                client.create_payment_payload(required(requirement)), 5
            )
            assert isinstance(payload, PaymentPayload)
            transaction = VersionedTransaction.from_bytes(
                base64.b64decode(payload.payload["transaction"])
            )
            assert transaction.version() == version
            assert transaction.verify_with_results() == [False, True]
            assert payload.accepted == requirement
        finally:
            # Upstream owns its RPC cache but exposes no scheme-level close method.
            for connection in scheme._clients.values():
                connection.close()
    assert methods == ["getAccountInfo", "getLatestBlockhash"]
    assert len(platform.requests) == 1
