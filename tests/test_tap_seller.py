import asyncio
import base64
import hashlib
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import replace
from typing import cast
from urllib.parse import urlsplit

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa

from inflowpay.tap.seller import (
    MemoryTapReplayStore,
    TapRequest,
    TapVerificationError,
    TapVerificationFacts,
    TapVerifier,
)

NOW = 1800000000
KEY = ed25519.Ed25519PrivateKey.from_private_bytes(b"\x11" * 32)
FIELDS = ("@method", "@authority", "@path", "@query")


def signed(
    *,
    body: bytes | str | None = None,
    method: str = "GET",
    url: str = "https://merchant.example:8443/catalog%2Fitems?q=red%20shoes&kind=a&kind=b",
    fields: tuple[str, ...] | None = None,
    params: dict[str, str | int] | None = None,
    wire: str | None = None,
) -> TapRequest:
    parameters = (
        params
        if params is not None
        else {
            "created": NOW,
            "expires": NOW + 300,
            "keyid": "test-key",
            "alg": "ed25519",
            "nonce": "nonce",
            "tag": "agent-browser-auth",
        }
    )
    fields = (
        fields
        if fields is not None
        else FIELDS + (("content-digest", "content-type") if body is not None else ())
    )
    headers = {}
    if body is not None:
        raw = body.encode() if isinstance(body, str) else body
        headers = {
            "content-type": "application/json",
            "content-digest": "sha-256=:"
            + base64.b64encode(hashlib.sha256(raw).digest()).decode()
            + ":",
        }
    canonical = (
        "("
        + " ".join(json.dumps(field) for field in fields)
        + ")"
        + "".join(";" + name + "=" + json.dumps(value) for name, value in parameters.items())
    )
    parsed = urlsplit(url)
    values = {
        "@method": method,
        "@authority": parsed.netloc,
        "@path": parsed.path or "/",
        "@query": "?" + parsed.query,
        **headers,
    }
    base = "\n".join(
        [
            *(f'"{field}": {values.get(field, "")}' for field in fields),
            f'"@signature-params": {canonical}',
        ]
    )
    headers["signature-input"] = "sig2=" + (wire if wire is not None else canonical)
    headers["signature"] = "sig2=:" + base64.b64encode(KEY.sign(base.encode())).decode() + ":"
    return TapRequest(method=method, url=url, headers=headers, body=body)


class Resolver:
    calls = 0

    async def resolve(self, keyid: str, algorithm: str) -> ed25519.Ed25519PublicKey | None:
        self.calls += 1
        assert algorithm == "ed25519"
        return KEY.public_key() if keyid == "test-key" else None


class Store(MemoryTapReplayStore):
    calls = 0

    async def claim(self, keyid: str, nonce: str, expires: int) -> bool:
        self.calls += 1
        return await super().claim(keyid, nonce, expires)


async def check_error(request: TapRequest, code: str, *, now: float = NOW) -> None:
    store = Store(lambda: now)
    calls = []

    async def handler(facts: TapVerificationFacts) -> None:
        calls.append(facts)

    before = deepcopy(request)
    async with TapVerifier(
        key_resolver=Resolver(), replay_store=store, clock=lambda: now
    ) as verifier:
        with pytest.raises(TapVerificationError) as error:
            await verifier.with_verified(request, handler)
    assert error.value.code == code
    assert not calls and store.calls == 0
    assert request == before


@pytest.mark.parametrize("body", [None, b"", b"{}", '{"name":"café"}'])
@pytest.mark.parametrize("tag", ["agent-browser-auth", "agent-payer-auth"])
@pytest.mark.parametrize("algorithm", ["ed25519", "Ed25519"])
async def test_real_signatures_and_replay(
    body: bytes | str | None, tag: str, algorithm: str
) -> None:
    params: dict[str, str | int] = {
        "tag": tag,
        "nonce": 'quote"slash\\',
        "expires": NOW + 300,
        "created": NOW,
        "alg": algorithm,
        "keyid": "test-key",
    }
    request = signed(body=body, params=params)
    before = deepcopy(request)
    async with TapVerifier(key_resolver=Resolver(), clock=lambda: NOW) as verifier:
        facts = await verifier.verify(request)
        assert facts.verified and facts.algorithm == "ed25519"
        assert facts.intent == ("pay" if tag == "agent-payer-auth" else "browse")
        assert facts.nonce == 'quote"slash\\'
        with pytest.raises(TapVerificationError, match="nonce"):
            await verifier.verify(request)
    assert request == before


