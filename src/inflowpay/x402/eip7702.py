from __future__ import annotations

import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Protocol, Self

from eth_abi.abi import encode
from eth_account.typed_transactions.set_code_transaction import Authorization as EvmAuthorization
from eth_keys.datatypes import Signature
from eth_utils.crypto import keccak
from x402.schemas import PaymentPayload, PaymentRequired

from .._runtime import Client
from ..options import ClientOptions
from ._core import INFLOW_EIP7702_GAS_SPONSORING
from ._payment import response_object

PERMIT2 = "0x000000000022d473030f116ddee9f6b43ac78ba3"
PROXY = "0x402085c248eea27d92e8b30b2c58ed07f9e20001"
DELEGATION = "0x77021100bd87b7008e5e1989d0eb38555d0d0000"
ENTRY_POINT = "0x0000000071727de22e5e9d8baf0edac6f37da032"


@dataclass(frozen=True)
class Authorization:
    address: str
    chain_id: int
    nonce: int


class SponsorshipSigner(Protocol):
    @property
    def address(self) -> str: ...

    async def allowance(self, token: str, owner: str, spender: str) -> int: ...

    async def sign_message(self, operation_hash: bytes) -> bytes:
        """Sign the hash using Ethereum's personal-message prefix."""
        ...

    async def sign_authorization(self, authorization: Authorization) -> bytes:
        """Return the delegation signature as 65 bytes: r, s, and recovery ID 27 or 28."""
        ...


def _require(condition: bool, detail: str) -> None:
    if not condition:
        raise ValueError(f"Invalid EIP-7702 {detail}")


def _address(value: object) -> str:
    _require(
        isinstance(value, str) and re.fullmatch(r"0x[0-9a-fA-F]{40}", value) is not None, "address"
    )
    return str(value).lower()


def _bytes(value: object, size: int | None = None) -> bytes:
    _require(
        isinstance(value, str) and re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", value) is not None,
        "bytes",
    )
    result = bytes.fromhex(str(value)[2:])
    _require(size is None or len(result) == size, "byte length")
    return result


def _integer(value: object) -> int:
    _require(type(value) is int and 0 <= value <= 9007199254740991, "integer")
    assert isinstance(value, int)
    return value


def _number(value: object, *, hexadecimal: bool = False, bits: int = 256) -> int:
    pattern = r"0x(?:0|[1-9a-f][0-9a-f]*)" if hexadecimal else r"(?:0|[1-9][0-9]*)"
    _require(isinstance(value, str) and re.fullmatch(pattern, value) is not None, "quantity")
    result = int(str(value), 16 if hexadecimal else 10)
    _require(result < 1 << bits, "quantity range")
    return result


def _call(signature: str, types: list[str], values: list[object]) -> bytes:
    return keccak(text=signature)[:4] + encode(types, values)


def _payment_batch(payment: PaymentPayload, owner: str) -> tuple[str, int, int, int, bytes]:
    requirement = payment.accepted
    _require(
        payment.x402_version == 2
        and re.fullmatch(r"eip155:[1-9][0-9]*", requirement.network) is not None,
        "payment network",
    )
    chain_id = _integer(int(requirement.network[7:]))
    authorization = response_object(payment.payload.get("permit2Authorization"))
    permitted = response_object(authorization.get("permitted"))
    witness = response_object(authorization.get("witness"))
    asset = _address(permitted.get("token"))
    amount = _number(permitted.get("amount"))
    deadline = _number(authorization.get("deadline"))
    _require(
        amount > 0
        and amount == _number(requirement.amount)
        and asset == _address(requirement.asset)
        and _address(authorization.get("from")) == owner
        and _address(authorization.get("spender")) == PROXY
        and _address(requirement.extra.get("permit2Proxy")) == PROXY
        and _address(witness.get("to")) == _address(requirement.pay_to),
        "payment authorization",
    )
    _require(deadline > int(time.time()), "payment deadline")
    settle = _call(
        "settle(((address,uint256),uint256,uint256),address,(address,uint256),bytes)",
        ["((address,uint256),uint256,uint256)", "address", "(address,uint256)", "bytes"],
        [
            ((asset, amount), _number(authorization.get("nonce")), deadline),
            owner,
            (_address(witness.get("to")), _number(witness.get("validAfter"))),
            _bytes(payment.payload.get("signature"), 65),
        ],
    )
    approve = _call("approve(address,uint256)", ["address", "uint256"], [PERMIT2, amount])
    batch = _call(
        "executeBatch((address,uint256,bytes)[])",
        ["(address,uint256,bytes)[]"],
        [[(asset, 0, approve), (PROXY, 0, settle)]],
    )
    return asset, amount, deadline, chain_id, batch


