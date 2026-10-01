import re
import secrets
from copy import deepcopy
from typing import TypedDict, cast

X402_VERSION = 2
NETWORK_INFLOW = "inflow:1"
INFLOW_AMOUNT_SCALE = 18
PAYMENT_IDENTIFIER = "payment-identifier"
INFLOW_EIP7702_GAS_SPONSORING = "inflowEip7702GasSponsoring"
_ID_PATTERN = "^[a-zA-Z0-9_-]+$"


class IdentifierDeclaration(TypedDict):
    info: dict[str, object]
    schema: dict[str, object]


def validate_payment_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 16 <= len(value) <= 128
        and re.fullmatch(_ID_PATTERN, value) is not None
    )


def generate_payment_id(prefix: str = "pay_") -> str:
    """Append 32 cryptographically random hexadecimal characters to the prefix."""
    if (
        not isinstance(prefix, str)
        or len(prefix) > 96
        or not re.fullmatch(r"[a-zA-Z0-9_-]*", prefix)
    ):
        raise ValueError(
            "Payment identifier prefix must contain at most 96 ASCII letters, digits, '_' or '-'"
        )
    return prefix + secrets.token_hex(16)


def declare_payment_identifier() -> IdentifierDeclaration:
    return {
        "info": {"required": False},
        "schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "id": {"type": "string", "minLength": 16, "maxLength": 128, "pattern": _ID_PATTERN},
                "required": {"type": "boolean"},
            },
            "required": ["required"],
        },
    }


def _object(value: object) -> dict[str, object]:
    # Extension input is decoded JSON; nested values remain untrusted objects.
    return cast(dict[str, object], value) if isinstance(value, dict) else {}


def read_payment_identifier(value: object) -> IdentifierDeclaration | None:
    """Read a declaration without sharing its mutable data with the caller."""
    fields = _object(value)
    info, schema = _object(fields.get("info")), _object(fields.get("schema"))
    properties = _object(schema.get("properties"))
    identifier = _object(properties.get("id"))
    if not (
        isinstance(info.get("required"), bool)
        and schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema"
        and schema.get("type") == "object"
        and identifier.get("type") == "string"
        and identifier.get("minLength") == 16
        and identifier.get("maxLength") == 128
        and identifier.get("pattern") == _ID_PATTERN
        and _object(properties.get("required")).get("type") == "boolean"
        and schema.get("required") == ["required"]
    ):
        return None
    return {"info": deepcopy(info), "schema": deepcopy(schema)}


def payment_identifier_entry(declaration: object, payment_id: str) -> IdentifierDeclaration | None:
    if not validate_payment_id(payment_id):
        return None
    entry = read_payment_identifier(declaration)
    if entry is not None:
        entry["info"]["id"] = payment_id
    return entry


def declare_sponsorship() -> dict[str, object]:
    return {INFLOW_EIP7702_GAS_SPONSORING: {"info": {"version": "1"}}}


def normalize_decimal_string(value: str) -> str:
    """Remove insignificant zeros without rounding; non-plain decimal strings are unchanged."""
    if re.fullmatch(r"-?[0-9]+(?:\.[0-9]+)?", value) is None:
        return value
    integer, _, fraction = value.removeprefix("-").partition(".")
    integer = integer.lstrip("0") or "0"
    fraction = fraction.rstrip("0")
    if integer == "0" and not fraction:
        return "0"
    result = integer + ("." + fraction if fraction else "")
    return "-" + result if value.startswith("-") else result
