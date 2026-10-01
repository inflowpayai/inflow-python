import base64
import json
from copy import deepcopy
from pathlib import Path
from typing import cast

import pytest
from mpp import Challenge

from inflowpay.mpp import (
    MppCodecError,
    WireObject,
    canonicalize,
    decode,
    decode_credential,
    decode_receipt,
    encode,
    from_pympp_challenge,
    parse_challenge_header,
    parse_challenge_headers,
    render_challenge_header,
    to_pympp_challenge,
    validate_payload,
    validate_request,
)

# Generated from inflow-specs 3bcc2a0 fixtures/mpp.mjs, selecting mpp-core.
CASES = json.loads(Path(__file__).with_name("fixtures").joinpath("mpp-core.json").read_text())
CHALLENGE: WireObject = {
    "id": "test-id",
    "realm": "seller.example",
    "method": "inflow",
    "intent": "charge",
    "request": "eyJhbW91bnQiOiIxIn0",
}
RECEIPT: WireObject = {
    "status": "success",
    "method": "inflow",
    "reference": "test-transaction",
    "timestamp": "2026-09-30T00:00:00Z",
}


@pytest.mark.parametrize("case", CASES, ids=[c["id"] for c in CASES])
def test_shared_core(case: dict[str, object]) -> None:
    data = cast(dict[str, object], case["input"])
    expected = cast(dict[str, object], case["expect"])

    def execute() -> object:
        match case["operation"]:
            case "mpp.core.encode":
                return encode(data["value"])
            case "mpp.core.decode":
                return decode(str(data["value"]))
            case "mpp.core.decode-credential":
                return decode_credential(str(data["value"]))
            case "mpp.core.decode-receipt":
                return decode_receipt(str(data["value"]))
            case "mpp.core.parse-challenges":
                return parse_challenge_headers(cast(str | list[str], data["headers"]))
            case _:
                raise AssertionError("Unknown shared operation")

    if "error" in expected:
        with pytest.raises(MppCodecError):
            execute()
    else:
        assert execute() == expected["result"]
        if case["operation"] in (
            "mpp.core.decode",
            "mpp.core.decode-credential",
            "mpp.core.decode-receipt",
        ):
            assert encode(expected["result"]) == data["value"]


def test_canonical_wire() -> None:
    value = {"z": None, "a": [None, {"b": None, "a": True}], "n": -0.0}
    before = deepcopy(value)
    assert canonicalize(value) == '{"a":[null,{"a":true}],"n":0}'
    assert value == before
    assert canonicalize({"\ue000": 1, "😀": 2}) == '{"😀":2,"":1}'
    assert canonicalize({"n": 1e21, "small": 1e-7}) == '{"n":1e+21,"small":1e-7}'
    for item in [None, True, 1, 1.5, "café\n", [1], {"a": False}]:
        assert decode(encode(item)) == item
    assert decode("e30=") == {}


@pytest.mark.parametrize(
    "value", [object(), {1: "bad"}, float("inf"), float("nan"), "\ud800", 2**64]
)
def test_invalid_canonical(value: object) -> None:
    with pytest.raises(MppCodecError):
        canonicalize(value)


@pytest.mark.parametrize("value", ["!", "a", "", "eyI", "_w", "TmFO", "SW5maW5pdHk"])
def test_invalid_decode(value: str) -> None:
    with pytest.raises(MppCodecError):
        decode(value)


def test_wire_extensions() -> None:
    credential = {
        "challenge": {**CHALLENGE, "extension": "kept"},
        "payload": {"proof": [1]},
        "new": 2,
    }
    assert decode_credential(encode(credential)) == credential
    assert decode_credential(encode({**credential, "source": ""}))["source"] == ""
    receipt = {
        **RECEIPT,
        "extension": {"value": 1},
        "externalId": "test",
        "subscriptionId": "sub",
        "challengeId": "ch",
        "settlement": {"amount": "1", "currency": "USD"},
    }
    assert decode_receipt(encode(receipt)) == receipt


@pytest.mark.parametrize(
    "value",
    [
        [],
        {},
        {"challenge": CHALLENGE, "payload": []},
        {"challenge": CHALLENGE, "payload": {}, "source": 1},
        {"challenge": {**CHALLENGE, "id": ""}, "payload": {}},
    ],
)
def test_invalid_credentials(value: object) -> None:
    with pytest.raises(MppCodecError):
        decode_credential(encode(value))


