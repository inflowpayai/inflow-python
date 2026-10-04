import base64
import hashlib
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import httpx

from ._types import TapRequest, TapVerificationError

_INPUT = re.compile(
    r' *sig2=\( *(?P<components>"[a-z@-]+"(?: +"[a-z@-]+")*) *\)(?P<parameters>[^\r\n]*)'
)
_BASE64 = r"(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}(?:==)?|[A-Za-z0-9+/]{3}=?)?"
_PARAMETER = re.compile(
    r"; *(created|expires|keyid|alg|nonce|tag)(?:=("
    r'"(?:[\x20-\x21\x23-\x5b\x5d-\x7e]|\\["\\])*"'
    r"|-?\d{1,12}\.\d{1,3}|-?\d{1,15}|\?[01]|:" + _BASE64 + r":"
    r"|[A-Za-z*][A-Za-z0-9!#$%&'*+.^_`|~:/-]*))?(?=;|[ \t]*$)",
    re.ASCII,
)
_SIGNATURE = re.compile(r" *sig2=:(" + _BASE64 + r"):[ \t]*")
_COMPONENTS = ("@method", "@authority", "@path", "@query")


def invalid(message: str) -> TapVerificationError:
    return TapVerificationError("SIGNATURE_INPUT_INVALID", message)


@dataclass(frozen=True)
class Parsed:
    components: tuple[str, ...]
    created: int
    expires: int
    keyid: str
    nonce: str
    tag: str
    parameters: str


def parse_input(value: str) -> Parsed:
    match = _INPUT.fullmatch(value)
    if match is None:
        raise invalid("The TAP Signature-Input field is invalid.")
    components = tuple(item[1:-1] for item in re.split(" +", match["components"]))
    if len(set(components)) != len(components):
        raise invalid("The TAP covered components are invalid.")
    parameters: dict[str, str | int | None] = {}
    remaining = match["parameters"].rstrip(" \t")
    while remaining:
        parameter = _PARAMETER.match(remaining)
        if parameter is None:
            raise invalid("The TAP Signature-Input field is invalid.")
        name, encoded = parameter.groups()
        decoded: str | int | None = None
        if encoded is not None:
            if encoded.startswith('"'):
                decoded = re.sub(r'\\(["\\])', r"\1", encoded[1:-1])
            elif re.fullmatch(r"-?[0-9]+", encoded):
                decoded = int(encoded)
        # Structured Field parameters retain their first position and last value, including type.
        parameters[name] = decoded
        remaining = remaining[parameter.end() :]
    created, expires = parameters.get("created"), parameters.get("expires")
    keyid, algorithm = parameters.get("keyid"), parameters.get("alg")
    nonce, tag = parameters.get("nonce"), parameters.get("tag")
    if not (
        isinstance(created, int)
        and isinstance(expires, int)
        and isinstance(keyid, str)
        and keyid
        and algorithm in ("ed25519", "Ed25519")
        and isinstance(nonce, str)
        and nonce
        and isinstance(tag, str)
        and tag in ("agent-browser-auth", "agent-payer-auth")
    ):
        raise invalid("The TAP signature parameters are invalid.")
    serialized = "(" + " ".join('"' + field + '"' for field in components) + ")"
    for name, item in parameters.items():
        serialized += ";" + name + "="
        serialized += (
            '"' + item.replace("\\", "\\\\").replace('"', '\\"') + '"'
            if isinstance(item, str)
            else str(item)
        )
    return Parsed(components, created, expires, keyid, nonce, tag, serialized)


def header(headers: Mapping[str, str | Sequence[str]], name: str) -> str | None:
    entries = headers.multi_items() if isinstance(headers, httpx.Headers) else headers.items()
    found = [value for key, value in entries if key.lower() == name]
    if len(found) != 1:
        return None
    value = found[0]
    if isinstance(value, str):
        return value
    return value[0] if len(value) == 1 else None


def required_header(headers: Mapping[str, str | Sequence[str]], name: str) -> str:
    value = header(headers, name)
    if value is None:
        raise invalid(f"The TAP {name} field is missing or ambiguous.")
    return value


def signature_bytes(value: str) -> bytes:
    match = _SIGNATURE.fullmatch(value)
    if match is None:
        raise invalid("The TAP Signature field is invalid.")
    encoded = match[1]
    return base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True)


def signature_base(request: TapRequest, parsed: Parsed, clock: float) -> bytes:
    required = _COMPONENTS + (
        ("content-digest", "content-type") if request.body is not None else ()
    )
    if set(parsed.components) != set(required):
        raise invalid("The TAP covered components are invalid.")
    if not 0 < parsed.expires - parsed.created <= 480:
        raise TapVerificationError(
            "SIGNATURE_LIFETIME_INVALID", "The TAP signature lifetime is invalid."
        )
    now = math.floor(clock)
    if now < parsed.created:
        raise TapVerificationError("SIGNATURE_NOT_YET_VALID", "The TAP signature is not yet valid.")
    if now >= parsed.expires:
        raise TapVerificationError("SIGNATURE_EXPIRED", "The TAP signature has expired.")
    try:
        url = httpx.URL(request.url)
        if url.scheme not in ("http", "https") or not url.host or url.userinfo or url.fragment:
            raise ValueError("Expected an absolute HTTP request URL")
    except (httpx.InvalidURL, ValueError) as error:
        raise invalid("The TAP request URL is invalid.") from error
    values = {
        "@method": request.method,
        "@authority": url.netloc.decode("ascii"),
        "@path": url.raw_path.split(b"?", 1)[0].decode("ascii"),
        "@query": "?" + url.query.decode("ascii"),
    }
    if request.body is not None:
        content_type = required_header(request.headers, "content-type")
        body = request.body.encode("utf-8") if isinstance(request.body, str) else request.body
        digest = "sha-256=:" + base64.b64encode(hashlib.sha256(body).digest()).decode("ascii") + ":"
        if header(request.headers, "content-digest") != digest:
            raise TapVerificationError(
                "CONTENT_DIGEST_INVALID", "The TAP content digest is invalid."
            )
        values.update({"content-digest": digest, "content-type": content_type})
    lines = [f'"{field}": {values[field]}' for field in parsed.components]
    lines.append(f'"@signature-params": {parsed.parameters}')
    return "\n".join(lines).encode("utf-8")
