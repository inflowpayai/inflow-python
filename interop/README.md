# Python and Node interoperability

These checks run Python Buyers against Node Sellers and Node Buyers against Python
Sellers over real loopback HTTP. Each peer uses its SDK and upstream payment
transport or web middleware. The InFlow platform is a synthetic HTTP server: no
accounts, wallets, live signatures, or settlement are involved.

## Run

Use Node 24. Check out `inflow-node` at the commit in `node.lock.json`, with a clean
working tree. In that repository, run `pnpm install --frozen-lockfile` and
`pnpm build`. In this repository:

```sh
make sync
make verify
node scripts/interoperability.mjs ../inflow-node /tmp/python-node-report.json
```

The report path must not already exist. Set `INTEROP_PYTHON` to use a Python
environment other than `.venv`; install all locked extras and development groups
in that environment first. The report records both source revisions, Python
dependencies, Node package versions, and each case's observed platform requests.

## What is checked

- MPP InFlow and Tempo charges in both directions; Python subscription creation
  and existing-subscription use against Node Sellers.
- x402 balance and exact payments in both directions.
- Immediate and pending approval results, verification rejection, settlement
  failure, and protected-handler failure.
- One purchase per request, application-header preservation, no platform API key
  sent to a merchant, receipt identity, and verification/settlement ordering.
- Deliberately corrupted receipts must fail the harness assertions in all four
  protocol/direction combinations.

MPP broadcasts before calling the protected handler. x402 verifies before the
handler and settles after a successful handler response. The assertions reflect
these different lifecycles rather than treating them as interchangeable.

Python MPP Seller subscriptions are excluded because pympp's Seller routes lack
the required terms; see [pympp #269](https://github.com/tempoxyz/pympp/issues/269).
The standalone Python MPP decorator does not set private cache headers on the
handler's response; the application owns those headers. Cache assertions apply
to Node MPP and both x402 middleware implementations.

This suite does not prove live platform authorization, blockchain execution, MCP
interoperability, or external-wallet signing. Native tests and shared conformance
checks remain separate requirements. These test peers are not shipped in the
Python distribution.
