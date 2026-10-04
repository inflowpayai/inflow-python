from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

ErrorCode = Literal[
    "CONTENT_DIGEST_INVALID",
    "KEY_NOT_FOUND",
    "KEY_RETRIEVAL_FAILED",
    "NONCE_REPLAYED",
    "SIGNATURE_EXPIRED",
    "SIGNATURE_INPUT_INVALID",
    "SIGNATURE_INVALID",
    "SIGNATURE_LIFETIME_INVALID",
    "SIGNATURE_NOT_YET_VALID",
]


class TapVerificationError(Exception):
    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, kw_only=True)
class TapRequest:
    method: str
    url: str
    headers: Mapping[str, str | Sequence[str]]
    body: bytes | str | None = None


@dataclass(frozen=True, kw_only=True)
class TapVerificationFacts:
    keyid: str
    intent: Literal["browse", "pay"]
    nonce: str
    created: int
    expires: int
    covered_components: tuple[str, ...]
    verified: Literal[True] = True
    algorithm: Literal["ed25519"] = "ed25519"


class TapKeyResolver(Protocol):
    async def resolve(self, keyid: str, algorithm: str) -> Ed25519PublicKey | None: ...


class TapReplayStore(Protocol):
    async def claim(self, keyid: str, nonce: str, expires: int) -> bool: ...
