# InFlow Python SDK

Python integration for InFlow payments using the Machine Payments Protocol (MPP)
and x402. The Python distribution and import namespace are both `inflowpay`.
Python 3.11 or newer is required.

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

Protocol and framework dependencies are optional. The `mpp` and `x402` extras
select payment libraries; `evm` and `svm` select x402 external-wallet dependencies;
`mcp` selects MCP dependencies for both protocols; `fastapi` selects the optional
web framework. The base package does not install those extras.

Runtime dependencies use compatible version ranges; `uv.lock` records the exact
versions used in development and CI. MCP dependencies use the 1.x line required by
pympp. No framework or blockchain package is imported by `import inflowpay`.

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

`validate_request` checks InFlow charge/subscription and Tempo charge request shapes;
`validate_payload` checks InFlow or Tempo credential payload shapes. Both return a
deep copy. These checks do not establish supported currencies, account permissions,
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
