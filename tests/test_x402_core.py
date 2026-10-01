import json
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest
from x402 import schemas

from inflowpay.x402 import (
    INFLOW_AMOUNT_SCALE,
    INFLOW_EIP7702_GAS_SPONSORING,
    NETWORK_INFLOW,
    PAYMENT_IDENTIFIER,
    X402_VERSION,
    PaymentPayload,
    PaymentRequired,
    PaymentRequirements,
    ResourceInfo,
    SettleResponse,
    SupportedKind,
    SupportedResponse,
    VerifyResponse,
    declare_payment_identifier,
    declare_sponsorship,
    generate_payment_id,
    normalize_decimal_string,
    payment_identifier_entry,
    read_payment_identifier,
    validate_payment_id,
)

# Copied unchanged from inflow-specs/fixtures/x402.mjs, x402-core suite.
CASES = json.loads(Path(__file__).with_name("fixtures").joinpath("x402-core.json").read_text())[
    "cases"
]


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_shared_core(case: dict[str, object]) -> None:
    data = cast(dict[str, object], case["input"])
    expected = cast(dict[str, object], case["expect"])["result"]
    before = deepcopy(data)
    match case["operation"]:
        case "x402.core.identifier-valid":
            result: object = validate_payment_id(data["value"])
        case "x402.core.identifier-declaration":
            result = declare_payment_identifier()
        case "x402.core.identifier-entry":
            result = payment_identifier_entry(data["declaration"], str(data["payment_id"]))
        case _:
            pytest.fail(f"Unknown operation: {case['operation']}")
    assert result == expected
    assert data == before


@pytest.mark.parametrize("prefix", ["pay_", "", "A_b-09", "p" * 96])
def test_generated_identifier(prefix: str) -> None:
    values = {generate_payment_id(prefix) for _ in range(100)}
    assert len(values) == 100
    for value in values:
        assert value.startswith(prefix)
        assert len(value) == len(prefix) + 32
        assert validate_payment_id(value)
        assert len(bytes.fromhex(value[len(prefix) :])) == 16


@pytest.mark.parametrize("prefix", [None, 1, "p" * 97, "space here", "é", "pay_\n"])
def test_invalid_prefix(prefix: object) -> None:
    with pytest.raises(ValueError, match="prefix"):
        generate_payment_id(cast(str, prefix))


@pytest.mark.parametrize("value", [False, 123, {}, [], "a" * 16 + "\n"])
def test_invalid_identifier(value: object) -> None:
    assert not validate_payment_id(value)


@pytest.mark.parametrize("value", [None, [], "", {"info": []}, {"info": {"required": 1}}])
def test_invalid_declaration(value: object) -> None:
    assert read_payment_identifier(value) is None


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("info", "required"), None),
        (("schema",), None),
        (("schema", "$schema"), "wrong"),
        (("schema", "type"), "array"),
        (("schema", "properties"), []),
        (("schema", "properties", "id"), None),
        (("schema", "properties", "id", "type"), "integer"),
        (("schema", "properties", "id", "minLength"), 15),
        (("schema", "properties", "id", "maxLength"), 129),
        (("schema", "properties", "id", "pattern"), ".*"),
        (("schema", "properties", "required"), None),
        (("schema", "properties", "required", "type"), "string"),
        (("schema", "required"), []),
        (("schema", "required"), ["id", "required"]),
        (("schema", "required"), "required"),
    ],
)
def test_malformed_schema(path: tuple[str, ...], value: object) -> None:
    declaration = declare_payment_identifier()
    current = cast(dict[str, object], declaration)
    for key in path[:-1]:
        current = cast(dict[str, object], current[key])
    current[path[-1]] = value
    assert read_payment_identifier(declaration) is None
    assert payment_identifier_entry(declaration, "pay_0123456789abcdef") is None


