"""Verify distribution files in isolated consumers outside the checkout."""

import argparse
import os
import shutil
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
for name in ('mpp', 'x402', 'fastapi', 'mcp', 'web3', 'solana', 'cryptography'):
    assert util.find_spec(name) is None, name
"""

OPTIONAL_CONSUMER = """
import sys
import inflowpay
assert not any(name in sys.modules for name in
               ('mpp', 'x402', 'fastapi', 'mcp', 'web3', 'cryptography'))
from mpp.extensions.mcp import McpClient
from x402.http.middleware.fastapi import payment_middleware
from x402.mechanisms.evm.exact import ExactEvmClientScheme
from x402.mechanisms.svm.exact import ExactSvmClientScheme
from inflowpay.x402.eip7702 import SponsorshipExtension, SponsorshipSigner
import x402.mcp
from inflowpay.tap.seller import TapVerifier
"""

TAP_CONSUMER = """
import asyncio, base64, sys
from importlib import util
import inflowpay
assert "cryptography" not in sys.modules
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from inflowpay.tap.seller import TapRequest, TapVerifier, TapVerificationError
key = Ed25519PrivateKey.from_private_bytes(b"\\x11" * 32)
parameters = ('("@method" "@authority" "@path" "@query");created=1800000000;'
              'expires=1800000300;keyid="test";alg="ed25519";nonce="one";tag="agent-browser-auth"')
base = ('"@method": GET\\n"@authority": merchant.example\\n"@path": /catalog\\n'
        '"@query": ?\\n"@signature-params": ' + parameters)
signature = base64.b64encode(key.sign(base.encode())).decode()
request = TapRequest(method="GET", url="https://merchant.example/catalog", headers={
    "signature-input": "sig2=" + parameters, "signature": "sig2=:" + signature + ":"})
class Resolver:
    async def resolve(self, keyid, algorithm):
        assert keyid == "test" and algorithm == "ed25519"
        return key.public_key()
async def check():
    async with TapVerifier(key_resolver=Resolver(), clock=lambda: 1800000000) as verifier:
        facts = await verifier.verify(request)
        assert facts.verified and facts.intent == "browse"
        try:
            await verifier.verify(request)
        except TapVerificationError as error:
            assert error.code == "NONCE_REPLAYED"
        else:
            raise AssertionError("Replay accepted")
asyncio.run(check())
for name in ("mpp", "x402", "mcp", "web3", "solana", "fastapi", "rfc8785"):
    assert util.find_spec(name) is None, name
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
async def check_card_buyer():
    import json
    key = dict(kty='RSA', alg='RSA-OAEP-256', use='enc', kid='test', n='test', e='AQAB')
    wire = dict(id='test', realm='seller.example', method='card', intent='charge',
        description='Report', request=encode(dict(amount='125', currency='usd', recipient='seller',
        methodDetails=dict(merchantName='Seller', acceptedNetworks=['visa'], encryptionJwk=key))))
    payload = dict(encryptedPayload='test-only', network='visa', panLastFour='4242',
        panExpirationMonth='12', panExpirationYear='2030')
    merchant = dict(name='Seller', url='https://seller.example', countryCode='US')
    def respond(request):
        assert json.loads(request.content) == dict(challenge=wire, options=dict(merchant=merchant))
        credential = encode(dict(challenge=wire, payload=payload))
        return httpx.Response(200, json=dict(state='ready', credential=credential))
    async with BuyerMethod(ClientOptions(transport=httpx.MockTransport(respond)),
                           method='card', merchant=merchant) as buyer:
        credential = await buyer.create_credential(to_pympp_challenge(wire))
        assert decode(credential.to_authorization()[8:]) == dict(challenge=wire, payload=payload)
asyncio.run(check_card_buyer())
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
from inflowpay.x402.buyer import Buyer
from inflowpay.x402.facilitator import Facilitator
from inflowpay.x402.seller import Seller
from inflowpay import ClientOptions
import asyncio
import httpx
async def check_buyer():
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={'kinds': []}))
    async with await Buyer.create(ClientOptions(transport=transport)) as buyer:
        assert (await buyer.get_supported()).kinds == []
asyncio.run(check_buyer())
async def check_seller():
    config = {'sellerId': 'seller', 'assets': [], 'wallets': [],
              'paymentMethods': [], 'supported': []}
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=
        config if request.url.path.endswith('/config') else {'kinds': []}))
    options = ClientOptions(api_key='test-key', transport=transport)
    async with await Seller.create(options) as seller:
        assert await seller.offers('$1') == []
        assert await seller.scheme_registrations() == []
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={'kinds': []}))
    options = ClientOptions(transport=transport)
    async with await Facilitator.create(options, anonymous=True) as facilitator:
        assert facilitator.get_supported().kinds == []
asyncio.run(check_seller())
for name in ('mpp', 'mcp', 'web3', 'solana', 'fastapi', 'rfc8785'):
    assert util.find_spec(name) is None, name
"""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dist-dir", type=Path)
    arguments = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="inflowpay-consumer-") as directory:
        temporary = Path(directory)
        output = arguments.dist_dir.resolve() if arguments.dist_dir else temporary / "dist"
        if arguments.dist_dir is None:
            subprocess.run(
                [sys.executable, "-m", "build", "--outdir", str(output), str(repository)],
                check=True,
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
        tap_environment = temporary / "tap-venv"
        subprocess.run(["uv", "venv", "--python", sys.executable, str(tap_environment)], check=True)
        tap_python = tap_environment / (
            "Scripts/python.exe" if sys.platform == "win32" else "bin/python"
        )
        subprocess.run(
            ["uv", "pip", "install", "--python", str(tap_python), f"{wheels[0]}[tap]"], check=True
        )
        subprocess.run([str(tap_python), "-I", "-c", TAP_CONSUMER], cwd=temporary, check=True)
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
                f"{wheels[0]}[evm,fastapi,mcp,mpp,svm,tap,x402]",
            ],
            check=True,
        )
        subprocess.run([str(python), "-I", "-c", OPTIONAL_CONSUMER], cwd=temporary, check=True)
        subprocess.run(
            ["uv", "pip", "install", "--python", str(python), "uvicorn==0.54.0"], check=True
        )
        shutil.copytree(
            repository / "examples",
            temporary / "examples",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        settings = dict(os.environ)
        for key in (
            "INFLOW_API_KEY",
            "MPP_SECRET_KEY",
            "INFLOW_BASE_URL",
            "TARGET_URL",
            "PUBLIC_ORIGIN",
        ):
            settings.pop(key, None)
        for name in ("mpp_buyer", "mpp_seller", "x402_buyer", "x402_seller", "tap_seller"):
            result = subprocess.run(
                [str(python), "-I", str(temporary / "examples" / f"{name}.py")],
                cwd=temporary,
                env=settings,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 1, result
            assert (
                "PUBLIC_ORIGIN" if name == "tap_seller" else "INFLOW_API_KEY"
            ) in result.stderr, result.stderr
            assert "Traceback" not in result.stderr, result.stderr


if __name__ == "__main__":
    main()
