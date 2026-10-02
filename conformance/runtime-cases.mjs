const clients = {
  "mpp-buyer": ["POST", "/v1/transactions/mpp"],
  "mpp-seller": ["GET", "/v1/mpp/config"],
  "x402-buyer": ["GET", "/v1/transactions/x402-supported"],
  "x402-seller": ["GET", "/v1/x402/config"],
};

export function runtimeCases(scenarios) {
  const cases = [];
  for (const [product, [method, path]] of Object.entries(clients)) {
    for (const [name, options, base] of [
      ["default", {}, "https://api.inflowpay.ai"],
      ["production", { environment: "production" }, "https://api.inflowpay.ai"],
      ["sandbox", { environment: "sandbox" }, "https://sandbox.inflowpay.ai"],
      [
        "override",
        { environment: "sandbox", base_url: "http://127.0.0.1:1234/prefix/" },
        "http://127.0.0.1:1234/prefix",
      ],
    ])
      cases.push({
        id: `${product}.environment.${name}`,
        suite: "runtime",
        operation: "runtime.environment",
        input: { product, api_key: "test-only-key", ...options },
        expect: { result: { destinations: [`${method} ${base}${path}`] } },
      });
    const request = (headers) => ({
      method,
      path,
      headers,
      ...(method === "POST"
        ? {
            json: {
              challenge: {
                id: "test",
                realm: "seller.example",
                method: "inflow",
                intent: "charge",
                request: Buffer.from(
                  JSON.stringify({ amount: "1", currency: "USD" }),
                ).toString("base64url"),
              },
              options: {},
            },
          }
        : {}),
    });
    const add = (id, input, exchanges, response) => {
      const entry = response.json?.errors?.[0];
      cases.push({
        id: `${product}.${id}`,
        suite: "runtime",
        operation: "runtime.request",
        input: { product, ...input },
        platform: { exchanges },
        expect: {
          result: {
            code: entry?.code ?? "UNEXPECTED_ERROR",
            message: entry?.message ?? "request failed",
            http_status: response.status,
            endpoint: path,
            token_calls: input.tokens?.length ?? 0,
            request_id: response.headers?.["x-request-id"] ?? "",
            sensitive_headers: [],
          },
        },
      });
    };
    for (const [id, scenario] of Object.entries(scenarios)) {
      if (!id.startsWith("auth.")) continue;
      const { request: original, response } = scenario.exchanges[0];
      if (response.status < 400) continue;
      if (product.endsWith("seller") && !original.headers["x-api-key"])
        continue;
      if (id.startsWith("auth.seller-required") && !product.endsWith("seller"))
        continue;
      const headers = original.headers;
      const input = headers.authorization
        ? { tokens: [headers.authorization.slice(7)] }
        : headers["x-api-key"]
          ? { api_key: headers["x-api-key"] }
          : {};
      add(id, input, [{ request: request(headers), response }], response);
    }
    for (const status of [
      301, 302, 303, 307, 308, 400, 401, 403, 404, 409, 412, 500,
    ]) {
      const response = {
        status,
        headers: {
          location: "/must-not-follow",
          "x-request-id": "test-request",
          "set-cookie": "test-only-secret",
        },
      };
      add(
        `http.${status}`,
        { api_key: "test-only-key" },
        [{ request: request({ "x-api-key": "test-only-key" }), response }],
        response,
      );
    }
    // Retry permission belongs to each public operation, not to GET in general.
    const retry = product === "mpp-seller";
    const transient = { status: 503 };
    const unauthorized = { status: 401 };
    const exchanges = [
      {
        request: request({ "x-api-key": "test-only-key" }),
        response: transient,
      },
    ];
    if (retry)
      exchanges.push({
        request: request({ "x-api-key": "test-only-key" }),
        response: unauthorized,
      });
    add(
      "retry-policy",
      { api_key: "test-only-key" },
      exchanges,
      retry ? unauthorized : transient,
    );
  }
  return { cases };
}