@pytest.mark.parametrize("headers", ["httpx", "list", "mixed-case"])
async def test_supported_header_containers(headers: str) -> None:
    request = signed()
    values: Mapping[str, str | Sequence[str]]
    if headers == "httpx":
        values = httpx.Headers(cast(dict[str, str], request.headers))
    elif headers == "list":
        values = {key: [cast(str, value)] for key, value in request.headers.items()}
    else:
        values = {key.upper(): value for key, value in request.headers.items()}
    async with TapVerifier(key_resolver=Resolver(), clock=lambda: NOW) as verifier:
        assert (await verifier.verify(replace(request, headers=values))).verified


@pytest.mark.parametrize(
    "value",
    [
        "",
        "sig1=()",
        "sig2=()",
        'sig2=("@method" "@method")',
        'sig2=("@method");created',
        'sig2=("@method");created=?1',
        'sig2=("@method");created=1.0',
        'sig2=("@method");created=invalid',
        'sig2=("@method");created=:YWJj:',
        'sig2=("@method");unknown=1',
        'sig2=("@method");created=\u0661',
        'sig2=("@method");nonce="bad\\n"',
        'sig2=("@method");nonce="bad\n"',
    ],
)
async def test_malformed_input(value: str) -> None:
    request = signed()
    await check_error(
        replace(request, headers={**request.headers, "signature-input": value}),
        "SIGNATURE_INPUT_INVALID",
    )


@pytest.mark.parametrize("wire", ["boolean", "integer", "decimal", "bytes", "token", "string"])
async def test_duplicate_parameter_last_value_first_position(wire: str) -> None:
    request = signed()
    canonical = cast(str, request.headers["signature-input"])[5:]
    old = {
        "boolean": "",
        "integer": "=0",
        "decimal": "=1.5",
        "bytes": "=:YWJj:",
        "token": "=test",
        "string": '="bad"',
    }[wire]
    value = canonical.replace(f";created={NOW}", f";created{old};created={NOW}")
    value = value.replace(f";expires={NOW + 300}", f"; expires=0{NOW + 300}") + " \t"
    request = replace(request, headers={**request.headers, "signature-input": "  sig2=" + value})
    async with TapVerifier(key_resolver=Resolver(), clock=lambda: NOW) as verifier:
        assert (await verifier.verify(request)).verified


@pytest.mark.parametrize(
    "field,value",
    [
        ("created", "1"),
        ("expires", "1"),
        ("keyid", ""),
        ("alg", "rsa"),
        ("nonce", ""),
        ("tag", "unknown"),
        ("keyid", 1),
        ("nonce", 1),
        ("tag", 1),
    ],
)
async def test_effective_parameter_types(field: str, value: str | int) -> None:
    request = signed()
    wire = cast(str, request.headers["signature-input"]) + ";" + field + "=" + json.dumps(value)
    await check_error(
        replace(request, headers={**request.headers, "signature-input": wire}),
        "SIGNATURE_INPUT_INVALID",
    )


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("created", NOW + 300, "SIGNATURE_LIFETIME_INVALID"),
        ("expires", NOW + 481, "SIGNATURE_LIFETIME_INVALID"),
        ("created", NOW + 1, "SIGNATURE_NOT_YET_VALID"),
        ("expires", NOW, "SIGNATURE_LIFETIME_INVALID"),
    ],
)
async def test_time_intervals(field: str, value: int, code: str) -> None:
    request = signed()
    wire = cast(str, request.headers["signature-input"]) + f";{field}={value}"
    await check_error(replace(request, headers={**request.headers, "signature-input": wire}), code)


async def test_exact_expiration_and_precreation() -> None:
    await check_error(signed(), "SIGNATURE_EXPIRED", now=NOW + 300)
    await check_error(signed(), "SIGNATURE_NOT_YET_VALID", now=NOW - 0.1)


@pytest.mark.parametrize("field", ["@method", "@path", "@query", "@authority"])
async def test_tamper_bound_values(field: str) -> None:
    request = signed()
    modified = {
        "@method": replace(request, method="get"),
        "@path": replace(request, url=request.url.replace("catalog%2Fitems", "catalog/items")),
        "@query": replace(request, url=request.url.replace("kind=a&kind=b", "kind=b&kind=a")),
        "@authority": replace(request, url=request.url.replace(":8443", ":8444")),
    }[field]
    await check_error(modified, "SIGNATURE_INVALID")


