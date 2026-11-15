import re
from collections.abc import Callable, Sequence
from typing import cast

from ._wire import WireObject, object_value, string


def _decimal(value: str) -> tuple[str, str]:
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        raise ValueError("max_amount must be a nonnegative decimal string")
    whole, _, fraction = value.partition(".")
    return whole, fraction.rstrip("0")


def _atomic(value: str, decimals: int) -> int:
    whole, fraction = _decimal(value)
    if len(fraction) > decimals:
        raise ValueError("max_amount exceeds currency precision")
    return int(whole + fraction.ljust(decimals, "0"))


def _strings(values: Sequence[str] | None, *, lower: bool) -> tuple[str, ...] | None:
    if values is None:
        return None
    if isinstance(values, (str, bytes)) or any(
        not isinstance(value, str) or not value for value in values
    ):
        raise ValueError("Payment allowlists must be sequences of nonempty strings")
    return tuple(value.lower() if lower else value for value in values)


def controls(
    method: str,
    intent: str,
    *,
    max_amount: str | None,
    max_atomic_amount: int | str | None,
    max_amount_decimals: int | None,
    currencies: Sequence[str] | None,
    expected_recipients: Sequence[str] | None,
    allowed_chain_ids: Sequence[int] | None,
) -> Callable[[WireObject], None]:
    configured = any(
        value is not None
        for value in (
            max_amount,
            max_atomic_amount,
            max_amount_decimals,
            currencies,
            expected_recipients,
            allowed_chain_ids,
        )
    )
    if intent != "charge" and configured:
        raise ValueError("Payment limits apply only to charge intents")
    if method != "tempo" and (max_amount_decimals is not None or allowed_chain_ids is not None):
        raise ValueError("max_amount_decimals and allowed_chain_ids apply only to Tempo")
    if method == "inflow" and max_atomic_amount is not None:
        raise ValueError("InFlow amounts use decimal currency units; use max_amount")
    if method == "card" and (currencies is not None or expected_recipients is not None):
        raise ValueError("CARD supports amount limits only")
    currency_set = _strings(currencies, lower=method == "tempo")
    recipients = _strings(expected_recipients, lower=method == "tempo")
    chains = None if allowed_chain_ids is None else tuple(allowed_chain_ids)
    if chains is not None and any(
        type(chain) is not int or not 0 <= chain <= 9007199254740991 for chain in chains
    ):
        raise ValueError("allowed_chain_ids must contain nonnegative safe integers")
    if max_amount_decimals is not None and (
        type(max_amount_decimals) is not int or not 0 <= max_amount_decimals <= 255
    ):
        raise ValueError("max_amount_decimals must be an integer from 0 to 255")
    atomic_limit = None
    if max_atomic_amount is not None:
        if isinstance(max_atomic_amount, bool) or not re.fullmatch(
            r"[0-9]+", str(max_atomic_amount)
        ):
            raise ValueError("max_atomic_amount must be a nonnegative integer or digit string")
        atomic_limit = int(max_atomic_amount)
    if (
        method == "tempo"
        and (max_amount is not None or atomic_limit is not None)
        and currency_set is None
    ):
        raise ValueError("Tempo amount limits require currencies")
    token_limit = None
    if max_amount is not None:
        _decimal(max_amount)
        if method == "inflow":
            if currency_set is None:
                raise ValueError("max_amount requires currencies")
        else:
            decimals = 2 if method == "card" else max_amount_decimals
            if decimals is None:
                raise ValueError("Tempo max_amount requires max_amount_decimals")
            token_limit = _atomic(max_amount, decimals)

    def check(request: WireObject) -> None:
        if currency_set is not None:
            currency = string(request.get("currency"))
            if (currency.lower() if method == "tempo" else currency) not in currency_set:
                raise ValueError("MPP charge currency is not allowed")
        details = object_value(request.get("methodDetails", {}))
        if chains is not None and details.get("chainId") not in chains:
            raise ValueError("MPP charge chain is not allowed")
        if recipients is not None:
            values = [request.get("recipient")]
            if method == "tempo":
                # Both callers run validate_request before applying limits.
                splits = cast(list[WireObject], details.get("splits", []))
                values.extend(split.get("recipient") for split in splits)
            for value in values:
                recipient = string(value)
                if (recipient.lower() if method == "tempo" else recipient) not in recipients:
                    raise ValueError("MPP charge recipient is not allowed")
        if method == "inflow":
            if max_amount is not None:
                amount = string(request.get("amount"))
                precision = max(len(_decimal(amount)[1]), len(_decimal(max_amount)[1]))
                if _atomic(amount, precision) > _atomic(max_amount, precision):
                    raise ValueError("MPP charge amount exceeds max_amount")
        elif token_limit is not None or atomic_limit is not None:
            amount_atomic = int(string(request.get("amount")))
            if token_limit is not None and amount_atomic > token_limit:
                raise ValueError("MPP charge amount exceeds max_amount")
            if atomic_limit is not None and amount_atomic > atomic_limit:
                raise ValueError("MPP charge amount exceeds max_atomic_amount")

    return check