@pytest.mark.parametrize(
    "change",
    [
        {"status": "failed"},
        {"reference": ""},
        {"method": 1},
        {"timestamp": "yesterday"},
        {"timestamp": "2026-99-30T00:00:00Z"},
        {"timestamp": "2026-09-30T00:00:00"},
        {"subscriptionId": ""},
        {"challengeId": 3},
        {"settlement": []},
        {"settlement": {}},
    ],
)
def test_invalid_receipts(change: dict[str, object]) -> None:
    with pytest.raises(MppCodecError):
        decode_receipt(encode({**RECEIPT, **change}))


def test_headers() -> None:
    challenge = {
        **CHALLENGE,
        "description": 'Pay "now", then \\ later\t',
        "digest": "sha-256=x",
        "opaque": "e30",
        "expires": "2099-01-01T00:00:00Z",
    }
    header = render_challenge_header(challenge)
    assert header == (
        'Payment id="test-id", realm="seller.example", method="inflow", intent="charge", '
        'request="eyJhbW91bnQiOiIxIn0", expires="2099-01-01T00:00:00Z", '
        r'description="Pay \"now\", then \\ later'
        '\t", digest="sha-256=x", opaque="e30"'
    )
    assert parse_challenge_headers(["", header + ", " + header]) == [challenge, challenge]
    assert parse_challenge_header(header + ', future="ignored" ') == challenge
    assert parse_challenge_header(
        "payment id=i, realm=r, method=inflow, intent=charge, request=e30"
    ) == {"id": "i", "realm": "r", "method": "inflow", "intent": "charge", "request": "e30"}


@pytest.mark.parametrize(
    "suffix",
    [
        ', id="duplicate"',
        ", bad",
        ', description="unterminated',
        ", ",
        " trailing",
        ', description="bad\r\n"',
        ', description="\x7f"',
    ],
)
def test_invalid_headers(suffix: str) -> None:
    with pytest.raises(MppCodecError):
        parse_challenge_header(render_challenge_header(CHALLENGE) + suffix)


def test_header_required_fields_and_injection() -> None:
    for value in ("Bearer token", "Payment "):
        with pytest.raises(MppCodecError):
            parse_challenge_header(value)
    for field in ("id", "realm", "method", "intent", "request", "description"):
        with pytest.raises(MppCodecError):
            render_challenge_header({**CHALLENGE, field: "\nInjected: 1"})


def test_pympp_boundary() -> None:
    request = base64.urlsafe_b64encode(b'{ "amount" : "1" }').decode().rstrip("=")
    wire: WireObject = {
        **CHALLENGE,
        "request": request,
        "description": "Pay",
        "digest": "hash",
        "expires": "2099-01-01T00:00:00Z",
        "opaque": "eyJrIjoidiJ9",
        "header": "X-Payment",
    }
    upstream = to_pympp_challenge(wire)
    assert isinstance(upstream, Challenge)
    assert upstream.request == {"amount": "1"}
    assert upstream.to_echo().request == request
    assert from_pympp_challenge(upstream) == wire
    assert from_pympp_challenge(to_pympp_challenge(CHALLENGE)) == CHALLENGE
    upstream.request["amount"] = "2"
    with pytest.raises(ValueError):
        from_pympp_challenge(upstream)


def test_pympp_raw_opaque_and_extensions() -> None:
    opaque = base64.urlsafe_b64encode(b'{ "route" : "test" }').decode().rstrip("=")
    wire: WireObject = {**CHALLENGE, "opaque": opaque, "extension": {"test": True}}
    upstream = to_pympp_challenge(wire)
    assert upstream.to_echo().opaque == opaque
    assert from_pympp_challenge(upstream) == wire
    assert upstream.to_echo().request == CHALLENGE["request"]
    plain = Challenge.from_www_authenticate(render_challenge_header(CHALLENGE))
    assert from_pympp_challenge(plain) == CHALLENGE
    assert to_pympp_challenge(CHALLENGE).to_echo().opaque is None
    assert upstream.opaque is not None
    upstream.opaque["route"] = "changed"
    with pytest.raises(MppCodecError):
        upstream.to_echo()


