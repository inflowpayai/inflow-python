import assert from "node:assert/strict";
import { createServer } from "node:http";
import { spawn, execFileSync } from "node:child_process";
import { readFileSync, writeFileSync } from "node:fs";
import { resolve, join } from "node:path";
import { once } from "node:events";

const root = resolve(import.meta.dirname, "..");
const nodeRoot = resolve(process.argv[2] ?? "../inflow-node");
const reportPath = resolve(process.argv[3] ?? "interoperability.json");
const pin = JSON.parse(
  readFileSync(join(root, "interop/node.lock.json")),
).revision;
const git = (cwd, ...args) =>
  execFileSync("git", args, { cwd, encoding: "utf8" }).trim();
assert.equal(
  git(nodeRoot, "rev-parse", "HEAD"),
  pin,
  "Node checkout must match the pin",
);
assert.equal(
  git(nodeRoot, "status", "--porcelain"),
  "",
  "Node checkout must be clean",
);
const python =
  process.env.INTEROP_PYTHON ||
  join(
    root,
    ".venv",
    process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
  );
const children = new Set();
let interrupted = false;
for (const signal of ["SIGINT", "SIGTERM"]) {
  process.once(signal, () => {
    interrupted = true;
    process.exitCode = 1;
    for (const child of children) child.kill("SIGKILL");
  });
}
const report = {
  sdk_revision: git(root, "rev-parse", "HEAD"),
  sdk_dirty: git(root, "status", "--porcelain") !== "",
  node_revision: pin,
  node_version: process.version,
  platform: "synthetic loopback HTTP; no live signing or settlement",
  node_packages: Object.fromEntries(
    ["mpp", "mpp-buyer", "mpp-seller", "x402", "x402-buyer", "x402-seller"].map(
      (name) => [
        name,
        JSON.parse(
          readFileSync(join(nodeRoot, "packages", name, "package.json")),
        ).version,
      ],
    ),
  ),
  exclusions: [
    "Python MPP Seller subscriptions: pympp#269; Python Buyer subscriptions are exercised against Node Sellers.",
  ],
  cases: [],
  passed: false,
};
const id = "22222222-2222-4222-8222-222222222222";
const approval = "33333333-3333-4333-8333-333333333333";
const sellerId = "11111111-1111-4111-8111-111111111111";
const mppCases = JSON.parse(
  readFileSync(join(root, "tests/fixtures/mpp-seller.json")),
);
const mppConfig = mppCases.cases
  .flatMap((c) => c.platform.exchanges)
  .find((e) => e.request.path === "/v1/mpp/config").response.json;
const xCases = JSON.parse(
  readFileSync(join(root, "tests/fixtures/x402-buyer.json")),
);
const supported = xCases.cases[0].platform.exchanges.find(
  (e) => e.request.path === "/v1/transactions/x402-supported",
).response.json;
const config = JSON.parse(
  readFileSync(join(root, "tests/fixtures/x402-seller.json")),
).cases.find((c) => c.id === "x402.seller.offers-default").input.config;
const encode = (object, encoding = "base64url") =>
  Buffer.from(JSON.stringify(object)).toString(encoding);

function peer(language, settings) {
  const child =
    language === "python"
      ? spawn(python, ["-m", "interop.peer"], { cwd: root })
      : spawn(process.execPath, [
          join(root, "interop/node-peer.mjs"),
          nodeRoot,
        ]);
  children.add(child);
  let stdout = "",
    stderr = "";
  child.stdout.on("data", (chunk) => {
    stdout += chunk;
    if (stdout.length > 1048576) child.kill("SIGKILL");
  });
  child.stderr.on("data", (chunk) => {
    stderr += chunk;
    if (stderr.length > 1048576) child.kill("SIGKILL");
  });
  child.stdin.on("error", () => {});
  child.stdin.end(JSON.stringify(settings));
  const timer = setTimeout(() => child.kill("SIGKILL"), 20000);
  const exited = new Promise((resolve, reject) => {
    child.on("error", reject);
    child.on("close", (code) => {
      clearTimeout(timer);
      children.delete(child);
      code === 0 && stdout.length <= 1048576 && stderr.length <= 1048576
        ? resolve(stdout)
        : reject(Error(`Peer exited ${code}: ${stderr}`));
    });
  });
  // A listening Seller intentionally remains alive until its case finishes.
  exited.catch(() => {});
  return {
    child,
    exited,
    async ready() {
      for (let i = 0; i < 400; i++) {
        if (stdout.includes("\n")) return JSON.parse(stdout.split("\n")[0]);
        if (child.exitCode !== null) throw Error(stderr);
        await new Promise((r) => setTimeout(r, 25));
      }
      throw Error("Seller startup timed out");
    },
  };
}

