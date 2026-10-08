import { test } from "node:test";
import assert from "node:assert/strict";
import { execFileSync, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { runtimeCases } from "../conformance/runtime-cases.mjs";
import {
  acceptsDescriptionFailure,
  checkContract,
  descriptionDiagnostic,
} from "./conformance.mjs";

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

test("description allowance requires the exact failure and a passing diagnostic", () => {
  const report = {
    completed: true,
    passed: false,
    implementation: { dependencies: { pympp: "0.11.0" } },
    results: [
      {
        case_id: "mpp.card.verify-reference",
        suite: "mpp-seller",
        status: "failed",
        message: "Unexpected platform request at exchange 2",
      },
    ],
  };
  const diagnostic = {
    completed: true,
    passed: true,
    results: [{ case_id: "mpp.card.verify-reference", status: "passed" }],
  };
  assert.equal(acceptsDescriptionFailure(report, diagnostic), true);
  for (const change of [
    (r) => {
      r.completed = false;
    },
    (r) => {
      r.passed = true;
    },
    (r) => {
      r.runner_error = "Adapter failed";
    },
    (r) => {
      r.implementation.dependencies.pympp = "0.12.0";
    },
    (r) => {
      r.results[0].case_id = "another-case";
    },
    (r) => {
      r.results[0].suite = "mpp-buyer";
    },
    (r) => {
      r.results[0].status = "skipped";
    },
    (r) => {
      r.results[0].status = "passed";
    },
    (r) => {
      r.results[0].message = "Unexpected platform request at exchange 3";
    },
    (r) => {
      r.results.push({ case_id: "other", status: "failed" });
    },
    (r) => {
      r.results.push({ case_id: "other", status: "not_run" });
    },
  ]) {
    const changed = structuredClone(report);
    change(changed);
    assert.equal(acceptsDescriptionFailure(changed, diagnostic), false);
  }
  for (const change of [
    (d) => {
      d.completed = false;
    },
    (d) => {
      d.passed = false;
    },
    (d) => {
      d.runner_error = "Failure";
    },
    (d) => {
      d.results = [];
    },
    (d) => {
      d.results[0].case_id = "other";
    },
    (d) => {
      d.results[0].status = "failed";
    },
  ]) {
    const changed = structuredClone(diagnostic);
    change(changed);
    assert.equal(acceptsDescriptionFailure(report, changed), false);
  }
  assert.equal(acceptsDescriptionFailure(report), false);
});

test("description diagnostic changes only outbound descriptions and their platform echoes", () => {
  const credential = {
    challenge: {
      description: "Test purchase",
      expires: "2030",
      request: "encoded",
    },
  };
  const index = {
    cases: [
      {
        id: "mpp.card.verify-reference",
        input: { credential },
        platform: {
          exchanges: [
            { request: { method: "GET" } },
            {
              request: { json: { credential } },
              response: {
                json: { credential, challenge: credential.challenge },
              },
            },
            { request: { json: { credential } } },
          ],
        },
      },
    ],
  };
  const before = structuredClone(index);
  const diagnostic = descriptionDiagnostic(index);
  assert.deepEqual(index, before);
  const expected = structuredClone(before);
  // Independent objects represent the JSON fixture, rather than shared references.
  const unaliased = JSON.parse(JSON.stringify(expected));
  delete unaliased.cases[0].platform.exchanges[1].request.json.credential
    .challenge.description;
  delete unaliased.cases[0].platform.exchanges[2].request.json.credential
    .challenge.description;
  delete unaliased.cases[0].platform.exchanges[1].response.json.credential
    .challenge.description;
  delete unaliased.cases[0].platform.exchanges[1].response.json.challenge
    .description;
  assert.deepEqual(diagnostic, unaliased);
  assert.equal(diagnostic.cases.length, 1);
  assert.throws(() => descriptionDiagnostic({ cases: [] }), /fixture changed/);
});
