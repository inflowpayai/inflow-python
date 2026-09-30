# InFlow Python SDK

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
