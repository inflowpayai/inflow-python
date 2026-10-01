import json
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI, Request
from mpp import Challenge, Credential, Receipt
from mpp.server.decorator import pay
from mpp.server.intent import broadcast_credential
from starlette.responses import JSONResponse

from inflowpay import ClientOptions
from inflowpay.mpp import WireObject, decode, encode
from inflowpay.mpp._wire import object_value, string
from inflowpay.mpp.seller import MppCredentialProblemError, MppSellerConfigurationError, Seller

# The fixture is the shared charge corpus; Seller subscriptions are outside the approved scope.
CASES = cast(
    list[WireObject],
    json.loads(Path(__file__).with_name("fixtures").joinpath("mpp-seller.json").read_text())[
        "cases"
    ],
)


async def route_binding(seller: Seller, data: WireObject) -> WireObject:
    terms = seller.charge_request(object_value(data["request"]))
    app = FastAPI()

    @app.get("/paid")
    @pay(
        intent=seller,
        request=lambda _: terms,
        method=seller.method,
        realm="seller.test",
        secret_key="local-test-secret",
    )
    async def paid(request: Request, credential: Credential, receipt: Receipt) -> JSONResponse:
        return JSONResponse({"paid": True})

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://seller.test"
    ) as client:
        response = await client.get("/paid")
        assert response.status_code == 402
        challenge = Challenge.from_www_authenticate(response.headers["www-authenticate"])
        credential = Credential(
            challenge=challenge.to_echo(),
            payload=object_value(data["credential_payload"]),
            source=string(data["source"]),
        )
        terms = seller.charge_request(object_value(data["replacement_request"]))
        response = await client.get(
            "/paid", headers={"Authorization": credential.to_authorization()}
        )
        return {"status": response.status_code}


@pytest.mark.parametrize("case", CASES, ids=[string(c["id"]) for c in CASES])
async def test_shared_seller(case: WireObject) -> None:
    data = object_value(case["input"])
    exchanges = object_value(case["platform"])["exchanges"]
    assert isinstance(exchanges, list)
    position = 0
    keys: dict[str, str] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal position
        assert position < len(exchanges), "Unexpected platform call"
        exchange = object_value(exchanges[position])
        position += 1
        expected = object_value(exchange["request"])
        assert request.method == expected["method"]
        assert request.url.path == expected["path"]
        for header, value in object_value(expected["headers"]).items():
            actual = request.headers.get(header)
            if isinstance(value, dict):
                assert actual
                if "capture" in value:
                    keys[string(value["capture"])] = actual
                else:
                    assert actual == keys[string(value["same"])]
            else:
                assert actual == value
        if "json" in expected:
            assert json.loads(request.content) == expected["json"]
        response = object_value(exchange["response"])
        return httpx.Response(int(str(response["status"])), json=response.get("json"))

    credential_wire = object_value(data.get("credential", {}))
    challenge = object_value(credential_wire.get("challenge", {}))
    method = string(data.get("method", challenge.get("method", "inflow")))
    async with await Seller.create(
        ClientOptions(api_key=string(data["api_key"]), transport=httpx.MockTransport(respond)),
        method=method,
    ) as seller:
        try:
            operation = case["operation"]
            if operation == "mpp.seller.prepare":
                result: object = seller.charge_request(object_value(data["request"]))
            elif operation == "mpp.seller.route-binding":
                result = await route_binding(seller, data)
            else:
                credential = Credential.from_authorization("Payment " + encode(credential_wire))
                request = object_value(decode(string(challenge["request"])))
                if operation == "mpp.seller.validate":
                    accepted = await seller.validate(credential, request)
                    result = {
                        "success": True,
                        "challenge": challenge,
                        "credential": credential_wire,
                        "details": accepted.details,
                        "method": accepted.method,
                        "intent": accepted.intent,
                        "request": accepted.request,
                        "source": accepted.source,
                    }
                else:
                    assert operation == "mpp.seller.verify"
                    receipt = await broadcast_credential(
                        intent=seller, credential=credential, request=request
                    )
                    result = decode(receipt.to_payment_receipt())
            actual: object = {"result": result}
        except MppCredentialProblemError as error:
            failure: WireObject = {"code": "payment-failed", "message": "Payment failed."}
            if data.get("include_problem", True):
                failure["details"] = {"problem": error.problem}
            actual = {"error": failure}
        except MppSellerConfigurationError:
            actual = {
                "error": {
                    "code": "unsupported-capability",
                    "message": "Unsupported payment capability.",
                }
            }
        assert actual == case["expect"]
    assert position == len(exchanges)
