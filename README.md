# InFlow Python SDK

Python integration for InFlow payments using the Machine Payments Protocol (MPP)
and x402. The Python distribution and import namespace are both `inflowpay`.
Python 3.11 or newer is required.

See the shared [SDK compatibility and support policy](https://github.com/inflowpayai/inflow-specs#sdk-compatibility-and-support)
for supported releases, dependency expectations, and security reporting.

Start with the [runnable Sandbox examples](examples/README.md) for MPP and x402
Buyers and Sellers, including account setup, commands, and expected results.

## Working with the repository

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then run:

```sh
make sync
make verify
```

`make sync` installs the exact development dependencies from `uv.lock`, including
optional integrations. `make verify` checks formatting, lint, strict typing, tests,
and complete line and branch coverage for each source file. It also builds the
source distribution and wheel, checks package metadata, and installs the wheel
outside the checkout to verify imports and the `py.typed` marker.
The consumer check exercises both the base installation and the combined optional
dependencies without issuing payments or contacting InFlow.

[Shared conformance checks](https://github.com/inflowpayai/inflow-python/blob/main/conformance/README.md) exercise the public SDK against
pinned InFlow contract fixtures and produce reports for Python 3.11–3.14.

Protocol and framework dependencies are optional. The `mpp` and `x402` extras
select payment libraries; `evm` and `svm` select x402 external-wallet dependencies;
`mcp` selects MCP dependencies for both protocols; `fastapi` selects the optional
web framework. The `tap` extra installs Ed25519 cryptography for agent recognition,
independently of payment libraries. The base package does not install those extras.

Runtime dependencies use compatible version ranges; `uv.lock` records the exact
versions used in development and CI. MCP dependencies use the 1.x line required by
pympp. No framework or blockchain package is imported by `import inflowpay`.

## TAP agent recognition

Visa Trusted Agent Protocol (TAP) lets a Seller recognize a request signed by a
trusted Agent key. It does **not** identify the customer, authorize access to their
account, approve a purchase, or prove payment. Keep application authentication,
AEP enrollment, and MPP or x402 payment checks separate.

```sh
pip install 'inflowpay[tap]'
```

The import is `inflowpay.tap.seller`. No InFlow account or API key is needed for
verification. The default resolver retrieves public keys from
`https://mcp.visa.com/.well-known/jwks`; a successful request needs a signature
created with a private key registered with that trusted source.

```python
from inflowpay.tap.seller import TapRequest, TapVerifier


async def verify_agent_request(verifier: TapVerifier, method, absolute_url, headers, body):
    return await verifier.verify(
        TapRequest(
            method=method,
            url=absolute_url,
            headers=headers,
            body=body,
        )
    )
```

Create the verifier at server startup and close it at shutdown with `aclose()`
or an application-lifetime `async with TapVerifier()` scope. Pass that verifier
to request handlers, as the [runnable TAP example](examples/README.md#tap-agent-recognition)
does. Creating one per request discards its key cache and process-local replay
history. `verify()` returns immutable `TapVerificationFacts`; alternatively,
`await verifier.with_verified(request, async_handler)` invokes your handler only
after signature verification and the replay claim succeed, returning its result.

Supply the method without changing its case, the absolute external URL with its
original encoded path and query, and the exact body bytes. `body=None` means no
body; `b""` is a supplied empty body and still requires signed `content-digest`
and `content-type` fields. Strings are encoded as UTF-8. Do not parse and
reserialize JSON before verification. Header names are case-insensitive. A mapping
can contain string values or lists of values; `httpx.Headers` is also accepted.
Duplicate values for required fields are rejected, not silently selected.
Obtain the external origin from trusted deployment configuration rather than
unvalidated `Forwarded` or `X-Forwarded-*` headers.

This implements InFlow's restricted Visa profile: one `sig2` Ed25519 signature
covering method, authority, path and query, plus body digest and content type when
a body is supplied. Signature lifetimes are at most eight minutes. Both
`ed25519` and `Ed25519` are accepted; facts report `ed25519`. Intent is `browse`
or `pay`, reflecting the signer's tag, not proof of payment. Repeated signature
parameters use their last value while retaining their first position, as required
by Structured Fields; duplicate covered components remain invalid.

### Keys, replay storage and cleanup

`VisaTapKeyResolver` accepts `url`, `cache_ttl` (3600 seconds), `cache_max_age`
(86400 seconds), `timeout` (3 seconds), `clock` and an optional HTTPX `transport`.
It shares concurrent refreshes, replaces the whole key set on success, and
remembers missing identifiers within that cache generation. A failed retrieval
can use a previously trusted matching key within `cache_max_age`; it cannot
introduce an unknown key or restore one removed by a successful refresh.
Redirects are not followed. The resolver owns its transport and closes it with
`aclose()`; do not share that transport with other clients.

For another trusted key source, pass `key_resolver` to `TapVerifier`. Implement
the public `TapKeyResolver` protocol's async `resolve(keyid, algorithm)` method,
returning a trusted `cryptography` `Ed25519PublicKey` or `None`. The request's
untrusted key identifier must not choose a network destination. The verifier
still checks the signature. An explicitly supplied resolver remains application-owned;
the verifier closes only the default resolver it creates itself.

`MemoryTapReplayStore` atomically claims a `(keyid, nonce)` pair until expiration,
but only within one process. For multiple workers or servers, supply a shared
`TapReplayStore` with an async, atomic `claim(keyid, nonce, expires)` operation.
Return `False` for a retained duplicate; let storage failures propagate. Invalid
signatures never consume a claim, and store failures never invoke the handler.
Nonce replay protection is not payment idempotency.

Python clocks return Unix seconds (`time.time` by default), rather than Node's
milliseconds. Validity is checked when verification starts, not again after
key retrieval or replay storage. Cancelling a caller stops its wait without
cancelling a shared key refresh needed by other requests. Closing the resolver
cancels and drains that refresh. Use a resolver on one event loop, and keep it
open until requests have finished.

`TapVerificationError.code` distinguishes malformed input, digest mismatch,
invalid lifetime, not-yet-valid or expired signatures, missing or unavailable
keys, invalid signatures, and replayed nonces. Exceptions from a custom resolver,
store, or handler propagate unchanged. Your application chooses the HTTP response;
the example returns a generic 401 for verification failures without exposing key
service details. TAP does not enable or modify any payment route automatically.

## Client configuration and lifetime

`ClientOptions` selects `production` (the default, `https://api.inflowpay.ai`) or
`sandbox` (`https://sandbox.inflowpay.ai`). `base_url` overrides that destination.
Use `api_key` for API-key authentication or an asynchronous `access_token` callback
for Bearer authentication, not both. Omit both for anonymous requests. The callback
runs for each HTTP attempt, allowing your application to refresh a token; its errors
propagate without being retried. It must support concurrent calls and cancellation.

The shared runtime owns its HTTPX client. It also owns any `AsyncBaseTransport`
supplied through `ClientOptions.transport`: closing the client closes that transport.
Give each client its own transport rather than sharing one between clients. Use
`async with` to close an SDK client when leaving its scope, or explicitly await
`aclose()` after all operations finish. An existing application-owned HTTPX client
cannot be supplied. Custom transports must honor task cancellation and perform one
request without following redirects or retrying it themselves.

The request timeout is 30 seconds by default and covers sending the HTTP request
and reading its response. Like the Node SDK, token retrieval occurs before this
request timeout; your token provider controls its own timeout. Cancelling the caller's
task interrupts token retrieval, requests, retry waits, and polling.

### Retries, polling, and approval cancellation

The shared transport performs one attempt unless the protocol operation explicitly
permits retries. Permitted retries are capped at three additional attempts, for
network failures, timeouts, and HTTP 429, 502, 503, or 504. They use exponential delays
starting at 200 milliseconds, with jitter. Redirects are returned as errors; request
credentials are not forwarded to another destination, and response cookies are not
sent on later requests.

Polling has a separate overall deadline. The protocol flow decides which states
are terminal and which failures permit another poll. An approved request is not,
by itself, evidence of a completed payment.

Approval cleanup performs one cancellation request with a five-second limit. It
waits independently of the cancelled payment operation, so cancelling that operation
does not immediately abort its cleanup request. Cleanup failures do not replace the
original payment failure. This deliberately differs from Node's fire-and-forget
cleanup: Python waits for completion or the limit before returning, matching the
Go SDK's bounded cleanup. No background cancellation queue is created. If the process
is forcibly terminated, delivery cannot be guaranteed. Cancelling an approval does
not reverse a settled payment or issue a refund.

`InflowApiError` exposes `code`, `http_status`, `endpoint`, `request_id`, `body`, and
response `headers`. Server error messages are preserved. Credential fields and the
credential used for the failed attempt are redacted from error responses; sensitive
response headers are omitted. Transport errors use `TIMEOUT` or `NETWORK_ERROR` with
`http_status=0`. Python task cancellation remains `asyncio.CancelledError`.

## MPP Buyer

Install `inflowpay[mpp]`. The Buyer obtains payment credentials from InFlow; it does
not sign transfers locally. Use an InFlow API key or Bearer-token provider belonging
to the paying account. The platform determines which payments that account may make.

```python
import os

import httpx

from inflowpay import ClientOptions
from inflowpay.mpp.buyer import BuyerMethod, payment_transport


async def buy(url: str) -> bytes:
    async with BuyerMethod(
        ClientOptions(environment="sandbox", api_key=os.environ["INFLOW_API_KEY"])
    ) as method:
        async with httpx.AsyncClient(transport=payment_transport([method])) as http:
            response = await http.get(url)
            response.raise_for_status()
            return response.content
```

The first request reaches the seller without a payment credential. If the seller
returns a compatible MPP challenge, the method asks InFlow to create the payment,
waits for approval when required, and returns the platform's credential. pympp then
sends that credential to the seller. Your InFlow API key goes only to InFlow, not
to the seller. Keep redirects disabled on the HTTPX client, its default setting.

### Select a payment method

`BuyerMethod` implements pympp's asynchronous method interface. Pass it to
`payment_transport` for HTTP requests or to pympp's `PaymentRuntime` for an existing
integration. You can also call `await method.create_credential(challenge)` with a
pympp `Challenge`; the result is a pympp `Credential` whose `to_authorization()`
retains the platform's payload, payer source, and additional wire fields.

| Constructor settings | Behavior |
| --- | --- |
| Defaults: `method="inflow", intent="charge"` | Pay an InFlow charge using the rail advertised by the seller. |
| `instrument_id="..."` | Select the funding instrument for an InFlow instrument-rail charge. |
| `method="tempo"` | Ask InFlow to produce a Tempo charge credential. No local wallet is required. |
| `method="card", merchant={...}` | Obtain an encrypted Visa CARD credential using the primary linked instrument, or the supplied `instrument_id`. |
| `intent="subscription"` | Purchase a subscription through the create-and-approve flow. |
| `intent="subscription", subscription_id="..."` | Authorize access using that existing subscription and the current seller challenge; do not purchase another subscription. |

Instrument and subscription identifiers are UUID strings. They are fixed when the
method is constructed. Unlike Node's per-call context, pympp passes only a challenge
to a method. Use separate method instances for different selections; do not change
one instance's settings between concurrent requests. Do not register several methods
with the same method/intent expecting pympp to choose a funding instrument: pympp
selects the first matching method. Select the intended instance in your application.

### Pay with a linked Visa card

Use `method="card"` for a merchant advertising CARD charge, not for an InFlow
instrument-rail challenge or a Stripe Shared Payment Token challenge:

```python
async with BuyerMethod(
    ClientOptions(environment="sandbox", api_key=os.environ["INFLOW_API_KEY"]),
    method="card",
    merchant={
        "name": "Example Store",
        "url": "https://store.example",
        "countryCode": "US",
    },
) as method:
    async with httpx.AsyncClient(transport=payment_transport([method])) as http:
        response = await http.get("https://store.example/report")
        response.raise_for_status()
```

The merchant name must be nonblank and at most 200 characters, the URL must be
absolute HTTP or HTTPS and at most 2048 characters, and `countryCode` must contain
two letters. InFlow checks the country, merchant, and card eligibility. The selected
card must have an enabled, unexpired USD allowance sufficient for the purchase.
Omit `instrument_id` to use the account's primary instrument; supply a hyphenated
UUID to select another linked card. The SDK does not create or infer an allowance.

Merchant settings are copied when the method is constructed. Use a separate instance
for each merchant context, because pympp does not pass Node's per-call context to
credential creation. That context does not replace the seller's signed challenge.

InFlow issues the encrypted credential; this SDK does not access card numbers or
decrypt its payload. The buyer rejects a returned CARD credential whose challenge
differs from the requested one, or whose payload has an invalid structure. Valid
billing fields and extensions are preserved. A ready credential is not a settlement
receipt: the seller still needs to accept and process it. An uncertain response is
not permission to create a second purchase.

### Waiting, errors, and shutdown

`poll_interval` defaults to five seconds; the platform's `retryAfterSeconds` takes
precedence. `pending_timeout` defaults to 900 seconds and starts after creation
returns. Both settings use seconds and permit zero. A zero pending timeout accepts
an immediately ready response but does not wait for a pending payment. The pending
budget includes both waits and polling requests. Creation and subscription
authorization use the separate request timeout from `ClientOptions`.

Transaction creation, polling, and subscription authorization each make one HTTP
attempt; a lost creation response is not proof that no payment was initiated.
The method raises `MppPaymentFailedError` with the platform's `problem`,
`MppPaymentExpiredError` with `transaction_id`, `MppPaymentTimeoutError` with
`transaction_id` and `timeout`, or `MppMalformedCredentialError` for unusable
responses. HTTP failures remain `InflowApiError`; application token-provider
exceptions propagate. Do not automatically restart a payment after a failure.

Cancel the caller's task to stop one operation, or `await method.cleanup()` to stop
all active operations on that instance. Cancellation remains `asyncio.CancelledError`.
The instance remains reusable after cleanup. `aclose()` stops active operations and
closes the owned platform transport; the method cannot be reused after closing.
The seller HTTP transport has its own lifetime and does not close your Buyer methods.
The nested context managers above close both in the correct order.

When a failed or cancelled purchase has returned an approval identifier, the method
attempts bounded approval cancellation as described above. If creation is interrupted
before that identifier arrives, it cannot cancel an unknown approval; server expiry
is the backstop. Cancelling subscription authorization never cancels the subscription.
`await method.cancel_approval(approval_id)` also exposes bounded, best-effort approval
cancellation when your application already knows the identifier.

### Automatic HTTP payment attempts

`payment_transport` configures pympp with `max_payment_retries=1`: one initial
request, at most one credential creation, and one paid retry. A further HTTP 402 is
returned to your application. Inspect that response instead of blindly issuing
another payment. Approval polling is separate and is not limited to one poll.

pympp defaults to three paid retries and creates a credential on each retry. Node's
mppx transport can reuse a credential for the same unresolved challenge; this Python
helper does not add such a cache. Applications using pympp's transport directly must
set their retry policy explicitly. The InFlow helper's limit does not affect other
transports that you construct.

If the paid retry loses its response or is cancelled, pympp raises
`PaymentOutcomeUnknownError`: the seller may already have received the credential.
That is different from cancelling an approval while waiting for InFlow. Retain the
exception's credential and request information for reconciliation; do not treat the
unknown outcome as permission to pay again.

### MCP tools

Install `inflowpay[mpp,mcp]` and pass the same Buyer method to pympp's
`McpClient`. It wraps an initialized MCP session; your application owns that
session and its connection.

```python
from mpp.extensions.mcp import McpClient

# Inside the Buyer method and initialized session's lifetimes:
client = McpClient(session, methods=[method])
result = await client.call_tool("premium_tool", {"query": "example"})
```

pympp handles the payment-required tool error, obtains a credential from the
Buyer method, and retries the tool once with payment metadata. InFlow charge,
Tempo charge, and InFlow subscription methods use the same platform flow as HTTP.
The subscription method's configured `subscription_id` determines whether it
purchases a subscription or authorizes an existing one. The HTTP helper's retry
setting does not configure MCP; pympp's MCP wrapper performs its own single retry.
See the MCP receipt limitation below before relying on receipt metadata.

## MPP Seller

Install `inflowpay[mpp,fastapi]` for a FastAPI service, or `inflowpay[mpp]` for the
framework-independent payment hooks. Create a **Seller** account and an API key
in the [Sandbox dashboard](https://sandbox.inflowpay.ai) for testing or the
[production dashboard](https://app.inflowpay.ai) for live payments. A Developer
account key does not authorize Seller configuration, validation, or broadcast.

`await Seller.create(options)` loads Seller configuration before returning. A
failed or cancelled setup closes its HTTP transport; retry setup with fresh
options/transport as needed. Successful configuration stays fixed for that Seller
instance. Unlike Node's asynchronous request hook, pympp's request transformation
is synchronous, so configuration is loaded during application startup rather than
inside a route. There is no background initialization or periodic refresh.

Use pympp's standalone `pay` decorator with the Seller as its charge intent:

```python
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from mpp import Credential, Receipt
from mpp.server.decorator import pay
from starlette.responses import JSONResponse

from inflowpay import ClientOptions
from inflowpay.mpp.seller import Seller


@asynccontextmanager
async def application():
    async with await Seller.create(
        ClientOptions(environment="sandbox", api_key=os.environ["INFLOW_API_KEY"])
    ) as seller:
        app = FastAPI()

        @app.get("/report")
        @pay(
            intent=seller,
            method=seller.method,
            request=seller.charge_request({"amount": "0.50", "currency": "USDC"}),
            realm="api.example.com",
            secret_key=os.environ["MPP_SECRET_KEY"],
        )
        async def report(
            request: Request, credential: Credential, receipt: Receipt
        ) -> JSONResponse:
            return JSONResponse(
                {"report": "Paid content"},
                headers={"Payment-Receipt": receipt.to_payment_receipt()},
            )

        yield app
```

Run your ASGI server inside `async with application() as app` so the Seller stays
open until the server finishes serving requests. Keep `MPP_SECRET_KEY` stable and
private across replicas serving the same challenges. It authenticates challenges
locally and is separate from the InFlow API key. Set `requires_auth=True` on `pay`
when application authentication uses `Authorization`; pympp then uses
`Payment-Authorization` for the payment credential. The decorator does not implement
your application's authentication or access-control policy.

### Prices and payment verification

For `method="inflow"`, `charge_request` preserves the decimal price, sets the
recipient from authenticated Seller configuration, and selects an advertised rail
for the currency. When several rails are advertised, provide `methodDetails.rail`.
If the selected rail requires an instrument, provide `methodDetails.instrumentId`.
An unsupported currency, ambiguous selection, or missing required instrument raises
`MppSellerConfigurationError`; the SDK does not invent a payment option.

For `method="tempo"`, supply the token `currency` address, recipient address, and
integer base-unit `amount`. For example, `"500000"` is half a token with six decimal
places. `methodDetails` defaults to `feePayer=False` and `supportedModes=["pull"]`;
explicit request values override those defaults. Do not supply `decimals` to
`charge_request`: it expects the payment method's wire amount, not a conversion hint.
Request preparation copies your data rather than modifying it.

pympp authenticates the challenge and checks the route's request before invoking
the Seller hooks. `validate` checks the credential with InFlow without settling it;
`broadcast` performs the terminal operation. Do not call these hooks with an
unverified credential instead of using pympp's verification entry point. Successful
validation alone does not authorize delivery of paid content.

Configuration, validation, and broadcast use the shared transient retry policy.
When the platform advertises idempotency support, a broadcast call generates one
key and reuses it across its HTTP retries; separate calls have separate keys.
The platform owns payment replay protection. Do not retry an entire payment flow
merely because its response was lost. Cancellation stops an in-flight request but
does not reverse a payment that reached the platform.

Payment rejections raise `MppCredentialProblemError`, retaining the platform
`problem`; pympp renders it as a payment error response. Invalid lifecycle responses
produce a fixed verification-failed problem. HTTP failures remain `InflowApiError`.
Receipts preserve the platform's timestamp precision and additional wire fields.
The endpoint must explicitly attach the `Payment-Receipt` header, as above.

Seller subscriptions and multiple InFlow offers on one endpoint are excluded for
the upstream reasons documented below. Buyer subscription support is independent.

## Upstream MPP compatibility

### Wire values and request validation

Install `inflowpay[mpp]` to use `inflowpay.mpp`. Its `WireObject` is a JSON dictionary
using the protocol's field names, such as `methodDetails` and `subscriptionId`.
Amounts are decimal strings, not floating-point values.

```python
from inflowpay.mpp import encode, parse_challenge_headers, validate_request

request = validate_request("inflow", "charge", {"amount": "1.50", "currency": "USD"})
encoded_request = encode(request)
# Pass one WWW-Authenticate value or a list of repeated values.
challenges = parse_challenge_headers(
    'Payment id="example", realm="seller.example", method="inflow", '
    f'intent="charge", request="{encoded_request}"'
)
```

`validate_request` checks InFlow charge/subscription, Tempo charge, and CARD charge
request shapes; `validate_payload` checks InFlow, Tempo, or CARD credential payload
shapes. Both return a deep copy. These checks do not establish account permissions,
signature validity, or settlement. Those require the payment workflow and platform.

`decode_credential` and `decode_receipt` retain the complete decoded JSON object,
including extension fields. `decode` also accepts other JSON values. Malformed wire
values raise `MppCodecError`. Challenge header parsing supports combined and repeated
Payment challenges, quoted commas and escapes, and rejects duplicate parameters and
control characters. Unknown header parameters are ignored, matching the Node SDK.

`canonicalize` and `encode` omit null object members but retain null array elements,
using RFC 8785 JSON ordering and number formatting. Monetary strings are unchanged.
The canonical JSON encoder rejects nonfinite numbers and integers outside the JSON
safe-integer range; use strings for exact large values. Decoding does not canonicalize.

Use `to_pympp_challenge` to pass an InFlow wire challenge to pympp and
`from_pympp_challenge` to read it back. The conversion preserves the original encoded
request and opaque bytes, and retains extension fields when converting back from the
returned object. It does not change the caller's dictionary. Keep the returned object
intact: rebuilding it as a plain pympp `Challenge` discards the retained extensions.
For a challenge created directly by pympp, only information present in that object
can be recovered. Receipt and credential decoding use InFlow wire dictionaries
instead of pympp's fixed-field models, which can discard fields.

These are codecs and shape checks, not proof of payment. pympp owns challenge
authentication and transport; the InFlow platform owns payment verification and settlement.

### Stripe Shared Payment Token acceptance

Create a Seller with `await Seller.create(options, method="stripe")`, using an
InFlow Seller API key. The Seller must have a verified Stripe business profile
advertised by `/v1/mpp/config`; setup fails if Stripe charge is unavailable.
The SDK reads the network profile and allowed payment methods from that response.
The application does not need a Stripe secret key.

Use `seller.stripe_request({"amount": "1.25"})` to prepare USD 1.25. This method
accepts dollar strings from `"0.50"` through `"999999.99"` and returns integer
cents in the challenge. Extra fractional digits are rejected, not rounded.
Pass its result to the standalone `mpp.server.pay` decorator with `method="stripe"`
and `intent=seller`. Do not pass dollar prices directly to pympp's high-level
route helpers. Currency, precision, network profile, and payment-method options
cannot override the configured USD Stripe offer.

Optional `externalId` identifies the purchase; a credential must repeat it exactly
when supplied, including an empty string. Optional `metadata` is a dictionary of
up to 45 string entries, with nonblank keys up to 40 characters and values up to
500. Brackets and the keys `externalId`, `inflowMppTransactionId`, `mppChallengeId`,
`mppIntent`, `mppMethod`, and `stripeNetworkProfile` are reserved.

An external Buyer supplies the Shared Payment Token. The InFlow Buyer SDK does
not create Stripe tokens. The Seller forwards the credential to InFlow for
validation and then settlement; failed or pending settlement does not release
the resource. A successful receipt must identify the Stripe method and the
submitted challenge. [Run the Stripe Seller example](examples/README.md#stripe-seller).

### CARD acceptance

Create a Seller with `await Seller.create(options, method="card")`, using an
InFlow Seller API key. Configuration supplies the recipient, merchant name, Visa
network, and RSA public encryption key. Setup fails when that profile is unavailable
or incomplete; the application cannot override these fields when pricing a route.

Use `seller.card_request({"amount": "1.25"})` to prepare USD 1.25, then pass the result
to the standalone `pay` decorator with `method="card"` and `intent=seller`. Amounts
are decimal dollar strings from `"0.50"` through `"999999.99"`, converted exactly to
integer cents. Optional `billingRequired` is a boolean; an omitted value remains
omitted. Optional `externalId` allows up to 255 characters, including an empty string.

The SDK checks the encrypted credential's structure without decrypting it, forwards
it to InFlow for validation and settlement, and requires a successful receipt for
the same CARD challenge before delivery. pympp checks the signed challenge and route
terms first. The description-preservation limitation below applies to this seller
flow, even when a buyer preserves the full challenge.

### Challenge description preservation

pympp 0.11.0 drops the optional, top-level `challenge.description` when converting
a challenge to a credential echo or parsing and serializing a credential. This
affects the pympp-backed payment flow, including CARD. A Seller forwarding a
parsed credential therefore sends it without that description, even when the
Buyer supplied one. The encoded request, payment amount, and encrypted payload
are not changed by this omission. A `description` inside the request is a
different field and is not the field lost here.

Applications that require an exact copy of the complete challenge cannot rely on
these conversions when `challenge.description` is present. Challenges without
that optional field avoid this particular limitation. Do not treat a missing
description as evidence that a payment failed or submit a replacement payment
because of it.

[Upstream issue #272](https://github.com/tempoxyz/pympp/issues/272) tracks this
credential-format defect against the MPP draft and mppx. The Seller does
not replace pympp's credential parser or reconstruct a missing description. InFlow's
Buyer retains the original challenge fields when producing its outgoing credential;
that does not prevent a receiving pympp Seller from dropping the description.

Shared conformance executes the description-preservation case and reports its
failure. CI permits that specific pympp 0.11.0 failure only when a companion check
confirms that the same operation succeeds with just the outbound description
omitted and the platform echoing that received credential. Other failures remain
blocking. An upstream version change or an unexpected pass requires reviewing or
removing the allowance.

### Seller route limitations

InFlow charges use decimal amounts: `"0.50"` means half a unit of the specified
currency. pympp 0.11.0's high-level `Mpp.pay` and `Mpp.compose` helpers convert
prices to integer token units instead. Do not pass InFlow decimal prices through
those helpers. The standalone `mpp.server.pay` decorator accepts a complete
request dictionary and preserves its amount when no `decimals` field is supplied.
The endpoint handler must attach the returned receipt to its response using
`receipt.to_payment_receipt()` as the `Payment-Receipt` header value.

Multiple InFlow offers on one endpoint and Seller subscription routes are not
supported by this integration. Upstream composition does not accept the standalone
decorated handlers, and its fixed route options do not expose subscription terms
such as `periodUnit`, `periodCount`, and `subscriptionExpires`. These are separate
limitations; neither changes the Buyer subscription support described above.

[Upstream issue #268](https://github.com/tempoxyz/pympp/issues/268) tracks parity
with mppx's method-specific amount handling.
[Upstream issue #269](https://github.com/tempoxyz/pympp/issues/269) separately tracks
subscription request fields in Seller routes and composition.
The integration does not reverse upstream's token conversion or implement a separate
offer-selection system to bypass these limitations.

### MCP payment receipts

[pympp](https://github.com/tempoxyz/pympp) provides Python support for the Machine
Payments Protocol (MPP), including payments for Model Context Protocol (MCP) tools.
Its `MCPReceipt` conversion in version 0.11.0 drops `externalId`, `subscriptionId`,
and `extra` from a core payment receipt. Its MCP receipt parser also discards these
fields and additional top-level fields supplied by a server.

For an integrator, this means a successful MCP tool payment can return a receipt
without the subscription or external reference needed to associate it with an
application record. Missing receipt metadata is not evidence that the payment
failed; do not repeat a payment to recover these identifiers. The MCP conversion
does not affect HTTP `Payment-Receipt` serialization.

[Upstream PR #267](https://github.com/tempoxyz/pympp/pull/267) preserves these
fields and additional receipt extensions through MCP serialization, parsing, and
core conversions. It also preserves the payment method when converting an MCP
receipt back to a core receipt. Until that fix is included in the version of
`pympp` you install, do not rely on its MCP receipt objects to retain those fields.

## x402 models and payment identifiers

Install `inflowpay[x402]` to use `inflowpay.x402`. Its `PaymentRequirements`,
`PaymentRequired`, `PaymentPayload`, verification, settlement, and capability
models are the actual classes from the upstream Python x402 SDK, not alternative
models that need converting before passing them to upstream code. They accept
InFlow's `balance` scheme and `inflow:1` network as well as blockchain schemes
and networks. Model availability does not imply that every scheme is supported
by a particular buyer or facilitator.

For example, declare an optional payment identifier on a payment request and
construct the matching entry for its payment payload:

```python
from inflowpay.x402 import (
    PAYMENT_IDENTIFIER,
    declare_payment_identifier,
    generate_payment_id,
    payment_identifier_entry,
)

declaration = declare_payment_identifier()
request_extensions = {PAYMENT_IDENTIFIER: declaration}
payment_id = generate_payment_id()
entry = payment_identifier_entry(declaration, payment_id)
assert entry is not None
payload_extensions = {PAYMENT_IDENTIFIER: entry}
```

The identifier is stored in `extensions["payment-identifier"]["info"]["id"]`.
It must contain 16–128 ASCII letters, digits, underscores, or hyphens.
`generate_payment_id()` uses a `pay_` prefix and 16 random bytes encoded as
32 hexadecimal characters. Generate one identifier for a payment and reuse it
when retrying that same payment; do not reuse it for a different payment.

`read_payment_identifier()` returns `None` for an invalid declaration.
`payment_identifier_entry()` returns `None` for an invalid declaration or identifier.
Both preserve extra fields in the declaration's `info` and `schema` and return
independent copies. The declaration uses `required: false`; supplying an
identifier remains useful for identifying retries even when it is optional.

Amounts in these models are strings in the payment method's smallest units.
InFlow balance amounts use 18 decimal places; blockchain assets use their own
decimal scale. `normalize_decimal_string()` removes insignificant zeros without
floating-point conversion, rounding, or unit conversion. It is not a price
validator: strings outside plain decimal notation are returned unchanged.

Use `model_dump(by_alias=True, exclude_none=True)` when producing JSON-compatible
wire dictionaries from upstream models. Upstream fills omitted requirement
`extra` with `{}`; additional values inside `extra`, `payload`, and `extensions`
are preserved. These typed models do not preserve arbitrary unknown top-level
fields. Parsing a model or creating an identifier does not verify or settle a payment.

## x402 Buyer

Install `inflowpay[x402]` to pay with an InFlow account. `Buyer.create()` loads
the account's supported payment methods before returning. Supply your buyer API
key or access-token provider through `ClientOptions`; those credentials are sent
to InFlow, not to the merchant.

```python
import asyncio
import os

import httpx
from x402.http.clients.httpx import x402AsyncTransport

from inflowpay import ClientOptions
from inflowpay.x402.buyer import Buyer


async def main():
    async with (
        await Buyer.create(
            ClientOptions(api_key=os.environ["INFLOW_API_KEY"], environment="sandbox")
        ) as buyer,
        httpx.AsyncClient(transport=x402AsyncTransport(buyer), follow_redirects=False) as http,
    ):
        response = await http.get(os.environ["PAID_RESOURCE_URL"])
        response.raise_for_status()
        print(response.text)


asyncio.run(main())
```

The upstream transport reads the merchant's payment requirements, asks the Buyer
for a payment payload, and retries the resource request with that payload. An
InFlow-managed payment can wait for the account owner to approve it. The default
approval wait is 15 minutes, polling every 5 seconds; configure `pending_timeout`
and `poll_interval` in seconds on `Buyer.create()`.

### Payment status and recovery

Both `BuyerMethod` (MPP) and `Buyer` (x402) expose
`await buyer.get_payment_status(transaction_id)`. Pass the original transaction
identifier after an uncertain payment result. The method returns the server's
transaction dictionary, including `status` and any `nextAction`, without creating,
confirming, or cancelling a payment. It does not open the next-action URL.

If `nextAction.type` is `authenticate_card`, your application can direct the buyer
to its `url` to authenticate in the dashboard. A later call reads the current
transaction state. Credential or payload availability does not establish that a
payment settled; neither do `INITIATED`, `PENDING`, or `PROCESSING` statuses.

Each call makes one request by default. Set `retries=1` to retry a transient read
failure; retries are capped at three. Cancelling the read leaves the payment alone.
An HTTP error, including a 404, is not permission to create a replacement payment.
Keep the original transaction identifier when the outcome remains uncertain.

### Linked-card payments

For a linked-card payment, pass `instrument_id="your-card-uuid"` to `Buyer.create()`.
It is forwarded only for the `instrument` scheme. Omit it to let InFlow select the
account's primary card. A rejected selection returns the server error; the SDK does
not try another card or create a replacement purchase. An explicit selection takes
precedence over `instrumentId` in transaction request extensions.
For automatic HTTP selection, also include `"instrument"` in `prefer`, for example
`prefer=("instrument",)`. The default preference remains balance followed by exact;
setting a card identifier does not change which payment scheme is selected.

Seller instrument offers require `schemes=["instrument"]` and a fiat USD price.
They are not included by default and do not use stablecoin expansion. Prices must
represent whole cents from USD 0.50 to 92233720368547758.07; the advertised wire
scale is retained. No blockchain wallet is required to construct a configured
instrument offer. MPP instrument receipts must match the method and challenge
before the protected resource is released.

### Show an approval before waiting

Use `await buyer.prepare(requirement, resource)` when your application needs the
`approval_id` and `transaction_id` before waiting. Pass a selected upstream
`PaymentRequirements` and `ResourceInfo`. Then call `await payment.await_payload()`
to obtain `encoded_payload`, `payment_payload`, and `transaction_id`, or
`await payment.cancel()` to request approval cancellation.

Repeated waits share the completed payload and run completion hooks once. A
timeout before receiving a payload allows another wait on the same handle without
creating a second approval. Cancelling an individual waiting task does not cancel
the server approval; neither does closing the Buyer. The application owns that
decision in the two-phase flow. The automatic `create_payment_payload()` flow
requests cancellation if it fails before receiving a signed payload.

### Payment selection and spending controls

`inflowpay.x402.buyer.Buyer` supports two signing paths. It requests an
InFlow-managed payment when InFlow supports the offered payment requirement.
Otherwise, it passes the request to an external-wallet scheme registered with
the upstream x402 client.

Use `register_policy()` to filter payment requirements for either path. For
InFlow-managed payments, the account's server-side policies and approval process
also apply. The upstream `set_spend_controls()` settings apply only to
external-wallet payments; they do not cap an InFlow-managed payment. This is the
same separation used by the InFlow Node SDK. If your application needs a local
amount limit for managed payments, enforce it in a registered payment policy.

The explicit `prepare(requirement, resource)` method uses the requirement you
provide, rather than selecting among offers or running selection policies. Apply
your application's selection policy before calling it. InFlow's server-side
policies and approvals still apply.

### External wallets

Install `inflowpay[evm]` or `inflowpay[svm]` and register an upstream signing scheme
with `buyer.register(network, scheme)` to allow external-wallet payments. The
Buyer first selects a supported InFlow payment; otherwise it delegates to the
registered upstream schemes. `prefer` controls the managed scheme order and
defaults to `("balance", "exact")`. Managed signing does not accept Permit2
requirements; those use an external wallet.

InFlow-managed payment requests use asynchronous network calls. External-wallet
payments run through the upstream Python x402 signing implementation. Its Solana
signer performs synchronous network requests for mint metadata and, when needed,
a recent blockhash. Those requests block other tasks on the same event loop even
when the caller awaits `create_payment_payload()`. The upstream TypeScript Solana
signer awaits these network requests instead.

The Python Buyer preserves upstream signing behavior; it does not move wallet
signers into background threads. Applications using external wallets should
account for this blocking work when sharing an event loop with other requests.
See [upstream issue #3649](https://github.com/x402-foundation/x402/issues/3649)
for the reproduction and comparison with the TypeScript implementation.

### EIP-7702 sponsorship

`inflowpay.x402.eip7702.SponsorshipExtension` supports external EVM wallets when
the merchant advertises `inflowEip7702GasSponsoring` for an exact Permit2 payment.
Install the `evm` extra. Construct the extension with anonymous `ClientOptions`,
a `SponsorshipSigner`, and an asynchronous consent callback, then register it with
`buyer.register_extension(extension)`. Keep the extension's asynchronous context
manager open while creating payments; it owns a separate HTTP client.

The signer provides its address and three asynchronous methods: `allowance()`
reads the token allowance; `sign_message()` signs the supplied operation hash
using Ethereum's personal-message prefix; and `sign_authorization()` signs the
supplied EIP-7702 authorization. Both signing methods return 65-byte signatures
with recovery ID 27 or 28. The signer must belong to the wallet registered with
the upstream payment scheme.

Sponsorship is skipped when the existing Permit2 allowance covers the payment.
Otherwise, the extension requests preparation from InFlow and verifies the
returned contracts, payment calls, operation hash, and expiry before signing.
If account delegation is required, the consent callback must return `True` only
after the owner agrees: delegation persists even if the payment fails. The
extension returns signed data; it does not broadcast a transaction. Availability
depends on the InFlow environment's sponsorship endpoint and supported networks.

## Accept x402 payments with FastAPI

Create an InFlow **Seller** account and an API key in its dashboard:
[Sandbox](https://sandbox.inflowpay.ai) for testing or
[Production](https://app.inflowpay.ai) for live payments. Use the matching
`environment` in `ClientOptions`.

```shell
pip install 'inflowpay[x402,fastapi]' uvicorn
export INFLOW_API_KEY='your-seller-api-key'
```

`Seller` reads your configured wallets, assets, and payment methods and converts
prices into upstream x402 route options. `Facilitator` sends verification and
settlement requests to InFlow. The upstream FastAPI middleware challenges the
buyer, verifies the payment before calling your handler, and settles after a
successful handler response.

```python
import asyncio
import os

import uvicorn
from fastapi import FastAPI
from x402 import x402ResourceServer
from x402.http.middleware.fastapi import payment_middleware
from x402.http.types import RouteConfig

from inflowpay import ClientOptions
from inflowpay.x402.facilitator import Facilitator
from inflowpay.x402.seller import Seller


async def serve() -> None:
    options = ClientOptions(environment="sandbox", api_key=os.environ["INFLOW_API_KEY"])
    async with (
        await Seller.create(options) as seller,
        await Facilitator.create(options) as facilitator,
    ):
        resource_server = x402ResourceServer(facilitator)
        for registration in await seller.scheme_registrations():
            resource_server.register(registration["network"], registration["server"])

        routes = {
            "GET /report": RouteConfig(accepts=await seller.offers("$0.01")),
        }
        app = FastAPI()
        app.middleware("http")(payment_middleware(routes, resource_server))

        @app.get("/report")
        async def report() -> dict[str, str]:
            return {"report": "Your paid report"}

        await uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=8000)).serve()


asyncio.run(serve())
```

Both clients own their HTTP connections and close them when their context exits.
`Seller.create()` requires an API key and preloads configuration and capabilities.
Configuration reads are cached for one hour; `await seller.config(refresh=True)`
forces a refresh. `await seller.get_supported(refresh=True)` refreshes the Seller's
capability lookup, not a running Facilitator or resource server. Values returned
by these methods can be modified without changing the client's cache.

### Prices, payment methods, and metering

`seller.offers()` accepts `"$0.01"`, `"0.01 USDC"`, or `"0.01"` with
`currency="USDC"`. Dollar prices select all stablecoin currencies in your Seller
configuration. An explicit `currency` overrides the currency in the price string.
Conversion uses decimal digits rather than floating-point arithmetic and rejects
prices that cannot be represented in an asset's smallest unit. Price strings
allow up to eight fractional digits.

Use `schemes=["balance"]` or `networks=["eip155:8453"]` to restrict offers.
Both filters apply when supplied together. Routes default to fixed-price offers
and a 300-second payment timeout; `max_timeout_seconds` changes the timeout.
An empty offers list means the selected configuration and filters produced no
payment option. Check it before exposing the route.

Metered `upto` payments require `inflowpay[evm]` and explicit `schemes=["upto"]`
on both `seller.offers()` and `seller.scheme_registrations()`. Your environment
must advertise metered Permit2 support for the asset. The route price is the
maximum authorized charge. In your FastAPI handler, call the upstream helper
`set_settlement_overrides(response, {"amount": "250000"})` to settle the actual
amount in atomic asset units. The buyer's signed maximum remains unchanged.
Without an override, settlement uses the route's maximum amount.

`await seller.route("0.01 USDC", schemes=["exact"], permit2=True)` selects
compatible Permit2 offers and adds a sponsorship declaration only when the token
and facilitator advertise support. It prefers EIP-2612 over InFlow EIP-7702.
Install `inflowpay[evm]` for EIP-2612 declarations. Balance offers are unaffected
by `permit2=True`; filter to `exact` for an on-chain-only route. InFlow-managed
buyers cannot sign Permit2 payments; these offers require external wallets.

### Settlement and framework behavior

The facilitator preserves a valid buyer payment identifier or derives one from
the signed payment material. Verification and settlement use the same identifier
without modifying the caller's payload. HTTP 412 `permit2_allowance_required`
is returned as an invalid verification result. Settlement retries only an HTTP
409 `idempotency_pending` response, at most five attempts and at most five seconds
between attempts. Other HTTP or transport failures are not retried automatically:
the payment may already have completed. Cancelling the task stops pending waits.

The upstream FastAPI middleware buffers the handler's successful response before
settlement; it does not stream the response to the buyer during settlement. A
rejected verification prevents the handler from running. A handler error prevents
after-handler settlement; a settlement failure replaces the successful handler
response with a payment failure. Avoid irreversible business side effects in a
handler without your own reconciliation design. InFlow does not roll back the
handler's work.

The clients are framework-independent and do not import FastAPI. Use them with
other upstream asynchronous adapters where appropriate. For anonymous external
on-chain facilitation, explicitly use
`await Facilitator.create(ClientOptions(environment="sandbox"), anonymous=True)`.
Anonymous setup rejects credentials; Seller configuration and InFlow balance
settlement require a Seller account.

## x402 facilitator capabilities

Facilitator capabilities describe the payment schemes, networks, and extensions
the facilitator supports. InFlow fetches these capabilities asynchronously during
adapter creation. Setup fails if that request fails, rather than returning an
adapter without capability data.

The upstream Python x402 resource server calls a synchronous `get_supported()`
method during initialization. The InFlow adapter answers from its preloaded
capabilities without making a network request. Payment verification and settlement
remain asynchronous.

Capabilities stay fixed for the lifetime of the adapter and its resource server.
To load capability changes, recreate both instances or restart the application.
Refreshing only the adapter's data or calling `initialize()` again on the same
upstream resource server is insufficient: x402 Python 2.25.0 retains existing
capability mappings, including networks removed from the facilitator's response.

This differs from the InFlow Node facilitator's one-hour capability cache. Node
refreshes that cache when it is queried after expiry; it does not automatically
reinitialize the application's resource server every hour. In Python, there is
no timed capability refresh or background polling.

## Cross-language verification

The [Python–Node interoperability suite](https://github.com/inflowpayai/inflow-python/blob/main/interop/README.md) exercises Buyers and
Sellers from both SDKs over local HTTP, including payment rejection and settlement
failure. It uses a synthetic InFlow platform and does not make live payments.

Maintainers can follow the [release instructions](https://github.com/inflowpayai/inflow-python/blob/main/RELEASING.md)
for versioning, Trusted Publishing setup, dry-runs, and publication.
