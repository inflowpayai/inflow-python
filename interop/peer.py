import asyncio
import json
import socket
import sys
from contextlib import AsyncExitStack
from typing import Any
from urllib.parse import urlsplit

import httpx
import uvicorn
from fastapi import FastAPI, Request
from mpp import Credential, Receipt
from mpp.server.decorator import pay
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import JSONResponse, Response
from x402 import x402ResourceServer
from x402.http import decode_payment_response_header
from x402.http.clients.httpx import x402AsyncTransport
from x402.http.middleware.fastapi import payment_middleware

from inflowpay import ClientOptions
from inflowpay.mpp import decode_receipt
from inflowpay.mpp.buyer import BuyerMethod, payment_transport
from inflowpay.mpp.seller import Seller as MppSeller
from inflowpay.x402.buyer import Buyer
from inflowpay.x402.facilitator import Facilitator
from inflowpay.x402.seller import Seller


# The harness passes JSON settings; production APIs retain their declared types.
async def run(settings: dict[str, Any]) -> None:
    for value in [
        settings["Platform"],
        *([settings["Target"]] if settings["Role"] == "buyer" else []),
    ]:
        url = urlsplit(value)
        if url.scheme != "http" or url.hostname != "127.0.0.1" or url.username or url.password:
            raise ValueError("Loopback endpoints required")
    role, protocol, variant = settings["Role"], settings["Protocol"], settings["Variant"]
    if role not in ("buyer", "seller") or protocol not in ("mpp", "x402"):
        raise ValueError("Unknown peer role or protocol")
    options = ClientOptions(base_url=settings["Platform"], api_key=f"test-only-{role}-key")
    async with AsyncExitStack() as stack:
        if role == "buyer":
            if protocol == "mpp":
                method = await stack.enter_async_context(
                    BuyerMethod(
                        options,
                        method="tempo" if variant == "tempo" else "inflow",
                        intent="subscription" if variant == "subscription" else "charge",
                        subscription_id=settings.get("SubscriptionID"),
                        poll_interval=0,
                        pending_timeout=5,
                    )
                )
                transport: httpx.AsyncBaseTransport = payment_transport([method])
            else:
                buyer = await stack.enter_async_context(
                    await Buyer.create(options, poll_interval=0, pending_timeout=5)
                )
                transport = x402AsyncTransport(buyer)
            http = await stack.enter_async_context(
                httpx.AsyncClient(transport=transport, follow_redirects=False)
            )
            async with asyncio.timeout(10):
                response = await http.get(
                    settings["Target"], headers={"X-App-Session": "test-only-session"}
                )
            receipt: object = None
            if raw := response.headers.get("Payment-Receipt"):
                receipt = decode_receipt(raw)
            elif raw := response.headers.get("PAYMENT-RESPONSE"):
                receipt = decode_payment_response_header(raw).model_dump(
                    by_alias=True, exclude_none=True
                )
            print(
                json.dumps(
                    {
                        "status": response.status_code,
                        "body": response.text,
                        "receipt": receipt,
                        "cache": response.headers.get("Cache-Control"),
                    }
                ),
                flush=True,
            )
            return

        app = FastAPI()
        http = await stack.enter_async_context(httpx.AsyncClient(follow_redirects=False))

        async def handle() -> None:
            evidence = await http.post(settings["Platform"] + "/handler")
            evidence.raise_for_status()

        if protocol == "mpp":
            seller = await stack.enter_async_context(
                await MppSeller.create(options, method="tempo" if variant == "tempo" else "inflow")
            )
            terms = seller.charge_request(
                {
                    "amount": "10000",
                    "currency": "0x20c0000000000000000000000000000000000000",
                    "recipient": "0x1111111111111111111111111111111111111111",
                }
                if variant == "tempo"
                else {"amount": "0.01", "currency": "USDC"}
            )

            @app.get("/paid")
            @pay(
                intent=seller,
                method=seller.method,
                request=terms,
                realm="interop",
                secret_key="test-only-binding-secret-at-least-32-bytes",
            )
            async def paid(
                request: Request, credential: Credential, receipt: Receipt
            ) -> JSONResponse:
                await handle()
                return JSONResponse(
                    {"paidResource": True},
                    status_code=settings["HandlerStatus"],
                    headers={"Payment-Receipt": receipt.to_payment_receipt()},
                )
        else:
            x_seller = await stack.enter_async_context(await Seller.create(options))
            facilitator = await stack.enter_async_context(await Facilitator.create(options))
            resource = x402ResourceServer(facilitator)
            for registration in await x_seller.scheme_registrations(schemes=[variant]):
                resource.register(registration["network"], registration["server"])
            route = await x_seller.route("0.01 USDC", schemes=[variant])
            app.middleware("http")(payment_middleware({"GET /paid": route}, resource))

            @app.get("/paid")
            async def x_paid() -> JSONResponse:
                await handle()
                return JSONResponse({"paidResource": True}, status_code=settings["HandlerStatus"])

        @app.middleware("http")
        async def credentials(request: Request, call_next: RequestResponseEndpoint) -> Response:
            if (
                request.headers.get("x-api-key")
                or request.headers.get("x-app-session") != "test-only-session"
            ):
                return JSONResponse({"error": "authentication boundary failure"}, status_code=500)
            return await call_next(request)

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
            task = asyncio.create_task(server.serve(sockets=[listener]))
            try:
                while not server.started:
                    if task.done():
                        await task
                        raise RuntimeError("Server did not start")
                    await asyncio.sleep(0.01)
                print(
                    json.dumps({"url": f"http://127.0.0.1:{listener.getsockname()[1]}/paid"}),
                    flush=True,
                )
                await task
            finally:
                server.should_exit = True
                await task


if __name__ == "__main__":
    asyncio.run(run(json.loads(sys.stdin.readline())))
