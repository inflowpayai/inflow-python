import asyncio
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI
from x402 import x402ResourceServer
from x402.http.middleware.fastapi import payment_middleware
from x402.http.types import RouteConfig

from inflowpay import ClientOptions
from inflowpay.x402.facilitator import Facilitator
from inflowpay.x402.seller import Seller


@asynccontextmanager
async def application(
    options: ClientOptions, *, instrument: bool = False
) -> AsyncIterator[FastAPI]:
    # Each client owns its HTTP connection pool; keep both open while serving.
    async with (
        await Seller.create(options) as seller,
        await Facilitator.create(options) as facilitator,
    ):
        resource = x402ResourceServer(facilitator)
        for registration in await seller.scheme_registrations():
            resource.register(registration["network"], registration["server"])
        offers = await seller.offers(
            "$1.25" if instrument else "0.01 USDC",
            schemes=["instrument"] if instrument else ["balance", "exact"],
        )
        if not offers:
            raise ValueError("The Seller configuration has no matching payment offers.")
        app = FastAPI()
        app.middleware("http")(
            payment_middleware({"GET /api/widgets": RouteConfig(accepts=offers)}, resource)
        )

        @app.get("/api/widgets")
        async def widgets() -> dict[str, list[int]]:
            # Settlement follows this response; only return content, not irreversible side effects.
            return {"widgets": [1, 2, 3]}

        @app.get("/free")
        async def free() -> dict[str, bool]:
            return {"ok": True}

        yield app


async def run() -> None:
    key = os.environ.get("INFLOW_API_KEY")
    if not key:
        raise ValueError("Set INFLOW_API_KEY to your Sandbox Seller API key.")
    options = ClientOptions(
        environment="sandbox", api_key=key, base_url=os.environ.get("INFLOW_BASE_URL")
    )
    scheme = os.environ.get("X402_SCHEME", "default")
    if scheme not in ("default", "instrument"):
        raise ValueError("X402_SCHEME must be default or instrument")
    async with application(options, instrument=scheme == "instrument") as app:
        price = "1.25 USD via instrument" if scheme == "instrument" else "0.01 USDC"
        print(
            f"x402: http://127.0.0.1:3001/api/widgets costs {price}; /free requires no payment.",
            flush=True,
        )
        await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=3001)).serve()


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
