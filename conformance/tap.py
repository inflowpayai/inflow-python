import asyncio
import base64
from contextlib import AsyncExitStack
from copy import deepcopy
from dataclasses import asdict
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from inflowpay.tap.seller import (
    MemoryTapReplayStore,
    TapKeyResolver,
    TapRequest,
    TapVerificationError,
    TapVerificationFacts,
    TapVerifier,
    VisaTapKeyResolver,
)


class InjectedFailure(Exception):
    pass


async def tap_execute(data: dict[str, Any]) -> dict[str, Any]:
    now = data["steps"][0]["now_ms"] / 1000
    handler_calls = 0
    claim_calls = 0
    memory = MemoryTapReplayStore(lambda: now)

    class Store:
        async def claim(self, keyid: str, nonce: str, expires: int) -> bool:
            nonlocal claim_calls
            claim_calls += 1
            if data.get("store_failure"):
                raise InjectedFailure("CUSTOM_STORE_FAILED")
            return await memory.claim(keyid, nonce, expires)

    class Resolver:
        async def resolve(self, keyid: str, algorithm: str) -> Ed25519PublicKey | None:
            nonlocal now
            if data.get("resolver_failure"):
                raise InjectedFailure("CUSTOM_RESOLVER_FAILED")
            if "resolver_completion_ms" in data:
                now = data["resolver_completion_ms"] / 1000
            if keyid != data["key"]["kid"] or algorithm != "ed25519":
                return None
            return Ed25519PublicKey.from_public_bytes(
                base64.urlsafe_b64decode(data["key"]["x"] + "=")
            )

    async with AsyncExitStack() as stack:
        resolver: TapKeyResolver = Resolver()
        if data.get("resolver") == "http":
            resolver = await stack.enter_async_context(
                VisaTapKeyResolver(
                    url=data["base_url"] + "/keys",
                    clock=lambda: now,
                    cache_ttl=data.get("cache_ttl_ms", 3600000) / 1000,
                    cache_max_age=data.get("cache_max_age_ms", 86400000) / 1000,
                )
            )
        verifier = await stack.enter_async_context(
            TapVerifier(
                key_resolver=resolver,
                replay_store=Store(),
                clock=lambda: now,
            )
        )
        steps = []

        async def handler(facts: TapVerificationFacts) -> None:
            nonlocal handler_calls
            handler_calls += 1
            value = asdict(facts)
            value["coveredComponents"] = list(value.pop("covered_components"))
            accepted.append(value)

        async def verify(request: TapRequest) -> None:
            before = deepcopy(request)
            try:
                await verifier.with_verified(request, handler)
            except TapVerificationError as error:
                rejected.append(error.code)
            except InjectedFailure as error:
                rejected.append(str(error))
            if request != before:
                raise RuntimeError("TAP request was mutated")

        for step in data["steps"]:
            now = step["now_ms"] / 1000
            accepted: list[dict[str, Any]] = []
            rejected: list[str] = []

            requests = [
                TapRequest(
                    method=item["method"],
                    url=item["url"],
                    headers=item["headers"],
                    body=base64.b64decode(item["body_base64"], validate=True)
                    if "body_base64" in item
                    else None,
                )
                for item in step["requests"]
            ]
            await asyncio.gather(*(verify(request) for request in requests))
            steps.append({"accepted": accepted, "rejected": sorted(rejected)})
        return {"steps": steps, "handler_calls": handler_calls, "claim_calls": claim_calls}
