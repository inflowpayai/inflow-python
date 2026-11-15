import asyncio
import json
from collections.abc import Sequence

import httpx
import pytest
from mpp.errors import NoMethodFoundError
from mpp.runtime import ChallengeCandidate, CredentialParams

from inflowpay import ClientOptions
from inflowpay.mpp import decode_credential, to_pympp_challenge
from inflowpay.mpp.buyer import BuyerMethod, PaymentOptions, payment_transport
from test_card_buyer import CHALLENGE, MERCHANT
from test_card_buyer import ready as card_ready
from test_mpp_buyer import ID, WIRE, ready, subscription


async def test_request_options_are_isolated_through_native_transport() -> None:
    challenge = to_pympp_challenge(WIRE)
    received: list[dict[str, object]] = []
    arrived = asyncio.Event()

    async def platform(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        received.append(body["options"])
        if len(received) == 3:
            arrived.set()
        await arrived.wait()
        return httpx.Response(200, json=ready())

    def seller(request: httpx.Request) -> httpx.Response:
        if "authorization" in request.headers:
            return httpx.Response(200)
        return httpx.Response(
            402, headers={"www-authenticate": challenge.to_www_authenticate("seller.example")}
        )

    alternate = "22222222-2222-4222-8222-222222222222"
    override = PaymentOptions(instrument_id=alternate)
    async with (
        BuyerMethod(
            ClientOptions(api_key="test", transport=httpx.MockTransport(platform)), instrument_id=ID
        ) as buyer,
        httpx.AsyncClient(
            transport=payment_transport([buyer], inner=httpx.MockTransport(seller))
        ) as client,
    ):
        responses = await asyncio.gather(
            client.get("https://seller.example/default"),
            client.get("https://seller.example/override", extensions={"inflow.payment": override}),
            client.get(
                "https://seller.example/clear",
                extensions={"inflow.payment": PaymentOptions(instrument_id=None)},
            ),
        )
        assert all(response.status_code == 200 for response in responses)
        assert {tuple(item.items()) for item in received} == {
            (("instrumentId", ID),),
            (("instrumentId", alternate),),
            (),
        }
        assert override == {"instrument_id": alternate}


@pytest.mark.parametrize("clear", [False, True])
async def test_subscription_default_can_be_cleared(clear: bool) -> None:
    paths: list[str] = []
    challenge = subscription()

    def platform(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(200, json=ready(json.loads(request.content)["challenge"]))

    async with BuyerMethod(
        ClientOptions(api_key="test", transport=httpx.MockTransport(platform)),
        intent="subscription",
        subscription_id=ID,
    ) as buyer:
        await buyer.create_credential(
            CredentialParams(
                challenge=challenge,
                context=PaymentOptions(subscription_id=None) if clear else PaymentOptions(),
            )
        )
    assert paths == ["/v1/transactions/mpp" if clear else f"/v1/subscriptions/{ID}/authorize"]


@pytest.mark.parametrize(
    "options",
    [
        {"unknown": True},
        {"instrument_id": "invalid"},
        {"instrument_id": 3},
        {"subscription_id": ID},
        {"merchant": {"name": "wrong method"}},
        [],
    ],
)
async def test_invalid_options_fail_before_platform_request(options: object) -> None:
    def platform(request: httpx.Request) -> httpx.Response:
        pytest.fail("Invalid options must not start a payment")

    async with BuyerMethod(
        ClientOptions(api_key="test", transport=httpx.MockTransport(platform))
    ) as buyer:
        with pytest.raises(ValueError):
            await buyer.create_credential(
                CredentialParams(challenge=to_pympp_challenge(WIRE), context=options)
            )


@pytest.mark.parametrize("reject", [False, True])
async def test_native_ordering_and_alternate_payment_header(reject: bool) -> None:
    wires = [{**WIRE, "id": value, "header": "Payment"} for value in ("first", "second")]
    calls: list[str] = []

    def platform(request: httpx.Request) -> httpx.Response:
        challenge = json.loads(request.content)["challenge"]
        calls.append(challenge["id"])
        assert challenge["header"] == "Payment"
        return httpx.Response(200, json=ready(challenge))

    def seller(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer application-token"
        if "payment" in request.headers:
            credential = decode_credential(request.headers["payment"][8:])
            echoed = credential["challenge"]
            assert isinstance(echoed, dict)
            assert echoed["id"] == "second"
            assert echoed["header"] == "Payment"
            return httpx.Response(200)
        return httpx.Response(
            402,
            headers=[
                ("www-authenticate", to_pympp_challenge(wire).to_www_authenticate("seller.example"))
                for wire in wires
            ],
        )

    async def order(candidates: Sequence[ChallengeCandidate]) -> Sequence[ChallengeCandidate]:
        return [] if reject else list(reversed(candidates))

    async with (
        BuyerMethod(
            ClientOptions(api_key="test", transport=httpx.MockTransport(platform))
        ) as buyer,
        httpx.AsyncClient(
            transport=payment_transport(
                [buyer], inner=httpx.MockTransport(seller), order_challenges=order
            )
        ) as client,
    ):
        if reject:
            with pytest.raises(NoMethodFoundError):
                await client.get(
                    "https://seller.example", headers={"authorization": "Bearer application-token"}
                )
        else:
            assert (
                await client.get(
                    "https://seller.example", headers={"authorization": "Bearer application-token"}
                )
            ).status_code == 200
    assert calls == ([] if reject else ["second"])


async def test_request_cancellation_does_not_cancel_other_payment() -> None:
    polling = asyncio.Event()
    paths: list[str] = []
    challenge = to_pympp_challenge(WIRE)

    async def platform(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/cancel"):
            return httpx.Response(204)
        if request.method == "POST":
            if json.loads(request.content)["options"].get("instrumentId") == ID:
                return httpx.Response(
                    200,
                    json={
                        "state": "pending",
                        "transactionId": "waiting",
                        "approvalId": "approval",
                        "retryAfterSeconds": 0,
                    },
                )
            await polling.wait()
            return httpx.Response(200, json=ready())
        polling.set()
        await asyncio.Event().wait()
        raise AssertionError("Cancelled polling must not complete")

    def seller(request: httpx.Request) -> httpx.Response:
        if "authorization" in request.headers:
            return httpx.Response(200)
        return httpx.Response(
            402, headers={"www-authenticate": challenge.to_www_authenticate("seller.example")}
        )

    async with (
        BuyerMethod(
            ClientOptions(api_key="test", transport=httpx.MockTransport(platform))
        ) as buyer,
        httpx.AsyncClient(
            transport=payment_transport([buyer], inner=httpx.MockTransport(seller))
        ) as client,
    ):
        waiting = asyncio.create_task(
            client.get(
                "https://seller.example/wait",
                extensions={"inflow.payment": PaymentOptions(instrument_id=ID)},
            )
        )
        other = asyncio.create_task(client.get("https://seller.example/other"))
        await asyncio.wait_for(polling.wait(), 2)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert (await other).status_code == 200
    assert paths.count("/v1/approvals/approval/cancel") == 1


async def test_card_request_merchant_is_copied_and_validated() -> None:
    merchant = {**MERCHANT, "name": "Selected seller"}
    started, release = asyncio.Event(), asyncio.Event()

    async def platform(request: httpx.Request) -> httpx.Response:
        started.set()
        await release.wait()
        assert json.loads(request.content)["options"]["merchant"]["name"] == "Selected seller"
        return httpx.Response(200, json=card_ready())

    async with BuyerMethod(
        ClientOptions(api_key="test", transport=httpx.MockTransport(platform)),
        method="card",
        merchant=MERCHANT,
    ) as buyer:
        task = asyncio.ensure_future(
            buyer.create_credential(
                CredentialParams(
                    challenge=to_pympp_challenge(CHALLENGE),
                    context=PaymentOptions(merchant=merchant),
                )
            )
        )
        await asyncio.wait_for(started.wait(), 2)
        merchant["name"] = "Mutated caller value"
        release.set()
        await task
        for options in (
            PaymentOptions(merchant=None),
            PaymentOptions(instrument_id=ID.replace("-", "")),
        ):
            with pytest.raises(ValueError):
                await buyer.create_credential(
                    CredentialParams(challenge=to_pympp_challenge(CHALLENGE), context=options)
                )


async def test_subscription_rejects_instrument_override() -> None:
    async with BuyerMethod(ClientOptions(), intent="subscription") as buyer:
        with pytest.raises(ValueError, match="instrument_id applies"):
            await buyer.create_credential(
                CredentialParams(challenge=subscription(), context=PaymentOptions(instrument_id=ID))
            )
