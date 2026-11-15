import json
from typing import Any

import httpx
import pytest
from mpp.runtime import CredentialParams

from inflowpay import ClientOptions
from inflowpay.mpp import WireObject, encode, to_pympp_challenge
from inflowpay.mpp.buyer import BuyerMethod, payment_transport
from test_card_buyer import CHALLENGE, MERCHANT
from test_card_buyer import ready as card_ready
from test_card_seller import PAYLOAD
from test_mpp_buyer import ID, WIRE, ready

TOKEN = "0x" + "ab" * 20
RECIPIENT = "0x" + "cd" * 20


def wire(method: str, amount: str, **extra: Any) -> WireObject:
    if method == "card":
        from inflowpay.mpp import decode

        request = decode(str(CHALLENGE["request"]))
        assert isinstance(request, dict)
        return {**CHALLENGE, "request": encode({**request, "amount": amount, **extra})}
    request = {"amount": amount, "currency": "USD", "recipient": ID}
    if method == "tempo":
        request.update(currency=TOKEN, recipient=RECIPIENT)
    return {**WIRE, "method": method, "request": encode({**request, **extra})}


@pytest.mark.parametrize(
    "method,settings",
    [
        ("inflow", {"max_amount": "1"}),
        ("inflow", {"max_atomic_amount": 1}),
        ("inflow", {"max_amount_decimals": 2}),
        ("inflow", {"allowed_chain_ids": []}),
        ("inflow", {"currencies": "USD"}),
        ("inflow", {"currencies": [""]}),
        ("inflow", {"currencies": [1]}),
        ("inflow", {"max_amount": "-1", "currencies": ["USD"]}),
        ("inflow", {"max_amount": "1e3", "currencies": ["USD"]}),
        ("card", {"max_amount": "0.501"}),
        ("card", {"currencies": ["usd"]}),
        ("card", {"expected_recipients": []}),
        ("card", {"max_atomic_amount": -1}),
        ("card", {"max_atomic_amount": True}),
        ("card", {"max_atomic_amount": "1.5"}),
        ("tempo", {"max_atomic_amount": 1}),
        ("tempo", {"max_amount": "1", "currencies": [TOKEN]}),
        ("tempo", {"max_amount_decimals": -1}),
        ("tempo", {"max_amount_decimals": 256}),
        ("tempo", {"max_amount_decimals": True}),
        ("tempo", {"allowed_chain_ids": [-1]}),
        ("tempo", {"allowed_chain_ids": [True]}),
        ("tempo", {"allowed_chain_ids": [9007199254740992]}),
    ],
)
def test_invalid_limits_fail_at_construction(method: str, settings: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        BuyerMethod(
            ClientOptions(),
            method=method,
            merchant=MERCHANT if method == "card" else None,
            **settings,
        )


def test_subscription_does_not_silently_accept_charge_limits() -> None:
    with pytest.raises(ValueError, match="charge intents"):
        BuyerMethod(ClientOptions(), intent="subscription", currencies=["USD"])


@pytest.mark.parametrize(
    "method,amount,settings,extra,allowed",
    [
        ("inflow", "1.000", {"max_amount": "1", "currencies": ["USD"]}, {}, True),
        ("inflow", "1.00000000000000000001", {"max_amount": "1", "currencies": ["USD"]}, {}, False),
        ("inflow", "0", {"max_amount": "0", "currencies": ["USD"]}, {}, True),
        ("inflow", "1", {"currencies": []}, {}, False),
        ("inflow", "1", {"expected_recipients": [ID]}, {}, True),
        ("inflow", "1", {"expected_recipients": []}, {}, False),
        ("inflow", "99999999999999999999999999999999999999", {}, {}, True),
        ("card", "100", {"max_amount": "1.00"}, {}, True),
        ("card", "101", {"max_amount": "1.00"}, {}, False),
        ("card", "100", {"max_atomic_amount": "100"}, {}, True),
        ("card", "101", {"max_atomic_amount": 100}, {}, False),
        (
            "tempo",
            "1000000",
            {"max_amount": "1", "max_amount_decimals": 6, "currencies": [TOKEN.upper()]},
            {},
            True,
        ),
        (
            "tempo",
            "1000001",
            {"max_amount": "1", "max_amount_decimals": 6, "currencies": [TOKEN]},
            {},
            False,
        ),
        ("tempo", "1", {"max_atomic_amount": 1, "currencies": [TOKEN]}, {}, True),
        ("tempo", "2", {"max_atomic_amount": 1, "currencies": [TOKEN]}, {}, False),
        ("tempo", "1", {"allowed_chain_ids": [42431]}, {"methodDetails": {"chainId": 42431}}, True),
        ("tempo", "1", {"allowed_chain_ids": [42431]}, {}, False),
        ("tempo", "1", {"allowed_chain_ids": []}, {"methodDetails": {"chainId": 42431}}, False),
        ("tempo", "1", {"expected_recipients": [RECIPIENT.upper()]}, {}, True),
        (
            "tempo",
            "1",
            {"expected_recipients": [RECIPIENT]},
            {"methodDetails": {"splits": [{"amount": "1", "recipient": TOKEN}]}},
            False,
        ),
        (
            "tempo",
            "1",
            {"expected_recipients": [RECIPIENT, TOKEN]},
            {"methodDetails": {"splits": [{"amount": "1", "recipient": TOKEN}]}},
            True,
        ),
    ],
)
async def test_limits_apply_to_selection_and_direct_creation(
    method: str, amount: str, settings: dict[str, Any], extra: dict[str, Any], allowed: bool
) -> None:
    selected = wire(method, amount, **extra)
    challenge = to_pympp_challenge(selected)
    calls: list[httpx.Request] = []

    def platform(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        body = json.loads(request.content)
        result = (
            card_ready({"challenge": body["challenge"], "payload": PAYLOAD})
            if method == "card"
            else ready(body["challenge"])
        )
        return httpx.Response(200, json=result)

    async with BuyerMethod(
        ClientOptions(api_key="test", transport=httpx.MockTransport(platform)),
        method=method,
        merchant=MERCHANT if method == "card" else None,
        **settings,
    ) as buyer:
        assert buyer.can_handle_challenge is not None
        assert buyer.can_handle_challenge(challenge) is allowed
        if allowed:
            await buyer.create_credential(CredentialParams(challenge=challenge))
        else:
            with pytest.raises(ValueError):
                await buyer.create_credential(CredentialParams(challenge=challenge))
    assert len(calls) == int(allowed)


async def test_native_transport_selects_affordable_offer_and_snapshots_limits() -> None:
    currencies = ["USD"]
    selected: list[str] = []

    def platform(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        selected.append(body["challenge"]["id"])
        return httpx.Response(200, json=ready(body["challenge"]))

    def seller(request: httpx.Request) -> httpx.Response:
        if "authorization" in request.headers:
            return httpx.Response(200)
        return httpx.Response(
            402,
            headers=[
                (
                    "www-authenticate",
                    to_pympp_challenge(
                        {**wire("inflow", amount), "id": amount}
                    ).to_www_authenticate("seller.example"),
                )
                for amount in ("10", "1")
            ],
        )

    async with BuyerMethod(
        ClientOptions(api_key="test", transport=httpx.MockTransport(platform)),
        max_amount="1",
        currencies=currencies,
    ) as buyer:
        currencies.clear()
        async with httpx.AsyncClient(
            transport=payment_transport([buyer], inner=httpx.MockTransport(seller))
        ) as client:
            assert (await client.get("https://seller.example")).status_code == 200
    assert selected == ["1"]
