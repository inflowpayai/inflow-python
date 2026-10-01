import asyncio
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI, Request
from mpp import Credential, Receipt
from mpp.server.decorator import pay
from starlette.responses import JSONResponse

from inflowpay import ClientOptions
from inflowpay.mpp.seller import Seller


@asynccontextmanager
async def application(options: ClientOptions, secret: str) -> AsyncIterator[FastAPI]:
    # Keep the Seller open for the whole server lifetime, not just startup.
    async with await Seller.create(options) as seller:
        app = FastAPI()

        @app.get("/api/widgets")
        @pay(
            intent=seller,
            method=seller.method,
            request=seller.charge_request({"amount": "0.01", "currency": "USDC"}),
            realm="localhost",
            secret_key=secret,
        )
        async def widgets(
            request: Request, credential: Credential, receipt: Receipt
        ) -> JSONResponse:
            # pympp verifies and broadcasts before reaching this handler.
            return JSONResponse(
                {"widgets": [1, 2, 3]}, headers={"Payment-Receipt": receipt.to_payment_receipt()}
            )

        @app.get("/free")
        async def free() -> dict[str, bool]:
            return {"ok": True}

        yield app


async def run() -> None:
    key, secret = os.environ.get("INFLOW_API_KEY"), os.environ.get("MPP_SECRET_KEY")
    if not key or not secret:
        raise ValueError(
            "Set INFLOW_API_KEY (Sandbox Seller key) and MPP_SECRET_KEY (private challenge key)."
        )
    options = ClientOptions(
        environment="sandbox", api_key=key, base_url=os.environ.get("INFLOW_BASE_URL")
    )
    async with application(options, secret) as app:
        print(
            "MPP: http://127.0.0.1:3000/api/widgets costs 0.01 USDC; /free requires no payment.",
            flush=True,
        )
        await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=3000)).serve()


def main() -> int:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"Seller failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
