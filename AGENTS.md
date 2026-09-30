# Repository instructions

The distribution and import namespace are `inflowpay`. Use Python 3.11 or newer;
CI runs Python 3.11–3.14. Keep protocol and framework dependencies optional.

## Sources and review

Before implementing behavior, trace the corresponding InFlow Node SDK path end to
end, including the upstream library and receiving API. Use
[inflow-node](https://github.com/inflowpayai/inflow-node) and
[inflow-specs](https://github.com/inflowpayai/inflow-specs) as authoritative project
references. Ask the user about genuine design choices rather than inventing behavior.

Follow the review sequence: trace requirements; identify failure cases; implement
coherent slices; skeptically review boundaries and side effects; run full checks;
review the final tested diff; then create or update the pull request. Inspect
credentials, payment retries, cancellation, concurrency, caller-owned data, and
resource cleanup. Tests must exercise real integration boundaries, not merely
repeat assumptions in mocks.

## Code and documentation

- Keep network operations asynchronous and explicitly manage client lifetimes.
- Do not introduce implicit event-loop patching or synchronous network wrappers.
- Keep imports free of optional protocol and framework dependencies unless their
  corresponding module is requested.
- Use strict types and typed errors; let the application choose its logging.
- Explain intentional upstream behavior differences in the README and briefly at
  the relevant code boundary. Write for a reader unfamiliar with the repository.
- Do not describe APIs that do not exist in examples or add placeholder methods.
- Keep each pull request to one source commit. Do not publish or merge without approval.

## Verification

Run `make sync` then `make verify`. Verification includes Ruff formatting/lint,
strict mypy, tests with complete line and branch coverage per source file, a build
from the source distribution, and installation into an isolated consumer environment.
Do not lower coverage thresholds or exclude executable code to make a gate pass.
Report exact checks and any unverified work in the handoff.
