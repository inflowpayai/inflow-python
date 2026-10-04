import time
from collections.abc import Awaitable, Callable
from threading import Lock
from types import TracebackType
from typing import Self, TypeVar

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ._keys import VisaTapKeyResolver
from ._signature import parse_input, required_header, signature_base, signature_bytes
from ._types import (
    TapKeyResolver,
    TapReplayStore,
    TapRequest,
    TapVerificationError,
    TapVerificationFacts,
)

__all__ = [
    "MemoryTapReplayStore",
    "TapKeyResolver",
    "TapReplayStore",
    "TapRequest",
    "TapVerificationError",
    "TapVerificationFacts",
    "TapVerifier",
    "VisaTapKeyResolver",
]
_T = TypeVar("_T")


class MemoryTapReplayStore:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._claims: dict[tuple[str, str], int] = {}
        self._lock = Lock()

    async def claim(self, keyid: str, nonce: str, expires: int) -> bool:
        with self._lock:
            now = self._clock()
            self._claims = {pair: end for pair, end in self._claims.items() if end > now}
            pair = (keyid, nonce)
            if pair in self._claims:
                return False
            self._claims[pair] = expires
            return True


class TapVerifier:
    def __init__(
        self,
        *,
        key_resolver: TapKeyResolver | None = None,
        replay_store: TapReplayStore | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._owned_resolver: VisaTapKeyResolver | None = None
        if key_resolver is None:
            self._owned_resolver = VisaTapKeyResolver(clock=clock)
            key_resolver = self._owned_resolver
        self._resolver = key_resolver
        self._store = replay_store if replay_store is not None else MemoryTapReplayStore(clock)
        self._clock = clock
        self._closed = False

    async def __aenter__(self) -> Self:
        self._check_open()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self._closed = True
        if self._owned_resolver is not None:
            await self._owned_resolver.aclose()

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("TAP verifier is closed")

    async def verify(self, request: TapRequest) -> TapVerificationFacts:
        self._check_open()
        value = required_header(request.headers, "signature-input")
        signature = required_header(request.headers, "signature")
        parsed = parse_input(value)
        # Freeze signed bytes before awaiting caller-supplied code. Expiration is checked here,
        # not after key retrieval or replay storage; a request may expire during those operations.
        base = signature_base(request, parsed, self._clock())
        key = await self._resolver.resolve(parsed.keyid, "ed25519")
        if key is None:
            raise TapVerificationError("KEY_NOT_FOUND", "The TAP verification key was not found.")
        if not isinstance(key, Ed25519PublicKey):
            raise TapVerificationError(
                "SIGNATURE_INVALID", "The TAP verification key is not Ed25519."
            )
        try:
            key.verify(signature_bytes(signature), base)
        except InvalidSignature as error:
            raise TapVerificationError(
                "SIGNATURE_INVALID", "The TAP signature is invalid."
            ) from error
        if not await self._store.claim(parsed.keyid, parsed.nonce, parsed.expires):
            raise TapVerificationError("NONCE_REPLAYED", "The TAP nonce has already been used.")
        return TapVerificationFacts(
            keyid=parsed.keyid,
            intent="pay" if parsed.tag == "agent-payer-auth" else "browse",
            nonce=parsed.nonce,
            created=parsed.created,
            expires=parsed.expires,
            covered_components=parsed.components,
        )

    async def with_verified(
        self,
        request: TapRequest,
        handler: Callable[[TapVerificationFacts], Awaitable[_T]],
    ) -> _T:
        return await handler(await self.verify(request))