async function runCase(
  protocol,
  sellerLanguage,
  scenario,
  variant,
  corruptReceipt = false,
) {
  if (interrupted) throw Error("Interoperability run interrupted");
  const existingSubscription = variant === "existing-subscription";
  const events = [],
    errors = [];
  let credential,
    payload,
    created = 0,
    verified = 0,
    completed = 0,
    polls = 0;
  const platform = createServer(async (req, res) => {
    res.setHeader("Content-Type", "application/json");
    const send = (object) => res.end(JSON.stringify(object));
    try {
      const chunks = [];
      for await (const chunk of req) chunks.push(chunk);
      const body = chunks.length ? JSON.parse(Buffer.concat(chunks)) : {};
      const path = req.url;
      events.push(`${req.method} ${path}`);
      if (path === "/handler") {
        assert.equal(req.method, "POST");
        return send({ ok: true });
      }
      assert.equal(
        req.headers["x-api-key"],
        `test-only-${path.startsWith("/v1/transactions") || path.startsWith("/v1/subscriptions/") ? "buyer" : "seller"}-key`,
      );
      if (path === "/v1/mpp/config") return send(mppConfig);
      if (path === "/v1/x402/config") return send(config);
      if (path === "/v1/transactions/x402-supported") return send(supported);
      if (path === "/v1/x402/supported")
        return send({ kinds: config.supported });
      if (
        path === "/v1/transactions/mpp" ||
        path === `/v1/subscriptions/${id}/authorize`
      ) {
        assert.equal(req.method, "POST");
        assert.equal(
          path,
          existingSubscription
            ? `/v1/subscriptions/${id}/authorize`
            : "/v1/transactions/mpp",
        );
        if (existingSubscription)
          assert.deepEqual(Object.keys(body), ["challenge"]);
        assert.equal(++created, 1);
        const request = JSON.parse(
          Buffer.from(body.challenge.request, "base64url"),
        );
        assert.equal(request.amount, variant === "tempo" ? "10000" : "0.01");
        assert.equal(
          request.currency,
          variant === "tempo"
            ? "0x20c0000000000000000000000000000000000000"
            : "USDC",
        );
        assert.equal(
          request.recipient,
          variant === "tempo"
            ? "0x1111111111111111111111111111111111111111"
            : sellerId,
        );
        credential = {
          challenge: body.challenge,
          source: "did:inflow:66666666-6666-4666-8666-666666666666",
          payload: existingSubscription
            ? {
                authorizationExpires: body.challenge.expires,
                authorizationId: approval,
                subscriptionId: id,
                transactionId: id,
                authorizationSignature: "synthetic-platform-signature",
              }
            : variant === "tempo"
              ? { type: "hash", hash: `0x${"11".repeat(32)}` }
              : { transactionId: id },
        };
        if (existingSubscription)
          return send({ credential: encode(credential) });
        return send(
          scenario === "pending"
            ? {
                state: "pending",
                transactionId: id,
                approvalId: approval,
                retryAfterSeconds: 0,
              }
            : {
                state: "ready",
                transactionId: id,
                credential: encode(credential),
              },
        );
      }
      if (path === `/v1/transactions/${id}/mpp`) {
        polls++;
        return send({
          state: "ready",
          transactionId: id,
          credential: encode(credential),
        });
      }
      if (path === "/v1/mpp/validate" || path === "/v1/mpp/broadcast") {
        assert.deepEqual(body.credential, credential);
        if (path.endsWith("/validate")) {
          verified++;
          if (scenario === "invalid")
            return send({
              success: false,
              problem: {
                type: "https://paymentauth.org/problems/verification-failed",
                title: "Rejected test payment",
                status: 402,
              },
            });
          return send({
            success: true,
            credential,
            challenge: credential.challenge,
            source: credential.source,
            method: credential.challenge.method,
            intent: credential.challenge.intent,
            request: JSON.parse(
              Buffer.from(credential.challenge.request, "base64url"),
            ),
            details: {},
          });
        }
        assert.equal(verified, 1);
        completed++;
        if (scenario === "settlement-failed")
          return send({
            problem: {
              type: "https://paymentauth.org/problems/verification-failed",
              title: "Rejected test payment",
              status: 402,
            },
          });
        return send({
          receipt: {
            method: credential.challenge.method,
            status: "success",
            reference: corruptReceipt ? approval : id,
            timestamp: "2026-09-29T00:00:00Z",
          },
        });
      }
      if (path === "/v1/transactions/x402") {
        assert.equal(++created, 1);
        assert.equal(
          body.accept.amount,
          variant === "exact" ? "10000" : "1000000",
        );
        assert.equal(
          body.accept.payTo,
          variant === "exact" ? config.wallets[0].address : sellerId,
        );
        payload = {
          x402Version: 2,
          accepted: body.accept,
          resource: body.resource,
          payload: { transactionId: id },
          extensions: {
            "payment-identifier": {
              info: { required: false, id: "interop-payment-identifier" },
              schema: {
                $schema: "https://json-schema.org/draft/2020-12/schema",
                type: "object",
                properties: {
                  id: {
                    type: "string",
                    minLength: 16,
                    maxLength: 128,
                    pattern: "^[a-zA-Z0-9_-]+$",
                  },
                  required: { type: "boolean" },
                },
                required: ["required"],
              },
            },
          },
        };
        return send({
          transactionId: id,
          approvalId: approval,
          approvalStatus: "APPROVED",
          amount: "0.01",
          currency: "USDC",
        });
      }
      if (path === `/v1/transactions/${id}/x402`) {
        polls++;
        if (scenario === "pending" && polls === 1)
          return send({ status: "INITIATED" });
        return send({
          status: "COMPLETED",
          paymentPayload: payload,
          encodedPayload: encode(payload, "base64"),
        });
      }
      if (path === "/v1/x402/verify" || path === "/v1/x402/settle") {
        assert.deepEqual(body.paymentPayload, payload);
        assert.deepEqual(body.paymentRequirements, payload.accepted);
        assert.deepEqual(body.paymentPayload.accepted, payload.accepted);
        assert.deepEqual(body.paymentPayload.resource, payload.resource);
        if (path.endsWith("/verify")) {
          verified++;
          return send({
            isValid: scenario !== "invalid",
            ...(scenario === "invalid"
              ? { invalidReason: "test_rejected" }
              : {}),
          });
        }
        assert.equal(verified, 1);
        completed++;
        return send({
          success: scenario !== "settlement-failed",
          network: payload.accepted.network,
          transaction: corruptReceipt ? approval : id,
          ...(scenario === "settlement-failed"
            ? { errorReason: "test_rejected" }
            : {}),
        });
      }
      throw Error(`Unexpected platform request ${req.method} ${path}`);
    } catch (error) {
      errors.push(error.message);
      res.statusCode = 400;
      send({ error: "Unexpected test request" });
    }
  });
  platform.listen(0, "127.0.0.1");
  await once(platform, "listening");
  const settings = {
    Protocol: protocol,
    Platform: `http://127.0.0.1:${platform.address().port}`,
    Variant: existingSubscription ? "subscription" : variant,
    ...(existingSubscription ? { SubscriptionID: id } : {}),
    HandlerStatus: scenario === "handler-failed" ? 500 : 200,
  };
  let seller;
  try {
    seller = peer(sellerLanguage, { ...settings, Role: "seller" });
    const { url } = await seller.ready();
    const buyer = peer(sellerLanguage === "python" ? "node" : "python", {
      ...settings,
      Role: "buyer",
      Target: url,
    });
    const result = JSON.parse(await buyer.exited);
    assert.deepEqual(errors, []);
    assert.equal(created, 1);
    assert.equal(verified, 1);
    if (existingSubscription) assert.equal(polls, 0);
    const handlerCount = events.filter((e) => e === "POST /handler").length;
    const denied = scenario === "invalid" || scenario === "settlement-failed";
    assert.equal(
      result.status,
      denied ? 402 : scenario === "handler-failed" ? 500 : 200,
    );
    assert.equal(
      handlerCount,
      scenario === "invalid" ||
        (protocol === "mpp" && scenario === "settlement-failed")
        ? 0
        : 1,
    );
    assert.equal(
      completed,
      scenario === "invalid" ||
        (protocol === "x402" && scenario === "handler-failed")
        ? 0
        : 1,
    );
    if (!denied && scenario !== "handler-failed") {
      assert.ok(result.receipt);
      if (protocol === "mpp") {
        assert.equal(result.receipt.reference, id, "receipt mismatch");
        assert.equal(result.receipt.status, "success");
        assert.equal(
          result.receipt.method,
          variant === "tempo" ? "tempo" : "inflow",
        );
      } else {
        assert.equal(result.receipt.transaction, id, "receipt mismatch");
        assert.equal(result.receipt.success, true);
        assert.equal(result.receipt.network, payload.accepted.network);
      }
      if (protocol === "x402" || sellerLanguage === "node")
        assert.match(result.cache, /private/i);
    }
    if (scenario === "pending")
      assert.ok(polls >= (protocol === "x402" ? 2 : 1));
    if (!denied)
      assert.deepEqual(JSON.parse(result.body), { paidResource: true });
    if (handlerCount && completed) {
      const terminal = events.findIndex((e) =>
        e.endsWith(protocol === "mpp" ? "/broadcast" : "/settle"),
      );
      const handler = events.indexOf("POST /handler");
      assert.ok(protocol === "mpp" ? terminal < handler : handler < terminal);
    }
    return {
      protocol,
      seller: sellerLanguage,
      buyer: sellerLanguage === "python" ? "node" : "python",
      scenario,
      variant,
      passed: true,
      events,
    };
  } finally {
    if (seller) {
      seller.child.kill("SIGTERM");
      await seller.exited.catch(() => {});
    }
    platform.closeAllConnections();
    await new Promise((r) => platform.close(r));
  }
}