@pytest.mark.parametrize("fields", [FIELDS[:-1], (*FIELDS, "other"), (*FIELDS, "content-type")])
async def test_exact_components(fields: tuple[str, ...]) -> None:
    await check_error(signed(fields=fields), "SIGNATURE_INPUT_INVALID")


@pytest.mark.parametrize("value", ["sig1=:YWJj:", "sig2=:!:", "sig2=:YWJj:", "sig2=::"])
async def test_signature_encoding(value: str) -> None:
    request = signed()
    code = (
        "SIGNATURE_INPUT_INVALID" if value in ("sig1=:YWJj:", "sig2=:!:") else "SIGNATURE_INVALID"
    )
    await check_error(replace(request, headers={**request.headers, "signature": value}), code)


@pytest.mark.parametrize(
    "url",
    [
        "/relative",
        "ftp://host/path",
        "https:///",
        "https://user@host/",
        "https://host/#fragment",
        "https://host:bad/",
        "https://host/\n",
    ],
)
async def test_invalid_request_url(url: str) -> None:
    await check_error(replace(signed(), url=url), "SIGNATURE_INPUT_INVALID")


@pytest.mark.parametrize(
    "field", ["signature-input", "signature", "content-type", "content-digest"]
)
@pytest.mark.parametrize("kind", ["missing", "duplicate-case", "multiple", "empty-list", "httpx"])
async def test_ambiguous_and_missing_headers(field: str, kind: str) -> None:
    request = signed(body=b"{}")
    headers: dict[str, str | Sequence[str]] = dict(request.headers)
    original = cast(str, headers[field])
    if kind == "missing":
        del headers[field]
    elif kind == "duplicate-case":
        headers[field.upper()] = original
    elif kind in ("multiple", "empty-list"):
        headers[field] = [original, original] if kind == "multiple" else []
    else:
        headers = cast(
            dict[str, str | Sequence[str]],
            httpx.Headers(
                [
                    *((key, cast(str, value)) for key, value in request.headers.items()),
                    (field, original),
                ]
            ),
        )
    await check_error(
        replace(request, headers=headers),
        "CONTENT_DIGEST_INVALID" if field == "content-digest" else "SIGNATURE_INPUT_INVALID",
    )


async def test_body_and_signature_failures_do_not_consume_nonce() -> None:
    request = signed(body=b"{}")
    await check_error(replace(request, body=b"[]"), "CONTENT_DIGEST_INVALID")
    async with TapVerifier(key_resolver=Resolver(), clock=lambda: NOW) as verifier:
        with pytest.raises(TapVerificationError):
            await verifier.verify(replace(request, method="POST"))
        assert (await verifier.verify(request)).verified


async def test_unknown_and_wrong_type_keys() -> None:
    request = signed()
    wire = cast(str, request.headers["signature-input"]).replace(
        'keyid="test-key"', 'keyid="unknown"'
    )
    await check_error(
        replace(request, headers={**request.headers, "signature-input": wire}), "KEY_NOT_FOUND"
    )

    class WrongKey:
        async def resolve(self, keyid: str, algorithm: str) -> ed25519.Ed25519PublicKey:
            # Deliberately violate the custom implementation's declared return type.
            return cast(
                ed25519.Ed25519PublicKey, rsa.generate_private_key(65537, 2048).public_key()
            )

    async with TapVerifier(key_resolver=WrongKey(), clock=lambda: NOW) as verifier:
        with pytest.raises(TapVerificationError) as error:
            await verifier.verify(request)
    assert error.value.code == "SIGNATURE_INVALID"


async def test_custom_failures_and_cancellation_do_not_invoke_handler() -> None:
    class FailedResolver:
        async def resolve(self, keyid: str, algorithm: str) -> ed25519.Ed25519PublicKey:
            raise LookupError("application key failure")

    class FailedStore:
        async def claim(self, keyid: str, nonce: str, expires: int) -> bool:
            raise LookupError("application store failure")

    async def handler(facts: TapVerificationFacts) -> None:
        pytest.fail("Unverified request reached handler")

    async with TapVerifier(key_resolver=FailedResolver(), clock=lambda: NOW) as verifier:
        with pytest.raises(LookupError, match="key failure"):
            await verifier.with_verified(signed(), handler)
    async with TapVerifier(
        key_resolver=Resolver(), replay_store=FailedStore(), clock=lambda: NOW
    ) as verifier:
        with pytest.raises(LookupError, match="store failure"):
            await verifier.with_verified(signed(), handler)