def test_actual_pympp_challenge_authentication() -> None:
    original = Challenge.create(
        secret_key="test-only-secret",
        realm="seller.example",
        method="inflow",
        intent="charge",
        request={"amount": "1", "currency": "USD"},
    )
    wire = from_pympp_challenge(original)
    converted = to_pympp_challenge(wire)
    assert converted.verify("test-only-secret", "seller.example")
    assert not converted.verify("wrong-secret", "seller.example")
    assert converted.to_echo() == original.to_echo()


def test_optional_paths() -> None:
    assert (
        parse_challenge_header(render_challenge_header({**CHALLENGE, "description": ""}))[
            "description"
        ]
        == ""
    )
    with pytest.raises(MppCodecError):
        render_challenge_header({**CHALLENGE, "description": 2})
    assert decode_receipt(encode({**RECEIPT, "challengeId": "test"}))["challengeId"] == "test"
    assert (
        validate_request(
            "inflow",
            "subscription",
            {
                "amount": "1",
                "currency": "USD",
                "periodCount": 1,
                "periodUnit": "month",
                "subscriptionExpires": "2099-01-01T00:00:00Z",
            },
        )["amount"]
        == "1"
    )
    assert (
        validate_request("tempo", "charge", {"amount": "1", "methodDetails": {}})["amount"] == "1"
    )
    assert (
        validate_request("tempo", "charge", {"amount": "1", "methodDetails": {"chainId": 1.5}})[
            "amount"
        ]
        == "1"
    )


@pytest.mark.parametrize(
    "field",
    [
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
    ],
)
@pytest.mark.parametrize(
    "value", ['quoted "value" \\ path', "bad\rvalue", "bad\nvalue", "bad\x00value", "bad\x7fvalue"]
)
def test_header_field_boundaries(field: str, value: str) -> None:
    challenge = {**CHALLENGE, field: value}
    if value.startswith("bad"):
        with pytest.raises(MppCodecError):
            render_challenge_header(challenge)
        with pytest.raises(MppCodecError):
            header = render_challenge_header({**CHALLENGE, field: "placeholder"})
            parse_challenge_header(header.replace('"placeholder"', f'"{value}"'))
    else:
        assert parse_challenge_header(render_challenge_header(challenge)) == challenge


@pytest.mark.parametrize("kind", ["hash", "transaction", "proof"])
@pytest.mark.parametrize("invalid", [None, "", "not-hex", 1])
def test_tempo_proof_requires_its_own_field(kind: str, invalid: object) -> None:
    required = "hash" if kind == "hash" else "signature"
    other = "signature" if kind == "hash" else "hash"
    for payload in [
        {"type": kind, other: "0x01"},
        {"type": kind, other: "0x01", required: invalid},
    ]:
        with pytest.raises(MppCodecError):
            validate_payload("tempo", payload)


@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("memo", ["0x" + "ab" * 32, "0xab", "0x" + "ab" * 33, "0x" + "gg" * 32])
def test_tempo_memo_boundaries(split: bool, memo: str) -> None:
    details: WireObject = {"memo": memo}
    if split:
        details = {"splits": [{"amount": "1", "recipient": "0x" + "1" * 40, "memo": memo}]}
    request = {"amount": "1", "methodDetails": details}
    if memo == "0x" + "ab" * 32:
        assert validate_request("tempo", "charge", request) == request
    else:
        with pytest.raises(MppCodecError):
            validate_request("tempo", "charge", request)


def test_request_success() -> None:
    requests: list[tuple[str, str, WireObject]] = [
        (
            "inflow",
            "charge",
            {
                "amount": "-1.5",
                "currency": "USD",
                "recipient": "11111111-1111-1111-1111-111111111111",
                "methodDetails": {
                    "rail": "instrument",
                    "instrumentId": "22222222-2222-2222-2222-222222222222",
                },
            },
        ),
        (
            "inflow",
            "subscription",
            {
                "amount": "1",
                "currency": "USDC",
                "periodUnit": "minute",
                "periodCount": 5,
                "subscriptionExpires": "2099-01-01T00:00:00Z",
                "externalId": "plan",
                "methodDetails": {},
            },
        ),
        (
            "tempo",
            "charge",
            {
                "amount": "0",
                "currency": "0x" + "1" * 40,
                "recipient": "0x" + "2" * 40,
                "description": "Pay",
                "externalId": "order",
                "methodDetails": {
                    "chainId": 4217,
                    "feePayer": False,
                    "memo": "0x" + "1" * 64,
                    "splits": [{"amount": "1", "recipient": "0x" + "3" * 40}],
                    "supportedModes": ["pull", "push"],
                },
            },
        ),
        ("tempo", "charge", {"amount": "10"}),
    ]
    for method, intent, request in requests:
        original = deepcopy(request)
        result = validate_request(method, intent, request)
        assert result == original and result is not request
        result["amount"] = "changed"
        assert request == original
    assert validate_payload("inflow", {"opaque": "proof"}) == {"opaque": "proof"}
    for kind in ("transaction", "proof", "hash"):
        payload = {
            "type": kind,
            "hash" if kind == "hash" else "signature": "0x01",
            "transactionId": "tx",
        }
        assert validate_payload("tempo", payload) == payload


