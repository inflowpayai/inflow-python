from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from types import TracebackType
from typing import Self, cast
from uuid import uuid4

from mpp import Credential, Receipt
from mpp.errors import PaymentError

# pympp exposes Validation here but omits an explicit typed re-export in 0.11.0.
from mpp.server import Validation  # type: ignore[attr-defined]

from .._runtime import Client
from ..options import ClientOptions
from ._requests import validate_request
from ._wire import (
    MppCodecError,
    WireObject,
    decode_credential,
    decode_receipt,
    encode,
    object_value,
    string,
)

__all__ = ["MppCredentialProblemError", "MppSellerConfigurationError", "Seller"]


class MppSellerConfigurationError(ValueError):
    pass


class MppCredentialProblemError(PaymentError):
    def __init__(self, problem: object = None) -> None:
        try:
            source = object_value(problem)
            for key in ("type", "title", "detail"):
                string(source.get(key))
            status = source.get("status")
            if type(status) is not int or not 100 <= status <= 599:
                raise MppCodecError("Expected a payment problem")
        except MppCodecError:
            source = {
                "type": "https://paymentauth.org/problems/verification-failed",
                "title": "Payment Verification Failed",
                "status": 402,
                "detail": "The platform returned a malformed credential lifecycle response.",
            }
        self.problem = deepcopy(source)
        self.type = string(source["type"])
        self.title = string(source["title"])
        self.status = cast(int, source["status"])
        super().__init__(string(source["detail"]))

    def to_problem_details(self, challenge_id: str | None = None) -> WireObject:
        result = super().to_problem_details(challenge_id)
        for key in ("hint", "details"):
            if key in self.problem:
                result[key] = deepcopy(self.problem[key])
        extensions = self.problem.get("extensions")
        if isinstance(extensions, dict):
            for key, value in extensions.items():
                if key not in {
                    "type",
                    "title",
                    "status",
                    "detail",
                    "hint",
                    "details",
                    "challengeId",
                }:
                    result[key] = deepcopy(value)
        return result


@dataclass(frozen=True, slots=True)
class _Receipt(Receipt):
    _wire: WireObject = field(default_factory=dict)

    def to_payment_receipt(self) -> str:
        # Preserve platform timestamp precision and method-specific receipt fields.
        return encode(self._wire)


def _wire_credential(credential: Credential) -> WireObject:
    wire = decode_credential(credential.to_authorization()[8:])
    # The platform requires a string; anonymous credentials have no payer DID.
    wire.setdefault("source", "")
    return wire