async def test_late_resolution_and_concurrent_replay() -> None:
    now: float = NOW

    class SlowResolver(Resolver):
        async def resolve(self, keyid: str, algorithm: str) -> ed25519.Ed25519PublicKey | None:
            nonlocal now
            await asyncio.sleep(0)
            now = NOW + 300
            return await super().resolve(keyid, algorithm)

    async with TapVerifier(key_resolver=SlowResolver(), clock=lambda: now) as verifier:
        assert (await verifier.verify(signed())).verified
    async with TapVerifier(key_resolver=Resolver(), clock=lambda: NOW) as verifier:
        outcomes = await asyncio.gather(
            *(verifier.verify(signed()) for _ in range(20)), return_exceptions=True
        )
        assert sum(isinstance(item, TapVerificationFacts) for item in outcomes) == 1
        assert all(
            isinstance(item, TapVerificationFacts)
            or (isinstance(item, TapVerificationError) and item.code == "NONCE_REPLAYED")
            for item in outcomes
        )


async def test_store_expiry_and_tuple_keys() -> None:
    now = NOW
    store = MemoryTapReplayStore(lambda: now)
    assert await store.claim("a\x00b", "c", NOW + 1)
    assert await store.claim("a", "b\x00c", NOW + 1)
    assert not await store.claim("a", "b\x00c", NOW + 1)
    now += 1
    assert await store.claim("a", "b\x00c", NOW + 2)


async def test_default_lifecycle_and_closed_use() -> None:
    async with TapVerifier() as verifier:
        with pytest.raises(TapVerificationError):
            await verifier.verify(TapRequest(method="GET", url="https://host/", headers={}))
    await verifier.aclose()
    with pytest.raises(RuntimeError, match="closed"):
        await verifier.verify(signed())
    with pytest.raises(RuntimeError, match="closed"):
        await verifier.__aenter__()


async def test_missing_signature_does_not_resolve_keys() -> None:
    resolver = Resolver()
    request = signed()
    headers = dict(request.headers)
    del headers["signature"]
    async with TapVerifier(key_resolver=resolver, clock=lambda: NOW) as verifier:
        with pytest.raises(TapVerificationError):
            await verifier.verify(replace(request, headers=headers))
    assert resolver.calls == 0


@pytest.mark.parametrize("boundary", ["resolver", "store"])
async def test_cancellation_propagates_without_handler(boundary: str) -> None:
    started = asyncio.Event()
    calls = []

    class WaitingResolver(Resolver):
        async def resolve(self, keyid: str, algorithm: str) -> ed25519.Ed25519PublicKey | None:
            if boundary == "resolver":
                started.set()
                await asyncio.Event().wait()
            return await super().resolve(keyid, algorithm)

    class WaitingStore:
        async def claim(self, keyid: str, nonce: str, expires: int) -> bool:
            started.set()
            await asyncio.Event().wait()
            return True

    async def handler(facts: TapVerificationFacts) -> None:
        calls.append(facts)

    async with TapVerifier(
        key_resolver=WaitingResolver(), replay_store=WaitingStore(), clock=lambda: NOW
    ) as verifier:
        task = asyncio.create_task(verifier.with_verified(signed(), handler))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert not calls


async def test_handler_result_and_error_propagation() -> None:
    async def handler(facts: TapVerificationFacts) -> str:
        return facts.keyid

    async def fail(facts: TapVerificationFacts) -> None:
        raise LookupError("handler failure")

    async with TapVerifier(key_resolver=Resolver(), clock=lambda: NOW) as verifier:
        assert (
            await verifier.with_verified(signed(url="https://merchant.example/"), handler)
            == "test-key"
        )
    async with TapVerifier(key_resolver=Resolver(), clock=lambda: NOW) as verifier:
        with pytest.raises(LookupError, match="handler failure"):
            await verifier.with_verified(signed(), fail)
