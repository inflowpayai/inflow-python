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
    for (const suite of ["runtime", "mpp", "x402", "tap"]) {
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
        const report = await run({
          index:
            suite === "runtime"
              ? runtimeCases(fixtures.runtimeScenarios)
              : fixtures[`${suite}Cases`],
          capabilities: {
            suites:
              suite === "runtime"
                ? ["runtime"]
                : suite === "tap"
                  ? ["tap-seller"]
                  : [`${suite}-core`, `${suite}-buyer`, `${suite}-seller`],
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
        });
        await output.writeFile(JSON.stringify(report, null, 2) + "\n");
        console.log(
          `${suite}: ${report.results.filter((r) => r.status === "passed").length}/${report.results.length} passed`,
        );
        if (!report.passed) {
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
