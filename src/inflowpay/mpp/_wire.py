from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import TypeAlias, cast

import rfc8785

JsonValue: TypeAlias = bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"] | None
WireObject: TypeAlias = dict[str, JsonValue]


class MppCodecError(ValueError):
    pass


def object_value(value: object) -> WireObject:
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        raise MppCodecError("Expected a JSON object")
    return cast(WireObject, value)


def string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise MppCodecError("Expected a non-empty string")
    return value


def timestamp(value: object) -> str:
    text = string(value)
    if not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", text
    ):
        raise MppCodecError("Expected an RFC 3339 timestamp")
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise MppCodecError("Expected an RFC 3339 timestamp") from error
    return text


def _normalize(value: object) -> JsonValue:
    # InFlow omits null object members; null array elements remain significant.
    if isinstance(value, dict):
        return {
            key: _normalize(item) for key, item in object_value(value).items() if item is not None
        }
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise MppCodecError("Expected a JSON value")


def canonicalize(value: object) -> str:
    try:
        return rfc8785.dumps(_normalize(value)).decode("utf-8")
    except (ValueError, UnicodeError) as error:
        raise MppCodecError("Value cannot be represented as canonical JSON") from error


def encode(value: object) -> str:
    return base64.urlsafe_b64encode(canonicalize(value).encode()).decode().rstrip("=")


def _invalid_constant(value: str) -> None:
    raise MppCodecError("Nonfinite JSON number")


def decode(value: str) -> JsonValue:
    if not re.fullmatch(r"[A-Za-z0-9_-]*={0,2}", value):
        raise MppCodecError("Invalid base64url")
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        return cast(JsonValue, json.loads(raw.decode("utf-8"), parse_constant=_invalid_constant))
    except (ValueError, UnicodeError, binascii.Error) as error:
        raise MppCodecError("Invalid base64url JSON") from error


def validate_challenge(value: object) -> WireObject:
    result = object_value(value)
    for field in ("id", "realm", "method", "intent", "request"):
        string(result.get(field))
    return result


def decode_credential(value: str) -> WireObject:
    credential = object_value(decode(value))
    validate_challenge(credential.get("challenge"))
    object_value(credential.get("payload"))
    if "source" in credential and not isinstance(credential["source"], str):
        raise MppCodecError("Credential source must be a string")
    return credential


def decode_receipt(value: str) -> WireObject:
    receipt = object_value(decode(value))
    for field in ("method", "reference"):
        string(receipt.get(field))
    timestamp(receipt.get("timestamp"))
    if receipt.get("status") != "success":
        raise MppCodecError("Receipt status must be success")
    for field in ("challengeId", "subscriptionId"):
        if field in receipt:
            string(receipt[field])
    if "settlement" in receipt:
        settlement = object_value(receipt["settlement"])
        for field in ("amount", "currency"):
            string(settlement.get(field))
    return receipt


_PARAM = re.compile(r'([!#$%&\'*+.^_`|~0-9A-Za-z-]+)\s*=\s*(?:"((?:\\.|[^"\\])*)"|([^,\s"]+))')
_FIELDS = (
    "id",
    "realm",
    "method",
    "intent",
    "request",
    "expires",
    "description",
    "digest",
    "opaque",
    "header",
)


def _header_text(value: str) -> str:
    if any(ord(char) < 32 and char != "\t" for char in value) or "\x7f" in value:
        raise MppCodecError("Control character in challenge header")
    return value


def render_challenge_header(challenge: Mapping[str, JsonValue]) -> str:
    validate_challenge(dict(challenge))
    parts = []
    for field in _FIELDS:
        if field in challenge:
            item = challenge[field]
            if not isinstance(item, str):
                raise MppCodecError("Challenge header values must be strings")
            value = _header_text(item)
            parts.append(field + '="' + value.replace("\\", "\\\\").replace('"', '\\"') + '"')
    return "Payment " + ", ".join(parts)


def parse_challenge_header(value: str) -> WireObject:
    value = _header_text(value).strip()
    if value[:8].lower() != "payment ":
        raise MppCodecError("Expected a Payment challenge")
    source = value[8:].strip()
    fields: WireObject = {}
    seen: set[str] = set()
    position = 0
    while True:
        match = _PARAM.match(source, position)
        if match is None:
            raise MppCodecError("Malformed challenge parameter")
        key, quoted, bare = match.groups()
        if key in seen:
            raise MppCodecError("Duplicate challenge parameter")
        seen.add(key)
        text = re.sub(r"\\(.)", r"\1", quoted) if quoted is not None else bare
        if key in _FIELDS:
            fields[key] = text
        position = match.end()
        remainder = source[position:]
        if not remainder.strip():
            break
        separator = re.match(r"\s*,\s*", remainder)
        if separator is None or position + separator.end() == len(source):
            raise MppCodecError("Expected another challenge parameter")
        position += separator.end()
    return validate_challenge(fields)


def parse_challenge_headers(values: str | Sequence[str]) -> list[WireObject]:
    result = []
    for value in [values] if isinstance(values, str) else values:
        start = 0
        quoted = escaped = False
        for index, char in enumerate(value):
            if escaped:
                escaped = False
            elif quoted and char == "\\":
                escaped = True
            elif char == '"':
                quoted = not quoted
            elif not quoted and char == "," and re.match(r"\s*Payment\s", value[index + 1 :], re.I):
                result.append(parse_challenge_header(value[start:index]))
                start = index + 1
        if value[start:].strip():
            result.append(parse_challenge_header(value[start:]))
    return result
