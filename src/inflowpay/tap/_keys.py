import asyncio
import base64
import re
import time
from collections.abc import Callable
from types import TracebackType
from typing import Self

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ._types import TapVerificationError


class VisaTapKeyResolver:
    def __init__(
        self,
        *,
        url: str = "https://mcp.visa.com/.well-known/jwks",
        cache_ttl: float = 3600,
        cache_max_age: float = 86400,
        timeout: float = 3,
        clock: Callable[[], float] = time.time,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = url
        self._ttl, self._max_age, self._clock = cache_ttl, cache_max_age, clock
        self._timeout = timeout
        self._client = httpx.AsyncClient(transport=transport, follow_redirects=False, timeout=None)
        self._cache: dict[str, Ed25519PublicKey] = {}
        self._missing: set[str] = set()
        self._updated: float | None = None
        self._task: asyncio.Task[None] | None = None
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
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        await self._client.aclose()

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("TAP key resolver is closed")

    async def resolve(self, keyid: str, algorithm: str) -> Ed25519PublicKey | None:
        self._check_open()
        if algorithm != "ed25519":
            return None
        if (
            self._updated is not None
            and self._clock() - self._updated <= self._ttl
            and (keyid in self._cache or keyid in self._missing)
        ):
            return self._cache.get(keyid)
        try:
            if self._task is None:
                self._task = asyncio.create_task(self._load())
                self._task.add_done_callback(self._finish)
            # wait() preserves shared retrieval when a caller cancels. Unlike Python 3.14's
            # shield(), it does not log late failures that _finish already observes.
            task = self._task
            await asyncio.wait((task,))
            task.result()
        except Exception as error:
            if self._updated is not None and self._clock() - self._updated <= self._max_age:
                cached = self._cache.get(keyid)
                if cached is not None:
                    return cached
            raise TapVerificationError(
                "KEY_RETRIEVAL_FAILED", "The TAP verification key could not be retrieved."
            ) from error
        resolved = self._cache.get(keyid)
        if resolved is None:
            self._missing.add(keyid)
        return resolved

    def _finish(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()
        self._task = None

    async def _load(self) -> None:
        async with asyncio.timeout(self._timeout):
            response = await self._client.get(self._url, headers={"accept": "application/json"})
            response.raise_for_status()
            payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("keys", []), list):
            raise ValueError("Key service returned an invalid key set")
        replacement: dict[str, Ed25519PublicKey] = {}
        for key in payload.get("keys", []):
            if not (
                isinstance(key, dict)
                and isinstance(key.get("kid"), str)
                and key.get("alg") in ("ed25519", "Ed25519")
                and key.get("kty") == "OKP"
                and key.get("crv") == "Ed25519"
                and key.get("use", "sig") == "sig"
            ):
                continue
            if key["kid"] in replacement:
                raise ValueError("Key service returned a duplicate key identifier")
            encoded = key.get("x")
            if not isinstance(encoded, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", encoded) is None:
                raise ValueError("Key service returned an invalid Ed25519 key")
            replacement[key["kid"]] = Ed25519PublicKey.from_public_bytes(
                base64.urlsafe_b64decode(encoded + "=")
            )
        self._cache = replacement
        self._missing.clear()
        self._updated = self._clock()
