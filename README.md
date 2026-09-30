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

## Upstream MPP compatibility

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
