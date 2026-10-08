import asyncio
import os
import sys

import httpx
from x402.http import decode_payment_response_header
from x402.http.clients.httpx import x402AsyncTransport

from inflowpay import ClientOptions
from inflowpay.x402.buyer import Buyer


async def run() -> None:
    key = os.environ.get("INFLOW_API_KEY")
    if not key:
        raise ValueError("Set INFLOW_API_KEY to your Sandbox buyer API key.")
    target = os.environ.get("TARGET_URL", "http://127.0.0.1:3001/api/widgets")
    scheme = os.environ.get("X402_SCHEME", "default")
    if scheme not in ("default", "instrument"):
        raise ValueError("X402_SCHEME must be default or instrument")
    print("Requesting resource; approve in the Sandbox dashboard if requested.", flush=True)
    # No external wallet or recovery hook is registered: a failed paid retry stops here.
    async with (
        await Buyer.create(
            ClientOptions(
                environment="sandbox", api_key=key, base_url=os.environ.get("INFLOW_BASE_URL")
            ),
            prefer=("instrument",) if scheme == "instrument" else ("balance", "exact"),
            instrument_id=os.environ.get("INFLOW_INSTRUMENT_ID"),
        ) as buyer,
        httpx.AsyncClient(transport=x402AsyncTransport(buyer), follow_redirects=False) as http,
    ):
        async with asyncio.timeout(900):
            response = await http.get(target)
        print(f"HTTP {response.status_code}\n{response.text}")
        receipt = response.headers.get("payment-response") or response.headers.get(
            "x-payment-response"
        )
        if receipt is not None:
            settled = decode_payment_response_header(receipt)
            print(
                f"Seller settlement: success={settled.success} network={settled.network} "
                f"transaction={settled.transaction}"
            )
            if not settled.success:
                raise ValueError(f"Settlement failed: {settled.error_reason}")
        else:
            print("No seller receipt returned; this does not establish whether payment occurred.")
        response.raise_for_status()


def main() -> int:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("Cancelled. A completed payment is not reversed.", file=sys.stderr)
        return 130
    except Exception as error:
        print(
            f"Request failed: {error}. Do not automatically retry an uncertain payment.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
