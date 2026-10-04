# Run the examples

The four payment programs connect to **InFlow Sandbox**. The Sellers run on your computer;
configuration, approvals, and payments use Sandbox accounts. They are not simulated
payments. Run the commands from the repository root with Python 3.11 or newer.

## Set up your accounts

These accounts are for payment examples. The separate [TAP example](#tap-agent-recognition)
verifies signed requests without an InFlow account or payment.

1. Register at [InFlow Sandbox](https://sandbox.inflowpay.ai). Accepting payments
   requires a **Seller** account and an API key from its dashboard. A Developer
   key cannot be used as a Seller key.
2. Use a separate account and key for the Buyer so the two sides are easy to follow.
   A Developer account can act as a Buyer; Seller accounts can also buy.
3. For a balance-funded request, have at least **0.01 USDC** in the Buyer's
   [Sandbox Balances](https://sandbox.inflowpay.ai/balances/). Use
   [Deposit](https://sandbox.inflowpay.ai/transactions/deposit/) to choose USDC and
   an available network. Use test assets on the network configured for Sandbox,
   not mainnet funds. Wait for the balance to be credited. If USDC or a deposit
   network is unavailable, resolve that account setup before paying. An API key
   does not provide funds or guarantee approval.
4. Install [uv](https://docs.astral.sh/uv/getting-started/installation/) and run:

   ```sh
   make sync
   ```

This installs the locked dependencies, including FastAPI, Uvicorn, HTTPX, and both
payment libraries. For an application outside this checkout, install
`inflowpay[mpp,fastapi]` or `inflowpay[x402,fastapi]` and `uvicorn` for a Seller;
Buyers need only the corresponding `inflowpay[mpp]` or `inflowpay[x402]` extra.

## MPP

Start the [Seller](mpp_seller.py) in one terminal. Generate a private challenge key
once; keep it separate from the InFlow API key:

```sh
export INFLOW_API_KEY='your-sandbox-seller-key'
export MPP_SECRET_KEY="$(uv run python -c 'import secrets; print(secrets.token_hex(32))')"
uv run --locked python -m examples.mpp_seller
```

The server listens at `http://127.0.0.1:3000`. Check both routes without paying:

```sh
curl -i http://127.0.0.1:3000/free
curl -i http://127.0.0.1:3000/api/widgets
```

`/free` returns HTTP 200 and `{"ok":true}`. `/api/widgets` returns HTTP 402 with
a `WWW-Authenticate: Payment ...` challenge for **0.01 USDC**.

In a second terminal, run the [Buyer](mpp_buyer.py):

```sh
export INFLOW_API_KEY='your-sandbox-buyer-key'
uv run --locked python -m examples.mpp_buyer
```

Keep the [Sandbox Approvals](https://sandbox.inflowpay.ai/approvals/) page open and
approve if requested. The Buyer prints HTTP 200, `{"widgets":[1,2,3]}`, and the
method and reference decoded from the Seller's `Payment-Receipt`.

The Buyer obtains a payment credential through InFlow and retries the resource
once. The Seller verifies and broadcasts the payment before its widgets handler
runs. This example uses one charge offer. MPP Seller subscriptions and composed
InFlow offers are not supported by this integration; see the
[upstream limitations](../README.md#seller-route-limitations).

## x402

Start the [Seller](x402_seller.py) in one terminal:

```sh
export INFLOW_API_KEY='your-sandbox-seller-key'
uv run --locked python -m examples.x402_seller
```

It listens at `http://127.0.0.1:3001` and builds **0.01 USDC** offers from the
Seller's configured balance and exact payment methods. It stops at startup if no
matching offers are available. Check the routes without paying:

```sh
curl -i http://127.0.0.1:3001/free
curl -i http://127.0.0.1:3001/api/widgets
```

`/free` returns HTTP 200. `/api/widgets` returns HTTP 402 with a `PAYMENT-REQUIRED`
header describing the available offers.

Run the [Buyer](x402_buyer.py) in a second terminal:

```sh
export INFLOW_API_KEY='your-sandbox-buyer-key'
uv run --locked python -m examples.x402_buyer
```

Approve in [Sandbox Approvals](https://sandbox.inflowpay.ai/approvals/) if requested.
Success prints HTTP 200, `{"widgets":[1,2,3]}`, and the success, network, and
transaction fields decoded from `PAYMENT-RESPONSE`.

The Buyer selects a supported offer, obtains its payment payload through InFlow,
and sends one paid request. The Seller verifies it, runs the widgets handler, and
settles before releasing the response. The handler only returns content: payment
middleware does not make database writes or other application side effects atomic
with settlement. No external wallet or retry-recovery hook is registered here.

## Settings and safe testing

| Variable          | Used by    | Meaning                                                     |
| ----------------- | ---------- | ----------------------------------------------------------- |
| `INFLOW_API_KEY`  | All        | Sandbox key; Sellers require a Seller account key            |
| `MPP_SECRET_KEY`  | MPP Seller | Private key for signing challenges                           |
| `TARGET_URL`      | Buyers     | Defaults to the matching local Seller's `/api/widgets`        |
| `INFLOW_BASE_URL` | All        | Optional platform override; leave unset to use Sandbox        |

The programs read exported variables, not `.env` files. Never commit real keys.
Leave `INFLOW_BASE_URL` unset unless intentionally testing a private deployment:
the API key is sent there. It is not sent to `TARGET_URL`.

Each Buyer allows up to fifteen minutes for its resource request and payment flow.
Press Ctrl-C to cancel. A pending approval is cancelled when the SDK has its
identifier; cancellation does not reverse a completed payment. A second 402, a
failed settlement receipt, or another HTTP error stops the program rather than
starting another payment. After a timeout or network error, check the account's
transactions before deciding whether to retry.

To exercise a free response, set `TARGET_URL` to the Seller's `/free` route.
The Buyer reports that no receipt was returned. A missing receipt alone does not
prove that no payment occurred; decoding a receipt also does not independently
verify settlement. These programs print neither payment credentials nor signatures.

The Sellers bind only to loopback and have no application login. Add your own
application authentication when required; paying is not a substitute for logging in.

## TAP agent recognition

The [TAP Seller](tap_seller.py) recognizes signed Agent requests independently of
payments. It needs no InFlow account, API key, or balance. From this checkout:

```sh
make sync
export PUBLIC_ORIGIN='http://127.0.0.1:3002'
uv run --locked python -m examples.tap_seller
curl -i http://127.0.0.1:3002/api/catalog
```

The unsigned request returns HTTP 401 with `{"error":"TAP verification failed"}`.
For HTTP 200, send a request signed by an Agent whose public key is available from
Visa's trusted key endpoint. That signature must cover the method, external
authority, encoded path and query, and, for POST bodies, the exact bytes' digest
and content type. The response contains verified Agent facts and a small catalog;
it does not grant access to a customer's account or charge them.

`PUBLIC_ORIGIN` is the origin the Agent signs. Behind a proxy, set it to the public
HTTPS origin, not the internal listening address. The example deliberately ignores
client-supplied forwarding headers. A proxy must preserve the signed path, query,
method, content type and body bytes. The example binds only to loopback, accepts
GET and POST, limits bodies to one mebibyte, and uses a process-local replay store.
Production multi-worker applications need a shared atomic replay store.

For a separate application, install `inflowpay[tap,fastapi]` and `uvicorn`. The TAP
SDK itself has no FastAPI requirement. Keep one `TapVerifier` open for the server's
lifetime, as `run()` does, then add your own account authorization and payment
checks inside the protected handler if needed.

The automated example tests create real Ed25519 signatures with synthetic keys
and exercise both the ASGI application and a loopback HTTP server. They require
neither a live Visa registration nor a payment. They do not establish that a
production proxy or registered Agent is configured correctly.

## Adapt the examples

- [Manual MPP waiting and cancellation](../README.md#waiting-errors-and-shutdown)
- [Displaying an x402 approval before waiting](../README.md#show-an-approval-before-waiting)
- [Metered x402 settlement](../README.md#prices-payment-methods-and-metering)
- [External wallets](../README.md#external-wallets) and
  [EIP-7702 sponsorship](../README.md#eip-7702-sponsorship)

`make verify` checks the example code's formatting, typing, line and branch coverage,
and Buyer-to-Seller flows through the actual payment middleware. Platform responses
in these tests are scripted; they do not establish live settlement. The commands
above are the walkthrough for checking your own Sandbox configuration.
