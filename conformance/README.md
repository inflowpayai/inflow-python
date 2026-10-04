# Shared conformance checks

These development checks run the public Python SDK against the fixtures and runner
in [inflow-specs](https://github.com/inflowpayai/inflow-specs). They do not ship in
the Python distribution or add production dependencies.

## Run locally

Use Node 24 and the pnpm version specified in the contract repository. Check out
`inflow-specs` at the full commit in `inflow-specs.lock.json`, in a separate local
directory with no uncommitted changes. Install its runner dependencies with
`pnpm install --frozen-lockfile`.

From this repository:

```sh
make sync
make verify
node --test scripts/conformance.test.mjs
reports=$(mktemp -d)
node scripts/conformance.mjs --contract-root ../inflow-specs --output-dir "$reports"
```

The script uses `.venv/bin/python` (`.venv/Scripts/python.exe` on Windows).
Set `CONFORMANCE_PYTHON` to select a different installed Python environment. That
environment must contain this SDK and all optional dependencies from the lockfile.

The output directory must exist. Existing report files are never overwritten.
The reports record the SDK and contract commits, dirty-state flags, installed
package versions, case results, and explicit unsupported features. A dirty-tree
run helps during development; retain clean-checkout reports when verifying a release.

The `shared conformance` workflow runs on Python 3.11–3.14 and uploads one report
artifact per Python version. A failed required case fails the job.

## What the adapters exercise

| Suite   | Python entry points                                                                                                         | Checks                                                                                                                                                                     |
| ------- | --------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Runtime | MPP `BuyerMethod` and `Seller.create`; x402 `Buyer.create` and `Seller.create`                                              | Environment destinations, authentication errors, account-specific errors, redirects, request identifiers, sensitive-header removal, and operation-specific retry behavior. |
| MPP     | Public codecs, `BuyerMethod.create_credential`, Seller preparation/validation, and pympp's `broadcast_credential` and `pay` | Wire data, approvals, cancellation, Buyer subscriptions, validation before broadcast, idempotency, and route binding.                                                      |
| x402    | Public identifier helpers, `Seller.offers`/`route`, `Buyer.prepare`, and `Facilitator.verify`/`settle`                      | Offer construction, sponsorship declarations, approval lifecycle, cancellation, concurrent waits, payment identifiers, and verification/settlement.                        |

The TAP suite calls `TapVerifier.with_verified`, `VisaTapKeyResolver` and
`MemoryTapReplayStore` through the public `inflowpay.tap.seller` module. Its 92
cases use real Ed25519 signatures, controlled clocks and loopback key endpoints.
They cover request binding, Structured Field parameters, validity intervals,
replay, cache replacement, outages and application-supplied failures. The adapter
does not parse signatures, construct signature bases or implement verification.
Handler and replay-claim counts come from the actual callback and store boundary.

The runner owns the loopback HTTP servers, expected request sequences and results.
The Python process receives inputs, not expected outcomes or response scripts.
Polling, retry, cancellation, validation and broadcast remain in the SDK and its
upstream libraries. Unknown adapter exceptions fail the case; they are not payment
successes or runtime skips.

Runtime cases use public product operations rather than exposing the SDK's private
HTTP client. Both Seller factories require an API key, so their runtime cases use
API keys; Buyer cases also cover Bearer and missing authentication. MPP Seller
configuration reads retry transient failures. The other three construction/payment
operations do not. These cases test those actual operation policies, not a generic
rule that every GET is retryable. Approval success and cancellation are exercised
by the payment suites rather than a separate raw approval client.

## Test setup and limits

Environment checks capture requests in a transport without sending them to public
hosts. Payment requests go only to the runner's `127.0.0.1` server. Use synthetic
credentials; these checks do not make live payments or prove server authorization.

x402 offer cases supply configuration through an HTTPX test transport. Python
Seller construction also loads supported capabilities; cases that do not supply
capabilities receive an empty list. Route cases use their supplied capabilities.
For facilitator verification/settlement and runtime Seller configuration-error
checks, the transport supplies an empty capabilities response for the construction
request only. Verification, settlement and configuration-error requests still use
real HTTP to the runner. This setup does not claim to test capability discovery;
the native SDK tests cover that construction behavior.

Ten MPP Seller-subscription cases are explicitly skipped because pympp's Seller
routes do not expose the necessary subscription terms. See
[pympp issue #269](https://github.com/tempoxyz/pympp/issues/269).
MPP Buyer-subscription cases remain required and execute normally.

Comparisons use the pinned runner's documented cross-language equivalences, including
the three descriptive EIP-2612 strings. The adapter does not replace schema constraints
or payment values to match expected output. Native unit, framework, consumer and
coverage checks remain required alongside these shared reports.
