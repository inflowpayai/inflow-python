from __future__ import annotations

import re
from collections.abc import Sequence
from copy import deepcopy
from typing import Any

from pydantic import Field
from x402.http.types import PaymentOption
from x402.schemas import AssetAmount, SupportedKind
from x402.schemas.base import BaseX402Model

PERMIT2_PROXY = "0x402085c248EeA27D92E8b30b2C58ed07f9E20001"


class Asset(BaseX402Model):
    asset_id: str
    asset_name: str
    currency: str
    blockchain: str
    network: str
    decimals: int = Field(ge=0)
    asset_transfer_method: str | None = None
    permit2_proxy: str | None = None
    token_name: str | None = None
    token_version: str | None = None
    supports_eip2612: bool = False
    supports_eip7702: bool = False


class Wallet(BaseX402Model):
    address: str
    blockchain: str
    fee_payer: str | None = None


class PaymentMethod(BaseX402Model):
    scheme: str
    network: str
    pay_to: str
    decimals: int = Field(ge=0)
    extra: dict[str, Any] = Field(default_factory=dict)


class SellerConfig(BaseX402Model):
    seller_id: str
    assets: list[Asset]
    wallets: list[Wallet]
    payment_methods: list[PaymentMethod]
    supported: list[SupportedKind]


def supports_permit2(asset: Asset) -> bool:
    return (
        asset.network.startswith("eip155:")
        and (asset.permit2_proxy or "").lower() == PERMIT2_PROXY.lower()
    )


def upto_kind(config: SellerConfig, asset: Asset) -> SupportedKind | None:
    if not asset.network.startswith("eip155:") or not asset.permit2_proxy:
        return None
    for kind in config.supported:
        extra = kind.extra or {}
        if (
            kind.x402_version == 2
            and kind.scheme == "upto"
            and kind.network == asset.network
            and extra.get("assetTransferMethod") == "permit2"
            and isinstance(extra.get("facilitatorAddress"), str)
            and extra["facilitatorAddress"]
            and isinstance(extra.get("permit2Proxy"), str)
            and extra["permit2Proxy"]
        ):
            return kind
    return None


def _price(value: str, currency: str | None) -> tuple[str, str, str]:
    matched = re.fullmatch(r"(\$?)([0-9]+)(?:\.([0-9]{1,8}))?(?:\s+([A-Z][A-Z0-9_]*))?", value)
    if matched is None or (matched[1] and matched[4]):
        raise ValueError("Price must be '$1.00', '1.00 USDC', or a plain amount with currency")
    selected = currency if currency is not None else ("USD" if matched[1] else matched[4])
    if not selected:
        raise ValueError("A currency is required for a plain amount")
    return matched[2], matched[3] or "", selected


def _atomic(integer: str, fraction: str, decimals: int) -> str:
    if fraction[decimals:].strip("0"):
        raise ValueError("Price cannot be represented in the asset's decimal precision")
    return (integer + fraction[:decimals].ljust(decimals, "0")).lstrip("0") or "0"


def build_offers(
    config: SellerConfig,
    price: str,
    *,
    currency: str | None = None,
    schemes: Sequence[str] | None = None,
    networks: Sequence[str] | None = None,
    max_timeout_seconds: int = 300,
    permit2: bool = False,
) -> list[PaymentOption]:
    integer, fraction, selected = _price(price, currency)

    def include(scheme: str, network: str) -> bool:
        return (schemes is None or scheme in schemes) and (networks is None or network in networks)

    result: list[PaymentOption] = []
    for wallet in config.wallets:
        for asset in config.assets:
            if asset.blockchain != wallet.blockchain or selected not in ("USD", asset.currency):
                continue
            methods: list[tuple[str, str | None, dict[str, Any]]] = []
            if not permit2 or supports_permit2(asset):
                methods.append(("exact", "permit2" if permit2 else asset.asset_transfer_method, {}))
            kind = upto_kind(config, asset) if schemes is not None and "upto" in schemes else None
            if kind is not None:
                methods.append(("upto", "permit2", kind.extra or {}))
            for scheme, method, kind_extra in methods:
                if not include(scheme, asset.network):
                    continue
                extra: dict[str, Any] = {"assetName": asset.asset_name}
                for key, value in (
                    ("name", asset.token_name),
                    ("version", asset.token_version),
                    ("assetTransferMethod", method),
                    ("feePayer", wallet.fee_payer),
                ):
                    if value is not None:
                        extra[key] = value
                if method == "permit2":
                    if asset.permit2_proxy is not None:
                        extra["permit2Proxy"] = asset.permit2_proxy
                    if asset.supports_eip2612:
                        extra["supportsEip2612"] = True
                    if asset.supports_eip7702:
                        extra["supportsEip7702"] = True
                result.append(
                    PaymentOption(
                        scheme=scheme,
                        network=asset.network,
                        pay_to=wallet.address,
                        price=AssetAmount(
                            asset=asset.asset_id, amount=_atomic(integer, fraction, asset.decimals)
                        ),
                        max_timeout_seconds=max_timeout_seconds,
                        extra={**extra, **deepcopy(kind_extra)},
                    )
                )
    currencies = (
        list(dict.fromkeys(asset.currency for asset in config.assets))
        if selected == "USD"
        else [selected]
    )
    for method_info in config.payment_methods:
        if include(method_info.scheme, method_info.network):
            for item in currencies:
                result.append(
                    PaymentOption(
                        scheme=method_info.scheme,
                        network=method_info.network,
                        pay_to=method_info.pay_to,
                        price=AssetAmount(
                            asset=item, amount=_atomic(integer, fraction, method_info.decimals)
                        ),
                        max_timeout_seconds=max_timeout_seconds,
                        extra={**deepcopy(method_info.extra), "assetName": item},
                    )
                )
    return result