def test_declarations_are_independent() -> None:
    declaration = declare_payment_identifier()
    declaration["info"]["merchant"] = {"name": "shop"}
    declaration["schema"]["title"] = "Identifier"
    original = deepcopy(declaration)
    entry = payment_identifier_entry(declaration, "pay_0123456789abcdef")
    assert entry is not None
    assert entry["info"]["required"] is False
    assert entry["schema"]["title"] == "Identifier"
    cast(dict[str, object], entry["info"]["merchant"])["name"] = "changed"
    cast(dict[str, object], entry["schema"]["properties"]).clear()
    assert declaration == original
    assert "merchant" not in declare_payment_identifier()["info"]
    assert read_payment_identifier(declare_payment_identifier()) is not None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("0", "0"),
        ("-000.000", "0"),
        ("00012.3400", "12.34"),
        ("-0012.3400", "-12.34"),
        ("0012", "12"),
        ("0.00001", "0.00001"),
        (
            "123456789012345678901234567890.123456789012345678900",
            "123456789012345678901234567890.1234567890123456789",
        ),
        ("1e3", "1e3"),
        ("NaN", "NaN"),
        ("", ""),
        ("+1", "+1"),
        ("1.", "1."),
        (".1", ".1"),
        (" 1", " 1"),
        ("1\n", "1\n"),
        ("\u0661", "\u0661"),
    ],
)
def test_decimal(value: str, expected: str) -> None:
    assert normalize_decimal_string(value) == expected


def test_sponsorship_declaration() -> None:
    result = declare_sponsorship()
    assert result == {INFLOW_EIP7702_GAS_SPONSORING: {"info": {"version": "1"}}}
    result.clear()
    assert declare_sponsorship()


def test_models_are_upstream_types() -> None:
    for model in (
        PaymentPayload,
        PaymentRequired,
        PaymentRequirements,
        ResourceInfo,
        SettleResponse,
        SupportedKind,
        SupportedResponse,
        VerifyResponse,
    ):
        assert model is getattr(schemas, model.__name__)
    assert X402_VERSION == 2
    assert NETWORK_INFLOW == "inflow:1"
    assert INFLOW_AMOUNT_SCALE == 18


@pytest.mark.parametrize("scheme", ["balance", "exact", "upto", "custom-scheme"])
def test_upstream_wire_round_trip(scheme: str) -> None:
    requirements = {
        "scheme": scheme,
        "network": NETWORK_INFLOW,
        "asset": "USDC",
        "amount": "1000000000000000000",
        "payTo": "seller-id",
        "maxTimeoutSeconds": 300,
        "extra": {"nested": {"future": ["value"]}},
    }
    wire = {
        "x402Version": 2,
        "accepted": requirements,
        "payload": {"transactionId": "transaction-id", "custom": {"signed": "preserved"}},
        "extensions": {
            PAYMENT_IDENTIFIER: payment_identifier_entry(
                declare_payment_identifier(), "pay_0123456789abcdef"
            ),
            "custom": {"future": [1, 2]},
        },
    }
    before = deepcopy(wire)
    model = PaymentPayload.model_validate(wire)
    assert model.model_dump(by_alias=True, exclude_none=True) == before
    assert wire == before
    required_wire = {"x402Version": 2, "accepts": [requirements], "extensions": wire["extensions"]}
    assert (
        PaymentRequired.model_validate(required_wire).model_dump(by_alias=True, exclude_none=True)
        == required_wire
    )


def test_upstream_optional_fields() -> None:
    requirements = PaymentRequirements.model_validate(
        {
            "scheme": "balance",
            "network": NETWORK_INFLOW,
            "asset": "USDC",
            "amount": "1",
            "payTo": "seller",
            "maxTimeoutSeconds": 30,
        }
    )
    assert requirements.extra == {}
    assert requirements.model_dump(by_alias=True)["extra"] == {}
    supported = SupportedResponse.model_validate(
        {"kinds": [{"x402Version": 2, "scheme": "balance", "network": NETWORK_INFLOW}]}
    )
    assert supported.extensions == []
    assert supported.signers == {}
