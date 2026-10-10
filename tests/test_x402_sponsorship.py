import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from x402.schemas import PaymentPayload, PaymentRequired

from inflowpay import ClientOptions
from inflowpay.x402.eip7702 import Authorization, SponsorshipExtension

# Generated with Node's viem encoder and EntryPoint 0.7 hash implementation.
VECTOR: dict[str, Any] = json.loads(
    Path(__file__).with_name("fixtures").joinpath("eip7702.json").read_text()
)


class Signer:
    def __init__(self) -> None:
        self.account = Account.from_key("0x" + "01" * 32)
        self.address = self.account.address
        self.available = 0
        self.calls: list[str] = []
        self.wrong_message = False
        self.wrong_authorization = False

    async def allowance(self, token: str, owner: str, spender: str) -> int:
        self.calls.append("allowance")
        assert token == VECTOR["payment"]["accepted"]["asset"].lower()
        assert owner == self.address.lower()
        assert spender == "0x000000000022d473030f116ddee9f6b43ac78ba3"
        return self.available

    async def sign_message(self, operation_hash: bytes) -> bytes:
        self.calls.append("message")
        account = Account.from_key("0x" + "02" * 32) if self.wrong_message else self.account
        return bytes(account.sign_message(encode_defunct(operation_hash)).signature)

    async def sign_authorization(self, authorization: Authorization) -> bytes:
        self.calls.append("authorization")
        account = Account.from_key("0x" + "02" * 32) if self.wrong_authorization else self.account
        # eth-account's LocalAccount annotation says SignedMessage, but Account's
        # authorization method returns the actual EIP-7702 SignedSetCodeAuthorization.
        signed = Account.sign_authorization(
            {
                "address": authorization.address,
                "chainId": authorization.chain_id,
                "nonce": authorization.nonce,
            },
            account.key,
        )
        return bytes(signed.r.to_bytes(32) + signed.s.to_bytes(32) + bytes([signed.y_parity + 27]))


