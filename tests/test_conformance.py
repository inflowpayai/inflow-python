import asyncio
import json
import subprocess
import sys
from copy import deepcopy

import pytest
from conformance.adapter import classify, options, respond, runtime_execute


@pytest.mark.parametrize(
    "url",
    [
        "https://api.inflowpay.ai",
        "http://localhost:1234",
        "http://127.0.0.1:1234/path",
        "http://key@127.0.0.1:1234",
        "http://127.0.0.1:1234?query=1",
        "http://127.0.0.1:1234#fragment",
    ],
)
def test_network_destination_is_loopback_only(url: str) -> None:
    with pytest.raises(RuntimeError, match="loopback"):
        options({"base_url": url})


@pytest.mark.parametrize(
    "product,path,method",
    [
        ("mpp-buyer", "/v1/transactions/mpp", "POST"),
        ("mpp-seller", "/v1/mpp/config", "GET"),
        ("x402-buyer", "/v1/transactions/x402-supported", "GET"),
        ("x402-seller", "/v1/x402/config", "GET"),
    ],
)
async def test_environment_uses_public_clients_without_outbound_http(
    product: str,
    path: str,
    method: str,
) -> None:
    for environment, base in [
        ("production", "https://api.inflowpay.ai"),
        ("sandbox", "https://sandbox.inflowpay.ai"),
    ]:
        result = await runtime_execute(
            "runtime.environment",
            {
                "product": product,
                "environment": environment,
                "api_key": "test-only-key",
            },
        )
        assert result == {"destinations": [f"{method} {base}{path}"]}


async def test_adapter_preserves_caller_input_and_reports_unknown_operations() -> None:
    request = {
        "adapter_version": "1",
        "sequence": 1,
        "case_id": "test",
        "operation": "mpp.core.encode",
        "input": {"value": {"amount": "1"}},
    }
    before = deepcopy(request)
    response = await respond(request)
    assert response["result"] == "eyJhbW91bnQiOiIxIn0"
    assert request == before
    request["operation"] = "unknown"
    assert (await respond(request))["error"]["code"] == "ADAPTER_ERROR"
    request["adapter_version"] = "2"
    assert (await respond(request))["error"]["message"] == "Unsupported adapter version"


def test_unknown_errors_are_not_payment_outcomes() -> None:
    for operation in ("x402.buyer.sign", "x402.seller.offers", "x402.seller.route"):
        for error in (RuntimeError("bug"), ValueError("bug"), asyncio.CancelledError()):
            with pytest.raises(type(error)):
                classify(error, operation, {})


def test_json_lines_process() -> None:
    request = {
        "adapter_version": "1",
        "sequence": 1,
        "case_id": "test",
        "operation": "x402.core.identifier-valid",
        "input": {"value": "short"},
    }
    result = subprocess.run(
        [sys.executable, "-m", "conformance.adapter"],
        input=json.dumps(request) + "\n",
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    assert json.loads(result.stdout) == {
        "adapter_version": "1",
        "sequence": 1,
        "case_id": "test",
        "result": False,
    }
    assert not result.stderr