class Seller:
    name = "charge"

    def __init__(self, client: Client, config: WireObject, method: str) -> None:
        self._client = client
        self._config = deepcopy(config)
        self.method = method

    @classmethod
    async def create(cls, options: ClientOptions, *, method: str = "inflow") -> Self:
        if method not in ("inflow", "tempo"):
            raise MppSellerConfigurationError("Seller supports inflow and tempo charges")
        if options.api_key is None:
            raise MppSellerConfigurationError("Seller setup requires an InFlow Seller API key")
        client = Client(options)
        try:
            config = object_value(await client.request("GET", "/v1/mpp/config", retries=3))
            string(config.get("sellerId"))
            object_value(config.get("featureFlags"))
            if not isinstance(config.get("supportedMethods"), list):
                raise MppCodecError("Expected supportedMethods array")
            return cls(client, config, method)
        except BaseException:
            await client.aclose()
            raise

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    def charge_request(self, request: WireObject) -> WireObject:
        result = deepcopy(request)
        # Use standalone pympp.pay: its high-level routes convert all prices to token units.
        if "decimals" in result:
            raise MppSellerConfigurationError("Supply wire amounts without a decimals field")
        details = object_value(result.get("methodDetails", {}))
        if self.method == "inflow":
            result["recipient"] = self._config["sellerId"]
            result["methodDetails"] = self._rail(string(result.get("currency")), details)
        else:
            string(result.get("currency"))
            string(result.get("recipient"))
            result["methodDetails"] = {
                "feePayer": False,
                "supportedModes": ["pull"],
                **details,
            }
        return validate_request(self.method, "charge", result)

    def _rail(self, currency: str, details: WireObject) -> WireObject:
        methods = self._config["supportedMethods"]
        assert isinstance(methods, list)
        method = next(
            (entry for entry in methods if isinstance(entry, dict) and entry.get("id") == "inflow"),
            {},
        )
        config = object_value(method.get("methodDetails", {}))
        matrix = object_value(config.get("intentCurrencyRails", {}))
        if matrix:
            advertised = object_value(matrix.get("charge", {})).get(currency, [])
        else:
            legacy = object_value(config.get("currencyRails", {})).get(currency)
            advertised = [] if legacy is None else [legacy]
        if not isinstance(advertised, list) or not advertised:
            raise MppSellerConfigurationError(f"Currency {currency} is not supported for charge")
        rail = details.get("rail")
        if rail is None and len(advertised) > 1:
            raise MppSellerConfigurationError(f"Select a rail for currency {currency}")
        selected = next(
            (
                item
                for item in advertised
                if isinstance(item, dict) and (rail is None or item.get("rail") == rail)
            ),
            {},
        )
        rail = selected.get("rail")
        if rail not in ("balance", "instrument"):
            raise MppSellerConfigurationError(f"Unsupported rail for currency {currency}")
        if (
            rail == "instrument"
            and selected.get("instrumentId") == "required"
            and "instrumentId" not in details
        ):
            raise MppSellerConfigurationError(
                f"An instrumentId is required for currency {currency}"
            )
        return {
            "rail": rail,
            **({"instrumentId": details["instrumentId"]} if "instrumentId" in details else {}),
        }

    async def validate(self, credential: Credential, request: WireObject) -> Validation:
        wire = _wire_credential(credential)
        result = await self._client.request(
            "POST", "/v1/mpp/validate", body={"credential": wire}, retries=3
        )
        try:
            response = object_value(result)
            if response.get("success") is not True:
                raise MppCredentialProblemError(response.get("problem"))
            challenge = object_value(wire["challenge"])
            for key, expected in (
                ("challenge", challenge),
                ("credential", wire),
                ("method", challenge["method"]),
                ("intent", challenge["intent"]),
                ("source", wire["source"]),
            ):
                if key not in response or encode(response[key]) != encode(expected):
                    raise MppCodecError("Validation response does not match credential")
            object_value(response.get("request"))
            details = object_value(response.get("details", {}))
        except MppCodecError as error:
            raise MppCredentialProblemError() from error
        return Validation(
            credential=credential,
            details=deepcopy(details),
            intent="charge",
            request=deepcopy(request),
        )

    async def broadcast(self, credential: Credential, request: WireObject) -> Receipt:
        flags = object_value(self._config["featureFlags"])
        headers = (
            {"Idempotency-Key": str(uuid4())} if flags.get("idempotencyKeyEnabled") is True else {}
        )
        result = await self._client.request(
            "POST",
            "/v1/mpp/broadcast",
            body={"credential": _wire_credential(credential)},
            headers=headers,
            retries=3,
        )
        try:
            response = object_value(result)
            if "receipt" not in response:
                raise MppCredentialProblemError(response.get("problem"))
            wire = decode_receipt(encode(response["receipt"]))
            receipt = Receipt.from_payment_receipt(encode(wire))
        except (MppCodecError, ValueError) as error:
            raise MppCredentialProblemError() from error
        return _Receipt(
            status=receipt.status,
            timestamp=receipt.timestamp,
            reference=receipt.reference,
            method=receipt.method,
            external_id=receipt.external_id,
            subscription_id=receipt.subscription_id,
            extra=receipt.extra,
            _wire=wire,
        )
