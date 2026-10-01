"""Build from source and verify the wheel outside the checkout."""

import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

CONSUMER = """
from importlib import metadata, util
from importlib.resources import files
import inflowpay
assert inflowpay.__version__ == metadata.version('inflowpay')
assert files('inflowpay').joinpath('py.typed').is_file()
for name in ('mpp', 'x402', 'fastapi', 'mcp', 'web3', 'solana'):
    assert util.find_spec(name) is None, name
"""

OPTIONAL_CONSUMER = """
import sys
import inflowpay
assert not any(name in sys.modules for name in ('mpp', 'x402', 'fastapi', 'mcp', 'web3'))
from mpp.extensions.mcp import McpClient
from x402.http.middleware.fastapi import payment_middleware
from x402.mechanisms.evm.exact import ExactEvmClientScheme
from x402.mechanisms.svm.exact import ExactSvmClientScheme
import x402.mcp
"""

MPP_CONSUMER = """
from importlib import util
from inflowpay.mpp import encode, decode, to_pympp_challenge, from_pympp_challenge
from inflowpay.mpp.buyer import BuyerMethod, payment_transport
from inflowpay.mpp.seller import Seller
from inflowpay import ClientOptions
import asyncio
import httpx
async def check_buyer():
    async with BuyerMethod(ClientOptions()) as buyer:
        transport = payment_transport([buyer])
        await transport.aclose()
asyncio.run(check_buyer())
async def check_seller():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
        'sellerId': '11111111-1111-4111-8111-111111111111',
        'featureFlags': {},
        'supportedMethods': [{'id': 'inflow', 'methodDetails': {
            'currencyRails': {'USDC': {'rail': 'balance'}}}}],
    }))
    options = ClientOptions(api_key='test-key', transport=transport)
    async with await Seller.create(options) as seller:
        assert seller.charge_request({'amount': '0.50', 'currency': 'USDC'})['amount'] == '0.50'
asyncio.run(check_seller())
wire = dict(id='test', realm='seller.example', method='inflow', intent='charge',
            request=encode({'amount': '1'}))
assert from_pympp_challenge(to_pympp_challenge(wire)) == wire
assert decode(wire['request']) == {'amount': '1'}
for name in ('x402', 'mcp', 'web3', 'solana', 'fastapi'):
    assert util.find_spec(name) is None, name
"""

X402_CONSUMER = """
from importlib import util
from inflowpay.x402 import (
    PaymentRequirements, declare_payment_identifier, generate_payment_id,
    payment_identifier_entry,
)
from x402.schemas import PaymentRequirements as UpstreamRequirements
assert PaymentRequirements is UpstreamRequirements
entry = payment_identifier_entry(declare_payment_identifier(), generate_payment_id())
assert entry is not None and entry['info']['required'] is False
for name in ('mpp', 'mcp', 'web3', 'solana', 'fastapi', 'rfc8785'):
    assert util.find_spec(name) is None, name
"""


def main() -> None:
    repository = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="inflowpay-consumer-") as directory:
        temporary = Path(directory)
        output = temporary / "dist"
        subprocess.run(
            [sys.executable, "-m", "build", "--outdir", str(output), str(repository)], check=True
        )
        artifacts = sorted(output.iterdir())
        subprocess.run(
            [sys.executable, "-m", "twine", "check", "--strict", *map(str, artifacts)], check=True
        )
        wheels = list(output.glob("*.whl"))
        if len(wheels) != 1:
            raise ValueError("Expected exactly one built wheel")
        with zipfile.ZipFile(wheels[0]) as wheel:
            names = wheel.namelist()
            assert "inflowpay/py.typed" in names
            assert any(name.endswith("/licenses/LICENSE") for name in names)
            assert all(name.startswith(("inflowpay/", "inflowpay-")) for name in names)
        environment = temporary / "venv"
        subprocess.run(["uv", "venv", "--python", sys.executable, str(environment)], check=True)
        python = environment / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python), str(wheels[0])], check=True
        )
        subprocess.run([str(python), "-I", "-c", CONSUMER], cwd=temporary, check=True)
        x402_environment = temporary / "x402-venv"
        subprocess.run(
            ["uv", "venv", "--python", sys.executable, str(x402_environment)], check=True
        )
        x402_python = x402_environment / (
            "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
        )
        subprocess.run(
            ["uv", "pip", "install", "--python", str(x402_python), f"{wheels[0]}[x402]"],
            check=True,
        )
        subprocess.run([str(x402_python), "-I", "-c", X402_CONSUMER], cwd=temporary, check=True)
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python), f"{wheels[0]}[mpp]"], check=True
        )
        subprocess.run([str(python), "-I", "-c", MPP_CONSUMER], cwd=temporary, check=True)
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                f"{wheels[0]}[evm,fastapi,mcp,mpp,svm,x402]",
            ],
            check=True,
        )
        subprocess.run([str(python), "-I", "-c", OPTIONAL_CONSUMER], cwd=temporary, check=True)


if __name__ == "__main__":
    main()