@pytest.mark.parametrize("count", ["1", "1.0", "1e0"])
def test_subscription_integer_json_representations(count: str) -> None:
    request = json.loads(
        '{"amount":"1","currency":"USD","periodUnit":"month",'
        f'"periodCount":{count},"subscriptionExpires":"2099-01-01T00:00:00Z"}}'
    )
    assert validate_request("inflow", "subscription", request) == request


@pytest.mark.parametrize(
    "method,intent,change",
    [
        ("other", "charge", {}),
        ("tempo", "subscription", {}),
        ("inflow", "charge", {"amount": "1e3"}),
        ("inflow", "charge", {"amount": "1,000"}),
        ("inflow", "charge", {"amount": 1}),
        ("inflow", "charge", {"currency": ""}),
        ("inflow", "charge", {"recipient": "bad"}),
        ("inflow", "charge", {"methodDetails": {"rail": "other"}}),
        ("inflow", "charge", {"methodDetails": {"instrumentId": "bad"}}),
        ("inflow", "subscription", {"amount": "0"}),
        ("inflow", "subscription", {"amount": "-1"}),
        ("inflow", "subscription", {"periodUnit": "other"}),
        ("inflow", "subscription", {"periodCount": True}),
        ("inflow", "subscription", {"periodCount": None}),
        ("inflow", "subscription", {"periodCount": "1"}),
        ("inflow", "subscription", {"periodCount": float("nan")}),
        ("inflow", "subscription", {"periodCount": float("inf")}),
        ("inflow", "subscription", {"periodCount": 0}),
        ("inflow", "subscription", {"periodCount": 1.5}),
        ("inflow", "subscription", {"subscriptionExpires": None}),
        ("inflow", "subscription", {"subscriptionExpires": "2099-01-01"}),
        ("inflow", "subscription", {"periodCount": 2**53}),
        ("inflow", "subscription", {"periodUnit": "minute", "periodCount": 1}),
        ("inflow", "subscription", {"externalId": " "}),
        ("inflow", "subscription", {"externalId": "x" * 129}),
        ("tempo", "charge", {"amount": "01"}),
        ("tempo", "charge", {"recipient": "bad"}),
        ("tempo", "charge", {"methodDetails": {"chainId": True}}),
        ("tempo", "charge", {"methodDetails": {"chainId": float("inf")}}),
        ("tempo", "charge", {"methodDetails": {"feePayer": 1}}),
        ("tempo", "charge", {"methodDetails": {"supportedModes": "pull"}}),
        ("tempo", "charge", {"methodDetails": {"supportedModes": ["other"]}}),
        ("tempo", "charge", {"methodDetails": {"splits": {}}}),
        ("tempo", "charge", {"methodDetails": {"splits": [{"amount": "1.1"}]}}),
    ],
)
def test_request_failures(method: str, intent: str, change: dict[str, object]) -> None:
    request = {
        "amount": "1",
        "currency": "0x" + "1" * 40 if method == "tempo" else "USDC",
        "periodUnit": "month",
        "periodCount": 1,
        "subscriptionExpires": "2099-01-01T00:00:00Z",
        **change,
    }
    with pytest.raises(MppCodecError):
        validate_request(method, intent, request)


@pytest.mark.parametrize(
    "method,payload",
    [
        ("other", {}),
        ("tempo", {"type": "bad"}),
        ("tempo", {"type": "hash"}),
        ("tempo", {"type": "proof", "signature": "nope"}),
    ],
)
def test_invalid_payload(method: str, payload: dict[str, object]) -> None:
    with pytest.raises(MppCodecError):
        validate_payload(method, payload)
