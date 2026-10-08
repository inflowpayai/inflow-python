import { execFileSync } from "node:child_process";
import { readFile, open } from "node:fs/promises";
import { resolve, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import { parseArgs } from "node:util";
import { runtimeCases } from "../conformance/runtime-cases.mjs";

const root = fileURLToPath(new URL("..", import.meta.url));
const command = (program, args, cwd = root) =>
  execFileSync(program, args, {
    cwd,
    encoding: "utf8",
    timeout: 180000,
  }).trim();
export function checkContract(path, revision) {
  if (!/^[0-9a-f]{40}$/.test(revision))
    throw new Error("Expected a full contract commit");
  if (
    command("git", ["rev-parse", "HEAD"], path) !== revision ||
    command("git", ["status", "--porcelain"], path)
  )
    throw new Error(`Use a clean contract checkout at ${revision}`);
}
const descriptionIssue = "https://github.com/tempoxyz/pympp/issues/272";
const descriptionCase = "mpp.card.verify-reference";

export function acceptsDescriptionFailure(report, diagnostic) {
  const failures = report.results.filter((item) => item.status !== "passed");
  return (
    report.completed === true &&
    !report.runner_error &&
    !report.passed &&
    report.implementation.dependencies.pympp === "0.11.0" &&
    failures.length === 1 &&
    failures[0].case_id === descriptionCase &&
    failures[0].suite === "mpp-seller" &&
    failures[0].status === "failed" &&
    failures[0].message === "Unexpected platform request at exchange 2" &&
    diagnostic?.completed === true &&
    diagnostic.passed === true &&
    !diagnostic.runner_error &&
    diagnostic.results.length === 1 &&
    diagnostic.results[0].case_id === descriptionCase &&
    diagnostic.results[0].status === "passed"
  );
}

export function descriptionDiagnostic(index) {
  const item = structuredClone(
    index.cases.find((item) => item.id === descriptionCase),
  );
  if (!item?.input.credential.challenge.description)
    throw new Error("Description fixture changed; review pympp#272 allowance");
  // Keep the caller's complete credential; the platform echoes the received credential.
  for (const exchange of item.platform.exchanges) {
    if (exchange.request.json?.credential) {
      exchange.request = structuredClone(exchange.request);
      delete exchange.request.json.credential.challenge.description;
    }
    if (exchange.response?.json?.credential) {
      exchange.response = structuredClone(exchange.response);
      delete exchange.response.json.credential.challenge.description;
      delete exchange.response.json.challenge.description;
    }
  }
  return { ...index, cases: [item] };
}
async function main() {
  const { values } = parseArgs({
    options: {
      "contract-root": { type: "string" },
      "output-dir": { type: "string" },
    },
  });
  if (!values["contract-root"] || !values["output-dir"])
    throw new Error("Use --contract-root PATH --output-dir EXISTING_DIRECTORY");
  const contractRoot = resolve(values["contract-root"]);
  const outputDirectory = resolve(values["output-dir"]);
  process.chdir(root);
  const pin = JSON.parse(
    await readFile(
      new URL("../conformance/inflow-specs.lock.json", import.meta.url),
      "utf8",
    ),
  );
  checkContract(contractRoot, pin.revision);
  const python =
    process.env.CONFORMANCE_PYTHON ||
    join(
      root,
      ".venv",
      process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
    );
  const implementation = JSON.parse(
    command(python, [
      "-c",
      `import json,platform; from importlib.metadata import distributions,version; print(json.dumps(dict(name="inflow-python",runtime="Python "+platform.python_version(),packages={"inflowpay":version("inflowpay")},dependencies={d.metadata["Name"]:d.version for d in distributions() if d.metadata["Name"]!="inflowpay"})))`,
    ]),
  );
  const { run } = await import(
    pathToFileURL(join(contractRoot, "runner/run.mjs"))
  );
  const controller = new AbortController();
  const abort = () => controller.abort();
  process.once("SIGINT", abort);
  process.once("SIGTERM", abort);
  try {
    for (const suite of [
      "runtime",
      "mpp",
      "x402",
      "tap",
      "payment-status",
      "stripe",
      "card",
    ]) {
      if (controller.signal.aborted) throw new Error("Conformance interrupted");
      const fixtures = await import(
        pathToFileURL(join(contractRoot, `fixtures/${suite}.mjs`))
      );
      const output = await open(
        join(outputDirectory, `${suite}.json`),
        "wx",
        0o600,
      );
      try {
        const configuration = {
          index:
            suite === "runtime"
              ? runtimeCases(fixtures.runtimeScenarios)
              : fixtures[
                  suite === "payment-status"
                    ? "paymentStatusCases"
                    : `${suite}Cases`
                ],
          capabilities: {
            suites:
              suite === "runtime"
                ? ["runtime"]
                : suite === "payment-status"
                  ? ["mpp-buyer", "x402-buyer"]
                  : suite === "card"
                    ? ["mpp-seller", "mpp-buyer"]
                    : suite === "stripe"
                      ? ["mpp-seller"]
                      : suite === "tap"
                        ? ["tap-seller"]
                        : [
                            `${suite}-core`,
                            `${suite}-buyer`,
                            `${suite}-seller`,
                          ],
            supported_features: [],
            unsupported_features:
              suite === "mpp"
                ? [
                    {
                      id: "mpp-seller-subscriptions",
                      reason:
                        "pympp Seller routes do not expose subscription terms; see tempoxyz/pympp#269.",
                    },
                  ]
                : [],
          },
          implementation,
          command: [python, "-m", "conformance.adapter"],
          contractRoot,
          sdkRoot: root,
          signal: controller.signal,
        };
        const report = await run(configuration);
        await output.writeFile(JSON.stringify(report, null, 2) + "\n");
        console.log(
          `${suite}: ${report.results.filter((r) => r.status === "passed").length}/${report.results.length} passed`,
        );
        let accepted = false;
        if (suite === "card") {
          const diagnostic = await run({
            ...configuration,
            index: descriptionDiagnostic(configuration.index),
            capabilities: {
              ...configuration.capabilities,
              suites: ["mpp-seller"],
            },
          });
          const diagnosticOutput = await open(
            join(outputDirectory, "card-description-diagnostic.json"),
            "wx",
            0o600,
          );
          try {
            await diagnosticOutput.writeFile(
              JSON.stringify(diagnostic, null, 2) + "\n",
            );
          } finally {
            await diagnosticOutput.close();
          }
          accepted = acceptsDescriptionFailure(report, diagnostic);
          if (accepted)
            console.warn(
              `Known failed case ${descriptionCase}: ${descriptionIssue}`,
            );
          else {
            process.exitCode = 1;
            console.error(
              `Review or remove the pympp#272 allowance: ${descriptionIssue}`,
            );
          }
        }
        if (!report.passed && !accepted) {
          process.exitCode = 1;
          console.error(
            report.runner_error ??
              report.results.filter(
                (r) => r.status !== "passed" && r.status !== "skipped",
              ),
          );
        }
      } finally {
        await output.close();
      }
    }
  } finally {
    process.removeListener("SIGINT", abort);
    process.removeListener("SIGTERM", abort);
  }
}
if (process.argv[1] === fileURLToPath(import.meta.url)) await main();
