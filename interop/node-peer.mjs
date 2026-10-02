import { createServer } from "node:http";
import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";
import { resolve } from "node:path";
import { createInterface } from "node:readline";

const root = resolve(process.argv[2]);
const lines = createInterface({ input: process.stdin });
const { value } = await lines[Symbol.asyncIterator]().next();
lines.close();
const s = JSON.parse(value);
for (const value of [s.Platform, ...(s.Role === "buyer" ? [s.Target] : [])]) {
  const url = new URL(value);
  if (
    url.protocol !== "http:" ||
    url.hostname !== "127.0.0.1" ||
    url.username ||
    url.password
  )
    throw Error("Loopback endpoints required");
}
const load = (name) =>
  import(pathToFileURL(resolve(root, "packages", name, "dist/index.js")));
const dependency = (name, specifier) =>
  import(
    pathToFileURL(
      createRequire(resolve(root, "packages", name, "package.json")).resolve(
        specifier,
      ),
    )
  );
const options = {
  baseUrl: s.Platform,
  apiKey: `test-only-${s.Role}-key`,
  timeoutMs: 5000,
};
const output = (value) => process.stdout.write(`${JSON.stringify(value)}\n`);

if (s.Role === "buyer") {
  let response,
    receipt = null;
  if (s.Protocol === "mpp") {
    const buyer = await load("mpp-buyer");
    const method =
      s.Variant === "tempo"
        ? buyer.tempo(options)
        : s.Variant === "subscription"
          ? buyer.inflow.subscription(options)
          : buyer.inflow(options);
    // Exercise one payment attempt; upstream defaults can buy again after a rejected credential.
    const client = buyer.Mppx.create({
      methods: [method],
      polyfill: false,
      maxPaymentRetries: 1,
    });
    try {
      response = await client.fetch(s.Target, {
        headers: { "X-App-Session": "test-only-session" },
        ...(s.SubscriptionID
          ? { context: { subscriptionId: s.SubscriptionID } }
          : {}),
      });
    } finally {
      method.cleanup();
    }
    if (response.headers.has("Payment-Receipt"))
      receipt = (await load("mpp")).decodeReceipt(
        response.headers.get("Payment-Receipt"),
      );
  } else {
    const buyer = await load("x402-buyer");
    const { x402HTTPClient } = await dependency(
      "x402-buyer",
      "@x402/core/client",
    );
    const client = new x402HTTPClient(
      await buyer.createInflowClient({ ...options, pollIntervalMs: 0 }),
    );
    response = await fetch(s.Target, {
      headers: { "X-App-Session": "test-only-session" },
      redirect: "error",
    });
    if (response.status === 402) {
      const required = client.getPaymentRequiredResponse((name) =>
        response.headers.get(name),
      );
      const payload = await client.createPaymentPayload(required);
      response = await fetch(s.Target, {
        headers: {
          ...client.encodePaymentSignatureHeader(payload),
          "X-App-Session": "test-only-session",
        },
        redirect: "error",
      });
    }
    if (response.headers.has("PAYMENT-RESPONSE"))
      receipt = client.getPaymentSettleResponse((name) =>
        response.headers.get(name),
      );
  }
  output({
    status: response.status,
    body: await response.text(),
    receipt,
    cache: response.headers.get("Cache-Control"),
  });
} else if (s.Role === "seller") {
  let listener;
  const handle = async () => {
    const response = await fetch(`${s.Platform}/handler`, {
      method: "POST",
      redirect: "error",
    });
    if (!response.ok) throw Error("Handler evidence rejected");
  };
  if (s.Protocol === "mpp") {
    const seller = await load("mpp-seller");
    const method =
      s.Variant === "tempo"
        ? seller.tempo({
            ...options,
            currency: "0x20c0000000000000000000000000000000000000",
            recipient: "0x1111111111111111111111111111111111111111",
          })
        : s.Variant === "subscription"
          ? seller.inflow.subscription(options)
          : seller.inflow(options);
    const framework = seller.Mppx.create({
      methods: [method],
      realm: "interop",
      secretKey: "test-only-binding-secret-at-least-32-bytes",
    });
    const terms = {
      amount: s.Variant === "tempo" ? "10000" : "0.01",
      ...(s.Variant === "tempo" ? {} : { currency: "USDC" }),
      ...(s.Variant === "subscription"
        ? {
            periodUnit: "month",
            periodCount: 1,
            subscriptionExpires: "2099-01-01T00:00:00Z",
          }
        : {}),
    };
    listener = async (req, res) => {
      const result = await framework[
        s.Variant === "subscription" ? "subscription" : "charge"
      ](terms)(
        new Request(`http://127.0.0.1${req.url}`, { headers: req.headers }),
      );
      const response =
        result.status === 402
          ? result.challenge
          : (await handle(),
            result.withReceipt(
              new Response('{"paidResource":true}', {
                status: s.HandlerStatus,
              }),
            ));
      res.writeHead(response.status, Object.fromEntries(response.headers));
      res.end(await response.text());
    };
  } else {
    const seller = await load("x402-seller");
    const require = createRequire(
      resolve(root, "examples/x402-seller-express/package.json"),
    );
    const { default: express } = await import(
      pathToFileURL(require.resolve("express"))
    );
    const { paymentMiddlewareFromConfig } = await import(
      pathToFileURL(require.resolve("@x402/express"))
    );
    const client = await seller.createInflowSellerClient(options);
    const route = await seller.inflowRoute(client, {
      price: "0.01 USDC",
      schemes: [s.Variant],
    });
    const app = express();
    app.use(
      paymentMiddlewareFromConfig(
        { "GET /paid": route },
        [seller.createInflowFacilitator(options)],
        await seller.inflowSchemeRegistrations(client, {
          schemes: [s.Variant],
        }),
      ),
    );
    app.get("/paid", async (_req, res) => {
      await handle();
      res.status(s.HandlerStatus).json({ paidResource: true });
    });
    listener = app;
  }
  const server = createServer((req, res) => {
    if (
      req.headers["x-api-key"] ||
      req.headers["x-app-session"] !== "test-only-session"
    ) {
      res.writeHead(500);
      res.end("authentication boundary failure");
      return;
    }
    Promise.resolve(listener(req, res)).catch((error) => {
      process.stderr.write(`${error.stack}\n`);
      res.writeHead(500);
      res.end("Peer failed");
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  output({ url: `http://127.0.0.1:${server.address().port}/paid` });
} else throw Error("Unknown peer role");
