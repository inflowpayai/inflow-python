from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from types import TracebackType
from typing import Generic, Self, TypedDict, TypeVar, cast

from pydantic import BaseModel
from x402.http.types import PaymentOption, RouteConfig
from x402.interfaces import PaymentFlowConfig, SchemeNetworkServer
from x402.schemas import AssetAmount, PaymentRequirements, Price, SupportedKind, SupportedResponse

from .._runtime import Client
from ..options import ClientOptions
from ._core import INFLOW_EIP7702_GAS_SPONSORING, declare_sponsorship
from ._seller import PERMIT2_PROXY, SellerConfig, build_offers, supports_permit2, upto_kind

__all__ = ["SchemeRegistration", "Seller", "SellerConfig"]

_T = TypeVar("_T", bound=BaseModel)


class _Cache(Generic[_T]):
    def __init__(self, client: Client, path: str, model: type[_T]) -> None:
        self.client, self.path, self.model = client, path, model
        self.value: _T | None = None
        self.expires = 0.0
        self.task: asyncio.Task[None] | None = None

    async def get(self, refresh: bool = False) -> _T:
        if refresh or self.value is None or asyncio.get_running_loop().time() >= self.expires:
            if self.task is None:
                self.task = asyncio.create_task(self._fetch())
                self.task.add_done_callback(self._finish)
            # shield() logs late failures after cancellation on Python 3.14,
            # even though _finish observes them. Keep the shared request independent.
            task = self.task
            await asyncio.wait((task,))
            task.result()
        assert self.value is not None
        return self.value.model_copy(deep=True)

    async def _fetch(self) -> None:
        value = self.model.model_validate(await self.client.request("GET", self.path))
        self.value = value
        self.expires = asyncio.get_running_loop().time() + 3600

    def _finish(self, task: asyncio.Task[None]) -> None:
        if not task.cancelled():
            task.exception()
        self.task = None

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)


class SchemeRegistration(TypedDict):
    network: str
    server: SchemeNetworkServer


class _Passthrough:
    def __init__(self, scheme: str, methods: list[str]) -> None:
        self.scheme = scheme
        self.default_asset_transfer_method = methods[0]
        self.payment_flows: Mapping[str, PaymentFlowConfig] = {
            method: {"supported": ("authorization",), "default": "authorization"}
            for method in methods
        }

    def parse_price(self, price: Price, network: str) -> AssetAmount:
        if not isinstance(price, AssetAmount):
            raise ValueError("Use Seller.offers to provide a price in atomic asset units")
        return price.model_copy(deep=True)

    def enhance_payment_requirements(
        self,
        requirements: PaymentRequirements,
        supported_kind: SupportedKind,
        extension_keys: list[str],
    ) -> PaymentRequirements:
        return requirements.model_copy(deep=True)


