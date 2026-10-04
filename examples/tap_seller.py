import asyncio
import os
import sys
from dataclasses import asdict

import httpx
import uvicorn
from fastapi import FastAPI, Request
from starlette.responses import JSONResponse

from inflowpay.tap.seller import TapRequest, TapVerificationError, TapVerificationFacts, TapVerifier


def create_app(verifier: TapVerifier, public_origin: str) -> FastAPI:
    origin = httpx.URL(public_origin)
    if (
        origin.scheme not in ("http", "https")
        or not origin.host
        or origin.userinfo
        or origin.raw_path != b"/"
        or origin.fragment
    ):
        raise ValueError(
            "PUBLIC_ORIGIN must be an absolute HTTP origin without a path or credentials"
        )
    app = FastAPI()

    @app.api_route("/api/catalog", methods=["GET", "POST"])
    async def catalog(request: Request) -> JSONResponse:
        # Preserve bytes, including an explicitly supplied empty body. Do not reserialize JSON.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 1024 * 1024:
                return JSONResponse({"error": "Request body too large"}, status_code=413)
        has_body = (
            "content-length" in request.headers
            or "transfer-encoding" in request.headers
            or bool(body)
        )
        # PUBLIC_ORIGIN is deployment configuration, never a client-supplied Forwarded header.
        path = request.scope["raw_path"].split(b"?", 1)[0].decode("ascii")
        query = request.scope["query_string"].decode("ascii")
        url = str(origin).rstrip("/") + path + ("?" + query if query else "")
        snapshot = TapRequest(
            method=request.method,
            url=url,
            headers=httpx.Headers(request.headers.raw),
            body=bytes(body) if has_body else None,
        )

        async def recognized(facts: TapVerificationFacts) -> JSONResponse:
            # Recognition is not customer authentication or payment. Add those checks separately.
            return JSONResponse({"agent": asdict(facts), "catalog": ["search", "contents"]})

        try:
            return await verifier.with_verified(snapshot, recognized)
        except TapVerificationError:
            return JSONResponse({"error": "TAP verification failed"}, status_code=401)

    return app


async def run() -> None:
    origin = os.environ.get("PUBLIC_ORIGIN")
    if not origin:
        raise ValueError("Set PUBLIC_ORIGIN to the externally visible origin of this server.")
    async with TapVerifier() as verifier:
        app = create_app(verifier, origin)
        print("TAP: http://127.0.0.1:3002/api/catalog requires a signed agent request.", flush=True)
        await uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=3002, proxy_headers=False)
        ).serve()


def main() -> int:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        print(f"TAP Seller failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