class Fixture:
    def __init__(self) -> None:
        self.payment = PaymentPayload.model_validate(deepcopy(VECTOR["payment"]))
        self.required = PaymentRequired.model_validate(deepcopy(VECTOR["required"]))
        self.prepared = deepcopy(VECTOR["prepared"])
        self.signer = Signer()
        self.consented = True
        self.requests: list[httpx.Request] = []
        self.extension = SponsorshipExtension(
            ClientOptions(environment="sandbox", transport=httpx.MockTransport(self.respond)),
            self.signer,
            self.consent,
        )

    async def consent(self, authorization: Authorization) -> bool:
        self.signer.calls.append("consent")
        assert authorization.chain_id == 8453
        return self.consented

    def respond(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert str(request.url) == "https://sandbox.inflowpay.ai/v1/x402/eip7702/prepare"
        assert request.method == "POST"
        assert "authorization" not in request.headers
        assert "x-api-key" not in request.headers
        body = json.loads(request.content)
        assert body["paymentPayload"] == self.payment.model_dump(by_alias=True, exclude_none=True)
        assert body["paymentRequirements"] == self.payment.accepted.model_dump(
            by_alias=True, exclude_none=True
        )
        return httpx.Response(200, json=self.prepared)


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("inflowpay.x402.eip7702.time.time", lambda: 2000000000)


@pytest.mark.parametrize("already_delegated", [False, True])
async def test_node_vector_and_real_signatures(already_delegated: bool) -> None:
    fixture = Fixture()
    if already_delegated:
        del fixture.prepared["authorization"]
    before = fixture.payment.model_copy(deep=True)
    async with fixture.extension:
        result = await fixture.extension.enrich_payment_payload(fixture.payment, fixture.required)
    assert fixture.payment == before
    assert result.extensions is not None
    info = result.extensions[fixture.extension.key]["info"]
    assert info["signature"] == VECTOR["operationSignature"]
    assert info["sponsorshipId"] == VECTOR["prepared"]["sponsorshipId"]
    if already_delegated:
        assert "authorizationSignature" not in info
        assert fixture.signer.calls == ["allowance", "message"]
    else:
        # Same delegation signed independently by Node's viem account.
        assert (
            info["authorizationSignature"]
            == "0x4dfa6bbf52578eae02c4879a2c8a2fd9cad9eed0a685f34d7a1cd6562e25ba833"
            "0e75be28dc48ef292a00dc9bc7262c5c27276b3d805aa70e53d20741fac51001c"
        )
        assert fixture.signer.calls == ["allowance", "consent", "authorization", "message"]


async def test_upstream_signer_and_registered_sponsorship(monkeypatch: pytest.MonkeyPatch) -> None:
    from x402.mechanisms.evm.exact import ExactEvmScheme

    from inflowpay.x402.buyer import Buyer

    fixture = Fixture()
    fixture.payment.resource = fixture.required.resource
    monkeypatch.setattr("x402.mechanisms.evm.exact.permit2_utils.create_permit2_nonce", lambda: "7")
    platform = httpx.MockTransport(lambda request: httpx.Response(200, json={"kinds": []}))
    async with (
        fixture.extension,
        await Buyer.create(ClientOptions(transport=platform)) as buyer,
    ):
        buyer.register("eip155:8453", ExactEvmScheme(fixture.signer.account))
        buyer.register_extension(fixture.extension)
        result = await buyer.create_payment_payload(fixture.required)
    assert isinstance(result, PaymentPayload)
    assert result.payload == fixture.payment.payload
    assert result.extensions is not None
    assert (
        result.extensions[fixture.extension.key]["info"]["signature"]
        == VECTOR["operationSignature"]
    )
    assert len(fixture.requests) == 1


@pytest.mark.parametrize(
    "reason", ["absent", "other-scheme", "not-permit2", "sufficient-allowance"]
)
async def test_no_sponsorship_when_unnecessary(reason: str) -> None:
    fixture = Fixture()
    if reason == "absent":
        fixture.required.extensions = None
    elif reason == "other-scheme":
        fixture.payment.accepted.scheme = "upto"
    elif reason == "not-permit2":
        fixture.payment.accepted.extra = {}
    else:
        fixture.signer.available = 123
    async with fixture.extension:
        assert (
            await fixture.extension.enrich_payment_payload(fixture.payment, fixture.required)
            is fixture.payment
        )
    assert not fixture.requests


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("sponsorshipId",), "invalid"),
        (("chainId",), 1),
        (("chainId",), True),
        (("chainId",), -1),
        (("chainId",), 9007199254740992),
        (("entryPoint",), "0x" + "00" * 20),
        (("entryPointVersion",), "0.8"),
        (("delegation",), "0x" + "00" * 20),
        (("userOperationHash",), "0x" + "00" * 32),
        (("userOperationHash",), "0x12"),
        (("userOperationHash",), "not-hex"),
        (("expiresAt",), 2000000000),
        (("expiresAt",), 2000000301),
        (("authorization", "nonce"), -1),
        (("authorization", "chainId"), 1),
        (("authorization", "address"), "0x" + "00" * 20),
        (("authorization", "extra"), 1),
        (("userOperation", "extra"), "0x0"),
        (("userOperation", "sender"), "0x" + "00" * 20),
        (("userOperation", "callData"), "0x"),
        (("userOperation", "nonce"), "0x0"),
        (("userOperation", "nonce"), "0x01"),
        (("userOperation", "callGasLimit"), hex(1 << 128)),
        (("userOperation", "callGasLimit"), "0x0"),
        (("userOperation", "verificationGasLimit"), "0x0"),
        (("userOperation", "paymaster"), "0x" + "11" * 20),
        (("userOperation", "paymasterData"), "0x00"),
        (("userOperation", "maxFeePerGas"), "0x1"),
        (("userOperation", "preVerificationGas"), "0x1"),
    ],
)
async def test_untrusted_preparation_rejected_before_signing(
    path: tuple[str, ...], value: object
) -> None:
    fixture = Fixture()
    target = fixture.prepared
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    async with fixture.extension:
        with pytest.raises(ValueError):
            await fixture.extension.enrich_payment_payload(fixture.payment, fixture.required)
    assert fixture.signer.calls == ["allowance"]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("accepted", "network"), "inflow:1"),
        (("accepted", "amount"), "124"),
        (("accepted", "asset"), "invalid"),
        (("payload", "permit2Authorization", "permitted", "amount"), "0"),
        (("payload", "permit2Authorization", "from"), "0x" + "00" * 20),
        (("payload", "permit2Authorization", "deadline"), "2000000000"),
        (("payload", "permit2Authorization", "nonce"), "-1"),
        (("payload", "signature"), "0x11"),
    ],
)
async def test_bad_payment_never_requests_sponsorship(path: tuple[str, ...], value: object) -> None:
    fixture = Fixture()
    data = deepcopy(VECTOR["payment"])
    target = data
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    fixture.payment = PaymentPayload.model_validate(data)
    async with fixture.extension:
        with pytest.raises(ValueError):
            await fixture.extension.enrich_payment_payload(fixture.payment, fixture.required)
    assert not fixture.requests


@pytest.mark.parametrize(
    "reason", ["consent", "allowance", "message", "authorization", "declaration"]
)
async def test_signer_and_consent_failures(reason: str) -> None:
    fixture = Fixture()
    if reason == "consent":
        fixture.consented = False
    elif reason == "allowance":
        fixture.signer.available = -1
    elif reason == "message":
        fixture.signer.wrong_message = True
    elif reason == "authorization":
        fixture.signer.wrong_authorization = True
    else:
        fixture.required.extensions = {fixture.extension.key: {"info": {"version": "2"}}}
    async with fixture.extension:
        with pytest.raises(ValueError):
            await fixture.extension.enrich_payment_payload(fixture.payment, fixture.required)


def test_sponsorship_rejects_platform_credentials() -> None:
    signer = Signer()

    async def consent(_: Authorization) -> bool:
        return True

    with pytest.raises(ValueError, match="anonymous"):
        SponsorshipExtension(ClientOptions(api_key="test-key"), signer, consent)

    async def provider() -> str:
        return "test-key"

    with pytest.raises(ValueError, match="anonymous"):
        SponsorshipExtension(ClientOptions(api_key_provider=provider), signer, consent)