try {
  report.python = JSON.parse(
    execFileSync(
      python,
      [
        "-c",
        'import json,platform; from importlib.metadata import distributions; print(json.dumps({"version":platform.python_version(),"dependencies":{d.metadata["Name"]:d.version for d in distributions()}}))',
      ],
      { encoding: "utf8" },
    ),
  );
  for (const protocol of ["mpp", "x402"])
    for (const variant of protocol === "mpp"
      ? ["charge", "subscription", "existing-subscription", "tempo"]
      : ["balance", "exact"])
      for (const seller of ["python", "node"])
        for (const scenario of [
          "ready",
          "pending",
          "invalid",
          "settlement-failed",
          "handler-failed",
        ]) {
          if (
            (seller === "python" &&
              ["subscription", "existing-subscription"].includes(variant)) ||
            (variant === "existing-subscription" && scenario === "pending")
          )
            continue;
          try {
            report.cases.push(
              await runCase(protocol, seller, scenario, variant),
            );
            process.stdout.write(
              `PASS ${protocol} ${variant} ${seller} seller ${scenario}\n`,
            );
          } catch (error) {
            report.cases.push({
              protocol,
              variant,
              seller,
              scenario,
              passed: false,
              error: error.stack,
            });
            process.stderr.write(
              `FAIL ${protocol} ${variant} ${seller} seller ${scenario}: ${error.message}\n`,
            );
          }
        }
  for (const protocol of ["mpp", "x402"]) {
    for (const seller of ["python", "node"]) {
      await assert.rejects(
        runCase(
          protocol,
          seller,
          "ready",
          protocol === "mpp" ? "charge" : "balance",
          true,
        ),
        (error) =>
          error.code === "ERR_ASSERTION" &&
          error.message.startsWith("receipt mismatch"),
        "The harness must reject a corrupted receipt",
      );
      report.cases.push({
        protocol,
        seller,
        negative_control: "corrupted receipt rejected",
        passed: true,
      });
    }
  }
  report.passed = !interrupted && report.cases.every((c) => c.passed);
  if (!report.passed) process.exitCode = 1;
} finally {
  await Promise.all(
    [...children].map(
      (child) =>
        new Promise((resolve) => {
          child.once("close", resolve);
          child.kill("SIGKILL");
        }),
    ),
  );
  writeFileSync(reportPath, `${JSON.stringify(report, null, 2)}\n`, {
    flag: "wx",
    mode: 0o600,
  });
}
