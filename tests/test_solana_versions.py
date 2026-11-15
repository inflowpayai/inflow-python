import base64
import json
from typing import Any

import pytest
from solders.hash import Hash
from solders.keypair import Keypair
from solders.message import to_bytes_versioned
from solders.signature import Signature
from solders.transaction import VersionedTransaction
from x402.mechanisms.svm.constants import TOKEN_PROGRAM_ADDRESS
from x402.mechanisms.svm.exact import ExactSvmScheme
from x402.mechanisms.svm.rpc import SvmRpcClient
from x402.mechanisms.svm.signers import KeypairSigner
from x402.mechanisms.svm.utils import get_transaction_version
from x402.schemas import PaymentPayload, PaymentRequired, PaymentRequirements, SupportedKind

from inflowpay import ClientOptions
from inflowpay.x402._seller import SellerConfig, build_offers
from inflowpay.x402.buyer import Buyer
from test_x402_buyer import REQUIREMENT, RESOURCE, Platform
from test_x402_seller import CONFIG


@pytest.mark.parametrize("versions", [None, [0], [1], [0, 1]])
async def test_managed_buyer_forwards_transaction_versions(versions: list[int] | None) -> None:
    platform = Platform()
    network = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
    platform.supported = {"kinds": [{"scheme": "exact", "network": network, "x402Version": 2}]}
    requirement = REQUIREMENT.model_copy(
        update={
            "scheme": "exact",
            "network": network,
            "extra": {
                "feePayer": "payer",
                **({} if versions is None else {"transactionVersions": versions}),
            },
        },
        deep=True,
    )
    before = requirement.model_copy(deep=True)
    async with await Buyer.create(ClientOptions(api_key="test", transport=platform)) as buyer:
        await buyer.prepare(requirement, RESOURCE)
    submitted = next(
        request for request in platform.requests if request.url.path == "/v1/transactions/x402"
    )
    assert json.loads(submitted.content)["accept"] == requirement.model_dump(
        by_alias=True, exclude_none=True
    )
    assert requirement == before


@pytest.mark.parametrize("versions", [None, [0], [1], [0, 1]])
@pytest.mark.parametrize("present", [False, True])
def test_seller_only_advertises_matching_exact_versions(
    versions: list[int] | None, present: bool
) -> None:
    config = SellerConfig.model_validate(CONFIG)
    network = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"
    config.assets.append(
        config.assets[0].model_copy(update={"network": network, "blockchain": "Solana"})
    )
    config.wallets.append(
        config.wallets[0].model_copy(update={"blockchain": "Solana", "fee_payer": "payer"})
    )
    config.supported = [kind for kind in config.supported if kind.network != network]
    config.supported.insert(
        0,
        SupportedKind(
            x402_version=2, scheme="upto", network=network, extra={"transactionVersions": [99]}
        ),
    )
    if present:
        config.supported.append(
            SupportedKind(
                x402_version=2,
                scheme="exact",
                network=network,
                extra={} if versions is None else {"transactionVersions": versions},
            )
        )
    before = config.model_copy(deep=True)
    offers = build_offers(config, "1 USDC")
    matched = [offer for offer in offers if offer.network == network]
    assert matched
    for offer in offers:
        assert (offer.extra or {}).get("transactionVersions") == (
            versions if present and offer.network == network else None
        )
    assert config == before


@pytest.mark.parametrize(
    "versions,client_versions,expected",
    [
        (None, None, 0),
        ([0], None, 0),
        ([1], None, 1),
        ([0, 1], None, 1),
        ([0, 1], [0], 0),
        ([1], [0], None),
        ([], None, None),
        ([2], None, None),
        ("1", None, None),
    ],
)
async def test_external_solana_builds_and_signs_negotiated_version(
    versions: Any,
    client_versions: list[int] | None,
    expected: int | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wallet = Keypair.from_seed(bytes([1]) * 32)
    fee_payer = Keypair.from_seed(bytes([2]) * 32).pubkey()
    recipient = Keypair.from_seed(bytes([3]) * 32).pubkey()
    mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
    calls: list[str] = []

    def rpc(_: SvmRpcClient, method: str, params: list[Any]) -> str:
        calls.append(method)
        assert method == "getAccountInfo"
        assert params[0] == mint
        data = bytearray(82)
        data[44] = 6
        return json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {
                    "context": {"slot": 1},
                    "value": {
                        "data": [base64.b64encode(data).decode(), "base64"],
                        "executable": False,
                        "lamports": 1,
                        "owner": TOKEN_PROGRAM_ADDRESS,
                        "rentEpoch": 0,
                    },
                },
            }
        )

    # Only RPC account data is synthetic; upstream builds and signs the transaction.
    monkeypatch.setattr(SvmRpcClient, "_call", rpc)
    requirement = PaymentRequirements(
        scheme="exact",
        network="solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
        asset=mint,
        amount="100000",
        pay_to=str(recipient),
        max_timeout_seconds=60,
        extra={
            "feePayer": str(fee_payer),
            "recentBlockhash": str(Hash.default()),
            **({} if versions is None else {"transactionVersions": versions}),
        },
    )
    scheme = ExactSvmScheme(KeypairSigner(wallet), transaction_versions=client_versions)
    platform = Platform()
    platform.supported = {"kinds": []}
    try:
        async with await Buyer.create(ClientOptions(api_key="test", transport=platform)) as buyer:
            buyer.register(requirement.network, scheme)
            required = PaymentRequired(accepts=[requirement], resource=RESOURCE)
            if expected is None:
                with pytest.raises(ValueError, match="unsupported_transaction_version"):
                    await buyer.create_payment_payload(required)
                assert not calls
                return
            result = await buyer.create_payment_payload(required)
            assert isinstance(result, PaymentPayload)
            transaction = VersionedTransaction.from_bytes(
                base64.b64decode(result.payload["transaction"])
            )
            assert get_transaction_version(transaction.message) == expected
            assert transaction.signatures[0] == Signature.default()
            assert transaction.signatures[1].verify(
                wallet.pubkey(), to_bytes_versioned(transaction.message)
            )
            assert calls == ["getAccountInfo"]
    finally:
        for client in scheme._clients.values():
            client.close()
