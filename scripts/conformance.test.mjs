import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { runtimeCases } from "../conformance/runtime-cases.mjs";
import { checkContract } from "./conformance.mjs";

const root = fileURLToPath(new URL("..", import.meta.url));
test("runtime cases preserve fixtures and use public operations", () => {
  const fixtures = {
    "auth.invalid-key": {
      exchanges: [
        {
          request: { headers: { "x-api-key": "test-only-key" } },
          response: { status: 401 },
        },
      ],
    },
    "auth.expired-bearer": {
      exchanges: [
        {
          request: { headers: { authorization: "Bearer test-only-token" } },
          response: { status: 401 },
        },
      ],
    },
    "auth.missing": {
      exchanges: [{ request: { headers: {} }, response: { status: 401 } }],
    },
    "auth.seller-required-developer-key": {
      exchanges: [
        {
          request: { headers: { "x-api-key": "test-only-key" } },
          response: {
            status: 403,
            json: {
              errors: [
                { code: "SELLER_ACCOUNT_REQUIRED", message: "Seller required" },
              ],
            },
          },
        },
      ],
    },
    "approval.cancel": { exchanges: [] },
    "auth.success": {
      exchanges: [{ request: { headers: {} }, response: { status: 200 } }],
    },
  };
  const before = structuredClone(fixtures);
  const { cases } = runtimeCases(fixtures);
  assert.deepEqual(fixtures, before);
  assert.equal(new Set(cases.map((item) => item.id)).size, cases.length);
  assert.equal(
    cases.filter((item) => item.operation === "runtime.environment").length,
    16,
  );
  for (const item of cases) {
    if (item.id.includes("seller-required"))
      assert.ok(item.input.product.endsWith("seller"));
    if (item.input.product.endsWith("seller")) assert.ok(item.input.api_key);
    if (item.id.endsWith("retry-policy"))
      assert.equal(
        item.platform.exchanges.length,
        item.input.product === "mpp-seller" ? 2 : 1,
      );
    assert.equal(
      item.input.tokens?.length ?? 0,
      item.expect.result.token_calls ?? 0,
    );
  }
});

test("invalid, mismatched and dirty contract revisions are rejected", () => {
  assert.throws(() => checkContract(root, "main"), /full contract commit/);
  assert.throws(
    () => checkContract(root, "0".repeat(40)),
    /clean contract checkout/,
  );
  const revision = execFileSync("git", ["rev-parse", "HEAD"], {
    cwd: root,
    encoding: "utf8",
  }).trim();
  if (
    execFileSync("git", ["status", "--porcelain"], {
      cwd: root,
      encoding: "utf8",
    }).trim()
  )
    assert.throws(
      () => checkContract(root, revision),
      /clean contract checkout/,
    );
  else checkContract(root, revision);
});

test("missing output configuration fails before launching an adapter", () => {
  const result = spawnSync(process.execPath, ["scripts/conformance.mjs"], {
    cwd: root,
    encoding: "utf8",
  });
  assert.notEqual(result.status, 0);
  assert.match(
    result.stderr,
    /--contract-root PATH --output-dir EXISTING_DIRECTORY/,
  );
});
