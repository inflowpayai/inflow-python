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
from inflowpay import ClientOptions
import asyncio
async def check_buyer():
    async with BuyerMethod(ClientOptions()) as buyer:
        transport = payment_transport([buyer])
        await transport.aclose()
asyncio.run(check_buyer())
wire = dict(id='test', realm='seller.example', method='inflow', intent='charge',
            request=encode({'amount': '1'}))
assert from_pympp_challenge(to_pympp_challenge(wire)) == wire
assert decode(wire['request']) == {'amount': '1'}
for name in ('x402', 'mcp', 'web3', 'solana', 'fastapi'):
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