class Seller:
    def __init__(self, client: Client) -> None:
        self._client = client
        self._config = _Cache(client, "/v1/x402/config", SellerConfig)
        self._supported = _Cache(client, "/v1/x402/supported", SupportedResponse)
        self._closed = False

    @classmethod
    async def create(cls, options: ClientOptions) -> Self:
        if options.api_key is None and options.api_key_provider is None:
            raise ValueError("Seller setup requires an InFlow Seller API key")
        seller = cls(Client(options))
        try:
            await asyncio.gather(seller.config(), seller.get_supported())
            return seller
        except BaseException:
            await seller.aclose()
            raise

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
        await asyncio.gather(self._config.close(), self._supported.close())
        await self._client.aclose()

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("x402 Seller is closed")

    async def config(self, *, refresh: bool = False) -> SellerConfig:
        self._check_open()
        return await self._config.get(refresh)

    async def get_supported(self, *, refresh: bool = False) -> SupportedResponse:
        self._check_open()
        return await self._supported.get(refresh)

    async def get_signer_addresses(self, network: str) -> list[str]:
        signers = (await self.get_supported()).signers
        if network in signers:
            return signers[network]
        namespace, separator, _ = network.partition(":")
        return signers.get(namespace + ":*", []) if namespace and separator else []

    async def offers(
        self,
        price: str,
        *,
        currency: str | None = None,
        schemes: Sequence[str] | None = None,
        networks: Sequence[str] | None = None,
        max_timeout_seconds: int = 300,
    ) -> list[PaymentOption]:
        return build_offers(
            await self.config(),
            price,
            currency=currency,
            schemes=schemes,
            networks=networks,
            max_timeout_seconds=max_timeout_seconds,
        )

    async def route(
        self,
        price: str,
        *,
        currency: str | None = None,
        schemes: Sequence[str] | None = None,
        networks: Sequence[str] | None = None,
        max_timeout_seconds: int = 300,
        permit2: bool = False,
    ) -> RouteConfig:
        offers = build_offers(
            await self.config(),
            price,
            currency=currency,
            schemes=schemes,
            networks=networks,
            max_timeout_seconds=max_timeout_seconds,
            permit2=permit2,
        )
        route = RouteConfig(accepts=offers)
        candidates = [
            offer for offer in offers if (offer.extra or {}).get("assetTransferMethod") == "permit2"
        ]
        if not candidates or not all(
            offer.scheme == "exact"
            and offer.network.startswith("eip155:")
            and str((offer.extra or {}).get("permit2Proxy", "")).lower() == PERMIT2_PROXY.lower()
            for offer in candidates
        ):
            return route
        eip2612 = all(
            (offer.extra or {}).get("supportsEip2612") is True
            and isinstance((offer.extra or {}).get("name"), str)
            and (offer.extra or {})["name"]
            and isinstance((offer.extra or {}).get("version"), str)
            and (offer.extra or {})["version"]
            for offer in candidates
        )
        eip7702 = all((offer.extra or {}).get("supportsEip7702") is True for offer in candidates)
        if not eip2612 and not eip7702:
            return route
        supported = await self.get_supported(refresh=True)

        def supports(require_eip7702: bool) -> bool:
            return all(
                any(
                    kind.x402_version == 2
                    and kind.scheme == offer.scheme
                    and kind.network == offer.network
                    and (not require_eip7702 or (kind.extra or {}).get("supportsEip7702") is True)
                    for kind in supported.kinds
                )
                for offer in candidates
            )

        if eip2612 and "eip2612GasSponsoring" in supported.extensions and supports(False):
            from x402.extensions.eip2612_gas_sponsoring import (
                declare_eip2612_gas_sponsoring_extension,
            )

            route.extensions = deepcopy(declare_eip2612_gas_sponsoring_extension())
        elif eip7702 and INFLOW_EIP7702_GAS_SPONSORING in supported.extensions and supports(True):
            route.extensions = declare_sponsorship()
        return route

    async def scheme_registrations(
        self, *, schemes: Sequence[str] | None = None
    ) -> list[SchemeRegistration]:
        config = await self.config()
        groups: dict[tuple[str, str], list[str]] = {}

        def add(scheme: str, network: str, method: object) -> None:
            if schemes is not None and scheme not in schemes:
                return
            selected = method if isinstance(method, str) else "default"
            methods = groups.setdefault((scheme, network), [])
            if selected not in methods:
                methods.append(selected)

        for asset in config.assets:
            add("exact", asset.network, asset.asset_transfer_method)
            if supports_permit2(asset):
                add("exact", asset.network, "permit2")
            if schemes is not None and "upto" in schemes and upto_kind(config, asset) is not None:
                add("upto", asset.network, "permit2")
        for method in config.payment_methods:
            add(method.scheme, method.network, method.extra.get("assetTransferMethod"))
        result: list[SchemeRegistration] = []
        for (scheme, network), methods in groups.items():
            server: SchemeNetworkServer
            if scheme == "upto":
                from x402.mechanisms.evm.upto import UptoEvmServerScheme

                # Upstream 2.25.0 has an untyped constructor and an inferred flow-map
                # type; the object implements SchemeNetworkServer at runtime.
                server = cast(Callable[[], SchemeNetworkServer], UptoEvmServerScheme)()
            else:
                server = _Passthrough(scheme, methods)
            result.append({"network": network, "server": server})
        return result
