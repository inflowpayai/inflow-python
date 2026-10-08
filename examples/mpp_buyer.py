import asyncio
import os
import sys

import httpx

from inflowpay import ClientOptions
from inflowpay.mpp import WireObject, decode_receipt
from inflowpay.mpp.buyer import BuyerMethod, payment_transport


async def run() -> None:
    key = os.environ.get("INFLOW_API_KEY")
    if not key:
        raise ValueError("Set INFLOW_API_KEY to your Sandbox buyer API key.")
    target = os.environ.get("TARGET_URL", "http://127.0.0.1:3000/api/widgets")
    payment_method = os.environ.get("MPP_METHOD", "inflow")
    if payment_method not in ("inflow", "card"):
        raise ValueError(
            "MPP_METHOD must be inflow or card; this Buyer does not issue Stripe tokens."
        )
    merchant: WireObject | None = None
    if payment_method == "card":
        # Supply the actual merchant context; the SDK does not infer it from the target URL.
        merchant = {
            "name": os.environ.get("CARD_MERCHANT_NAME", ""),
            "url": os.environ.get("CARD_MERCHANT_URL", ""),
            "countryCode": os.environ.get("CARD_MERCHANT_COUNTRY", ""),
        }
    print("Requesting resource; approve in the Sandbox dashboard if requested.", flush=True)
    # The platform key belongs to BuyerMethod, never to the merchant HTTP client.
    async with (
        BuyerMethod(
            ClientOptions(
                environment="sandbox", api_key=key, base_url=os.environ.get("INFLOW_BASE_URL")
            ),
            method=payment_method,
            merchant=merchant,
            instrument_id=os.environ.get("INFLOW_INSTRUMENT_ID"),
        ) as method,
        httpx.AsyncClient(transport=payment_transport([method]), follow_redirects=False) as http,
    ):
        async with asyncio.timeout(900):
            response = await http.get(target)
        print(f"HTTP {response.status_code}\n{response.text}")
        receipt = response.headers.get("payment-receipt")
        if receipt is not None:
            decoded = decode_receipt(receipt)
            print(f"Seller receipt: {decoded['method']} reference={decoded['reference']}")
        else:
            print("No seller receipt returned; this does not establish whether payment occurred.")
        # A second 402 is an error, not permission to purchase again.
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