def _operation_hash(value: object, owner: str, batch: bytes, chain_id: int) -> bytes:
    operation = response_object(value)
    _require(
        set(operation)
        == {
            "sender",
            "nonce",
            "callData",
            "callGasLimit",
            "verificationGasLimit",
            "preVerificationGas",
            "maxFeePerGas",
            "maxPriorityFeePerGas",
            "paymaster",
            "paymasterData",
            "paymasterVerificationGasLimit",
            "paymasterPostOpGasLimit",
        },
        "operation fields",
    )
    _require(
        _address(operation["sender"]) == owner and _bytes(operation["callData"]) == batch,
        "operation payment",
    )
    nonce = _number(operation["nonce"], hexadecimal=True)
    _require(nonce >> 64 == 1, "account nonce key")
    call_gas = _number(operation["callGasLimit"], hexadecimal=True, bits=128)
    verification_gas = _number(operation["verificationGasLimit"], hexadecimal=True, bits=128)
    _require(
        call_gas > 0
        and verification_gas > 0
        and _address(operation["paymaster"]) == "0x" + "00" * 20
        and operation["paymasterData"] == "0x"
        and all(
            operation[key] == "0x0"
            for key in (
                "preVerificationGas",
                "maxFeePerGas",
                "maxPriorityFeePerGas",
                "paymasterVerificationGasLimit",
                "paymasterPostOpGasLimit",
            )
        ),
        "bundler sponsorship profile",
    )
    # InFlow pins EntryPoint 0.7 with empty initCode and paymasterAndData.
    packed = encode(
        ["address", "uint256", "bytes32", "bytes32", "bytes32", "uint256", "bytes32", "bytes32"],
        [
            owner,
            nonce,
            keccak(b""),
            keccak(batch),
            verification_gas.to_bytes(16) + call_gas.to_bytes(16),
            0,
            bytes(32),
            keccak(b""),
        ],
    )
    return keccak(
        encode(["bytes32", "address", "uint256"], [keccak(packed), ENTRY_POINT, chain_id])
    )


def _signature(value: bytes, message_hash: bytes, owner: str) -> str:
    _require(isinstance(value, bytes) and len(value) == 65 and value[64] in (27, 28), "signature")
    signature = Signature(value[:64] + bytes([value[64] - 27]))
    _require(
        signature.recover_public_key_from_msg_hash(message_hash).to_address() == owner,
        "signature owner",
    )
    return "0x" + value.hex()


class SponsorshipExtension:
    key = INFLOW_EIP7702_GAS_SPONSORING
    hooks = None
    transport_hooks = None

    def __init__(
        self,
        options: ClientOptions,
        signer: SponsorshipSigner,
        consent: Callable[[Authorization], Awaitable[bool]],
    ) -> None:
        # This public endpoint is independent of the managed Buyer's account credentials.
        if (
            options.api_key is not None
            or options.api_key_provider is not None
            or options.access_token is not None
        ):
            raise ValueError("EIP-7702 sponsorship uses anonymous ClientOptions")
        self._owner = _address(signer.address)
        self._signer = signer
        self._consent = consent
        self._client = Client(options)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def enrich_payment_payload(
        self, payment_payload: PaymentPayload, payment_required: PaymentRequired
    ) -> PaymentPayload:
        declaration = (payment_required.extensions or {}).get(self.key)
        if (
            declaration is None
            or payment_payload.accepted.scheme != "exact"
            or payment_payload.accepted.extra.get("assetTransferMethod") != "permit2"
        ):
            return payment_payload
        declaration = response_object(declaration)
        info = response_object(declaration.get("info"))
        _require(
            set(declaration) == {"info"} and set(info) == {"version"} and info["version"] == "1",
            "declaration",
        )
        payment = payment_payload.model_copy(deep=True)
        asset, amount, deadline, chain_id, batch = _payment_batch(payment, self._owner)
        allowance = await self._signer.allowance(asset, self._owner, PERMIT2)
        _require(type(allowance) is int and allowance >= 0, "allowance")
        if allowance >= amount:
            return payment_payload
        prepared = response_object(
            await self._client.request(
                "POST",
                "/v1/x402/eip7702/prepare",
                body={
                    "paymentPayload": payment.model_dump(by_alias=True, exclude_none=True),
                    "paymentRequirements": payment.accepted.model_dump(
                        by_alias=True, exclude_none=True
                    ),
                },
            )
        )
        identifier = prepared.get("sponsorshipId")
        _require(
            isinstance(identifier, str)
            and re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", identifier)
            is not None,
            "sponsorship identifier",
        )
        _require(
            _integer(prepared.get("chainId")) == chain_id
            and _address(prepared.get("entryPoint")) == ENTRY_POINT
            and prepared.get("entryPointVersion") == "0.7"
            and _address(prepared.get("delegation")) == DELEGATION,
            "preparation contracts",
        )
        operation_hash = _operation_hash(
            prepared.get("userOperation"), self._owner, batch, chain_id
        )
        _require(_bytes(prepared.get("userOperationHash"), 32) == operation_hash, "operation hash")
        expires = _integer(prepared.get("expiresAt"))

        def check_expiry() -> None:
            _require(int(time.time()) < expires <= deadline, "sponsorship expiry")

        check_expiry()
        signed: dict[str, object] = {"version": "1", "sponsorshipId": identifier}
        if "authorization" in prepared:
            fields = response_object(prepared["authorization"])
            _require(set(fields) == {"address", "chainId", "nonce"}, "authorization fields")
            authorization = Authorization(
                _address(fields["address"]), _integer(fields["chainId"]), _integer(fields["nonce"])
            )
            _require(
                authorization.address == DELEGATION and authorization.chain_id == chain_id,
                "delegation authorization",
            )
            # Delegation persists even if payment execution fails; consent is mandatory.
            _require(await self._consent(authorization), "delegation consent")
            check_expiry()
            authorization_hash = bytes(
                EvmAuthorization(
                    chainId=chain_id,
                    address=bytes.fromhex(DELEGATION[2:]),
                    nonce=authorization.nonce,
                ).hash()
            )
            signed["authorizationSignature"] = _signature(
                await self._signer.sign_authorization(authorization),
                authorization_hash,
                self._owner,
            )
        check_expiry()
        signature = await self._signer.sign_message(operation_hash)
        signed["signature"] = _signature(
            signature, keccak(b"\x19Ethereum Signed Message:\n32" + operation_hash), self._owner
        )
        check_expiry()
        payment.extensions = {**(payment.extensions or {}), self.key: {"info": signed}}
        return payment
